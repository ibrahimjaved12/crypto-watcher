"""Doji pilot: seeded stratified sampler, near-miss bounds, blind contact sheets, key separation, defect statistics."""
import importlib.util
from pathlib import Path
import sys
import unittest

import numpy as np

from market_analysis.patterns import pattern_pilot as pp

MIN60 = 60 * 60_000
START = 1_704_067_200_000  # 2024-01-01 00:00 UTC
SUBTYPES = ("dragonfly", "gravestone", "long_legged", "standard")


def make_frame(count=600, seed=3, tf=60, symbol="BTCUSDT"):
    """Consecutive candles with chosen body ratios (event / near-miss / control) and ranges 6..30 on ATR 10."""
    rng = np.random.default_rng(seed)
    rows = []
    for k in range(count):
        span = float(rng.uniform(6, 30))
        kind = rng.choice(["event", "near", "control", "tiny"], p=[0.3, 0.1, 0.4, 0.2])
        body = {"event": rng.uniform(0, 0.10), "near": rng.uniform(0.1001, 0.15), "control": rng.uniform(0.16, 0.9),
                "tiny": rng.uniform(0, 0.1)}[kind]
        if kind == "tiny":
            span = float(rng.uniform(1, 4.5))  # range_atr < 0.5: in no class
        rows.append((k, 100.0, span, body, SUBTYPES[int(rng.integers(0, 4))] if body <= 0.10 else ""))
    f = {"open_ms": np.array([START + k * MIN60 for k in range(count)], float), "tf": np.full(count, float(tf)),
         "symbol": np.array([symbol] * count, dtype=object), "status": np.array(["ok"] * count, dtype=object),
         "segment": np.array(["development"] * count, dtype=object)}
    f["decision_ms"] = f["open_ms"] + MIN60
    f["open"] = np.array([r[1] for r in rows])
    span = np.array([r[2] for r in rows])
    body = np.array([r[3] for r in rows])
    f["close"] = f["open"] + body * span
    f["high"] = f["close"] + (span - body * span) / 2
    f["low"] = f["open"] - (span - body * span) / 2
    f["body_ratio"], f["range_atr"] = body, span / 10.0
    f["atr_prev"], f["sigma_tf"] = np.full(count, 10.0), np.full(count, 0.01)
    f["doji_subtype"] = np.array([r[4] for r in rows], dtype=object)
    f["in_segment"] = np.ones(count, bool)
    return f


class ClassTests(unittest.TestCase):
    def test_boundaries_between_event_near_miss_and_control(self):
        f = make_frame(8)
        f["range_atr"][:] = 1.0
        f["body_ratio"][:] = [0.10, 0.1001, 0.15, 0.1501, 0.0, 0.5, 0.05, 0.12]
        f["range_atr"][5] = 0.49          # too small for any class
        f["status"][6] = "invalid"
        masks = pp.class_masks(f)
        order = lambda m: np.flatnonzero(m).tolist()  # noqa: E731
        self.assertEqual(order(masks["event"]), [0, 4])
        self.assertEqual(order(masks["near_miss"]), [1, 2, 7])
        self.assertEqual(order(masks["control"]), [3])
        stacked = masks["event"].astype(int) + masks["near_miss"] + masks["control"]
        self.assertTrue((stacked <= 1).all())  # disjoint
        window = pp.class_masks(f, (int(f["open_ms"][2]), int(f["open_ms"][5])))
        self.assertEqual((order(window["near_miss"]), order(window["event"]), order(window["control"])),
                         ([2], [4], [3]))


