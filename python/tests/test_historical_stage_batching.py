"""Batch post-replay workers, cached shared V1 readers and batch-aware staging."""

from decimal import Decimal
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from market_analysis import historical_market_state_study_execution as execution
from market_analysis import historical_operational_events as events
from market_analysis import historical_shared_v1 as shared_v1
from market_analysis import historical_study_runtime as runtime
from market_analysis.experiments.market_state_common import advance_canonical_branch
from market_analysis.historical_replay import canonical_replay_point_id
from market_analysis.historical_replay_runtime import _canonical_bytes, _sha
from market_analysis.historical_run_directory import owned_run_directory
from market_analysis.historical_shared_v1 import SharedV1Reference, SharedV1Writer
from market_analysis.historical_study_runtime import (
    SPOOL_VERSION, CompactStudyPoint, StudyPointStream, input_descriptor,
    run_stage, run_stage_batch,
)
from market_analysis.market_episode_lifecycle import MarketEpisodeLifecycleConfig
from market_analysis.movement_classifier import MarketClassifierConfig, SymbolSourceTimeEvidence

from test_market_state_shared_v1_branch_parity import SYMBOLS, _evaluation

RUN_FINGERPRINT = "f" * 64
POINT_COUNT = 12


def _reset_process_caches():
    runtime._BATCH_WORKER = False
    runtime._VALIDATED_SPOOLS.clear()
    runtime._COMPACT_POINTS.clear()
    shared_v1._PROCESS_CACHE_ENABLED = False
    shared_v1._VERIFIED_BRANCHES.clear()


def _evidence():
    return tuple(SymbolSourceTimeEvidence(symbol, None, None, None) for symbol in SYMBOLS)


def _write_spool(period_root, count=POINT_COUNT):
    root = Path(period_root) / "study-points"
    root.mkdir(parents=True)
    identity = {"run_fingerprint": RUN_FINGERPRINT, "phase": "development",
                "point_count": count, "first_boundary": 0,
                "last_boundary": (count - 1) * 5_000}
    digest = hashlib.sha256()
    with (root / "points.jsonl").open("wb") as handle:
        for tick in range(count):
            boundary = tick * 5_000
            raw = _canonical_bytes(CompactStudyPoint(
                canonical_replay_point_id(RUN_FINGERPRINT, boundary), boundary,
                _evaluation(boundary), _evidence(), "development"))
            handle.write(raw)
            digest.update(raw)
    body = {"schema_version": SPOOL_VERSION, "identity": identity, "point_count": count,
            "first_boundary": 0, "last_boundary": (count - 1) * 5_000,
            "stream_sha256": digest.hexdigest()}
    (root / "manifest.json").write_bytes(
        _canonical_bytes({**body, "metadata_sha256": _sha(_canonical_bytes(body))}))
    return StudyPointStream(root, identity)


def _write_shared_v1(stream):
    writer = SharedV1Writer(stream)
    previous_state = None
    for point in stream:
        classification, lifecycle = advance_canonical_branch(
            point.movement_evaluation, point.source_time_evidence, previous_state,
            MarketClassifierConfig(), MarketEpisodeLifecycleConfig())
        writer.append(point, classification, lifecycle)
        previous_state = lifecycle.next_state
    return writer.publish()


def _drive(reader, points, *, passes=2):
    """Exercise branch_for_point (incl. wrap-around), get() and finish()."""
    observed = []
    for _ in range(passes):
        for point in points:
            evaluation, evidence = point.movement_evaluation, point.source_time_evidence
            branch = reader.branch_for_point(evaluation, evidence)
            again = reader.branch_for_point(evaluation, evidence)
            boundary = point.evaluation_boundary_time_ms
            observed.append((boundary, branch, again is branch, reader.get(boundary) is branch,
                             reader.get(boundary - 5_000), reader.get(boundary + 5_000),
                             reader.count))
    reader.finish()
    observed.append(reader.completed)
    return observed


def _outcome(function, *args):
    try:
        return ("ok", function(*args))
    except Exception as exc:  # noqa: BLE001 - the exception itself is compared
        return ("error", type(exc), str(exc))


class _ProcessCacheTestCase(unittest.TestCase):
    def setUp(self):
        _reset_process_caches()
        self.addCleanup(_reset_process_caches)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.period_root = Path(directory.name) / "period"
        self.stream = _write_spool(self.period_root)

    def fresh_stream(self):
        return StudyPointStream(self.stream.root, self.stream.identity)


