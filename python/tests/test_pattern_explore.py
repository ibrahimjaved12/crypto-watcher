"""Doji exploration (pattern_explore): paths, barriers, rules, matching, baseline, statistics, leads, guard, report."""
import csv
import gzip
import tempfile
import unittest
from pathlib import Path

import numpy as np

from market_analysis.benchmark.experiment_log import ExperimentLog
from market_analysis.benchmark.hidden_guard import HiddenStretchLocked
from market_analysis.patterns import pattern_explore as pe

START = 1_704_067_200_000  # 2024-01-01 00:00 UTC
H4 = 240 * 60_000
TEXT = (*pe.REQUIRED_TEXT, *pe.OPTIONAL_TEXT)
NUMERIC = (*pe.REQUIRED_NUMERIC, *pe.OPTIONAL_NUMERIC)


def frame_from(rows: list) -> dict:
    """A frame from row dicts (missing numeric -> NaN, missing text -> '')."""
    f = {name: np.array([float(r.get(name, np.nan)) for r in rows]) for name in NUMERIC}
    f.update({name: np.array([r.get(name, "") for r in rows], dtype=object) for name in TEXT})
    f["in_segment"] = f["segment"] == "development"
    return f


def candle(k, o, h, l, c, *, symbol="BTCUSDT", status="ok", segment="development", open_ms=None, **extra):
    span = h - l
    row = {"symbol": symbol, "status": status, "segment": segment, "tf": 240,
           "open_ms": START + k * H4 if open_ms is None else open_ms,
           "open": o, "high": h, "low": l, "close": c, "atr_prev": 10.0, "sigma_tf": 0.01,
           "body_ratio": abs(c - o) / span if span else np.nan, "range_atr": span / 10.0,
           "upper_ratio": (h - max(o, c)) / span if span else np.nan,
           "lower_ratio": (min(o, c) - l) / span if span else np.nan, "atr_pct_prev": 0.5, "rel_volume_prior20": 1.0}
    row["decision_ms"] = row["open_ms"] + H4
    row.update(extra)
    return row


