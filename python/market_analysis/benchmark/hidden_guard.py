"""Preregistered, auditable hidden-stretch access (single writer per gate).

Tokens are in-process capabilities, not a security boundary against code that
deliberately accesses module-private internals. No wall clock is consulted.
Sequence and content hashes detect corruption, not adversarially rehashed logs
or removal of a complete suffix without an external checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime
import json
import os
from pathlib import Path
import re
from weakref import WeakSet

from .canonical import canonical_bytes, content_hash, exact_from_str
from .segments import _range, segment_bounds_ms
from ..data_lake import month_bounds_ms

_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_MINT = object()
_TOKENS: WeakSet = WeakSet()


class HiddenGuardError(ValueError):
    """Invalid plan or corrupt opening ledger."""


class HiddenAlreadyOpened(HiddenGuardError):
    """A question has already opened the hidden stretch."""


class HiddenStretchLocked(PermissionError):
    """Hidden data requires a verified opening token."""


def _text(value, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise HiddenGuardError(f"{name} must be a non-empty string")


def _question(value) -> None:
    _text(value, "question_id")
    if any(char in value for char in ("/", "\\", "\0")) or value in (".", ".."):
        raise HiddenGuardError("question_id must be a safe filename component")


def _utc(value) -> None:
    if type(value) is not str or not _UTC.fullmatch(value):
        raise HiddenGuardError("timestamp must be YYYY-MM-DDTHH:MM:SSZ")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as error:
        raise HiddenGuardError(f"invalid UTC timestamp: {value!r}") from error


def _hash(value) -> None:
    if type(value) is not str or not _HASH.fullmatch(value):
        raise HiddenGuardError("plan_id must be a sha256 hex digest")


@dataclass(frozen=True)
class Plan:
    question_id: str
    hypothesis: str
    strategies: list[dict]
    variants: int
    statistic: str
    threshold: str
    split_id: str
    data_snapshot_id: str
    created_utc: str
    schema: str = field(default="plan-v1", init=False)

    def __post_init__(self):
        _question(self.question_id)
        for name in ("hypothesis", "statistic", "split_id", "data_snapshot_id"):
            _text(getattr(self, name), name)
        if type(self.variants) is not int or self.variants < 1:
            raise HiddenGuardError("variants must be an integer >= 1")
        _utc(self.created_utc)
        exact_from_str(self.threshold)
        if type(self.strategies) is not list or not self.strategies:
            raise HiddenGuardError("strategies must be a non-empty list")
        for strategy in self.strategies:
            if type(strategy) is not dict or set(strategy) != {"strategy_id", "strategy_version", "config"}:
                raise HiddenGuardError("unexpected strategy fields")
            for name in ("strategy_id", "strategy_version"):
                _text(strategy[name], name)
            if type(strategy["config"]) is not dict:
                raise HiddenGuardError("strategy config must be a dict")
        # Retain canonical bytes privately; expose fresh lists so nested mutations
        # cannot change a frozen plan or its identity.
        object.__setattr__(self, "strategies", canonical_bytes(self.strategies))

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        if name == "strategies" and type(value) is bytes:
            return json.loads(value)
        return value

    def to_fields(self) -> dict:
        return {item.name: getattr(self, item.name) for item in fields(self)}

    @property
    def plan_id(self) -> str:
        return content_hash(self.to_fields())


def _plan_path(plans_dir, question_id: str, plan_id: str) -> Path:
    _question(question_id)
    _hash(plan_id)
    return Path(plans_dir) / f"{question_id}__{plan_id[:16]}.plan.json"


def write_plan(plans_dir, plan: Plan) -> Path:
    payload = canonical_bytes({**plan.to_fields(), "plan_id": plan.plan_id})
    path = _plan_path(plans_dir, plan.question_id, plan.plan_id)
    try:
        with path.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        if path.read_bytes() != payload:
            raise HiddenGuardError(f"refusing to overwrite different plan: {path}") from None
    return path


def _load(raw: bytes) -> dict:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise HiddenGuardError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=unique)
        if type(value) is not dict or canonical_bytes(value) != raw:
            raise HiddenGuardError("record is not canonical JSON")
        return value
    except (ValueError, TypeError, UnicodeError) as error:
        raise HiddenGuardError(f"invalid canonical record: {error}") from error


def read_plan(path) -> Plan:
    value = _load(Path(path).read_bytes())
    if set(value) != {item.name for item in fields(Plan)} | {"plan_id"} or value["schema"] != "plan-v1":
        raise HiddenGuardError("unexpected plan fields/schema")
    plan_id = value.pop("plan_id")
    value.pop("schema")
    plan = Plan(**value)
    if plan.plan_id != plan_id:
        raise HiddenGuardError("plan_id hash mismatch")
    return plan


@dataclass(frozen=True, eq=False, init=False)
class OpenToken:
    question_id: str
    plan_id: str
    opened_utc: str

    def __init__(self, question_id: str, plan_id: str, opened_utc: str, *, _mint=None):
        if _mint is not _MINT:
            raise TypeError("OpenToken can only be minted by HiddenGate")
        object.__setattr__(self, "question_id", question_id)
        object.__setattr__(self, "plan_id", plan_id)
        object.__setattr__(self, "opened_utc", opened_utc)
        _TOKENS.add(self)


class HiddenGate:
    def __init__(self, plans_dir, opens_path):
        self.plans_dir, self.opens_path = Path(plans_dir), Path(opens_path)

    def read(self) -> list[dict]:
        """Validate every line, including sequence, hash and reopening policy."""
        try:
            raw = self.opens_path.read_bytes()
        except FileNotFoundError:
            return []
        if not raw:
            return []
        if not raw.endswith(b"\n"):
            raise HiddenGuardError("opening log has no trailing newline")
        records, seen = [], set()
        for number, line in enumerate(raw.splitlines(), 1):
            try:
                value = _load(line)
                if set(value) != {"schema", "seq", "question_id", "plan_id", "opened_utc",
                                  "forced", "reason", "line_hash"}:
                    raise HiddenGuardError("unexpected opening fields")
                if value["schema"] != "hidden-open-v1":
                    raise HiddenGuardError("unsupported opening schema")
                if type(value["seq"]) is not int or value["seq"] != number:
                    raise HiddenGuardError("seq does not match line number")
                if value["line_hash"] != content_hash({k: v for k, v in value.items() if k != "line_hash"}):
                    raise HiddenGuardError("line_hash mismatch")
                _question(value["question_id"])
                _hash(value["plan_id"])
                _utc(value["opened_utc"])
                if type(value["forced"]) is not bool or type(value["reason"]) is not str:
                    raise HiddenGuardError("invalid forced/reason fields")
                if value["forced"] and not value["reason"].strip():
                    raise HiddenGuardError("forced opening needs a reason")
                if value["question_id"] in seen and not value["forced"]:
                    raise HiddenGuardError("unforced repeated opening")
                seen.add(value["question_id"])
                records.append(value)
            except HiddenGuardError as error:
                raise HiddenGuardError(f"{self.opens_path.name} line {number}: {error}") from error
        return records

    def open_hidden(self, question_id: str, plan_id: str, *, now_utc: str,
                    force: bool = False, reason: str = "") -> OpenToken:
        _utc(now_utc)
        if type(force) is not bool or type(reason) is not str:
            raise HiddenGuardError("force must be bool and reason must be str")
        if force and not reason.strip():
            raise HiddenGuardError("forced opening needs a reason")
        plan = read_plan(_plan_path(self.plans_dir, question_id, plan_id))
        if plan.question_id != question_id or plan.plan_id != plan_id:
            raise HiddenGuardError("plan identity does not match requested opening")
        # Fixed-width ISO UTC strings order chronologically; a plan dated after the
        # opening was not registered before the hidden data could be seen.
        if plan.created_utc > now_utc:
            raise HiddenGuardError("plan is dated after the opening time")
        records = self.read()
        if any(row["question_id"] == question_id for row in records) and not force:
            raise HiddenAlreadyOpened(question_id)
        body = dict(schema="hidden-open-v1", seq=len(records) + 1, question_id=question_id,
                    plan_id=plan_id, opened_utc=now_utc, forced=force, reason=reason)
        payload = canonical_bytes({**body, "line_hash": content_hash(body)}) + b"\n"
        descriptor = os.open(self.opens_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            written = os.write(descriptor, payload)
            if written != len(payload):
                raise HiddenGuardError("short write to opening log")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return OpenToken(question_id, plan_id, now_utc, _mint=_MINT)

    def verify(self, token) -> bool:
        records = self.read()
        if type(token) is not OpenToken or token not in _TOKENS:
            return False
        return any(all(row[name] == getattr(token, name)
                       for name in ("question_id", "plan_id", "opened_utc")) for row in records)

    def plan_for(self, token) -> Plan:
        """The pre-registered plan a verified token was opened under."""
        if not self.verify(token):
            raise HiddenStretchLocked("hidden stretch requires a verified opening token")
        plan = read_plan(_plan_path(self.plans_dir, token.question_id, token.plan_id))
        if plan.question_id != token.question_id or plan.plan_id != token.plan_id:
            raise HiddenGuardError("plan identity does not match the opening token")
        return plan


def require_access(first_ms: int, end_ms: int, token, gate: HiddenGate | None) -> None:
    """Loader hook: authorize a half-open data range before reading it."""
    _range(first_ms, end_ms)
    first, end = segment_bounds_ms("hidden")
    if first_ms < end_ms and first_ms < end and first < end_ms:
        if gate is None or not gate.verify(token):
            raise HiddenStretchLocked("hidden stretch requires a verified opening token")


def require_months(months, token, gate: HiddenGate | None) -> None:
    """Loader hook for a possibly non-contiguous list of UTC months."""
    for month in months:
        first, end, _ = month_bounds_ms(month)
        require_access(first, end, token, gate)