class CachedSharedV1ReaderTests(_ProcessCacheTestCase):
    def setUp(self):
        super().setUp()
        self.reference = _write_shared_v1(self.fresh_stream())
        self.points = tuple(self.fresh_stream())

    def cached_reader(self):
        shared_v1.enable_process_cache()
        first = self.reference.reader(self.fresh_stream())
        _drive(first, self.points, passes=1)
        reader = self.reference.reader(self.fresh_stream())
        self.assertIsNotNone(reader._cached)
        return reader

    def test_cached_reader_matches_fresh_reader_including_wrap_around(self):
        fresh = _drive(self.reference.reader(self.fresh_stream()), self.points, passes=3)
        cached = _drive(self.cached_reader(), self.points, passes=3)
        self.assertEqual(cached, fresh)
        self.assertEqual(repr(cached), repr(fresh))

    def test_first_reader_wrap_switches_to_verified_cache(self):
        shared_v1.enable_process_cache()
        reader = self.reference.reader(self.fresh_stream())
        self.assertIsNone(reader._cached)
        observed = _drive(reader, self.points, passes=2)
        self.assertIsNotNone(reader._cached)
        shared_v1._PROCESS_CACHE_ENABLED = False
        shared_v1._VERIFIED_BRANCHES.clear()
        self.assertEqual(observed, _drive(self.reference.reader(self.fresh_stream()),
                                          self.points, passes=2))

    def test_other_point_objects_are_verified_against_the_file(self):
        reader = self.cached_reader()
        other_points = tuple(self.fresh_stream())  # equal values, different objects
        self.assertIsNot(other_points[0].movement_evaluation, self.points[0].movement_evaluation)
        cached = _drive(reader, other_points, passes=1)
        shared_v1._PROCESS_CACHE_ENABLED = False
        self.assertEqual(cached, _drive(self.reference.reader(self.fresh_stream()),
                                        other_points, passes=1))

    def test_same_errors_as_fresh_reader(self):
        def errors():
            results = {}
            bad_reference = SharedV1Reference(self.reference.path, "0" * 64)
            results["manifest digest"] = _outcome(bad_reference.reader, self.fresh_stream())

            def partial():
                reader = self.reference.reader(self.fresh_stream())
                for point in self.points[:5]:
                    reader.branch_for_point(point.movement_evaluation, point.source_time_evidence)
                reader.finish()
            results["count mismatch"] = _outcome(partial)

            def finish_twice():
                reader = self.reference.reader(self.fresh_stream())
                _drive(reader, self.points, passes=1)
                reader.finish()
            results["finish twice"] = _outcome(finish_twice)

            def read_after_finish():
                reader = self.reference.reader(self.fresh_stream())
                for point in self.points[:3]:
                    reader.branch_for_point(point.movement_evaluation, point.source_time_evidence)
                try:
                    reader.finish()
                except ValueError:
                    pass
                point = self.points[3]
                reader.branch_for_point(point.movement_evaluation, point.source_time_evidence)
            results["read after finish"] = _outcome(read_after_finish)

            def past_end():
                reader = self.reference.reader(self.fresh_stream())
                for point in self.points:
                    reader.branch_for_point(point.movement_evaluation, point.source_time_evidence)
                reader.branch_for_point(_evaluation(POINT_COUNT * 5_000), _evidence())
            results["past end"] = _outcome(past_end)

            def out_of_order():
                reader = self.reference.reader(self.fresh_stream())
                point = self.points[2]
                reader.branch_for_point(point.movement_evaluation, point.source_time_evidence)
            results["out of order"] = _outcome(out_of_order)
            return results

        fresh = errors()
        self.cached_reader()
        cached = errors()
        self.assertEqual(cached, fresh)
        self.assertTrue(all(outcome[0] == "error" for outcome in fresh.values()), fresh)

    def test_wrong_manifest_branch_digest_or_count_raises_in_both_modes(self):
        path = Path(self.reference.path)
        manifest_path = path.with_suffix(".manifest.json")
        original = manifest_path.read_bytes()
        metadata = runtime._read_json_bytes(original)
        for change in ({"branch_sha256": "0" * 64}, {"count": POINT_COUNT + 1}):
            for cached in (False, True):
                with self.subTest(change=change, cached=cached):
                    manifest_path.write_bytes(original)
                    _reset_process_caches()
                    if cached:
                        self.cached_reader()
                    raw = _canonical_bytes({**metadata, **change})
                    manifest_path.write_bytes(raw)
                    reference = SharedV1Reference(str(path), _sha(raw))

                    def consume():
                        _drive(reference.reader(self.fresh_stream()), self.points, passes=1)
                    outcome = _outcome(consume)
                    self.assertEqual(outcome[0], "error")
                    self.assertIs(outcome[1], ValueError)
        manifest_path.write_bytes(original)

    def test_modified_file_invalidates_cache(self):
        path = Path(self.reference.path)
        self.cached_reader()
        status = path.stat()
        os.utime(path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))
        reader = self.reference.reader(self.fresh_stream())
        self.assertIsNone(reader._cached)
        reader.handle.close()
        with path.open("ab") as handle:
            handle.write(b"\n")
        reader = self.reference.reader(self.fresh_stream())
        self.assertIsNone(reader._cached)
        with self.assertRaisesRegex(ValueError, "did not complete with matching digest/count"):
            _drive(reader, self.points, passes=1)