def synthetic(symbols=("BTCUSDT", "ETHUSDT"), count=360, seed=1, drift_after_doji=0.0) -> dict:
    """Random 4h candles; every 7th candle is a doji (dragonfly or gravestone alternately), so events fall on
    every UTC hour of the 4h grid."""
    rng = np.random.default_rng(seed)
    rows = []
    for symbol in symbols:
        price = 100.0
        for k in range(count):
            o = price
            if k % 7 == 0:
                c = o * (1 + rng.normal(0, 0.0002))
                up = (k // 7) % 2 == 0
                h, l = (max(o, c) + 0.5, min(o, c) - 9) if up else (max(o, c) + 9, min(o, c) - 0.5)
                subtype = "dragonfly" if up else "gravestone"
            else:
                c = o * (1 + rng.normal(drift_after_doji if k % 7 == 1 else 0, 0.03))
                h, l = max(o, c) + abs(rng.normal(0, 1)), min(o, c) - abs(rng.normal(0, 1))
                subtype = ""
            rows.append(candle(k, o, h, l, c, symbol=symbol, doji_subtype=subtype,
                               atr_pct_prev=rng.uniform(), rel_volume_prior20=rng.lognormal(0, 0.3),
                               trend_dir=rng.choice([-1.0, 1.0]), touch_round=float(rng.random() < 0.2)))
            price = c
    return frame_from(rows)


class PathTests(unittest.TestCase):
    def test_gaps_invalid_segment_and_symbol_end_the_path(self):
        rows = [candle(k, 100, 101, 99, 100) for k in range(6)]
        rows[3]["status"] = "invalid"
        rows += [candle(10, 100, 101, 99, 100)]  # a gap: k = 6..9 missing
        rows += [candle(k, 100, 101, 99, 100, segment="validation") for k in (11, 12)]
        rows += [candle(k, 100, 101, 99, 100, symbol="ETHUSDT") for k in range(3)]
        f = frame_from(rows)
        p = pe.build_paths(f, [0, 4, 5, 6, 8], 4)
        self.assertEqual(p.length.tolist(), [2, 1, 0, 0, 0])  # invalid at 3; gap after 5; segment/symbol boundaries
        self.assertTrue(np.isnan(p.close[0, 2]))
        self.assertEqual(p.entry[0], 100)


class BarrierTests(unittest.TestCase):
    def path(self, candles):
        rows = [candle(0, 100, 101, 99, 100)] + [candle(k + 1, *c) for k, c in enumerate(candles)]
        f = frame_from(rows)
        return f, pe.build_paths(f, [0], len(candles))

    def test_first_passage_and_ambiguous(self):
        f, p = self.path([(100, 105, 95, 100), (100, 111, 99, 110)])  # atr 10: +-1 ATR = 110 / 90
        out = pe.barrier_outcomes(p, f, 1.0, 2)
        self.assertEqual((out["code"][0], out["time"][0]), (1, 2))
        f, p = self.path([(100, 111, 89, 100)])  # both barriers inside one candle
        self.assertEqual(pe.barrier_outcomes(p, f, 1.0, 1)["code"][0], 0)
        f, p = self.path([(100, 101, 99, 100), (100, 101, 99, 100)])
        self.assertEqual(pe.barrier_outcomes(p, f, 1.0, 2)["code"][0], 2)
        self.assertTrue(np.isnan(pe.barrier_outcomes(p, f, 1.0, 3)["code"][0]))  # path too short: unknown

    def test_excursion_map_never_resolves_ambiguity_optimistically(self):
        f, p = self.path([(100, 121, 89, 100)] + [(100, 101, 99, 100)] * 3)
        cells = pe.excursion_map(p, f, np.array([1.0]), np.array([-1]), p.entry, 3)
        cell = next(c for c in cells if c["stop_atr"] == 1.0 and c["target_atr"] == 2.0 and "time_exit_h" not in c)
        self.assertEqual(cell["ambiguous_share"], 1.0)
        self.assertTrue(np.isnan(cell["mean_gross_bp_excl_ambiguous"]))
        self.assertAlmostEqual(cell["mean_gross_bp_ambiguous_as_stop"], 1e4 * np.log(90 / 100))
        f, p = self.path([(100, 101, 99, 100), (85, 86, 80, 84)] + [(100, 101, 99, 100)] * 2)
        cell = next(c for c in pe.excursion_map(p, f, np.array([1.0]), np.array([-1]), p.entry, 3)
                    if c["stop_atr"] == 1.0 and c["target_atr"] == 1.0)
        self.assertAlmostEqual(cell["mean_gross_bp_excl_ambiguous"], 1e4 * np.log(85 / 100))  # gap: exit at the open


class RuleTests(unittest.TestCase):
    def test_break_direction(self):
        rows = [candle(0, 100, 105, 95, 100), candle(1, 100, 104, 96, 101), candle(2, 101, 107, 100, 106),
                candle(3, 106, 108, 104, 107), candle(4, 107, 109, 105, 108)]
        f = frame_from(rows)
        p = pe.build_paths(f, [0], 4)
        rule = pe.break_rule(p, f, 3)
        self.assertEqual((rule["direction"][0], rule["offset"][0], rule["entry"][0]), (1, 1, 106))
        self.assertEqual(pe.break_rule(p, f, 1)["direction"][0], 0)  # no close beyond 105/95 in candle i+1
        follow = pe.follow_through(p, f, rule["direction"], rule["offset"], rule["entry"], 2)
        self.assertAlmostEqual(follow["ret_sigma"][0], np.log(108 / 106) / 0.01)
        self.assertAlmostEqual(follow["mfe_atr"][0], (109 - 106) / 10)
        short = pe.build_paths(f, [3], 4)
        self.assertTrue(np.isnan(pe.break_rule(short, f, 3)["direction"][0]))  # path ended undecided

    def test_subtype_direction(self):
        rows = [candle(0, 100, 100.5, 90, 100, doji_subtype="dragonfly"),
                candle(1, 100, 110, 99.5, 100, doji_subtype="gravestone"),
                candle(2, 100, 105, 95, 100, doji_subtype="long_legged"),
                candle(3, 96, 100, 90, 99.6), candle(4, 100, 110, 99.5, 101)]
        f = frame_from(rows)
        events = np.array([True, True, True, False, False])
        self.assertEqual(pe.subtype_direction(f, np.arange(5), events).tolist(), [1, -1, 0, 1, -1])


class MatchingTests(unittest.TestCase):
    def test_match_weights(self):
        code = np.array([0, 0, 0, 1, 1, 2, -1])
        event = np.array([1, 0, 0, 1, 0, 1, 1], bool)
        ew, cw = pe.match_weights(code, event)
        self.assertEqual(ew.tolist(), [1, 0, 0, 1, 0, 0, 0])  # stratum 2 has no control; code -1 is unusable
        self.assertEqual(cw.tolist(), [0, 0.5, 0.5, 0, 1, 0, 0])
        self.assertEqual(pe.match_weights(np.array([-1, -1]), np.array([True, False]))[1].tolist(), [0, 0])

    def test_strata_codes(self):
        f = synthetic(count=60)
        rows = np.arange(len(f["tf"]))
        code = pe.strata_codes(f, rows, intraday=True)
        self.assertEqual(code.shape, rows.shape)
        f["atr_pct_prev"][3] = np.nan
        self.assertEqual(pe.strata_codes(f, rows, intraday=True)[3], -1)
        same = pe.strata_codes(f, rows, intraday=True, with_session=True)
        self.assertGreaterEqual(len(set(same.tolist())), len(set(pe.strata_codes(f, rows, intraday=True).tolist())))

    def test_baseline_is_seeded_and_hour_matched(self):
        f = synthetic(count=240)
        events = pe.event_mask(f)
        a, b = pe.baseline_mask(f, events), pe.baseline_mask(f, events)
        self.assertTrue((a == b).all())
        hours = pe.hours(f)
        for s in ("BTCUSDT", "ETHUSDT"):
            for h in range(0, 24, 4):
                cell = (f["symbol"] == s) & (hours == h)
                wanted = 2 * int((events & cell).sum())
                self.assertEqual(int((a & cell).sum()), min(wanted, int((pe.population(f) & cell).sum())))
        self.assertFalse((a & ~pe.population(f)).any())


class StatisticsTests(unittest.TestCase):
    def test_day_clustered_se_and_bootstrap(self):
        rng = np.random.default_rng(3)
        v = rng.normal(0, 1, 200)
        out = pe.describe(v, np.arange(200), B=200)
        self.assertAlmostEqual(out["se"], float(v.std(ddof=1) / np.sqrt(200)))  # one row per day: the iid SE
        self.assertLess(out["ci95"][0], out["mean"])
        self.assertGreater(out["ci95"][1], out["mean"])
        clustered = pe.describe(np.repeat(v[:50], 4), np.repeat(np.arange(50), 4))
        self.assertGreater(clustered["se"], pe.describe(np.repeat(v[:50], 4), np.arange(200))["se"])
        diff = pe.difference(v + 1, np.arange(200), v, np.arange(200), B=200)
        self.assertAlmostEqual(diff["difference"], 1.0)
        self.assertAlmostEqual(diff["se"], 0.0, places=9)  # paired by day: the shared noise cancels
        weighted = pe.describe(np.array([1.0, 3.0]), [0, 1], np.array([3.0, 1.0]))
        self.assertAlmostEqual(weighted["mean"], 1.5)

    def test_bh_counting(self):
        p = [0.01, 0.04, 0.03, float("nan"), 0.5]
        q = pe.bh_qvalues(p)
        self.assertAlmostEqual(q[0], 0.01 * 5 / 1)
        self.assertAlmostEqual(q[2], min(0.03 * 5 / 2, 0.04 * 5 / 3))
        self.assertTrue(np.isnan(q[3]))  # not estimable, but counted in m
        rows = [{"p": value} for value in p]
        self.assertEqual(pe.add_qvalues(rows), 5)
        self.assertIn("q_bh", rows[0])


class LeadTests(unittest.TestCase):
    def test_lead_rule(self):
        ok = pe.is_lead(2.5, 150, 1.0, [1.0, 1.0], [1, 1, 1, 1, -1, -1])
        self.assertEqual(ok, (True, True, 4))
        self.assertFalse(pe.is_lead(1.9, 150, 1.0, [1.0, 1.0], [1] * 6)[0])        # |t| < 2
        self.assertFalse(pe.is_lead(3.0, 99, 1.0, [1.0, 1.0], [1] * 6)[0])         # n < 100
        self.assertFalse(pe.is_lead(3.0, 150, 1.0, [1.0, -1.0], [1] * 6)[0])       # halves disagree
        self.assertFalse(pe.is_lead(3.0, 150, -1.0, [-1.0, -1.0], [-1, -1, -1, 1, 1, float("nan")])[0])
        rows = [{"family": "drift", "lead": True, "t": t, "tf": 240, "slice": "s", "bucket": "b", "h": 4}
                for t in (2.0, -5.0, 3.0)] + [{"family": "drift", "lead": False, "t": 9.0, "tf": 240, "slice": "s",
                                               "bucket": "b", "h": 4}]
        self.assertEqual([r["t"] for r in pe.top_leads(rows, 2)], [-5.0, 3.0])


class GuardTests(unittest.TestCase):
    def test_segment_guard(self):
        self.assertEqual(pe.check_explore_segment("development"), "development")
        with self.assertRaises(pe.ExploreError):
            pe.check_explore_segment("validation")
        with self.assertRaises(pe.ExploreError):
            pe.check_explore_segment("validation", "not-a-hash")
        self.assertEqual(pe.check_explore_segment("validation", "a" * 64), "validation")
        with self.assertRaises(HiddenStretchLocked):
            pe.check_explore_segment("hidden", "a" * 64)
        with self.assertRaises(pe.ExploreError):
            pe.check_tier("hourly")


class ExtensionTests(unittest.TestCase):
    """Intraday development = development-ext + development; paths never cross the kline / aggTrades boundary."""

    def test_study_segments_and_in_segment_membership(self):
        self.assertEqual(pe.STUDY_SEGMENTS["development"]["intraday"], ("development-ext", "development"))
        self.assertEqual(pe.STUDY_SEGMENTS["development"]["daily_weekly"], ("development-ext",))
        self.assertEqual(pe.STUDY_SEGMENTS["validation"]["intraday"], ("validation",))
        f = frame_from([candle(0, 100, 101, 99, 100, segment="development-ext"),
                        candle(1, 100, 101, 99, 100, segment="development"),
                        candle(2, 100, 101, 99, 100, segment="validation")])
        names = pe.STUDY_SEGMENTS["development"]["intraday"]
        self.assertEqual(np.isin(f["segment"], names).tolist(), [True, True, False])

    def test_paths_stop_at_the_bar_source_boundary(self):
        rows = [candle(k, 100, 101, 99, 100) for k in range(8)]
        f = frame_from(rows)
        f["bar_source"] = np.array(["kline"] * 4 + ["aggtrade"] * 4, dtype=object)
        p = pe.build_paths(f, [0, 2, 3, 4], 4)
        self.assertEqual(p.length.tolist(), [3, 1, 0, 3])  # the same segment, but never across kline -> aggtrade
        plain = pe.build_paths(frame_from(rows), [0, 2, 3, 4], 4)  # no bar_source column: one source
        self.assertEqual(plain.length.tolist(), [4, 4, 4, 3])

    def extended(self, with_ext: bool):
        """Doji-rich frame; with_ext adds 2023 kline rows (development-ext) before the 2024 aggTrades rows."""
        base = synthetic(symbols=("BTCUSDT",), count=240, seed=4)
        if not with_ext:
            return base
        ext = synthetic(symbols=("BTCUSDT",), count=240, seed=5)
        shift = -240 * H4
        ext["open_ms"] = ext["open_ms"] + shift
        ext["decision_ms"] = ext["decision_ms"] + shift
        ext["segment"] = np.array(["development-ext"] * 240, dtype=object)
        merged = {name: np.concatenate([ext[name], base[name]]) for name in base if name != "in_segment"}
        merged["bar_source"] = np.array(["kline"] * 240 + ["aggtrade"] * 240, dtype=object)
        merged["in_segment"] = np.isin(merged["segment"], pe.STUDY_SEGMENTS["development"]["intraday"])
        return merged

    def test_extension_rows_are_read_and_paths_do_not_cross(self):
        f = self.extended(True)
        pop = pe.population(f)
        self.assertEqual(int(pop[:240].sum()) > 0, True)  # development-ext rows are in the population
        last_ext = np.array([239])
        self.assertEqual(pe.build_paths(f, last_ext, 6).length.tolist(), [0])
        unit = pe.build_unit(f, 240, intraday=True)
        self.assertTrue(((f["bar_source"][unit.rows][unit.event] == "kline")).any())
        for k in range(6):  # a kline event row's path never touches aggtrade rows
            kline_rows = unit.rows[unit.paths.length > k][f["bar_source"][unit.rows[unit.paths.length > k]]
                                                          == "kline"]
            self.assertTrue((f["bar_source"][kline_rows + k + 1] == "kline").all())

    def test_a_table_without_extension_rows_gives_the_same_report(self):
        base = synthetic(symbols=("BTCUSDT",), count=200, seed=2)
        def report(frame):
            return pe.run_explore({240: frame}, tier="intraday", segment="development", table_report="reports/x",
                                  table_hash="f" * 64, code_commit="abc", created_utc="2026-10-16T00:00:00Z", B=10,
                                  absent=["touch_sig"], review_mode="open")[0]["report_hash"]
        reference = report(base)
        with_source = {**base, "bar_source": np.array(["aggtrade"] * 200, dtype=object)}
        with_set = {**base, "in_segment": np.isin(base["segment"], pe.STUDY_SEGMENTS["development"]["intraday"])}
        self.assertEqual(report(with_source), reference)  # an all-aggtrade bar_source column changes nothing
        self.assertEqual(report(with_set), reference)     # membership in the segment set = the old equality
        self.assertEqual(report({**base, "bar_source": np.array([""] * 200, dtype=object)}), reference)


class ReportTests(unittest.TestCase):
    def test_read_frame_keeps_neighbours_and_lists_absent_columns(self):
        f = synthetic(symbols=("BTCUSDT",), count=12)
        header = [*pe.REQUIRED_NUMERIC, *pe.REQUIRED_TEXT, "doji_subtype"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "candles__BTCUSDT__240__x.csv.gz"
            with gzip.open(path, "wt", encoding="ascii", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(header)
                for i in range(12):
                    seg = "development" if i < 10 else "validation"
                    writer.writerow([seg if n == "segment" else
                                     (f[n][i] if n in TEXT else repr(float(f[n][i]))) for n in header])
            frame, absent = pe.read_frame([path], "development")
            both, _ = pe.read_frame([path], ("development-ext", "development"))
            self.assertEqual(both["in_segment"].tolist(), frame["in_segment"].tolist())
            self.assertEqual(frame["bar_source"].tolist(), [""] * 12)  # absent column: one source
            self.assertNotIn("bar_source", absent)
        self.assertEqual(len(frame["tf"]), 12)
        self.assertEqual(int(frame["in_segment"].sum()), 10)
        self.assertIn("touch_sig", absent)
        self.assertTrue(np.isnan(frame["touch_sig"]).all())

    def test_round_trip_and_experiment_log(self):
        frames = {240: synthetic()}
        report, reviews = pe.run_explore(frames, tier="intraday", segment="development", table_report="reports/x",
                                         table_hash="f" * 64, code_commit="abc", created_utc="2026-10-16T00:00:00Z",
                                         B=20, absent=["touch_sig"], review=("touch:touch_round", "1"),
                                         review_mode="open")  # the old packs: old reports reproduce
        pe.check_report(report)
        self.assertEqual(report["by_tf"]["15"], {"status": "no rows"})
        self.assertGreater(report["tests_evaluated"], 0)
        self.assertEqual(report["tests_evaluated"], len(report["slices"]))
        self.assertTrue(pe.report_dir(report).startswith("reports/pattern-explore/doji-development__"))
        lines = pe.public_lines(report)
        self.assertTrue(lines[-1].endswith(report["report_hash"]))
        self.assertFalse(any("lead" in line for line in lines))
        files = pe.csv_files(report)
        self.assertEqual(set(files), {"direction_free.csv", "directional.csv", "excursion_map.csv", "slices.csv",
                                      "leads.csv"})
        self.assertIn("review_240_all.html", reviews)
        self.assertIn("review_240_touch-touch_round_1.html", reviews)
        self.assertIn("Sampling rule", reviews["review_240_all.html"])
        self.assertIn("# Doji exploration", pe.markdown(report, sorted(reviews)))
        tampered = {**report, "segment": "validation"}
        with self.assertRaises(pe.ExploreError):
            pe.check_report(tampered)
        records = pe.experiment_records(report, run_id="1", data_snapshot_id="f" * 64)
        keys = {(r["tf"], r["slice"], r["bucket"], r["family"]) for r in report["slices"]}
        self.assertEqual(len(records), len(keys))
        with tempfile.TemporaryDirectory() as tmp:
            log = ExperimentLog(Path(tmp) / "doji-explore.jsonl")
            log.append_many(records)
            self.assertEqual(log.trial_count(pe.QUESTION_ID), len(keys))
            log.append_many(pe.experiment_records(report, run_id="2", data_snapshot_id="f" * 64))
            self.assertEqual(log.trial_count(pe.QUESTION_ID), len(keys))  # a rerun is a REPLAY, not new variants

    def explore(self, frames, **kwargs):
        return pe.run_explore(frames, tier="intraday", segment="development", table_report="reports/x",
                              table_hash="f" * 64, code_commit="abc", created_utc="2026-10-16T00:00:00Z", B=10,
                              absent=["touch_sig"], **kwargs)

    def test_blind_review_is_the_default_and_open_mode_keeps_old_parameters(self):
        frames = {240: synthetic(symbols=("BTCUSDT",), count=240)}
        blind, files = self.explore(frames, review=("touch:touch_round", "1"))
        self.assertEqual(blind["parameters"]["review"]["mode"], "blind")
        self.assertEqual(blind["by_tf"]["240"]["review"]["mode"], "blind")
        self.assertEqual({"review_240_blind.html", "review_240_blind_key.csv", "review_240_blind_annotations.csv",
                          "review_240_blind_annotations_README.txt",
                          "review_240_touch-touch_round_1_blind.html", "review_240_touch-touch_round_1_blind_key.csv",
                          "review_240_touch-touch_round_1_blind_annotations.csv",
                          "review_240_touch-touch_round_1_blind_annotations_README.txt"}, set(files))
        self.assertNotIn("Sampling rule", files["review_240_blind.html"])
        self.assertIn("class", files["review_240_blind_key.csv"].splitlines()[0])
        self.assertNotIn("event", files["review_240_blind.html"].lower())
        self.assertIn("Blind review pack", pe.markdown(blind, sorted(files)))
        opened, _ = self.explore(frames, review_mode="open")
        self.assertNotIn("review", opened["parameters"])  # an open-mode report keeps its old parameters
        self.assertNotEqual(opened["report_hash"], blind["report_hash"])
        with self.assertRaises(pe.ExploreError):
            self.explore(frames, review_mode="x")

    def test_review_pack_is_never_misses_only(self):
        f = synthetic(count=120)
        unit = pe.build_unit(f, 240, intraday=True)
        sample = pe.review_sample(unit, n=10)
        pack = sample["packs"]["balanced_hits_misses"]
        self.assertEqual(pack.get("hits"), pack.get("misses"))
        unit.paths.close[:] = np.nan  # no outcome at all: no hits, so the balanced pack is withheld
        withheld = pe.review_sample(unit, n=10)["packs"]["balanced_hits_misses"]
        self.assertEqual(withheld["rows"], [])
        self.assertIn("withheld", withheld["rule"])


if __name__ == "__main__":
    unittest.main()
