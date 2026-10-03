"""Generated fixtures for compact, lossless Part-B candidate adapters."""

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
import unittest

from market_analysis.historical_market_state_candidate_evidence import (
    adapt_candidate_result,
)
from market_analysis.historical_market_state_study import (
    parse_historical_market_state_study_manifest_json,
)


MINUTE = 60_000
MANIFEST_PATH = (Path(__file__).parents[2] / "research" /
                 "historical-market-state-study-v1" / "selection" /
                 "historical-market-state-study-v1-manifest.json")


@dataclass(frozen=True)
class FixturePoint:
    evaluation_boundary_time_ms: int
    status: str
    positive_accumulator: float
    cusum_directional_onset: bool


@dataclass(frozen=True)
class FixtureCUSUMResult:
    points: tuple[FixturePoint, ...]
    summaries: dict


@dataclass(frozen=True)
class FixturePELTResult:
    segmentations: dict
    summaries: dict


@dataclass(frozen=True)
class FixtureBOCPDObservation:
    evaluation_boundary_time_ms: int
    detector_state: str
    recent_change_probability: float


@dataclass(frozen=True)
class FixtureBOCPDPoint:
    evaluation_boundary_time_ms: int
    bocpd_observation: FixtureBOCPDObservation


@dataclass(frozen=True)
class FixtureBOCPDRegion:
    start_boundary_time_ms: int
    end_boundary_time_ms: int
    observed_through_boundary_time_ms: int


@dataclass(frozen=True)
class FixtureBOCPDResult:
    points: tuple[FixtureBOCPDPoint, ...]
    detection_regions_by_partition: dict
    summaries: dict


class CandidateEvidenceAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = parse_historical_market_state_study_manifest_json(
            MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.period = cls.manifest.selected_periods[0]

    def test_continuous_points_are_minute_only_and_events_keep_native_time(self):
        start = self.period.start_boundary_time_ms
        result = FixtureCUSUMResult((
            FixturePoint(start + 5_000, "CUSUM_UP", 1.25, False),
            FixturePoint(start + MINUTE, "CUSUM_UP", 1.5, False),
            FixturePoint(start + MINUTE + 5_000, "CUSUM_UP", 1.75, True),
        ), {"development": {"native_count": 3}})
        descriptor = SimpleNamespace(
            experiment_id="EXP-75-02", algorithm_version="cusum-v1",
            config_version="cusum-config-a")

        bundle = adapt_candidate_result(self.period, descriptor, result)

        self.assertEqual(tuple(item.evidence_kind for item in bundle.records),
                         ("CONTINUOUS", "EVENT"))
        self.assertEqual(bundle.records[0].decision_time_ms, start + MINUTE)
        self.assertEqual(bundle.records[1].decision_time_ms, start + MINUTE + 5_000)
        self.assertEqual(bundle.records[1].native_evidence["positive_accumulator"], 1.75)
        self.assertEqual(bundle.native_summaries, {"native_count": 3})
        self.assertEqual(len(bundle.result_sha256), 64)

    def test_bocpd_event_persists_exact_causal_onset_separately(self):
        start = self.period.start_boundary_time_ms
        onset = start + 5_000
        observation = FixtureBOCPDObservation(onset, "CHANGE", 0.75)
        region = FixtureBOCPDRegion(onset, onset + 10_000, onset + 20_000)
        result = FixtureBOCPDResult(
            (FixtureBOCPDPoint(onset, observation),),
            {"development": (region,)},
            {"development": {"region_count": 1}})
        descriptor = SimpleNamespace(
            experiment_id="EXP-75-04B", algorithm_version="bocpd-v1",
            config_version="bocpd-config-a")

        bundle = adapt_candidate_result(self.period, descriptor, result)

        self.assertEqual(len(bundle.records), 1)
        event = bundle.records[0]
        self.assertEqual(event.evidence_kind, "EVENT")
        self.assertEqual(event.decision_time_ms, onset)
        self.assertEqual(
            event.native_evidence["causal_onset_observation"]
            ["evaluation_boundary_time_ms"], onset)
        self.assertEqual(
            event.native_evidence["causal_onset_observation"]
            ["recent_change_probability"], 0.75)
        self.assertEqual(
            event.native_evidence["descriptive_detection_region"]
            ["end_boundary_time_ms"], onset + 10_000)

    def test_pelt_segments_remain_retrospective_without_decision_time(self):
        descriptor = SimpleNamespace(
            experiment_id="EXP-75-04A", algorithm_version="pelt-v1",
            config_version="pelt-config-a")
        result = FixturePELTResult(
            {"development": ({"start": 12, "end": 30, "mean_delta": 0.2},)},
            {"development": {"segment_count": 1}})

        bundle = adapt_candidate_result(self.period, descriptor, result)

        self.assertEqual(len(bundle.records), 1)
        self.assertEqual(bundle.records[0].evidence_kind, "RETROSPECTIVE")
        self.assertIsNone(bundle.records[0].decision_time_ms)
        self.assertEqual(bundle.records[0].native_evidence["segmentation_key"],
                         "development")


if __name__ == "__main__":
    unittest.main()
