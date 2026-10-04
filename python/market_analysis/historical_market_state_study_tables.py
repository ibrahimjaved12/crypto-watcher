"""Per-period Part-C analysis tables derived once from fully validated period reports.

A table holds exactly what Part C and the HMM tools read from one period
report: the report's provenance header, its Track-A summaries, the adapted
rows of the requested configs, and (development only) the HMM view and the
report side of the EXP-75-09 join. Each table is derived from a report that
was decoded and fully validated once, and is bound to it by ``report_sha256``
and ``report_file_sha256``. Consumers produce byte-identical artifacts.

Pure library, no CLI. Reports stay untouched as the archive.
"""
from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from . import historical_market_state_study_execution as part_b
from . import historical_market_state_study_adapter as adapter
from .historical_market_state_study_artifacts import TrackAReference, decode, encode
from .historical_market_state_study_features import UpstreamInputProvenance

TABLE_VERSION = 'historical-market-state-analysis-table-v1'
HEADER_KEYS = ('period', 'study_manifest_sha256', 'extension_coverage_manifest_sha256',
               'period_report_schema_version', 'code_revision', 'report_sha256',
               'event_time_v1_context_version', 'event_time_v1_context_sha256',
               'bocpd_onset_evidence_version', 'bocpd_onset_evidence_sha256', 'study_version',
               'hmm_model_sha256')
TABLE_KEYS = frozenset(('table_version', 'report_header', 'report_file_sha256', 'hmm_cross_fit_sha256',
                        'hmm_final_model_sha256', 'native_state_quality_summaries', 'hmm_view',
                        'development_hmm_join', 'adapted', 'table_sha256'))
# The canonical replay keys part_b._validated_period_hmm_block compares with the HMM block scope.
HMM_SCOPE_KEYS = ('movement_algorithm_version', 'movement_config_version', 'universe_id',
                  'universe_version', 'configured_universe', 'provider', 'exchange', 'price_type')
HMM_FAMILY = 'EXP-75-09'
_JOIN_RECORD_KEYS = ('experiment_id', 'algorithm_version', 'config_version', 'evidence_kind', 'decision_time_ms')


def table_path(table_dir, period):
    return Path(table_dir) / part_b._period_filename(period)


def hmm_view_from_report(report, period):
    """Exactly the report fields the development HMM tools read.

    Consumed unchanged by part_b._validated_period_hmm_block and
    crossfit._v1_continuous_times, which re-validate it.
    """
    from .historical_market_state_hmm_crossfit import _v1_continuous_times
    times = _v1_continuous_times(report, period)  # invalid minute evidence fails at derive time
    replay = report['canonical_replay_manifest']
    return {
        'report_sha256': report['report_sha256'],
        'extension_coverage_manifest_sha256': report['extension_coverage_manifest_sha256'],
        'hmm_development_training_block': report.get('hmm_development_training_block'),
        'hmm_development_training_block_sha256': report.get('hmm_development_training_block_sha256'),
        # Present keys only: an absent key must stay absent for the scope's .get defaults.
        'canonical_replay_manifest': {key: replay[key] for key in HMM_SCOPE_KEYS if key in replay},
        'candidate_evidence': [{'experiment_id': 'V1', 'evidence_kind': 'CONTINUOUS', 'decision_time_ms': time}
                               for time in sorted(times)],
    }


def _is_five_minute_window(entry):
    # The exact predicates adapter.window uses for minutes == 5.
    return ((isinstance(entry, (list, tuple)) and len(entry) == 2 and type(entry[0]) is int and entry[0] == 5)
            or (isinstance(entry, dict) and entry.get('window_minutes') == 5))


def _slim_v1_native_evidence(native):
    """Only the V1 classification's 5-minute windows, which are all the join reads."""
    if isinstance(native, dict):
        classification = native.get('classification')
        if isinstance(classification, dict) and isinstance(classification.get('windows'), list):
            return {'classification': {**classification, 'windows': [
                entry for entry in classification['windows'] if _is_five_minute_window(entry)]}}
    return native


def _outcome(call):
    try:
        return ('value', call())
    except Exception as exc:  # noqa: BLE001 - compared exactly, then re-raised below if different
        return ('error', type(exc), str(exc))


