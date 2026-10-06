"""Focused snapshot presentation/path and pinned-source hygiene regressions."""

from contextlib import ExitStack, nullcontext
import base64
from copy import deepcopy
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


class ContentsDecodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.campaign = load_script("github_campaign")

    def test_unwrapped_and_ascii_whitespace_wrapped_contents(self):
        raw = json.dumps({"synthetic": "Contents fixture", "items": list(range(30))}).encode()
        encoded = base64.b64encode(raw).decode("ascii")
        wrapped = [encoded,
                   "\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + "\n",
                   "\r\n".join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + "\r\n",
                   " \t" + encoded[:4] + "\v\f" + encoded[4:] + "\r\n"]
        for content in wrapped:
            with self.subTest(content=content):
                self.assertEqual(self.campaign.decode_contents({"encoding": "base64", "content": content}), raw)

    def test_malformed_base64_and_non_ascii_content_are_integrity_errors(self):
        for content in ("Zm?8=", "Zg==!", "Zg=", "Zg===", "Zg==\u00a0", "Zg==\u2003"):
            with self.subTest(content=content):
                with self.assertRaises(ValueError) as rejected:
                    self.campaign.decode_contents({"encoding": "base64", "content": content})
                self.assertEqual(self.campaign.safe_error(rejected.exception, "record-receipt")["category"], "INTEGRITY")

    def test_unexpected_file_response_shapes_are_rejected(self):
        responses = (None, [], {}, {"encoding": "utf-8", "content": "Zg=="},
                     {"encoding": "base64"}, {"encoding": "base64", "content": None},
                     {"encoding": "base64", "content": b"Zg=="},
                     {"encoding": "base64", "content": 123})
        for response in responses:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    self.campaign.decode_contents(response)

    def test_decoded_contents_still_use_bounded_json_reader(self):
        raw = b'{"duplicate":1,"duplicate":2}'
        decoded = self.campaign.decode_contents({"encoding": "base64", "content": base64.b64encode(raw).decode()})
        with self.assertRaisesRegex(ValueError, "duplicate JSON field"):
            self.campaign.read(decoded)
        with patch.object(self.campaign, "MAX", 1):
            with self.assertRaisesRegex(ValueError, "bounded control metadata"):
                self.campaign.read(decoded)


class WrappedAccountingReceiptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.campaign = load_script("github_campaign")

    def receipt_fixture(self, newline):
        campaign = self.campaign
        control = campaign.control
        spec = {
            "version": "historical-study-campaign-v1", "campaign_id": "synthetic-contents",
            "runtime_sha": "a" * 40, "orchestration_sha": "a" * 40, "producer_revision": "b" * 40,
            **{key: "c" * 64 for key in ("study_manifest_sha256", "coverage_manifest_sha256",
                                       "study_file_sha256", "coverage_file_sha256")},
            "dependency_locks": {"requirements.txt": "d" * 64, "requirements-research.txt": "e" * 64},
            "expected_membership": {"development": [0], "validation": [], "test": []},
            "inputs": {"0": {"release_tag": "synthetic-inputs", "manifest_sha256": "f" * 64}},
            "publication_reserve_minutes": 15, "max_new_stages": 1, "job_minutes": 20,
            "budget": {"ceiling_minutes": 120, "max_runs": 2, "no_progress_cap": 2},
            "control": {"version": control.PLAN, "allow_test": False, "tasks": [{
                "id": "preflight-development-0", "operation": "preflight", "phase": "development",
                "period_index": 0, "depends_on": [], "expected_outputs": ["preflight"]}]},
            "retention": {"version": "historical-retention-v1", "redundant_recovery": "verified-consolidation-only",
                          "abandoned_drafts": "retain-unless-proven", "inputs_and_results": "retain"},
        }
        record = control.initial(spec)
        control.control_action(record, spec, "start", "100")
        key = record["handoff"]["key"]
        bindings = {"STUDY_OPERATION": "preflight", "STUDY_PHASE": "development", "STUDY_JOB_MINUTES": "20",
                    "STUDY_PERIOD_INDEX": "0", "STUDY_ALLOW_TEST": "false",
                    "STUDY_RESUME_GENERATION": "", "STUDY_RESUME_SHA": ""}
        self.assertTrue(control.claim(record, spec, key, "123", "1", "9" * 64, bindings))
        locator = {"control_commit": "1" * 40, "control_blob_sha": "2" * 40}
        typed = {"receipt_contract": control.RECEIPT, "task_id": "preflight-development-0", "handoff_key": key,
                 "operation": "preflight", "run_id": "123", "attempt": "1", "task_complete": True,
                 "campaign_complete": True, "verified_outputs": ["preflight"], "setup_outcome": "success",
                 "scientific_state": "YIELDED", "termination_reason": "completed"}
        value = {"version": "historical-study-file-bundle-v2", "kind": "recovery", "identity": control.identity(spec),
                 "assets": [{"asset": "part-0001.tar", "size": 10, "sha256": "3" * 64}],
                 "metadata": {"generation": "recovery-synthetic-123-1", "parent": None,
                              "local_completed_work": ["4" * 64], "completed_work": ["4" * 64],
                              "measured_minutes": 1, "task_receipt": typed,
                              "lineage": {"reserved_minutes": record["reserved_minutes"],
                                          "run_count": record["run_count"], "no_progress_runs": 0},
                              "consolidation": {"version": control.COMPACT_LINEAGE,
                                                "accounting": control.accounting_reference(record, locator),
                                                "parent_receipt": None, "finalized_receipts": []}}}
        value["bundle_sha256"] = control.digest(value)
        receipt = control.receipt_reference(value, 1)
        encoded = base64.b64encode(control.canonical(record).encode()).decode("ascii")
        row = {"sha": locator["control_blob_sha"], "encoding": "base64",
               "content": newline.join(encoded[i:i + 60] for i in range(0, len(encoded), 60)) + newline}
        assets = [{"id": 10, "name": "manifest.json"},
                  {"id": 11, "name": "part-0001.tar", "state": "uploaded", "size": 10,
                   "digest": "sha256:" + "3" * 64}]
        requested = []

        def request(path):
            requested.append(path)
            if path.startswith("/releases/tags/"):
                return {"id": 1, "draft": False}
            if path.startswith("/releases/1/assets?"):
                return deepcopy(assets)
            if path == "/contents/campaigns/synthetic-contents/control.json?ref=" + locator["control_commit"]:
                return deepcopy(row)
            self.fail("Unexpected synthetic receipt request")

        store = SimpleNamespace(spec=spec, path="/contents/campaigns/synthetic-contents/control.json",
                                api=SimpleNamespace(request=request))
        return store, receipt, value, row, requested, locator

    def test_receipt_verification_accepts_line_wrapped_immutable_accounting(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=newline):
                store, receipt, value, row, requested, locator = self.receipt_fixture(newline)
                self.assertTrue(row["content"].endswith(newline))
                self.assertGreater(row["content"].count(newline), 1)
                with patch.object(self.campaign, "remote_manifest", return_value=value):
                    self.assertEqual(self.campaign.verify_receipt(store, receipt), value)
                self.assertIn(store.path + "?ref=" + locator["control_commit"], requested)

    def test_wrapped_accounting_still_rejects_wrong_immutable_blob(self):
        store, receipt, value, row, _, _ = self.receipt_fixture("\n")
        row["sha"] = "6" * 40
        with patch.object(self.campaign, "remote_manifest", return_value=value):
            with self.assertRaisesRegex(ValueError, "immutable accounting blob reference mismatch"):
                self.campaign.verify_receipt(store, receipt)


if __name__ == "__main__":
    unittest.main()
