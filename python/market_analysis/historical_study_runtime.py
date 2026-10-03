"""Disk-backed operational study inputs and local, disposable scientific workers.

None of these cache schemas or measurements participate in scientific hashes.
The JSON value codec preserves Decimal, tuples and non-string mapping keys; it
never unpickles code and only reconstructs dataclasses in scientific modules.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from functools import lru_cache
import hashlib
import importlib
import math
import os
from pathlib import Path
try:
    import resource
except ImportError:  # Optional operational observability only.
    resource = None
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from .experiments.market_state_common import MarketStateExperimentPoint
from .historical_replay import CompactStudyReplay, canonical_replay_point_id
from .historical_replay_runtime import (
    _atomic_write, _canonical_bytes, _decode, _read_json_bytes, _sha,
)
from .movement_classifier import SymbolSourceTimeEvidence
from .movement_metrics import MarketMovementEvaluation, MarketMovementWindowResult

SPOOL_VERSION = "historical-study-point-spool-v2"
STAGE_VERSION = "historical-study-stage-v2"


def current_runtime_implementation_revision() -> str:
    """Resolve HEAD only when the executable package source is clean there."""
    root = Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True)
        revision = completed.stdout.strip()
        if (len(revision) not in (40, 64)
                or any(character not in "0123456789abcdef" for character in revision)):
            raise ValueError("could not determine a valid runtime implementation revision")
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain",
             "--untracked-files=all", "--", "python/market_analysis"],
            check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("could not verify runtime implementation source at HEAD") from exc
    if status.stdout.strip():
        raise ValueError("runtime implementation source tree is not clean at HEAD")
    return revision


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


def _compact_decode(row):
    if set(row) != {item.name for item in fields(CompactStudyPoint)}:
        raise ValueError("invalid compact study point fields")
    return CompactStudyPoint(
        _decode(str, row["point_id"]),
        _decode(int, row["evaluation_boundary_time_ms"]),
        _decode(MarketMovementEvaluation, row["movement_evaluation"]),
        _decode(tuple[SymbolSourceTimeEvidence, ...], row["source_time_evidence"]),
        _decode(str, row["phase"]))


class ValidatedPointConsumption:
    """A completion token belongs to this specific pass, never to a generator close."""
    def __init__(self, stream):
        self.completed = False
        self.iterator = stream._validated_points(self)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def close(self):
        self.iterator.close()


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
        count, first, last = (body[name] for name in ("point_count", "first_boundary", "last_boundary"))
        if (type(count) is not int or count <= 0 or type(first) is not int or type(last) is not int
                or first < 0 or first % 5_000 or last < first or last % 5_000
                or (last - first) // 5_000 + 1 != count
                or any(body[name] != identity[name] for name in ("point_count", "first_boundary", "last_boundary"))
                or type(body["stream_sha256"]) is not str or len(body["stream_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in body["stream_sha256"])):
            raise ValueError("invalid compact spool count/range/hash metadata")
        self._consumption = None

    def __len__(self):
        return self.metadata["point_count"]

    @property
    def validated_completion(self):
        return self._consumption is not None and self._consumption.completed

    def consume(self):
        self._consumption = ValidatedPointConsumption(self)
        return self._consumption

    def __iter__(self):
        return self.consume()

    def _validated_points(self, consumption):
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
                yield point
        if (digest.hexdigest() != self.metadata["stream_sha256"]
                or count != self.metadata["point_count"] or count == 0
                or first != self.metadata["first_boundary"]
                or last != self.metadata["last_boundary"]
                or count != self.identity["point_count"]
                or first != self.identity["first_boundary"]
                or last != self.identity["last_boundary"]):
            raise ValueError("compact study spool digest/count/range mismatch")

        consumption.completed = True

    def validate(self):
        consumption = self.consume()
        for point in consumption:
            pass
        if not consumption.completed:
            raise ValueError("compact spool validation did not complete")


def create_study_point_stream(store):
    root = store.root / "study-points"
    if not store.root.exists():
        raise ValueError("compact spool requires a validated completed replay")
    if not store._complete:
        raise ValueError("compact spool requires a validated COMPLETE replay checkpoint")
    identity = {**store.identity, "final_replay_checkpoint_sha256": store.previous_sha,
                "point_count": store.point_count,
                "first_boundary": store.start_boundary, "last_boundary": store.end_boundary}
    if root.exists():
        existing = StudyPointStream(root, identity)
        existing.validate()
        return existing
    temporary = Path(tempfile.mkdtemp(prefix=".study-points-", dir=store.root))
    try:
        digest = hashlib.sha256()
        count = 0
        with (temporary / "points.jsonl").open("wb") as stream:
            for point in store.iter_points():
                row = CompactStudyPoint(point.point_id, point.evaluation_boundary_time_ms,
                                        point.movement_evaluation, point.source_time_evidence,
                                        store.identity["phase"])
                raw = _canonical_bytes(row)
                stream.write(raw)
                digest.update(raw)
                count += 1
            stream.flush()
            os.fsync(stream.fileno())
        # Generator validation/completion metadata is final only after exhaustion.
        if not store._complete:
            raise ValueError("compact spool requires a validated completed replay")
        identity = {**store.identity, "final_replay_checkpoint_sha256": store.previous_sha,
                    "point_count": store.point_count,
                    "first_boundary": store.start_boundary, "last_boundary": store.end_boundary}
        body = {"schema_version": SPOOL_VERSION, "identity": identity,
                "point_count": count, "first_boundary": store.start_boundary,
                "last_boundary": store.end_boundary, "stream_sha256": digest.hexdigest()}
        _atomic_write(temporary / "manifest.json",
                      _canonical_bytes({**body, "metadata_sha256": _sha(_canonical_bytes(body))}))
        StudyPointStream(temporary, identity).validate()
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


@lru_cache(maxsize=None)
def _operational_fields(cls):
    return tuple(item for item in fields(cls) if item.init and item.name != "_index")


def encode(value, point_reference=None):
    """Lossless local JSON values, distinct from scientific report JSON."""
    if point_reference is not None:
        if value is point_reference.source_time_evidence:
            return {"point_sources": True}
        if isinstance(value, MarketMovementWindowResult):
            for minute, window in point_reference.movement_evaluation.windows.items():
                if value is window:
                    return {"point_window": minute}
    if value is None or type(value) in (str, int, float, bool):
        if type(value) is float and not math.isfinite(value):
            raise ValueError("nonfinite operational float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite operational Decimal")
        return {"decimal": str(value)}
    if isinstance(value, Path):
        return {"path": str(value)}
    if isinstance(value, date):
        return {"date": value.isoformat()}
    if isinstance(value, StudyPointStream):
        return {"stream": str(value.root), "identity": value.identity}
    if isinstance(value, SimpleNamespace):
        return {"namespace": encode(vars(value), point_reference)}
    if is_dataclass(value):
        return {"dataclass": [type(value).__module__, type(value).__name__],
                "fields": {item.name: encode(getattr(value, item.name), point_reference)
                           for item in _operational_fields(type(value))}}
    if isinstance(value, Mapping):
        return {"mapping": [[encode(key, point_reference), encode(item, point_reference)] for key, item in value.items()]}
    if isinstance(value, (tuple, list)):
        return {"tuple" if isinstance(value, tuple) else "list": [encode(item, point_reference) for item in value]}
    raise ValueError(f"unsupported operational value: {type(value)}")


@lru_cache(maxsize=None)
def _operational_type(module, name):
    if not module.startswith("market_analysis.") or "." in name or name.startswith("_"):
        raise ValueError("unsupported operational dataclass")
    cls = getattr(importlib.import_module(module), name)
    if not is_dataclass(cls) or cls.__module__ != module:
        raise ValueError("operational type must be a scientific dataclass")
    return cls


def decode(value, point_reference=None):
    if not isinstance(value, dict):
        if value is None or type(value) in (str, int, bool):
            return value
        if type(value) is float and math.isfinite(value):
            return value
        raise ValueError("invalid operational primitive")
    expected_fields = {"stream": {"stream", "identity"}, "dataclass": {"dataclass", "fields"}}
    tags = set(value) - {"identity", "fields"}
    if len(tags) != 1 or set(value) != expected_fields.get(next(iter(tags)), tags):
        raise ValueError("invalid operational value fields")
    if "point_window" in value:
        if point_reference is None or type(value["point_window"]) is not int:
            raise ValueError("movement reference requires validated current point")
        return point_reference[0].windows[value["point_window"]]
    if "point_sources" in value:
        if point_reference is None or value["point_sources"] is not True:
            raise ValueError("source reference requires validated current point")
        return point_reference[1]
    if "decimal" in value:
        if type(value["decimal"]) is not str:
            raise ValueError("operational Decimal must be a string")
        decimal = Decimal(value["decimal"])
        if not decimal.is_finite():
            raise ValueError("nonfinite operational Decimal")
        return decimal
    if "path" in value:
        return Path(value["path"])
    if "date" in value:
        return date.fromisoformat(value["date"])
    if "stream" in value:
        return StudyPointStream(value["stream"], value["identity"])
    if "namespace" in value:
        return SimpleNamespace(**decode(value["namespace"], point_reference))
    if "mapping" in value:
        pairs = [(decode(key, point_reference), decode(item, point_reference)) for key, item in value["mapping"]]
        if len(dict(pairs)) != len(pairs):
            raise ValueError("duplicate operational mapping key")
        return dict(pairs)
    if "tuple" in value or "list" in value:
        key = "tuple" if "tuple" in value else "list"
        items = [decode(item, point_reference) for item in value[key]]
        return tuple(items) if key == "tuple" else items
    if "dataclass" in value:
        module, name = value["dataclass"]
        cls = _operational_type(module, name)
        if set(value["fields"]) != {item.name for item in _operational_fields(cls)}:
            raise ValueError("operational dataclass fields mismatch")
        kwargs = {key: decode(item, point_reference) for key, item in value["fields"].items()}
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


def _stage_metadata(path, identity=None, stream=None):
    raw = path.read_bytes()
    payload = _read_json_bytes(raw)
    if type(payload) is not dict or set(payload) != {
            "schema_version", "identity", "result", "stage_result_sha256"}:
        raise ValueError("invalid post-replay stage fields")
    body = dict(payload)
    sha = body.pop("stage_result_sha256")
    actual = body["identity"]
    if (raw != _canonical_bytes(payload) or sha != _sha(_canonical_bytes(body))
            or body["schema_version"] != STAGE_VERSION
            or (identity is not None and actual != identity)
            or (stream is not None and (
                any(actual.get(key) != value for key, value in stream.identity.items())
                or actual.get("compact_stream_sha256") != stream.metadata["stream_sha256"]))):
        raise ValueError("post-replay stage identity/SHA mismatch")
    result = decode(body["result"])
    from .historical_stage_records import StageRecords, StageValue
    for item in (result if isinstance(result, tuple) else (result,)):
        if isinstance(item, (StageRecords, StageValue)):
            item.verify()
    if actual.get("stage_id") == "v1":
        from .historical_shared_v1 import _file_sha
        reference = result[2]
        metadata_raw = Path(reference.path).with_suffix(".manifest.json").read_bytes()
        metadata = _read_json_bytes(metadata_raw)
        if (metadata_raw != _canonical_bytes(metadata)
                or _sha(metadata_raw) != reference.metadata_sha256
                or _file_sha(reference.path) != metadata["branch_sha256"]):
            raise ValueError("shared V1 artifact SHA mismatch")
    return result, sha


def _load_stage(path, identity):
    result, sha = _stage_metadata(path, identity)
    measurement_path = path.with_suffix(".observations.json")
    measurements = (_read_json_bytes(measurement_path.read_bytes())
                    if measurement_path.exists() else {})
    return result, sha, measurements


def stage_dependencies(stream, stage_ids):
    dependencies = []
    for stage_id in stage_ids:
        path = stream.root.parent / "post-replay" / f"{stage_id}.json"
        _, sha = _stage_metadata(path, stream=stream)
        payload = _read_json_bytes(path.read_bytes())
        identity = payload["identity"]
        if identity.get("stage_id") != stage_id:
            raise ValueError("post-replay dependency stage mismatch")
        dependencies.append({"stage_id": stage_id,
            "stage_identity_sha256": _sha(_canonical_bytes(identity)), "stage_result_sha256": sha})
    return tuple(dependencies)


def input_descriptor(request):
    """Complete small inputs, and immutable identities for large consumed values.

    Both parent and worker derive this contract. The payload also has its own
    byte hash; a descriptor cannot be swapped independently of consumed inputs.
    Runtime revision binds implicit algorithm defaults and numerical policies.
    """
    result = {}
    for key, value in request.items():
        if key == "prepared":
            archive = value.archive_dataset
            replay = value.canonical_replay_result
            result[key] = {"period": encode(value.period),
                "study_manifest_sha256": value.study_manifest_sha256,
                "core_eligibility_sha256": value.core_eligibility_sha256,
                "code_revision": value.code_revision,
                "replay_manifest": encode(replay.manifest),
                "replay_diagnostics": encode(replay.diagnostics),
                "final_checkpoint": encode(replay.final_checkpoint),
                "stream": encode(replay.points),
                "shared_v1": encode(value.canonical_v1_branch_by_boundary),
                "archive": None if archive is None else {
                    name: encode(getattr(archive, name, None)) for name in (
                        "archive_manifest", "universe", "config")},
                "archive_evidence": None if archive is None else {
                    name: getattr(getattr(archive, name, None), "evidence_sha256", None)
                    for name in ("ohlc_evidence", "taker_flow_evidence")}}
        elif key == "supplementary":
            result[key] = {name: {"coverage": encode(item["coverage"]),
                "root": encode(item["root"]),
                "evidence_sha256": getattr(item["evidence"], "evidence_sha256", None)}
                for name, item in value.items()}
        elif key == "forward_evidence":
            result[key] = value.evidence_sha256
        elif key == "hmm_model":
            result[key] = getattr(value, "model_sha256", None)
        else:
            result[key] = encode(value)
    return result


def _observation(started, *, reused, path=None, worker=None):
    return {"observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "monotonic_duration_seconds": time.perf_counter() - started,
            "reused": reused, "parent_memory": memory_usage(),
            "worker_observations": worker,
            "stage_metadata_bytes": path.stat().st_size if path else None,
            "artifact_sizes_bytes": ({artifact.name: artifact.stat().st_size
                for artifact in path.parent.glob(f"{path.stem}.*")
                if not artifact.name.endswith((".request.json", ".job.json"))} if path else {})}


def run_stage(stream, stage_id, request=None, *, descriptor=None, prepare=None,
              progress=None, local_result=None, worker_lease=None):
    """Lookup by complete descriptor before preparing any large payload."""
    started = time.perf_counter()
    root = stream.root.parent / "post-replay"
    if worker_lease is None:
        raise ValueError("post-replay stage requires period directory ownership")
    worker_lease.validate(root.parent)
    root.mkdir(exist_ok=True)
    if descriptor is None:
        descriptor = input_descriptor(request)
    identity = {**stream.identity, "compact_stream_sha256": stream.metadata["stream_sha256"],
                "stage_id": stage_id, "input_descriptor": descriptor,
                "descriptor_sha256": _sha(_canonical_bytes(descriptor))}
    identity = _read_json_bytes(_canonical_bytes(identity))
    path = root / f"{stage_id}.json"
    if path.exists():
        result, sha, measurements = _load_stage(path, identity)
        for suffix in ("request.json", "job.json"):
            (root / f"{stage_id}.{suffix}").unlink(missing_ok=True)
        if progress:
            progress("POST_REPLAY_STAGE_REUSED", {"stage_id": stage_id,
                     "stage_result_sha256": sha, **_observation(started, reused=True,
                     path=path, worker=measurements)})
        return result
    if local_result is not None:
        _publish_stage(path, identity, local_result())
    else:
        request = prepare() if prepare is not None else request
        if input_descriptor(request) != descriptor:
            raise ValueError("stage payload does not match its input descriptor")
        request_raw = _canonical_bytes(encode(request))
        request_path = root / f"{stage_id}.request.json"
        _atomic_write(request_path, request_raw)
        job_path = root / f"{stage_id}.job.json"
        _atomic_write(job_path, _canonical_bytes({"identity": identity,
                      "request_sha256": _sha(request_raw),
                      "request_path": str(request_path), "result_path": str(path)}))
        if progress:
            progress("POST_REPLAY_STAGE_STARTED", {"stage_id": stage_id,
                     **_observation(started, reused=False)})
        environment = dict(os.environ)
        package_root = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = os.pathsep.join(filter(None, (
            package_root, environment.get("PYTHONPATH"))))
        _run_owned_worker(job_path, environment, worker_lease)
    result, sha, measurements = _load_stage(path, identity)
    # Completion and record hashes have been verified after durable publication.
    for suffix in ("request.json", "job.json"):
        (root / f"{stage_id}.{suffix}").unlink(missing_ok=True)
    if progress:
        progress("POST_REPLAY_STAGE_COMPLETED", {"stage_id": stage_id,
                 "stage_result_sha256": sha, **_observation(started, reused=False,
                 path=path, worker=measurements)})
    return result


def _run_owned_worker(job_path, environment, lease):
    """Inherit only the lease, and reap an interrupted child before returning."""
    lease.validate(Path(job_path).parent.parent)
    arguments = [sys.executable, "-m", "market_analysis.historical_study_runtime",
                 str(job_path), str(lease.descriptor)]
    # The descriptor is ephemeral argv, not create-only job/request identity.
    child = subprocess.Popen(arguments, env=environment, close_fds=True,
                             pass_fds=(lease.descriptor,))
    try:
        returncode = child.wait()
        if returncode:
            raise subprocess.CalledProcessError(returncode, arguments)
    except BaseException:
        try:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # The finally block kills and reaps before lease release.
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
        raise


def _write_records(path, records):
    from .historical_stage_records import StageRecords
    from .historical_market_state_study_json import iter_canonical_study_json
    from .historical_shared_v1 import _file_sha
    temporaries = []
    scientific_path = path.with_suffix(".scientific.jsonl")
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                prefix=f".{path.stem}.records-", delete=False) as handle, \
             tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                prefix=f".{path.stem}.records-", delete=False) as scientific:
            temporaries = [Path(handle.name), Path(scientific.name)]
            digest, scientific_digest, count = hashlib.sha256(), hashlib.sha256(), 0
            for item in records:
                raw = _canonical_bytes(encode(item))
                handle.write(raw)
                digest.update(raw)
                for part in iter_canonical_study_json(item):
                    raw = part.encode("ascii")
                    scientific.write(raw)
                    scientific_digest.update(raw)
                scientific.write(b"\n")
                scientific_digest.update(b"\n")
                count += 1
            for stream in (handle, scientific):
                stream.flush()
                os.fsync(stream.fileno())
        for temporary, destination, sha in zip(temporaries, (path, scientific_path),
                                                (digest.hexdigest(), scientific_digest.hexdigest())):
            if destination.exists():
                if _file_sha(destination) != sha:
                    raise ValueError("conflicting scientific stage records")
            else:
                os.link(temporary, destination)
        return StageRecords(str(path), count, digest.hexdigest(),
                            str(scientific_path), scientific_digest.hexdigest())
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)


def _publish_stage(path, identity, result):
    action = identity["input_descriptor"]["action"]
    if action in ("candidate", "v1"):
        records = _write_records(path.with_suffix(".records.jsonl"), result[0])
        if action == "candidate" and result[4] is not None:
            from .historical_stage_records import StageValue
            from .historical_market_state_study_json import iter_canonical_study_json
            scientific_path = path.with_suffix(".hmm-block.json")
            # The block is required in the HMM worker; the parent keeps a reference.
            raw = "".join(iter_canonical_study_json(result[4])).encode("ascii")
            _atomic_write(scientific_path, raw)
            result = (*result[:4], StageValue(str(scientific_path), _sha(raw),
                                             result[4].block_sha256), result[5])
        result = (records, *result[1:])
    elif action == "event-context":
        result = _write_records(path.with_suffix(".records.jsonl"), result)
    elif action == "outcomes":
        result = tuple(_write_records(path.with_suffix(f".{index}.records.jsonl"), records)
                       for index, records in enumerate(result))
    body = {"schema_version": STAGE_VERSION, "identity": identity, "result": encode(result)}
    _atomic_write(path, _canonical_bytes({**body, "stage_result_sha256": _sha(_canonical_bytes(body))}))


def _valid_git_revision(value):
    return (type(value) is str and len(value) in (40, 64)
            and all(character in "0123456789abcdef" for character in value))


def _verify_worker_runtime_revision(identity):
    expected = identity.get("runtime_implementation_revision")
    if not _valid_git_revision(expected):
        raise ValueError("post-replay job lacks a valid runtime implementation revision")
    try:
        actual = current_runtime_implementation_revision()
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise ValueError("could not determine worker runtime implementation revision") from exc
    if not _valid_git_revision(actual):
        raise ValueError("could not determine a valid worker runtime implementation revision")
    if actual != expected:
        raise ValueError("post-replay worker runtime implementation revision mismatch")


def _execute_scientific_stage(request):
    from . import historical_market_state_study_execution as execution
    prepared = request.get("prepared")
    action = request["action"]
    if action == "v1":
        from .historical_shared_v1 import SharedV1Writer
        writer = SharedV1Writer(prepared.canonical_replay_result.points)
        records, states, _ = execution._build_v1_evidence(prepared.period,
                    prepared.canonical_replay_result.points, retain_branch=False,
                    branch_writer=writer)
        return records, states, writer.publish()
    if action == "candidate":
        if request["selector"] in ("hmm",) or request["selector"].startswith(("fixed-", "atr-")):
            compact = tuple(prepared.canonical_replay_result.points)
            prepared.canonical_replay_result = CompactStudyReplay(
                prepared.canonical_replay_result.manifest, compact,
                prepared.canonical_replay_result.diagnostics,
                prepared.canonical_replay_result.final_checkpoint)
            prepared.experiment_points = tuple(point.experiment_point() for point in compact)
        reader = None
        if prepared.canonical_v1_branch_by_boundary is not None:
            reader = prepared.canonical_v1_branch_by_boundary.reader(request["prepared_stream"])
            prepared.canonical_v1_branch_by_boundary = reader
        result = execution._candidate_execution(prepared, request["supplementary"],
                    request["hmm_model"], stage_selector=request["selector"])
        if reader is not None:
            reader.finish()
        return result
    if action == "event-context":
        reader = prepared.canonical_v1_branch_by_boundary.reader(prepared.canonical_replay_result.points)
        prepared.canonical_v1_branch_by_boundary = reader
        result = execution._event_time_v1_context(prepared, (), event_times=request["boundaries"])
        reader.finish()
        return result
    if action == "outcomes":
        return execution._continuous_and_event_outcomes(
            request["forward_evidence"], request["records"], request["states"], request["period"])
    raise ValueError("unknown post-replay action")


def _worker(job_path, lease_descriptor=None):
    from .historical_run_directory import inherited_run_directory_lease
    with inherited_run_directory_lease(lease_descriptor, Path(job_path).parent.parent) as lease:
        _worker_owned(job_path, lease)


def _worker_owned(job_path, lease):
    started = time.perf_counter()
    job = _read_json_bytes(Path(job_path).read_bytes())
    if type(job) is not dict or type(job.get("identity")) is not dict:
        raise ValueError("invalid post-replay worker job")
    lease.validate(Path(job["result_path"]).parent.parent)
    if Path(job["request_path"]).parent.resolve() != Path(job_path).parent.resolve():
        raise ValueError("post-replay request lies outside the leased stage directory")
    identity = job["identity"]
    _verify_worker_runtime_revision(identity)
    raw = Path(job["request_path"]).read_bytes()
    if _sha(raw) != job.get("request_sha256"):
        raise ValueError("post-replay request SHA mismatch")
    request = decode(_read_json_bytes(raw))
    if _canonical_bytes(input_descriptor(request)) != _canonical_bytes(identity["input_descriptor"]):
        raise ValueError("scientific inputs differ from stage descriptor")
    required = request.get("required_stage_results", ())
    if required:
        spool_identity = {key: value for key, value in identity.items()
                          if key not in ("stage_id", "compact_stream_sha256", "input_descriptor", "descriptor_sha256")}
        reference_stream = StudyPointStream(Path(job["result_path"]).parent.parent / "study-points", spool_identity)
        if stage_dependencies(reference_stream, tuple(item["stage_id"] for item in required)) != required:
            raise ValueError("consumed upstream stage hashes differ from the descriptor")
    source_stream = (request["prepared"].canonical_replay_result.points
                     if request.get("prepared") is not None else None)
    result = _execute_scientific_stage(request)
    if isinstance(source_stream, StudyPointStream) and not source_stream.validated_completion:
        raise ValueError("stage cannot publish after incomplete point consumption")
    path = Path(job["result_path"])
    lease.validate(path.parent.parent)
    _publish_stage(path, identity, result)
    # Sample after encoding, hashing, fsync and immutable stage publication.
    lease.validate(path.parent.parent)
    _atomic_write(path.with_suffix(".observations.json"), _canonical_bytes({
        "observed_at_utc": datetime.now(timezone.utc).isoformat(),
        "monotonic_duration_seconds": time.perf_counter() - started,
        "sampling_boundary": "after durable stage publication; not process-exit peak",
        "worker_memory": {"process_id": os.getpid(), **memory_usage()},
        "stage_metadata_bytes": path.stat().st_size,
        "artifact_sizes_bytes": {artifact.name: artifact.stat().st_size
            for artifact in path.parent.glob(f"{path.stem}.*")
            if artifact.name != path.name and not artifact.name.endswith((".request.json", ".job.json"))},
        "record_artifact_bytes": (path.with_suffix(".records.jsonl").stat().st_size
                                  if path.with_suffix(".records.jsonl").exists() else None)}))


if __name__ == "__main__":
    from market_analysis.historical_study_runtime import _worker as canonical_worker
    canonical_worker(sys.argv[1], int(sys.argv[2]) if len(sys.argv) == 3 else None)
