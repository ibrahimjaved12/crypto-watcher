"""Disk-backed operational study inputs and local, disposable scientific workers.

None of these cache schemas or measurements participate in scientific hashes.
The JSON value codec preserves Decimal, tuples and non-string mapping keys; it
never unpickles code and only reconstructs dataclasses in scientific modules.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date
from decimal import Decimal
import hashlib
import importlib
import os
from pathlib import Path
try:
    import resource
except ImportError:  # Optional operational observability only.
    resource = None
import subprocess
import sys
import tempfile
from types import SimpleNamespace

from .experiments.market_state_common import MarketStateExperimentPoint
from .historical_replay import canonical_replay_point_id
from .historical_replay_runtime import (
    _atomic_write, _canonical_bytes, _decode, _read_json_bytes, _sha,
)
from .movement_classifier import SymbolSourceTimeEvidence
from .movement_metrics import MarketMovementEvaluation

SPOOL_VERSION = "historical-study-point-spool-v1"
STAGE_VERSION = "historical-study-stage-v1"


@dataclass(frozen=True)
class CompactStudyPoint:
    point_id: str
    evaluation_boundary_time_ms: int
    movement_evaluation: MarketMovementEvaluation
    source_time_evidence: tuple[SymbolSourceTimeEvidence, ...]
    phase: str

    def experiment_point(self):
        return MarketStateExperimentPoint(
            self.movement_evaluation, self.source_time_evidence, self.phase)


@dataclass(frozen=True)
class CompactStudyReplay:
    """Study-only replay projection; never carries buckets or collector states."""
    manifest: object
    points: object
    diagnostics: object
    final_checkpoint: object


def _compact_decode(row):
    if set(row) != {item.name for item in fields(CompactStudyPoint)}:
        raise ValueError("invalid compact study point fields")
    return CompactStudyPoint(
        _decode(str, row["point_id"]),
        _decode(int, row["evaluation_boundary_time_ms"]),
        _decode(MarketMovementEvaluation, row["movement_evaluation"]),
        _decode(tuple[SymbolSourceTimeEvidence, ...], row["source_time_evidence"]),
        _decode(str, row["phase"]))


class StudyPointStream:
    def __init__(self, root, identity):
        self.root = Path(root)
        self.identity = identity
        raw = (self.root / "manifest.json").read_bytes()
        self.metadata = _read_json_bytes(raw)
        if set(self.metadata) != {"schema_version", "identity", "point_count",
                                  "first_boundary", "last_boundary", "stream_sha256",
                                  "metadata_sha256"}:
            raise ValueError("invalid compact study spool metadata fields")
        body = dict(self.metadata)
        sha = body.pop("metadata_sha256", None)
        if (raw != _canonical_bytes(self.metadata)
                or sha != _sha(_canonical_bytes(body))
                or body.get("schema_version") != SPOOL_VERSION
                or body.get("identity") != identity):
            raise ValueError("compact study spool identity/SHA mismatch")
        self.validate()

    def __len__(self):
        return self.metadata["point_count"]

    def __iter__(self):
        with (self.root / "points.jsonl").open("rb") as stream:
            for line in stream:
                yield _compact_decode(_read_json_bytes(line))

    def validate(self):
        digest = hashlib.sha256()
        count = 0
        first = last = None
        with (self.root / "points.jsonl").open("rb") as stream:
            for line in stream:
                digest.update(line)
                point = _compact_decode(_read_json_bytes(line))
                boundary = point.evaluation_boundary_time_ms
                if (line != _canonical_bytes(point)
                        or point.point_id != canonical_replay_point_id(
                            self.identity["run_fingerprint"], boundary)
                        or point.movement_evaluation.evaluation_boundary_time_ms != boundary
                        or point.phase != self.identity["phase"]
                        or (last is not None and boundary != last + 5_000)):
                    raise ValueError("compact study spool content mismatch")
                first = boundary if first is None else first
                last = boundary
                count += 1
        if (digest.hexdigest() != self.metadata["stream_sha256"]
                or count != self.metadata["point_count"] or count == 0
                or first != self.metadata["first_boundary"]
                or last != self.metadata["last_boundary"]
                or count != self.identity["point_count"]
                or first != self.identity["first_boundary"]
                or last != self.identity["last_boundary"]):
            raise ValueError("compact study spool digest/count/range mismatch")


def create_study_point_stream(store):
    store.load_latest()
    if not store._complete:
        raise ValueError("compact spool requires a validated completed replay")
    identity = {**store.identity, "final_replay_checkpoint_sha256": store.previous_sha,
                "point_count": store.point_count,
                "first_boundary": store.start_boundary, "last_boundary": store.end_boundary}
    root = store.root / "study-points"
    if root.exists():
        return StudyPointStream(root, identity)
    temporary = Path(tempfile.mkdtemp(prefix=".study-points-", dir=store.root))
    try:
        digest = hashlib.sha256()
        count = 0
        with (temporary / "points.jsonl").open("wb") as stream:
            for point in store.iter_points():
                row = CompactStudyPoint(point.point_id, point.evaluation_boundary_time_ms,
                                        point.movement_evaluation, point.source_time_evidence,
                                        identity["phase"])
                raw = _canonical_bytes(row)
                stream.write(raw)
                digest.update(raw)
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        body = {"schema_version": SPOOL_VERSION, "identity": identity,
                "point_count": count, "first_boundary": store.start_boundary,
                "last_boundary": store.end_boundary, "stream_sha256": digest.hexdigest()}
        _atomic_write(temporary / "manifest.json",
                      _canonical_bytes({**body, "metadata_sha256": _sha(_canonical_bytes(body))}))
        StudyPointStream(temporary, identity)
        os.rename(temporary, root)
        descriptor = os.open(store.root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary.exists():
            for path in temporary.iterdir():
                path.unlink()
            temporary.rmdir()
    return StudyPointStream(root, identity)


def encode(value):
    """Lossless local JSON values, distinct from scientific report JSON."""
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    if isinstance(value, Path):
        return {"path": str(value)}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, StudyPointStream):
        return {"stream": str(value.root), "identity": value.identity}
    if isinstance(value, SimpleNamespace):
        return {"namespace": encode(vars(value))}
    if is_dataclass(value):
        return {"dataclass": [type(value).__module__, type(value).__name__],
                "fields": {item.name: encode(getattr(value, item.name))
                           for item in fields(value) if item.init and item.name != "_index"}}
    if isinstance(value, Mapping):
        return {"mapping": [[encode(key), encode(item)] for key, item in value.items()]}
    if isinstance(value, (tuple, list)):
        return {"tuple" if isinstance(value, tuple) else "list": [encode(item) for item in value]}
    raise ValueError(f"unsupported operational value: {type(value)}")


def decode(value):
    if not isinstance(value, dict):
        return value
    if "decimal" in value:
        return Decimal(value["decimal"])
    if "path" in value:
        return Path(value["path"])
    if "date" in value:
        return date.fromisoformat(value["date"])
    if "stream" in value:
        return StudyPointStream(value["stream"], value["identity"])
    if "namespace" in value:
        return SimpleNamespace(**decode(value["namespace"]))
    if "mapping" in value:
        pairs = [(decode(key), decode(item)) for key, item in value["mapping"]]
        if len(dict(pairs)) != len(pairs):
            raise ValueError("duplicate operational mapping key")
        return dict(pairs)
    if "tuple" in value or "list" in value:
        key = "tuple" if "tuple" in value else "list"
        items = [decode(item) for item in value[key]]
        return tuple(items) if key == "tuple" else items
    if "dataclass" in value:
        module, name = value["dataclass"]
        if not module.startswith("market_analysis.") or "." in name or name.startswith("_"):
            raise ValueError("unsupported operational dataclass")
        cls = getattr(importlib.import_module(module), name)
        if not is_dataclass(cls):
            raise ValueError("operational type must be a scientific dataclass")
        kwargs = {key: decode(item) for key, item in value["fields"].items()}
        if name == "BinanceBoundedHistoricalReplayDataset":
            kwargs["_index"] = None  # Workers cannot access raw trades or a provider.
        return cls(**kwargs)
    raise ValueError("unknown operational JSON value")


def memory_usage():
    try:
        current = None
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                current = int(line.split()[1]) * 1024
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        return {"current_rss_bytes": current, "peak_rss_bytes": peak}
    except (OSError, ValueError, AttributeError):
        return {}


def _load_stage(path, identity):
    raw = path.read_bytes()
    payload = _read_json_bytes(raw)
    body = dict(payload)
    sha = body.pop("stage_result_sha256", None)
    if (raw != _canonical_bytes(payload) or sha != _sha(_canonical_bytes(body))
            or body.get("identity") != identity or body.get("schema_version") != STAGE_VERSION):
        raise ValueError("post-replay stage identity/SHA mismatch")
    return decode(body["result"]), sha, body.get("memory", {})


def run_stage(stream, stage_id, request, *, progress=None):
    """Validate/reuse or spawn one fresh interpreter; never fork the parent heap."""
    root = stream.root.parent / "post-replay"
    root.mkdir(exist_ok=True)
    encoded = encode(request)
    request_raw = _canonical_bytes(encoded)
    identity = {**stream.identity, "compact_stream_sha256": stream.metadata["stream_sha256"],
                "stage_id": stage_id, "request_sha256": _sha(request_raw),
                "scientific_stage": request.get("scientific_stage"),
                "supplementary_source_sha256": request.get("supplementary_source_sha256")}
    path = root / f"{stage_id}.json"
    if path.exists():
        result, sha, memory = _load_stage(path, identity)
        if progress:
            progress("POST_REPLAY_STAGE_REUSED", {"stage_id": stage_id,
                     "stage_result_sha256": sha, **memory_usage()})
        return result
    request_path = root / f"{stage_id}.request.json"
    _atomic_write(request_path, request_raw)
    job_path = root / f"{stage_id}.job.json"
    _atomic_write(job_path, _canonical_bytes({"identity": identity,
                  "request_path": str(request_path), "result_path": str(path)}))
    if progress:
        progress("POST_REPLAY_STAGE_STARTED", {"stage_id": stage_id, "scientific_stage": request.get("scientific_stage"), **memory_usage()})
    environment = dict(os.environ)
    package_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
        package_root, environment.get("PYTHONPATH"))))
    subprocess.run([sys.executable, "-m", "market_analysis.historical_study_runtime",
                    str(job_path)], check=True, env=environment)
    result, sha, memory = _load_stage(path, identity)
    if progress:
        progress("POST_REPLAY_STAGE_COMPLETED", {"stage_id": stage_id,
                 "stage_result_sha256": sha, "worker_memory": memory, **memory_usage()})
    return result


def _worker(job_path):
    from . import historical_market_state_study_execution as execution
    job = _read_json_bytes(Path(job_path).read_bytes())
    raw = Path(job["request_path"]).read_bytes()
    if _sha(raw) != job["identity"]["request_sha256"]:
        raise ValueError("post-replay request SHA mismatch")
    request = decode(_read_json_bytes(raw))
    prepared = request.get("prepared")
    action = request["action"]
    if action == "v1":
        result = execution._build_v1_evidence(prepared.period,
                    prepared.canonical_replay_result.points, retain_branch=False)[:2]
    elif action == "candidate":
        compact = tuple(prepared.canonical_replay_result.points)
        prepared.canonical_replay_result = CompactStudyReplay(
            prepared.canonical_replay_result.manifest, compact,
            prepared.canonical_replay_result.diagnostics,
            prepared.canonical_replay_result.final_checkpoint)
        prepared.experiment_points = tuple(point.experiment_point() for point in compact)
        result = execution._candidate_execution(prepared, request["supplementary"],
                    request["hmm_model"], stage_selector=request["selector"])
    elif action == "event-context":
        result = execution._event_time_v1_context(prepared, request["records"])
    elif action == "outcomes":
        result = execution._continuous_and_event_outcomes(
            request["forward_evidence"], request["records"], request["states"], request["period"])
    else:
        raise ValueError("unknown post-replay action")
    body = {"schema_version": STAGE_VERSION, "identity": job["identity"],
            "result": encode(result), "memory": {"process_id": os.getpid(), **memory_usage()}}
    _atomic_write(Path(job["result_path"]), _canonical_bytes(
        {**body, "stage_result_sha256": _sha(_canonical_bytes(body))}))


if __name__ == "__main__":
    from market_analysis.historical_study_runtime import _worker as canonical_worker
    canonical_worker(sys.argv[1])
