"""Compact, sequential full V1 branches bound to the validated point spool.

Movement windows and source evidence are references to the current point, never
copies in the branch file. Readers retain only the current and previous branch.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile

from .historical_replay_runtime import _atomic_write, _canonical_bytes, _read_json_bytes, _sha

SHARED_V1_VERSION = "historical-shared-v1-branch-v1"


class SharedV1Writer:
    def __init__(self, stream):
        self.stream = stream
        self.path = stream.root.parent / "shared-v1.jsonl"
        handle = tempfile.NamedTemporaryFile(mode="wb", dir=stream.root.parent,
                                             prefix=".shared-v1-", delete=False)
        self.handle, self.temporary = handle, Path(handle.name)
        self.digest = hashlib.sha256()
        self.count = 0

    def append(self, point, classification, lifecycle):
        from .historical_study_runtime import encode
        encoded = encode(classification, point)
        raw = _canonical_bytes({"boundary": point.evaluation_boundary_time_ms,
                "point_id": point.point_id,
                "movement_sha256": _sha(_canonical_bytes(point.movement_evaluation)),
                "source_sha256": _sha(_canonical_bytes(point.source_time_evidence)),
                "classification": encoded, "lifecycle": encode(lifecycle, point)})
        self.handle.write(raw)
        self.digest.update(raw)
        self.count += 1

    def publish(self):
        if not self.stream.validated_completion or self.count != len(self.stream):
            raise ValueError("shared V1 requires complete validated point consumption")
        self.handle.flush()
        os.fsync(self.handle.fileno())
        self.handle.close()
        try:
            os.link(self.temporary, self.path)
        except FileExistsError:
            if _file_sha(self.path) != self.digest.hexdigest():
                raise ValueError("conflicting shared V1 branch")
        self.temporary.unlink()
        metadata = {"version": SHARED_V1_VERSION, "identity": self.stream.identity,
                    "point_stream_sha256": self.stream.metadata["stream_sha256"],
                    "branch_sha256": self.digest.hexdigest(), "count": self.count}
        _atomic_write(self.path.with_suffix(".manifest.json"), _canonical_bytes(metadata))
        return SharedV1Reference(str(self.path), _sha(_canonical_bytes(metadata)))


def _file_sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class SharedV1Reference:
    path: str
    metadata_sha256: str

    def reader(self, stream):
        return SharedV1Reader(self, stream)


class SharedV1Reader:
    def __init__(self, reference, stream):
        self.reference, self.stream = reference, stream
        self.path = Path(reference.path)
        raw = self.path.with_suffix(".manifest.json").read_bytes()
        self.metadata = _read_json_bytes(raw)
        if (type(self.metadata) is not dict or set(self.metadata) != {
                "version", "identity", "point_stream_sha256", "branch_sha256", "count"}
                or raw != _canonical_bytes(self.metadata)
                or _sha(raw) != reference.metadata_sha256
                or self.metadata["version"] != SHARED_V1_VERSION
                or self.metadata["identity"] != stream.identity
                or self.metadata["point_stream_sha256"] != stream.metadata["stream_sha256"]
                or self.metadata["count"] != len(stream)):
            raise ValueError("shared V1 metadata identity mismatch")
        self.first_boundary = stream.metadata["first_boundary"]
        self.handle = self.path.open("rb")
        self.digest = hashlib.sha256()
        self.count = 0
        self.current = self.previous = None
        self.current_boundary = self.previous_boundary = None
        self.completed = False

    def branch_for_point(self, evaluation, evidence):
        from .historical_study_runtime import decode
        boundary = evaluation.evaluation_boundary_time_ms
        if boundary == self.first_boundary and self.count == self.metadata["count"]:
            self.finish()
            self.__init__(self.reference, self.stream)
        if boundary == self.current_boundary:
            return self.current
        expected = (self.first_boundary if self.current_boundary is None
                    else self.current_boundary + 5_000)
        if boundary != expected:
            raise ValueError("shared V1 reader requires sequential five-second points")
        raw = self.handle.readline()
        row = _read_json_bytes(raw)
        from .historical_replay import canonical_replay_point_id
        if (type(row) is not dict or set(row) != {"boundary", "point_id", "movement_sha256",
                                                 "source_sha256", "classification", "lifecycle"}
                or raw != _canonical_bytes(row) or row["boundary"] != boundary
                or row["point_id"] != canonical_replay_point_id(
                    self.metadata["identity"]["run_fingerprint"], boundary)
                or row["movement_sha256"] != _sha(_canonical_bytes(evaluation))
                or row["source_sha256"] != _sha(_canonical_bytes(evidence))):
            raise ValueError("shared V1 branch point/source mismatch")
        self.digest.update(raw)
        self.count += 1
        self.previous, self.previous_boundary = self.current, self.current_boundary
        self.current = (decode(row["classification"], (evaluation, evidence)),
                        decode(row["lifecycle"], (evaluation, evidence)))
        self.current_boundary = boundary
        return self.current

    def get(self, boundary):
        if boundary == self.current_boundary:
            return self.current
        if boundary == self.previous_boundary:
            return self.previous
        return None

    def finish(self):
        try:
            if (self.handle.read(1) or self.count != self.metadata["count"]
                    or self.digest.hexdigest() != self.metadata["branch_sha256"]):
                raise ValueError("shared V1 stream did not complete with matching digest/count")
            self.completed = True
        finally:
            self.handle.close()