class SamplerTests(unittest.TestCase):
    def test_deterministic_and_seed_dependent(self):
        f = make_frame()
        a = pp.sample_frame(f, 60, per_stratum=3, seed=1)
        b = pp.sample_frame(f, 60, per_stratum=3, seed=1)
        self.assertEqual(a, b)
        c = pp.sample_frame(f, 60, per_stratum=3, seed=2)
        self.assertNotEqual([s["row"] for s in a["samples"]], [s["row"] for s in c["samples"]])
        self.assertEqual([s["row"] for s in pp.sample_frame(f, 60, per_stratum=3, seed=1)["samples"]],
                         [s["row"] for s in a["samples"]])

    def test_stratification_counts_and_no_padding(self):
        f = make_frame()
        out = pp.sample_frame(f, 60, per_stratum=4, seed=7)
        masks = pp.class_masks(f)
        by_stratum = {}
        for s in out["samples"]:
            by_stratum.setdefault((s["class"], s["stratum"]), []).append(s["row"])
            self.assertTrue(masks[s["class"]][s["row"]])  # every sample is in its class
        total = 0
        for row in out["counts"]:
            key = (row["class"], row["stratum"])
            expected = min(row["requested"], row["available"])
            self.assertEqual(row["sampled"], expected)
            self.assertEqual(len(by_stratum.get(key, [])), expected)
            self.assertEqual(len(set(by_stratum.get(key, []))), expected)  # without replacement
            total += expected
        self.assertEqual(total, len(out["samples"]))
        thin = pp.sample_frame(f, 60, per_stratum=40, seed=7)  # more than most strata hold: all rows, never padded
        short = [r for r in thin["counts"] if r["sampled"] < r["requested"]]
        self.assertTrue(short and all(r["sampled"] == r["available"] for r in short))
        events = [r for r in out["counts"] if r["class"] == "event"]
        self.assertTrue(all(len(r["stratum"].split("|")) == 3 for r in events))     # subtype|strength|tercile
        self.assertEqual({r["requested"] for r in out["counts"] if r["class"] == "control"}, {4 * pp.CONTROL_FACTOR})
        self.assertEqual({r["requested"] for r in out["counts"] if r["class"] == "near_miss"},
                         {4 * pp.NEAR_MISS_FACTOR})
        strengths = {r["stratum"].split("|")[1] for r in events}
        self.assertEqual(strengths, {"b<=0.05", "b0.05-0.10"})

    def test_near_miss_and_control_bounds(self):
        f = make_frame()
        for s in pp.sample_frame(f, 60, per_stratum=6, seed=3)["samples"]:
            body, size = f["body_ratio"][s["row"]], f["range_atr"][s["row"]]
            self.assertGreaterEqual(size, 0.5)
            if s["class"] == "near_miss":
                self.assertTrue(0.10 < body <= 0.15)
            elif s["class"] == "control":
                self.assertTrue(body > 0.15)
            else:
                self.assertTrue(body <= 0.10)

    def test_scope_and_single_stratum(self):
        f = make_frame()
        scope = np.zeros(len(f["tf"]), bool)
        scope[:100] = True
        out = pp.sample_frame(f, 60, per_stratum=5, seed=3, scope=scope, stratify=False)
        events = [s for s in out["samples"] if s["class"] == "event"]
        self.assertTrue(events and all(s["row"] < 100 and s["stratum"] == "all" for s in events))
        self.assertLessEqual(len(events), 5)

    def test_opaque_ids_are_unique_and_class_free(self):
        f = make_frame()
        samples = pp.sample_frame(f, 60, per_stratum=3, seed=1)["samples"]
        ids = pp.opaque_ids(f, 60, samples, 1)
        self.assertEqual(len(set(ids)), len(ids))
        self.assertTrue(all(i.startswith("P") and len(i) == 9 for i in ids))
        self.assertEqual(ids, pp.opaque_ids(f, 60, samples, 1))
        self.assertNotEqual(ids, pp.opaque_ids(f, 60, samples, 2))