class CompactPointCacheTests(_ProcessCacheTestCase):
    def test_batch_worker_reuses_points_with_adopted_completion(self):
        runtime._BATCH_WORKER = True
        first_stream = self.fresh_stream()
        first = runtime._compact_points(first_stream)
        self.assertTrue(first_stream.validated_completion)
        second_stream = self.fresh_stream()
        second = runtime._compact_points(second_stream)
        self.assertIs(second, first)
        self.assertTrue(second_stream.validated_completion)
        self.assertEqual(len(first[0]), POINT_COUNT)
        self.assertEqual(first[1], tuple(point.experiment_point() for point in first[0]))

    def test_per_stage_worker_never_caches(self):
        first = runtime._compact_points(self.fresh_stream())
        second = runtime._compact_points(self.fresh_stream())
        self.assertIsNot(second[0], first[0])
        self.assertEqual(second, first)
        self.assertEqual(runtime._COMPACT_POINTS, {})

    def test_adoption_requires_a_validated_unchanged_spool(self):
        runtime._BATCH_WORKER = True
        with self.assertRaisesRegex(ValueError, "not fully validated"):
            self.fresh_stream().adopt_validated_completion()
        first = runtime._compact_points(self.fresh_stream())
        points_path = self.stream.root / "points.jsonl"
        status = points_path.stat()
        os.utime(points_path, ns=(status.st_atime_ns, status.st_mtime_ns + 1_000_000_000))
        changed = self.fresh_stream()
        with self.assertRaisesRegex(ValueError, "not fully validated"):
            changed.adopt_validated_completion()
        again = runtime._compact_points(changed)
        self.assertIsNot(again[0], first[0])
        self.assertTrue(changed.validated_completion)


def _synthetic_requests(names):
    return [(name, {"action": "event-context", "selector": name,
                    "boundaries": (index, index + 1)})
            for index, name in enumerate(names)]


