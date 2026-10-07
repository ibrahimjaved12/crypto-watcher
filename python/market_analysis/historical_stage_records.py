"""Verified stage evidence retained on disk, with repeatable bounded readers."""
from dataclasses import dataclass
import hashlib
import gzip
import io
import json
import zlib
from pathlib import Path

from .historical_replay_runtime import _canonical_bytes, _read_json_bytes

# Successful StageRecords.verify() passes in this process, keyed by the full
# declared reference and each stream file's identity. Failures are never kept.
_VERIFIED = set()


def _canonical_record_bytes(value):
    """``_canonical_bytes`` for a parsed JSON record, without the safe-graph copy.

    Parsed JSON holds only str/int/float/bool/None/list/str-keyed dict, for
    which ``study_json_safe`` is the identity, so the spelling is the same.
    """
    try:
        return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    except ValueError:
        # Out-of-range floats (e.g. 1e999): raise exactly the reference error.
        return _canonical_bytes(value)


class _StoredReader(io.RawIOBase):
    """Hash stored bytes as the buffered/decompression reader consumes them."""
    def __init__(self, handle, digest):
        super().__init__()
        self.handle, self.digest = handle, digest

    def readable(self):
        return True

    def readinto(self, buffer):
        raw = self.handle.read(len(buffer))
        self.digest.update(raw)
        buffer[:len(raw)] = raw
        return len(raw)


@dataclass(frozen=True)
class StageRecords:
    path: str
    count: int
    sha256: str
    scientific_path: str | None = None
    scientific_sha256: str | None = None
    storage_encoding: str = "identity"
    stored_sha256: str | None = None
    scientific_stored_sha256: str | None = None

    def __post_init__(self):
        if type(self.count) is not int or self.count < 0 or not isinstance(self.path, str) or not self.path:
            raise ValueError("invalid stage record reference")
        if self.storage_encoding not in ("identity", "gzip"):
            raise ValueError("invalid stage storage encoding")
        for sha in (self.sha256, self.scientific_sha256, self.stored_sha256, self.scientific_stored_sha256):
            if sha is not None and (type(sha) is not str or len(sha) != 64
                                    or any(char not in "0123456789abcdef" for char in sha)):
                raise ValueError("invalid stage record SHA")
        if self.sha256 is None or (self.scientific_path is None) != (self.scientific_sha256 is None):
            raise ValueError("incomplete scientific record reference")
        if self.scientific_path is not None and (type(self.scientific_path) is not str or not self.scientific_path):
            raise ValueError("invalid scientific record path")
        if self.scientific_path is None and self.scientific_stored_sha256 is not None:
            raise ValueError("orphan scientific storage hash")
        if self.storage_encoding == "gzip" and (self.stored_sha256 is None or
                self.scientific_path is not None and self.scientific_stored_sha256 is None):
            raise ValueError("gzip stage records require stored hashes")

    def __len__(self):
        return self.count

    def _lines(self, scientific=False, *, check_canonical=True, with_parsed=False):
        """Verified raw lines; ``with_parsed`` yields ``(raw, value)`` where
        ``value`` is the operational parse made by the canonical check (else None)."""
        path = self.scientific_path if scientific else self.path
        expected = self.scientific_sha256 if scientific else self.sha256
        stored = self.scientific_stored_sha256 if scientific else self.stored_sha256
        logical_digest, stored_digest, count = hashlib.sha256(), hashlib.sha256(), 0
        try:
            with Path(path).open("rb") as handle, io.BufferedReader(_StoredReader(handle, stored_digest)) as reader:
                stream = gzip.GzipFile(fileobj=reader, mode="rb") if self.storage_encoding == "gzip" else reader
                try:
                    for raw in stream:
                        if not raw.endswith(b"\n"):
                            raise ValueError("truncated stage records")
                        value = None
                        if scientific:
                            raw.decode("ascii")
                        elif check_canonical:
                            value = _read_json_bytes(raw)
                            if raw != _canonical_record_bytes(value):
                                raise ValueError("noncanonical stage record")
                        logical_digest.update(raw)
                        count += 1
                        yield (raw, value) if with_parsed else raw
                finally:
                    if stream is not reader:
                        stream.close()
        except (gzip.BadGzipFile, EOFError, zlib.error, UnicodeError) as exc:
            raise ValueError("corrupt stage record storage") from exc
        if count != self.count or logical_digest.hexdigest() != expected:
            raise ValueError("stage records count/logical SHA mismatch")
        if stored is not None and stored_digest.hexdigest() != stored:
            raise ValueError("stage records stored SHA mismatch")

    def __iter__(self):
        from .historical_study_runtime import decode
        for raw in self._lines():
            yield decode(_read_json_bytes(raw))

    def __study_items__(self):
        return iter(self)

    def __study_array_content__(self):
        if self.scientific_path is None:
            from .historical_market_state_study_json import iter_canonical_study_json
            separator = ""
            for record in self:
                yield separator
                yield from iter_canonical_study_json(record)
                separator = ","
            return
        separator = ""
        for raw in self._lines(scientific=True):
            yield separator
            yield raw[:-1].decode("ascii")
            separator = ","

    def __study_json_chunks__(self):
        yield "["
        yield from self.__study_array_content__()
        yield "]"

    def _verification_key(self):
        """Every declared value ``_lines()`` reads or compares against, plus each
        stream file's identity, or None. Explicit values: never the dataclass hash."""
        declared = (self.path, self.scientific_path, self.storage_encoding, self.count,
                    self.sha256, self.stored_sha256,
                    self.scientific_sha256, self.scientific_stored_sha256)
        files = []
        try:
            for path in (self.path, self.scientific_path):
                if path is None:
                    continue
                resolved = Path(path).resolve()
                status = resolved.stat()
                files.append((str(resolved), status.st_size, status.st_mtime_ns,
                              status.st_ctime_ns, status.st_ino, status.st_dev))
        except OSError:
            return None
        return declared, tuple(files)

    def _verified(self):
        key = self._verification_key()
        return key is not None and key in _VERIFIED

    def verify(self):
        key = self._verification_key()
        if key is not None and key in _VERIFIED:
            return
        for _ in self._lines():
            pass
        if self.scientific_path is not None:
            for _ in self._lines(scientific=True):
                pass
        # Remember only when the files did not change while being verified.
        if key is not None and self._verification_key() == key:
            _VERIFIED.add(key)


