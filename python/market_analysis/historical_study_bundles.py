"""Sealed, streaming inventories. No replay, acquisition or coverage refreeze.

Bundles are ordered raw file chunks, not tar archives: there is no executable
archive metadata, decompressor expansion or link extraction surface.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat

from . import historical_market_state_study_execution as execution

BASE = Path('/tmp/crypto-study')
V1 = 'historical-study-file-bundle-v1'
VERSION = 'historical-study-file-bundle-v2'
PART_LIMIT = 1024 ** 3
BLOCK = 1024 ** 2


class BundleFootprintError(ValueError):
    def __init__(self, reason, **measurements):
        super().__init__(reason)
        self.measurements = {'reason': reason, **measurements}


SOURCES = {'core': 'core', 'mark_trade': 'mark', 'open_interest': 'open-interest',
           'funding': 'funding', 'liquidation': 'liquidation'}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}', value):
        raise ValueError('use a bounded alphanumeric campaign/generation identifier')
    return value


def sha(value, length=64):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{%d}' % length, value):
        raise ValueError('a full lowercase SHA pin is required')
    return value


def safe_relative(value):
    path = PurePosixPath(value)
    if (not isinstance(value, str) or not value or path.is_absolute()
            or not path.parts or '..' in path.parts or path.as_posix() != value or '\\' in value
            or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid canonical relative bundle path')
    return path


def regular(path):
    path = Path(path)
    # Reject links in every component, not only the leaf.
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise ValueError('bundle paths may not contain symbolic links')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('bundle files must be regular files without hardlinks')
    return info


def file_sha(path, check=None):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(BLOCK), b''):
            if check:
                check()
            digest.update(chunk)
    return digest.hexdigest()


def sealed(body):
    return {**body, 'bundle_sha256': execution._digest(body)}


def asset_table(value):
    """Unique required assets; v1 inventories retain their original hashes."""
    if value['version'] == VERSION:
        needed = {part['asset'] for row in value['files'] for part in row['extents']}
        return {row['asset']: row for row in value['assets'] if row['asset'] in needed}
    return {part['asset']: part for row in value['files'] for part in row['parts']}


def _partition(path, facts, identity):
    if path.startswith('manifests/'):
        return 'metadata'
    parts = safe_relative(path).parts
    if path.startswith('inputs/'):
        period = identity.get('period', {})
        return 'inputs:' + str(period.get('phase', 'unspecified')) + ':' + str(facts.get('partition', period.get('study_period_index', 'unspecified')))
    if len(parts) > 3 and parts[0] == 'campaigns' and parts[2] in ('checkpoints', 'outputs'):
        return parts[2] + ':' + parts[3]
    return 'operational'


def validate_bundle(value, *, selected=False):
    if value['version'] not in (V1, VERSION):
        raise ValueError('unsupported bundle version')
    names, used, ranges = set(), set(), {}
    table = {}
    if value['version'] == VERSION:
        for asset in value['assets']:
            if set(asset) != {'asset', 'size', 'sha256', 'partition'}:
                raise ValueError('invalid asset table fields')
            identifier(asset['asset'])
            sha(asset['sha256'])
            if (asset['asset'] in table or type(asset['size']) is not int
                    or not 0 < asset['size'] <= PART_LIMIT or not isinstance(asset['partition'], str)):
                raise ValueError('invalid asset table size/membership')
            table[asset['asset']] = asset
    total = 0
    for row in value['files']:
        name = str(safe_relative(row['path']))
        if name in names or type(row['absent']) is not bool or type(row['size']) is not int or row['size'] < 0:
            raise ValueError('invalid duplicate path/absence/size')
        names.add(name)
        parts = row['parts'] if value['version'] == V1 else row['extents']
        if row['absent']:
            if parts or row['size'] != 0 or row['sha256'] is not None:
                raise ValueError('invalid absence inventory')
            continue
        sha(row['sha256'])
        offset = 0
        for part in parts:
            identifier(part['asset'])
            if value['version'] == V1:
                sha(part['sha256'])
                if (part['asset'] in used or type(part['size']) is not int
                        or not 0 < part['size'] <= PART_LIMIT or type(part['offset']) is not int
                        or part['offset'] != offset):
                    raise ValueError('invalid v1 chunk ordering/size/asset')
                offset += part['size']
            else:
                if set(part) != {'asset', 'asset_offset', 'file_offset', 'length'}:
                    raise ValueError('invalid extent fields')
                asset = table.get(part['asset'])
                if (asset is None or any(type(part[k]) is not int for k in ('asset_offset', 'file_offset', 'length'))
                        or part['file_offset'] != offset or part['asset_offset'] < 0 or part['length'] <= 0
                        or part['asset_offset'] + part['length'] > asset['size']
                        or asset['partition'] != _partition(name, row['facts'], value['identity'])):
                    raise ValueError('invalid extent coverage/bounds/partition')
                ranges.setdefault(part['asset'], []).append((part['asset_offset'], part['asset_offset'] + part['length']))
                offset += part['length']
            used.add(part['asset'])
        if offset != row['size']:
            raise ValueError('invalid file extent coverage')
        total += offset
    if value['version'] == VERSION:
        for name, intervals in ranges.items():
            end = 0
            for start, stop in sorted(intervals):
                if start < end or (not selected and start != end):
                    raise ValueError('overlapping/incomplete asset extents')
                end = stop
            if not selected and end != table[name]['size']:
                raise ValueError('unclaimed packed asset bytes')
        if not selected and used != set(table):
            raise ValueError('unreferenced asset inventory')
        transfer = sum(table[name]['size'] for name in used)
    else:
        transfer = total
    if (type(value['uncompressed_bytes']) is not int or type(value['transfer_bytes']) is not int
            or total != value['uncompressed_bytes'] or transfer != value['transfer_bytes']):
        raise ValueError('bundle measured footprint mismatch')
    return value


def read_bundle(path, expected):
    sha(expected)
    value = execution._read_json(Path(path))
    execution._verify_hashed_payload(value, 'bundle_sha256', 'bundle')
    if value['bundle_sha256'] != expected:
        raise ValueError('trusted inventory hash mismatch')
    return validate_bundle(value)


def pack_files(files, destination, *, kind, identity, metadata=None, check=None,
               max_bytes=12 * 1024 ** 3):
    """Deterministic bounded raw packing, isolated by metadata/period/phase."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    rows, assets, total, names = [], [], 0, set()
    output = None
    partition = None
    asset_size = 0
    def finish():
        nonlocal output
        if output is not None:
            output.flush()
            os.fsync(output.fileno())
            output.close()
            assets.append({'asset': asset, 'size': asset_size, 'sha256': asset_digest.hexdigest(), 'partition': partition})
            output = None
    try:
        ordered = sorted(files, key=lambda x: (_partition(x[0], x[2], identity), x[0]))
        for relative, source, facts in ordered:
            relative = str(safe_relative(relative))
            if relative in names:
                raise ValueError('duplicate source inventory entry')
            names.add(relative)
            row = {'path': relative, 'facts': facts, 'absent': source is None,
                   'size': 0, 'sha256': None, 'extents': []}
            if source is not None:
                info = regular(source)
                total += info.st_size
                free = shutil.disk_usage(destination).free
                if total > max_bytes or free < info.st_size + BLOCK:
                    raise BundleFootprintError('bundle-packing-limit', measured_bytes=total, limit_bytes=max_bytes,
                                               free_bytes=free, required_next_file_bytes=info.st_size + BLOCK)
                group = _partition(relative, facts, identity)
                digest = hashlib.sha256()
                offset = 0
                with Path(source).open('rb') as handle:
                    while offset < info.st_size:
                        if output is not None and (partition != group or asset_size == PART_LIMIT):
                            finish()
                        if output is None:
                            partition = group
                            asset = 'pack-%06d' % len(assets)
                            output = (destination / asset).open('xb')
                            asset_digest = hashlib.sha256()
                            asset_size = 0
                        length = min(PART_LIMIT - asset_size, info.st_size - offset)
                        row['extents'].append({'asset': asset, 'asset_offset': asset_size, 'file_offset': offset, 'length': length})
                        remaining = length
                        while remaining:
                            if check:
                                check()
                            chunk = handle.read(min(BLOCK, remaining))
                            if not chunk:
                                raise ValueError('source changed while packing')
                            output.write(chunk)
                            digest.update(chunk)
                            asset_digest.update(chunk)
                            asset_size += len(chunk)
                            remaining -= len(chunk)
                        offset += length
                    if handle.read(1):
                        raise ValueError('source grew while packing')
                final_info = regular(source)
                if (final_info.st_size, final_info.st_mtime_ns, final_info.st_ino) != (info.st_size, info.st_mtime_ns, info.st_ino):
                    raise ValueError('source changed during streaming bundle creation')
                row.update(size=offset, sha256=digest.hexdigest())
                if facts.get('frozen_sha256') is not None and row['sha256'] != facts['frozen_sha256']:
                    raise ValueError('original package differs from frozen SHA')
            rows.append(row)
        finish()
    finally:
        if output is not None:
            output.close()
    value = sealed({'version': VERSION, 'kind': kind, 'identity': identity,
                    'metadata': metadata or {}, 'files': sorted(rows, key=lambda r: r['path']), 'assets': assets,
                    'uncompressed_bytes': total, 'transfer_bytes': sum(a['size'] for a in assets)})
    validate_bundle(value)
    execution._replace_atomic(destination / 'manifest.json', execution._canonical(value))
    return value


