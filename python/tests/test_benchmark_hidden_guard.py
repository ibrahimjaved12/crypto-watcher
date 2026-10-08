"""Preregistration, append-only opening audit and hidden loader hooks."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from market_analysis.benchmark.canonical import canonical_bytes, content_hash
from market_analysis.benchmark.hidden_guard import (
    HiddenAlreadyOpened, HiddenGate, HiddenGuardError, HiddenStretchLocked,
    OpenToken, Plan, read_plan, require_access, require_months, write_plan,
)
from market_analysis.benchmark.segments import segment_bounds_ms

NOW = "2026-10-08T12:00:00Z"


def plan(**changes):
    values = dict(question_id="q-breakout", hypothesis="Breakouts exceed the threshold",
                  strategies=[dict(strategy_id="breakout", strategy_version="1", config={"window": 20})],
                  variants=1, statistic="mean_return", threshold="1/10", split_id="chronological-v1",
                  data_snapshot_id="snapshot-1", created_utc=NOW)
    values.update(changes)
    return Plan(**values)


class PlanTests(unittest.TestCase):
    def test_hash_stability_and_frozen_nested_data(self):
        strategies = [dict(strategy_id="breakout", strategy_version="1", config={"b": 2, "a": [1]})]
        record = plan(strategies=strategies)
        other = plan(strategies=[dict(config={"a": [1], "b": 2}, strategy_version="1", strategy_id="breakout")])
        self.assertEqual(record.plan_id, other.plan_id)
        self.assertEqual(record.plan_id, content_hash(record.to_fields()))
        strategies[0]["config"]["a"].append(2)
        record.strategies[0]["config"]["a"].append(3)
        self.assertEqual(record.plan_id, other.plan_id)
        with self.assertRaises(FrozenInstanceError):
            record.variants = 2
        for changes in ({"variants": 2}, {"threshold": "1/5"}, {"hypothesis": "other"},
                        {"question_id": "q-other"}, {"created_utc": "2026-10-09T12:00:00Z"}):
            self.assertNotEqual(record.plan_id, replace(record, **changes).plan_id)

    def test_validation(self):
        for changes in ({"variants": 0}, {"variants": True}, {"threshold": "0.1"},
                        {"threshold": "2/20"}, {"question_id": "../escape"},
                        {"created_utc": "2026-02-30T12:00:00Z"},
                        {"created_utc": "2026-10-08T12:00:00+00:00"},
                        {"strategies": []}, {"strategies": [{"strategy_id": "a"}]}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                plan(**changes)


class GateTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.opens = self.directory / "opens.jsonl"
        self.gate = HiddenGate(self.directory, self.opens)
        self.plan = plan()
        self.path = write_plan(self.directory, self.plan)

    def open(self, **kwargs):
        return self.gate.open_hidden(self.plan.question_id, self.plan.plan_id, now_utc=NOW, **kwargs)

    def test_plan_roundtrip_idempotence_and_tamper(self):
        self.assertEqual(self.path.name, f"q-breakout__{self.plan.plan_id[:16]}.plan.json")
        self.assertEqual(read_plan(self.path), self.plan)
        self.assertEqual(write_plan(self.directory, self.plan), self.path)
        self.assertEqual(self.path.read_bytes(), canonical_bytes({**self.plan.to_fields(), "plan_id": self.plan.plan_id}))
        self.path.write_bytes(self.path.read_bytes().replace(b"Breakouts", b"Something"))
        with self.assertRaisesRegex(HiddenGuardError, "hash mismatch"):
            read_plan(self.path)
        with self.assertRaisesRegex(HiddenGuardError, "overwrite"):
            write_plan(self.directory, self.plan)
        with self.assertRaises(HiddenGuardError):
            self.open()

    def test_missing_or_mismatched_plan(self):
        with self.assertRaises(FileNotFoundError):
            self.gate.open_hidden("absent", self.plan.plan_id, now_utc=NOW)
        other = plan(question_id="q-other")
        source = write_plan(self.directory, other)
        self.path.write_bytes(source.read_bytes())
        with self.assertRaisesRegex(HiddenGuardError, "identity"):
            self.open()
        self.assertFalse(self.opens.exists())

    def test_tokens_are_minted_immutable_and_file_verified(self):
        with self.assertRaises(TypeError):
            OpenToken(self.plan.question_id, self.plan.plan_id, NOW)
        with self.assertRaises(TypeError):
            OpenToken(self.plan.question_id, self.plan.plan_id, NOW, _mint=object())
        token = self.open()
        self.assertTrue(self.gate.verify(token))
        self.assertTrue(HiddenGate(self.directory, self.opens).verify(token))
        self.assertFalse(HiddenGate(self.directory, self.directory / "other.jsonl").verify(token))
        self.assertFalse(self.gate.verify(None))
        forged = object.__new__(OpenToken)
        for name in ("question_id", "plan_id", "opened_utc"):
            object.__setattr__(forged, name, getattr(token, name))
        self.assertFalse(self.gate.verify(forged))
        with self.assertRaises(FrozenInstanceError):
            token.plan_id = "0" * 64

    def test_second_open_refused_even_for_different_plan(self):
        self.open()
        with self.assertRaises(HiddenAlreadyOpened):
            self.open()
        other = replace(self.plan, variants=2)
        write_plan(self.directory, other)
        with self.assertRaises(HiddenAlreadyOpened):
            self.gate.open_hidden(other.question_id, other.plan_id, now_utc=NOW)
        self.assertEqual(len(self.gate.read()), 1)

    def test_force_requires_reason_and_is_recorded(self):
        self.open()
        for reason in ("", " \t"):
            with self.assertRaises(HiddenGuardError):
                self.open(force=True, reason=reason)
        token = self.open(force=True, reason="Explicit research reopening")
        rows = self.gate.read()
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertFalse(rows[0]["forced"])
        self.assertTrue(rows[1]["forced"])
        self.assertEqual(rows[1]["reason"], "Explicit research reopening")
        self.assertTrue(self.gate.verify(token))
        for row in rows:
            self.assertEqual(row["line_hash"], content_hash({k: v for k, v in row.items() if k != "line_hash"}))

    def test_one_append_write_and_fsync(self):
        import os
        with patch("market_analysis.benchmark.hidden_guard.os.write", wraps=os.write) as write, \
                patch("market_analysis.benchmark.hidden_guard.os.fsync", wraps=os.fsync) as fsync:
            self.open()
        write.assert_called_once()
        fsync.assert_called_once()
        self.assertTrue(write.call_args.args[1].endswith(b"\n"))

    def test_tampered_log_fails_closed(self):
        token = self.open()
        self.open(force=True, reason="Repeat")
        original = self.opens.read_bytes()
        lines = original.splitlines(keepends=True)
        for corrupt in (original.replace(b"Repeat", b"Edited"), original[:-1],
                        lines[1] + lines[0], lines[1], original + b"\n"):
            with self.subTest(corrupt=corrupt):
                self.opens.write_bytes(corrupt)
                with self.assertRaises(HiddenGuardError):
                    self.gate.verify(token)
                with self.assertRaises(HiddenGuardError):
                    self.open(force=True, reason="Retry")
        self.opens.write_bytes(original)
        row = self.gate.read()[0]
        row["seq"] = 2
        row["line_hash"] = content_hash({k: v for k, v in row.items() if k != "line_hash"})
        self.opens.write_bytes(canonical_bytes(row) + b"\n")
        with self.assertRaisesRegex(HiddenGuardError, "seq"):
            self.gate.read()

    def test_access_boundaries_and_month_lists(self):
        for name in ("development", "validation"):
            require_access(*segment_bounds_ms(name), None, self.gate)
        first, end = segment_bounds_ms("hidden")
        for left, right in ((first - 1, first), (end, end + 1), (first, first)):
            require_access(left, right, None, None)
        for left, right in ((first, first + 1), (first - 1, first + 1), (end - 1, end), (first, end)):
            with self.assertRaises(HiddenStretchLocked):
                require_access(left, right, None, self.gate)
        require_months(["2024-01", "2025-12", "2026-10"], None, None)
        require_months([], None, None)
        with self.assertRaises(HiddenStretchLocked):
            require_months(["2025-12", "2026-01"], None, self.gate)
        with self.assertRaises(HiddenStretchLocked):
            require_access(first, end, None, None)
        token = self.open()
        require_access(first, end, token, self.gate)
        require_months(["2026-01", "2026-09"], token, self.gate)
        with self.assertRaises(ValueError):
            require_access(end, first, token, self.gate)


if __name__ == "__main__":
    unittest.main()