class BlindTests(unittest.TestCase):
    def sheet(self, mode, row=45):
        f = make_frame(60)
        picked = {"samples": [{"row": row, "class": "event", "stratum": "dragonfly|b<=0.05|t1"}]}
        ids = pp.opaque_ids(f, 60, picked["samples"], 1)
        return f, ids, pp.contact_sheet(f, 60, picked["samples"], ids, mode, f"Doji pilot BTCUSDT 60m ({mode})")

    def test_blind_chart_ends_at_the_signal_candle_and_reveals_nothing(self):
        f, ids, html = self.sheet("blind")
        self.assertEqual(html.count("<line"), 40)       # one wick per candle: exactly 40 candles of context
        self.assertEqual(html.count('fill="#f2d16b"'), 1)  # the signal candle is marked ...
        after_marker = html.split('fill="#f2d16b"')[1]
        self.assertEqual(after_marker.count("<line"), 1)  # ... and only its own wick follows: nothing after it
        self.assertIn(ids[0], html)
        lowered = html.lower()
        for word in ("event", "near_miss", "control", "dragonfly", "body_ratio", "range_atr", "outcome", "return",
                     str(int(f["open_ms"][45]))):
            self.assertNotIn(word, lowered)
        # only the candles up to the signal change the drawing: change the later candles, the sheet is identical
        g = make_frame(60)
        g["high"][46:] += 500.0
        g["low"][46:] -= 500.0
        picked = [{"row": 45, "class": "event", "stratum": "x"}]
        self.assertEqual(html, pp.contact_sheet(g, 60, picked, pp.opaque_ids(g, 60, picked, 1), "blind",
                                                "Doji pilot BTCUSDT 60m (blind)"))

    def test_open_mode_shows_twelve_candles_after_and_the_fields(self):
        _, _, html = self.sheet("open")
        self.assertEqual(html.count("<line"), 52)
        self.assertIn("event", html)
        self.assertIn("body_ratio=", html)
        with self.assertRaises(pp.PilotError):
            self.sheet("x")

    def test_build_pilot_keeps_the_key_separate(self):
        f = make_frame()
        window = (int(f["open_ms"][0]), int(f["open_ms"][-1]) + MIN60)
        built = pp.build_pilot({60: f}, symbol="BTCUSDT", window=window, per_stratum=2, seed=5, mode="blind")
        files = built["files"]
        self.assertEqual(set(files), {"pilot_60.html", "pilot_key.csv", "annotations.csv"})
        key = pp.read_key(files["pilot_key.csv"])
        self.assertEqual(len(key), built["ids"])
        self.assertEqual(files["annotations.csv"].splitlines()[0], "id,detector_ok,defect_type,visual_strength,notes")
        self.assertEqual({line.split(",")[0] for line in files["annotations.csv"].splitlines()[1:]}, set(key))
        self.assertTrue(all(row["class"] in pp.CLASSES for row in key.values()))
        sheet = files["pilot_60.html"]
        self.assertTrue(all(i in sheet for i in key))
        self.assertNotIn("near_miss", sheet)
        manifest = pp.manifest(symbol="BTCUSDT", table_report="r", table_hash="h", window=window, per_stratum=2, seed=5,
                               mode="blind", counts=built["counts"], files=files, code_commit="a", created_utc="b")
        again = pp.build_pilot({60: f}, symbol="BTCUSDT", window=window, per_stratum=2, seed=5, mode="blind")
        self.assertEqual(manifest["pilot_hash"], pp.manifest(
            symbol="BTCUSDT", table_report="r", table_hash="h", window=window, per_stratum=2, seed=5, mode="blind",
            counts=again["counts"], files=again["files"], code_commit="c", created_utc="d")["pilot_hash"])
        text = "\n".join(pp.public_lines(manifest))
        self.assertIn("class=near_miss", text)
        self.assertTrue(all(i not in text for i in key))  # counts only, no ids
        self.assertTrue(pp.pilot_dir(manifest).startswith("reports/pattern-pilot/"))

    def test_window_must_lie_inside_the_development_segments(self):
        segments = lambda tf: (("development-ext", 1_609_459_200_000, 1_704_067_200_000),  # noqa: E731
                               ("development", 1_704_067_200_000, 1_751_328_000_000))
        self.assertEqual(pp.check_window((60,), "2025-01", "2025-06", segments)[0], 1_735_689_600_000)
        for bad in (("2020-12", "2021-03"), ("2025-01", "2025-07"), ("2025-06", "2025-01")):
            with self.assertRaises(pp.PilotError):
                pp.check_window((60,), *bad, segments)