def unpack_files(value, parts, staging, *, max_bytes, headroom_bytes, check=None):
    validate_bundle(value, selected=True)
    staging = Path(staging)
    staging.mkdir(parents=True, exist_ok=False)
    total = value['uncompressed_bytes']
    free = shutil.disk_usage(staging).free
    if total > max_bytes or free < total + headroom_bytes:
        raise BundleFootprintError('staging-disk-limit', assembled_bytes=total, limit_bytes=max_bytes,
                                   free_bytes=free, required_bytes=total + headroom_bytes)
    # Shared assets are hashed once, never once per reconstructed file.
    for name, asset in asset_table(value).items():
        source = Path(parts) / identifier(name)
        if regular(source).st_size != asset['size'] or file_sha(source, check) != asset['sha256']:
            raise ValueError('downloaded asset size/SHA mismatch')
    for row in value['files']:
        path = staging.joinpath(*safe_relative(row['path']).parts)
        if row['absent']:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name('.' + path.name + '.reconstruct-tmp')
        digest = hashlib.sha256()
        try:
            with temporary.open('xb') as output:
                extents = row['extents'] if value['version'] == VERSION else [
                    {'asset': p['asset'], 'asset_offset': 0, 'length': p['size']} for p in row['parts']]
                for extent in extents:
                    with (Path(parts) / extent['asset']).open('rb') as handle:
                        handle.seek(extent['asset_offset'])
                        remaining = extent['length']
                        while remaining:
                            if check:
                                check()
                            chunk = handle.read(min(BLOCK, remaining))
                            if not chunk:
                                raise ValueError('truncated packed extent')
                            output.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            if regular(temporary).st_size != row['size'] or digest.hexdigest() != row['sha256']:
                raise ValueError('restored whole-file size/SHA mismatch')
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return staging


