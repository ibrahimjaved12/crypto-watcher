"""Exact artifact joins for #123 Part C; no runners, inference or raw data I/O."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import math
from pathlib import Path
from statistics import median

from . import historical_market_state_study_execution as part_b
from .historical_experiment_batch import EXPERIMENT_SUITE_V1
from .historical_market_state_study import ORDERED_SYMBOLS
from .historical_market_state_study_evaluation import CandidateConfigIdentity
from .historical_market_state_study_features import (
    FAMILY_EVALUATION_SPECS, AlignedStudyDay, AlignedStudyObservation,
    FinalizedPeriodProvenance, UpstreamInputProvenance, predictive_family,
    absolute_market_return, future_breadth_extremity, persistence_weakening_binary,
)


def _identity(record):
    if not isinstance(record, dict):
        raise ValueError('malformed candidate identity record')
    return tuple(record.get(k) for k in ('experiment_id', 'algorithm_version', 'config_version'))


ORIGINAL_IDENTITIES = tuple((d.experiment_id, d.algorithm_version, d.config_version)
                            for d in EXPERIMENT_SUITE_V1)
ALL_CANDIDATE_IDENTITIES = ORIGINAL_IDENTITIES + tuple(
    ('EXP-75-06B', part_b.ATR_ALGORITHM_VERSION, c.version) for c in part_b.ATR_CONFIGURATIONS
) + (
    ('EXP-75-10', part_b.MARK_TRADE_ALGORITHM_VERSION, part_b.MARK_TRADE_CONFIG_VERSION),
    ('EXP-75-11-OI', part_b.OI_ALGORITHM_VERSION, part_b.OI_CONFIG_VERSION),
    ('EXP-75-11-FUNDING', part_b.FUNDING_ALGORITHM_VERSION, part_b.FUNDING_CONFIG_VERSION),
    ('EXP-75-11-LIQUIDATION', part_b.LIQ_ALGORITHM_VERSION, part_b.LIQ_CONFIG_VERSION),
    ('EXP-75-12', part_b.TAKER_FLOW_ALGORITHM_VERSION, part_b.TAKER_FLOW_CONFIG_VERSION),
)
PREDICTIVE_CONFIGS = tuple(CandidateConfigIdentity(*i) for i in ALL_CANDIDATE_IDENTITIES
                          if i[0] != 'EXP-75-04A')
if (len(set(ALL_CANDIDATE_IDENTITIES)) != len(ALL_CANDIDATE_IDENTITIES)
        or Counter(i.family_id for i in PREDICTIVE_CONFIGS)
        != Counter({s.family_id: s.expected_config_count for s in FAMILY_EVALUATION_SPECS
                    if s.causal_forward_test})):
    raise ValueError('implementation config registry differs from frozen Part-C family registry')


def validate_candidate_registry(report):
    if tuple(_identity(r) for r in report.get('experiment_suite_identity', ())) != ORIGINAL_IDENTITIES:
        raise ValueError('Part-B original fixed config registry mismatch')
    summaries = tuple(_identity(r) for r in report.get('native_state_quality_summaries', ()))
    if len(summaries) != len(ALL_CANDIDATE_IDENTITIES) or set(summaries) != set(ALL_CANDIDATE_IDENTITIES):
        raise ValueError('missing, duplicate or extra fixed config summary')
    seen = set()
    for record in report['candidate_evidence']:
        identity = _identity(record)
        if identity[0] != 'V1' and identity not in ALL_CANDIDATE_IDENTITIES:
            raise ValueError('candidate evidence has an unfrozen config identity')
        if identity[0] == 'EXP-75-04A' and record['evidence_kind'] != 'RETROSPECTIVE':
            raise ValueError('PELT evidence must remain retrospective')
        native = record.get('native_evidence')
        if isinstance(native, dict):
            for name, expected in (('candidate_algorithm_version', identity[1]),
                                   ('candidate_config_version', identity[2]),
                                   ('evaluation_boundary_time_ms', record['decision_time_ms'])):
                if name in native and native[name] != expected:
                    raise ValueError('candidate native identity differs from its evidence wrapper')
        key = identity + (record['evidence_kind'], record['decision_time_ms'])
        # PELT can have multiple independently keyed retrospective segments.
        if identity[0] != 'EXP-75-04A' and key in seen:
            raise ValueError('duplicate candidate evidence boundary')
        seen.add(key)


def period_provenance(report, cross_fit_sha, final_model_sha):
    p = report['period']
    return FinalizedPeriodProvenance(
        p['study_period_index'], date.fromisoformat(p['utc_date']), p['phase'],
        report['study_manifest_sha256'], report['extension_coverage_manifest_sha256'],
        report['period_report_schema_version'], report['code_revision'], report['report_sha256'],
        report['event_time_v1_context_version'], report['event_time_v1_context_sha256'],
        report['bocpd_onset_evidence_version'], report['bocpd_onset_evidence_sha256'],
        cross_fit_sha, final_model_sha, study_version=report['study_version'])


def load_phase_reports(manifest, coverage, output_dir, phase, revision, cross_fit_sha, final_model_sha):
    """Open exact filenames only. Caller must gate test before invoking this function."""
    reports, sources = [], []
    for period in manifest.selected_periods:
        if period.phase != phase:
            continue
        report = part_b.load_finalized_period_report(
            Path(output_dir) / part_b.PERIOD_DIRECTORY / part_b._period_filename(period),
            manifest, period, coverage_sha256=coverage['coverage_manifest_sha256'], code_revision=revision)
        validate_candidate_registry(report)
        if phase != 'development' and report.get('hmm_model_sha256') != final_model_sha:
            raise ValueError('period was not generated with the frozen final HMM')
        reports.append(report)
        sources.append(period_provenance(report, cross_fit_sha, final_model_sha))
    upstream = UpstreamInputProvenance(phase, tuple(sources), cross_fit_sha, final_model_sha)
    return tuple(reports), upstream


class UnavailableObservation(ValueError):
    """Persisted evidence cannot supply a paired primary observation."""


def number(value):
    if isinstance(value, dict):
        if value.get('available') is not True:
            raise UnavailableObservation('metric unavailable')
        value = value.get('value')
    if value is None or isinstance(value, bool):
        raise UnavailableObservation('numeric evidence unavailable')
    try:
        result = float(Decimal(value) if isinstance(value, str) else value)
    except (TypeError, ValueError, InvalidOperation, OverflowError) as exc:
        raise UnavailableObservation('numeric evidence invalid') from exc
    if not math.isfinite(result):
        raise UnavailableObservation('numeric evidence nonfinite')
    return result


def window(value, minutes):
    windows = value.get('windows') if isinstance(value, dict) else None
    if not isinstance(windows, (list, tuple)):
        raise UnavailableObservation('window unavailable')
    # Integer-keyed classification maps use the lossless study encoder.
    matches = [entry[1] for entry in windows if isinstance(entry, (list, tuple))
               and len(entry) == 2 and type(entry[0]) is int and entry[0] == minutes]
    matches += [entry for entry in windows if isinstance(entry, dict)
                and entry.get('window_minutes') == minutes]
    if len(matches) > 1:
        raise ValueError('duplicate exact window')
    if not matches:
        raise UnavailableObservation('exact window unavailable')
    return matches[0]


def direction(w):
    value = w.get('direction_state')
    if value not in ('BROAD_RISE', 'BROAD_DROP', 'NEUTRAL'):
        raise UnavailableObservation('V1/candidate direction unavailable')
    return value


def baseline_features(classification, timestamp):
    w = window(classification, 5)
    state = direction(w)
    pace = w.get('pace', {})
    if not isinstance(pace, dict):
        raise UnavailableObservation('V1 pace unavailable')
    if state == 'NEUTRAL':
        if (pace.get('available') is not False or 'value' not in pace or pace['value'] is not None
                or pace.get('reason') != 'NO_BROAD_DIRECTION'):
            raise UnavailableObservation('inconsistent NEUTRAL V1 pace')
    elif pace.get('available') is not True or pace.get('value') not in (
            'ACCELERATING', 'DECELERATING', 'MIXED'):
        raise UnavailableObservation('V1 pace unavailable')
    breadth = w.get('breadth', {})
    fractions = []
    for side in ('rising', 'falling', 'material_rising', 'material_falling'):
        metric = breadth.get(side, {})
        if metric.get('available') is not True:
            raise UnavailableObservation('V1 breadth unavailable')
        fractions.append(number(metric.get('value', {}).get('fraction')))
    rvol = []
    for row in w.get('volume_context', ()):
        if row.get('symbol') in ORDERED_SYMBOLS:
            try:
                rvol.append(number(row.get('rvol')))
            except UnavailableObservation:
                pass
    if not rvol:
        raise UnavailableObservation('no available configured-symbol RVOL')
    bucket = timestamp // 21_600_000 % 4
    return (float(state == 'BROAD_RISE'), float(state == 'BROAD_DROP'),
            number(w.get('median_normalized_movement')), *fractions,
            number(w.get('dispersion_mad_normalized_movement')), median(rvol),
            number(w.get('median_acceleration')), float(pace['value'] == 'ACCELERATING'),
            float(pace['value'] == 'DECELERATING'), *(float(bucket == b) for b in (1, 2, 3)))


def candidate_features(identity, native, v1, *, onset=None, model_sha=None):
    family = identity.family_id
    category = None
    if family == 'EXP-75-01':
        values = (number(native.get('ewma_primary_median_normalized_movement'))
                  - number(native.get('raw_primary_median_normalized_movement')),
                  float(direction(window(native.get('candidate_classification'), 5)) != direction(window(v1, 5))))
    elif family == 'EXP-75-02':
        category = native.get('cusum_direction_state')
        values = (number(native.get('positive_accumulator')), number(native.get('negative_accumulator')),
                  float(category == 'DOWN_SHIFT'))
    elif family == 'EXP-75-03':
        values = (number(native.get('kalman_filtered_primary_median_normalized_movement'))
                  - number(native.get('raw_primary_median_normalized_movement')), number(native.get('kalman_trend')))
    elif family == 'EXP-75-04B':
        if onset is None or onset.get('available') is not True:
            raise UnavailableObservation('causal BOCPD onset unavailable')
        category = onset.get('detector_state')
        values = (number(onset.get('recent_change_probability')),)
    elif family in ('EXP-75-05', 'EXP-75-06A', 'EXP-75-06B'):
        c, b = window(native.get('candidate_classification'), 5), window(v1, 5)
        field = 'median_acceleration' if family == 'EXP-75-05' else 'median_normalized_movement'
        values = (number(c.get(field)) - number(b.get(field)),)
        if family != 'EXP-75-05':
            values += (float(direction(c) != direction(b)),)
    elif family in ('EXP-75-07', 'EXP-75-08'):
        key, ready, fields = (('pca_evidence', 'PCA_READY', ('explained_variance_ratio', 'current_pc1_energy_fraction'))
                              if family == 'EXP-75-07' else ('correlation_evidence', 'CORRELATION_READY',
                                                           ('median_pairwise_correlation', 'network_edge_density')))
        evidence = native.get(key, {})
        if evidence.get('status') != ready:
            raise UnavailableObservation('coordination evidence not ready')
        values = tuple(number(evidence.get(f)) for f in fields)
    elif family == 'EXP-75-09':
        if native.get('model_sha256') != model_sha:
            raise ValueError('HMM evidence model identity mismatch')
        if native.get('status') != 'HMM_READY':
            raise UnavailableObservation('HMM evidence unavailable')
        category = native.get('hard_state')
        if category not in ('LOW_MOVEMENT', 'MID_MOVEMENT', 'HIGH_MOVEMENT'):
            raise UnavailableObservation('HMM category unavailable')
        values = (float(category == 'LOW_MOVEMENT'), float(category == 'HIGH_MOVEMENT'), number(native.get('posterior_entropy')))
    elif family == 'EXP-75-10':
        w = window(native, 5)
        rows = w.get('symbols', ())
        if (w.get('all_configured_symbols_ready') is not True
                or tuple(r.get('symbol') for r in rows) != ORDERED_SYMBOLS
                or any(r.get('status') != 'READY' for r in rows)):
            raise UnavailableObservation('mark/trade primary symbols not ready')
        values = (median(number(r.get('divergence')) for r in rows),)
    elif family == 'EXP-75-11-OI':
        values = tuple(number(window(native, m).get('market_summary', {}).get('median_delta_oi')) for m in (5, 15))
    elif family == 'EXP-75-11-FUNDING':
        values = (number(window(native, 60).get('market_summary', {}).get('median_funding_per_hour')),)
    elif family == 'EXP-75-11-LIQUIDATION':
        s = window(native, 15).get('market_summary', {})
        if number(s.get('covered_count')) <= 0:
            raise UnavailableObservation('liquidation source unavailable')
        total, long, short = (number(s.get(f'pooled_observed_{side}_notional')) for side in ('total', 'long', 'short'))
        if min(total, long, short) < 0:
            raise ValueError('negative liquidation notional')
        if total == 0 and (long != 0 or short != 0 or s.get('active_count', 0) != 0):
            raise ValueError('zero liquidation total is not a covered quiet interval')
        values = (math.log1p(total), (long - short) / total if total > 0 else 0.0,
                  number(s.get('liquidation_breadth')))
    elif family == 'EXP-75-12':
        s = window(native, 5).get('market_summary', {})
        denominator = number(s.get('sign_breadth_denominator'))
        if denominator <= 0:
            raise UnavailableObservation('no active taker observations')
        values = (number(s.get('pooled_notional_imbalance')),
                  (number(s.get('buy_sign_count')) - number(s.get('sell_sign_count'))) / denominator)
    else:
        raise ValueError('family has no predictive adapter')
    return tuple(values), category


def extract_outcome(outcome_id, outcome, path):
    if outcome_id in ('state_persistence', 'v1_direction_persistence', 'persistence_weakening'):
        if path is None or path.get('status') != 'AVAILABLE':
            raise UnavailableObservation('state path unavailable/censored')
        if outcome_id == 'persistence_weakening':
            try:
                return persistence_weakening_binary(path.get(outcome_id))
            except ValueError as exc:
                raise UnavailableObservation('weakening label unavailable') from exc
        value = path.get(outcome_id)
        if outcome_id == 'v1_direction_persistence' and path.get('initial_v1_direction_state') not in ('BROAD_RISE', 'BROAD_DROP'):
            raise UnavailableObservation('initial V1 direction is neutral')
        if type(value) is not bool:
            raise UnavailableObservation('persistence label unavailable')
        return float(value)
    if outcome_id == 'future_breadth_extremity':
        returns = tuple(number(r.get('forward_log_return')) for r in outcome.get('per_symbol', ()) if r.get('status') == 'AVAILABLE')
        if not returns:
            raise UnavailableObservation('eligible symbol labels unavailable')
        return future_breadth_extremity(returns)
    field = {'signed_market_return': 'market_forward_return', 'absolute_market_return': 'market_forward_return',
             'realized_volatility': 'market_forward_realized_volatility', 'forward_dispersion': 'forward_cross_sectional_dispersion'}[outcome_id]
    value = number(outcome.get(field))
    return absolute_market_return(value) if outcome_id == 'absolute_market_return' else value


def unique_index(rows, key):
    result = {}
    for row in rows:
        k = key(row)
        if k in result:
            raise ValueError('duplicate exact join identity')
        result[k] = row
    return result


@dataclass(frozen=True)
class AdaptedDay:
    day: AlignedStudyDay
    contexts: tuple[tuple[str, str, int, str | None], ...]
    exclusions: tuple[tuple[str, int], ...]


def adapt_period(report, source, identity, *, held_out_fold=None):
    """All indexes use exact persisted identities; missing joins exclude rows."""
    if identity not in PREDICTIVE_CONFIGS:
        raise ValueError('unfrozen predictive config')
    if source.period_report_sha256 != part_b._verify_hashed_payload(report, 'report_sha256', 'period report'):
        raise ValueError('adapter source report SHA mismatch')
    if period_provenance(report, source.hmm_cross_fit_sha256, source.hmm_final_model_sha256) != source:
        raise ValueError('adapter report period/scientific provenance mismatch')
    spec = predictive_family(identity.family_id)
    key = (identity.family_id, identity.algorithm_version, identity.config_version)
    records = [r for r in report['candidate_evidence'] if _identity(r) == key and r['evidence_kind'] == spec.observation_mode]
    model_sha = source.hmm_final_model_sha256
    if identity.family_id == 'EXP-75-09' and source.phase == 'development':
        if held_out_fold is None or held_out_fold['held_out_period']['period_report_sha256'] != source.period_report_sha256:
            raise ValueError('development HMM requires its validated held-out fold')
        part_b._verify_hashed_payload(held_out_fold, 'artifact_sha256', 'held-out HMM fold')
        if (held_out_fold['held_out_period']['study_period_index'] != source.study_period_index
                or held_out_fold['held_out_period']['utc_date'] != source.utc_date.isoformat()):
            raise ValueError('held-out HMM period identity mismatch')
        model_sha = held_out_fold['fold_model_sha256']
        records = [{'decision_time_ms': r['evaluation_boundary_time_ms'], 'native_evidence': r}
                   for block in held_out_fold['held_out_evidence'] for r in block]
    candidate = unique_index(records, lambda r: r['decision_time_ms'])
    if spec.observation_mode == 'CONTINUOUS':
        v1 = unique_index((r for r in report['candidate_evidence'] if r['experiment_id'] == 'V1'
                           and r['evidence_kind'] == 'CONTINUOUS'), lambda r: r['decision_time_ms'])
        outcomes = unique_index((r for h, rows in report['candidate_independent_continuous_outcomes']
                                 if h == spec.horizon_minutes for r in rows), lambda r: r['decision_time_ms'])
        paths = unique_index((r for r in report['secondary_v1_continuous_state_paths']
                              if r['experiment_id'] == 'CONTINUOUS_GRID' and r['horizon_minutes'] == spec.horizon_minutes
                              and r.get('algorithm_version') == part_b.FORWARD_OUTCOMES_VERSION
                              and r.get('config_version') == f'{spec.horizon_minutes}m'),
                             lambda r: r['decision_time_ms'])
    else:
        v1 = unique_index(report['event_time_v1_context'], lambda r: r['decision_time_ms'])
        outcomes = unique_index((r for group in report['candidate_event_outcomes'] if _identity(group) == key
                                 for h, rows in group['outcomes_by_horizon'] if h == spec.horizon_minutes
                                 for r in rows), lambda r: r['decision_time_ms'])
        paths = unique_index((r for r in report['secondary_v1_event_state_paths'] if _identity(r) == key
                              and r['horizon_minutes'] == spec.horizon_minutes), lambda r: r['decision_time_ms'])
    onsets = unique_index((r for r in report['bocpd_onset_evidence'] if _identity(r) == key), lambda r: r['decision_time_ms'])
    observations, contexts, excluded = [], [], Counter()
    for timestamp, record in sorted(candidate.items()):
        if spec.observation_mode == 'CONTINUOUS' and timestamp % (spec.horizon_minutes * 60_000):
            continue  # Candidate minute evidence is denser than the primary grid.
        try:
            context, persisted = v1.get(timestamp), outcomes.get(timestamp)
            if context is None or persisted is None:
                raise UnavailableObservation('missing exact V1/outcome join')
            if persisted.get('horizon_minutes') != spec.horizon_minutes or persisted.get('decision_time_ms') != timestamp:
                raise ValueError('outcome exact timestamp/horizon mismatch')
            if spec.observation_mode == 'EVENT':
                if persisted.get('confirmatory_independent') is not True:
                    raise UnavailableObservation('event not confirmatory independent')
                classification = context['classification']
                outcome = persisted['outcome']
                if outcome.get('decision_time_ms') != timestamp or outcome.get('horizon_minutes') != spec.horizon_minutes:
                    raise ValueError('nested event outcome identity mismatch')
            else:
                classification = context['native_evidence']['classification']
                outcome = persisted
            baseline = baseline_features(classification, timestamp)
            features, category = candidate_features(identity, record['native_evidence'], classification,
                onset=onsets.get(timestamp, {}).get('onset_observation'), model_sha=model_sha)
            label = extract_outcome(spec.outcome_id, outcome, paths.get(timestamp, {}).get('state_path'))
            observation_key = f'{identity.family_id}:{timestamp}:{spec.horizon_minutes}:{spec.outcome_id}'
            observations.append(AlignedStudyObservation(source.study_period_index, source.utc_date, source.phase,
                *key, spec.observation_mode, timestamp, spec.horizon_minutes, spec.outcome_id, spec.outcome_kind,
                observation_key, baseline, features, label))
            contexts.append((observation_key, direction(window(classification, 5)), timestamp // 21_600_000 % 4, category))
        except UnavailableObservation as exc:
            excluded[str(exc)] += 1
    day = AlignedStudyDay(source.study_period_index, source.utc_date, source.phase, *key,
        spec.observation_mode, spec.horizon_minutes, spec.outcome_id, spec.outcome_kind,
        1440 // spec.horizon_minutes if spec.observation_mode == 'CONTINUOUS' else None,
        tuple(observations), source)
    return AdaptedDay(day, tuple(contexts), tuple(sorted(excluded.items())))