class DefectTests(unittest.TestCase):
    KEY = ("id,symbol,tf,open_ms,decision_ms,class,stratum,body_ratio,range_atr,subtype\n"
           "P1,BTCUSDT,60,1,2,event,dragonfly|b<=0.05|t1,0.01,1.0,dragonfly\n"
           "P2,BTCUSDT,60,1,2,event,dragonfly|b<=0.05|t1,0.02,1.0,dragonfly\n"
           "P3,BTCUSDT,60,1,2,event,standard|b0.05-0.10|t2,0.08,1.2,standard\n"
           "P4,BTCUSDT,240,1,2,event,standard|b0.05-0.10|t2,0.09,1.2,standard\n"
           "P5,BTCUSDT,240,1,2,near_miss,t1,0.12,1.0,\n"
           "P6,BTCUSDT,240,1,2,control,t1,0.5,1.0,\n"
           "P7,BTCUSDT,240,1,2,control,t1,0.6,1.0,\n")
    ANN = ("id,detector_ok,defect_type,visual_strength,notes\n"
           "P1,y,,strong,\nP2,y,,ok,\nP3,n,not_doji,weak,looks like a spinning top\nP4,unsure,,weak,\n"
           "P5,n,wrong_subtype,weak,\nP6,y,,,\nP7,,,,\n")

    def tables(self):
        key = pp.read_key(self.KEY)
        return pp.defect_tables(key, pp.read_annotations(self.ANN, key))

    def test_rates_by_class_timeframe_and_stratum(self):
        t = self.tables()
        self.assertEqual((t["key_ids"], t["annotated"]), (7, 6))  # P7 is not annotated
        event = t["by_class"]["event"]
        self.assertEqual((event["annotated"], event["ok"], event["defect"], event["unsure"]), (4, 2, 1, 1))
        self.assertAlmostEqual(event["rate"], 1 / 3)  # unsure is excluded from the rate
        lo, hi = event["wilson95"]
        self.assertTrue(0 < lo < 1 / 3 < hi < 1)
        self.assertEqual(t["by_class"]["near_miss"]["rate"], 1.0)
        self.assertEqual(t["by_timeframe"]["60"]["defect"], 1)
        self.assertEqual(t["by_class_timeframe"]["event|240"]["unsure"], 1)
        self.assertEqual(t["by_stratum"]["event|60|dragonfly|b<=0.05|t1"]["defect"], 0)
        self.assertEqual(t["by_stratum"]["event|60|standard|b0.05-0.10|t2"]["defect"], 1)
        self.assertEqual(t["defect_types"], {"event": {"not_doji": 1}, "near_miss": {"wrong_subtype": 1}})

    def test_visual_strength_split(self):
        split = self.tables()["by_visual_strength"]
        self.assertEqual(split["event|strong"]["ok"], 1)
        self.assertEqual(split["event|weak"]["defect"], 1)
        self.assertEqual(split["event|weak"]["unsure"], 1)
        self.assertEqual(split["control|unrated"]["annotated"], 1)
        text = "\n".join(pp.defect_markdown(self.tables()))
        self.assertIn("No threshold is applied", text)
        self.assertIn("[Wilson95]", text)

    def test_invalid_annotations_are_rejected(self):
        key = pp.read_key(self.KEY)
        for bad in (self.ANN.replace("P3,n,", "P9,n,"), self.ANN.replace("P1,y,", "P1,maybe,"),
                    self.ANN.replace("not_doji", "spinning"), self.ANN.replace("weak,looks", "meh,looks"),
                    self.ANN + "P1,y,,,\n"):
            with self.assertRaises(pp.PilotError):
                pp.read_annotations(bad, key)
        with self.assertRaises(pp.PilotError):
            pp.read_key(self.KEY + "P1,BTCUSDT,60,1,2,event,x,0.01,1.0,\n")

    def test_script_prints_the_tables(self):
        path = Path(__file__).resolve().parents[2] / "scripts" / "research" / "pattern_pilot_defects.py"
        spec = importlib.util.spec_from_file_location("test_pilot_defects_script", path)
        module = importlib.util.module_from_spec(spec)
        previous = sys.path[:]
        try:
            spec.loader.exec_module(module)
        finally:
            sys.path[:] = previous
        import io
        import tempfile
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "key.csv").write_text(self.KEY, encoding="utf-8")
            (Path(tmp) / "ann.csv").write_text(self.ANN, encoding="utf-8")
            out = io.StringIO()
            with redirect_stdout(out):
                module.main(["--annotations", str(Path(tmp) / "ann.csv"), "--key", str(Path(tmp) / "key.csv")])
        self.assertIn("### Defect rate by class", out.getvalue())
        self.assertIn("| event | 4 | 2 | 1 | 1 |", out.getvalue())


if __name__ == "__main__":
    unittest.main()