def install_files(value, staging):
    """Install only verified regular files, with conflict checks; never relocate JSON."""
    for row in value['files']:
        path = BASE.joinpath(*safe_relative(row['path']).parts)
        if row['absent']:
            if path.exists() or path.is_symlink():
                raise ValueError('frozen absent input unexpectedly exists')
            continue
        source = Path(staging) / row['path']
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError('link in stable installation path')
        if path.exists():
            if regular(path).st_size != row['size'] or file_sha(path) != row['sha256']:
                raise ValueError('existing stable file conflicts with verified bundle')
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, path)


def period_root(campaign, period):
    return BASE / 'campaigns' / identifier(campaign) / 'checkpoints' / (
        f'period-{period.study_period_index:02d}-{period.utc_date.isoformat()}-{period.phase}')


def classify_recovery_inventory(inventory, manifest, campaign, expected_membership):
    """Return original frozen periods named by canonical recovery file paths.

    Campaign IDs and generic operational filenames are opaque, not phase evidence.
    This inspects only sealed inventory metadata, never scientific file contents.
    """
    campaign = identifier(campaign)
    checkpoint_names = {period_root(campaign, period).name: period
                        for period in manifest.selected_periods}
    report_names = {execution._period_filename(period): period
                    for period in manifest.selected_periods}
    included = {}
    for row in inventory['files']:
        parts = safe_relative(row['path']).parts
        if len(parts) < 3 or parts[:2] != ('campaigns', campaign):
            raise ValueError('recovery inventory escapes the exact campaign root')
        tail = parts[2:]
        period = None
        if tail[0] == 'checkpoints':
            if len(tail) < 3 or tail[1] not in checkpoint_names:
                raise ValueError('unknown/malformed recovery checkpoint period directory')
            period = checkpoint_names[tail[1]]
        elif tail[0] == 'outputs' and len(tail) >= 2 and tail[1] in (
                execution.PERIOD_DIRECTORY, '.period-manifests'):
            if len(tail) != 3 or tail[2] not in report_names:
                raise ValueError('unknown/malformed recovery report or sidecar filename')
            period = report_names[tail[2]]
        if period is not None:
            if period.study_period_index not in expected_membership[period.phase]:
                raise ValueError('recovery period is outside declared campaign membership')
            included[period.study_period_index] = period
    return tuple(included[index] for index in sorted(included))