def development_hmm_join(report):
    """Report side of the EXP-75-09 development join; the folds supply the candidate side later."""
    spec = adapter.predictive_family(HMM_FAMILY)
    if spec.observation_mode != 'CONTINUOUS':
        raise ValueError('development HMM join assumes a continuous primary grid')
    horizon = spec.horizon_minutes
    interval = horizon * 60_000
    evidence = []
    for record in report['candidate_evidence']:
        if (record['experiment_id'] != 'V1' or record['evidence_kind'] != 'CONTINUOUS'
                or record['decision_time_ms'] % interval):
            continue
        entry = {key: record[key] for key in _JOIN_RECORD_KEYS if key in record}
        if 'native_evidence' in record:
            full = record['native_evidence']
            slim = _slim_v1_native_evidence(full)
            if slim is not full:
                timestamp = record['decision_time_ms']
                for read in (lambda c: adapter.baseline_features(c, timestamp),
                             lambda c: adapter.direction(adapter.window(c, 5))):
                    if (_outcome(lambda: read(full['classification']))
                            != _outcome(lambda: read(slim['classification']))):
                        raise ValueError('slim V1 classification differs')
            entry['native_evidence'] = slim
        evidence.append(entry)
    return {
        'candidate_evidence': evidence,
        'candidate_independent_continuous_outcomes': [
            [h, rows] for h, rows in report['candidate_independent_continuous_outcomes'] if h == horizon],
        'secondary_v1_continuous_state_paths': [
            r for r in report['secondary_v1_continuous_state_paths']
            if r['experiment_id'] == 'CONTINUOUS_GRID' and r['horizon_minutes'] == horizon
            and r.get('algorithm_version') == part_b.FORWARD_OUTCOMES_VERSION
            and r.get('config_version') == f'{horizon}m'],
        # Normally empty; kept exact should a report ever carry EXP-75-09 onsets.
        'bocpd_onset_evidence': [r for r in report['bocpd_onset_evidence']
                                 if adapter._identity(r)[0] == HMM_FAMILY],
    }


def build_period_table(report, manifest, period, *, report_file_sha256, cross_fit_sha, final_model_sha, identities):
    """Sealed table for an already loaded, validated and registry-checked report."""
    development = period.phase == 'development'
    if development:
        if cross_fit_sha is not None or final_model_sha is not None:
            raise ValueError('development analysis tables bind no HMM prerequisite')
    else:
        if cross_fit_sha is None or final_model_sha is None:
            raise ValueError('non-development analysis tables require the HMM prerequisites')
        if report.get('hmm_model_sha256') != final_model_sha:
            raise ValueError('period was not generated with the frozen final HMM')
    hmm_view = hmm_view_from_report(report, period) if development else None
    join = development_hmm_join(report) if development else None
    source = adapter.period_provenance(report, cross_fit_sha, final_model_sha)
    index = adapter.index_report(report)
    adapted = []
    for identity in dict.fromkeys(identities):  # first-occurrence order, as adapt_phase
        if development and identity.family_id == HMM_FAMILY:
            continue  # derived at consume time from development_hmm_join and its fold
        item = adapter.adapt_period(report, source, identity, report_verified=True, index=index)
        adapted.append({'identity': encode(identity), 'day': encode(replace(item.day, source_provenance=None)),
                        'contexts': encode(item.contexts), 'exclusions': encode(item.exclusions)})
    body = {
        'table_version': TABLE_VERSION,
        'report_header': {key: report.get(key) for key in HEADER_KEYS},
        'report_file_sha256': report_file_sha256,
        'hmm_cross_fit_sha256': cross_fit_sha,
        'hmm_final_model_sha256': final_model_sha,
        'native_state_quality_summaries': report['native_state_quality_summaries'],
        'hmm_view': hmm_view,
        'development_hmm_join': join,
        'adapted': adapted,
    }
    return json.loads(part_b._artifact_json(body, 'table_sha256'))


def _load_validated_report(path, manifest, coverage, period, code_revision):
    report = part_b.load_finalized_period_report(
        path, manifest, period, coverage_sha256=coverage['coverage_manifest_sha256'], code_revision=code_revision)
    adapter.validate_candidate_registry(report)
    file_sha = part_b._period_sidecar(Path(path), manifest, coverage, period, code_revision,
                                      report['report_sha256'])['report_file_sha256']
    return report, file_sha


def derive_period_table(path, manifest, coverage, period, code_revision, *,
                        cross_fit_sha=None, final_model_sha=None, identities):
    report, file_sha = _load_validated_report(path, manifest, coverage, period, code_revision)
    table = build_period_table(report, manifest, period, report_file_sha256=file_sha,
                               cross_fit_sha=cross_fit_sha, final_model_sha=final_model_sha, identities=identities)
    report = None
    part_b._log_peak_memory(f'derive period {period.study_period_index}')
    return table


def _table_bytes(table):
    return (part_b._artifact_json(table, 'table_sha256') + '\n').encode('utf-8')


def write_period_table(path, table):
    """Create-only; an existing table must be byte-identical."""
    path = Path(path)
    if path.exists():
        if path.read_bytes() != _table_bytes(table):
            raise part_b.StudyArtifactConflictError(f'a different analysis table already exists: {path.name}')
        return path
    part_b._write_atomic_new(path, part_b._artifact_json(table, 'table_sha256'))
    return path


