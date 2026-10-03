"""Append-only trial ledger for the #182 benchmark harness.

The experiment log is a research notebook with one line per evaluated variant
(strategy, version, config, split, data snapshot, question). It exists so the
number of variants tried can be counted for multiple-testing correction. It is
NOT the app's signal/outcome logging.

Each line is the canonical JSON of the record plus ``schema``, ``kind``, a
1-based ``seq`` equal to its line number, the content-derived ``trial_id`` and a
``line_hash`` over everything else, so edits, deletions and reordering are
detected on read. Re-running a variant whose identity already has an OK line is
recorded as REPLAY and never counts toward N again.

Concurrency: a single ``append``/``append_many`` is one ``os.write`` of complete
lines to a file opened with ``O_APPEND``, followed by ``fsync``, so a line is
never interleaved with another process's line. Duplicate (REPLAY) detection and
``seq`` numbering read the file first and are NOT safe against two concurrent
writers. The intended use is a single writer per file: the merge step of a run.
"""
from __future__ import annotations

from dataclasses import dataclass, fields, replace
from datetime import datetime
from enum import Enum
import json
import os
from pathlib import Path
import re

from .canonical import canonical_bytes, content_hash

SCHEMA_VERSION = "experiment-log-v1"
KIND = "trial"
_CREATED_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_ID_FIELDS = ("question_id", "family_id", "strategy_id", "strategy_version", "split_id", "data_snapshot_id",
              "code_commit")
_IDENTITY_FIELDS = ("strategy_id", "strategy_version", "config", "split_id", "data_snapshot_id", "question_id")


class TrialStatus(str, Enum):
    OK = "OK"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"
    REPLAY = "REPLAY"


class ExperimentLogError(ValueError):
    """An invalid record or a corrupt/tampered log line (message names the line)."""