def last_verified_unit(files, work, index=None):
    """Derive claims ONLY from the transitive closure returned by validation."""
    units = []
    selectors = ['v1', *execution._candidate_stage_selectors(), 'event-context', 'outcomes']
    for path in files:
        if path.suffix != '.json' or path.name.endswith(('.observations.json', '.job.json', '.manifest.json')):
            continue
        if path.name.startswith('checkpoint-') or path.stem in selectors or path.parent.name == execution.PERIOD_DIRECTORY:
            value = execution._read_json(path)
            identity = value.get('identity', value.get('period', {}))
            number = identity.get('study_period_index', value.get('study_period_index'))
            if index is not None and number != index:
                continue
            for field, kind, rank in (('checkpoint_sha256', 'checkpoint', 0), ('stage_result_sha256', 'stage', 1), ('report_sha256', 'period', 2)):
                digest = value.get(field)
                if digest in work:
                    unit = {'kind': kind, 'sha256': digest, 'study_period_index': number}
                    if kind == 'checkpoint':
                        unit.update(checkpoint_sha256=digest, completed_boundary=value['boundary'],
                                    completed_output_boundaries=value['cumulative_point_count'])
                    if kind == 'stage':
                        unit.update(stage_id=identity['stage_id'], stage_result_sha256=digest)
                    position = value.get('boundary', 0) if kind == 'checkpoint' else selectors.index(path.stem) if kind == 'stage' else 0
                    units.append(((number if number is not None else -1, rank, position), unit))
    return max(units, key=lambda item: item[0])[1] if units else None



