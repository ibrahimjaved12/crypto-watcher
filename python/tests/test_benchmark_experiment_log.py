"""Append-only trial ledger for the #182 benchmark harness."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from market_analysis.benchmark.canonical import content_hash
from market_analysis.benchmark.experiment_log import (
    SCHEMA_VERSION, ExperimentLog, ExperimentLogError, TrialRecord, TrialStatus,
)


def trial(**changes) -> TrialRecord:
    values = dict(question_id="q-level-hits", family_id="breakout", strategy_id="donchian",
                  strategy_version="1", config={"window": 20, "symbols": ["BTCUSDT", "ETHUSDT"]},
                  split_id="development", data_snapshot_id="rd-2025-01", code_commit="abc123",
                  status=TrialStatus.OK, counts_toward_n=True, count_reason="", result_hash="f" * 64,
                  result_summary={"trades": 12, "net_pnl": "3/2", "note": None, "ok": True},
                  created_utc="2026-10-08T12:00:00Z")
    values.update(changes)
    return TrialRecord(**values)


class LogTestCase(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "trials.jsonl"
        self.log = ExperimentLog(self.path)


class TrialRecordTests(unittest.TestCase):
    def test_identity_ignores_family_status_results_and_time(self):
        base = trial()
        same = trial(family_id="other", status=TrialStatus.FAILED, result_hash=None, result_summary={},
                     created_utc="2027-01-01T00:00:00Z", code_commit="def456")
        self.assertEqual(base.trial_id, same.trial_id)
        self.assertEqual(base.trial_id, content_hash({
            "strategy_id": "donchian", "strategy_version": "1", "config": {"window": 20, "symbols": ["BTCUSDT", "ETHUSDT"]},
            "split_id": "development", "data_snapshot_id": "rd-2025-01", "question_id": "q-level-hits"}))
        for changes in ({"config": {"window": 21, "symbols": ["BTCUSDT", "ETHUSDT"]}}, {"split_id": "validation"},
                        {"strategy_version": "2"}, {"question_id": "q2"}, {"data_snapshot_id": "rd-2025-02"}):
            with self.subTest(changes=changes):
                self.assertNotEqual(trial(**changes).trial_id, base.trial_id)

    def test_validation(self):
        invalid = ({"strategy_id": ""}, {"family_id": None}, {"config": {"x": 1.5}}, {"config": [1]},
                   {"result_summary": {"sharpe": 1.2}}, {"result_summary": {"nested": {"a": 1}}},
                   {"status": TrialStatus.REPLAY, "counts_toward_n": True, "count_reason": "x"},
                   {"counts_toward_n": False, "count_reason": ""}, {"created_utc": "2026-10-08 12:00:00"},
                   {"created_utc": "2026-13-08T12:00:00Z"}, {"status": "MAYBE"}, {"counts_toward_n": 1},
                   {"result_hash": ""})
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ExperimentLogError):
                trial(**changes)
        self.assertIs(trial(status="FAILED").status, TrialStatus.FAILED)


class AppendReadTests(LogTestCase):
    def test_missing_and_empty_logs_are_empty(self):
        self.assertEqual(self.log.read(), [])
        self.path.write_bytes(b"")
        self.assertEqual(self.log.read(), [])
        self.assertEqual(self.log.trial_count(), 0)

    def test_round_trip_and_line_format(self):
        first = self.log.append(trial())
        second = self.log.append(trial(strategy_id="keltner", status=TrialStatus.FAILED))
        self.assertEqual(self.log.read(), [(1, first), (2, second)])
        lines = self.path.read_bytes().split(b"\n")
        self.assertEqual(lines[-1], b"")
        self.assertTrue(lines[0].startswith(b'{"code_commit":"abc123"'))
        self.assertIn(f'"schema":"{SCHEMA_VERSION}"'.encode(), lines[0])
        self.assertIn(b'"kind":"trial"', lines[0])
        self.assertIn(b'"seq":2', lines[1])
        self.assertIn(f'"trial_id":"{first.trial_id}"'.encode(), lines[0])

    def test_missing_parent_directory_raises(self):
        log = ExperimentLog(self.path.parent / "absent" / "trials.jsonl")
        with self.assertRaises(ExperimentLogError):
            log.append(trial())
        self.assertFalse((self.path.parent / "absent").exists())

    def test_replay_conversion_single_appends(self):
        self.log.append(trial())
        replay = self.log.append(trial(created_utc="2026-10-09T00:00:00Z"))
        self.assertEqual((replay.status, replay.counts_toward_n, replay.count_reason),
                         (TrialStatus.REPLAY, False, "replay of line 1"))
        self.assertEqual(self.log.read()[1], (2, replay))
        self.assertEqual(self.log.trial_count(), 1)

    def test_failed_lines_do_not_trigger_replay_but_count_once(self):
        self.log.append(trial(status=TrialStatus.FAILED))
        retry = self.log.append(trial(status=TrialStatus.FAILED))
        self.assertIs(retry.status, TrialStatus.FAILED)
        ok = self.log.append(trial())
        self.assertIs(ok.status, TrialStatus.OK)
        self.log.append(trial(status=TrialStatus.ABANDONED, strategy_id="keltner"))
        self.log.append(trial(status=TrialStatus.ABANDONED, strategy_id="keltner"))
        self.assertEqual(self.log.trial_count(), 2)  # donchian once, keltner once
        self.assertEqual(self.log.counted_trial_ids(), [trial().trial_id, trial(strategy_id="keltner").trial_id])

    def test_replay_conversion_within_a_batch(self):
        written = self.log.append_many([trial(), trial(strategy_id="keltner"), trial(created_utc="2026-10-10T00:00:00Z")])
        self.assertEqual([record.status for record in written], [TrialStatus.OK, TrialStatus.OK, TrialStatus.REPLAY])
        self.assertEqual(written[2].count_reason, "replay of line 1")
        later = self.log.append_many([trial(strategy_id="keltner")])
        self.assertEqual(later[0].count_reason, "replay of line 2")
        self.assertEqual([seq for seq, _ in self.log.read()], [1, 2, 3, 4])
        self.assertEqual(self.log.trial_count(), 2)
        self.assertEqual(self.log.append_many([]), [])

    def test_trial_count_filters(self):
        self.log.append_many([
            trial(),
            trial(strategy_id="keltner", family_id="channel"),
            trial(strategy_id="rsi", question_id="q-next-candle", family_id="oscillator"),
            trial(strategy_id="bollinger", counts_toward_n=False, count_reason="smoke test"),
        ])
        self.assertEqual(self.log.trial_count(), 3)
        self.assertEqual(self.log.trial_count(question_id="q-level-hits"), 2)
        self.assertEqual(self.log.trial_count(family_id="channel"), 1)
        self.assertEqual(self.log.trial_count(question_id="q-next-candle", family_id="breakout"), 0)
        self.assertEqual(self.log.trial_count(question_id="absent"), 0)

    def test_append_many_is_all_or_nothing(self):
        self.log.append(trial())
        before = self.path.read_bytes()
        # The first records are valid and already converted when the bad one is reached.
        with self.assertRaises(ExperimentLogError):
            self.log.append_many([trial(strategy_id="keltner"), trial(), "not a record"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.log.read()), 1)


class TamperTests(LogTestCase):
    def setUp(self):
        super().setUp()
        self.log.append_many([trial(), trial(strategy_id="keltner"), trial(strategy_id="rsi")])
        self.lines = self.path.read_bytes().split(b"\n")[:-1]

    def rewrite(self, lines):
        self.path.write_bytes(b"".join(line + b"\n" for line in lines))

    def assert_line_error(self, line):
        with self.assertRaisesRegex(ExperimentLogError, rf"trials\.jsonl line {line}: "):
            self.log.read()
        with self.assertRaises(ExperimentLogError):
            self.log.append(trial(strategy_id="new"))  # a corrupt log is never appended to

    def test_edited_byte_in_the_middle_of_a_line(self):
        edited = self.lines[1].replace(b'"split_id":"development"', b'"split_id":"developmenu"')
        self.assertNotEqual(edited, self.lines[1])
        self.rewrite([self.lines[0], edited, self.lines[2]])
        self.assert_line_error(2)

    def test_deleted_line(self):
        self.rewrite([self.lines[0], self.lines[2]])
        self.assert_line_error(2)

    def test_swapped_lines(self):
        self.rewrite([self.lines[0], self.lines[2], self.lines[1]])
        self.assert_line_error(2)

    def test_truncated_last_line(self):
        self.path.write_bytes(b"".join(line + b"\n" for line in self.lines[:2]) + self.lines[2][:40])
        self.assert_line_error(3)

    def test_non_canonical_and_invalid_json(self):
        self.rewrite([self.lines[0], self.lines[1].replace(b'","', b'", "', 1), self.lines[2]])
        self.assert_line_error(2)
        self.rewrite([self.lines[0], self.lines[1], b"{not json"])
        self.assert_line_error(3)


if __name__ == "__main__":
    unittest.main()
