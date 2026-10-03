"""Artifact-only #123 Part-C phase orchestration.

Run with ``python -m market_analysis.historical_market_state_study_part_c``.
There are intentionally no archive, download, replay or candidate-runner inputs.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import mean, median

from . import historical_market_state_study_execution as part_b
from . import historical_market_state_study_evaluation as core
from .historical_market_state_hmm_crossfit import validate_hmm_crossfit_index, HMM_CROSSFIT_DIRECTORY
from .historical_market_state_study_adapter import PREDICTIVE_CONFIGS, load_phase_reports, adapt_period, period_provenance
from .historical_market_state_study_artifacts import (
    TrackAReference, ScientificTestReport, read_artifact, write_artifact,
)
from .historical_market_state_study_features import (
    FROZEN_EVALUATION_PLAN, FROZEN_STUDY_MANIFEST_SHA256, PRIMARY_CONFIRMATORY_FAMILY,
    PartBScientificContract, EvidenceValueDay, layer_one_stratifier, predictive_family,
)


def verify_inputs(study_manifest, coverage_manifest, revision):
    manifest = part_b.load_study_manifest(study_manifest)
    if manifest.manifest_sha256 != FROZEN_STUDY_MANIFEST_SHA256:
        raise ValueError('Part-C requires the exact frozen study manifest')
    coverage = part_b.load_and_validate_coverage(coverage_manifest, manifest, revision)
    return manifest, coverage


def load_hmm_prerequisites(manifest, coverage, revision, index_path, model_path):
    index_path = Path(index_path)
    index = validate_hmm_crossfit_index(index_path, manifest,
        coverage_sha256=coverage['coverage_manifest_sha256'], code_revision=revision)
    model = part_b.load_frozen_hmm_model(model_path, manifest, coverage, revision)
    folds = {}
    for entry in index['ordered_folds']:
        fold = part_b._read_json(index_path.parent / HMM_CROSSFIT_DIRECTORY / entry['fold_file'])
        digest = part_b._verify_hashed_payload(fold, 'artifact_sha256', 'held-out HMM fold')
        if digest != entry['artifact_sha256']:
            raise ValueError('HMM fold changed after index validation')
        folds[entry['study_period_index']] = fold
    final_payload = part_b._read_json(Path(model_path))
    part_b._verify_hashed_payload(final_payload, 'artifact_sha256', 'final HMM prerequisite')
    if (final_payload['model_sha256'] != model.model_sha256
            or tuple(final_payload['training_block_sha256s']) != tuple(
                folds[i]['held_out_period']['training_block_sha256'] for i in range(10))
            or tuple(p['period_report_sha256'] for p in final_payload['ordered_development_periods']) != tuple(
                folds[i]['held_out_period']['period_report_sha256'] for i in range(10))):
        raise ValueError('final HMM and cross-fit index bind different development evidence')
    return index['index_sha256'], model.model_sha256, folds


def require_prerequisites(development, manifest, coverage, revision, cross_fit_sha=None, final_model_sha=None):
    contract = development.upstream_provenance.shared_contract
    expected = PartBScientificContract(manifest.study_version, manifest.manifest_sha256,
        coverage['coverage_manifest_sha256'], part_b.PERIOD_REPORT_SCHEMA_VERSION, revision,
        cross_fit_sha or development.hmm_cross_fit_sha256,
        final_model_sha or development.hmm_final_model_sha256)
    if contract != expected or development.plan != FROZEN_EVALUATION_PLAN:
        raise ValueError('frozen Part-C parent/prerequisite identity mismatch')
    if set(r.identity for r in development.config_results) != set(PREDICTIVE_CONFIGS):
        raise ValueError('development artifact does not bind the exact implementation configs')


def _report_source_pairs(reports, upstream):
    if len(reports) != len(upstream.periods):
        raise ValueError('artifact adapter requires the complete phase report roster')
    pairs = tuple(zip(reports, upstream.periods))
    if any(period_provenance(r, upstream.hmm_cross_fit_sha256, upstream.hmm_final_model_sha256) != p
           for r, p in pairs):
        raise ValueError('phase report/source provenance mismatch')
    return pairs


def track_a_references(reports, upstream):
    return tuple(TrackAReference(p.study_period_index, p.utc_date, p.phase, p.period_report_sha256,
        tuple(r['native_state_quality_summaries']), part_b._digest(r['native_state_quality_summaries']))
        for r, p in _report_source_pairs(reports, upstream))


def adapt_configs(reports, upstream, identities, folds=None):
    pairs = _report_source_pairs(reports, upstream)
    return {identity: tuple(adapt_period(report, source, identity,
        held_out_fold=(folds or {}).get(source.study_period_index))
        for report, source in pairs) for identity in identities}


def exclusion_records(adapted):
    return tuple({'family_id': identity.family_id, 'algorithm_version': identity.algorithm_version,
        'config_version': identity.config_version, 'study_period_index': item.day.study_period_index,
        'phase': item.day.phase, 'scheduled_primary_count': item.day.scheduled_primary_count,
        'usable_observations': len(item.day.observations), 'exclusions': item.exclusions}
        for identity, days in adapted.items() for item in days)


def layer_one_description(development, adapted, selected_days=None):
    groups = defaultdict(list)
    for identity, items in adapted.items():
        spec = layer_one_stratifier(identity.family_id)
        binding = next((b for b in development.layer_one_terciles if b.identity == identity), None)
        if spec.mode == 'CONTINUOUS_TERCILE' and binding is None:
            continue
        feature_index = (next(i for i, feature in enumerate(predictive_family(identity.family_id).candidate_features)
                              if feature.name == spec.candidate_feature_name)
                         if spec.mode == 'CONTINUOUS_TERCILE' else None)
        allowed = None if selected_days is None else {
            (d.study_period_index, r.observation_key) for d in selected_days.get(identity, ()) for r in d.observations}
        for item in items:
            contexts = {c[0]: c[1:] for c in item.contexts}
            for row in item.day.observations:
                if allowed is not None and (row.study_period_index, row.observation_key) not in allowed:
                    continue
                direction, utc_bucket, native = contexts[row.observation_key]
                category = binding.bins.assign(row.candidate_features[feature_index]) if binding else native
                if category is None or (spec.mode == 'NATIVE_CATEGORY' and category not in spec.allowed_categories):
                    raise ValueError('unregistered Layer-1 native category')
                groups[(identity.family_id, identity.config_version, row.phase, direction, utc_bucket, category)].append(
                    (row.study_period_index, row.outcome))
    return tuple({'family_id': key[0], 'config_version': key[1], 'phase': key[2],
        'v1_direction': key[3], 'utc_six_hour_bucket': key[4], 'candidate_category': key[5],
        'observation_count': len(rows), 'day_count': len({i for i, _ in rows}),
        'outcome_mean': mean(y for _, y in rows), 'outcome_median': median(y for _, y in rows)}
        for key, rows in sorted(groups.items()))


def run_development(args):
    manifest, coverage = verify_inputs(args.study_manifest, args.coverage_manifest, args.part_b_revision)
    cross_sha, final_sha, folds = load_hmm_prerequisites(manifest, coverage, args.part_b_revision,
                                                       args.hmm_crossfit_index, args.final_hmm_model)
    reports, upstream = load_phase_reports(manifest, coverage, args.part_b_output_dir, 'development',
                                          args.part_b_revision, cross_sha, final_sha)
    adapted = adapt_configs(reports, upstream, PREDICTIVE_CONFIGS, folds)
    results, nominations, samples, pairs, evidence, bins = [], [], [], [], [], []
    selected_days = {}
    for family in PRIMARY_CONFIRMATORY_FAMILY:
        configs = tuple(i for i in PREDICTIVE_CONFIGS if i.family_id == family)
        common, family_results, nomination = core.evaluate_development_family(configs,
            tuple(item.day for identity in configs for item in adapted[identity]))
        samples.append(common)
        results.extend(family_results)
        nominations.append(nomination)
        if nomination.status != 'NOMINATED':
            continue
        days = next(sample.days for sample in common.samples if sample.identity == nomination.identity)
        selected_days[nomination.identity] = days
        pairs.append(core.fit_final_development_pair(nomination, days))
        stratifier = layer_one_stratifier(family)
        if stratifier.mode != 'CONTINUOUS_TERCILE':
            continue
        feature_index = next(i for i, feature in enumerate(predictive_family(family).candidate_features)
                             if feature.name == stratifier.candidate_feature_name)
        source = core.LayerOneContinuousEvidence(nomination.identity, stratifier,
            tuple(EvidenceValueDay(d.study_period_index, d.utc_date, d.phase,
                  tuple(r.candidate_features[feature_index] for r in d.observations)) for d in days),
            tuple((d.study_period_index, d.source_provenance.provenance_sha256) for d in days))
        evidence.append(source)
        bins.append(core.freeze_layer_one_bins(source))
    development = core.DevelopmentFreeze(manifest.manifest_sha256, tuple(results), tuple(nominations),
        tuple(pairs), tuple(samples), upstream, layer_one_terciles=tuple(bins), layer_one_evidence=tuple(evidence),
        hmm_cross_fit_sha256=cross_sha, hmm_final_model_sha256=final_sha)
    nominated = {i: adapted[i] for i in selected_days}
    write_artifact(args.output, 'development', development, track_a=track_a_references(reports, upstream),
        layer_one=layer_one_description(development, nominated, selected_days), exclusions=exclusion_records(adapted))
    return development


def run_validation(args):
    development, _ = read_artifact(args.development_freeze, 'development')
    manifest, coverage = verify_inputs(args.study_manifest, args.coverage_manifest, args.part_b_revision)
    cross_sha, final_sha, _ = load_hmm_prerequisites(manifest, coverage, args.part_b_revision,
                                                   args.hmm_crossfit_index, args.final_hmm_model)
    require_prerequisites(development, manifest, coverage, args.part_b_revision, cross_sha, final_sha)
    reports, upstream = load_phase_reports(manifest, coverage, args.part_b_output_dir, 'validation',
                                          args.part_b_revision, cross_sha, final_sha)
    adapted = adapt_configs(reports, upstream, tuple(p.identity for p in development.predictive_pairs))
    decisions = tuple(core.evaluate_validation(pair, tuple(d.day for d in adapted[pair.identity]), upstream, development)
                      for pair in development.predictive_pairs)
    validation = core.freeze_validation(development, decisions, upstream)
    authorization = core.authorize_test(development, validation)
    write_artifact(args.output, 'validation', validation, track_a=track_a_references(reports, upstream),
                   layer_one=layer_one_description(development, adapted), exclusions=exclusion_records(adapted))
    write_artifact(args.test_authorization_output, 'authorization', authorization)
    return validation, authorization


def run_test(args):
    # Parent reconstruction and exact aggregate authorization precede ALL test report I/O.
    development, dev_metadata = read_artifact(args.development_freeze, 'development')
    validation, val_metadata = read_artifact(args.validation_freeze, 'validation')
    supplied, _ = read_artifact(args.test_authorization, 'authorization')
    if core.authorize_test(development, validation) != supplied:
        raise ValueError('supplied authorization is not the exact current aggregate authorization')
    manifest, coverage = verify_inputs(args.study_manifest, args.coverage_manifest, args.part_b_revision)
    require_prerequisites(development, manifest, coverage, args.part_b_revision)
    members = tuple(m for m in supplied.members if m.status == 'AUTHORIZED')
    upstream, results, test_track_a, descriptive, exclusions = None, (), (), (), ()
    if members:
        reports, upstream = load_phase_reports(manifest, coverage, args.part_b_output_dir, 'test',
            args.part_b_revision, development.hmm_cross_fit_sha256, development.hmm_final_model_sha256)
        adapted = adapt_configs(reports, upstream, tuple(m.identity for m in members))
        results = tuple(core.evaluate_test(development, validation, supplied, member.family_id,
            tuple(d.day for d in adapted[member.identity]), upstream) for member in members)
        test_track_a = track_a_references(reports, upstream)
        descriptive = layer_one_description(development, adapted)
        exclusions = exclusion_records(adapted)
    report = ScientificTestReport(development.freeze_sha256, validation.freeze_sha256, supplied, upstream,
        results, core.study_primary_holm(supplied, results), development.config_results,
        development.nominations, validation.decisions,
        core.final_family_evidence_summaries(supplied, development.config_results,
            development.nominations, validation.decisions, results))
    write_artifact(args.output, 'test', report,
        track_a=dev_metadata['track_a'] + val_metadata['track_a'] + test_track_a,
        layer_one=dev_metadata['layer_one'] + val_metadata['layer_one'] + descriptive,
        exclusions=dev_metadata['exclusions'] + val_metadata['exclusions'] + exclusions)
    return report


def build_cli_parser():
    parser = argparse.ArgumentParser(description='Artifact-only frozen historical study Part C')
    commands = parser.add_subparsers(dest='phase', required=True)
    for phase in ('development', 'validation', 'test'):
        command = commands.add_parser(phase)
        for name in ('study-manifest', 'coverage-manifest', 'part-b-output-dir', 'output'):
            command.add_argument('--' + name, type=Path, required=True)
        command.add_argument('--part-b-revision', required=True, help='Pinned artifact producer revision; independent of consumer HEAD')
        if phase != 'test':
            command.add_argument('--hmm-crossfit-index', type=Path, required=True)
            command.add_argument('--final-hmm-model', type=Path, required=True)
        if phase != 'development':
            command.add_argument('--development-freeze', type=Path, required=True)
        if phase == 'validation':
            command.add_argument('--test-authorization-output', type=Path, required=True)
        if phase == 'test':
            command.add_argument('--validation-freeze', type=Path, required=True)
            command.add_argument('--test-authorization', type=Path, required=True)
    return parser


def main(argv=None):
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    try:
        {'development': run_development, 'validation': run_validation, 'test': run_test}[args.phase](args)
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f'Part-C artifact error: {exc}\n')
    print(f'Frozen {args.phase} artifact: {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