def planned_packages(period):
    from .binance_historical_archive import BinanceUSDMArchiveRequest, daily_kline_relative_path
    from .historical_mark_price_evidence import _package_days, daily_mark_price_relative_path
    from .historical_open_interest_evidence import open_interest_package_days, daily_open_interest_relative_path
    from .historical_funding_evidence import funding_package_months, monthly_funding_relative_path
    from .historical_liquidation_evidence import liquidation_package_days, daily_liquidation_relative_path
    config = execution.study_replay_config(period.utc_date)
    request = BinanceUSDMArchiveRequest(Path('/unused'), execution.study_universe(), config)
    result = {name: [] for name in SOURCES}
    for symbol, family, day, relative in execution._core_expected_packages(request):
        result['core'].append((str(relative), symbol))
    # Explicit label tail; dedup if required_kline_dates already includes it.
    end_day = datetime.fromtimestamp(period.end_boundary_time_ms / 1000, timezone.utc).date()
    for symbol in execution.ORDERED_SYMBOLS:
        result['core'].append((str(daily_kline_relative_path(symbol, end_day)), symbol))
        first = (period.start_boundary_time_ms + 59_999) // 60_000 * 60_000 - 16 * 60_000
        last = period.end_boundary_time_ms // 60_000 * 60_000
        result['mark_trade'].extend((str(daily_mark_price_relative_path(symbol, day)), symbol)
                                   for day in _package_days(first, last))
        result['open_interest'].extend((str(daily_open_interest_relative_path(symbol, day)), symbol)
                                     for day in open_interest_package_days(period.start_boundary_time_ms, period.end_boundary_time_ms))
        result['funding'].extend((str(monthly_funding_relative_path(symbol, month)), symbol)
                                for month in funding_package_months(period.start_boundary_time_ms, period.end_boundary_time_ms))
    result['liquidation'] = [(str(daily_liquidation_relative_path(day)), None)
                            for day in liquidation_package_days(period.start_boundary_time_ms, period.end_boundary_time_ms)]
    return {key: sorted(set(rows)) for key, rows in result.items()}


def metadata_paths(phase):
    names = [] if phase == 'development' else ['development', 'crossfit-index', 'hmm-model']
    if phase == 'test':
        names += ['validation', 'authorization']
    return {'manifests/study.json', 'manifests/coverage.json',
            *(f'manifests/prerequisites/{name}.json' for name in names)}


def prepare_inputs(manifest_path, coverage_path, phase, index, roots, destination, prerequisites):
    manifest = execution.load_study_manifest(manifest_path)
    coverage = execution.load_and_validate_coverage(coverage_path, manifest)
    period, = execution.select_execution_periods(manifest, phase, period_index=index, allow_test=phase == 'test')
    frozen = coverage['periods'][index]
    files = [('manifests/study.json', Path(manifest_path), {'role': 'study'}),
             ('manifests/coverage.json', Path(coverage_path), {'role': 'coverage'})]
    for name, source in prerequisites.items():
        safe_relative(name)
        if f'manifests/prerequisites/{name}.json' not in metadata_paths(phase):
            raise ValueError('metadata contains later-phase or unknown prerequisite evidence')
        files.append((f'manifests/prerequisites/{name}.json', Path(source), {'role': name}))
    for source, packages in planned_packages(period).items():
        summary = frozen['core'] if source == 'core' else frozen['sources'][source]
        expected = {row['package_name']: row for row in summary['packages']}
        if expected and set(expected) != {name for name, _ in packages}:
            raise ValueError('planner differs from frozen selected-period package membership')
        for name, symbol in packages:
            root = Path(roots[source])
            facts = expected.get(name)
            path = root / name
            if facts is None:
                # A whole-loader failure has no per-package identities. The
                # trusted owner inventory binds supplied original bytes and the
                # exact frozen failure; preflight must reproduce that failure.
                facts = {'archive_present': path.is_file(),
                         'checksum_present': Path(str(path) + '.CHECKSUM').is_file(),
                         'sha256': None, 'frozen_source_summary': summary}
            if facts['archive_present'] and not path.is_file():
                raise ValueError('missing original frozen package')
            for checksum in (False, True) if source != 'liquidation' else (False,):
                relative = name + ('.CHECKSUM' if checksum else '')
                original = root / relative
                present = facts.get('checksum_present') if checksum else facts['archive_present']
                if present and not original.is_file():
                    raise ValueError('missing original frozen checksum')
                # Later downloads in the owner's root are excluded when the
                # frozen facts recorded absence; never wipe owner archives.
                files.append((f'inputs/{SOURCES[source]}/{relative}', original if present else None,
                              {'source': source, 'source_root': str(BASE / 'inputs' / SOURCES[source]),
                               'symbol': symbol, 'partition': f'period-{index}',
                               'canonical_filename': relative, 'frozen_package': facts,
                               'frozen_sha256': facts.get('sha256') if not checksum else None}))
    identity = {'study_manifest_sha256': manifest.manifest_sha256,
                'coverage_manifest_sha256': coverage['coverage_manifest_sha256'],
                'producer_revision': coverage['code_revision'], 'period': execution.report_json_safe(period),
                'source_identities': coverage['source_identities'],
                'study_file_sha256': file_sha(manifest_path), 'coverage_file_sha256': file_sha(coverage_path)}
    return pack_files(files, destination, kind='inputs', identity=identity)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['prepare-input-bundle'])
    parser.add_argument('--study-manifest', type=Path, required=True)
    parser.add_argument('--coverage-manifest', type=Path, required=True)
    parser.add_argument('--phase', choices=['development', 'validation', 'test'], required=True)
    parser.add_argument('--period-index', type=int, required=True)
    for source in SOURCES:
        parser.add_argument('--' + source.replace('_', '-') + '-root', type=Path, required=True)
    parser.add_argument('--prerequisite', action='append', default=[], metavar='NAME=PATH')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    prerequisites = {}
    for pair in args.prerequisite:
        name, path = pair.split('=', 1)
        if name in prerequisites:
            raise ValueError('duplicate prerequisite name')
        prerequisites[name] = path
    result = prepare_inputs(args.study_manifest, args.coverage_manifest, args.phase, args.period_index,
                            {name: getattr(args, name + '_root') for name in SOURCES}, args.output, prerequisites)
    print('Inventory SHA-256:', result['bundle_sha256'])




