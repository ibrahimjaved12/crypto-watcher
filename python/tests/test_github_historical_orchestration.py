"""Focused snapshot presentation/path and pinned-source hygiene regressions."""

from contextlib import ExitStack, nullcontext
from datetime import date
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from market_analysis import historical_campaign_control as control
from market_analysis import historical_study_bundles as bundles


REPO = Path(__file__).resolve().parents[2]


def load_script(name):
    definition = importlib.util.spec_from_file_location(
        "snapshot_regression_" + name, REPO / "scripts/research" / (name + ".py"))
    module = importlib.util.module_from_spec(definition)
    previous_path = sys.path[:]
    try:
        definition.loader.exec_module(module)
    finally:
        sys.path[:] = previous_path
    return module


class SnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.study = load_script("github_study")

    def snapshot_fixture(self, operation, *, checkpoint_exists=False,
                         evidence_missing=False):
        study = self.study
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            base = Path(temporary)
            transfers = base / "transfers"
            transfers.mkdir()
            (transfers / "science-started").write_text("started\n")
            (transfers / "control-claim-reference.json").write_text("{}")
            (transfers / "input-manifest.json").write_text(json.dumps({
                "files": [{"facts": {"source": "core"}},
                          {"facts": {"source": "liquidation"}}]}))
            period = SimpleNamespace(study_period_index=0, utc_date=date(2024, 1, 1),
                                     phase="development")
            campaign_id = "snapshot-regression"
            root = base / "campaigns" / campaign_id
            (root / "operations").mkdir(parents=True)
            status_path = root / "operations/status.json"
            status_path.write_text(json.dumps({
                "scientific_state": "YIELDED" if operation == "preflight" else "FINALIZED_PERIOD",
                "preflight_passed": True}))
            stack.enter_context(patch.object(bundles, "BASE", base))
            checkpoint = bundles.period_root(campaign_id, period)
            if checkpoint_exists:
                checkpoint.mkdir(parents=True)
            stages = ["v1", *study.execution._candidate_stage_selectors(), "event-context", "outcomes"]
            required = {checkpoint / "prepared-replay.json", checkpoint / "study-points/manifest.json",
                        *(checkpoint / "post-replay" / (stage + ".json") for stage in stages)}
            output = root / "outputs" / study.execution.PERIOD_DIRECTORY / study.execution._period_filename(period)
            files = {output, *required}
            if evidence_missing:
                files.remove(checkpoint / "prepared-replay.json")
            task_id = "development-0-" + operation
            record = {"reserved_minutes": 310, "no_progress_runs": 0, "receipts": [],
                      "sequence": 1, "tasks": {task_id: "PENDING"}, "handoff": {
                          "key": "a" * 64, "run_id": "123", "attempt": "1", "parent": None,
                          "task": {"id": task_id, "operation": operation,
                                   "expected_outputs": ["verified-period"]}}}
            spec = {"campaign_id": campaign_id, "control": {},
                    "limits": {"max_uncompressed_bytes": 100000, "max_transfer_bytes": 100000}}
            packages = {"core": [("btc.zip", "BTCUSDT"), ("eth.zip", "ETHUSDT")],
                        "funding": [("btc-month.zip", "BTCUSDT")],
                        "liquidation": [("liquidation-day.zip", None)]}
            captured = {}

            def pack(rows, destination, **kwargs):
                captured.update(rows=rows, **kwargs)
                destination.mkdir(parents=True)
                (destination / "manifest.json").write_text("{}")
                return {"transfer_bytes": 2}

            patches = {
                "BASE": base,
                "settings": lambda: (spec, operation, "development", 0, 30, study.time.time()),
                "frozen_inputs": lambda _: (object(), {"source_identities": {}}),
                "prerequisites": lambda *args: None,
                "budget_parent": lambda *args: {"run_count": 0, "no_progress_runs": 0,
                                               "sampled_cumulative_runner_minutes": 0},
                "campaign_identity": lambda _: {"campaign_id": campaign_id},
                "verify_recovery_tree": lambda *args, **kwargs: (files, ["committed-unit"]),
                "checked_claim": lambda *args, **kwargs: record,
                "last_verified_unit": lambda *args: None,
                "disk_sample": lambda _: {},
                "pack_files": pack,
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(study, name, value))
            stack.enter_context(patch.object(study.events, "span", side_effect=lambda *args: nullcontext()))
            locks = stack.enter_context(patch.object(study, "owned_run_directory",
                                                     side_effect=lambda *args, **kwargs: nullcontext()))
            stack.enter_context(patch.object(study, "planned_packages", return_value=packages))
            stack.enter_context(patch.object(study.execution, "select_execution_periods", return_value=(period,)))
            stack.enter_context(patch.object(control, "accounting_reference", return_value={}))
            stack.enter_context(patch.dict(os.environ, {"GITHUB_OUTPUT": str(base / "output"),
                                                       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}))
            study.snapshot()
            self.assertEqual((base / "output").read_text(), "ready=true\n")
            self.assertEqual(packages["liquidation"], [("liquidation-day.zip", None)])
            self.assertEqual(captured["metadata"]["presentation"]["sources"], ["core", "liquidation"])
            self.assertEqual({row[1] for row in captured["rows"]}, files)
            self.assertEqual([call.args[0] for call in locks.call_args_list],
                             [root, checkpoint] if checkpoint_exists else [root])
            return captured["metadata"]

    def test_preflight_symbols_exclude_only_non_coin_specific_packages(self):
        metadata = self.snapshot_fixture("preflight")
        self.assertEqual(metadata["presentation"]["symbols"], ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(metadata["scientific_state"], "YIELDED")

    def test_execute_period_evidence_paths_with_and_without_checkpoint_directory(self):
        for checkpoint_exists in (False, True):
            with self.subTest(checkpoint_exists=checkpoint_exists):
                metadata = self.snapshot_fixture("execute-period", checkpoint_exists=checkpoint_exists)
                self.assertTrue(metadata["task_receipt"]["task_complete"])
                self.assertEqual(metadata["task_receipt"]["verified_outputs"], ["verified-period"])

    def test_execute_period_still_requires_all_evidence(self):
        metadata = self.snapshot_fixture("execute-period", checkpoint_exists=True, evidence_missing=True)
        self.assertFalse(metadata["task_receipt"]["task_complete"])
        self.assertEqual(metadata["task_receipt"]["verified_outputs"], [])


class PinnedSourceHygieneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.campaign = load_script("github_campaign")

    def test_bytecode_is_ignored_but_dirty_or_unexpected_source_is_rejected(self):
        campaign = self.campaign
        with TemporaryDirectory() as temporary, ExitStack() as stack:
            repo = Path(temporary)
            scripts = repo / "scripts/research"
            scripts.mkdir(parents=True)
            (scripts / ".gitignore").write_bytes((REPO / "scripts/research/.gitignore").read_bytes())
            tracked = scripts / "github_campaign.py"
            tracked.write_text("# tracked source\n")
            python = repo / "python"
            python.mkdir()
            requirements = python / "requirements.txt"
            requirements.write_text("# pinned requirements\n")

            def git(*args):
                return subprocess.run(["git", "-C", str(repo), *args], check=True,
                                      capture_output=True, text=True).stdout.strip()

            git("init", "-q")
            git("add", ".")
            git("-c", "core.hooksPath=/dev/null", "-c", "user.name=Regression Fixture",
                "-c", "user.email=fixture@example.invalid", "commit", "-qm", "Fixture")
            head = git("rev-parse", "HEAD")
            spec = {"runtime_sha": head, "orchestration_sha": head, "producer_revision": "b" * 40,
                    "dependency_locks": {"requirements.txt": hashlib.sha256(requirements.read_bytes()).hexdigest()}}
            transferred = repo / "campaign.json"
            transferred.write_text(json.dumps(spec))
            stack.enter_context(patch.object(campaign, "REPO", repo))
            stack.enter_context(patch.object(campaign, "Path", return_value=transferred))
            stack.enter_context(patch.object(campaign.control, "plan", return_value=[]))
            cache = scripts / "__pycache__"
            cache.mkdir()
            (cache / "github_campaign.cpython-310.pyc").write_bytes(b"generated bytecode")
            (scripts / "legacy.pyo").write_bytes(b"generated bytecode")
            self.assertEqual(campaign.pinned_spec(), spec)

            tracked.write_text("# changed tracked source\n")
            with self.assertRaisesRegex(ValueError, "exact pinned runtime"):
                campaign.pinned_spec()
            tracked.write_text("# tracked source\n")
            for unexpected in (scripts / "unexpected.py", cache / "unexpected.py"):
                with self.subTest(path=unexpected.relative_to(repo)):
                    unexpected.write_text("# unexpected source\n")
                    with self.assertRaisesRegex(ValueError, "exact pinned runtime"):
                        campaign.pinned_spec()
                    unexpected.unlink()
            self.assertEqual(campaign.pinned_spec(), spec)

            spec["runtime_sha"] = spec["orchestration_sha"] = "c" * 40
            transferred.write_text(json.dumps(spec))
            with self.assertRaisesRegex(ValueError, "exact pinned runtime"):
                campaign.pinned_spec()


if __name__ == "__main__":
    unittest.main()