class BatchWorkerTests(_ProcessCacheTestCase):
    STAGES = ("s0", "s1", "s2", "s3", "s4")

    def setUp(self):
        super().setUp()
        self.failing = set()
        self.unpublishing = False
        self.events = []
        self.worker_calls = []
        for name, value in (("_run_owned_worker", self.in_process_worker),
                            ("_execute_scientific_stage", self.synthetic_stage),
                            ("_verify_worker_runtime_revision", lambda identity: None)):
            patcher = patch.object(runtime, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def synthetic_stage(self, request):
        selector = request["selector"]
        if selector in self.failing:
            raise ValueError("synthetic stage failure")
        return tuple({"selector": selector, "index": index, "value": Decimal("1.25") * index,
                      "boundaries": request["boundaries"]} for index in range(4))

    def in_process_worker(self, job_path, environment, lease, *, stage_id=None, batch=False):
        """A fresh worker process, run in-process; any exception is a non-zero exit."""
        self.worker_calls.append((stage_id, batch))
        _reset_process_caches()
        try:
            lease.validate(Path(job_path).parent.parent)
            if self.unpublishing:
                return
            (runtime._worker_batch_owned if batch else runtime._worker_owned)(job_path, lease)
        except Exception as exc:
            raise subprocess.CalledProcessError(1, ["worker"]) from exc
        finally:
            _reset_process_caches()

    def progress(self, event, details):
        self.events.append((event, details.get("stage_id")))

    def stages(self, names=STAGES):
        return [(name, input_descriptor(request), (lambda request=request: request))
                for name, request in _synthetic_requests(names)]

    def run_all(self, lease, batch_size):
        stages = self.stages()
        if batch_size == 1:
            return [run_stage(self.stream, name, descriptor=descriptor, prepare=prepare,
                              progress=self.progress, worker_lease=lease)
                    for name, descriptor, prepare in stages]
        results, position = [], 0
        while position < len(stages):
            completed = run_stage_batch(self.stream, stages[position:position + batch_size],
                                        progress=self.progress, worker_lease=lease)
            self.assertTrue(completed)
            results.extend(result for _, result, _ in completed)
            position += len(completed)
        return results

    def published(self):
        root = self.period_root / "post-replay"
        return {path.name: path.read_bytes() for path in sorted(root.iterdir())
                if not path.name.endswith(".observations.json")}

    def test_batch_size_three_publishes_identical_stages_to_per_stage_workers(self):
        with owned_run_directory(self.period_root) as lease:
            single = self.run_all(lease, 1)
            single_files = self.published()
            single_events = list(self.events)
            shutil.rmtree(self.period_root / "post-replay")
            self.events.clear()
            batched = self.run_all(lease, 3)
            batched_files = self.published()
        self.assertEqual(batched, single)
        self.assertEqual(batched_files, single_files)
        self.assertEqual(sorted(batched_files), sorted(
            name for stage in self.STAGES for name in (
                f"{stage}.json", f"{stage}.records.jsonl.gz",
                f"{stage}.records.scientific.jsonl.gz")))
        for stage in self.STAGES:
            self.assertIn(("POST_REPLAY_STAGE_COMPLETED", stage), self.events)
            self.assertIn(("POST_REPLAY_STAGE_COMPLETED", stage), single_events)
        self.assertEqual([event for event in self.events if event[0] == "BEFORE_NEW_STAGE"],
                         [("BEFORE_NEW_STAGE", "s0"), ("BEFORE_NEW_STAGE", "s3")])
        self.assertNotIn("POST_REPLAY_STAGE_REUSED", {event for event, _ in self.events})
        self.assertEqual(self.worker_calls[-2:], [("batch:s0..s2", True), ("batch:s3..s4", True)])

    def test_published_stages_are_reused_and_excluded_from_the_batch(self):
        with owned_run_directory(self.period_root) as lease:
            stages = self.stages()
            run_stage(self.stream, "s0", descriptor=stages[0][1], prepare=stages[0][2],
                      progress=self.progress, worker_lease=lease)
            self.events.clear()
            completed = run_stage_batch(self.stream, stages[:3], progress=self.progress,
                                        worker_lease=lease)
        self.assertEqual([name for name, _, _ in completed], ["s0", "s1", "s2"])
        self.assertEqual(self.events[0], ("POST_REPLAY_STAGE_REUSED", "s0"))
        self.assertEqual(self.worker_calls[-1], ("batch:s1..s2", True))

    def test_worker_duration_drives_completion_event_and_returned_seconds(self):
        recorded = []
        with owned_run_directory(self.period_root) as lease:
            completed = run_stage_batch(self.stream, self.stages()[:2],
                                        progress=lambda event, details: recorded.append((event, details)),
                                        worker_lease=lease)
        root = self.period_root / "post-replay"
        for name, _, seconds in completed:
            observed = runtime._read_json_bytes((root / f"{name}.observations.json").read_bytes())
            self.assertEqual(observed["batch_worker"]["size"], 2)
            self.assertEqual(seconds, observed["monotonic_duration_seconds"])
            event = next(details for event, details in recorded
                         if event == "POST_REPLAY_STAGE_COMPLETED" and details["stage_id"] == name)
            self.assertEqual(event["monotonic_duration_seconds"], seconds)

    def test_past_stop_epoch_runs_one_stage_and_parent_re_enters(self):
        with owned_run_directory(self.period_root) as lease, \
                patch.object(runtime, "_batch_stop_after_epoch", lambda: 0.0):
            stages = self.stages()[:3]
            first = run_stage_batch(self.stream, stages, progress=self.progress, worker_lease=lease)
            self.assertEqual([name for name, _, _ in first], ["s0"])
            root = self.period_root / "post-replay"
            self.assertEqual(sorted(path.name for path in root.iterdir()
                                    if path.name.endswith((".job.json", ".request.json"))
                                    or path.name.startswith(".batch-")), [])
            self.assertFalse((root / "s1.json").exists())
            second = run_stage_batch(self.stream, stages[1:], progress=self.progress,
                                     worker_lease=lease)
            self.assertEqual([name for name, _, _ in second], ["s1"])
            third = run_stage_batch(self.stream, stages[2:], progress=self.progress,
                                    worker_lease=lease)
            self.assertEqual([name for name, _, _ in third], ["s2"])
        self.assertEqual([event for event in self.events if event[0] == "BEFORE_NEW_STAGE"],
                         [("BEFORE_NEW_STAGE", "s0"), ("BEFORE_NEW_STAGE", "s1"),
                          ("BEFORE_NEW_STAGE", "s2")])

    def test_yield_before_new_batch_starts_no_worker(self):
        class Yield(Exception):
            pass

        def progress(event, details):
            if event == "BEFORE_NEW_STAGE":
                raise Yield
        with owned_run_directory(self.period_root) as lease:
            with self.assertRaises(Yield):
                run_stage_batch(self.stream, self.stages()[:3], progress=progress,
                                worker_lease=lease)
        self.assertEqual(self.worker_calls, [])

    def test_mid_batch_failure_keeps_earlier_stages_and_raises_like_run_stage(self):
        self.failing = {"s1"}
        with owned_run_directory(self.period_root) as lease:
            stages = self.stages()
            with self.assertRaises(subprocess.CalledProcessError):
                run_stage(self.stream, "s1", descriptor=stages[1][1], prepare=stages[1][2],
                          worker_lease=lease)
            shutil.rmtree(self.period_root / "post-replay")
            with self.assertRaises(subprocess.CalledProcessError):
                run_stage_batch(self.stream, stages[:3], progress=self.progress,
                                worker_lease=lease)
            root = self.period_root / "post-replay"
            self.assertTrue((root / "s0.json").exists())
            self.assertFalse((root / "s1.json").exists())
            # The failed stage keeps its diagnostics; never-started stages leave none.
            self.assertTrue((root / "s1.job.json").exists())
            self.assertFalse((root / "s2.job.json").exists())
            self.assertIn(("POST_REPLAY_STAGE_COMPLETED", "s0"), self.events)
            self.failing = set()
            self.events.clear()
            completed = run_stage_batch(self.stream, stages[:3], progress=self.progress,
                                        worker_lease=lease)
        self.assertEqual([name for name, _, _ in completed], ["s0", "s1", "s2"])
        self.assertEqual(self.events[0], ("POST_REPLAY_STAGE_REUSED", "s0"))

    def test_exit_zero_without_publication_fails_like_run_stage(self):
        self.unpublishing = True
        with owned_run_directory(self.period_root) as lease:
            stages = self.stages()
            single = _outcome(lambda: run_stage(self.stream, "s0", descriptor=stages[0][1],
                                                prepare=stages[0][2], worker_lease=lease))
            batched = _outcome(lambda: run_stage_batch(self.stream, stages[:2],
                                                       worker_lease=lease))
        self.assertEqual(single[0], "error")
        self.assertIs(batched[1], single[1])

    def test_batch_job_must_stay_inside_the_leased_stage_directory(self):
        with owned_run_directory(self.period_root) as lease:
            root = self.period_root / "post-replay"
            root.mkdir()
            batch_path = root / ".batch-x.json"
            batch_path.write_bytes(_canonical_bytes({
                "batch": [str(self.period_root / "elsewhere.job.json")],
                "stop_after_epoch": 1.0}))
            with self.assertRaisesRegex(ValueError, "outside the leased stage directory"):
                runtime._worker_batch_owned(batch_path, lease)
            batch_path.write_bytes(_canonical_bytes({"batch": [], "stop_after_epoch": 1.0}))
            with self.assertRaisesRegex(ValueError, "invalid post-replay batch job"):
                runtime._worker_batch_owned(batch_path, lease)


class RemainingComputeSecondsTests(unittest.TestCase):
    def setUp(self):
        saved = {name: getattr(events, name)
                 for name in ("_sink", "_public", "_started", "_deadline", "_context")}
        self.addCleanup(lambda: [setattr(events, name, value) for name, value in saved.items()])

    def test_without_configured_deadline(self):
        events._deadline = None
        self.assertEqual(events.remaining_compute_seconds(), float("inf"))
        stop = runtime._batch_stop_after_epoch()
        self.assertGreater(stop, time.time() + 10 ** 8)

    def test_with_configured_deadline(self):
        with TemporaryDirectory() as directory:
            events.configure(Path(directory) / "events.jsonl", operation="test",
                             phase="development", deadline_epoch=time.time() + 200)
            remaining = events.remaining_compute_seconds()
            self.assertTrue(195 < remaining <= 200, remaining)
            self.assertAlmostEqual(runtime._batch_stop_after_epoch(),
                                   time.time() + remaining - 30, delta=2)
        events._deadline = time.monotonic() - 5
        self.assertEqual(events.remaining_compute_seconds(), 0.0)


def _fake_result(selector):
    return ((f"record-{selector}",), [{"selector": selector}], (f"identity-{selector}",),
            {selector: "report"} if not selector.startswith(("hmm", "fixed-", "atr-")) else {},
            "block" if selector == "hmm" else None, "model-sha" if selector == "hmm" else None)


class StagedCandidateBatchingTests(unittest.TestCase):
    def run_execution(self, batch_size, *, stop_after=2):
        calls = []

        def single(stream, selector, **options):
            calls.append(("single", selector))
            return _fake_result(selector)

        def batch(stream, stages, *, progress=None, worker_lease=None):
            names = [name for name, _, _ in stages]
            calls.append(("batch", tuple(names)))
            # Simulate a clean deadline stop after a short prefix.
            return [(name, _fake_result(name), 0.5) for name in names[:stop_after]]

        metrics = execution.StudyPeriodRuntimeMetrics()
        prepared = SimpleNamespace(canonical_replay_result=SimpleNamespace(points="stream"))
        with patch.object(execution, "run_stage", single), \
                patch.object(execution, "run_stage_batch", batch), \
                patch.object(execution, "stage_dependencies", lambda stream, ids: ()), \
                patch.object(execution, "_stage_prepared", lambda prepared, selector: None), \
                patch.object(execution, "input_descriptor",
                             lambda request: {"selector": request["selector"]}), \
                patch.dict(os.environ, {"STUDY_STAGE_BATCH_SIZE": str(batch_size)}):
            result = execution._staged_candidate_execution(
                prepared, {}, None, runtime_metrics=metrics, worker_lease=object())
        return result, calls, metrics

    def test_batched_and_per_stage_execution_return_identical_results(self):
        selectors = execution._candidate_stage_selectors()
        core = [s for s in selectors if s == "hmm" or s.startswith(("fixed-", "atr-"))]
        extensions = [s for s in selectors if s not in core]
        single, single_calls, _ = self.run_execution(1)
        batched, batched_calls, metrics = self.run_execution(3)
        self.assertEqual(batched, single)
        self.assertEqual(single_calls, [("single", selector) for selector in selectors])
        self.assertEqual([selector for kind, names in batched_calls if kind == "batch"
                          for selector in names[:2]], core)
        self.assertEqual([names for kind, names in batched_calls if kind == "single"], extensions)
        self.assertTrue(all(len(names) <= 3 for kind, names in batched_calls if kind == "batch"))
        timings = metrics.report_timings()
        self.assertGreaterEqual(timings["core_experiment_seconds"],
                                0.5 * len([s for s in core if not s.startswith("atr-")]))
        self.assertGreaterEqual(timings["atr_06b_seconds"],
                                0.5 * len([s for s in core if s.startswith("atr-")]))

    def test_batch_size_setting(self):
        for raw, expected in (("", execution.STAGE_BATCH_SIZE), ("1", 1), ("12", 12)):
            with patch.dict(os.environ, {"STUDY_STAGE_BATCH_SIZE": raw}):
                self.assertEqual(execution._stage_batch_size(), expected)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("STUDY_STAGE_BATCH_SIZE", None)
            self.assertEqual(execution._stage_batch_size(), 9)
        for raw in ("0", "-2", "abc", "2.5"):
            with patch.dict(os.environ, {"STUDY_STAGE_BATCH_SIZE": raw}), \
                    self.assertRaisesRegex(ValueError, "STUDY_STAGE_BATCH_SIZE"):
                execution._stage_batch_size()


if __name__ == "__main__":
    unittest.main()
