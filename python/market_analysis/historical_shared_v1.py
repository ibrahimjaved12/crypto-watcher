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


# Opt-in, per-process cache of fully verified branches (batch workers only).
# Key: resolved file path, size, mtime_ns, manifest SHA, count and branch SHA.
_PROCESS_CACHE_ENABLED = False
_VERIFIED_BRANCHES = {}


def enable_process_cache():
    """Let later readers in this process reuse a branch an earlier reader verified."""
    global _PROCESS_CACHE_ENABLED
    _PROCESS_CACHE_ENABLED = True


@dataclass(frozen=True)
class _VerifiedBranch:
    # Per row: (evaluation, evidence, decoded branch, file offset). The decoded
    # branch references those exact point objects, so it is reused only for them.
    rows: tuple
    branch_sha256: str


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
        self._cache_key = self._cached = self._collected = None
        self._closed = False
        if _PROCESS_CACHE_ENABLED:
            self._cache_key = self._file_key()
            self._cached = _VERIFIED_BRANCHES.get(self._cache_key)
            if self._cached is None:
                self._collected = []
        self.handle = None if self._cached is not None else self.path.open("rb")
        self.digest = hashlib.sha256()
        self.count = 0
        self.current = self.previous = None
        self.current_boundary = self.previous_boundary = None
        self.completed = False

    def _file_key(self):
        status = self.path.stat()
        return (str(self.path.resolve()), status.st_size, status.st_mtime_ns,
                self.reference.metadata_sha256, self.metadata["count"],
                self.metadata["branch_sha256"])

    def _checked_branch(self, raw, boundary, evaluation, evidence):
        from .historical_study_runtime import decode
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
        return (decode(row["classification"], (evaluation, evidence)),
                decode(row["lifecycle"], (evaluation, evidence)))

    def branch_for_point(self, evaluation, evidence):
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
        if self._cached is not None:
            current = self._cached_branch(boundary, evaluation, evidence)
        else:
            offset = (self.handle.tell() if self._collected is not None
                      and not self.handle.closed else None)
            raw = self.handle.readline()
            current = self._checked_branch(raw, boundary, evaluation, evidence)
            self.digest.update(raw)
            if self._collected is not None:
                self._collected.append((evaluation, evidence, current, offset))
        self.count += 1
        self.previous, self.previous_boundary = self.current, self.current_boundary
        self.current = current
        self.current_boundary = boundary
        return self.current

    def _cached_branch(self, boundary, evaluation, evidence):
        # Mirrors a fresh reader: closed-file and end-of-file errors included.
        if self._closed:
            raise ValueError("readline of closed file")
        rows = self._cached.rows
        if self.count >= len(rows):
            _read_json_bytes(b"")
            raise ValueError("shared V1 branch point/source mismatch")
        cached_evaluation, cached_evidence, current, offset = rows[self.count]
        if evaluation is cached_evaluation and evidence is cached_evidence:
            # Same objects that were hashed against this verified row before.
            return current
        with self.path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.readline()
        return self._checked_branch(raw, boundary, evaluation, evidence)

    def get(self, boundary):
        if boundary == self.current_boundary:
            return self.current
        if boundary == self.previous_boundary:
            return self.previous
        return None

    def finish(self):
        if self._cached is not None:
            if self._closed:
                raise ValueError("read of closed file")
            self._closed = True
            # The verified file has exactly count rows hashing to branch_sha256.
            if (self.count != self.metadata["count"]
                    or self._cached.branch_sha256 != self.metadata["branch_sha256"]):
                raise ValueError("shared V1 stream did not complete with matching digest/count")
            self.completed = True
            return
        try:
            if (self.handle.read(1) or self.count != self.metadata["count"]
                    or self.digest.hexdigest() != self.metadata["branch_sha256"]):
                raise ValueError("shared V1 stream did not complete with matching digest/count")
            self.completed = True
        finally:
            self.handle.close()
        collected, self._collected = self._collected, None
        if (collected is not None and len(collected) == self.metadata["count"]
                and self._file_key() == self._cache_key):
            _VERIFIED_BRANCHES[self._cache_key] = _VerifiedBranch(
                tuple(collected), self.digest.hexdigest())