@dataclass(frozen=True)
class JoinedStageRecords:
    sources: tuple

    def __len__(self):
        return sum(len(source) for source in self.sources)

    def __iter__(self):
        for source in self.sources:
            yield from source

    def __study_items__(self):
        return iter(self)

    def __study_array_content__(self):
        separator = ""
        for source in self.sources:
            if not len(source):
                continue
            yield separator
            if hasattr(source, "__study_array_content__"):
                yield from source.__study_array_content__()
            else:
                from .historical_market_state_study_json import iter_canonical_study_json
                inner_separator = ""
                for record in source:
                    yield inner_separator
                    yield from iter_canonical_study_json(record)
                    inner_separator = ","
            separator = ","

    def __study_json_chunks__(self):
        yield "["
        yield from self.__study_array_content__()
        yield "]"


@dataclass(frozen=True)
class CandidateDecision:
    experiment_id: str
    algorithm_version: str
    config_version: str
    decision_time_ms: int | None
    evidence_kind: str


_DECISION_FIELDS = ("experiment_id", "algorithm_version", "config_version",
                    "decision_time_ms", "evidence_kind")


def _leaf_sources(records):
    if isinstance(records, JoinedStageRecords):
        for source in records.sources:
            yield from _leaf_sources(source)
    else:
        yield records


def _decision_fields(raw):
    """The five plain decision scalars of one scientific line, or None."""
    value = json.loads(raw)
    if type(value) is not dict:
        return None
    fields = tuple(value.get(name) for name in _DECISION_FIELDS)
    if (not all(name in value for name in _DECISION_FIELDS)
            or any(type(fields[index]) is not str for index in (0, 1, 2, 4))
            or not (fields[3] is None or type(fields[3]) is int)):
        return None
    return fields


