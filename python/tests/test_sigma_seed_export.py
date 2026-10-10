"""Guarded seed orchestration with synthetic plans; no downloads, git, or expensive sigma work."""
import importlib.util
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from market_analysis.benchmark.hidden_guard import Plan, HiddenAlreadyOpened, write_plan
from market_analysis.forward.sigma_state import empty_state


def load_exporter():
    scripts = Path(__file__).resolve().parents[2] / "scripts" / "research"
    spec = importlib.util.spec_from_file_location("sigma_seed_export_under_test", scripts / "export_sigma_seed.py")
    module = importlib.util.module_from_spec(spec)
    previous = sys.path[:]
    try:
        sys.path.insert(0, str(scripts))
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = previous
    return module


class ExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exporter = load_exporter()

    def plan(self, root, symbols, cutoff):
        plan = Plan(question_id="synthetic-sigma-seed", hypothesis="synthetic export permission",
                    strategies=[{"strategy_id": "forward-sigma-seed", "strategy_version": "forward-sigma-v1",
                                 "config": {"symbols": symbols, "cutoff": cutoff,
                                            "first_month": "2024-01", "data_revision": 1}}],
                    variants=1, statistic="synthetic", threshold="1", split_id="synthetic",
                    data_snapshot_id="synthetic", created_utc="2025-01-01T00:00:00Z")
        (root / "plans").mkdir()
        write_plan(root / "plans", plan)
        return plan

    def test_month_boundary_and_symbol_validation(self):
        for symbols, cutoff in (("BTCUSDT,BTCUSDT", "2025-05-01T00:00:00Z"),
                                ("UNKNOWN", "2025-05-01T00:00:00Z"),
                                ("BTCUSDT", "2025-05-02T00:00:00Z")):
            with self.assertRaises(ValueError):
                self.exporter.selection(symbols, cutoff)
        symbols, months, _ = self.exporter.selection("ETHUSDT,BTCUSDT", "2025-05-01T00:00:00Z")
        self.assertEqual(symbols, ["BTCUSDT", "ETHUSDT"])
        self.assertEqual((months[0], months[-1]), ("2024-01", "2025-04"))

    def test_hidden_history_refuses_missing_or_mismatched_plan_and_durable_open_failure(self):
        ex = self.exporter
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = Mock(path=root)
            with self.assertRaises(ex.ExportError):
                ex.authorize(checkout, ["2026-09"], ["BTCUSDT"], "2026-10-01T00:00:00Z", "",
                             now_utc="2026-10-11T00:00:00Z")
            plan = self.plan(root, ["BTCUSDT"], "2026-10-01T00:00:00Z")
            with self.assertRaises(ex.ExportError):
                ex.authorize(checkout, ["2026-09"], ["ETHUSDT"], "2026-10-01T00:00:00Z", plan.plan_id,
                             now_utc="2026-10-11T00:00:00Z")
            checkout.commit_and_push.side_effect = RuntimeError("synthetic push failure")
            with self.assertRaises(RuntimeError):
                ex.authorize(checkout, ["2026-09"], ["BTCUSDT"], "2026-10-01T00:00:00Z", plan.plan_id,
                             now_utc="2026-10-11T00:00:00Z")
            self.assertTrue((root / ex.xr.OPENS).exists())

    def test_opening_is_pushed_before_download_and_seed_is_private(self):
        ex = self.exporter
        with TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            cutoff = "2026-10-01T00:00:00Z"
            plan = self.plan(root, ["BTCUSDT"], cutoff)
            order = []
            checkout = Mock(path=root)
            checkout.commit_and_push.side_effect = lambda paths, message: order.append(("push", paths))
            state = empty_state("BTCUSDT", 1735689600000, source="lake_seed")
            args = SimpleNamespace(symbols="BTCUSDT", cutoff=cutoff, plan_id=plan.plan_id,
                                   workdir=Path(directory) / "work")
            with patch.object(ex.xr, "NOW_UTC", "2026-10-11T00:00:00Z"), \
                    patch.object(ex.xr, "_download_symbol_bars", side_effect=lambda *a: order.append(("download", []))), \
                    patch.object(ex, "load_symbol_bars", return_value=SimpleNamespace(minutes=10)) as loader, \
                    patch.object(ex, "seed_state", return_value=state):
                entries, digest = ex.export(args, checkout, Mock())
            self.assertEqual(order[0], ("push", [ex.xr.OPENS]))
            self.assertEqual(order[1][0], "download")
            self.assertEqual(order[2][0], "push")
            self.assertIsNotNone(loader.call_args.kwargs["token"])
            self.assertTrue(loader.call_args.kwargs["gate"].verify(loader.call_args.kwargs["token"]))
            record = json.loads((root / entries[0]["path"]).read_text())
            self.assertEqual(record["payload"]["source"], "lake_seed")
            self.assertTrue((root / f"seeds/sigma/manifests/{digest}.json").exists())
            with self.assertRaises(HiddenAlreadyOpened):
                ex.authorize(checkout, ["2026-09"], ["BTCUSDT"], cutoff, plan.plan_id,
                             now_utc="2026-10-11T01:00:00Z")

    def test_development_months_need_no_plan(self):
        checkout = Mock()
        self.assertEqual(self.exporter.authorize(checkout, ["2025-04"], ["BTCUSDT"],
                                                "2025-05-01T00:00:00Z", "", now_utc="2026-10-11T00:00:00Z"),
                         (None, None))
        checkout.commit_and_push.assert_not_called()
