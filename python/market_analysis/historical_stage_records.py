"""Verified stage evidence retained on disk, with repeatable bounded readers."""
from dataclasses import dataclass
import hashlib
from pathlib import Path

from .historical_replay_runtime import _canonical_bytes, _read_json_bytes


@dataclass(frozen=True)
class StageRecords:
    path: str
    count: int
    sha256: str
    scientific_path: str | None = None
    scientific_sha256: str | None = None

    def __post_init__(self):
        if type(self.count) is not int or self.count < 0 or not isinstance(self.path, str) or not self.path:
            raise ValueError("invalid stage record reference")
        for sha in (self.sha256, self.scientific_sha256):
            if sha is not None and (type(sha) is not str or len(sha) != 64
                                    or any(char not in "0123456789abcdef" for char in sha)):
                raise ValueError("invalid stage record SHA")
        if (self.scientific_path is None) != (self.scientific_sha256 is None):
            raise ValueError("incomplete scientific record reference")

    def __len__(self):
        return self.count

    def __iter__(self):
        from .historical_study_runtime import decode
        digest = hashlib.sha256()
        count = 0
        with Path(self.path).open("rb") as stream:
            for raw in stream:
                value = _read_json_bytes(raw)
                if raw != _canonical_bytes(value):
                    raise ValueError("noncanonical stage record")
                digest.update(raw)
                count += 1
                yield decode(value)
        if count != self.count or digest.hexdigest() != self.sha256:
            raise ValueError("stage records count/digest mismatch")

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
        digest, count, separator = hashlib.sha256(), 0, ""
        with Path(self.scientific_path).open("rb") as stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    raise ValueError("truncated scientific stage record")
                digest.update(raw)
                count += 1
                yield separator
                yield raw[:-1].decode("ascii")
                separator = ","
        if count != self.count or digest.hexdigest() != self.scientific_sha256:
            raise ValueError("scientific stage records count/SHA mismatch")

    def __study_json_chunks__(self):
        yield "["
        yield from self.__study_array_content__()
        yield "]"

    def verify(self):
        digest = hashlib.sha256()
        count = 0
        with Path(self.path).open("rb") as stream:
            for raw in stream:
                if not raw.endswith(b"\n"):
                    raise ValueError("truncated stage records")
                digest.update(raw)
                count += 1
        if count != self.count or digest.hexdigest() != self.sha256:
            raise ValueError("stage records count/digest mismatch")
        if self.scientific_path is not None:
            from .historical_shared_v1 import _file_sha
            if _file_sha(self.scientific_path) != self.scientific_sha256:
                raise ValueError("scientific stage records SHA mismatch")


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


def extract_candidate_inputs(records):
    compact, bocpd = [], []
    for item in records:
        if item.evidence_kind in ("EVENT", "RETROSPECTIVE"):
            compact.append(CandidateDecision(item.experiment_id, item.algorithm_version,
                item.config_version, item.decision_time_ms, item.evidence_kind))
        if item.experiment_id == "EXP-75-04B" and item.evidence_kind == "EVENT":
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
