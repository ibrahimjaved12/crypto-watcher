"""doji-geo-v3 (pattern_explore): frozen variant registry, core = doji-geo-v2, guard-banded controls, tier column,
variants in the experiment log, and the pilot's 'soft' class."""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from market_analysis.benchmark.experiment_log import ExperimentLog
from market_analysis.patterns import pattern_explore as pe
from market_analysis.patterns import pattern_pilot as pp
from test_pattern_explore import candle, frame_from, synthetic
from test_pattern_pilot import make_frame

DEFINITION_HASH = "9c15e507a16173e11a3a906287dbd84cd0121cd394146ae6e6943177b132ae98"
# body_ratio, range_atr of eight hand-made rows
ROWS = [(0.10, 1.0), (0.12, 1.0), (0.15, 1.0), (0.1501, 1.0), (0.12, 2.0), (0.05, 0.4), (0.20, 1.0), (0.2001, 1.0)]


def shapes() -> dict:
    f = make_frame(len(ROWS))
    f["body_ratio"] = np.array([r[0] for r in ROWS])
    f["range_atr"] = np.array([r[1] for r in ROWS])
    return f


def mixed_frame(count=210) -> dict:
    """Random-walk 4h candles of range 1 ATR: every 7th a core doji, every 7th + 3 a soft one (body 0.11),
    the rest ordinary bodies of 0.3-0.8 of the range."""
    rng = np.random.default_rng(5)
    rows, price = [], 100.0
    for k in range(count):
        span = 10.0
        body = 0.02 if k % 7 == 0 else 0.11 if k % 7 == 3 else float(rng.uniform(0.3, 0.8))
        sign = 1.0 if rng.random() < 0.5 else -1.0
        o, c = price, price + sign * body * span
        pad = (span - abs(c - o)) / 2
        rows.append(candle(k, o, max(o, c) + pad, min(o, c) - pad, c, doji_subtype="standard" if body < 0.1 else "",
                           atr_pct_prev=float(rng.uniform()), rel_volume_prior20=float(rng.lognormal(0, 0.3))))
        price = c
    return frame_from(rows)


class RegistryTests(unittest.TestCase):
    def test_definitions_are_frozen(self):
        self.assertEqual(pe.geo_v3_hash(), DEFINITION_HASH)
        rules = pe.geo_v3_rules()
        self.assertEqual(rules["core"]["event"], "body_ratio <= 0.10 and range_atr >= 0.50")
        self.assertIn("0.10 < body_ratio <= 0.15 and range_atr >= 0.50)", rules["soft15"]["event"])
        self.assertTrue(rules["softatr"]["event"].endswith("and body_ratio*range_atr <= 0.12)"))
        self.assertEqual({k: v["control"] for k, v in rules.items()},
                         {"core": "body_ratio > 0.15 and range_atr >= 0.50",
                          "soft15": "body_ratio > 0.20 and range_atr >= 0.50",
                          "softatr": "body_ratio > 0.20 and range_atr >= 0.50"})
        with self.assertRaises(pe.ExploreError):
            pe.check_variant("soft20")

    def test_changing_a_threshold_changes_the_hash(self):
        saved = dict(pe.GEO_V3["soft15"])
        try:
            pe.GEO_V3["soft15"]["soft_body_max"] = 0.16
            self.assertNotEqual(pe.geo_v3_hash(), DEFINITION_HASH)
        finally:
            pe.GEO_V3["soft15"].update(saved)
        self.assertEqual(pe.geo_v3_hash(), DEFINITION_HASH)


