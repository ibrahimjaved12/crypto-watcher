"""Compact lossless adapters from existing candidate results to study evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
import hashlib
import json
from typing import Any, Iterable

from .historical_market_state_study_json import study_json_safe as report_json_safe
from .experiments.market_state_common import validate_experiment_points


CANDIDATE_EVIDENCE_VERSION = "historical-market-state-candidate-evidence-v1"
EVIDENCE_KINDS = frozenset(("CONTINUOUS", "EVENT", "RETROSPECTIVE"))
CONTINUOUS_MINUTE_MS = 60_000


def _canonical(value):
    return json.dumps(report_json_safe(value), sort_keys=True,
                      separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _sha(value):
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _timestamp(item):
    value = getattr(item, "evaluation_boundary_time_ms", None)
    return value if type(value) is int else None


def _native_status(item):
    for name in ("status", "native_status", "detector_state", "cusum_direction_state"):
        value = getattr(item, name, None)
        if isinstance(value, str) and value:
            return value
    observation = getattr(item, "bocpd_observation", None)
    value = getattr(observation, "detector_state", None)
    return value if isinstance(value, str) and value else "NATIVE_RESULT"


def _native_point(item):
    excluded = frozenset(("movement_evaluation", "baseline_movement_evaluation",
                          "candidate_movement_evaluation", "source_time_evidence",
                          "baseline_classification", "baseline_lifecycle_state",
                          "baseline_transitions"))
    if is_dataclass(item):
        retained = {f.name: getattr(item, f.name) for f in fields(item)
                    if f.name not in excluded}
    elif isinstance(item, Mapping):
        retained = {key: value for key, value in item.items() if key not in excluded}
    else:
        return report_json_safe(item)
    return report_json_safe(retained)


@dataclass(frozen=True)
class HistoricalStudyCandidateEvidence:
    study_period_index: int
    utc_date: str
    phase: str
    experiment_id: str
    algorithm_version: str
    config_version: str
    decision_time_ms: int | None
    evidence_kind: str
    native_status: str
    native_evidence: Any
    candidate_evidence_sha256: str = field(init=False)

    def __post_init__(self):
        if (type(self.study_period_index) is not int or self.study_period_index < 0
                or not isinstance(self.utc_date, str) or not self.utc_date
                or self.phase not in ("development", "validation", "test")
                or not all(isinstance(item, str) and item for item in (
                    self.experiment_id, self.algorithm_version, self.config_version,
                    self.native_status))
                or self.evidence_kind not in EVIDENCE_KINDS
                or (self.decision_time_ms is not None
                    and (type(self.decision_time_ms) is not int
                         or self.decision_time_ms < 0
                         or self.decision_time_ms % 5_000))
                or (self.evidence_kind == "RETROSPECTIVE"
                    and self.decision_time_ms is not None)):
            raise ValueError("invalid common candidate-evidence identity")
        object.__setattr__(self, "candidate_evidence_sha256", _sha({
            key: getattr(self, key) for key in (
                "study_period_index", "utc_date", "phase", "experiment_id",
                "algorithm_version", "config_version", "decision_time_ms",
                "evidence_kind", "native_status", "native_evidence")
        }))


@dataclass(frozen=True)
class CandidateEvidenceBundle:
    version: str
    records: tuple[HistoricalStudyCandidateEvidence, ...]
    native_summaries: Any
    result_sha256: str = field(init=False)

    def __post_init__(self):
        records = tuple(self.records)
        if (self.version != CANDIDATE_EVIDENCE_VERSION
                or any(not isinstance(item, HistoricalStudyCandidateEvidence)
                       for item in records)):
            raise ValueError("invalid candidate evidence bundle")
        object.__setattr__(self, "records", records)
        object.__setattr__(self, "result_sha256", _sha({
            "version": self.version, "records": records,
            "native_summaries": self.native_summaries,
        }))


def _record(period, descriptor, timestamp, kind, status, native):
    return HistoricalStudyCandidateEvidence(
        period.study_period_index, period.utc_date.isoformat(), period.phase,
        descriptor.experiment_id, descriptor.algorithm_version,
        descriptor.config_version, timestamp, kind, status, native)


def adapt_candidate_result(period, descriptor, result) -> CandidateEvidenceBundle:
    """Preserve native outputs; only persist continuous points on UTC minutes.

    CUSUM and BOCPD event starts are copied from detector outputs, while PELT
    segmentations are explicitly retrospective and cannot acquire labels.
    No detector statistic or candidate formula is recomputed here.
    """
    if not all(hasattr(period, item) for item in (
            "study_period_index", "utc_date", "phase")):
        raise ValueError("candidate adapter requires a frozen study period")
    if not all(hasattr(descriptor, item) for item in (
            "experiment_id", "algorithm_version", "config_version")):
        raise ValueError("candidate adapter requires a fixed suite descriptor")
    family = descriptor.experiment_id
    records = []

    if family == "EXP-75-04A":
        segmentations = getattr(result, "segmentations", None)
        if segmentations is None:
            raise ValueError("PELT output must provide retrospective segmentations")
        for key, segmentation in sorted(segmentations.items()):
            records.append(_record(
                period, descriptor, None, "RETROSPECTIVE", "RETROSPECTIVE_ONLY",
                {"segmentation_key": key, "native_segmentation": report_json_safe(segmentation)}))
        native_summaries = report_json_safe(getattr(result, "summaries", {}))
        return CandidateEvidenceBundle(CANDIDATE_EVIDENCE_VERSION,
                                       tuple(records), native_summaries)

    points = (getattr(result, "points", None)
              or getattr(result, "paired_points", None)
              or getattr(result, "baseline_points", None))
    if points is None:
        raise ValueError(f"{family} result has no native point collection")
    for point in points:
        timestamp = _timestamp(point)
        if timestamp is None:
            raise ValueError(f"{family} point lacks a decision timestamp")
        if (timestamp < period.start_boundary_time_ms
                or timestamp >= period.end_boundary_time_ms):
            continue
        native = (_native_point(point) if timestamp % CONTINUOUS_MINUTE_MS == 0
                  or (family == "EXP-75-02" and getattr(point, "cusum_directional_onset", False))
                  else None)
        if timestamp % CONTINUOUS_MINUTE_MS == 0:
            records.append(_record(period, descriptor, timestamp, "CONTINUOUS",
                                   _native_status(point), native))
        if family == "EXP-75-02" and getattr(point, "cusum_directional_onset", False):
            records.append(_record(period, descriptor, timestamp, "EVENT",
                                   _native_status(point), native))

    if family == "EXP-75-04B":
        regions = getattr(result, "detection_regions_by_partition", {})
        point_by_boundary = {
            _timestamp(point): point for point in points
            if _timestamp(point) is not None
        }
        if len(point_by_boundary) != len(points):
            raise ValueError("BOCPD onset association requires unique exact point boundaries")
        for region in regions.get(period.phase, ()):
            onset = getattr(region, "start_boundary_time_ms", None)
            if type(onset) is not int:
                raise ValueError("BOCPD region lacks its native onset boundary")
            if not period.start_boundary_time_ms <= onset < period.end_boundary_time_ms:
                continue
            onset_point = point_by_boundary.get(onset)
            onset_observation = getattr(onset_point, "bocpd_observation", None)
            if (onset_point is None or onset_observation is None
                    or getattr(onset_observation, "evaluation_boundary_time_ms", None) != onset
                    or getattr(onset_observation, "candidate_algorithm_version", None)
                    != descriptor.algorithm_version
                    or getattr(onset_observation, "candidate_config_version", None)
                    != descriptor.config_version):
                raise ValueError("BOCPD region onset lacks its exact causal observation")
            native = {
                "causal_onset_observation": report_json_safe(onset_observation),
                "descriptive_detection_region": report_json_safe(region),
            }
            records.append(_record(period, descriptor, onset, "EVENT",
                                   "DETECTION_REGION", native))

    summaries = getattr(result, "summaries", {})
    native_summaries = report_json_safe(summaries.get(period.phase, {}))
    return CandidateEvidenceBundle(CANDIDATE_EVIDENCE_VERSION,
                                   tuple(records), native_summaries)


def adapt_v1_evidence(period, decision_time_ms: int, classification,
                      lifecycle, transitions: Iterable) -> HistoricalStudyCandidateEvidence:
    """Persist existing canonical classifier/lifecycle values without recalculation."""
    native = {
        "classification": report_json_safe(classification),
        "lifecycle_state": report_json_safe(lifecycle),
        "transitions": report_json_safe(tuple(transitions)),
    }
    status = "V1_CLASSIFIED" if classification is not None else "V1_UNAVAILABLE"
    record = HistoricalStudyCandidateEvidence(
        period.study_period_index, period.utc_date.isoformat(), period.phase,
        "V1", getattr(classification, "algorithm_version", "market-state-classifier-v1"),
        getattr(getattr(classification, "config", None), "version", "market-state-classifier-config-v1"),
        decision_time_ms, "CONTINUOUS", status, native)
    return record


def validate_study_phase_prepared(prepared) -> None:
    """Validate extension inputs against the shared one-replay study stream."""
    archive = getattr(prepared, "archive_dataset", None)
    replay = getattr(prepared, "replay_result", None)
    provided = getattr(prepared, "experiment_points", ())
    points = tuple(provided) if provided is not None else None
    phase = getattr(prepared, "study_phase", None)
    if (archive is None or replay is None
            or phase not in ("development", "validation", "test")
            or (points is not None and (not points or len(points) != len(replay.points)))):
        raise ValueError("invalid prepared uniform-phase study extension")
    if points is not None:
        validate_experiment_points(points)
    if (archive.archive_manifest.content_sha256 != replay.manifest.dataset_content_sha256
            or archive.archive_manifest.dataset_id != replay.manifest.dataset_id
            or archive.archive_manifest.dataset_version != replay.manifest.dataset_version):
        raise ValueError("study extension does not match the verified core dataset")
    symbols = replay.manifest.configured_universe
    from .historical_study_inputs import HistoricalStudyArchiveInputs
    if isinstance(archive, HistoricalStudyArchiveInputs):
        manifest = replay.manifest
        from .movement_metrics import MarketMovementConfig
        if (archive.config.movement_config != MarketMovementConfig()
                or archive.universe.symbols != symbols
                or (archive.universe.id, archive.universe.version)
                != (manifest.universe_id, manifest.universe_version)
                or (archive.config.output_start_boundary_time_ms,
                    archive.config.output_end_boundary_time_ms)
                != (manifest.output_start_boundary_time_ms, manifest.output_end_boundary_time_ms)
                or archive.config.movement_config.version != manifest.movement_config_version):
            raise ValueError("study archive configuration differs from canonical replay")
        if getattr(prepared, "mark_evidence", None) is not None and archive.ohlc_evidence is None:
            raise ValueError("mark/trade study requires verified OHLC evidence")
    for name in ("mark_evidence", "oi_evidence", "funding_evidence",
                 "liquidation_evidence"):
        evidence = getattr(prepared, name, None)
        if evidence is None:
            continue
        if (getattr(evidence, "configured_symbols", None) != symbols
                or (getattr(evidence, "requested_start_boundary_time_ms", None),
                    getattr(evidence, "requested_end_boundary_time_ms", None))
                != (replay.manifest.output_start_boundary_time_ms,
                    replay.manifest.output_end_boundary_time_ms)):
            raise ValueError("supplementary source evidence differs from the frozen period")
    if points is None:
        from .historical_study_runtime import StudyPointStream
        if (not isinstance(replay.points, StudyPointStream)
                or replay.points.identity["run_fingerprint"] != replay.manifest.run_fingerprint
                or replay.points.identity["phase"] != phase):
            raise ValueError("streamed extension requires identity-bound study points")
        return
    from .historical_replay import canonical_replay_point_id
    for replay_point, point in zip(replay.points, points):
        if (replay_point.point_id != canonical_replay_point_id(
                    replay.manifest.run_fingerprint, replay_point.evaluation_boundary_time_ms)
                or point.movement_evaluation is not replay_point.movement_evaluation
                or point.source_time_evidence != replay_point.source_time_evidence
                or point.partition != phase):
            raise ValueError("study extension requires the exact uniform-phase replay stream")