def _candidate_decisions(source):
    """Yield ``(experiment_id, algorithm_version, config_version,
    decision_time_ms, evidence_kind, item_or_None)`` in record order.

    Both streams are read in lockstep and keep their full count/SHA checks.

    ``extract_candidate_inputs`` calls ``verify()`` first, so the reference is
    normally verified (memoized) here and takes the light path: decision
    scalars come from the scientific stream, and only EXP-75-04B EVENT lines
    (or lines whose scientific scalars are not plain) are decoded.

    The fallback, for a reference whose verification could not be memoized
    (e.g. its file identity raised OSError), runs the canonical check and the
    full typed decode of every operational line (reusing the canonical
    check's parse), and every record's decision scalars must match the
    scientific stream. The producing worker only encodes its validated typed
    records (``_canonical_bytes(encode(item))``) and never decodes them back,
    so this is the pass that proves the bytes decode. When both streams were
    fully consumed and the files are unchanged, the reference is memoized as
    ``verify()`` would.
    """
    if not isinstance(source, StageRecords) or source.scientific_path is None:
        for item in source:
            yield (item.experiment_id, item.algorithm_version, item.config_version,
                   item.decision_time_ms, item.evidence_kind, item)
        return
    from .historical_study_runtime import decode
    key = source._verification_key()
    verified = key is not None and key in _VERIFIED
    operational = source._lines(check_canonical=not verified, with_parsed=True)
    try:
        for science in source._lines(scientific=True):
            line = next(operational, None)
            if line is None:
                # Operational passed its count check, so the scientific stream is too long.
                raise ValueError("stage records count/logical SHA mismatch")
            raw, parsed = line
            fields = _decision_fields(science)
            if not verified:
                item = decode(parsed)
                actual = (item.experiment_id, item.algorithm_version, item.config_version,
                          item.decision_time_ms, item.evidence_kind)
                if fields is not None and actual != fields:
                    raise ValueError("stage records stream mismatch")
                yield (*actual, item)
            elif fields is None or (fields[0] == "EXP-75-04B" and fields[4] == "EVENT"):
                item = decode(_read_json_bytes(raw))
                yield (item.experiment_id, item.algorithm_version, item.config_version,
                       item.decision_time_ms, item.evidence_kind, item)
            else:
                yield (*fields, None)
        for _ in operational:
            pass  # Longer operational stream fails its own count/SHA check.
    finally:
        operational.close()
    # Both streams passed every verify() check (canonical ones included):
    # same success-only, unchanged-files rule as verify().
    if not verified and key is not None and source._verification_key() == key:
        _VERIFIED.add(key)


def extract_candidate_inputs(records):
    compact, bocpd = [], []
    for source in _leaf_sources(records):
        if isinstance(source, StageRecords) and source.scientific_path is not None:
            source.verify()  # memoized; failures raise exactly as verify() does
        for (experiment_id, algorithm_version, config_version, decision_time_ms,
             evidence_kind, item) in _candidate_decisions(source):
            if evidence_kind in ("EVENT", "RETROSPECTIVE"):
                compact.append(CandidateDecision(experiment_id, algorithm_version,
                    config_version, decision_time_ms, evidence_kind))
            if experiment_id == "EXP-75-04B" and evidence_kind == "EVENT":
                bocpd.append(item)
    from .historical_market_state_study_execution import _bocpd_onset_evidence
    return tuple(compact), _bocpd_onset_evidence(bocpd)


@dataclass(frozen=True)
class FilteredStageRecords:
    source: StageRecords
    experiment_id: str
    matching: bool

    def __iter__(self):
        for record in self.source:
            if (record["experiment_id"] == self.experiment_id) == self.matching:
                yield record

    def __study_items__(self):
        return iter(self)


@dataclass(frozen=True)
class StageValue:
    path: str
    sha256: str
    block_sha256: str

    def verify(self):
        from .historical_shared_v1 import _file_sha
        if _file_sha(self.path) != self.sha256:
            raise ValueError("stage scientific value SHA mismatch")

    def __study_json_chunks__(self):
        digest = hashlib.sha256()
        with Path(self.path).open("rb") as stream:
            for raw in iter(lambda: stream.read(1024 * 1024), b""):
                # Scientific JSON is ASCII, so byte chunk boundaries are safe.
                digest.update(raw)
                yield raw.decode("ascii")
        if digest.hexdigest() != self.sha256:
            raise ValueError("stage scientific value SHA mismatch")