class MaskTests(unittest.TestCase):
    def test_core_is_exactly_doji_geo_v2(self):
        for f in (shapes(), synthetic(symbols=("BTCUSDT",), count=120)):
            v2 = pe.event_mask(f)
            for name in pe.GEO_V3:
                core, soft = pe.variant_tiers(f, name)
                self.assertEqual(core.tolist(), v2.tolist())
                self.assertFalse((core & soft).any())
            self.assertFalse(pe.variant_tiers(f, "core")[1].any())

    def test_core_unit_equals_the_v2_unit(self):
        f = synthetic(symbols=("BTCUSDT",), count=120)
        old = pe.build_unit(f, 240, intraday=True)
        new = pe.build_unit(f, 240, intraday=True, event_rows=pe.event_mask(f), control_rows=pe.control_mask(f))
        for name in ("rows", "event", "control", "baseline", "code", "event_w", "control_w"):
            self.assertEqual(getattr(old, name).tolist(), getattr(new, name).tolist())

    def test_soft_tiers_and_tier_column(self):
        f = shapes()
        soft = lambda v: np.flatnonzero(pe.variant_tiers(f, v)[1]).tolist()  # noqa: E731
        self.assertEqual(soft("soft15"), [1, 2, 4])   # (0.10, 0.15], range_atr >= 0.5
        self.assertEqual(soft("softatr"), [1])        # 0.12*1.0 <= 0.12; 0.15*1.0 and 0.12*2.0 exceed it
        self.assertEqual(soft("core"), [])
        tier = pe.variant_tier(f, "soft15")
        self.assertEqual(tier.tolist(), ["core", "soft", "soft", "", "soft", "", "", ""])
        self.assertEqual(pe.variant_tier(f, "softatr").tolist(), ["core", "soft", "", "", "", "", "", ""])
        self.assertEqual(set(pe.variant_tier(f, "core").tolist()), {"core", ""})

    def test_guard_band_keeps_boundary_candles_out_of_controls(self):
        f = shapes()
        controls = lambda v: np.flatnonzero(pe.variant_control_mask(f, v)).tolist()  # noqa: E731
        self.assertEqual(controls("core"), [3, 6, 7])    # > 0.15; 0.12 and 0.15 sit in the guard band
        self.assertEqual(controls("soft15"), [7])        # > 0.20; 0.20 itself is in the band
        self.assertEqual(controls("softatr"), [7])
        for name in pe.GEO_V3:
            events = pe.variant_event_mask(f, name)
            self.assertFalse((events & pe.variant_control_mask(f, name)).any())
            self.assertTrue(set(controls(name)) <= set(np.flatnonzero(pe.control_mask(f)).tolist()))  # v2 controls


class RunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report, cls.extra = pe.run_variants(
            {240: mixed_frame()}, tier="intraday", segment="development", table_report="reports/x",
            table_hash="f" * 64, code_commit="abc", created_utc="2026-10-16T00:00:00Z", B=0, absent=["touch_sig"])

    def test_report_splits_core_and_soft_per_variant(self):
        r = self.report
        pe.check_variants_report(r)
        self.assertEqual(r["definition_hash"], DEFINITION_HASH)
        block = r["by_tf"]["240"]
        events = {v: {g: int(item["counts"]["event"]["rows"]) for g, item in groups.items() if "counts" in item}
                  for v, groups in block.items()}
        self.assertEqual(set(events["core"]), {"all"})
        self.assertEqual(set(events["soft15"]), {"all", "core", "soft"})
        self.assertGreater(events["soft15"]["soft"], 0)
        self.assertEqual(events["soft15"]["all"], events["soft15"]["core"] + events["soft15"]["soft"])
        self.assertEqual(events["core"]["all"], events["soft15"]["core"])
        self.assertEqual(events["softatr"]["core"], events["core"]["all"])
        controls = lambda v, g: int(block[v][g]["counts"]["control_matched"]["rows"])  # noqa: E731
        self.assertLessEqual(controls("soft15", "all"), controls("core", "all"))  # a wider guard band
        self.assertEqual({row["variant"] for row in r["slices"]}, set(pe.GEO_V3))
        self.assertTrue({"all", "core", "soft"} <= {row["group"] for row in r["slices"] if row["variant"] != "core"})
        self.assertEqual(r["tests_evaluated"], len(r["slices"]))
        self.assertTrue(pe.variants_report_dir(r).startswith("reports/pattern-explore/doji-geo-v3-development__"))
        self.assertTrue(any("variant=soft15 group=soft" in line for line in pe.variants_public_lines(r)))
        self.assertIn("doji-geo-v3", pe.variants_markdown(r))
        self.assertIn("slices.csv", pe.variants_csv_files(r, self.extra))

    def test_events_file_carries_the_tier_column(self):
        lines = self.extra["events.csv"].splitlines()
        self.assertEqual(lines[0].split(",")[-3:], ["tier_core", "tier_soft15", "tier_softatr"])
        tiers = {tuple(line.split(",")[-3:]) for line in lines[1:]}
        self.assertEqual(tiers, {("core", "core", "core"), ("", "soft", "soft")})

    def test_every_variant_is_logged_with_its_definition_hash(self):
        records = pe.variants_experiment_records(self.report, run_id="1", data_snapshot_id="f" * 64)
        self.assertEqual({r.config["event"] for r in records}, {f"doji-geo-v3:{v}" for v in pe.GEO_V3})
        self.assertEqual({r.config["definition_hash"] for r in records}, {DEFINITION_HASH})
        self.assertEqual({r.config["tier_group"] for r in records}, {"all", "core", "soft"})
        with tempfile.TemporaryDirectory() as tmp:
            log = ExperimentLog(Path(tmp) / "doji-explore.jsonl")
            log.append_many(records)
            self.assertEqual(log.trial_count(pe.QUESTION_ID), len(records))  # each variant x group is a new variant
            log.append_many(pe.variants_experiment_records(self.report, run_id="2", data_snapshot_id="f" * 64))
            self.assertEqual(log.trial_count(pe.QUESTION_ID), len(records))  # a rerun is a REPLAY

    def test_tampered_definition_or_segment_is_refused(self):
        with self.assertRaises(pe.ExploreError):
            pe.check_variants_report({**self.report, "definition_hash": "0" * 64})
        with self.assertRaises(pe.ExploreError):
            pe.run_variants({}, tier="intraday", segment="validation", table_report="x", table_hash="f" * 64,
                            code_commit="a", created_utc="2026-10-16T00:00:00Z")