def load_period_table(path, manifest, period, *, code_revision, coverage_sha256=None):
    table = part_b._read_json(Path(path))
    part_b._verify_hashed_payload(table, 'table_sha256', 'analysis table')
    header = table.get('report_header')
    if (set(table) != TABLE_KEYS or table.get('table_version') != TABLE_VERSION
            or not isinstance(header, dict) or set(header) != set(HEADER_KEYS)
            or header['study_manifest_sha256'] != manifest.manifest_sha256
            or header['period'] != part_b.report_json_safe(period)
            or header['code_revision'] != code_revision
            or header['period_report_schema_version'] != part_b.PERIOD_REPORT_SCHEMA_VERSION
            or header['study_version'] != manifest.study_version
            or (coverage_sha256 is not None and header['extension_coverage_manifest_sha256'] != coverage_sha256)):
        raise part_b.StudyArtifactConflictError('analysis table scientific identity mismatch')
    return table


def load_hmm_view(path, manifest, period, *, code_revision, coverage_sha256=None):
    table = load_period_table(path, manifest, period, code_revision=code_revision, coverage_sha256=coverage_sha256)
    if period.phase != 'development' or table['hmm_view'] is None:
        raise ValueError('analysis table has no development HMM view')
    return table['hmm_view']


def adapt_phase_from_tables(manifest, coverage, table_dir, phase, revision, cross_fit_sha, final_model_sha,
                            identities, folds=None):
    """Same contract and outputs as part_c.adapt_phase, read from analysis tables."""
    periods = tuple(period for period in manifest.selected_periods if period.phase == phase)
    paths = tuple(table_path(table_dir, period) for period in periods)
    for period, path in zip(periods, paths):
        if not path.is_file():
            raise ValueError(f'{phase} period {period.study_period_index} analysis table is missing: {path}')
    development = phase == 'development'
    days = {identity: [] for identity in identities}
    track_a, sources = [], []
    for period, path in zip(periods, paths):
        table = load_period_table(path, manifest, period, code_revision=revision,
                                  coverage_sha256=coverage['coverage_manifest_sha256'])
        header = table['report_header']
        source = adapter.period_provenance(header, cross_fit_sha, final_model_sha)
        expected_shas = (None, None) if development else (cross_fit_sha, final_model_sha)
        if (table['hmm_cross_fit_sha256'], table['hmm_final_model_sha256']) != expected_shas:
            raise ValueError('analysis table was derived with different HMM prerequisites')
        if not development and header.get('hmm_model_sha256') != final_model_sha:
            raise ValueError('period was not generated with the frozen final HMM')
        stored = {decode(entry['identity']): entry for entry in table['adapted']}
        held_out = (folds or {}).get(period.study_period_index)
        for identity in days:
            if development and identity.family_id == HMM_FAMILY:
                item = adapter.adapt_period({**header, **table['development_hmm_join']}, source, identity,
                                            held_out_fold=held_out, report_verified=True)
            else:
                entry = stored.get(identity)
                if entry is None:
                    raise ValueError('analysis table lacks the requested config')
                item = adapter.AdaptedDay(replace(decode(entry['day']), source_provenance=source),
                                          decode(entry['contexts']), decode(entry['exclusions']))
            days[identity].append(item)
        sources.append(source)
        summaries = table['native_state_quality_summaries']
        track_a.append(TrackAReference(source.study_period_index, source.utc_date, source.phase,
                                       source.period_report_sha256, tuple(summaries), part_b._digest(summaries)))
        table = None
        part_b._log_peak_memory(f'part-c {phase} table {period.study_period_index}')
    upstream = UpstreamInputProvenance(phase, tuple(sources), cross_fit_sha, final_model_sha)
    if len(track_a) != len(periods) or len(upstream.periods) != len(periods):
        raise ValueError('artifact adapter requires the complete phase report roster')
    return {identity: tuple(items) for identity, items in days.items()}, tuple(track_a), upstream


def verify_period_table(path, table_file, manifest, coverage, period, code_revision, *,
                        cross_fit_sha, final_model_sha, identities):
    """Audit: re-derive byte-for-byte, then recompute rows with the default adapter path."""
    report, file_sha = _load_validated_report(path, manifest, coverage, period, code_revision)
    table = build_period_table(report, manifest, period, report_file_sha256=file_sha,
                               cross_fit_sha=cross_fit_sha, final_model_sha=final_model_sha, identities=identities)
    if Path(table_file).read_bytes() != _table_bytes(table):
        raise ValueError('analysis table differs from its re-derivation')
    source = adapter.period_provenance(report, cross_fit_sha, final_model_sha)
    for entry in table['adapted']:
        identity = decode(entry['identity'])
        # Default path: full report hash, full candidate_evidence scans, no index.
        expected = adapter.adapt_period(report, source, identity)
        if (decode(entry['day']) != replace(expected.day, source_provenance=None)
                or decode(entry['contexts']) != expected.contexts
                or decode(entry['exclusions']) != expected.exclusions):
            raise ValueError('analysis table row differs from the default adapter path')
    report = None
    part_b._log_peak_memory(f'verify period {period.study_period_index}')
    return table
