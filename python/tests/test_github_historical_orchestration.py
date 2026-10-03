"""Synthetic recovery storage, capacity, diagnostics and Contents regressions."""

from contextlib import ExitStack, nullcontext
import base64
from copy import deepcopy
from datetime import date
from decimal import Decimal
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse

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
                         evidence_missing=False, capacity_error=None):
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

            def measure(paths, **kwargs):
                self.assertEqual(set(paths), files)
                measured_status = json.loads(status_path.read_text())
                self.assertEqual(measured_status['verified_committed_work_count'], 1)
                captured['frozen_status'] = status_path.read_bytes()
                return 2

            def capacity(*args, **kwargs):
                if capacity_error is not None:
                    raise capacity_error
                return {'measured_bytes': 2}

            def pack(rows, destination, **kwargs):
                self.assertEqual(status_path.read_bytes(), captured['frozen_status'])
                captured.update(rows=rows, **kwargs)
                destination.mkdir(parents=True)
                (destination / "manifest.json").write_text("{}")
                return {"uncompressed_bytes": 2, "transfer_bytes": 2}

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
                "footprint": measure,
                "publication_capacity": capacity,
            }
            for name, value in patches.items():
                stack.enter_context(patch.object(study, name, value))
            configured = stack.enter_context(patch.object(study.events, "configure"))
            emitted = stack.enter_context(patch.object(study.events, "emit"))
            stack.enter_context(patch.object(study.events, "span", side_effect=lambda *args: nullcontext()))
            locks = stack.enter_context(patch.object(study, "owned_run_directory",
                                                     side_effect=lambda *args, **kwargs: nullcontext()))
            stack.enter_context(patch.object(study, "planned_packages", return_value=packages))
            stack.enter_context(patch.object(study.execution, "select_execution_periods", return_value=(period,)))
            stack.enter_context(patch.object(control, "accounting_reference", return_value={}))
            stack.enter_context(patch.dict(os.environ, {"GITHUB_OUTPUT": str(base / "output"),
                                                       "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}))
            if capacity_error is not None:
                with self.assertRaises(bundles.BundleFootprintError) as rejected:
                    study.snapshot()
                self.assertIs(rejected.exception, capacity_error)
                self.assertNotIn('rows', captured)
                self.assertFalse((base / 'transfers/publication').exists())
                self.assertFalse((base / 'output').exists())
                return rejected.exception
            study.snapshot()
            sink = configured.call_args.args[0]
            self.assertFalse(sink.is_relative_to(root))
            self.assertNotIn(sink, files)
            self.assertEqual([call.args[0] for call in emitted.call_args_list],
                             ['RECOVERY_FOOTPRINT', 'PACKING_STARTED', 'PACKING_COMPLETED'])
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

    def test_snapshot_capacity_failure_prevents_packing_and_ready_output(self):
        error = bundles.BundleFootprintError('publication-disk-limit', measured_bytes=2, free_bytes=1, required_bytes=10)
        self.snapshot_fixture('preflight', capacity_error=error)


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


class CompressedStageRecordsTests(unittest.TestCase):
    def setUp(self):
        from market_analysis import historical_study_runtime as runtime
        from market_analysis import historical_stage_records as records
        self.runtime, self.records = runtime, records

    def scientific_bytes(self, value):
        from market_analysis.historical_market_state_study_json import iter_canonical_study_json
        return ''.join(iter_canonical_study_json(value)).encode('ascii')

    def identity_reference(self, root, items):
        operational = b''.join(self.runtime._canonical_bytes(self.runtime.encode(item)) for item in items)
        scientific = b''.join(self.scientific_bytes(item) + b'\n' for item in items)
        path, science = root / 'identity.jsonl', root / 'identity.scientific.jsonl'
        path.write_bytes(operational)
        science.write_bytes(scientific)
        return self.records.StageRecords(str(path), len(items), hashlib.sha256(operational).hexdigest(),
            str(science), hashlib.sha256(scientific).hexdigest())

    def test_identity_and_gzip_preserve_exact_logical_scientific_bytes(self):
        from decimal import Decimal
        import gzip
        for items in ([], [{'experiment_id': 'one', 'amount': Decimal('1.2300'), 'text': 'snowman \u2603'},
                          {'experiment_id': 'two', 'amount': Decimal('0.00001'), 'sequence': (2, 1)}]):
            with self.subTest(count=len(items)), TemporaryDirectory() as directory:
                root = Path(directory)
                identity = self.identity_reference(root, items)
                compressed = self.runtime._write_records(root / 'stage.records.jsonl', iter(items))
                self.assertEqual(list(identity), items)
                self.assertEqual(list(compressed), items)
                self.assertEqual(len(identity), len(compressed))
                self.assertEqual(identity.sha256, compressed.sha256)
                self.assertEqual(identity.scientific_sha256, compressed.scientific_sha256)
                self.assertEqual(self.scientific_bytes(identity), self.scientific_bytes(compressed))
                self.assertEqual(self.scientific_bytes(compressed), self.scientific_bytes(items))
                for plain, zipped, stored_sha in ((identity.path, compressed.path, compressed.stored_sha256),
                        (identity.scientific_path, compressed.scientific_path, compressed.scientific_stored_sha256)):
                    raw = Path(zipped).read_bytes()
                    self.assertEqual(gzip.decompress(raw), Path(plain).read_bytes())
                    self.assertEqual(hashlib.sha256(raw).hexdigest(), stored_sha)
                    self.assertEqual(raw[4:8], b'\0' * 4)  # fixed mtime
                    self.assertFalse(raw[3] & 8)  # no temporary filename header
                identity.verify()
                compressed.verify()
                self.assertTrue(compressed.path.endswith('.records.jsonl.gz'))
                self.assertTrue(compressed.scientific_path.endswith('.records.scientific.jsonl.gz'))

    def test_indexed_outcomes_joining_and_filtering(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            items = ({'experiment_id': 'one', 'n': 1}, {'experiment_id': 'two', 'n': 2})
            refs = self.runtime._publish_stage(root / 'outcomes.json',
                {'input_descriptor': {'action': 'outcomes'}}, ([items[0]], [], [items[1]]))
            for index, ref in enumerate(refs):
                self.assertTrue(ref.path.endswith(f'.{index}.records.jsonl.gz'))
                self.assertTrue(ref.scientific_path.endswith(f'.{index}.records.scientific.jsonl.gz'))
                ref.verify()
            joined = self.records.JoinedStageRecords(refs)
            self.assertEqual(list(joined), list(items))
            self.assertEqual(len(joined), 2)
            self.assertEqual(self.scientific_bytes(joined), self.scientific_bytes(list(items)))
            both = self.runtime._write_records(root / 'both.records.jsonl', items)
            for matching, expected in ((True, [items[0]]), (False, [items[1]])):
                filtered = self.records.FilteredStageRecords(both, 'one', matching)
                self.assertEqual(list(filtered), expected)
                self.assertEqual(self.scientific_bytes(filtered), self.scientific_bytes(expected))

    def test_exact_legacy_and_new_field_shapes(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            ref = self.identity_reference(Path(directory), [])
            full = self.runtime.encode(ref)
            self.assertEqual(self.runtime.decode(full), ref)
            legacy = deepcopy(full)
            for field in ('storage_encoding', 'stored_sha256', 'scientific_stored_sha256'):
                del legacy['fields'][field]
            self.assertEqual(self.runtime.decode(legacy), ref)
            for payload in (full, legacy):
                unexpected = deepcopy(payload)
                unexpected['fields']['unknown'] = None
                with self.assertRaises(ValueError):
                    self.runtime.decode(unexpected)
            for field in ('storage_encoding', 'stored_sha256', 'scientific_stored_sha256'):
                mixed = deepcopy(legacy)
                mixed['fields'][field] = full['fields'][field]
                with self.assertRaises(ValueError):
                    self.runtime.decode(mixed)
            for encoding in ('brotli', None, 1):
                with self.assertRaises(ValueError):
                    replace(ref, storage_encoding=encoding)
            with self.assertRaises(ValueError):
                replace(ref, storage_encoding='gzip')
            with self.assertRaises(ValueError):
                replace(ref, scientific_path=None, scientific_sha256=None, scientific_stored_sha256='a' * 64)
            compressed = self.runtime._write_records(Path(directory) / 'new.records.jsonl', [])
            self.assertEqual(self.runtime.decode(self.runtime.encode(compressed)), compressed)
            for field in ('stored_sha256', 'scientific_stored_sha256'):
                with self.assertRaises(ValueError):
                    replace(compressed, **{field: None})
                with self.assertRaises(ValueError):
                    replace(compressed, **{field: 'not-a-hash'})
            other = self.runtime.encode(self.records.StageValue('value', 'a' * 64, 'b' * 64))
            del other['fields']['block_sha256']
            with self.assertRaises(ValueError):
                self.runtime.decode(other)

    def test_logical_count_stored_hash_and_gzip_corruption_rejected(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            ref = self.runtime._write_records(Path(directory) / 'stage.records.jsonl', [{'experiment_id': 'one'}])
            for bad in (replace(ref, count=2), replace(ref, sha256='0' * 64),
                        replace(ref, scientific_sha256='0' * 64), replace(ref, stored_sha256='0' * 64),
                        replace(ref, scientific_stored_sha256='0' * 64)):
                with self.assertRaises(ValueError):
                    bad.verify()
            for field, sha_field in (('path', 'stored_sha256'), ('scientific_path', 'scientific_stored_sha256')):
                path = Path(getattr(ref, field))
                original = path.read_bytes()
                for corrupt in (original[:-1], original[:-8], b'not gzip',
                                original[:-8] + bytes([original[-8] ^ 1]) + original[-7:]):
                    path.write_bytes(corrupt)
                    # A matching stored hash cannot disguise corrupt compression.
                    bad = replace(ref, **{sha_field: hashlib.sha256(corrupt).hexdigest()})
                    with self.assertRaises(ValueError):
                        bad.verify()
                path.write_bytes(original)
                altered = bytearray(original)
                altered[9] ^= 1  # valid gzip OS metadata tamper, same logical payload
                path.write_bytes(altered)
                with self.assertRaisesRegex(ValueError, 'stored SHA'):
                    ref.verify()
                path.write_bytes(original)

    def test_noncanonical_and_non_ascii_plain_records_remain_rejected(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            ref = self.identity_reference(Path(directory), [1])
            for raw in (b'1', b'1 \n'):
                Path(ref.path).write_bytes(raw)
                bad = replace(ref, sha256=hashlib.sha256(raw).hexdigest())
                with self.assertRaises(ValueError):
                    list(bad)
            Path(ref.path).write_bytes(b'1\n')
            Path(ref.scientific_path).write_bytes(b'"\xff"\n')
            with self.assertRaises(ValueError):
                ref.verify()

    def test_deterministic_repeats_conflicts_and_failure_cleanup(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            items = [{'experiment_id': 'one', 'n': 1}]
            ref = self.runtime._write_records(root / 'stage.records.jsonl', items)
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            self.assertEqual(self.runtime._write_records(root / 'stage.records.jsonl', items), ref)
            second = self.runtime._write_records(root / 'second.records.jsonl', items)
            self.assertEqual(ref.stored_sha256, second.stored_sha256)
            self.assertEqual(ref.scientific_stored_sha256, second.scientific_stored_sha256)
            with self.assertRaisesRegex(ValueError, 'conflicting'):
                self.runtime._write_records(root / 'stage.records.jsonl', [{'experiment_id': 'different'}])
            for name, raw in before.items():
                self.assertEqual((root / name).read_bytes(), raw)
            def interrupted():
                yield items[0]
                raise TimeoutError('synthetic deadline')
            with self.assertRaises(TimeoutError):
                self.runtime._write_records(root / 'failed.records.jsonl', interrupted())
            self.assertFalse(list(root.glob('failed*')))
            self.assertFalse(list(root.glob('.*.records-*')))
            with patch.object(self.records, '_read_json_bytes', side_effect=TimeoutError('deadline')):
                with self.assertRaises(TimeoutError):
                    ref.verify()

    def test_raw_v2_and_legacy_v1_round_trip_both_files_and_rebase(self):
        with TemporaryDirectory() as directory:
            base = Path(directory) / 'source'
            stage = base / 'campaigns/synthetic/checkpoints/period-synthetic/post-replay/stage.json'
            stage.parent.mkdir(parents=True)
            identity = {'input_descriptor': {'action': 'event-context'}}
            ref = self.runtime._publish_stage(stage, identity, [{'experiment_id': 'one'}])
            referenced = {Path(value) for value in bundles._walk_strings(json.loads(stage.read_text())['result'])
                          if value.startswith(str(base) + '/')}
            self.assertEqual(referenced, {Path(ref.path), Path(ref.scientific_path)})
            sources = [stage, *sorted(referenced)]
            rows = [(str(path.relative_to(base)), path, {}) for path in sources]
            parts = Path(directory) / 'v2-parts'
            with patch.object(bundles, 'PART_LIMIT', 64):
                value = bundles.pack_files(rows, parts, kind='recovery', identity={})
                self.assertEqual(value['version'], bundles.VERSION)
                self.assertEqual(value['transfer_bytes'], sum(path.stat().st_size for path in sources))
                self.assertEqual(len(value['files']), 3)
                versions = [(value, parts)]
                legacy_parts = Path(directory) / 'v1-parts'
                legacy_parts.mkdir()
                legacy_files = []
                for index, (name, path, facts) in enumerate(rows):
                    raw = path.read_bytes()
                    chunks = []
                    for offset in range(0, len(raw), 64):
                        asset, chunk = f'legacy-{index}-{offset}', raw[offset:offset + 64]
                        (legacy_parts / asset).write_bytes(chunk)
                        chunks.append({'asset': asset, 'size': len(chunk), 'offset': offset,
                                       'sha256': hashlib.sha256(chunk).hexdigest()})
                    legacy_files.append({'path': name, 'facts': facts, 'absent': False, 'size': len(raw),
                                         'sha256': hashlib.sha256(raw).hexdigest(), 'parts': chunks})
                total = sum(path.stat().st_size for path in sources)
                legacy = bundles.sealed({'version': bundles.V1, 'kind': 'recovery', 'identity': {},
                    'metadata': {}, 'files': legacy_files, 'uncompressed_bytes': total, 'transfer_bytes': total})
                versions.append((legacy, legacy_parts))
                for version, assets in versions:
                    staging = Path(directory) / version['version']
                    bundles.unpack_files(version, assets, staging, max_bytes=100000, headroom_bytes=0)
                    mapped = []
                    def rebase(path):
                        result = staging / Path(path).relative_to(base)
                        mapped.append(result)
                        return result
                    restored, _ = self.runtime._stage_metadata(staging / stage.relative_to(base),
                                                              identity, reference_path=rebase)
                    self.assertEqual({path.relative_to(staging) for path in mapped},
                                     {Path(ref.path).relative_to(base), Path(ref.scientific_path).relative_to(base)})
                    self.assertEqual(restored.sha256, ref.sha256)
                    for path in sources:
                        self.assertEqual((staging / path.relative_to(base)).read_bytes(), path.read_bytes())


class RecoveryCapacityTests(unittest.TestCase):
    def test_footprint_deduplicates_checks_deadlines_and_rejects_links(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'source'
            path.write_bytes(b'synthetic')
            self.assertEqual(bundles.footprint([path, path]), 9)
            with self.assertRaises(TimeoutError):
                bundles.footprint([path], check=lambda: (_ for _ in ()).throw(TimeoutError()))
            link = path.with_name('symlink')
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                bundles.footprint([link])
            hardlink = path.with_name('hardlink')
            os.link(path, hardlink)
            with self.assertRaises(ValueError):
                bundles.footprint([path])
            with self.assertRaises(ValueError):
                bundles.footprint([Path(directory)])

    def test_all_capacity_failures_precede_directory_and_part_creation(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.write_bytes(b'x')
            size = 5 * 1024**3  # no multi-GiB allocation
            for reason, assembled, transfer, free in (
                    ('assembled-inventory-limit', size - 1, size, size * 2),
                    ('transfer-limit', size, size - 1, size * 2),
                    ('publication-disk-limit', size, size, size + bundles.PUBLICATION_OVERHEAD - 1)):
                with self.subTest(reason=reason), patch.object(bundles, 'regular', return_value=SimpleNamespace(st_size=size)), \
                     patch.object(bundles.shutil, 'disk_usage', return_value=SimpleNamespace(free=free)):
                    destination = Path(directory) / reason
                    with self.assertRaises(bundles.BundleFootprintError) as caught:
                        bundles.pack_files([('source', source, {})], destination, kind='recovery', identity={},
                                           max_bytes=assembled, max_transfer_bytes=transfer)
                    self.assertEqual(caught.exception.measurements['reason'], reason)
                    self.assertEqual(caught.exception.measurements['measured_bytes'], size)
                    self.assertFalse(destination.exists())

    def test_source_growth_after_preflight_remains_integrity(self):
        with TemporaryDirectory() as directory:
            source = Path(directory) / 'source'
            source.write_bytes(b'x')
            original = bundles.regular
            calls = 0
            def changed(path):
                nonlocal calls
                calls += 1
                if calls == 3:
                    source.write_bytes(b'grew')
                return original(path)
            with patch.object(bundles, 'regular', side_effect=changed):
                with self.assertRaisesRegex(ValueError, 'source changed') as caught:
                    bundles.pack_files([('source', source, {})], Path(directory) / 'parts', kind='recovery', identity={})
            self.assertNotIsInstance(caught.exception, bundles.BundleFootprintError)
            self.assertFalse(list((Path(directory) / 'parts').glob('pack-*')))

    def test_durable_boundary_capacity_failure_is_not_a_successful_yield(self):
        from market_analysis.historical_study_batch import SliceController
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'checkpoints').mkdir()
            (root / 'checkpoints/durable.jsonl.gz').write_bytes(b'durable')
            controller = SliceController(1, 60, root / 'operations/status.json', {}, root, 0,
                capacity_limits={'max_uncompressed_bytes': 1, 'max_transfer_bytes': 100})
            with patch('market_analysis.historical_operational_events.emit'), self.assertRaises(bundles.BundleFootprintError):
                controller.observe('POST_REPLAY_STAGE_COMPLETED', {'stage_id': 'atr-0', 'stage_result_sha256': 'a' * 64})
            self.assertEqual(controller.status['scientific_state'], 'FAILED')
            self.assertEqual(controller.last['stage_id'], 'atr-0')
            self.assertIsNone(controller.status['yield_reason'])
            with patch.object(controller, 'check_capacity') as capacity, patch.object(controller, 'save'), \
                 patch('market_analysis.historical_operational_events.emit'):
                controller.observe('REPLAY_CURRENT_PROGRESS', {})
                capacity.assert_not_called()

    def test_capacity_includes_committed_report_sidecars(self):
        from market_analysis.historical_study_batch import SliceController
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sidecars = root / 'outputs/.period-manifests'
            sidecars.mkdir(parents=True)
            (sidecars / '000-2024-01-01.json').write_bytes(b'committed')
            (sidecars / '.uncommitted.tmp').write_bytes(b'ignored')
            controller = SliceController(64, 60, root / 'operations/status.json', {}, root, 0,
                capacity_limits={'max_uncompressed_bytes': 8, 'max_transfer_bytes': 100})
            with self.assertRaises(bundles.BundleFootprintError) as caught:
                controller.check_capacity()
            self.assertEqual(caught.exception.measurements['measured_bytes'], 9)



class ResourceDiagnosticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.campaign = load_script('github_campaign')
        cls.study = load_script('github_study')

    def test_acquisition_capacity_uses_typed_resource_contract(self):
        spec = {'limits': {'max_uncompressed_bytes': 100, 'max_transfer_bytes': 80,
                           'headroom_bytes': 20}}
        for assembled, transfer, free, reason, measured, limit in (
                (101, 80, 1000, 'assembled-inventory-limit', 101, 100),
                (100, 81, 1000, 'transfer-limit', 81, 80),
                (100, 80, 199, 'staging-disk-limit', None, None)):
            with self.subTest(reason=reason), patch.object(self.study, 'shutil_free', return_value=free), \
                 self.assertRaises(bundles.BundleFootprintError) as caught:
                self.study.limits(spec, {'uncompressed_bytes': assembled, 'transfer_bytes': transfer})
            diagnostic = self.campaign.safe_error(caught.exception, 'inputs')
            self.assertEqual(diagnostic['category'], 'RESOURCE_LIMIT')
            self.assertEqual(diagnostic['reason'], reason)
            if measured is not None:
                self.assertEqual(diagnostic['measured_bytes'], measured)
                self.assertEqual(diagnostic['limit_bytes'], limit)
            else:
                self.assertEqual(diagnostic['free_bytes'], 199)
                self.assertEqual(diagnostic['required_bytes'], 200)
        with patch.object(self.study, 'shutil_free', return_value=200):
            self.study.limits(spec, {'uncompressed_bytes': 100, 'transfer_bytes': 80})

    def test_typed_resource_diagnostics_and_strict_measurements(self):
        error = bundles.BundleFootprintError('publication-disk-limit', measured_bytes=100, free_bytes=80, required_bytes=200)
        diagnostic = self.campaign.safe_error(error, 'snapshot')
        self.assertEqual(diagnostic['category'], 'RESOURCE_LIMIT')
        self.assertEqual(self.campaign.safe_diagnostic(diagnostic), diagnostic)
        self.assertEqual(diagnostic['reason'], 'publication-disk-limit')
        self.assertEqual(control.failure_state('snapshot', diagnostic['category']), 'PUBLICATION_FAILED')
        self.assertEqual(self.campaign.safe_error(ValueError('corrupt SHA'), 'snapshot')['category'], 'INTEGRITY')
        self.assertEqual(control.failure_state('snapshot', 'INTEGRITY'), 'INTEGRITY_FAILED')
        for invalid in (-1, True, 1.5, float('inf'), 2**63, '100'):
            sanitized = self.campaign.safe_diagnostic({**diagnostic, 'measured_bytes': invalid, 'path': '/private'})
            self.assertNotIn('measured_bytes', sanitized)
            self.assertNotIn('path', sanitized)
        for reason in ('private exception text', ['publication-disk-limit'], None):
            sanitized = self.campaign.safe_diagnostic({**diagnostic, 'reason': reason})
            self.assertNotIn('reason', sanitized)
            self.assertNotIn('free_bytes', sanitized)
        forged = ValueError('corruption')
        forged.diagnostic_category = 'RESOURCE_LIMIT'
        forged.measurements = {'reason': 'private exception text', 'measured_bytes': 1}
        self.assertEqual(self.campaign.safe_error(forged, 'snapshot')['category'], 'INTEGRITY')

    def test_retention_finalizer_and_private_diagnostic_preserve_resource_accounting(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            diagnostic = self.campaign.safe_error(bundles.BundleFootprintError('transfer-limit',
                measured_bytes=101, limit_bytes=100), 'snapshot')
            with patch.object(self.study, 'BASE', base), patch.dict(sys.modules, {'github_campaign': self.campaign}):
                self.study.retain_outcome({**diagnostic, 'private_path': '/private', 'raw_exception': 'secret'})
            retained = json.loads((base / 'transfers/operation-outcomes.json').read_text())
            self.assertEqual(retained, [diagnostic])
            record = {'state': 'RUNNING', 'reserved_minutes': 615, 'run_count': 2, 'receipts': ['earlier-receipt'],
                      'ledger': [], 'handoff': {'key': 'a' * 64, 'mutation_id': 'b' * 64,
                                               'state': 'CLAIMED', 'run_id': '123', 'attempt': '1'}}
            def update(action):
                action(record)
                return record, 'sha'
            store = SimpleNamespace(get=lambda: (record, 'sha'), update=update, view=lambda _: None, spec={})
            def local_path(path):
                return base / path.removeprefix('/tmp/crypto-study/')
            env = {'STUDY_HANDOFF_KEY': 'a' * 64, 'STUDY_CLAIM_MUTATION_ID': 'b' * 64,
                   'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '1',
                   'STUDY_ENTRY_OUTCOME': 'success', 'STUDY_SETUP_OUTCOME': 'success',
                   'STUDY_PARENTS_OUTCOME': 'success', 'STUDY_SCIENTIFIC_OUTCOME': 'success',
                   'STUDY_SNAPSHOT_OUTCOME': 'failure', 'STUDY_PUBLICATION_OUTCOME': 'skipped'}
            with patch.object(self.campaign, 'Path', side_effect=local_path), \
                 patch.object(self.campaign, 'diagnostic') as private, patch.dict(os.environ, env):
                self.campaign.finalizer(store)
            self.assertEqual(record['state'], 'PUBLICATION_FAILED')
            self.assertEqual(record['reserved_minutes'], 615)
            self.assertEqual(record['run_count'], 2)
            self.assertEqual(record['receipts'], ['earlier-receipt'])
            self.assertEqual(record['handoff']['outcome']['resource_failure'],
                             {'reason': 'transfer-limit', 'measured_bytes': 101, 'limit_bytes': 100})
            private.assert_called_once_with(store.spec, diagnostic)

    def test_resource_events_only_accept_bounded_integer_measurements(self):
        from market_analysis import historical_operational_events as events
        row = events.allowlisted({'category': 'RECOVERY_FOOTPRINT', 'measured_bytes': 100,
                                  'required_bytes': True, 'limit_bytes': -1, 'free_bytes': float('inf'), 'transfer_bytes': 1.5,
                                  'assembled_limit_bytes': 200, 'private_path': '/private'})
        self.assertEqual(row['measured_bytes'], 100)
        self.assertEqual(row['assembled_limit_bytes'], 200)
        for field in ('required_bytes', 'limit_bytes', 'free_bytes', 'transfer_bytes', 'private_path'):
            self.assertNotIn(field, row)


class UploadReconcileTests(unittest.TestCase):
    PREFIX = "https://api.github.com/repos/owner/private"
    UPLOAD_URL = "https://uploads.github.com/repos/owner/private/releases/1/assets{?name,label}"

    @classmethod
    def setUpClass(cls):
        cls.study = load_script("github_study")
        cls.campaign = load_script("github_campaign")

    def setUp(self):
        # github_study imports TransportError lazily from github_campaign.
        modules = patch.dict(sys.modules, {"github_campaign": self.campaign})
        modules.start()
        self.addCleanup(modules.stop)

    def fake_github(self, uploads, patch_outcome="ok"):
        """Synthetic release API. Upload outcomes: ok, stored-ambiguous (stored,
        then the connection fails), partial-ambiguous, ambiguous (nothing stored)."""
        campaign = self.campaign
        github = object.__new__(self.study.GitHub)
        github.prefix, github.branch = self.PREFIX, "main"
        github.deadline = SimpleNamespace(check=lambda: None, remaining=lambda: 10_000)
        state = SimpleNamespace(assets={}, next_id=100, calls=[], draft=True, uploads=list(uploads))

        def reply(value):
            return io.BytesIO(json.dumps(value).encode())

        def response(url, *, accept="application/vnd.github+json", method="GET", body=None,
                     content_type=None, size=None, timeout=30):
            path = url[len(self.PREFIX):] if url.startswith(self.PREFIX) else url
            state.calls.append((method, path.split("?")[0], timeout))
            if method == "POST" and url.startswith("https://uploads.github.com/"):
                name = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["name"][0]
                outcome = state.uploads.pop(0)
                if outcome == "ambiguous":
                    raise campaign.TransportError("AMBIGUOUS_WRITE", method)
                if name in state.assets:
                    raise campaign.TransportError("HTTP", method, 422)
                raw = Path(body).read_bytes()
                stored = ({"state": "starter", "size": len(raw) // 2} if outcome == "partial-ambiguous" else
                          {"state": "uploaded", "size": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()})
                state.next_id += 1
                state.assets[name] = {"id": state.next_id, "name": name, **stored}
                if outcome != "ok":
                    raise campaign.TransportError("AMBIGUOUS_WRITE", method)
                return reply(state.assets[name])
            if method == "POST" and path == "/releases":
                return reply({"id": 1, "draft": True, "upload_url": self.UPLOAD_URL})
            if method == "GET" and path.startswith("/releases/1/assets?"):
                return reply(list(state.assets.values()))
            if path.startswith("/releases/assets/"):
                asset_id = int(path.rsplit("/", 1)[1])
                name = next(key for key, asset in state.assets.items() if asset["id"] == asset_id)
                if method == "DELETE":
                    del state.assets[name]
                    return io.BytesIO(b"")
                return reply(state.assets[name])
            if path == "/releases/1":
                if method == "PATCH":
                    if patch_outcome != "unapplied-ambiguous":
                        state.draft = False
                    if patch_outcome != "ok":
                        raise campaign.TransportError("AMBIGUOUS_WRITE", method)
                return reply({"id": 1, "draft": state.draft})
            self.fail("unexpected synthetic request " + method + " " + path)

        github.response = response
        return github, state

    def part(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "part-0001.tar"
        path.write_bytes(b"synthetic release part bytes")
        return path

    def calls(self, state, method):
        return [call for call in state.calls if call[0] == method]

    def test_ambiguous_upload_with_correctly_stored_asset_succeeds(self):
        github, state = self.fake_github(["stored-ambiguous"])
        github.upload_verified({"id": 1, "upload_url": self.UPLOAD_URL}, self.part())
        self.assertEqual(len(self.calls(state, "POST")), 1)
        self.assertEqual(self.calls(state, "POST")[0][2], 600)
        self.assertFalse(self.calls(state, "DELETE"))
        self.assertEqual(state.assets["part-0001.tar"]["state"], "uploaded")

    def test_ambiguous_upload_with_partial_asset_deletes_then_reuploads(self):
        github, state = self.fake_github(["partial-ambiguous", "ok"])
        path = self.part()
        github.upload_verified({"id": 1, "upload_url": self.UPLOAD_URL}, path)
        self.assertEqual(len(self.calls(state, "POST")), 2)
        self.assertEqual(len(self.calls(state, "DELETE")), 1)
        asset = state.assets["part-0001.tar"]
        self.assertEqual((asset["state"], asset["size"]), ("uploaded", path.stat().st_size))
        self.assertEqual(asset["digest"], "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest())

    def test_ambiguous_upload_with_absent_asset_retries_once(self):
        github, state = self.fake_github(["ambiguous", "ok"])
        github.upload_verified({"id": 1, "upload_url": self.UPLOAD_URL}, self.part())
        self.assertEqual([call[2] for call in self.calls(state, "POST")], [600, 600])
        self.assertFalse(self.calls(state, "DELETE"))
        self.assertIn("part-0001.tar", state.assets)

    def test_second_ambiguous_upload_failure_raises(self):
        github, state = self.fake_github(["ambiguous", "ambiguous"])
        with self.assertRaises(self.campaign.TransportError) as raised:
            github.upload_verified({"id": 1, "upload_url": self.UPLOAD_URL}, self.part())
        self.assertEqual(raised.exception.code, "AMBIGUOUS_WRITE")
        self.assertEqual(len(self.calls(state, "POST")), 2)

    def publish(self, patch_outcome):
        github, state = self.fake_github(["ok"], patch_outcome)
        directory = self.part().parent / "generation"
        directory.mkdir()
        (directory / "manifest.json").write_text("{}")
        presentation = patch.object(self.study, "release_presentation", return_value=("title", "body"))
        presentation.start()
        self.addCleanup(presentation.stop)
        return github, state, lambda: github.publish("recovery-synthetic-1-1", directory)

    def test_ambiguous_publish_patch_with_published_release_succeeds(self):
        github, state, publish = self.publish("applied-ambiguous")
        self.assertIs(publish()["draft"], False)
        self.assertEqual(state.calls[-1], ("GET", "/releases/1", 30))

    def test_ambiguous_publish_patch_with_draft_release_raises(self):
        github, state, publish = self.publish("unapplied-ambiguous")
        with self.assertRaises(self.campaign.TransportError) as raised:
            publish()
        self.assertEqual(raised.exception.code, "AMBIGUOUS_WRITE")
        self.assertTrue(state.draft)


class PeriodReportVerificationTests(unittest.TestCase):
    REVISION = "a" * 40

    def setUp(self):
        from market_analysis import historical_market_state_study_execution as execution
        self.execution = execution
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "outputs" / execution.PERIOD_DIRECTORY / "period.json"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(json.dumps({"report_sha256": "d" * 64}).encode())
        self.sidecar = self.root / "outputs" / ".period-manifests" / "period.json"
        self.manifest = SimpleNamespace(manifest_sha256="b" * 64)
        self.coverage = {"coverage_manifest_sha256": "c" * 64}
        self.period = {"study_period_index": 0, "utc_date": "2024-01-01", "phase": "development"}
        # Stands in for the complete decode/validation of a ~2.5 GB report.
        full = patch.object(execution, "load_finalized_period_report",
                            side_effect=lambda path, *args, **kwargs: json.loads(Path(path).read_bytes()))
        self.full = full.start()
        self.addCleanup(full.stop)

    def sha(self, full):
        return self.execution.period_report_sha_for_verification(
            self.path, self.manifest, self.coverage, self.period, self.REVISION, full=full)

    def test_fast_path_matches_full_validation_without_decoding(self):
        expected = self.sha(True)
        self.assertEqual(expected, "d" * 64)
        self.assertTrue(self.sidecar.exists())
        self.assertEqual(self.full.call_count, 1)
        self.assertEqual(self.sha(False), expected)
        self.assertEqual(self.full.call_count, 1)

    def test_fast_path_without_sidecar_takes_full_path(self):
        self.assertEqual(self.sha(False), "d" * 64)
        self.assertEqual(self.full.call_count, 1)

    def test_fast_path_rejects_one_byte_report_change(self):
        self.sha(True)
        raw = bytearray(self.path.read_bytes())
        raw[-3] ^= 1
        self.path.write_bytes(raw)
        with self.assertRaises((ValueError, self.execution.StudyArtifactConflictError)):
            self.sha(False)

    def test_fast_path_rejects_altered_sidecar(self):
        self.sha(True)
        value = json.loads(self.sidecar.read_bytes())
        value["report_sha256"] = "e" * 64
        self.sidecar.write_bytes(json.dumps(value).encode())
        with self.assertRaises((ValueError, self.execution.StudyArtifactConflictError)):
            self.sha(False)

    def test_recovery_tree_defaults_to_full_report_validation(self):
        execution = self.execution
        self.sidecar.parent.mkdir(parents=True)
        self.sidecar.write_text("{}")
        campaign_root = self.root / "base" / "campaigns" / "synthetic-campaign"
        campaign_root.mkdir(parents=True)
        (self.root / "outputs").rename(campaign_root / "outputs")
        period = SimpleNamespace(study_period_index=0, phase="development")
        manifest = SimpleNamespace(selected_periods=[period], manifest_sha256="b" * 64)
        identity = {"expected_membership": {"development": [0]}, "producer_revision": self.REVISION}
        verify = Mock(return_value="f" * 64)
        with patch.object(execution, "_period_filename", return_value="period.json"), \
             patch.object(execution, "period_report_sha_for_verification", verify):
            for keywords, full in (({}, True), ({"full_report_validation": False}, False)):
                with self.subTest(full=full):
                    _, work = bundles.verify_recovery_tree(self.root / "base", "synthetic-campaign", manifest,
                                                           self.coverage, identity, **keywords)
                    self.assertEqual(work, ["f" * 64])
                    self.assertIs(verify.call_args.kwargs["full"], full)


def _reference_extract_candidate_inputs(records):
    """The pre-optimization extract_candidate_inputs, kept as a differential oracle."""
    from market_analysis.historical_stage_records import CandidateDecision
    compact, bocpd = [], []
    for item in records:
        if item.evidence_kind in ("EVENT", "RETROSPECTIVE"):
            compact.append(CandidateDecision(item.experiment_id, item.algorithm_version,
                item.config_version, item.decision_time_ms, item.evidence_kind))
        if item.experiment_id == "EXP-75-04B" and item.evidence_kind == "EVENT":
            bocpd.append(item)
    from market_analysis.historical_market_state_study_execution import _bocpd_onset_evidence
    return tuple(compact), _bocpd_onset_evidence(bocpd)


class StageVerificationTests(unittest.TestCase):
    def setUp(self):
        from market_analysis import historical_study_runtime as runtime
        from market_analysis import historical_stage_records as records
        self.runtime, self.records = runtime, records

    def test_fast_canonical_record_encoder_matches_reference(self):
        from market_analysis.historical_replay_runtime import _canonical_bytes, _read_json_bytes
        texts = ["0", "-0", "0.0", "-0.0", "1.5", "-2.25e-10", "1e300", "5e-324", "1e-7", "0.1",
                 "1.7976931348623157e308", "100000000000000000000.0", "123456789012345678901234567890",
                 "-" + "9" * 80, "true", "false", "null", '""', "[]", "{}",
                 '"snowman \\u2603 face \\ud83d\\ude00 nul \\u0000 tab \\t quote \\" slash \\/ e \\u00e9"',
                 '"lone \\ud800 surrogate"', '"\xc3\xa9 raw utf-8"',
                 '[1, 2.0, "x", [null, {"b": 1, "a": -0.0}], [], {}]',
                 '{"z": {"y": [3, {"b": "\\u00e9", "a": 1e-7}]}, "a": 1e22, "m": 0.1, "": -1}',
                 '{"experiment_id": "EXP-75-04B", "native_evidence": {"decimal": "1.2300"}, "n": -5000}']
        for text in texts:
            with self.subTest(text=text):
                value = _read_json_bytes(text.encode("utf-8"))
                self.assertEqual(self.records._canonical_record_bytes(value), _canonical_bytes(value))
        for value in ({"k": -0.0, "big": 2 ** 70, "neg": -2 ** 70}, [2 ** 64, -2 ** 64, 0.5, -1e-300],
                      {"☃": "\U0001F600", "nested": [[[{"deep": [True, False, None]}]]]}):
            with self.subTest(value=value):
                self.assertEqual(self.records._canonical_record_bytes(value), _canonical_bytes(value))
        overflow = _read_json_bytes(b"1e999")
        with self.assertRaises(ValueError) as reference:
            _canonical_bytes(overflow)
        with self.assertRaises(ValueError) as fast:
            self.records._canonical_record_bytes(overflow)
        self.assertEqual((type(fast.exception), str(fast.exception)),
                         (type(reference.exception), str(reference.exception)))

    def test_verification_memo_skips_only_unchanged_successful_references(self):
        from dataclasses import replace
        with TemporaryDirectory() as directory:
            ref = self.runtime._write_records(Path(directory) / "stage.records.jsonl",
                                              [{"experiment_id": "one"}, {"experiment_id": "two"}])
            ref.verify()
            with patch.object(self.records, "_read_json_bytes", side_effect=AssertionError("memo miss")):
                ref.verify()  # same reference and unchanged files: no passes
                with self.assertRaises(AssertionError):
                    replace(ref, sha256="0" * 64).verify()  # different declared hash re-verifies
            bad = replace(ref, scientific_sha256="0" * 64)
            for _ in range(2):  # failures are never remembered
                with self.assertRaises(ValueError):
                    bad.verify()
            path = Path(ref.path)
            altered = bytearray(path.read_bytes())
            altered[9] ^= 1  # same size, valid gzip, different stored bytes
            replacement = path.with_name("replacement")
            replacement.write_bytes(altered)
            os.replace(replacement, path)
            with self.assertRaisesRegex(ValueError, "stored SHA"):
                ref.verify()

    def evidence(self, experiment, kind, boundary, native):
        from market_analysis.historical_market_state_candidate_evidence import HistoricalStudyCandidateEvidence
        return HistoricalStudyCandidateEvidence(0, "2024-01-01", "development", experiment, "algo-1",
                                                "cfg-☃", boundary, kind, "OK", native)

    def test_extract_candidate_inputs_matches_full_decode_reference(self):
        from market_analysis import historical_market_state_study_execution as execution
        first = [self.evidence("EXP-75-01", "EVENT", 5_000, {"x": Decimal("1.50")}),
                 self.evidence("EXP-75-04B", "EVENT", 10_000, {"causal_onset_observation": {"p": 0.5}}),
                 self.evidence("EXP-75-04B", "RETROSPECTIVE", None, {}),
                 self.evidence("EXP-75-04B", "CONTINUOUS", 60_000, {}),
                 self.evidence("EXP-75-02", "RETROSPECTIVE", None, [1, 2]),
                 self.evidence("EXP-75-03", "EVENT", None, {"empty": None})]
        second = [self.evidence("V1", "CONTINUOUS", 120_000, {"s": "x"}),
                  self.evidence("EXP-75-04B", "EVENT", 15_000, {"causal_onset_observation": {"p": 0.25}})]
        in_memory = (self.evidence("EXP-75-04B", "EVENT", 20_000, {"causal_onset_observation": {"p": 1}}),)
        decoded = []
        decode = self.runtime.decode

        def counting_decode(value, *args, **kwargs):
            decoded.append(1)
            return decode(value, *args, **kwargs)

        with TemporaryDirectory() as directory, \
             patch.object(execution, "_bocpd_onset_evidence", side_effect=lambda items: tuple(items)):
            root = Path(directory)
            first_ref = self.runtime._write_records(root / "first.records.jsonl", first)
            second_ref = self.runtime._write_records(root / "second.records.jsonl", second)
            joined = self.records.JoinedStageRecords((self.records.JoinedStageRecords((first_ref,)),
                                                      second_ref, in_memory))
            expected = _reference_extract_candidate_inputs(joined)
            for verified in (False, True):
                if verified:
                    first_ref.verify()
                    second_ref.verify()
                with self.subTest(verified=verified):
                    decoded.clear()
                    with patch.object(self.runtime, "decode", side_effect=counting_decode):
                        actual = self.records.extract_candidate_inputs(joined)
                    self.assertEqual(actual, expected)
                    self.assertEqual([[type(field) for field in vars(item).values()] for item in actual[0]],
                                     [[type(field) for field in vars(item).values()] for item in expected[0]])
                    self.assertEqual(len(actual[1]), 3)
                    # Unverified: every spooled line gets the typed decode. Verified in
                    # this process: only the two spooled EXP-75-04B EVENT lines.
                    self.assertEqual(len(decoded), 2 if verified else len(first) + len(second))

    def test_extract_candidate_inputs_keeps_stream_integrity_checks(self):
        from dataclasses import replace
        from market_analysis import historical_market_state_study_execution as execution
        with TemporaryDirectory() as directory, \
             patch.object(execution, "_bocpd_onset_evidence", side_effect=lambda items: tuple(items)):
            ref = self.runtime._write_records(Path(directory) / "stage.records.jsonl",
                                              [self.evidence("EXP-75-01", "EVENT", 5_000, {})])
            for bad in (replace(ref, sha256="0" * 64), replace(ref, scientific_sha256="0" * 64),
                        replace(ref, count=2)):
                with self.assertRaises(ValueError):
                    self.records.extract_candidate_inputs(bad)

    def handmade(self, root, operational, scientific):
        """Identity-encoded reference from separately chosen stream contents:
        hash-valid and canonical, so only structural checks can reject it."""
        from market_analysis.historical_market_state_study_json import iter_canonical_study_json
        operational_raw = b"".join(self.runtime._canonical_bytes(value) for value in operational)
        scientific_raw = b"".join("".join(iter_canonical_study_json(item)).encode("ascii") + b"\n"
                                  for item in scientific)
        path, science = root / "handmade.jsonl", root / "handmade.scientific.jsonl"
        path.write_bytes(operational_raw)
        science.write_bytes(scientific_raw)
        return self.records.StageRecords(str(path), len(operational), hashlib.sha256(operational_raw).hexdigest(),
                                         str(science), hashlib.sha256(scientific_raw).hexdigest())

    def test_extract_candidate_inputs_rejects_operational_scientific_disagreement(self):
        from market_analysis import historical_market_state_study_execution as execution
        consistent = self.evidence("EXP-75-01", "EVENT", 5_000, {})
        for scientific in (self.evidence("EXP-75-02", "EVENT", 5_000, {}),
                           self.evidence("EXP-75-01", "EVENT", 10_000, {})):
            with self.subTest(scientific=scientific), TemporaryDirectory() as directory, \
                 patch.object(execution, "_bocpd_onset_evidence", side_effect=lambda items: tuple(items)):
                ref = self.handmade(Path(directory), [self.runtime.encode(consistent)], [scientific])
                with self.assertRaisesRegex(ValueError, "stage records stream mismatch"):
                    self.records.extract_candidate_inputs(ref)

    def test_structurally_malformed_record_rejected_until_verified_in_process(self):
        # Decision (b): the producer never decodes what it writes, so the first,
        # unverified pass keeps the typed decode and rejects a hash-valid but
        # malformed record. verify() checks canonical bytes and hashes only;
        # after it succeeds, non-EVENT lines are no longer decoded.
        from market_analysis import historical_market_state_study_execution as execution
        item = self.evidence("EXP-75-01", "CONTINUOUS", 60_000, {})
        malformed = self.runtime.encode(item)
        malformed["fields"]["unknown"] = None
        with TemporaryDirectory() as directory, \
             patch.object(execution, "_bocpd_onset_evidence", side_effect=lambda items: tuple(items)):
            ref = self.handmade(Path(directory), [malformed], [item])
            with self.assertRaises(ValueError):
                self.records.extract_candidate_inputs(ref)
            ref.verify()
            self.assertEqual(self.records.extract_candidate_inputs(ref), ((), ()))

    def test_verification_memo_key_uses_declared_values_not_dataclass_hash(self):
        from dataclasses import dataclass, field, fields, replace
        with TemporaryDirectory() as directory:
            ref = self.runtime._write_records(Path(directory) / "stage.records.jsonl", [{"experiment_id": "one"}])
            ref.verify()
            self.assertIn(ref._verification_key(), self.records._VERIFIED)
            for other in (replace(ref, sha256="0" * 64), replace(ref, scientific_sha256="0" * 64),
                          replace(ref, stored_sha256="0" * 64), replace(ref, scientific_stored_sha256="0" * 64),
                          replace(ref, count=2)):
                with self.subTest(other=other):
                    self.assertNotEqual(other._verification_key(), ref._verification_key())
                    self.assertFalse(other._verified())
                    with self.assertRaises(ValueError):
                        other.verify()
                    self.assertNotIn(other._verification_key(), self.records._VERIFIED)

            @dataclass(frozen=True)
            class Annotated(self.records.StageRecords):
                notes: list = field(default_factory=list)

            annotated = Annotated(**{item.name: getattr(ref, item.name) for item in fields(ref)}, notes=["x"])
            with self.assertRaises(TypeError):
                hash(annotated)
            self.assertTrue(annotated._verified())  # same declared values and files as ref
            annotated.verify()


class CandidateBatchLayoutTests(unittest.TestCase):
    SELECTORS = ("hmm", *(f"fixed-{index:02d}" for index in range(20)),
                 *(f"atr-{index}" for index in range(10)), "taker-flow", "funding")

    def layout(self, workers, batch_size_env=None):
        from market_analysis import historical_market_state_study_execution as execution
        with patch.dict(os.environ, {}):
            os.environ.pop("STUDY_STAGE_BATCH_SIZE", None)
            if batch_size_env is not None:
                os.environ["STUDY_STAGE_BATCH_SIZE"] = batch_size_env
            return execution._candidate_batch_layout(self.SELECTORS, {}, workers)

    def groups(self, batch_size, batched):
        return [self.SELECTORS[start:min(start + batch_size, batched)] for start in range(0, batched, batch_size)]

    def test_pool_batch_size_is_balanced_across_workers(self):
        self.assertEqual(self.layout(3), (11, 31))
        self.assertEqual([len(group) for group in self.groups(*self.layout(3))], [11, 11, 9])
        self.assertEqual(self.layout(3, ""), (11, 31))

    def test_single_worker_keeps_default_batch_size(self):
        from market_analysis import historical_market_state_study_execution as execution
        self.assertEqual(self.layout(1), (execution.STAGE_BATCH_SIZE, 31))
        self.assertEqual(execution.STAGE_BATCH_SIZE, 9)

    def test_explicit_batch_size_overrides_derivation(self):
        for workers in (1, 3):
            with self.subTest(workers=workers):
                self.assertEqual(self.layout(workers, "5"), (5, 31))
                self.assertEqual(self.layout(workers, "1"), (1, 0))
                with self.assertRaises(ValueError):
                    self.layout(workers, "0")

    def test_batches_cover_each_batchable_selector_once_in_order(self):
        for workers, env in ((1, None), (2, None), (3, None), (4, None), (3, "5"), (1, "31"), (3, "40")):
            with self.subTest(workers=workers, env=env):
                batch_size, batched = self.layout(workers, env)
                flat = [selector for group in self.groups(batch_size, batched) for selector in group]
                self.assertEqual(flat, list(self.SELECTORS[:31]))


_FAKE_POOL_WORKER = """
import os, sys, time
from pathlib import Path
from market_analysis import historical_study_runtime as runtime
from market_analysis.historical_replay_runtime import _read_json_bytes

def fake_stage(job_path, lease, *, batch_position=None):
    job = _read_json_bytes(Path(job_path).read_bytes())
    stage_id = job["identity"]["stage_id"]
    with open(os.environ["FAKE_STAGE_LOG"], "a") as log:
        log.write(stage_id + "\\n")
    if stage_id == os.environ.get("FAKE_FAILING_STAGE"):
        raise SystemExit(3)
    if stage_id in os.environ.get("FAKE_SLOW_STAGES", "").split(","):
        time.sleep(300)
    runtime._publish_stage(Path(job["result_path"]), job["identity"], ("result", stage_id))

runtime._worker_owned = fake_stage
runtime._worker_pool(sys.argv[1], int(sys.argv[2]), int(sys.argv[3]))
"""


class ParallelStageWorkerTests(unittest.TestCase):
    BATCH = 3

    def setUp(self):
        from market_analysis import historical_study_runtime as runtime
        self.runtime = runtime
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.log = self.root / "stages.log"
        environment = patch.dict(os.environ, {"FAKE_STAGE_LOG": str(self.log)})
        environment.start()
        self.addCleanup(environment.stop)

    def stream(self, name):
        period = self.root / name
        (period / "study-points").mkdir(parents=True)
        return SimpleNamespace(root=period / "study-points", metadata={"stream_sha256": "f" * 64},
                               identity={"study_period_index": 0, "runtime_implementation_revision": "a" * 40})

    def stages(self, count=8):
        stages = []
        for index in range(count):
            request = {"action": "fake", "value": index}
            stages.append((f"fake-{index:02d}", self.runtime.input_descriptor(request),
                           lambda request=request: request))
        return stages

    def batches(self, stages):
        return [stages[start:start + self.BATCH] for start in range(0, len(stages), self.BATCH)]

    def fake_pool_arguments(self, pool_path, worker, lease):
        return [sys.executable, "-c", _FAKE_POOL_WORKER, str(pool_path), str(worker), str(lease.descriptor)]

    def in_process_batch(self, batch_path, environment, lease, *, stage_id=None, batch=False):
        from market_analysis.historical_replay_runtime import _read_json_bytes
        for job_path in _read_json_bytes(Path(batch_path).read_bytes())["batch"]:
            job = _read_json_bytes(Path(job_path).read_bytes())
            with self.log.open("a") as log:
                log.write(job["identity"]["stage_id"] + "\n")
            self.runtime._publish_stage(Path(job["result_path"]), job["identity"],
                                        ("result", job["identity"]["stage_id"]))

    def sequential(self, stream, stages, lease):
        completed, position = [], 0
        with patch.object(self.runtime, "_run_owned_worker", side_effect=self.in_process_batch):
            while position < len(stages):
                batch = self.runtime.run_stage_batch(stream, stages[position:position + self.BATCH],
                                                     worker_lease=lease)
                completed.extend(batch)
                position += len(batch)
        return completed

    def parallel(self, stream, stages, lease, workers=3):
        with patch.object(self.runtime, "_pool_worker_arguments", side_effect=self.fake_pool_arguments):
            return [item for batch in self.runtime.run_stage_batches(
                stream, self.batches(stages), workers=workers, worker_lease=lease) for item in batch]

    def published(self, stream):
        root = stream.root.parent / "post-replay"
        return {path.name: path.read_bytes() for path in root.iterdir()}

    def test_one_and_three_workers_produce_identical_ordered_outputs(self):
        from market_analysis.historical_run_directory import owned_run_directory
        stages = self.stages()
        outputs = {}
        for name, runner in (("one", self.sequential), ("three", self.parallel)):
            stream = self.stream(name)
            with owned_run_directory(stream.root.parent) as lease:
                completed = runner(stream, stages, lease)
            outputs[name] = ([(stage_id, result) for stage_id, result, _ in completed], self.published(stream))
        self.assertEqual(outputs["one"], outputs["three"])
        self.assertEqual([stage_id for stage_id, _ in outputs["three"][0]], [stage[0] for stage in stages])
        self.assertEqual(set(outputs["three"][1]), {stage[0] + ".json" for stage in stages})
        # Every stage ran exactly once per mode: each pool batch was claimed once.
        ran = self.log.read_text().split()
        self.assertEqual(sorted(ran), sorted([stage[0] for stage in stages] * 2))

    def test_one_failing_worker_fails_the_run_and_terminates_the_others(self):
        from market_analysis.historical_run_directory import owned_run_directory
        stages = self.stages(9)
        stream = self.stream("failing")
        children, real_popen = [], subprocess.Popen

        def recording_popen(*args, **kwargs):
            children.append(real_popen(*args, **kwargs))
            return children[-1]

        slow = ",".join(stage[0] for stage in stages[1:])
        with patch.dict(os.environ, {"FAKE_FAILING_STAGE": stages[0][0], "FAKE_SLOW_STAGES": slow}), \
             patch.object(self.runtime.subprocess, "Popen", side_effect=recording_popen), \
             owned_run_directory(stream.root.parent) as lease:
            started = time.monotonic()
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                self.parallel(stream, stages, lease)
        self.assertLess(time.monotonic() - started, 120)
        self.assertEqual(raised.exception.returncode, 3)
        self.assertEqual(len(children), 3)
        self.assertTrue(all(child.returncode is not None for child in children))
        self.assertTrue(any(child.returncode < 0 for child in children))
        remaining = set(self.published(stream))
        self.assertFalse({name for name in remaining if name.startswith((".pool-", ".batch-"))})
        self.assertFalse({stage[0] + ".json" for stage in stages} & remaining)

    def test_each_pool_batch_is_claimed_exactly_once(self):
        runtime = self.runtime
        root = self.root / "period" / "post-replay"
        root.mkdir(parents=True)
        batch_paths = []
        for index in range(12):
            path = root / f".batch-fake-{index:02d}.json"
            path.write_bytes(runtime._canonical_bytes({"batch": [str(root / "unused.job.json")],
                                                      "stop_after_epoch": float(time.time() + 3600)}))
            batch_paths.append(str(path))
        pool_path = root / ".pool-fake-00.json"
        pool_path.write_bytes(runtime._canonical_bytes({"batches": batch_paths}))
        ran, lock = [], threading.Lock()

        def fake_batch(batch_path, lease):
            time.sleep(0.01)
            with lock:
                ran.append(str(batch_path))

        lease = SimpleNamespace(validate=lambda root: None)
        with patch.object(runtime, "_worker_batch_owned", side_effect=fake_batch):
            threads = [threading.Thread(target=runtime._worker_pool_owned, args=(pool_path, worker, lease))
                       for worker in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(sorted(ran), sorted(batch_paths))
        for index in range(len(batch_paths)):
            self.assertRegex(runtime._pool_claim_path(pool_path, index).read_text(), r"^[0-3]\n$")

    def test_stage_worker_count_is_clamped(self):
        from market_analysis import historical_market_state_study_execution as execution
        cpus = os.cpu_count() or 1
        for raw, expected in ((None, min(3, cpus)), ("", min(3, cpus)), ("1", 1), ("0", 1),
                              ("-4", 1), ("1000", cpus)):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {} if raw is None else {"STUDY_STAGE_WORKERS": raw}):
                    if raw is None:
                        os.environ.pop("STUDY_STAGE_WORKERS", None)
                    self.assertEqual(execution._stage_workers(), expected)
        with patch.dict(os.environ, {"STUDY_STAGE_WORKERS": "three"}):
            with self.assertRaises(ValueError):
                execution._stage_workers()


if __name__ == "__main__":
    unittest.main()
