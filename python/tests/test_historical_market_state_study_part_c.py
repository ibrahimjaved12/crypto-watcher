"""Small generated artifacts only. No market archives, replay or study execution."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from market_analysis import historical_market_state_study_adapter as adapter
from market_analysis import historical_market_state_study_artifacts as io
from market_analysis import historical_market_state_study_part_c as cli
from market_analysis import historical_market_state_study_execution as part_b
from market_analysis import historical_market_state_study_evaluation as core
from market_analysis import historical_market_state_study_features as f
from market_analysis import historical_market_state_study_tables as tables
from market_analysis.historical_market_state_candidate_evidence import (
    HistoricalStudyCandidateEvidence, adapt_v1_evidence, _native_point, CANDIDATE_EVIDENCE_VERSION,
)
from market_analysis.historical_market_state_study_json import study_json_safe, canonical_study_json
from market_analysis.historical_experiment_batch import report_json_safe, _canonical_json
from market_analysis.historical_market_state_study import ORDERED_SYMBOLS
from market_analysis.experiments.market_state_hmm_regimes import HMMFeatureRow, HMMDevelopmentTrainingBlock
from market_analysis.movement_metrics import Metric

MANIFEST_PATH = (Path(__file__).parents[2] / 'research' / 'historical-market-state-study-v1' /
                 'selection' / 'historical-market-state-study-v1-manifest.json')
COVERAGE = 'a' * 64
CROSS = 'b' * 64
FINAL = 'c' * 64
REVISION = 'synthetic-part-b-producer'


def identity(family='EXP-75-03'):
    return next(i for i in adapter.PREDICTIVE_CONFIGS if i.family_id == family)


def metric(value):
    return {'available': True, 'value': value, 'reason': None}


def classification():
    window = {'direction_state': 'BROAD_RISE', 'pace': metric('ACCELERATING'),
        'median_normalized_movement': metric(2), 'median_acceleration': metric(3),
        'dispersion_mad_normalized_movement': metric(4),
        'breadth': {side: metric({'fraction': value}) for side, value in zip(
            ('rising', 'falling', 'material_rising', 'material_falling'), (0.8, 0.2, 0.6, 0.1))},
        'volume_context': [{'symbol': symbol, 'rvol': metric(i + 1)} for i, symbol in enumerate(ORDERED_SYMBOLS)]}
    return {'windows': [[1, deepcopy(window)], [5, window], [15, deepcopy(window)]]}


def native(family):
    if family == 'EXP-75-03':
        return {'kalman_filtered_primary_median_normalized_movement': metric(4),
                'raw_primary_median_normalized_movement': metric(2), 'kalman_trend': 0.5}
    if family == 'EXP-75-02':
        return {'positive_accumulator': 1, 'negative_accumulator': 2, 'cusum_direction_state': 'DOWN_SHIFT'}
    if family == 'EXP-75-09':
        return {'status': 'HMM_READY', 'model_sha256': FINAL, 'hard_state': 'HIGH_MOVEMENT', 'posterior_entropy': 0.3}
    if family == 'EXP-75-04B':
        return {'descriptive_detection_region': {'duration': 999, 'observed_through': 999},
                'causal_onset_observation': {'recent_change_probability': 0.8}}
    return {}


def seal(report):
    report.pop('report_sha256', None)
    report['report_sha256'] = part_b._digest(report)
    return report


def generated_report(index=0, config=None, timestamp=None, evidence=None):
    manifest = part_b.load_study_manifest(MANIFEST_PATH)
    period = manifest.selected_periods[index]
    config = config or identity()
    spec = f.predictive_family(config.family_id)
    timestamp = period.start_boundary_time_ms if timestamp is None else timestamp
    records = []
    v1_time = period.start_boundary_time_ms if spec.observation_mode == 'EVENT' else timestamp
    for family, algorithm, version, boundary, kind, payload in (
        ('V1', part_b.V1_CLASSIFIER_ALGORITHM_VERSION, part_b.MarketClassifierConfig().version,
         v1_time, 'CONTINUOUS', {'classification': classification()}),
        (config.family_id, config.algorithm_version, config.config_version, timestamp, spec.observation_mode,
         native(config.family_id) if evidence is None else evidence)):
        if family == 'EXP-75-04B':
            payload['causal_onset_observation'] = {
                'evaluation_boundary_time_ms': timestamp, 'raw_primary_median_normalized_movement': metric(1.0),
                'available': True, 'detector_state': 'CHANGE', 'run_length_zero_probability': 0.1,
                'recent_change_probability': 0.8, 'map_run_length_steps': 1,
                'expected_run_length_steps': 1.5, 'hypothesis_count': 2, 'observations_since_reset': 2,
                'candidate_algorithm_version': config.algorithm_version, 'candidate_config_version': config.config_version}
        records.append(study_json_safe(HistoricalStudyCandidateEvidence(index, period.utc_date.isoformat(),
            period.phase, family, algorithm, version, boundary, kind, 'READY', payload)))
    replay = {'movement_algorithm_version': 'movement-v1', 'movement_config_version': 'config-v1',
        'universe_id': 'universe-v1', 'universe_version': '1', 'configured_universe': list(ORDERED_SYMBOLS),
        'provider': 'provider', 'exchange': 'exchange', 'price_type': 'trade', 'dataset_content_sha256': 'd' * 64}
    replay['run_fingerprint'] = part_b._digest(replay)
    outcome = {'decision_time_ms': timestamp, 'horizon_minutes': spec.horizon_minutes,
        'market_forward_return': 0.2, 'market_forward_realized_volatility': 0.3,
        'forward_cross_sectional_dispersion': 0.4,
        'per_symbol': [{'symbol': symbol, 'status': 'AVAILABLE', 'forward_log_return': value}
                       for symbol, value in zip(ORDERED_SYMBOLS, (0.1, -0.1, 0, 0.1, 0.1))]}
    state_path = {'status': 'AVAILABLE', 'state_persistence': True, 'v1_direction_persistence': True,
                  'initial_v1_direction_state': 'BROAD_RISE', 'persistence_weakening': 'PERSISTED'}
    contexts = [] if spec.observation_mode == 'CONTINUOUS' else [{
        'decision_time_ms': timestamp, 'classification': classification(), 'lifecycle_state': {}, 'transitions': [],
        'provenance': {**{k: v for k, v in replay.items() if k not in ('dataset_content_sha256', 'run_fingerprint')},
                       'source_time_evidence': []}}]
    onsets = [] if config.family_id != 'EXP-75-04B' else [{
        'experiment_id': config.family_id, 'algorithm_version': config.algorithm_version,
        'config_version': config.config_version, 'decision_time_ms': timestamp,
        'onset_observation': records[1]['native_evidence']['causal_onset_observation']}]
    body = {'period_report_schema_version': part_b.PERIOD_REPORT_SCHEMA_VERSION,
        'execution_version': part_b.EXECUTION_VERSION, 'study_version': manifest.study_version,
        'candidate_evidence_version': CANDIDATE_EVIDENCE_VERSION,
        'forward_outcomes_version': part_b.FORWARD_OUTCOMES_VERSION, 'tool_config_version': part_b.TOOL_CONFIG_VERSION,
        'study_manifest_sha256': manifest.manifest_sha256, 'extension_coverage_manifest_sha256': COVERAGE,
        'code_revision': REVISION, 'period': study_json_safe(period),
        'core_eligibility_sha256': 'e' * 64, 'core_archive_content_sha256': 'd' * 64,
        'canonical_replay_manifest': replay, 'canonical_replay_run_fingerprint': replay['run_fingerprint'],
        'taker_flow_evidence_identity': {
            'algorithm_version': 'taker-buy-sell-imbalance-v2-exact-sign'},
        'canonical_replay_diagnostics': {}, 'canonical_replay_diagnostics_sha256': part_b._digest({}),
        'candidate_evidence': records, 'candidate_evidence_sha256': part_b._digest(records),
        'v1_evidence_sha256': part_b._digest(records[:1]),
        'experiment_suite_identity': [dict(zip(('experiment_id', 'algorithm_version', 'config_version'), i))
                                      for i in adapter.ORIGINAL_IDENTITIES],
        'native_state_quality_summaries': [dict(zip(('experiment_id', 'algorithm_version', 'config_version'), i),
                                              summary={'synthetic': True}) for i in adapter.ALL_CANDIDATE_IDENTITIES],
        'event_time_v1_context_version': part_b.EVENT_TIME_V1_CONTEXT_VERSION, 'event_time_v1_context': contexts,
        'bocpd_onset_evidence_version': part_b.BOCPD_ONSET_EVIDENCE_VERSION, 'bocpd_onset_evidence': onsets,
        'hmm_model_sha256': FINAL if period.phase != 'development' else None,
        'forward_label_evidence': {'evidence_version': part_b.FORWARD_OUTCOMES_VERSION,
            'numeric_policy': part_b.FORWARD_NUMERIC_POLICY, 'price_type': 'trade', 'interval': '1m',
            'evidence_sha256': 'f' * 64, 'source_dataset_content_sha256': 'd' * 64,
            'availability_convention': 'close_time_ms < decision_time and first_seen_at_ms <= decision_time',
            'label_range_start_boundary_time_ms': period.start_boundary_time_ms - 60_000,
            'label_range_end_boundary_time_ms': period.end_boundary_time_ms + 60 * 60_000,
            'label_tail_is_not_in_replay_input': True},
        'candidate_independent_continuous_outcomes': [[spec.horizon_minutes, [outcome]]],
        'candidate_event_outcomes': [], 'secondary_v1_continuous_state_paths': [{
            'experiment_id': 'CONTINUOUS_GRID', 'horizon_minutes': spec.horizon_minutes,
            'algorithm_version': part_b.FORWARD_OUTCOMES_VERSION, 'config_version': f'{spec.horizon_minutes}m',
            'decision_time_ms': timestamp, 'state_path': state_path}], 'secondary_v1_event_state_paths': []}
    body.update(pelt_forward_label_count=0, pelt_no_causal_outcomes=True)
    if spec.observation_mode == 'EVENT':
        event_identity = dict(zip(('experiment_id', 'algorithm_version', 'config_version'),
                                 (config.family_id, config.algorithm_version, config.config_version)))
        body['candidate_event_outcomes'] = [{**event_identity, 'outcomes_by_horizon': [[spec.horizon_minutes, [{
            'decision_time_ms': timestamp, 'horizon_minutes': spec.horizon_minutes,
            'confirmatory_independent': True, 'outcome': outcome}]]]}]
        body['secondary_v1_event_state_paths'] = [{**event_identity, 'horizon_minutes': spec.horizon_minutes,
            'decision_time_ms': timestamp, 'state_path': state_path}]
    for name, version in (('event_time_v1_context', part_b.EVENT_TIME_V1_CONTEXT_VERSION),
                          ('bocpd_onset_evidence', part_b.BOCPD_ONSET_EVIDENCE_VERSION)):
        body[name + '_sha256'] = part_b._digest({'version': version, 'records': body[name]})
    if period.phase == 'development':
        block = HMMDevelopmentTrainingBlock(index, period.utc_date.isoformat(), period.start_boundary_time_ms,
            period.end_boundary_time_ms, ('movement-v1', 'config-v1', 'universe-v1', '1', ORDERED_SYMBOLS,
                                         'provider', 'exchange', 'trade'),
            ((HMMFeatureRow(v1_time, (1.0, 0.1, 0.2, 1.0)),),), 1439)
        body['hmm_development_training_block'] = study_json_safe(block)
        body['hmm_development_training_block_sha256'] = block.block_sha256
    return seal(body)


@contextmanager
def period_files(phase, missing=()):
    """Empty report files at the real period paths of one phase (loaders are patched)."""
    manifest = part_b.load_study_manifest(MANIFEST_PATH)
    with TemporaryDirectory() as root:
        for period in manifest.selected_periods:
            if period.phase == phase and period.study_period_index not in missing:
                path = Path(root) / part_b.PERIOD_DIRECTORY / part_b._period_filename(period)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('')
        yield root


def phase_visitor(reports, upstream):
    """Fake for_each_phase_report: visit each report/source pair in order, return upstream."""
    def visit_all(*inputs, visit):
        for report, source in zip(reports, upstream.periods):
            visit(report, source)
        return upstream
    return visit_all


def adapt(report, config=None, fold=None):
    return adapter.adapt_period(report, adapter.period_provenance(report, CROSS, FINAL),
                                config or identity(), held_out_fold=fold)


@lru_cache(maxsize=1)
def unavailable_freeze():
    reports = tuple(generated_report(i) for i in range(10))
    upstream = f.UpstreamInputProvenance('development', tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL)
    samples, results, nominations = [], [], []
    for family in f.PRIMARY_CONFIRMATORY_FAMILY:
        configs = tuple(i for i in adapter.PREDICTIVE_CONFIGS if i.family_id == family)
        common, family_results, nomination = core.evaluate_development_family(configs, ())
        samples.append(common)
        results.extend(family_results)
        nominations.append(nomination)
    return core.DevelopmentFreeze(f.FROZEN_STUDY_MANIFEST_SHA256, tuple(results), tuple(nominations), (),
        tuple(samples), upstream, hmm_cross_fit_sha256=CROSS, hmm_final_model_sha256=FINAL)


def empty_validation(development):
    reports = tuple(generated_report(i) for i in range(10, 18))
    upstream = f.UpstreamInputProvenance('validation', tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL)
    return core.freeze_validation(development, (), upstream)


@lru_cache(maxsize=1)
def nominated_freeze():
    # Reuse the existing finite basis-row unit fixture, with actual frozen configs.
    from test_historical_market_state_study_evaluation import synthetic_day
    base = unavailable_freeze()
    configs = tuple(i for i in adapter.PREDICTIVE_CONFIGS if i.family_id == 'EXP-75-03')
    days = tuple(replace(synthetic_day(index), algorithm_version=config.algorithm_version,
        config_version=config.config_version, source_provenance=base.upstream_provenance.periods[index],
        observations=tuple(replace(row, algorithm_version=config.algorithm_version,
            config_version=config.config_version) for row in synthetic_day(index).observations))
        for config in configs for index in range(8))
    common, results, nomination = core.evaluate_development_family(configs, days)
    selected = next(s.days for s in common.samples if s.identity == nomination.identity)
    pair = core.fit_final_development_pair(nomination, selected)
    stratifier = f.layer_one_stratifier(nomination.family_id)
    source = core.LayerOneContinuousEvidence(nomination.identity, stratifier,
        tuple(f.EvidenceValueDay(d.study_period_index, d.utc_date, d.phase,
                                tuple(r.candidate_features[1] for r in d.observations)) for d in selected),
        tuple((d.study_period_index, d.source_provenance.provenance_sha256) for d in selected))
    return replace(base,
        config_results=tuple(r for r in base.config_results if r.identity.family_id != 'EXP-75-03') + results,
        nominations=tuple(n for n in base.nominations if n.family_id != 'EXP-75-03') + (nomination,),
        common_samples=tuple(c for c in base.common_samples if c.samples[0].identity.family_id != 'EXP-75-03') + (common,),
        predictive_pairs=(pair,), layer_one_evidence=(source,), layer_one_terciles=(core.freeze_layer_one_bins(source),))


def nominated_validation(development, veto=False):
    from test_historical_market_state_study_evaluation import synthetic_day
    reports = tuple(generated_report(i) for i in range(10, 18))
    upstream = f.UpstreamInputProvenance('validation', tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL)
    pair = development.predictive_pairs[0]
    days = []
    for index in range(10, 18):
        original = synthetic_day(index, 'validation')
        rows = tuple(replace(r, algorithm_version=pair.identity.algorithm_version,
            config_version=pair.identity.config_version,
            outcome=(2 + 0.3 * r.baseline_features[2] - 1.5 * r.candidate_features[0]
                     + 0.7 * r.candidate_features[1] if veto else r.outcome)) for r in original.observations)
        days.append(replace(original, algorithm_version=pair.identity.algorithm_version,
            config_version=pair.identity.config_version, observations=rows, source_provenance=upstream.periods[index - 10]))
    decision = core.evaluate_validation(pair, days, upstream, development)
    return core.freeze_validation(development, (decision,), upstream), tuple(days)


class PartCArtifactIntegrationTests(unittest.TestCase):
    def test_development_orchestration_represents_all_fixed_configs_even_unavailable(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports = tuple(generated_report(i) for i in range(10))
        upstream = f.UpstreamInputProvenance('development', tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL)
        folds = {}
        for report in reports:
            index = report['period']['study_period_index']
            timestamp = report['candidate_evidence'][0]['decision_time_ms']
            fold = {'held_out_period': {'period_report_sha256': report['report_sha256'], 'study_period_index': index,
                    'utc_date': report['period']['utc_date']}, 'fold_model_sha256': '9' * 64,
                    'held_out_evidence': [[{**native('EXP-75-09'), 'model_sha256': '9' * 64,
                                           'evaluation_boundary_time_ms': timestamp}]]}
            fold['artifact_sha256'] = part_b._digest(fold)
            folds[index] = fold
        args = SimpleNamespace(study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION,
            hmm_crossfit_index='index', final_hmm_model='model', part_b_output_dir='development', output='dev')
        with patch.object(cli, 'verify_inputs', return_value=(manifest, {'coverage_manifest_sha256': COVERAGE})), \
                patch.object(cli, 'load_hmm_prerequisites', return_value=(CROSS, FINAL, folds)), \
                patch.object(cli, 'for_each_phase_report', side_effect=phase_visitor(reports, upstream)) as loader, \
                patch.object(cli, 'write_artifact') as writer:
            development = cli.run_development(args)
        self.assertEqual(loader.call_args.args[3], 'development')
        self.assertEqual({r.identity for r in development.config_results}, set(adapter.PREDICTIVE_CONFIGS))
        self.assertEqual(len(development.nominations), 15)
        self.assertTrue(all(n.status == 'NOT_EVALUABLE' for n in development.nominations))
        self.assertEqual(len(writer.call_args.kwargs['track_a']), 10)
        self.assertEqual(io.decode(io.encode(development)), development)

    def test_layer_one_descriptions_use_frozen_bins_and_keep_track_a_separate(self):
        development = nominated_freeze()
        config = development.predictive_pairs[0].identity
        report = generated_report(config=config)
        item = adapt(report, config)
        table = cli.layer_one_description(development, {config: (item,)})
        self.assertEqual(len(table), 1)
        self.assertEqual(table[0]['candidate_category'], development.layer_one_terciles[0].bins.assign(0.5))
        self.assertEqual(table[0]['observation_count'], 1)
        self.assertEqual(table[0]['day_count'], 1)
        self.assertEqual(table[0]['v1_direction'], 'BROAD_RISE')
        self.assertFalse(any(key in table[0] for key in ('rank', 'track_a_supported', 'classification')))

    def test_no_archive_or_download_cli_inputs(self):
        parser = cli.build_cli_parser()
        subcommands = next(action for action in parser._actions if hasattr(action, 'choices') and isinstance(action.choices, dict))
        for command in subcommands.choices.values():
            options = {option for action in command._actions for option in action.option_strings}
            self.assertIn('--part-b-revision', options)
            self.assertFalse(any('archive' in option or 'download' in option or 'replay' in option for option in options))

    def test_nominated_pair_models_and_layer_one_round_trip(self):
        development = nominated_freeze()
        validation, _ = nominated_validation(development)
        self.assertEqual(validation.decisions[0].status, 'CONFIRMED')
        authorization = core.authorize_test(development, validation)
        for value in (development, validation, authorization):
            self.assertEqual(io.decode(io.encode(value)), value)
        self.assertEqual(len(development.layer_one_terciles), 1)
        self.assertEqual(len(development.predictive_pairs), 1)
        pair = development.predictive_pairs[0]
        self.assertIsInstance(pair.baseline_preprocessing, core.FrozenStandardizer)
        self.assertIsInstance(pair.baseline_model, core.FrozenLinearModel)
        references = cli.track_a_references(tuple(generated_report(i) for i in range(10)), development.upstream_provenance)
        with TemporaryDirectory() as root:
            path = Path(root) / 'nominated-development.json'
            io.write_artifact(path, 'development', development, track_a=references)
            restored, _ = io.read_artifact(path, 'development')
        self.assertEqual(restored, development)
        self.assertEqual(io.encode(restored), io.encode(development))
        self.assertEqual(restored.freeze_sha256, development.freeze_sha256)
        self.assertEqual(restored.predictive_pairs[0].pair_sha256, pair.pair_sha256)
        self.assertEqual(restored.layer_one_terciles[0].binding_sha256, development.layer_one_terciles[0].binding_sha256)

    def test_authorized_test_opens_after_gate_and_only_scores_authorized_family(self):
        development = nominated_freeze()
        validation, _ = nominated_validation(development)
        authorization = core.authorize_test(development, validation)
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports = tuple(generated_report(i, development.predictive_pairs[0].identity) for i in range(18, 30))
        upstream = f.UpstreamInputProvenance('test', tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL)
        metadata = {'track_a': (), 'layer_one': (), 'exclusions': ()}
        args = SimpleNamespace(development_freeze='dev', validation_freeze='val', test_authorization='auth',
            study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION, part_b_output_dir='test', output='report')
        order = []
        original_authorize = core.authorize_test

        def gate(*parents):
            order.append('authorize')
            return original_authorize(*parents)

        def load(*inputs, visit):
            self.assertEqual(order[0], 'authorize')
            order.append('open-test')
            self.assertEqual(inputs[3], 'test')
            return phase_visitor(reports, upstream)(*inputs, visit=visit)

        with patch.object(cli, 'read_artifact', side_effect=((development, metadata), (validation, metadata), (authorization, {}))), \
                patch.object(cli, 'verify_inputs', return_value=(manifest, {'coverage_manifest_sha256': COVERAGE})), \
                patch.object(core, 'authorize_test', side_effect=gate), \
                patch.object(cli, 'for_each_phase_report', side_effect=load), \
                patch.object(core, 'evaluate_test', wraps=core.evaluate_test) as scorer, \
                patch.object(cli, 'write_artifact'):
            result = cli.run_test(args)
        self.assertEqual(scorer.call_count, 1)
        self.assertEqual(scorer.call_args.args[3], 'EXP-75-03')
        self.assertEqual(result.upstream_provenance, upstream)
        self.assertEqual(len(result.holm), 15)
        self.assertEqual(io.decode(io.encode(result)), result)
        self.assertEqual(len(result.final_family_summaries), 15)
        authorized_summary = next(s for s in result.final_family_summaries if s.family_id == 'EXP-75-03')
        self.assertEqual(authorized_summary.classification, result.results[0].classification)
        self.assertEqual(authorized_summary.test_result_sha256, result.results[0].result_sha256)
        with self.assertRaises(ValueError):
            core.final_family_evidence_summaries(authorization, development.config_results,
                development.nominations, validation.decisions, ())
        with self.assertRaises(ValueError):
            replace(result, final_family_summaries=tuple(
                replace(s, test_result_sha256='0' * 64) if s.family_id == authorized_summary.family_id else s
                for s in result.final_family_summaries))
        track_a = cli.track_a_references(tuple(generated_report(i) for i in range(10)), development.upstream_provenance)
        track_a += cli.track_a_references(tuple(generated_report(i) for i in range(10, 18)), validation.upstream_provenance)
        track_a += cli.track_a_references(reports, upstream)
        with TemporaryDirectory() as root:
            path = Path(root) / 'authorized-test.json'
            io.write_artifact(path, 'test', result, track_a=track_a)
            restored, _ = io.read_artifact(path, 'test')
            self.assertEqual(restored, result)
            self.assertEqual(restored.final_family_summaries, result.final_family_summaries)

    def test_validation_veto_prevents_all_test_report_reads(self):
        development = nominated_freeze()
        validation, _ = nominated_validation(development, veto=True)
        self.assertEqual(validation.decisions[0].status, 'NOT_CONFIRMED')
        authorization = core.authorize_test(development, validation)
        self.assertEqual(next(m.status for m in authorization.members if m.family_id == 'EXP-75-03'), 'VETOED')
        metadata = {'track_a': (), 'layer_one': (), 'exclusions': ()}
        args = SimpleNamespace(development_freeze='dev', validation_freeze='val', test_authorization='auth',
            study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION, part_b_output_dir='test', output='report')
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        with patch.object(cli, 'read_artifact', side_effect=((development, metadata), (validation, metadata), (authorization, {}))), \
                patch.object(cli, 'verify_inputs', return_value=(manifest, {'coverage_manifest_sha256': COVERAGE})), \
                patch.object(cli, 'for_each_phase_report') as loader, patch.object(core, 'evaluate_test') as scorer, \
                patch.object(cli, 'write_artifact'):
            report = cli.run_test(args)
        loader.assert_not_called()
        scorer.assert_not_called()
        self.assertEqual(report.results, ())
        self.assertEqual(tuple(s.family_id for s in report.final_family_summaries), f.PRIMARY_CONFIRMATORY_FAMILY)
        summary = next(s for s in report.final_family_summaries if s.family_id == 'EXP-75-03')
        self.assertEqual(summary.classification, core.EvidenceClassification.UNSTABLE_ACROSS_PERIODS)
        self.assertIsNone(summary.test_result_sha256)
        references = cli.track_a_references(tuple(generated_report(i) for i in range(10)), development.upstream_provenance)
        references += cli.track_a_references(tuple(generated_report(i) for i in range(10, 18)), validation.upstream_provenance)
        with TemporaryDirectory() as root:
            path = Path(root) / 'vetoed-test.json'
            io.write_artifact(path, 'test', report, track_a=references)
            self.assertEqual(io.read_artifact(path, 'test')[0], report)
            for changes in ({'classification': core.EvidenceClassification.INCONCLUSIVE},
                            {'nomination_sha256': '0' * 64},
                            {'validation_decision_sha256': '0' * 64},
                            {'test_result_sha256': '0' * 64},
                            {'authorization_status': 'COVERAGE_LIMITED'}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    altered = replace(summary, **changes)
                    replace(report, final_family_summaries=tuple(altered if s.family_id == summary.family_id else s
                                                                for s in report.final_family_summaries))
            for members in (report.final_family_summaries[:-1], report.final_family_summaries[::-1],
                            report.final_family_summaries[:-1] + (report.final_family_summaries[0],)):
                with self.assertRaises(ValueError):
                    replace(report, final_family_summaries=members)
            # Re-seal both the inner summary and outer envelope: deterministic
            # report reconstruction must still reject an invented classification.
            envelope = json.loads(path.read_text())
            altered = replace(summary, classification=core.EvidenceClassification.INCONCLUSIVE)
            entries = envelope['value']['fields']['final_family_summaries']['$tuple']
            offset = f.PRIMARY_CONFIRMATORY_FAMILY.index(summary.family_id)
            entries[offset] = io.encode(altered)
            envelope.pop('artifact_sha256')
            envelope['artifact_sha256'] = part_b._digest(envelope)
            path.write_text(canonical_study_json(envelope))
            with self.assertRaises(ValueError):
                io.read_artifact(path, 'test')

    def test_complete_summaries_validation_feasibility_and_nonpositive_development(self):
        development = nominated_freeze()
        validation, _ = nominated_validation(development, veto=True)
        for status in ('COVERAGE_LIMITED', 'FIT_FAILED'):
            if status == 'COVERAGE_LIMITED':
                decision = core.evaluate_validation(development.predictive_pairs[0], (), validation.upstream_provenance, development)
            else:
                decision = replace(validation.decisions[0], status='FIT_FAILED', reason='SYNTHETIC_FIT_FAILURE', day_results=())
            frozen = core.freeze_validation(development, (decision,), validation.upstream_provenance)
            authorization = core.authorize_test(development, frozen)
            summaries = core.final_family_evidence_summaries(authorization, development.config_results,
                development.nominations, frozen.decisions, ())
            self.assertEqual(len(summaries), 15)
            self.assertTrue(all(s.classification == core.EvidenceClassification.COVERAGE_LIMITED for s in summaries))
        # Explicit synthetic negative held-out losses, preserving all reference
        # masks and recomputing nomination/pair hashes with the existing core.
        results = tuple(replace(r, folds=tuple(replace(fold, day_result=replace(fold.day_result,
            baseline_loss=fold.day_result.extended_loss, extended_loss=fold.day_result.baseline_loss,
            delta=-fold.day_result.delta)) for fold in r.folds)) if r.identity.family_id == 'EXP-75-03' else r
            for r in development.config_results)
        nomination = core.nominate_configuration(tuple(r for r in results if r.identity.family_id == 'EXP-75-03'))
        pair = replace(development.predictive_pairs[0], development_median=nomination.median_delta,
                       nomination_sha256=nomination.nomination_sha256)
        negative = replace(development, config_results=results, predictive_pairs=(pair,),
            nominations=tuple(nomination if n.family_id == 'EXP-75-03' else n for n in development.nominations))
        veto, _ = nominated_validation(negative, veto=True)
        authorization = core.authorize_test(negative, veto)
        summaries = core.final_family_evidence_summaries(authorization, negative.config_results,
            negative.nominations, veto.decisions, ())
        summary = next(s for s in summaries if s.family_id == 'EXP-75-03')
        self.assertEqual(summary.classification, core.EvidenceClassification.INCONCLUSIVE)
        self.assertIsNone(summary.test_result_sha256)

    def test_all_persisted_outcome_transforms_and_censoring(self):
        outcome = {'market_forward_return': -0.2, 'market_forward_realized_volatility': 0.3,
            'forward_cross_sectional_dispersion': 0.4, 'per_symbol': [
                {'status': 'AVAILABLE', 'forward_log_return': v} for v in (1, -1, 0, 1, 1)]}
        for name, expected in (('signed_market_return', -0.2), ('absolute_market_return', 0.2),
                               ('realized_volatility', 0.3), ('forward_dispersion', 0.4), ('future_breadth_extremity', 0.4)):
            self.assertEqual(adapter.extract_outcome(name, outcome, None), expected)
        path = {'status': 'AVAILABLE', 'state_persistence': False, 'v1_direction_persistence': True,
                'initial_v1_direction_state': 'BROAD_RISE', 'persistence_weakening': 'WEAKENED'}
        self.assertEqual(adapter.extract_outcome('state_persistence', outcome, path), 0)
        self.assertEqual(adapter.extract_outcome('v1_direction_persistence', outcome, path), 1)
        self.assertEqual(adapter.extract_outcome('persistence_weakening', outcome, path), 0)
        for status in ('CENSORED', 'UNAVAILABLE'):
            with self.assertRaises(adapter.UnavailableObservation):
                adapter.extract_outcome('state_persistence', outcome, {**path, 'status': status})

    def test_extension_and_coordination_feature_schemas(self):
        c = classification()
        expected = (
            ('EXP-75-01', {'ewma_primary_median_normalized_movement': metric(4),
                          'raw_primary_median_normalized_movement': metric(2), 'candidate_classification': c}, (2, 0)),
            ('EXP-75-05', {'candidate_classification': c}, (0,)),
            ('EXP-75-06A', {'candidate_classification': c}, (0, 0)),
            ('EXP-75-06B', {'candidate_classification': c}, (0, 0)),
            ('EXP-75-07', {'pca_evidence': {'status': 'PCA_READY', 'explained_variance_ratio': 0.8,
                                           'current_pc1_energy_fraction': 0.7}}, (0.8, 0.7)),
            ('EXP-75-08', {'correlation_evidence': {'status': 'CORRELATION_READY', 'median_pairwise_correlation': 0.5,
                                                  'network_edge_density': 0.6}}, (0.5, 0.6)),
            ('EXP-75-10', {'windows': [{'window_minutes': 5, 'all_configured_symbols_ready': True,
                                      'symbols': [{'symbol': s, 'status': 'READY', 'divergence': str(i)}
                                                  for i, s in enumerate(ORDERED_SYMBOLS)]}]}, (2,)),
            ('EXP-75-11-OI', {'windows': [{'window_minutes': m, 'market_summary': {'median_delta_oi': str(m)}}
                                        for m in (5, 15)]}, (5, 15)),
            ('EXP-75-11-FUNDING', {'windows': [{'window_minutes': 60, 'market_summary': {'median_funding_per_hour': '0.001'}}]}, (0.001,)),
        )
        for family, evidence, values in expected:
            with self.subTest(family=family):
                self.assertEqual(adapter.candidate_features(identity(family), evidence, c)[0], values)

    def test_lossless_integer_window_dataclasses_and_global_serializer_unchanged(self):
        @dataclass(frozen=True)
        class Classification:
            windows: dict
        value = Classification({1: Metric.present(1), 5: Metric.present(5), 15: Metric.present(15)})
        encoded = study_json_safe(value)
        self.assertEqual(dict(encoded['windows'])[5]['value'], 5)
        self.assertTrue(all(type(key) is int for key, _ in encoded['windows']))
        with self.assertRaises(TypeError):
            report_json_safe(value)
        period = part_b.load_study_manifest(MANIFEST_PATH).selected_periods[0]
        record = adapt_v1_evidence(period, period.start_boundary_time_ms, value, {}, ())
        self.assertEqual(record.native_evidence['classification'], encoded)
        self.assertEqual(_native_point(value), encoded)
        self.assertEqual(canonical_study_json({'a': Metric.present(3)}), _canonical_json({'a': Metric.present(3)}))
        self.assertEqual(study_json_safe(frozenset((3, 1))), [1, 3])
        for bad in (float('nan'), float('inf'), object()):
            with self.assertRaises((TypeError, ValueError)):
                study_json_safe(bad)

    def test_public_finalized_loader_rejects_identity_hash_revision_and_coverage(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        period = manifest.selected_periods[10]
        report = generated_report(10)
        with TemporaryDirectory() as root:
            path = Path(root) / 'period.json'
            path.write_text(canonical_study_json(report))
            self.assertEqual(part_b.load_finalized_period_report(path, manifest, period,
                coverage_sha256=COVERAGE, code_revision=REVISION), report)
            for version in ('taker-buy-sell-imbalance-v1', 'invented', None):
                changed = deepcopy(report)
                if version is None:
                    del changed['taker_flow_evidence_identity']
                else:
                    changed['taker_flow_evidence_identity']['algorithm_version'] = version
                path.write_text(canonical_study_json(seal(changed)))
                with self.subTest(taker_flow_version=version), self.assertRaisesRegex(
                        ValueError, 'incompatible taker-flow calculation'):
                    part_b.load_finalized_period_report(path, manifest, period,
                        coverage_sha256=COVERAGE, code_revision=REVISION)
            path.write_text(canonical_study_json(report))
            for revision, coverage in (('consumer-head', COVERAGE), (REVISION, '0' * 64)):
                with self.assertRaises(ValueError):
                    part_b.load_finalized_period_report(path, manifest, period, coverage_sha256=coverage, code_revision=revision)
            for field in ('report_sha256', 'period_report_schema_version', 'candidate_evidence_sha256', 'v1_evidence_sha256',
                          'event_time_v1_context_sha256', 'bocpd_onset_evidence_sha256', 'canonical_replay_run_fingerprint'):
                changed = deepcopy(report)
                changed[field] = '0' * 64
                if field != 'report_sha256':
                    seal(changed)
                path.write_text(canonical_study_json(changed))
                with self.subTest(field=field), self.assertRaises(ValueError):
                    part_b.load_finalized_period_report(path, manifest, period, coverage_sha256=COVERAGE, code_revision=REVISION)
            path.write_text(canonical_study_json(report))
            with self.assertRaises(ValueError):
                part_b.load_finalized_period_report(path, manifest, manifest.selected_periods[11],
                    coverage_sha256=COVERAGE, code_revision=REVISION)

    def test_fixed_registry_rejects_missing_extra_wrong_and_duplicate_configs(self):
        report = generated_report()
        adapter.validate_candidate_registry(report)
        for action in ('missing', 'extra', 'wrong', 'duplicate'):
            changed = deepcopy(report)
            rows = changed['native_state_quality_summaries']
            if action == 'missing': rows.pop()
            elif action == 'extra': rows.append({'experiment_id': 'invented'})
            elif action == 'wrong': rows[0]['config_version'] = 'invented'
            else: rows[-1] = rows[0]
            with self.subTest(action=action), self.assertRaises(ValueError):
                adapter.validate_candidate_registry(changed)
        self.assertEqual({i.family_id for i in adapter.PREDICTIVE_CONFIGS}, set(f.PRIMARY_CONFIRMATORY_FAMILY))

    def test_exact_continuous_join_and_config_independent_keys(self):
        report = generated_report()
        first = adapt(report).day
        self.assertEqual(first.scheduled_primary_count, 96)
        self.assertEqual(first.observations[0].candidate_features, (2, 0.5))
        other = [i for i in adapter.PREDICTIVE_CONFIGS if i.family_id == 'EXP-75-03'][1]
        second = adapt(generated_report(config=other), other).day
        self.assertEqual(first.observations[0].observation_key, second.observations[0].observation_key)
        for shift in (1000, 60_000):
            changed = deepcopy(report)
            changed['candidate_independent_continuous_outcomes'][0][1][0]['decision_time_ms'] += shift
            self.assertEqual(adapt(seal(changed)).day.observations, ())
        changed = deepcopy(report)
        changed['candidate_independent_continuous_outcomes'][0][0] = 5
        self.assertEqual(adapt(seal(changed)).day.observations, ())
        changed = deepcopy(report)
        changed['candidate_evidence'][0]['decision_time_ms'] += 60_000
        self.assertEqual(adapt(seal(changed)).day.observations, ())

    def test_baseline_exact_order_and_no_missing_rvol_imputation(self):
        vector = adapter.baseline_features(classification(), 6 * 60 * 60_000)
        self.assertEqual(len(vector), len(f.BASELINE_FEATURES))
        self.assertEqual(vector, (1, 0, 2, 0.8, 0.2, 0.6, 0.1, 4, 3, 3, 1, 0, 1, 0, 0))
        c = classification()
        c['windows'][1][1]['volume_context'][0]['rvol'] = metric(None)
        self.assertEqual(adapter.baseline_features(c, 0)[8], 3.5)
        for row in c['windows'][1][1]['volume_context']:
            row['rvol'] = {'available': False}
        with self.assertRaises(adapter.UnavailableObservation):
            adapter.baseline_features(c, 0)

    def test_structural_neutral_pace_retains_reference_rows_and_rejects_inconsistency(self):
        c = classification()
        primary = c['windows'][1][1]
        primary.update(direction_state='NEUTRAL', pace={'available': False, 'value': None, 'reason': 'NO_BROAD_DIRECTION'})
        vector = adapter.baseline_features(c, 0)
        self.assertEqual(vector[:2], (0, 0))
        self.assertEqual(vector[10:12], (0, 0))
        report = generated_report()
        report['candidate_evidence'][0]['native_evidence']['classification'] = c
        self.assertEqual(len(adapt(seal(report)).day.observations), 1)
        for pace in ({'available': False, 'value': None, 'reason': 'WARMING'},
                     {'available': True, 'value': 'ACCELERATING', 'reason': None},
                     {'available': True, 'value': 'MIXED', 'reason': None},
                     {'available': False, 'value': 'ACCELERATING', 'reason': 'NO_BROAD_DIRECTION'},
                     {'available': False, 'reason': 'NO_BROAD_DIRECTION'}):
            primary['pace'] = pace
            with self.subTest(pace=pace), self.assertRaises(adapter.UnavailableObservation):
                adapter.baseline_features(c, 0)
        for state in ('BROAD_RISE', 'BROAD_DROP'):
            primary.update(direction_state=state, pace={'available': False, 'value': None, 'reason': 'WARMING'})
            self.assertEqual(adapt(seal(report)).day.observations, ())

    def test_unavailable_baseline_candidate_and_outcome_exclude_both_models(self):
        for target in ('baseline', 'candidate', 'outcome'):
            report = generated_report()
            if target == 'baseline':
                report['candidate_evidence'][0]['native_evidence']['classification']['windows'][1][1]['pace']['available'] = False
            elif target == 'candidate':
                report['candidate_evidence'][1]['native_evidence']['kalman_trend'] = None
            else:
                report['candidate_independent_continuous_outcomes'][0][1][0]['market_forward_return'] = None
            adapted = adapt(seal(report))
            self.assertEqual(adapted.day.observations, ())
            self.assertIsNotNone(adapted.day.source_provenance)
            self.assertEqual(sum(n for _, n in adapted.exclusions), 1)

    def test_cusum_exact_event_independence_and_context(self):
        config = identity('EXP-75-02')
        start = part_b.load_study_manifest(MANIFEST_PATH).selected_periods[0].start_boundary_time_ms
        report = generated_report(config=config, timestamp=start + 5_000)
        row = adapt(report, config).day.observations[0]
        self.assertEqual(row.decision_time_ms, start + 5_000)
        self.assertEqual(row.candidate_features, (1, 2, 1))
        self.assertEqual(adapt(report, config).contexts[0][-1], 'DOWN_SHIFT')
        for target in ('independence', 'context', 'outcome'):
            changed = deepcopy(report)
            if target == 'independence':
                changed['candidate_event_outcomes'][0]['outcomes_by_horizon'][0][1][0]['confirmatory_independent'] = False
            elif target == 'context': changed['event_time_v1_context'][0]['decision_time_ms'] += 5_000
            else: changed['candidate_event_outcomes'][0]['outcomes_by_horizon'][0][1][0]['decision_time_ms'] += 5_000
            self.assertEqual(adapt(seal(changed), config).day.observations, ())

    def test_bocpd_uses_dedicated_onset_not_descriptive_region(self):
        config = identity('EXP-75-04B')
        report = generated_report(config=config)
        row = adapt(report, config).day.observations[0]
        self.assertEqual(row.candidate_features, (0.8,))
        report['candidate_evidence'][1]['native_evidence']['descriptive_detection_region'] = {'end': 123456, 'duration': 123456}
        self.assertEqual(adapt(seal(report), config).day.observations[0].candidate_features, (0.8,))
        report['bocpd_onset_evidence'][0]['decision_time_ms'] += 5_000
        self.assertEqual(adapt(seal(report), config).day.observations, ())

    def test_finalized_event_loader_preserves_integer_window_context(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        period = manifest.selected_periods[10]
        with TemporaryDirectory() as root:
            path = Path(root) / 'event.json'
            for family in ('EXP-75-02', 'EXP-75-04B'):
                config = identity(family)
                report = generated_report(10, config, period.start_boundary_time_ms + 5_000)
                path.write_text(canonical_study_json(report))
                finalized = part_b.load_finalized_period_report(path, manifest, period,
                    coverage_sha256=COVERAGE, code_revision=REVISION)
                self.assertEqual(len(adapt(finalized, config).day.observations), 1)
                self.assertEqual(finalized['event_time_v1_context'][0]['classification']['windows'][1][0], 5)

    def test_hmm_development_requires_held_out_and_later_phases_frozen_final(self):
        config = identity('EXP-75-09')
        report = generated_report(config=config)
        with self.assertRaises(ValueError): adapt(report, config)
        evidence = {**native(config.family_id), 'model_sha256': '9' * 64, 'hard_state': 'LOW_MOVEMENT',
                    'evaluation_boundary_time_ms': report['candidate_evidence'][1]['decision_time_ms']}
        fold = {'held_out_period': {'period_report_sha256': report['report_sha256'], 'study_period_index': 0,
                                    'utc_date': report['period']['utc_date']},
                'fold_model_sha256': '9' * 64, 'held_out_evidence': [[evidence]]}
        fold['artifact_sha256'] = part_b._digest(fold)
        self.assertEqual(adapt(report, config, fold).day.observations[0].candidate_features, (1, 0, 0.3))
        for index in (10, 18):
            later = generated_report(index, config)
            self.assertEqual(adapt(later, config).day.observations[0].candidate_features, (0, 1, 0.3))
            later['candidate_evidence'][1]['native_evidence']['model_sha256'] = '0' * 64
            with self.assertRaises(ValueError): adapt(seal(later), config)

    def test_liquidation_covered_zero_is_distinct_from_unavailable(self):
        config = identity('EXP-75-11-LIQUIDATION')
        summary = {'covered_count': 5, 'pooled_observed_total_notional': '0',
            'pooled_observed_long_notional': '0', 'pooled_observed_short_notional': '0', 'liquidation_breadth': 0}
        evidence = {'windows': [{'window_minutes': 15, 'market_summary': summary}]}
        self.assertEqual(adapter.candidate_features(config, evidence, classification())[0], (0, 0, 0))
        summary['covered_count'] = 0
        with self.assertRaises(adapter.UnavailableObservation): adapter.candidate_features(config, evidence, classification())

    def test_taker_no_active_and_missing_pooled_imbalance_are_not_zero(self):
        config = identity('EXP-75-12')
        summary = {'sign_breadth_denominator': 0, 'pooled_notional_imbalance': None, 'buy_sign_count': 0, 'sell_sign_count': 0}
        evidence = {'windows': [{'window_minutes': 5, 'market_summary': summary}]}
        with self.assertRaises(adapter.UnavailableObservation): adapter.candidate_features(config, evidence, classification())
        summary.update(sign_breadth_denominator=5, buy_sign_count=3, sell_sign_count=1)
        with self.assertRaises(adapter.UnavailableObservation): adapter.candidate_features(config, evidence, classification())
        summary['pooled_notional_imbalance'] = '0.2'
        self.assertEqual(adapter.candidate_features(config, evidence, classification())[0], (0.2, 0.4))

    def test_full_development_typed_round_trip_safe_resume_conflict_and_tamper(self):
        development = unavailable_freeze()
        references = cli.track_a_references(tuple(generated_report(i) for i in range(10)), development.upstream_provenance)
        self.assertEqual({n.family_id for n in development.nominations}, set(f.PRIMARY_CONFIRMATORY_FAMILY))
        self.assertEqual({r.identity for r in development.config_results}, set(adapter.PREDICTIVE_CONFIGS))
        with TemporaryDirectory() as root:
            path = Path(root) / 'development.json'
            io.write_artifact(path, 'development', development, track_a=references)
            self.assertEqual(io.read_artifact(path, 'development')[0], development)
            io.write_artifact(path, 'development', development, track_a=references)
            with self.assertRaises(ValueError): io.write_artifact(path, 'development', development,
                track_a=references, exclusions=({'extra': True},))
            payload = json.loads(path.read_text())
            payload['value']['fields']['freeze_sha256'] = '0' * 64
            payload.pop('artifact_sha256')
            payload['artifact_sha256'] = part_b._digest(payload)
            path.write_text(canonical_study_json(payload))
            with self.assertRaises(ValueError): io.read_artifact(path, 'development')
        with self.assertRaises(ValueError): io.decode({'$type': 'Path', 'fields': {}})

    def test_test_parent_mismatch_fails_before_test_loader_and_zero_authorized_never_opens(self):
        development = unavailable_freeze()
        validation = empty_validation(development)
        authorization = core.authorize_test(development, validation)
        args = SimpleNamespace(development_freeze='dev', validation_freeze='val', test_authorization='auth',
            study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION, part_b_output_dir='test', output='report')
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        with patch.object(cli, 'read_artifact', side_effect=((development, {'track_a': (), 'layer_one': (), 'exclusions': ()}),
                (validation, {'track_a': (), 'layer_one': (), 'exclusions': ()}),
                (replace(authorization, validation_freeze_sha256='0' * 64), {}))), patch.object(cli, 'for_each_phase_report') as loader:
            with self.assertRaises(ValueError): cli.run_test(args)
            loader.assert_not_called()
        with patch.object(cli, 'read_artifact', side_effect=((development, {'track_a': (), 'layer_one': (), 'exclusions': ()}),
                (validation, {'track_a': (), 'layer_one': (), 'exclusions': ()}), (authorization, {}))), \
                patch.object(cli, 'verify_inputs', return_value=(manifest, {'coverage_manifest_sha256': COVERAGE})), \
                patch.object(cli, 'for_each_phase_report') as loader, patch.object(core, 'evaluate_test') as scorer, \
                patch.object(cli, 'write_artifact'):
            report = cli.run_test(args)
            loader.assert_not_called()
            scorer.assert_not_called()
            self.assertIsNone(report.upstream_provenance)
            self.assertEqual(len(report.holm), 15)
            self.assertTrue(all(m.raw_p == 1 for m in report.holm))
            self.assertEqual(len(report.final_family_summaries), 15)
            self.assertTrue(all(s.classification == core.EvidenceClassification.COVERAGE_LIMITED
                                for s in report.final_family_summaries))

    def test_validation_no_reselection_or_refit(self):
        development = unavailable_freeze()
        validation = empty_validation(development)
        args = SimpleNamespace(development_freeze='dev', study_manifest='manifest', coverage_manifest='coverage',
            part_b_revision=REVISION, hmm_crossfit_index='index', final_hmm_model='model', part_b_output_dir='validation',
            output='val', test_authorization_output='auth')
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports = tuple(generated_report(i) for i in range(10, 18))
        with patch.object(cli, 'read_artifact', return_value=(development, {})), \
                patch.object(cli, 'verify_inputs', return_value=(manifest, {'coverage_manifest_sha256': COVERAGE})), \
                patch.object(cli, 'load_hmm_prerequisites', return_value=(CROSS, FINAL, {})), \
                patch.object(cli, 'for_each_phase_report',
                             side_effect=phase_visitor(reports, validation.upstream_provenance)) as loader, \
                patch.object(core, 'fit_final_development_pair') as refit, \
                patch.object(core, 'evaluate_development_family') as reselect, \
                patch.object(cli, 'write_artifact'):
            result, authorization = cli.run_validation(args)
            self.assertEqual(result, validation)
            self.assertEqual(loader.call_args.args[3], 'validation')
            refit.assert_not_called()
            reselect.assert_not_called()
            self.assertTrue(all(m.status == 'NOT_EVALUABLE' for m in authorization.members))

    def test_exact_phase_loader_never_scans_other_phase_or_accepts_wrong_final_hmm(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports = tuple(generated_report(i) for i in range(10, 18))
        with period_files('validation') as root, \
                patch.object(part_b, 'load_finalized_period_report', side_effect=reports) as loader:
            _, upstream = adapter.load_phase_reports(manifest, {'coverage_manifest_sha256': COVERAGE},
                root, 'validation', REVISION, CROSS, FINAL)
            self.assertEqual(len(loader.call_args_list), 8)
            self.assertEqual([p.study_period_index for p in upstream.periods], list(range(10, 18)))
            self.assertTrue(all('/periods/01' in str(c.args[0]) for c in loader.call_args_list))
        wrong = deepcopy(reports[0])
        wrong['hmm_model_sha256'] = '0' * 64
        with period_files('validation') as root, \
                patch.object(part_b, 'load_finalized_period_report', return_value=wrong), self.assertRaises(ValueError):
            adapter.load_phase_reports(manifest, {'coverage_manifest_sha256': COVERAGE}, root, 'validation', REVISION, CROSS, FINAL)

    def test_track_a_pelt_is_separate_and_cannot_be_predictive(self):
        reports = tuple(generated_report(i) for i in range(10))
        upstream = f.UpstreamInputProvenance('development',
            tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL)
        reference = cli.track_a_references(reports, upstream)[0]
        self.assertTrue(any(s['experiment_id'] == 'EXP-75-04A' for s in reference.native_state_quality_summaries))
        with self.assertRaises(ValueError): core.CandidateConfigIdentity('EXP-75-04A', 'pelt', 'config')
        with self.assertRaises(ValueError): replace(adapt(reports[0]).day.observations[0], family_id='EXP-75-04A')
        self.assertFalse(any(i.family_id == 'EXP-75-04A' for i in adapter.PREDICTIVE_CONFIGS))
        self.assertNotIn('track_a_supported', io.encode(reference)['fields'])


def development_folds(reports):
    folds = {}
    for report in reports:
        index = report['period']['study_period_index']
        timestamp = report['candidate_evidence'][0]['decision_time_ms']
        fold = {'held_out_period': {'period_report_sha256': report['report_sha256'], 'study_period_index': index,
                'utc_date': report['period']['utc_date']}, 'fold_model_sha256': '9' * 64,
                'held_out_evidence': [[{**native('EXP-75-09'), 'model_sha256': '9' * 64,
                                       'evaluation_boundary_time_ms': timestamp}]]}
        fold['artifact_sha256'] = part_b._digest(fold)
        folds[index] = fold
    return folds


def outcome(call):
    """A value or the exact failure, so equivalence also covers raised errors."""
    try:
        return ('value', call())
    except Exception as exc:  # noqa: BLE001 - compared, never swallowed silently
        return ('error', type(exc), str(exc))


class OneReportAtATimeTests(unittest.TestCase):
    def phase(self, phase, indexes, config=None):
        reports = tuple(generated_report(i, config) for i in indexes)
        upstream = f.UpstreamInputProvenance(phase, tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports),
                                             CROSS, FINAL)
        return reports, upstream

    def test_adapt_phase_equals_adapt_configs_and_track_a_references(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        coverage = {'coverage_manifest_sha256': COVERAGE}
        development = self.phase('development', range(10))
        validation = self.phase('validation', range(10, 18), identity('EXP-75-02'))
        cases = (('development', development, adapter.PREDICTIVE_CONFIGS, development_folds(development[0])),
                 ('validation', validation, adapter.PREDICTIVE_CONFIGS, None),
                 ('validation', validation, (identity('EXP-75-02'), identity()), None),
                 ('validation', validation, (), None))
        for phase, (reports, upstream), identities, folds in cases:
            with self.subTest(phase=phase, identities=len(identities)):
                expected = (cli.adapt_configs(reports, upstream, identities, folds),
                            cli.track_a_references(reports, upstream), upstream)
                with patch.object(cli, 'for_each_phase_report', side_effect=phase_visitor(reports, upstream)) as loader:
                    actual = cli.adapt_phase(manifest, coverage, 'output', phase, REVISION, CROSS, FINAL,
                                             identities, folds)
                self.assertEqual(loader.call_args.args, (manifest, coverage, 'output', phase, REVISION, CROSS, FINAL))
                self.assertEqual(actual, expected)
                self.assertEqual(tuple(actual[0]), tuple(expected[0]))  # identity order too

    def test_adapt_phase_requires_the_complete_roster(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports, upstream = self.phase('validation', range(10, 18))

        def one_fewer(*inputs, visit):
            for report, source in zip(reports[:-1], upstream.periods):
                visit(report, source)
            return upstream

        with patch.object(cli, 'for_each_phase_report', side_effect=one_fewer), \
                self.assertRaisesRegex(ValueError, 'complete phase report roster'):
            cli.adapt_phase(manifest, {'coverage_manifest_sha256': COVERAGE}, 'output', 'validation',
                            REVISION, CROSS, FINAL, (identity(),))

    def test_verified_and_indexed_adapt_period_equals_default(self):
        fixtures = [generated_report(10)] + [generated_report(10, config) for config in adapter.PREDICTIVE_CONFIGS]
        for number, report in enumerate(fixtures):
            source = adapter.period_provenance(report, CROSS, FINAL)
            index = adapter.index_report(report)
            # The shared fixture runs every identity; each own-config fixture runs its config.
            identities = adapter.PREDICTIVE_CONFIGS if number == 0 else (adapter.PREDICTIVE_CONFIGS[number - 1],)
            for config in identities:
                with self.subTest(fixture=number, config=config):
                    self.assertEqual(
                        outcome(lambda: adapter.adapt_period(report, source, config, report_verified=True, index=index)),
                        outcome(lambda: adapter.adapt_period(report, source, config)))

    def test_index_report_preserves_identity_and_v1_order(self):
        report = generated_report(10, identity('EXP-75-02'))
        report['candidate_evidence'] = report['candidate_evidence'] + [deepcopy(report['candidate_evidence'][0])]
        index = adapter.index_report(report)
        self.assertEqual(index.v1_records, [r for r in report['candidate_evidence'] if r['experiment_id'] == 'V1'])
        for key, records in index.by_identity.items():
            self.assertEqual(records, [r for r in report['candidate_evidence'] if adapter._identity(r) == key])
        self.assertTrue(all(a is b for a, b in zip(index.v1_records, (r for r in report['candidate_evidence']
                                                                     if r['experiment_id'] == 'V1'))))

    def test_report_verified_skips_only_the_report_rehash(self):
        report = generated_report(10)
        source = adapter.period_provenance(report, CROSS, FINAL)
        for verified, expected in ((False, 1), (True, 0)):
            with self.subTest(report_verified=verified), \
                    patch.object(part_b, '_verify_hashed_payload', wraps=part_b._verify_hashed_payload) as verify:
                adapter.adapt_period(report, source, identity(), report_verified=verified)
                self.assertEqual(sum(1 for c in verify.call_args_list if c.args[1] == 'report_sha256'), expected)
        with self.assertRaisesRegex(ValueError, 'adapter source report SHA mismatch'):
            adapter.adapt_period(report, replace(source, period_report_sha256='0' * 64), identity(),
                                 report_verified=True)
        tampered = dict(report, code_revision='other')
        with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
            adapter.adapt_period(tampered, source, identity(), report_verified=True)

    def test_for_each_phase_report_loads_then_visits_one_at_a_time(self):
        import weakref
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports = tuple(generated_report(i) for i in range(10, 18))

        class Report(dict):
            """weakref-able copy of a generated report."""

        events, alive, pending = [], [], list(reports)

        def load(*inputs, **options):
            self.assertTrue(all(ref() is None for ref in alive), 'previous report still referenced')
            report = Report(pending.pop(0))
            alive.append(weakref.ref(report))
            events.append('load')
            return report

        def visit(report, source):
            events.append('visit')

        with period_files('validation') as root, patch.object(part_b, 'load_finalized_period_report', side_effect=load):
            upstream = adapter.for_each_phase_report(manifest, {'coverage_manifest_sha256': COVERAGE},
                root, 'validation', REVISION, CROSS, FINAL, visit=visit)
        self.assertEqual(events, ['load', 'visit'] * 8)
        self.assertEqual(upstream, f.UpstreamInputProvenance('validation',
            tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL))

        events.clear()
        pending[:] = reports

        def failing(report, source):
            events.append('visit')
            if len(events) == 4:
                raise ValueError('synthetic visit failure')

        with period_files('validation') as root, \
                patch.object(part_b, 'load_finalized_period_report', side_effect=lambda *a, **k: (
                    events.append('load'), pending.pop(0))[1]), \
                self.assertRaisesRegex(ValueError, 'synthetic visit failure'):
            adapter.for_each_phase_report(manifest, {'coverage_manifest_sha256': COVERAGE},
                root, 'validation', REVISION, CROSS, FINAL, visit=failing)
        self.assertEqual(events, ['load', 'visit', 'load', 'visit'])

    def test_for_each_phase_report_requires_every_file_before_any_load(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        visit = []
        with period_files('validation', missing=(14,)) as root, \
                patch.object(part_b, 'load_finalized_period_report') as loader, \
                self.assertRaisesRegex(ValueError, r'validation period 14 report is missing: .*periods'):
            adapter.for_each_phase_report(manifest, {'coverage_manifest_sha256': COVERAGE},
                root, 'validation', REVISION, CROSS, FINAL, visit=lambda report, source: visit.append(source))
        loader.assert_not_called()
        self.assertEqual(visit, [])

    def test_load_phase_reports_unchanged(self):
        manifest = part_b.load_study_manifest(MANIFEST_PATH)
        reports = tuple(generated_report(i) for i in range(10, 18))
        with period_files('validation') as root, patch.object(part_b, 'load_finalized_period_report', side_effect=reports):
            loaded, upstream = adapter.load_phase_reports(manifest, {'coverage_manifest_sha256': COVERAGE},
                root, 'validation', REVISION, CROSS, FINAL)
        self.assertIsInstance(loaded, tuple)
        self.assertEqual(len(loaded), len(reports))
        self.assertTrue(all(a is b for a, b in zip(loaded, reports)))
        self.assertEqual(upstream, f.UpstreamInputProvenance('validation',
            tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports), CROSS, FINAL))


class AnalysisTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = part_b.load_study_manifest(MANIFEST_PATH)
        cls.coverage = {'coverage_manifest_sha256': COVERAGE}
        cls.hmm = identity('EXP-75-09')

    def period(self, index):
        return self.manifest.selected_periods[index]

    def build(self, report, index, identities, cross=None, final=None):
        return tables.build_period_table(report, self.manifest, self.period(index), report_file_sha256='0' * 64,
                                         cross_fit_sha=cross, final_model_sha=final, identities=identities)

    def write_tables(self, table_dir, reports, identities, cross=None, final=None):
        for report in reports:
            index = report['period']['study_period_index']
            tables.write_period_table(tables.table_path(table_dir, self.period(index)),
                                      self.build(report, index, identities, cross, final))

    def from_reports(self, phase, reports, identities, folds=None):
        upstream = f.UpstreamInputProvenance(phase, tuple(adapter.period_provenance(r, CROSS, FINAL) for r in reports),
                                             CROSS, FINAL)
        with patch.object(cli, 'for_each_phase_report', side_effect=phase_visitor(reports, upstream)):
            return cli.adapt_phase(self.manifest, self.coverage, 'output', phase, REVISION, CROSS, FINAL,
                                   identities, folds)

    def from_tables(self, table_dir, phase, identities, folds=None, cross=CROSS, final=FINAL):
        return tables.adapt_phase_from_tables(self.manifest, self.coverage, table_dir, phase, REVISION,
                                              cross, final, identities, folds)

    def test_build_write_load_round_trip_and_identity_checks(self):
        period = self.period(10)
        table = self.build(generated_report(10), 10, (identity(),), CROSS, FINAL)
        with TemporaryDirectory() as root:
            path = tables.table_path(root, period)
            tables.write_period_table(path, table)
            tables.write_period_table(path, table)  # identical content is a safe resume
            with self.assertRaises(part_b.StudyArtifactConflictError):
                tables.write_period_table(path, dict(table, report_file_sha256='1' * 64))
            self.assertEqual(tables.load_period_table(path, self.manifest, period, code_revision=REVISION,
                                                      coverage_sha256=COVERAGE), table)
            other_manifest = SimpleNamespace(manifest_sha256='0' * 64, study_version=self.manifest.study_version)
            for manifest, target, revision, coverage in ((other_manifest, period, REVISION, COVERAGE),
                                                         (self.manifest, period, 'other-revision', COVERAGE),
                                                         (self.manifest, period, REVISION, '0' * 64),
                                                         (self.manifest, self.period(11), REVISION, COVERAGE)):
                with self.subTest(revision=revision, coverage=coverage, period=target.study_period_index), \
                        self.assertRaises(part_b.StudyArtifactConflictError):
                    tables.load_period_table(path, manifest, target, code_revision=revision, coverage_sha256=coverage)
            tampered = json.loads(path.read_text())
            tampered['report_file_sha256'] = '2' * 64
            path.write_text(canonical_study_json(tampered) + '\n')
            with self.assertRaisesRegex(ValueError, 'analysis table SHA-256 mismatch'):
                tables.load_period_table(path, self.manifest, period, code_revision=REVISION)
            path.write_text(part_b._artifact_json(dict(table, table_version='other-version'), 'table_sha256') + '\n')
            with self.assertRaises(part_b.StudyArtifactConflictError):
                tables.load_period_table(path, self.manifest, period, code_revision=REVISION)

    def test_development_tables_equal_reports_including_hmm_join(self):
        for config in (None, self.hmm):
            reports = tuple(generated_report(i, config) for i in range(10))
            folds = development_folds(reports)
            expected = self.from_reports('development', reports, adapter.PREDICTIVE_CONFIGS, folds)
            with self.subTest(config=config), TemporaryDirectory() as table_dir:
                self.write_tables(table_dir, reports, adapter.PREDICTIVE_CONFIGS)
                actual = self.from_tables(table_dir, 'development', adapter.PREDICTIVE_CONFIGS, folds)
                self.assertEqual(actual, expected)
                self.assertEqual(tuple(actual[0]), tuple(expected[0]))
                # The stored rows never include development EXP-75-09.
                stored = tables.load_period_table(tables.table_path(table_dir, self.period(0)), self.manifest,
                                                  self.period(0), code_revision=REVISION)['adapted']
                self.assertNotIn(self.hmm, [io.decode(entry['identity']) for entry in stored])
        self.assertTrue(any(item.day.observations for item in actual[0][self.hmm]))

    def test_validation_tables_equal_reports_for_the_derived_subset(self):
        identities = (identity('EXP-75-02'), identity(), self.hmm)
        for config in (identity('EXP-75-02'), self.hmm):
            reports = tuple(generated_report(i, config) for i in range(10, 18))
            expected = self.from_reports('validation', reports, identities)
            with self.subTest(config=config), TemporaryDirectory() as table_dir:
                self.write_tables(table_dir, reports, identities, CROSS, FINAL)
                actual = self.from_tables(table_dir, 'validation', identities)
                self.assertEqual(actual, expected)
                self.assertEqual(tuple(actual[0]), tuple(expected[0]))

    def test_non_development_tables_fail_closed(self):
        report = generated_report(10)
        with self.assertRaisesRegex(ValueError, 'frozen final HMM'):
            self.build(report, 10, (identity(),), CROSS, '0' * 64)
        with self.assertRaises(ValueError):
            self.build(report, 10, (identity(),))
        with self.assertRaises(ValueError):
            self.build(generated_report(0), 0, (identity(),), CROSS, FINAL)
        reports = tuple(generated_report(i) for i in range(10, 18))
        with TemporaryDirectory() as table_dir:
            self.write_tables(table_dir, reports, (identity(),), CROSS, FINAL)
            with self.assertRaisesRegex(ValueError, 'different HMM prerequisites'):
                self.from_tables(table_dir, 'validation', (identity(),), final='0' * 64)
            with self.assertRaisesRegex(ValueError, 'lacks the requested config'):
                self.from_tables(table_dir, 'validation', (identity('EXP-75-02'),))
            tables.table_path(table_dir, self.period(13)).unlink()
            with patch.object(tables, 'load_period_table') as loader, \
                    self.assertRaisesRegex(ValueError, 'validation period 13 analysis table is missing'):
                self.from_tables(table_dir, 'validation', (identity(),))
            loader.assert_not_called()

    def test_slim_hmm_join_equals_full_report_and_self_checks(self):
        report = generated_report(0, self.hmm)
        fold = development_folds((report,))[0]
        source = adapter.period_provenance(report, CROSS, FINAL)
        join = tables.development_hmm_join(report)
        header = {key: report.get(key) for key in tables.HEADER_KEYS}
        full = adapter.adapt_period(report, source, self.hmm, held_out_fold=fold)
        slim = adapter.adapt_period({**header, **join}, source, self.hmm, held_out_fold=fold, report_verified=True)
        self.assertEqual(slim, full)
        self.assertTrue(full.day.observations)
        windows = join['candidate_evidence'][0]['native_evidence']['classification']['windows']
        self.assertEqual([entry[0] for entry in windows], [5])
        with patch.object(tables, '_slim_v1_native_evidence',
                          side_effect=lambda native: {'classification': {**native['classification'], 'windows': []}}), \
                self.assertRaisesRegex(ValueError, 'slim V1 classification differs'):
            tables.development_hmm_join(report)

    def write_report(self, root, index, report):
        path = Path(root) / 'part-b' / part_b.PERIOD_DIRECTORY / part_b._period_filename(self.period(index))
        path.parent.mkdir(parents=True)
        path.write_text(canonical_study_json(report))
        return path

    def test_verify_period_table_passes_fresh_and_fails_on_altered_row(self):
        period, identities = self.period(10), (identity(), identity('EXP-75-02'))
        with TemporaryDirectory() as root:
            path = self.write_report(root, 10, generated_report(10))
            table = tables.derive_period_table(path, self.manifest, self.coverage, period, REVISION,
                cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)
            table_file = tables.write_period_table(tables.table_path(Path(root) / 'tables', period), table)
            tables.verify_period_table(path, table_file, self.manifest, self.coverage, period, REVISION,
                cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)
            altered = deepcopy(table)
            altered['adapted'][0]['exclusions'] = io.encode((('synthetic exclusion', 1),))
            table_file.write_text(part_b._artifact_json(altered, 'table_sha256') + '\n')
            with self.assertRaises(ValueError):
                tables.verify_period_table(path, table_file, self.manifest, self.coverage, period, REVISION,
                    cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)

    def sidecar(self, root, index):
        return Path(root) / 'part-b' / '.period-manifests' / part_b._period_filename(self.period(index))

    def test_derive_rejects_rows_that_do_not_survive_the_json_round_trip(self):
        def drifting(content):
            table = json.loads(content)
            fields = table['adapted'][0]['day']['fields']
            fields['study_period_index'] = float(fields['study_period_index'])  # 10 read back as 10.0
            return table

        report = generated_report(10)
        self.build(report, 10, (identity(),), CROSS, FINAL)  # clean round trip
        with patch.object(tables, '_parse_sealed', side_effect=drifting), \
                self.assertRaisesRegex(ValueError, 'analysis table row does not survive the JSON round trip'):
            self.build(report, 10, (identity(),), CROSS, FINAL)

    def test_verify_period_table_rejects_int_float_drift(self):
        period, identities = self.period(10), (identity(),)
        with TemporaryDirectory() as root:
            path = self.write_report(root, 10, generated_report(10))
            table = tables.derive_period_table(path, self.manifest, self.coverage, period, REVISION,
                cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)
            table_file = tables.write_period_table(tables.table_path(Path(root) / 'tables', period), table)
            verify = lambda: tables.verify_period_table(path, table_file, self.manifest, self.coverage, period,
                REVISION, cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)
            verify()
            self.assertTrue(json.loads(table_file.read_text())['adapted'][0]['contexts'])

            # Default-path audit: a context bucket recomputed as 1.0 instead of 1 compares equal
            # with ==, but not as canonical JSON.
            original = adapter.adapt_period

            def drifting(report, source, identity, **options):
                item = original(report, source, identity, **options)
                if options:
                    return item  # the derive path (report_verified/index) stays exact
                return adapter.AdaptedDay(item.day, tuple((c[0], c[1], float(c[2]), c[3]) for c in item.contexts),
                                          item.exclusions)

            with patch.object(adapter, 'adapt_period', side_effect=drifting), \
                    self.assertRaisesRegex(ValueError, 'differs from the default adapter path'):
                verify()

            # Stored table: one int rewritten as a float, correctly re-sealed.
            altered = deepcopy(table)
            fields = altered['adapted'][0]['day']['fields']
            fields['study_period_index'] = float(fields['study_period_index'])
            table_file.write_text(part_b._artifact_json(altered, 'table_sha256') + '\n')
            with self.assertRaises(ValueError):
                verify()

    def test_verify_requires_the_existing_sidecar_and_never_writes_one(self):
        period, identities = self.period(10), (identity(),)
        report = generated_report(10)
        with TemporaryDirectory() as root:
            path = self.write_report(root, 10, report)
            table = tables.build_period_table(report, self.manifest, period, report_file_sha256='0' * 64,
                cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)
            table_file = tables.write_period_table(tables.table_path(Path(root) / 'tables', period), table)
            with patch.object(part_b, 'load_finalized_period_report') as loader, \
                    self.assertRaisesRegex(ValueError, 'requires the existing period sidecar'):
                tables.verify_period_table(path, table_file, self.manifest, self.coverage, period, REVISION,
                    cross_fit_sha=CROSS, final_model_sha=FINAL, identities=identities)
            loader.assert_not_called()
            self.assertFalse(self.sidecar(root, 10).parent.exists())

    def test_derive_creates_a_missing_sidecar(self):
        period = self.period(10)
        with TemporaryDirectory() as root:
            path = self.write_report(root, 10, generated_report(10))
            self.assertFalse(self.sidecar(root, 10).exists())
            table = tables.derive_period_table(path, self.manifest, self.coverage, period, REVISION,
                cross_fit_sha=CROSS, final_model_sha=FINAL, identities=(identity(),))
            sidecar = part_b._read_json(self.sidecar(root, 10))
            self.assertEqual(sidecar['report_file_sha256'], table['report_file_sha256'])
            self.assertEqual(sidecar['report_sha256'], table['report_header']['report_sha256'])

    def table_args(self, index, *extra):
        return cli.build_cli_parser().parse_args([
            'derive-table', '--study-manifest', 'manifest', '--coverage-manifest', 'coverage',
            '--part-b-output-dir', 'part-b', '--analysis-table-dir', 'tables', '--part-b-revision', REVISION,
            '--period-index', str(index), *extra])

    def test_table_cli_gates_run_before_any_report_is_opened(self):
        development = unavailable_freeze()
        validation = empty_validation(development)
        authorization = core.authorize_test(development, validation)
        parents = ('--development-freeze', 'dev', '--hmm-crossfit-index', 'index', '--final-hmm-model', 'model',
                   '--validation-freeze', 'val', '--test-authorization', 'auth')
        cases = ((replace(authorization, validation_freeze_sha256='0' * 64), 'not the exact current aggregate'),
                 (authorization, 'no authorized test members'))
        for supplied, message in cases:
            with self.subTest(message=message), \
                    patch.object(cli, 'verify_inputs', return_value=(self.manifest, self.coverage)), \
                    patch.object(cli, 'read_artifact', side_effect=((development, {}), (validation, {}), (supplied, {}))), \
                    patch.object(cli, 'load_hmm_prerequisites') as prerequisites, \
                    patch.object(part_b, 'load_finalized_period_report') as loader, \
                    self.assertRaisesRegex(ValueError, message):
                cli.run_table_command(self.table_args(18, *parents))
            prerequisites.assert_not_called()
            loader.assert_not_called()
        with patch.object(cli, 'verify_inputs', return_value=(self.manifest, self.coverage)), \
                patch.object(cli, 'read_artifact') as reader, \
                patch.object(part_b, 'load_finalized_period_report') as loader, \
                self.assertRaisesRegex(ValueError, '--development-freeze is required'):
            cli.run_table_command(self.table_args(10, '--hmm-crossfit-index', 'index', '--final-hmm-model', 'model'))
        reader.assert_not_called()
        loader.assert_not_called()

    def test_phase_runs_with_tables_never_open_reports(self):
        manifest = self.manifest
        reports = tuple(generated_report(i) for i in range(10))
        folds = development_folds(reports)
        expected = self.from_reports('development', reports, adapter.PREDICTIVE_CONFIGS, folds)
        args = SimpleNamespace(study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION,
            hmm_crossfit_index='index', final_hmm_model='model', part_b_output_dir=None,
            analysis_table_dir='tables', output='dev')
        with patch.object(cli, 'verify_inputs', return_value=(manifest, self.coverage)), \
                patch.object(cli, 'load_hmm_prerequisites', return_value=(CROSS, FINAL, folds)) as prerequisites, \
                patch.object(tables, 'adapt_phase_from_tables', return_value=expected) as from_tables, \
                patch.object(cli, 'for_each_phase_report') as from_reports, \
                patch.object(cli, 'write_artifact') as writer:
            cli.run_development(args)
        from_reports.assert_not_called()
        self.assertEqual(from_tables.call_args.args[2:4], ('tables', 'development'))
        self.assertEqual(prerequisites.call_args.kwargs['table_dir'], 'tables')
        self.assertEqual(writer.call_args.kwargs['track_a'], expected[1])

        development = unavailable_freeze()
        validation_reports = tuple(generated_report(i) for i in range(10, 18))
        validation_args = SimpleNamespace(development_freeze='dev', study_manifest='manifest',
            coverage_manifest='coverage', part_b_revision=REVISION, hmm_crossfit_index='index',
            final_hmm_model='model', part_b_output_dir=None, analysis_table_dir='tables', output='val',
            test_authorization_output='auth')
        with patch.object(cli, 'read_artifact', return_value=(development, {})), \
                patch.object(cli, 'verify_inputs', return_value=(manifest, self.coverage)), \
                patch.object(cli, 'load_hmm_prerequisites', return_value=(CROSS, FINAL, {})), \
                patch.object(tables, 'adapt_phase_from_tables',
                             return_value=self.from_reports('validation', validation_reports, ())) as from_tables, \
                patch.object(cli, 'for_each_phase_report') as from_reports, \
                patch.object(cli, 'write_artifact'):
            cli.run_validation(validation_args)
        from_reports.assert_not_called()
        self.assertEqual(from_tables.call_args.args[2:4], ('tables', 'validation'))

        development = nominated_freeze()
        validation, _ = nominated_validation(development)
        authorization = core.authorize_test(development, validation)
        members = tuple(m.identity for m in authorization.members if m.status == 'AUTHORIZED')
        test_reports = tuple(generated_report(i, development.predictive_pairs[0].identity) for i in range(18, 30))
        metadata = {'track_a': (), 'layer_one': (), 'exclusions': ()}
        test_args = SimpleNamespace(development_freeze='dev', validation_freeze='val', test_authorization='auth',
            study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION,
            part_b_output_dir=None, analysis_table_dir='tables', output='report')
        with patch.object(cli, 'read_artifact', side_effect=((development, metadata), (validation, metadata),
                                                             (authorization, {}))), \
                patch.object(cli, 'verify_inputs', return_value=(manifest, self.coverage)), \
                patch.object(tables, 'adapt_phase_from_tables',
                             return_value=self.from_reports('test', test_reports, members)) as from_tables, \
                patch.object(cli, 'for_each_phase_report') as from_reports, \
                patch.object(cli, 'write_artifact'):
            cli.run_test(test_args)
        from_reports.assert_not_called()
        self.assertEqual(from_tables.call_args.args[2:4], ('tables', 'test'))

    def test_phase_runs_require_a_report_or_table_source(self):
        args = SimpleNamespace(study_manifest='manifest', coverage_manifest='coverage', part_b_revision=REVISION,
            hmm_crossfit_index='index', final_hmm_model='model', part_b_output_dir=None,
            analysis_table_dir=None, output='dev')
        with patch.object(cli, 'verify_inputs') as inputs, \
                self.assertRaisesRegex(ValueError, '--part-b-output-dir is required without --analysis-table-dir'):
            cli.run_development(args)
        inputs.assert_not_called()


if __name__ == '__main__':
    unittest.main()
