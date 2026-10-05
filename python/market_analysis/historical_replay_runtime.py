"""Local, versioned operational checkpoints for the single scientific replay core."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

from .historical_market_state_study_json import canonical_study_json
from .historical_replay import (
    HISTORICAL_REPLAY_RUNTIME_VERSION, HistoricalMarketReplayPoint,
    HistoricalReplayRuntimeState, HistoricalReplayTrade, canonical_replay_point_id,
)
from .movement import (
    MarketObservation, MovementBucket, MovementBucketEngineState,
    MovementPendingBucketState,
)
from .movement_classifier import SymbolSourceTimeEvidence
from .movement_metrics import (
    BreadthSide, ExcludedSymbol, MarketMovementEvaluation,
    MarketMovementWindowResult, Metric, SymbolMovementResult,
    WindowAggregates, WindowBreadth,
)

REPLAY_CHECKPOINT_SCHEMA_VERSION = "historical-replay-checkpoint-v1"
REPLAY_POINT_CHUNK_SCHEMA_VERSION = "historical-replay-points-v1"
_ALLOWED = frozenset((
    HistoricalMarketReplayPoint, HistoricalReplayRuntimeState,
    HistoricalReplayTrade,
    MarketObservation, MovementBucket, MovementBucketEngineState,
    MovementPendingBucketState, SymbolSourceTimeEvidence,
    BreadthSide, ExcludedSymbol, MarketMovementEvaluation,
    MarketMovementWindowResult, Metric, SymbolMovementResult,
    WindowAggregates, WindowBreadth,
))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_bytes(value) -> bytes:
    return (canonical_study_json(value) + "\n").encode("utf-8")


def _strict_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate checkpoint JSON field")
        result[key] = value
    return result


def _read_json_bytes(data: bytes):
    try:
        return json.loads(data.decode("utf-8"), object_pairs_hook=_strict_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(
                              ValueError(f"invalid checkpoint constant: {value}")))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed checkpoint JSON") from exc


@lru_cache(maxsize=None)
def _dataclass_metadata(cls):
    # Type variables remain unresolved here: substitutions belong to each call.
    declared = fields(cls)
    return declared, frozenset(item.name for item in declared), get_type_hints(cls)


def _decode(annotation, value, substitutions=None):
    """Reconstruct only allowlisted replay value types, checking every field."""
    substitutions = substitutions or {}
    if annotation in substitutions:
        annotation = substitutions[annotation]
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, UnionType):
        if value is None and type(None) in args:
            return None
        choices = [item for item in args if item is not type(None)]
        for choice in choices:
            try:
                return _decode(choice, value, substitutions)
            except (TypeError, ValueError, KeyError, IndexError):
                continue
        raise ValueError("checkpoint union field has invalid value")
    if annotation is type(None):
        if value is not None:
            raise ValueError("checkpoint field must be null")
        return None
    if annotation in (str, int, float, bool):
        if type(value) is not annotation:
            raise ValueError("checkpoint scalar has wrong type")
        return value
    if annotation is Decimal:
        if type(value) is not str:
            raise ValueError("checkpoint Decimal must be a string")
        try:
            result = Decimal(value)
        except Exception as exc:
            raise ValueError("invalid checkpoint Decimal") from exc
        if not result.is_finite():
            raise ValueError("nonfinite checkpoint Decimal")
        return result
    if origin is tuple:
        if type(value) is not list:
            raise ValueError("checkpoint tuple must be an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], item, substitutions) for item in value)
        if len(args) != len(value):
            raise ValueError("checkpoint tuple length mismatch")
        return tuple(_decode(kind, item, substitutions)
                     for kind, item in zip(args, value))
    if origin in (Mapping, dict):
        key_kind, value_kind = args
        if key_kind is int:
            if type(value) is not list:
                raise ValueError("checkpoint integer mapping must be pairs")
            pairs = [_decode(tuple[int, value_kind], row, substitutions)
                     for row in value]
            if len({key for key, _ in pairs}) != len(pairs):
                raise ValueError("duplicate checkpoint mapping key")
            return dict(pairs)
        if type(value) is not dict:
            raise ValueError("checkpoint mapping must be an object")
        return {_decode(key_kind, key, substitutions):
                _decode(value_kind, item, substitutions)
                for key, item in value.items()}
    cls = origin or annotation
    if cls in _ALLOWED and is_dataclass(cls):
        if type(value) is not dict:
            raise ValueError("checkpoint dataclass must be an object")
        declared, names, hints = _dataclass_metadata(cls)
        if set(value) != names:
            raise ValueError(f"checkpoint {cls.__name__} fields mismatch")
        local = dict(substitutions)
        if origin is not None:
            local.update(zip(cls.__parameters__, args))
        kwargs = {item.name: _decode(hints[item.name], value[item.name], local)
                  for item in declared}
        return cls(**kwargs)
    raise ValueError(f"unsupported checkpoint type: {annotation}")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"conflicting replay checkpoint file: {path.name}")
            return
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError(f"conflicting replay checkpoint file: {path.name}")
        temporary.unlink()
        temporary = None
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class ReplayCheckpointStore:
    """Chain of immutable hourly point chunks and exact post-boundary states."""

    def __init__(self, root: Path, identity: dict, start_boundary: int,
                 end_boundary: int, *, progress=None):
        self.root = Path(root)
        self.identity = identity
        self.start_boundary = start_boundary
        self.end_boundary = end_boundary
        self.progress = progress
        self.previous_sha = None
        self.previous_boundary = None
        self.point_count = 0
        self.chunk_points = []
        self._complete = False

    def _checkpoint_boundaries(self):
        boundary = self.start_boundary + 3_600_000
        while boundary < self.end_boundary:
            yield boundary
            boundary += 3_600_000
        yield self.end_boundary

    def _iter_validated_chunks(self):
        self.point_count = 0
        self.previous_sha = None
        self.previous_boundary = None
        self._complete = False
        paths = sorted(self.root.glob("checkpoint-*.json"))
        if not paths:
            return
        expected = iter(self._checkpoint_boundaries())
        state = None
        for path in paths:
            boundary = next(expected, None)
            if boundary is None or path.name != f"checkpoint-{boundary}.json":
                raise ValueError("replay checkpoint chain has a boundary gap")
            raw = path.read_bytes()
            metadata = _read_json_bytes(raw)
            if type(metadata) is not dict:
                raise ValueError("malformed replay checkpoint metadata")
            required = {"schema_version", "runtime_version", "identity", "boundary",
                        "status", "point_chunk_sha256", "replay_state_sha256",
                        "point_count", "cumulative_point_count",
                        "previous_checkpoint_sha256", "checkpoint_sha256"}
            if (set(metadata) != required
                    or metadata.get("status") != (
                        "REPLAY_COMPLETE" if boundary == self.end_boundary
                        else "IN_PROGRESS")):
                raise ValueError("malformed replay checkpoint fields")
            current_sha = metadata.get("checkpoint_sha256")
            body = dict(metadata)
            body.pop("checkpoint_sha256", None)
            if (metadata.get("schema_version") != REPLAY_CHECKPOINT_SCHEMA_VERSION
                    or metadata.get("runtime_version") != HISTORICAL_REPLAY_RUNTIME_VERSION
                    or metadata.get("identity") != self.identity
                    or metadata.get("boundary") != boundary
                    or metadata.get("previous_checkpoint_sha256") != self.previous_sha
                    or current_sha != _sha(_canonical_bytes(body))
                    or raw != _canonical_bytes(metadata)):
                raise ValueError("replay checkpoint identity or SHA mismatch")
            chunk_sha = metadata.get("point_chunk_sha256")
            chunk_path = self.root / "chunks" / f"{chunk_sha}.jsonl"
            chunk_raw = chunk_path.read_bytes()
            if _sha(chunk_raw) != chunk_sha:
                raise ValueError("replay point chunk SHA mismatch")
            lines = chunk_raw.splitlines(keepends=True)
            if not lines or any(not line.endswith(b"\n") for line in lines):
                raise ValueError("truncated replay point chunk")
            header = _read_json_bytes(lines[0])
            if (type(header) is not dict
                    or set(header) != {"schema_version", "run_fingerprint",
                                       "start_boundary", "end_boundary",
                                       "point_count", "first_point_id", "last_point_id"}
                    or header.get("schema_version") != REPLAY_POINT_CHUNK_SCHEMA_VERSION
                    or header.get("run_fingerprint") != self.identity["run_fingerprint"]
                    or header.get("start_boundary") != (
                        self.start_boundary if self.previous_boundary is None
                        else self.previous_boundary + 5_000)
                    or header.get("end_boundary") != boundary
                    or header.get("point_count") != len(lines) - 1
                    or metadata.get("point_count") != len(lines) - 1):
                raise ValueError("replay point chunk boundary/count mismatch")
            chunk = [_decode(HistoricalMarketReplayPoint, _read_json_bytes(line))
                     for line in lines[1:]]
            if (lines[0] != _canonical_bytes(header)
                    or any(line != _canonical_bytes(point)
                           for line, point in zip(lines[1:], chunk))):
                raise ValueError("noncanonical replay point chunk")
            expected_boundaries = tuple(range(header["start_boundary"],
                                              boundary + 1, 5_000))
            if (tuple(point.evaluation_boundary_time_ms for point in chunk)
                    != expected_boundaries
                    or any(point.point_id != _point_id(
                        self.identity["run_fingerprint"],
                        point.evaluation_boundary_time_ms) for point in chunk)
                    or header.get("first_point_id") != chunk[0].point_id
                    or header.get("last_point_id") != chunk[-1].point_id):
                raise ValueError("replay point chunk content mismatch")
            symbols = self.identity.get("configured_symbols")
            grace = self.identity.get("finalization_grace_ms")
            for point in chunk:
                if (point.movement_evaluation.evaluation_boundary_time_ms
                        != point.evaluation_boundary_time_ms
                        or (grace is not None and point.replay_clock_time_ms
                            != point.evaluation_boundary_time_ms + grace)
                        or (symbols is not None and (
                            [symbol for symbol, _ in point.endpoint_buckets] != symbols
                            or [symbol for symbol, _ in point.source_states] != symbols
                            or [item.symbol for item in point.source_time_evidence] != symbols))
                        or any(bucket.boundary_time_ms != point.evaluation_boundary_time_ms
                               for _, bucket in point.endpoint_buckets)):
                    raise ValueError("replay point scientific structure mismatch")
            state_sha = metadata.get("replay_state_sha256")
            state_raw = (self.root / "states" / f"{state_sha}.json").read_bytes()
            if _sha(state_raw) != state_sha:
                raise ValueError("replay state SHA mismatch")
            state = _decode(HistoricalReplayRuntimeState,
                            _read_json_bytes(state_raw))
            if state_raw != _canonical_bytes(state):
                raise ValueError("noncanonical replay state")
            if (state.run_fingerprint != self.identity["run_fingerprint"]
                    or state.completed_boundary_time_ms != boundary
                    or state.emitted_point_count != self.point_count + len(chunk)
                    or metadata["cumulative_point_count"] != state.emitted_point_count):
                raise ValueError("replay state boundary/count mismatch")
            self.point_count += len(chunk)
            del lines, chunk_raw, point
            self.previous_sha = current_sha
            self.previous_boundary = boundary
            self._complete = metadata.get("status") == "REPLAY_COMPLETE"
            if self._complete and boundary != self.end_boundary:
                raise ValueError("premature replay complete checkpoint")
            yield chunk, state
            del chunk
        if self.previous_boundary == self.end_boundary and not self._complete:
            raise ValueError("final replay checkpoint is not complete")

    def load_complete_metadata(self):
        """Verify a COMPLETE chain's bytes without reconstructing point graphs.

        Used only with a separately validated compact spool and prepared bundle.
        Partial recovery keeps load_latest's full state/point reconstruction.
        """
        final = self.root / f"checkpoint-{self.end_boundary}.json"
        if not final.exists():
            return False
        previous_sha = None
        count = 0
        paths = sorted(self.root.glob("checkpoint-*.json"))
        boundaries = tuple(self._checkpoint_boundaries())
        if len(paths) != len(boundaries):
            raise ValueError("complete replay chain has missing checkpoints")
        previous_boundary = None
        for path, boundary in zip(paths, boundaries):
            raw = path.read_bytes()
            metadata = _read_json_bytes(raw)
            required = {"schema_version", "runtime_version", "identity", "boundary", "status",
                        "point_chunk_sha256", "replay_state_sha256", "point_count",
                        "cumulative_point_count", "previous_checkpoint_sha256", "checkpoint_sha256"}
            if type(metadata) is not dict or set(metadata) != required:
                raise ValueError("invalid completed replay metadata fields")
            body = dict(metadata)
            sha = body.pop("checkpoint_sha256", None)
            start = self.start_boundary if previous_boundary is None else previous_boundary + 5_000
            chunk_count = (boundary - start) // 5_000 + 1
            count += chunk_count
            if (path.name != f"checkpoint-{boundary}.json"
                    or raw != _canonical_bytes(metadata) or sha != _sha(_canonical_bytes(body))
                    or body.get("schema_version") != REPLAY_CHECKPOINT_SCHEMA_VERSION
                    or body.get("runtime_version") != HISTORICAL_REPLAY_RUNTIME_VERSION
                    or body.get("identity") != self.identity
                    or body.get("boundary") != boundary
                    or body.get("previous_checkpoint_sha256") != previous_sha
                    or body.get("point_count") != chunk_count
                    or body.get("cumulative_point_count") != count
                    or body.get("status") != ("REPLAY_COMPLETE" if boundary == self.end_boundary else "IN_PROGRESS")):
                raise ValueError("completed replay metadata chain mismatch")
            for directory, key, suffix in (("chunks", "point_chunk_sha256", ".jsonl"),
                                            ("states", "replay_state_sha256", ".json")):
                digest = hashlib.sha256()
                with (self.root / directory / f"{body[key]}{suffix}").open("rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                if digest.hexdigest() != body[key]:
                    raise ValueError("completed replay artifact SHA mismatch")
            previous_sha, previous_boundary = sha, boundary
        self.previous_sha, self.previous_boundary = previous_sha, previous_boundary
        self.point_count, self._complete = count, True
        return True

    def load_latest(self):
        """Validate each bounded chunk once, retaining only the final state."""
        state = None
        for chunk, state in self._iter_validated_chunks():
            del chunk
        return state

    def add_point(self, point: HistoricalMarketReplayPoint,
                  state: HistoricalReplayRuntimeState | None):
        if self._complete:
            raise ValueError("cannot append to completed replay")
        self.chunk_points.append(point)
        if point.evaluation_boundary_time_ms not in self._checkpoint_boundaries():
            if state is not None:
                raise ValueError("unexpected replay checkpoint state")
            return
        if state is None:
            raise ValueError("missing replay checkpoint state")
        boundary = point.evaluation_boundary_time_ms
        start = (self.start_boundary if self.previous_boundary is None
                 else self.previous_boundary + 5_000)
        if (tuple(item.evaluation_boundary_time_ms for item in self.chunk_points)
                != tuple(range(start, boundary + 1, 5_000))):
            raise ValueError("replay checkpoint point boundaries are not contiguous")
        header = {
            "schema_version": REPLAY_POINT_CHUNK_SCHEMA_VERSION,
            "run_fingerprint": self.identity["run_fingerprint"],
            "start_boundary": start, "end_boundary": boundary,
            "point_count": len(self.chunk_points),
            "first_point_id": self.chunk_points[0].point_id,
            "last_point_id": self.chunk_points[-1].point_id,
        }
        chunk_raw = _canonical_bytes(header) + b"".join(
            _canonical_bytes(item) for item in self.chunk_points)
        chunk_sha = _sha(chunk_raw)
        state_raw = _canonical_bytes(state)
        state_sha = _sha(state_raw)
        _atomic_write(self.root / "chunks" / f"{chunk_sha}.jsonl", chunk_raw)
        _atomic_write(self.root / "states" / f"{state_sha}.json", state_raw)
        body = {
            "schema_version": REPLAY_CHECKPOINT_SCHEMA_VERSION,
            "runtime_version": HISTORICAL_REPLAY_RUNTIME_VERSION,
            "identity": self.identity,
            "boundary": boundary,
            "status": "REPLAY_COMPLETE" if boundary == self.end_boundary else "IN_PROGRESS",
            "point_chunk_sha256": chunk_sha,
            "replay_state_sha256": state_sha,
            "point_count": len(self.chunk_points),
            "cumulative_point_count": state.emitted_point_count,
            "previous_checkpoint_sha256": self.previous_sha,
        }
        checkpoint_sha = _sha(_canonical_bytes(body))
        metadata = {**body, "checkpoint_sha256": checkpoint_sha}
        _atomic_write(self.root / f"checkpoint-{boundary}.json",
                      _canonical_bytes(metadata))
        self.point_count += len(self.chunk_points)
        self.chunk_points.clear()
        self.previous_sha = checkpoint_sha
        self.previous_boundary = boundary
        self._complete = boundary == self.end_boundary
        if self.progress is not None:
            self.progress("REPLAY_CHECKPOINT_WRITTEN", {
                "completed_boundary": boundary,
                "completed_output_boundaries": state.emitted_point_count,
                "total_output_boundaries": (self.end_boundary - self.start_boundary) // 5_000 + 1,
                "checkpoint_sha256": checkpoint_sha,
                "point_count": self.point_count,
            })
            if self._complete:
                self.progress("REPLAY_COMPLETE", {"completed_boundary": boundary})


    def iter_points(self):
        """Revalidate the chain and read immutable chunks without retaining the day."""
        for chunk, state in self._iter_validated_chunks():
            yield from chunk
            del chunk, state


def _point_id(fingerprint, boundary):
    return canonical_replay_point_id(fingerprint, boundary)
