"""Portable, exact-period operational slices and campaign validation.

Budgets/status/lineage are separate from scientific requests and report hashes.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import signal
import shutil
import time

from . import historical_market_state_study_execution as execution
from .historical_study_bundles import (
    BASE, SOURCES, identifier, sha, file_sha, regular, read_bundle, safe_relative,
    install_files, verify_recovery_tree, classify_recovery_inventory,
)
from .historical_study_runtime import current_runtime_implementation_revision

CAMPAIGN_VERSION = 'historical-study-campaign-v1'
STATUS_VERSION = 'historical-study-slice-status-v1'
LAYOUT_VERSION = 'crypto-study-absolute-layout-v1'


def positive(value, name):
    if type(value) is not int or value <= 0:
        raise ValueError(name + ' must be a positive integer')
    return value


def campaign_spec(path):
    spec = execution._read_json(Path(path))
    if spec.get('version') != CAMPAIGN_VERSION:
        raise ValueError('unsupported campaign schema')
    identifier(spec['campaign_id'])
    sha(spec['runtime_sha'], 40)
    sha(spec['orchestration_sha'], 40)
    sha(spec['producer_revision'], 40)
    for key in ('study_manifest_sha256', 'coverage_manifest_sha256',
                'study_file_sha256', 'coverage_file_sha256'):
        sha(spec[key])
    if spec['runtime_sha'] != spec['orchestration_sha']:
        raise ValueError('initial platform requires runtime and orchestration at the same pinned trusted commit')
    for name in ('requirements.txt', 'requirements-research.txt'):
        sha(spec['dependency_locks'][name])
        if file_sha(BASE / 'repo' / 'python' / name) != spec['dependency_locks'][name]:
            raise ValueError('dependency lock pin mismatch')
    for name in ('max_transfer_bytes', 'max_uncompressed_bytes', 'headroom_bytes'):
        positive(spec['limits'][name], name)
    for name in ('ceiling_minutes', 'max_runs', 'no_progress_cap'):
        positive(spec['budget'][name], name)
    if spec['budget']['ceiling_minutes'] > 120 and not spec.get('expanded_budget_authorized', False):
        raise ValueError('campaign beyond pilot ceiling requires explicit expanded budget authorization')
    return spec


def campaign_identity(spec):
    return {'campaign_id': spec['campaign_id'], 'campaign_spec_sha256': execution._digest(spec),
            'runtime_sha': spec['runtime_sha'], 'orchestration_sha': spec['orchestration_sha'],
            'producer_revision': spec['producer_revision'],
            'study_manifest_sha256': spec['study_manifest_sha256'],
            'coverage_manifest_sha256': spec['coverage_manifest_sha256'],
            'dependency_locks': spec['dependency_locks'], 'layout_version': LAYOUT_VERSION,
            'expected_membership': spec['expected_membership'], 'inputs': spec['inputs']}


def frozen_inputs(spec):
    study = BASE / 'manifests/study.json'
    coverage_path = BASE / 'manifests/coverage.json'
    if (file_sha(study) != spec['study_file_sha256']
            or file_sha(coverage_path) != spec['coverage_file_sha256']):
        raise ValueError('original frozen manifest/coverage bytes changed')
    manifest = execution.load_study_manifest(study)
    from .historical_market_state_study_features import FROZEN_STUDY_MANIFEST_SHA256
    if manifest.manifest_sha256 != FROZEN_STUDY_MANIFEST_SHA256:
        raise ValueError('platform requires the exact frozen study manifest')
    coverage = execution.load_and_validate_coverage(coverage_path, manifest, spec['producer_revision'])
    if (manifest.manifest_sha256 != spec['study_manifest_sha256']
            or coverage['coverage_manifest_sha256'] != spec['coverage_manifest_sha256']):
        raise ValueError('frozen campaign identity mismatch')
    for phase in ('development', 'validation', 'test'):
        indices = spec['expected_membership'][phase]
        if (not isinstance(indices, list) or len(indices) != len(set(indices))
                or indices != sorted(indices)):
            raise ValueError('expected membership must be sorted and unique')
        for index in indices:
            execution.select_execution_periods(manifest, phase, period_index=index, allow_test=phase == 'test')
    return manifest, coverage


def prerequisites(spec, manifest, coverage, phase, allow_test):
    """Authorize phases before raw archive access or opening any test report."""
    if phase == 'development':
        if allow_test:
            raise ValueError('allow_test requires the explicit test phase')
        return None
    from .historical_market_state_study_artifacts import read_artifact
    from .historical_market_state_study_part_c import load_hmm_prerequisites, require_prerequisites
    from .historical_market_state_study_evaluation import authorize_test
    root = BASE / 'manifests/prerequisites'
    development, _ = read_artifact(root / 'development.json', 'development')
    model = root / 'hmm-model.json'
    crossfit, final, _ = load_hmm_prerequisites(manifest, coverage, spec['producer_revision'],
                                               root / 'crossfit-index.json', model)
    require_prerequisites(development, manifest, coverage, spec['producer_revision'], crossfit, final)
    if phase == 'test':
        if allow_test is not True:
            raise ValueError('test requires explicit allow_test authorization')
        validation, _ = read_artifact(root / 'validation.json', 'validation')
        supplied, _ = read_artifact(root / 'authorization.json', 'authorization')
        if authorize_test(development, validation) != supplied:
            raise ValueError('test authorization differs from exact validated phase parents')
        if validation.upstream_provenance.shared_contract != development.upstream_provenance.shared_contract:
            raise ValueError('validation parent scientific/HMM contract mismatch')
    elif allow_test:
        raise ValueError('allow_test requires the explicit test phase')
    return model


def disk_sample(root):
    usage = shutil.disk_usage(BASE)
    categories = {}
    for name, directory in (('inputs', BASE / 'inputs'), ('transfers', BASE / 'transfers'),
                            ('checkpoints', root / 'checkpoints'), ('outputs', root / 'outputs')):
        categories[name] = sum(p.stat().st_size for p in directory.rglob('*') if p.is_file()) if directory.exists() else 0
    categories['disposable_sqlite_bytes'] = sum(p.stat().st_size for directory in Path('/tmp').glob('binance-core-verify-*')
        for p in directory.rglob('*') if p.is_file())
    return {'free_bytes': usage.free, 'used_bytes': usage.used, 'sampled_directory_bytes': categories}


class SliceYield(Exception):
    """Only raised at a known durable parent boundary, never for corruption."""


class SliceController:
    def __init__(self, max_new_stages, max_elapsed_seconds, status_path, identity, root, headroom):
        positive(max_new_stages, 'max_new_stages')
        positive(max_elapsed_seconds, 'max_elapsed_seconds')
        self.limit, self.seconds = max_new_stages, max_elapsed_seconds
        self.started = time.monotonic()
        self.path, self.root, self.headroom = Path(status_path), root, headroom
        self.new, self.reused, self.checkpoints = [], [], []
        self.last, self.pending = None, None
        self.high_water = 0
        self.status = {'version': STATUS_VERSION, 'identity': identity,
                       'started_at_utc': datetime.now(timezone.utc).isoformat(),
                       'scientific_state': 'FAILED', 'remote_published': False,
                       'yield_reason': None, 'failure_category': None}

    def reason(self):
        if len(self.new) >= self.limit:
            return 'max-new-stages'
        if time.monotonic() - self.started >= self.seconds:
            return 'max-elapsed-seconds'
        if shutil.disk_usage(BASE).free < self.headroom:
            return 'disk-headroom'
        return None

    def save(self):
        disk = disk_sample(self.root)
        self.high_water = max(self.high_water, disk['used_bytes'])
        self.status.update(last_verified_unit=self.last, newly_completed_stages=self.new,
                           reused_stages=self.reused, newly_written_checkpoints=self.checkpoints,
                           monotonic_elapsed_seconds=time.monotonic() - self.started,
                           observed_at_utc=datetime.now(timezone.utc).isoformat(),
                           disk=disk, sampled_disk_high_water_bytes=self.high_water)
        execution._replace_atomic(self.path, execution._canonical(self.status))

    def stop(self, reason):
        self.status.update(scientific_state='YIELDED', yield_reason=reason)
        self.save()
        raise SliceYield(reason)

    def observe(self, event, details):
        if event == 'REPLAY_CHECKPOINT_WRITTEN':
            self.last = {'kind': 'checkpoint', **details}
            self.checkpoints.append(details['checkpoint_sha256'])
            reason = self.reason()
            if details['completed_output_boundaries'] == details['total_output_boundaries']:
                self.pending = reason  # publish COMPLETE spool/prepared data first
            elif reason:
                self.stop(reason)
        elif event == 'PREPARED_REPLAY_WRITTEN' and self.pending:
            self.stop(self.pending)
        elif event in ('POST_REPLAY_STAGE_COMPLETED', 'POST_REPLAY_STAGE_REUSED'):
            self.last = {'kind': 'stage', **details}
            (self.new if event.endswith('COMPLETED') else self.reused).append(
                {'stage_id': details['stage_id'], 'sha256': details['stage_result_sha256']})
            if event.endswith('COMPLETED') and self.reason():
                self.stop(self.reason())
        elif event in ('BEFORE_NEW_STAGE', 'BEFORE_PERIOD_FINALIZATION', 'CORE_ARCHIVE_INDEX_READY'):
            if self.reason():
                self.stop(self.reason())
        elif event in ('PERIOD_ARTIFACT_FINALIZED', 'SKIPPED_EXISTING_ARTIFACT'):
            self.last = {'kind': 'period', **details}
        self.save()


def preflight(spec, manifest, coverage, period, inventory):
    expected = {'study_manifest_sha256': manifest.manifest_sha256,
                'coverage_manifest_sha256': coverage['coverage_manifest_sha256'],
                'producer_revision': spec['producer_revision'], 'period': execution.report_json_safe(period),
                'source_identities': coverage['source_identities'],
                'study_file_sha256': spec['study_file_sha256'], 'coverage_file_sha256': spec['coverage_file_sha256']}
    if inventory['kind'] != 'inputs' or inventory['identity'] != expected:
        raise ValueError('selected input inventory does not match campaign/frozen period')
    from .historical_study_bundles import planned_packages
    declared = {row['path']: row for row in inventory['files'] if row['path'].startswith('inputs/')}
    expected_names = set()
    frozen_period = coverage['periods'][period.study_period_index]
    for source, packages in planned_packages(period).items():
        summary = frozen_period['core'] if source == 'core' else frozen_period['sources'][source]
        facts = {row['package_name']: row for row in summary['packages']}
        if facts and set(facts) != {name for name, _ in packages}:
            raise ValueError('package planner/frozen membership mismatch')
        for name, symbol in packages:
            frozen_fact = facts.get(name)
            for checksum in ((False, True) if source != 'liquidation' else (False,)):
                relative = name + ('.CHECKSUM' if checksum else '')
                destination = f'inputs/{SOURCES[source]}/{relative}'
                expected_names.add(destination)
                row = declared.get(destination)
                if frozen_fact is None and row is not None:
                    frozen_fact = row['facts']['frozen_package']
                    if frozen_fact.get('frozen_source_summary') != summary or frozen_fact.get('sha256') is not None:
                        raise ValueError('unknown package facts do not bind the exact frozen loader failure')
                if row is None or row['facts']['frozen_package'] != frozen_fact:
                    raise ValueError('input package facts differ from frozen coverage')
                present = bool(frozen_fact.get('checksum_present') if checksum else frozen_fact['archive_present'])
                if (row['absent'] != (not present) or row['facts']['source'] != source
                        or row['facts']['canonical_filename'] != relative or row['facts']['symbol'] != symbol
                        or row['facts']['partition'] != f'period-{period.study_period_index}'
                        or row['facts']['source_root'] != str(BASE / 'inputs' / SOURCES[source])
                        or (not checksum and frozen_fact.get('sha256') is not None
                            and row['sha256'] != frozen_fact['sha256'])):
                    raise ValueError('input asset/absence/hash/partition binding mismatch')
    if set(declared) != expected_names:
        raise ValueError('undeclared selected-period package membership')
    for row in inventory['files']:
        path = BASE / row['path']
        if row['absent']:
            if path.exists() or path.is_symlink():
                raise ValueError('original source absence changed')
        elif regular(path).st_size != row['size'] or file_sha(path) != row['sha256']:
            raise ValueError('installed input byte identity mismatch')
    roots = {name: BASE / 'inputs' / folder for name, folder in SOURCES.items() if name != 'core'}
    sources = execution._load_source_evidence(period, roots)
    frozen = coverage['periods'][period.study_period_index]['sources']
    for name, value in sources.items():
        if execution._canonical(value['coverage']) != execution._canonical(frozen[name]):
            raise ValueError('selected source coverage differs from original frozen facts')
    # No aggTrade pre-scan: raw trade scientific validation belongs to execution.
    if shutil.disk_usage(BASE).free < spec['limits']['headroom_bytes']:
        raise ValueError('insufficient disk for SQLite/spool/recovery headroom')


def aggregate(spec, manifest, coverage, phase, root):
    expected = spec['expected_membership'][phase]
    if not expected:
        raise ValueError('aggregation requires explicit nonempty expected membership')
    found = {}
    for path in (root / 'outputs' / execution.PERIOD_DIRECTORY).glob('*.json'):
        period = next((p for p in manifest.selected_periods if path.name == execution._period_filename(p)), None)
        if period is None or period.study_period_index in found:
            raise ValueError('unknown/duplicate finalized report filename')
        report = execution.load_finalized_period_report(path, manifest, period,
            coverage_sha256=coverage['coverage_manifest_sha256'], code_revision=spec['producer_revision'])
        found[period.study_period_index] = report['report_sha256']
    actual = sorted(index for index in found if manifest.selected_periods[index].phase == phase)
    if actual != expected:
        raise ValueError('finalized reports do not match exact declared aggregation membership')
    execution._update_execution_index(root / 'outputs', manifest, coverage, spec['producer_revision'])
    return {'phase': phase, 'expected_membership': expected, 'report_hashes': {str(i): found[i] for i in expected},
            'complete_phase': len(expected) == len([p for p in manifest.selected_periods if p.phase == phase])}


def run(args):
    from .historical_run_directory import owned_run_directory
    spec = campaign_spec(args.campaign_spec)
    root = BASE / 'campaigns' / spec['campaign_id']
    with owned_run_directory(root, cleanup=False):
        try:
            return _run_owned(args)
        except BaseException as exc:
            import traceback
            execution._replace_atomic(root / 'operations/last-failure.txt', traceback.format_exc())
            execution._replace_atomic(root / 'operations/status.json', execution._canonical({
                'version': STATUS_VERSION, 'identity': campaign_identity(spec),
                'phase': args.phase, 'requested_period_index': args.period_index,
                'scientific_state': 'FAILED', 'failure_category': type(exc).__name__,
                'remote_published': False, 'last_verified_unit': None, 'disk': disk_sample(root)}))
            return 1


def _run_owned(args):
    spec = campaign_spec(args.campaign_spec)
    if current_runtime_implementation_revision() != spec['runtime_sha']:
        raise ValueError('actual clean runtime HEAD differs from campaign pin')
    manifest, coverage = frozen_inputs(spec)
    root = BASE / 'campaigns' / spec['campaign_id']
    root.mkdir(parents=True, exist_ok=True)
    period = None
    if args.operation not in ('aggregate', 'validate-parents') or args.period_index is not None:
        period, = execution.select_execution_periods(manifest, args.phase, period_index=args.period_index,
                                                     allow_test=args.allow_test)
        if period.study_period_index not in spec['expected_membership'][args.phase]:
            raise ValueError('period is outside declared campaign membership')
    model = prerequisites(spec, manifest, coverage, args.phase, args.allow_test)
    if args.operation == 'validate-parents':
        return 0
    if args.restore_manifest:
        recovery = read_bundle(args.restore_manifest, args.restore_sha)
        if recovery['kind'] != 'recovery' or recovery['identity'] != campaign_identity(spec):
            raise ValueError('recovery campaign/runtime/lock/layout identity mismatch')
        # Even aggregation/development dispatch cannot inspect a restored test
        # report without reconstructing exact test authorization first.
        included = classify_recovery_inventory(recovery, manifest, spec['campaign_id'],
                                               spec['expected_membership'])
        phases = {item.phase for item in included}
        if 'test' in phases:
            if args.phase != 'test':
                raise ValueError('later test evidence requires its explicit authorized phase')
            prerequisites(spec, manifest, coverage, 'test', args.allow_test)
        if 'validation' in phases and args.phase == 'development':
            raise ValueError('later validation evidence requires its explicit phase prerequisites')
        _, work = verify_recovery_tree(args.restore_staging, spec['campaign_id'], manifest, coverage,
                                       campaign_identity(spec))
        if work != recovery['metadata']['completed_work']:
            raise ValueError('recovery committed-work inventory mismatch')
        install_files(recovery, args.restore_staging)
    identity = {**campaign_identity(spec), 'phase': args.phase,
                'period': execution.report_json_safe(period) if period else None}
    controller = SliceController(args.max_new_stages, args.max_elapsed_seconds,
                root / 'operations/status.json', identity, root, spec['limits']['headroom_bytes'])
    if args.restore_manifest:
        controller.status['restored_completed_work'] = recovery['metadata']['completed_work']
        previous_status = execution._read_json(root / 'operations/status.json') if (root / 'operations/status.json').exists() else {}
        controller.last = previous_status.get('last_verified_unit')
    controller.save()
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError('supervisor deadline')))
    try:
        if args.operation == 'aggregate':
            controller.status['aggregation'] = aggregate(spec, manifest, coverage, args.phase, root)
            controller.status['scientific_state'] = 'FINALIZED_PERIOD'
        else:
            locator = spec['inputs'][str(args.period_index)]
            inventory = read_bundle(BASE / 'transfers/input-manifest.json', locator['manifest_sha256'])
            preflight(spec, manifest, coverage, period, inventory)
            if args.operation == 'execute-period':
                execution.execute_study_periods(manifest, BASE / 'manifests/coverage.json', BASE / 'inputs/core',
                    root / 'outputs', phase=args.phase, period_index=args.period_index, allow_test=args.allow_test,
                    code_revision=spec['producer_revision'], mark_archive_root=BASE / 'inputs/mark',
                    open_interest_archive_root=BASE / 'inputs/open-interest', funding_archive_root=BASE / 'inputs/funding',
                    liquidation_archive_root=BASE / 'inputs/liquidation', hmm_model_path=model,
                    checkpoint_dir=root / 'checkpoints', runtime_report_path=root / 'operations/runtime.jsonl',
                    progress_report_path=root / 'operations/progress.jsonl', slice_controller=controller)
                controller.status['scientific_state'] = 'FINALIZED_PERIOD'
            else:
                controller.status.update(scientific_state='YIELDED', yield_reason='preflight-only')
        controller.save()
        return 0
    except SliceYield:
        return 0
    except BaseException as exc:
        import traceback
        execution._replace_atomic(root / 'operations/last-failure.txt', traceback.format_exc())
        controller.status.update(scientific_state='FAILED', failure_category=type(exc).__name__)
        controller.save()
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['validate-parents', 'preflight', 'execute-period', 'aggregate'])
    parser.add_argument('--campaign-spec', type=Path, required=True)
    parser.add_argument('--phase', choices=['development', 'validation', 'test'], required=True)
    parser.add_argument('--period-index', type=int)
    parser.add_argument('--allow-test', action='store_true')
    parser.add_argument('--max-new-stages', type=int, default=1)
    parser.add_argument('--max-elapsed-seconds', type=int, default=2400)
    parser.add_argument('--restore-manifest', type=Path)
    parser.add_argument('--restore-sha')
    parser.add_argument('--restore-staging', type=Path)
    args = parser.parse_args()
    if args.operation not in ('aggregate', 'validate-parents') and args.period_index is None:
        parser.error('an exact period index is required')
    if args.operation == 'aggregate' and args.period_index is not None:
        parser.error('aggregation uses declared expected membership, not period-index')
    try:
        return run(args)
    except (OSError, ValueError, RuntimeError):
        # Details and data never reach public stdout. Transport/supervisor records
        # a sanitized failure category and leaves private diagnostics available.
        print('Study operation failed validation; inspect private diagnostics and campaign pins.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