class PilotSoftClassTests(unittest.TestCase):
    def test_default_pilot_is_unchanged(self):
        f = shapes()
        plain = pp.sample_frame(f, 60, per_stratum=2, seed=1)
        self.assertNotIn("soft", pp.class_masks(f))
        self.assertEqual(plain, pp.sample_frame(f, 60, per_stratum=2, seed=1, variant=None))
        with_core = pp.sample_frame(f, 60, per_stratum=2, seed=1, variant="core")
        self.assertEqual([c for c in with_core["counts"] if c["class"] != "soft"], plain["counts"])
        self.assertEqual(pp.class_masks(f, variant="core")["soft"].sum(), 0)

    def test_soft_class_is_the_extra_candles_and_classes_stay_disjoint(self):
        f = shapes()
        rows = lambda m: np.flatnonzero(m).tolist()  # noqa: E731
        m15 = pp.class_masks(f, variant="soft15")
        self.assertEqual((rows(m15["soft"]), rows(m15["near_miss"]), rows(m15["control"])), ([1, 2, 4], [], [7]))
        mat = pp.class_masks(f, variant="softatr")
        self.assertEqual((rows(mat["soft"]), rows(mat["near_miss"])), ([1], [2, 4]))
        for masks in (m15, mat):
            self.assertTrue((sum(m.astype(int) for m in masks.values()) <= 1).all())
        picked = pp.sample_frame(f, 60, per_stratum=2, seed=1, variant="soft15")
        self.assertEqual({s["row"] for s in picked["samples"] if s["class"] == "soft"}, {1, 2, 4})
        self.assertEqual(pp.RATE_KIND["soft"], "false_positive")

    def test_manifest_gains_variant_keys_only_for_a_variant_run(self):
        kw = dict(symbol="BTCUSDT", table_report="r", table_hash="a" * 64, window=(0, 1), per_stratum=2, seed=1,
                  mode="blind", counts=[], files={}, code_commit="c", created_utc="2026-10-16T00:00:00Z")
        plain, soft = pp.manifest(**kw), pp.manifest(**kw, variant="soft15")
        self.assertNotIn("variant", plain)
        self.assertEqual((soft["variant"], soft["definition_hash"]), ("soft15", DEFINITION_HASH))
        self.assertIn("soft", soft["classes"])
        self.assertNotEqual(plain["pilot_hash"], soft["pilot_hash"])


if __name__ == "__main__":
    unittest.main()