# Recovery helpers are intentionally usable without any GitHub transport.
def _walk_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def verify_recovery_tree(base, campaign, manifest, coverage, identity, *, check=None, locks_held=False, validated=None):
    """Validate committed units at stable paths or a verified staging mirror.

    Reference resolution is validation-only. Stored requests are never rewritten.
    Return the complete transitive file closure of committed units and diagnostics.
    """
    from .historical_replay_runtime import ReplayCheckpointStore, _canonical_bytes, _sha
    from .historical_study_runtime import StudyPointStream, _stage_metadata, decode, encode
    from .historical_run_directory import owned_run_directory
    base = Path(base)
    campaign_root = base / 'campaigns' / identifier(campaign)
    stable_root = BASE / 'campaigns' / campaign
    files, work, reports = set(), [], {}

    def add(path):
        regular(path)
        files.add(Path(path))

    def resolve(path):
        supplied = Path(path)
        if not supplied.is_absolute() or '..' in supplied.parts:
            raise ValueError('recovery has an invalid absolute reference')
        try:
            relative = supplied.relative_to(stable_root)
        except ValueError:
            raise ValueError('recovery reference escapes the campaign') from None
        return campaign_root / relative

    def prefix(actual, period):
        expected = {'study_manifest_sha256': manifest.manifest_sha256,
                    'extension_coverage_manifest_sha256': coverage['coverage_manifest_sha256'],
                    'scientific_producer_revision': identity['producer_revision'],
                    'runtime_implementation_revision': identity['runtime_sha'],
                    'study_period_index': period.study_period_index,
                    'utc_date': period.utc_date.isoformat(), 'phase': period.phase}
        if any(actual.get(key) != value for key, value in expected.items()):
            raise ValueError('recovery scientific/runtime/period identity mismatch')
        eligibility = execution._frozen_eligibility(manifest, period)
        if (actual.get('archive_content_sha256') != eligibility.provenance.archive_manifest.content_sha256
                or actual.get('archive_files') != execution.report_json_safe(eligibility.provenance.archive_manifest.archive_files)):
            raise ValueError('recovery raw identities differ from frozen selection')

    checkpoints = campaign_root / 'checkpoints'
    roots = sorted(checkpoints.glob('period-*')) if checkpoints.exists() else []
    with ExitStack() as stack:
        for root in roots:
            if not root.is_dir() or root.is_symlink():
                raise ValueError('invalid period recovery directory')
            if not locks_held:
                stack.enter_context(owned_run_directory(root, cleanup=False))
        for root in roots:
            if check:
                check()
            period = next((p for p in manifest.selected_periods
                           if root.name == period_root(campaign, p).name), None)
            if period is None or period.study_period_index not in identity['expected_membership'][period.phase]:
                raise ValueError('unknown/undeclared checkpoint period')
            paths = sorted(root.glob('checkpoint-*.json'), key=lambda p: int(p.stem.split('-')[1]))
            if not paths:
                continue
            replay_identity = execution._read_json(paths[0])['identity']
            prefix(replay_identity, period)
            config = execution.study_replay_config(period.utc_date)
            store = ReplayCheckpointStore(root, replay_identity, config.output_start_boundary_time_ms,
                                          config.output_end_boundary_time_ms)
            if check:
                check()
            store.load_latest()  # existing complete chain/state/scientific point validation
            for path in paths:
                value = execution._read_json(path)
                add(path)
                add(root / 'chunks' / (value['point_chunk_sha256'] + '.jsonl'))
                add(root / 'states' / (value['replay_state_sha256'] + '.json'))
                work.append(value['checkpoint_sha256'])
            prepared = root / 'prepared-replay.json'
            stream = None
            if prepared.exists():
                value = execution._read_json(prepared)
                body = {key: item for key, item in value.items() if key != 'bundle_sha256'}
                if (prepared.read_bytes() != _canonical_bytes(value)
                        or value.get('bundle_sha256') != _sha(_canonical_bytes(body))):
                    raise ValueError('prepared replay canonical hash mismatch')
                if value['version'] != 'historical-prepared-replay-v1' or not store._complete:
                    raise ValueError('prepared replay is not COMPLETE')
                prefix(value['identity'], period)
                if value['identity'] != replay_identity:
                    raise ValueError('prepared/checkpoint identities differ')
                dataset = decode(value['dataset'])
                eligibility = execution._frozen_eligibility(manifest, period)
                if (dataset.config != config or dataset.universe != execution.study_universe()
                        or dataset.archive_manifest != eligibility.provenance.archive_manifest
                        or value['eligibility_sha256'] != eligibility.eligibility_sha256):
                    raise ValueError('prepared replay configuration/archive mismatch')
                if (execution._replay_identity(manifest, coverage, period, dataset, config, identity['producer_revision'],
                                              identity['runtime_sha']) != replay_identity
                        or decode(value['manifest']) != execution.historical_replay_run_manifest(dataset)
                        or dataset.taker_flow_evidence.algorithm_version != execution.TAKER_FLOW_ALGORITHM_VERSION):
                    raise ValueError('prepared scientific/normalized calculation identity mismatch')
                add(prepared)
                work.append(value['bundle_sha256'])
            if (root / 'study-points/manifest.json').exists():
                if not store._complete:
                    raise ValueError('compact spool lacks a COMPLETE replay')
                spool = root / 'study-points'
                spool_identity = execution._read_json(spool / 'manifest.json')['identity']
                expected_spool = {**replay_identity, 'final_replay_checkpoint_sha256': store.previous_sha,
                                  'point_count': store.point_count, 'first_boundary': store.start_boundary,
                                  'last_boundary': store.end_boundary}
                if spool_identity != expected_spool:
                    raise ValueError('spool identity differs from COMPLETE checkpoint')
                stream = StudyPointStream(spool, spool_identity)
                stream.validate()
                if validated is not None and prepared.exists():
                    from .historical_owned_validation import CompleteReplayValidation
                    closure = [prepared, spool / 'manifest.json', spool / 'points.jsonl', *paths]
                    for checkpoint_path in paths:
                        checkpoint = execution._read_json(checkpoint_path)
                        closure.extend((root / 'chunks' / (checkpoint['point_chunk_sha256'] + '.jsonl'),
                                        root / 'states' / (checkpoint['replay_state_sha256'] + '.json')))
                    validated[str(root.relative_to(base))] = CompleteReplayValidation(
                        replay_identity, store.previous_sha, store.point_count, store.start_boundary,
                        store.end_boundary, tuple(str(path.relative_to(root)) for path in closure))
                if check:
                    check()
                add(spool / 'manifest.json')
                add(spool / 'points.jsonl')
                work.append(stream.metadata['metadata_sha256'])
            stage_root = root / 'post-replay'
            if stage_root.exists():
                for path in sorted(stage_root.glob('*.json')):
                    if path.stem not in ('v1', 'event-context', 'outcomes', *execution._candidate_stage_selectors()):
                        continue
                    if stream is None or not prepared.exists():
                        raise ValueError('completed stage lacks verified prepared replay/spool')
                    value = execution._read_json(path)
                    prefix(value['identity'], period)
                    if path.stem != value['identity']['stage_id']:
                        raise ValueError('stage filename/identity mismatch')
                    descriptor = value['identity']['input_descriptor']
                    if (value['identity']['descriptor_sha256'] != _sha(_canonical_bytes(descriptor))
                            or descriptor.get('scientific_stage') != encode(execution._stage_science_identity(path.stem))):
                        raise ValueError('stage scientific descriptor/config identity mismatch')
                    result, _ = _stage_metadata(path, stream=stream, reference_path=resolve)
                    if path.stem == 'v1':
                        from dataclasses import replace
                        # Existing reader validates full shared metadata against
                        # this spool; stage metadata already verifies file SHA.
                        reader = replace(result[2], path=str(resolve(result[2].path))).reader(stream)
                        reader.handle.close()
                    add(path)
                    work.append(value['stage_result_sha256'])
                    for reference in _walk_strings(value['result']):
                        if reference.startswith(str(stable_root) + '/'):
                            target = resolve(reference)
                            add(target)
                            metadata = target.with_suffix('.manifest.json')
                            if metadata.exists():
                                add(metadata)
                    observed = path.with_suffix('.observations.json')
                    if observed.exists():
                        add(observed)
                for job_path in stage_root.glob('*.job.json'):
                    job = execution._read_json(job_path)
                    prefix(job['identity'], period)
                    request = resolve(job['request_path'])
                    if request.parent != stage_root or file_sha(request, check) != job['request_sha256']:
                        raise ValueError('failed-stage request diagnostics identity mismatch')
                    resolve(job['result_path'])
                    add(job_path)
                    add(request)
        output = campaign_root / 'outputs'
        period_dir = output / execution.PERIOD_DIRECTORY
        for path in sorted(period_dir.glob('*.json')):
            period = next((p for p in manifest.selected_periods if path.name == execution._period_filename(p)), None)
            if period is None or period.study_period_index not in identity['expected_membership'][period.phase]:
                raise ValueError('unknown/undeclared finalized report')
            report = execution.load_finalized_period_report(path, manifest, period,
                        coverage_sha256=coverage['coverage_manifest_sha256'], code_revision=identity['producer_revision'])
            execution._period_sidecar(path, manifest, coverage, period, identity['producer_revision'], report['report_sha256'])
            add(path)
            add(output / '.period-manifests' / path.name)
            work.append(report['report_sha256'])
            reports[period.study_period_index] = (path.name, report['report_sha256'])
        for directory in (campaign_root / 'operations',):
            if directory.exists():
                for path in directory.rglob('*'):
                    if path.is_file() and not path.name.startswith('.'):
                        add(path)
        index = output / execution.EXECUTION_INDEX_FILENAME
        if index.exists():
            value = execution._read_json(index)
            execution._verify_hashed_payload(value, 'index_sha256', 'execution index')
            if (value.get('index_version') != execution.EXECUTION_INDEX_VERSION
                    or value.get('study_manifest_sha256') != manifest.manifest_sha256
                    or value.get('extension_coverage_manifest_sha256') != coverage['coverage_manifest_sha256']
                    or value.get('code_revision') != identity['producer_revision']):
                raise ValueError('execution index identity mismatch')
            seen = set()
            for row in value['periods']:
                number = row['study_period_index']
                if number in seen or reports.get(number) != (row['report_file'], row['report_sha256']):
                    raise ValueError('index references missing/conflicting finalized report')
                seen.add(number)
            add(index)
        return files, sorted(set(work))


if __name__ == '__main__':
    main()