@dataclass(frozen=True)
class TrialRecord:
    question_id: str
    family_id: str
    strategy_id: str
    strategy_version: str
    config: dict
    split_id: str
    data_snapshot_id: str
    code_commit: str
    status: TrialStatus
    counts_toward_n: bool
    count_reason: str
    result_hash: str | None
    result_summary: dict
    created_utc: str

    def __post_init__(self):
        for name in _ID_FIELDS:
            value = getattr(self, name)
            if type(value) is not str or not value:
                raise ExperimentLogError(f"{name} must be a non-empty string, got {value!r}")
        if type(self.config) is not dict:
            raise ExperimentLogError(f"config must be a dict, got {type(self.config).__name__}")
        try:
            # Canonical copies: later mutation of the caller's dicts cannot change the record.
            object.__setattr__(self, "config", json.loads(canonical_bytes(self.config)))
        except TypeError as error:
            raise ExperimentLogError(f"config is not canonicalizable: {error}") from None
        if type(self.result_summary) is not dict:
            raise ExperimentLogError(f"result_summary must be a dict, got {type(self.result_summary).__name__}")
        for key, value in self.result_summary.items():
            if type(key) is not str:
                raise ExperimentLogError(f"result_summary key {key!r} must be a string")
            if not (value is None or type(value) in (str, int, bool)):
                raise ExperimentLogError(f"result_summary.{key} must be str, int, bool or None, "
                                         f"got {type(value).__name__}")
        object.__setattr__(self, "result_summary", dict(self.result_summary))
        if not isinstance(self.status, TrialStatus):
            try:
                object.__setattr__(self, "status", TrialStatus(self.status))
            except ValueError:
                raise ExperimentLogError(f"unknown trial status: {self.status!r}") from None
        if type(self.counts_toward_n) is not bool:
            raise ExperimentLogError(f"counts_toward_n must be a bool, got {self.counts_toward_n!r}")
        if type(self.count_reason) is not str:
            raise ExperimentLogError(f"count_reason must be a string, got {self.count_reason!r}")
        if self.status is TrialStatus.REPLAY and self.counts_toward_n:
            raise ExperimentLogError("a REPLAY trial never counts toward N")
        if not self.counts_toward_n and not self.count_reason:
            raise ExperimentLogError("a trial that does not count toward N needs a count_reason")
        if self.result_hash is not None and (type(self.result_hash) is not str or not self.result_hash):
            raise ExperimentLogError(f"result_hash must be None or a non-empty string, got {self.result_hash!r}")
        if type(self.created_utc) is not str or not _CREATED_UTC.match(self.created_utc):
            raise ExperimentLogError(f"created_utc must be YYYY-MM-DDTHH:MM:SSZ, got {self.created_utc!r}")
        try:
            datetime.strptime(self.created_utc, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ExperimentLogError(f"created_utc is not a valid UTC time: {self.created_utc!r}") from None

    @property
    def trial_id(self) -> str:
        """Identity of the evaluated variant; family, status, results and time are not part of it."""
        return content_hash({name: getattr(self, name) for name in _IDENTITY_FIELDS})

    def to_fields(self) -> dict:
        values = {field.name: getattr(self, field.name) for field in fields(self)}
        values["status"] = self.status.value
        return values


_LINE_KEYS = frozenset({"schema", "kind", "seq", "trial_id", "line_hash", *(field.name for field in fields(TrialRecord))})


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


class ExperimentLog:
    """One append-only experiment log file (single writer; see the module docstring)."""

    def __init__(self, path) -> None:
        self.path = Path(path)

    # ------------------------------------------------------------------ reading

    def _error(self, line: int, message: str) -> ExperimentLogError:
        return ExperimentLogError(f"{self.path.name} line {line}: {message}")

    def read(self) -> list[tuple[int, TrialRecord]]:
        """Every (seq, record), fully verified; a missing or empty file is an empty log."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return []
        if not data:
            return []
        lines = data.split(b"\n")
        if lines[-1]:
            raise self._error(len(lines), "last line has no trailing newline (truncated write?)")
        return [(number, self._parse_line(number, raw)) for number, raw in enumerate(lines[:-1], start=1)]

    def _parse_line(self, number: int, raw: bytes) -> TrialRecord:
        try:
            value = json.loads(raw.decode("ascii"), object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        except (UnicodeError, ValueError) as error:
            raise self._error(number, f"not valid canonical JSON ({error})") from None
        if type(value) is not dict or set(value) != _LINE_KEYS:
            raise self._error(number, "unexpected line fields")
        try:
            canonical = canonical_bytes(value)
        except TypeError as error:
            raise self._error(number, f"not canonical: {error}") from None
        if canonical != raw:
            raise self._error(number, "line is not in canonical form")
        if value["schema"] != SCHEMA_VERSION or value["kind"] != KIND:
            raise self._error(number, f"unsupported schema/kind {value['schema']!r}/{value['kind']!r}")
        if type(value["seq"]) is not int or value["seq"] != number:
            raise self._error(number, f"seq {value['seq']!r} does not match the line number (deleted or moved line?)")
        body = {key: item for key, item in value.items() if key != "line_hash"}
        if value["line_hash"] != content_hash(body):
            raise self._error(number, "line_hash mismatch (edited line?)")
        try:
            record = TrialRecord(**{field.name: value[field.name] for field in fields(TrialRecord)})
        except ExperimentLogError as error:
            raise self._error(number, f"invalid record: {error}") from None
        if record.trial_id != value["trial_id"]:
            raise self._error(number, "trial_id does not match the record identity")
        return record

    # ------------------------------------------------------------------ writing

    def _line(self, seq: int, record: TrialRecord) -> bytes:
        body = {"schema": SCHEMA_VERSION, "kind": KIND, "seq": seq, **record.to_fields(), "trial_id": record.trial_id}
        return canonical_bytes({**body, "line_hash": content_hash(body)}) + b"\n"

    def _prepare(self, records) -> tuple[list[TrialRecord], bytes]:
        """Validate and convert a batch against the current file; nothing is written."""
        existing = self.read()
        first_ok: dict[str, int] = {}
        for seq, record in existing:
            if record.status is TrialStatus.OK:
                first_ok.setdefault(record.trial_id, seq)
        written, lines = [], []
        seq = len(existing)
        for record in records:
            if not isinstance(record, TrialRecord):
                raise ExperimentLogError(f"expected TrialRecord, got {type(record).__name__}")
            seq += 1
            trial_id = record.trial_id
            if trial_id in first_ok:
                record = replace(record, status=TrialStatus.REPLAY, counts_toward_n=False,
                                 count_reason=f"replay of line {first_ok[trial_id]}")
            elif record.status is TrialStatus.OK:
                first_ok[trial_id] = seq
            written.append(record)
            lines.append(self._line(seq, record))
        return written, b"".join(lines)

    def _write(self, payload: bytes) -> None:
        if not self.path.parent.is_dir():
            raise ExperimentLogError(f"log directory does not exist: {self.path.parent}")
        descriptor = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise ExperimentLogError(f"short write to {self.path.name}: {written} of {len(payload)} bytes")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def append(self, record: TrialRecord) -> TrialRecord:
        """Append one trial (converted to REPLAY if its identity already has an OK line)."""
        written, payload = self._prepare([record])
        self._write(payload)
        return written[0]

    def append_many(self, records) -> list[TrialRecord]:
        """All-or-nothing: validate and convert every record, then one write and one fsync."""
        written, payload = self._prepare(list(records))
        if payload:
            self._write(payload)
        return written

    # ------------------------------------------------------------------ counting

    def counted_trial_ids(self, question_id: str | None = None, family_id: str | None = None) -> list[str]:
        """Distinct trial_ids counting toward N (non-REPLAY, counts_toward_n), in first-appearance order."""
        seen: dict[str, None] = {}
        for _, record in self.read():
            if (record.counts_toward_n and record.status is not TrialStatus.REPLAY
                    and (question_id is None or record.question_id == question_id)
                    and (family_id is None or record.family_id == family_id)):
                seen.setdefault(record.trial_id, None)
        return list(seen)

    def trial_count(self, question_id: str | None = None, family_id: str | None = None) -> int:
        return len(self.counted_trial_ids(question_id, family_id))
