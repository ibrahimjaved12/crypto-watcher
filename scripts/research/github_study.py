#!/usr/bin/env python3
"""Allowlisted GitHub transport and deadline supervisor; never scientific math.

Only metadata/inputs/restore/publish need RESEARCH_DATA_TOKEN. All numerical
execution is a token-free fresh subprocess with an explicitly bounded lifetime.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = Path('/tmp/crypto-study/repo')
sys.path.insert(0, str(REPO / 'python'))
from market_analysis import historical_market_state_study_execution as execution
from market_analysis.historical_study_batch import (
    BASE, STATUS_VERSION, campaign_spec, campaign_identity, frozen_inputs,
    prerequisites, disk_sample,
)
from market_analysis.historical_study_bundles import (
    BLOCK, PART_LIMIT, BundleFootprintError, identifier, sha, safe_relative, regular, file_sha,
    read_bundle, unpack_files, install_files, pack_files, verify_recovery_tree,
    classify_recovery_inventory, asset_table, validate_bundle, last_verified_unit, metadata_paths, planned_packages, period_root,
)
from market_analysis.historical_run_directory import owned_run_directory
from market_analysis import historical_operational_events as events


class Deadline:
    def __init__(self, epoch):
        self.end = time.monotonic() + epoch - time.time()

    def remaining(self):
        return self.end - time.monotonic()

    def check(self):
        if self.remaining() <= 0:
            raise TimeoutError('operational deadline expired')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class CheckedFile:
    def __init__(self, path, deadline):
        self.handle = Path(path).open('rb')
        self.deadline = deadline

    def read(self, size=-1):
        self.deadline.check()
        return self.handle.read(min(size if size >= 0 else BLOCK, BLOCK))

    def close(self):
        self.handle.close()


class GitHub:
    def __init__(self, repository, deadline):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or ''):
            raise ValueError('configure RESEARCH_DATA_REPOSITORY as owner/private-repo')
        if repository.lower() == os.environ.get('GITHUB_REPOSITORY', '').lower():
            raise ValueError('restricted data requires a separate private companion repository')
        self.repository, self.deadline = repository, deadline
        self.token = os.environ.get('RESEARCH_DATA_TOKEN')
        if not self.token:
            raise ValueError('configure the scoped RESEARCH_DATA_TOKEN secret')
        self.opener = urllib.request.build_opener(NoRedirect())
        self.prefix = 'https://api.github.com/repos/' + repository
        details = self.json(self.prefix)
        if details.get('private') is not True:
            raise ValueError('companion storage repository must be private')
        self.branch = details['default_branch']

    def response(self, url, *, accept='application/vnd.github+json', method='GET',
                 body=None, content_type=None, size=None):
        last_status = None
        for attempt in range(3):
            self.deadline.check()
            current = url
            upload = CheckedFile(body, self.deadline) if isinstance(body, Path) else None
            try:
                for redirect in range(5):
                    parsed = urllib.parse.urlparse(current)
                    host = parsed.hostname or ''
                    if (parsed.scheme != 'https' or parsed.username or parsed.password
                            or not (host in ('api.github.com', 'uploads.github.com', 'github.com')
                                    or host.endswith('.githubusercontent.com'))):
                        raise ValueError('untrusted GitHub download redirect')
                    headers = {'Accept': accept, 'User-Agent': 'crypto-study-private-transfer',
                               'X-GitHub-Api-Version': '2022-11-28'}
                    # Never forward credentials to a different redirect host.
                    if host == urllib.parse.urlparse(url).hostname and host in ('api.github.com', 'uploads.github.com'):
                        headers['Authorization'] = 'Bearer ' + self.token
                    if content_type:
                        headers['Content-Type'] = content_type
                    if size is not None:
                        headers['Content-Length'] = str(size)
                    request = urllib.request.Request(current, data=upload or body, headers=headers, method=method)
                    try:
                        response = self.opener.open(request, timeout=max(1, min(30, self.deadline.remaining())))
                        events.emit("REQUEST_COMPLETED", requests_completed=1)
                        return response
                    except urllib.error.HTTPError as exc:
                        if exc.code in (301, 302, 303, 307, 308) and method == 'GET':
                            current = urllib.parse.urljoin(current, exc.headers.get('Location', ''))
                            exc.close()
                            continue
                        code = exc.code
                        last_status = code
                        exc.close()
                        # Mutations are not blindly retried: an ambiguous draft or
                        # upload must remain uncommitted, not silently overwrite.
                        if method != 'GET' or code not in (429, 500, 502, 503, 504):
                            from github_campaign import TransportError
                            raise TransportError('HTTP', method, code) from None
                        break
                else:
                    raise ValueError('too many download redirects')
            except InterruptedError:
                raise
            except (urllib.error.URLError, TimeoutError, OSError):
                if method != 'GET':
                    from github_campaign import TransportError
                    raise TransportError('AMBIGUOUS_WRITE', method) from None
            finally:
                if upload:
                    upload.close()
            if attempt < 2:
                events.emit("TRANSFER_RETRY", retries=1)
                time.sleep(min(2 ** attempt, max(0, self.deadline.remaining())))
        from github_campaign import TransportError
        raise TransportError('HTTP' if last_status else 'NETWORK', method, last_status)

    def json(self, url, method='GET', value=None):
        raw = execution._canonical(value).encode() if value is not None else None
        with self.response(url, method=method, body=raw, content_type='application/json' if raw else None) as response:
            payload = response.read(8 * BLOCK + 1)
        if len(payload) > 8 * BLOCK:
            raise ValueError('GitHub metadata exceeds bounded size')
        return json.loads(payload, object_pairs_hook=execution._strict_pairs)

    def release(self, tag):
        identifier(tag)
        release = self.json(self.prefix + '/releases/tags/' + urllib.parse.quote(tag, safe=''))
        if release['draft']:
            raise ValueError('partial draft generation is not resumable')
        return release

    def assets(self, release_id):
        assets = {}
        for page in range(1, 1001):
            rows = self.json(self.prefix + f'/releases/{release_id}/assets?per_page=100&page={page}')
            for asset in rows:
                if asset['name'] in assets:
                    raise ValueError('duplicate release asset')
                assets[asset['name']] = asset
            if len(rows) < 100:
                return assets
        raise ValueError('release asset inventory is unbounded')

    def download(self, asset, destination, size, digest=None):
        if asset['size'] != size or size > PART_LIMIT:
            raise ValueError('remote asset size mismatch/limit')
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix('.download-tmp')
        # Retry entire bounded streams; incomplete destinations are never installed.
        for attempt in range(3):
            try:
                started = time.monotonic()
                count = 0
                last_report = time.monotonic()
                with self.response(self.prefix + '/releases/assets/' + str(asset['id']), accept='application/octet-stream') as response, temporary.open('wb') as output:
                    while True:
                        self.deadline.check()
                        chunk = response.read(BLOCK)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > size:
                            raise ValueError('remote stream exceeds asset size')
                        output.write(chunk)
                        if time.monotonic() - last_report >= 45:
                            events.emit("HEARTBEAT", stage="download", **events.resources())
                            last_report = time.monotonic()
                    output.flush()
                    os.fsync(output.fileno())
                if count != size:
                    raise OSError('truncated download')
                events.emit("COMPLETED", stage="download", duration_seconds=time.monotonic() - started, downloaded_bytes=count)
                with events.span("download-hash"):
                    actual = file_sha(temporary, self.deadline.check)
                if digest and actual != digest:
                    raise ValueError('downloaded asset SHA mismatch')
                if asset.get('digest') and asset['digest'] != 'sha256:' + actual:
                    raise ValueError('GitHub asset digest mismatch')
                os.replace(temporary, destination)
                events.emit("ASSET_COMPLETED", assets_completed=1)
                return
            except (OSError, TimeoutError):
                events.emit("TRANSFER_INTERRUPTED", downloaded_bytes=count)
                temporary.unlink(missing_ok=True)
                if attempt < 2:
                    events.emit("TRANSFER_RETRY", retries=1)
                if attempt == 2:
                    raise
        raise RuntimeError('download did not finish')

    def manifest(self, tag, expected, destination):
        release = self.release(tag)
        assets = self.assets(release['id'])
        asset = assets['manifest.json']
        if asset['size'] > 8 * BLOCK:
            raise ValueError('inventory exceeds metadata size limit')
        self.download(asset, destination, asset['size'])
        return read_bundle(destination, expected), assets

    def fetch_parts(self, value, assets, directory):
        directory.mkdir(parents=True, exist_ok=True)
        for name, part in asset_table(value).items():
            path = directory / name
            if path.exists() and regular(path).st_size == part['size'] and file_sha(path, self.deadline.check) == part['sha256']:
                events.emit('ASSET_REUSED')
                continue
            self.download(assets[name], path, part['size'], part['sha256'])
        events.emit('TRANSFER_SELECTED', transfer_bytes=value['transfer_bytes'], selected_bytes=value['uncompressed_bytes'])

    def verify_lineage(self, value, spec):
        if 'control' in spec:
            from market_analysis.historical_campaign_control import consolidation
            from github_campaign import Store, verify_receipt
            closure = consolidation(value, campaign_identity(spec))
            if not hasattr(self, '_verified_finalized'):
                self._verified_finalized = {}
                self._control_store = Store(spec)
            from market_analysis.historical_campaign_control import receipt_reference, COMPACT_LINEAGE
            if closure['version'] == COMPACT_LINEAGE:
                reference = receipt_reference(value, value['metadata']['measured_minutes'])
                self._control_store.api.end = min(time.monotonic() + 240, self.deadline.end)
                verify_receipt(self._control_store, reference, self._verified_finalized)
            else:
                for receipt in closure['finalized_receipts']:
                    verify_receipt(self._control_store, receipt, self._verified_finalized)
            return
        seen = set()
        for depth in range(spec['budget']['max_runs']):
            metadata = value['metadata']
            tag = identifier(metadata['generation'])
            if tag in seen or value['identity'] != campaign_identity(spec):
                raise ValueError('invalid/cyclic recovery lineage identity')
            seen.add(tag)
            lineage = metadata['lineage']
            for field in ('reserved_minutes', 'run_count', 'no_progress_runs'):
                if type(lineage[field]) is not int or lineage[field] < 0:
                    raise ValueError('invalid recovery lineage counters')
            parent = metadata['parent']
            if parent is None:
                if lineage['run_count'] != 1:
                    raise ValueError('initial recovery lineage run count mismatch')
                return
            previous, _ = self.manifest(identifier(parent['generation']), sha(parent['manifest_sha256']),
                                         BASE / 'transfers' / ('lineage-%d.json' % depth))
            prior = previous['metadata']['lineage']
            if (previous['kind'] != 'recovery' or previous['metadata']['generation'] != parent['generation']
                    or not set(previous['metadata']['completed_work']).issubset(metadata['completed_work'])
                    or lineage['run_count'] != prior['run_count'] + 1
                    or lineage['reserved_minutes'] < prior['reserved_minutes']
                    or lineage['sampled_cumulative_runner_minutes'] < prior['sampled_cumulative_runner_minutes']
                    or lineage['no_progress_runs'] != (prior['no_progress_runs'] + 1
                        if metadata['completed_work'] == previous['metadata']['completed_work'] else 0)):
                raise ValueError('recovery lineage/progress does not match exact sealed parent')
            value = previous
        raise ValueError('recovery ancestry exceeds campaign retry bound')

    def reserve_allocation(self, spec, minutes):
        """Conservative append-only allocation, even if a run never saves recovery.

        Published OR interrupted draft reservations count. Omitting resume cannot
        reset the whole-campaign ceiling. Never delete these operational records.
        """
        key = execution._digest({'campaign_id': spec['campaign_id']})[:12]
        prefix = 'allocation-' + key + '-'
        tag = prefix + str(int(os.environ['GITHUB_RUN_ID'])) + '-' + str(int(os.environ['GITHUB_RUN_ATTEMPT'])) + '-' + str(minutes)
        total, count, existing = 0, 0, False
        for page in range(1, 1001):
            rows = self.json(self.prefix + f'/releases?per_page=100&page={page}')
            for release in rows:
                name = release['tag_name']
                if not name.startswith(prefix):
                    continue
                suffix = name[len(prefix):]
                if not re.fullmatch(r'[0-9]+-[0-9]+-[0-9]+', suffix):
                    raise ValueError('invalid existing campaign allocation record')
                allocated = int(suffix.rsplit('-', 1)[1])
                if not 16 <= allocated <= 350:
                    raise ValueError('invalid reserved campaign allocation')
                total += allocated
                count += 1
                existing = existing or name == tag
            if len(rows) < 100:
                break
        else:
            raise ValueError('allocation ledger exceeds bounded inventory')
        if not existing:
            if total + minutes > spec['budget']['ceiling_minutes'] or count + 1 > spec['budget']['max_runs']:
                raise ValueError('total private campaign allocation exceeds declared runner-minute/run ceiling')
            directory = BASE / 'transfers/allocation'
            directory.mkdir(parents=True, exist_ok=False)
            execution._replace_atomic(directory / 'manifest.json', execution._canonical({
                'version': 'historical-study-allocation-v1', 'identity': campaign_identity(spec),
                'reserved_job_minutes': minutes, 'generation': tag}))
            self.publish(tag, directory)
            total += minutes
        execution._replace_atomic(BASE / 'transfers/allocation-total.json', execution._canonical({'reserved_minutes': total}))

    def upload_verified(self, release, path):
        path = Path(path)
        size, digest = regular(path).st_size, file_sha(path, self.deadline.check)
        if size > PART_LIMIT:
            raise ValueError('release asset exceeds 1 GiB part limit')
        url = release['upload_url'].split('{', 1)[0] + '?name=' + urllib.parse.quote(path.name, safe='')
        with self.response(url, method='POST', body=path, size=size, content_type='application/octet-stream') as response:
            raw = response.read(8 * BLOCK + 1)
        if len(raw) > 8 * BLOCK:
            raise ValueError('upload metadata exceeds limit')
        asset = json.loads(raw)
        confirmed = self.json(self.prefix + '/releases/assets/' + str(asset['id']))
        if confirmed['size'] != size or confirmed['state'] != 'uploaded':
            raise ValueError('uploaded asset size/state mismatch')
        if confirmed.get('digest'):
            if confirmed['digest'] != 'sha256:' + digest:
                raise ValueError('uploaded asset API digest mismatch')
        else:
            verify = BASE / 'transfers/readback' / path.name
            self.download(confirmed, verify, size, digest)
            verify.unlink()

    def publish(self, tag, directory):
        identifier(tag)
        # Creation conflicts fail. No existing generation is replaced or cleaned.
        title, body = release_presentation(tag, directory)
        release = self.json(self.prefix + '/releases', 'POST', {
            'tag_name': tag, 'target_commitish': self.branch, 'name': title,
            'draft': True, 'prerelease': True, 'make_latest': 'false',
            'body': body})
        for path in sorted(Path(directory).iterdir()):
            if path.name != 'manifest.json':
                self.upload_verified(release, path)
        self.upload_verified(release, Path(directory) / 'manifest.json')
        result = self.json(self.prefix + '/releases/' + str(release['id']), 'PATCH', {'draft': False})
        if result.get('draft') is not False:
            raise ValueError('remote generation publication was not verified')
        return result



class OperationalInterruption(InterruptedError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def retain_outcome(details):
    try:
        path = BASE / 'transfers/operation-outcomes.json'
        records = execution._read_json(path) if path.exists() else []
        execution._replace_atomic(path, execution._canonical((records + [details])[-16:]))
    except (OSError, ValueError, KeyError):
        pass
    if details.get('operation') == 'validate-parents':
        # The supervisor is authenticated transport; the validator child still
        # receives token_free_environment(). Preserve typed evidence separately
        # when the later finalizer cannot write authority.
        from github_campaign import diagnostic
        try:
            spec = execution._read_json(BASE / 'transfers/campaign.json')
            if 'control' in spec:
                owned = checked_claim(spec, allow_stop=True)['handoff']
                diagnostic(spec, {**details, 'handoff_key': owned['key'], 'claim_mutation_id': owned['mutation_id']})
        except (OSError, ValueError, KeyError, RuntimeError):
            pass


def checked_claim(spec, *, remote=False, allow_stop=False):
    from market_analysis.historical_campaign_control import validate, entry_bindings, TERMINAL
    local = validate(execution._read_json(BASE / 'transfers/control-claim.json'), spec)
    record = local
    if remote:
        from github_campaign import Store
        record, _ = Store(spec).get()
        if record is None:
            raise ValueError('claimed authority disappeared')
    h = entry_bindings(record, spec, os.environ.get('STUDY_HANDOFF_KEY'), os.environ)
    owner = local['handoff']
    if (record['state'] in TERMINAL | {'OUTCOME_UNRESOLVED'} or h['run_id'] != os.environ['GITHUB_RUN_ID']
            or h.get('attempt') != os.environ['GITHUB_RUN_ATTEMPT'] or h['state'] != 'CLAIMED'
            or h.get('mutation_id') != owner.get('mutation_id')):
        raise ValueError('stale or terminal campaign claim')
    if record['stop_requested'] and not allow_stop:
        raise OperationalInterruption('OWNER_STOP')
    return record


def restore_finalized(spec, github=None):
    """Aggregation restores exact declared reports/sidecars, never earlier spools."""
    record = checked_claim(spec, remote=True)
    _, _, phase, _, minutes, start = settings()
    manifest, coverage = frozen_inputs(spec)
    prerequisites(spec, manifest, coverage, phase, os.environ.get('STUDY_ALLOW_TEST') == 'true')
    github = github or GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), Deadline(start + (minutes - 15) * 60))
    task = record['handoff']['task']
    wanted = {t['id']: t for t in spec['control']['tasks'] if t['id'] in task['depends_on'] and t['operation'] == 'execute-period'}
    receipts = {r['task_id']: r for r in record['receipts'] if r['task_complete'] and r['task_id'] in wanted}
    if set(receipts) != set(wanted):
        raise ValueError('aggregate lacks exact finalized dependency receipts')
    for task_id, receipt in receipts.items():
        value, assets = github.manifest(receipt['generation'], receipt['manifest_sha256'], BASE / 'transfers' / ('final-' + identifier(task_id) + '.json'))
        github.verify_lineage(value, spec)
        period, = execution.select_execution_periods(manifest, phase, period_index=wanted[task_id]['period_index'], allow_test=phase == 'test')
        name = execution._period_filename(period)
        prefix = 'campaigns/' + spec['campaign_id'] + '/outputs/'
        paths = {prefix + execution.PERIOD_DIRECTORY + '/' + name, prefix + '.period-manifests/' + name}
        selected = subset(value, lambda row: row['path'] in paths)
        if {r['path'] for r in selected['files']} != paths:
            raise ValueError('finalized result closure lacks report/sidecar')
        limits(spec, selected)
        parts, stage = BASE / 'transfers/final-parts', BASE / 'transfers/final-staging'
        github.fetch_parts(selected, assets, parts)
        unpack_files(selected, parts, stage, max_bytes=spec['limits']['max_uncompressed_bytes'], headroom_bytes=spec['limits']['headroom_bytes'], check=github.deadline.check)
        install_files(selected, stage)
        shutil.rmtree(stage)
        shutil.rmtree(parts)
    verify_recovery_tree(BASE, spec['campaign_id'], manifest, coverage, campaign_identity(spec), check=github.deadline.check)


def release_presentation(tag, directory):
    """Human presentation is derived from a sealed inventory, never proof itself."""
    path = Path(directory) / 'manifest.json'
    value = execution._read_json(path)
    if 'bundle_sha256' not in value:
        return tag, 'Private legacy allocation record; retained accounting.'
    read_bundle(path, value['bundle_sha256'])
    metadata = value.get('metadata', {})
    display = metadata.get('presentation', value.get('identity', {}).get('period', {}))
    typed = metadata.get('task_receipt', {})
    purpose = ('Finalized results/evidence' if typed.get('task_complete') and display.get('operation') == 'execute-period'
               else ('Campaign terminal results' if typed.get('campaign_complete') else 'Finalized aggregate results') if typed.get('task_complete') and display.get('operation') == 'aggregate'
               else 'Inputs' if value['kind'] == 'inputs' else 'Study recovery' if value['kind'] == 'recovery' else 'Prepared core cache')
    title = purpose + (' — Slice ' + str(display['sequence']) if 'sequence' in display else '') + ' — ' + str(display.get('phase', '')) + ' ' + str(display.get('period_index', display.get('study_period_index', ''))) + ' — ' + str(display.get('date', display.get('utc_date', '')) or '')
    run = 'https://github.com/' + os.environ.get('GITHUB_REPOSITORY', '') + '/actions/runs/' + os.environ.get('GITHUB_RUN_ID', '')
    body = ('Purpose: ' + purpose + '\nScientific state: ' + str(metadata.get('scientific_state', 'not scientific completion'))
            + '\nSymbols: ' + ', '.join(display.get('symbols', sorted({r.get('facts', {}).get('symbol') for r in value['files'] if r.get('facts', {}).get('symbol')})))
            + '\nSources: ' + ', '.join({'core': 'Binance futures aggTrades', 'mark': 'Binance mark price', 'open-interest': 'Binance open interest', 'funding': 'Binance funding', 'liquidation': 'Tardis liquidations'}.get(source, source) for source in display.get('sources', sorted({r.get('facts', {}).get('source') for r in value['files'] if r.get('facts', {}).get('source')})))
            + '\nLocal committed units: ' + str(len(metadata.get('completed_work', []))) + '\nTransfer bytes: ' + str(value['transfer_bytes'])
            + '\nRuntime: `' + str(value['identity'].get('runtime_sha', 'input producer')) + '`\nOrigin: ' + run
            + '\nInventory: `' + value['bundle_sha256'] + '`\nUse the sealed inventory, not this description, for validation.')
    index = ['# ' + title, body, '', '| File | Bytes | SHA-256 |', '| --- | ---: | --- |']
    index.extend('| ' + row['path'] + ' | ' + str(row.get('size', 0)) + ' | ' + str(row.get('sha256', 'recorded absence')) + ' |' for row in value['files'])
    (Path(directory) / 'bundle-index.md').write_text('\n'.join(index) + '\n')
    return title[:200], body

def settings():
    spec = campaign_spec(BASE / 'transfers/campaign.json')
    operation = os.environ['STUDY_OPERATION']
    phase = os.environ['STUDY_PHASE']
    if operation not in ('preflight', 'execute-period', 'aggregate') or phase not in ('development', 'validation', 'test'):
        raise ValueError('unknown typed operation/phase')
    index = None if operation == 'aggregate' else int(os.environ['STUDY_PERIOD_INDEX'])
    if index is not None and (index < 0 or index > 29 or index not in spec['expected_membership'][phase]):
        raise ValueError('exact period/phase is outside campaign membership')
    if os.environ.get('STUDY_ALLOW_TEST', 'false') not in ('true', 'false'):
        raise ValueError('invalid explicit test authorization flag')
    minutes = int(os.environ.get('STUDY_JOB_MINUTES', '60'))
    if not 16 <= minutes <= 350:
        raise ValueError('job budget must be 16..350 minutes with a 15-minute reserve')
    start = float(os.environ['STUDY_JOB_STARTED_EPOCH'])
    if not 0 < start <= time.time():
        raise ValueError('invalid actual job start time')
    return spec, operation, phase, index, minutes, start


def bootstrap():
    # This command runs from the main-branch checkout before any data secret is set.
    path = str(safe_relative(os.environ['STUDY_CAMPAIGN_SPEC']))
    if not path.startswith('research/campaigns/') or not path.endswith('.json'):
        raise ValueError('campaign spec must be a trusted research/campaigns JSON file')
    regular(REPO / path)
    raw = (REPO / path).read_bytes()
    spec = json.loads(raw, object_pairs_hook=execution._strict_pairs)
    pin = sha(spec['runtime_sha'], 40)
    for revision in (pin, sha(spec['orchestration_sha'], 40)):
        subprocess.run(['git', '-C', str(REPO), 'merge-base', '--is-ancestor', revision, 'origin/main'], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if pin != spec['orchestration_sha']:
        raise ValueError('initial platform requires one trusted runtime/orchestration commit')
    subprocess.run(['git', '-C', str(REPO), 'checkout', '--detach', pin], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    BASE.joinpath('transfers').mkdir(parents=True, exist_ok=True)
    (BASE / 'transfers/campaign.json').write_bytes(raw)
    actual = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(REPO), 'status', '--porcelain', '--untracked-files=all', '--',
                            'python/market_analysis', 'scripts/research', '.github/workflows'],
                           check=True, capture_output=True, text=True).stdout.strip()
    if actual != pin or dirty:
        raise ValueError('pinned runtime/orchestration checkout is not clean')
    # Re-enter the pinned helper before trusting any orchestration implementation.
    subprocess.run([sys.executable, str(REPO / 'scripts/research/github_study.py'), 'validate-settings'], check=True)


def budget_parent(spec, minutes, recovery=None):
    if 'control' in spec:
        path = BASE / 'transfers/control-claim.json'
        if not path.exists():
            # Pin validation precedes the remote claim; it never allocates work.
            return {'reserved_minutes': 0, 'run_count': 0, 'no_progress_runs': 0, 'sampled_cumulative_runner_minutes': 0}
        from market_analysis.historical_campaign_control import validate
        record = validate(execution._read_json(path), spec)
        return {'reserved_minutes': record['reserved_minutes'], 'run_count': record['run_count'] - 1,
                'no_progress_runs': record['no_progress_runs'], 'sampled_cumulative_runner_minutes': record['measured_minutes']}
    old = recovery['metadata']['lineage'] if recovery else {'reserved_minutes': 0, 'run_count': 0, 'no_progress_runs': 0,
                                                          'sampled_cumulative_runner_minutes': 0}
    if (old['reserved_minutes'] + minutes > spec['budget']['ceiling_minutes']
            or old['run_count'] + 1 > spec['budget']['max_runs']
            or old['no_progress_runs'] >= spec['budget']['no_progress_cap']):
        raise ValueError('campaign allocation/retry/no-progress cap reached; owner must explicitly revise the campaign')
    return old


def subset(value, predicate):
    result = {**value, 'files': [row for row in value['files'] if predicate(row)]}
    result['uncompressed_bytes'] = sum(row['size'] for row in result['files'])
    result['transfer_bytes'] = sum(row['size'] for row in asset_table(result).values())
    validate_bundle(result, selected=True)
    return result


def limits(spec, value):
    if (value['transfer_bytes'] > spec['limits']['max_transfer_bytes']
            or value['uncompressed_bytes'] > spec['limits']['max_uncompressed_bytes']):
        raise BundleFootprintError('configured-bundle-limit', transfer_bytes=value['transfer_bytes'],
            assembled_bytes=value['uncompressed_bytes'], max_transfer_bytes=spec['limits']['max_transfer_bytes'],
            max_assembled_bytes=spec['limits']['max_uncompressed_bytes'])
    if shutil_free() < value['uncompressed_bytes'] + value['transfer_bytes'] + spec['limits']['headroom_bytes']:
        raise BundleFootprintError('transfer-staging-headroom', free_bytes=shutil_free(),
            required_bytes=value['uncompressed_bytes'] + value['transfer_bytes'] + spec['limits']['headroom_bytes'])


def shutil_free():
    import shutil
    return shutil.disk_usage(BASE).free


def fetch_inputs(command):
    spec, operation, phase, index, minutes, start = settings()
    deadline = Deadline(start + (minutes - 15) * 60)
    if 'control' in spec:
        checked_claim(spec, remote=True)
    if command == 'inputs':
        manifest, coverage = frozen_inputs(spec)
        prerequisites(spec, manifest, coverage, phase, os.environ.get('STUDY_ALLOW_TEST') == 'true')
    github = GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), deadline)
    if command == 'metadata':
        if 'control' in spec:
            checked_claim(spec, remote=True)
        tag = os.environ.get('STUDY_RESUME_GENERATION', '')
        expected = os.environ.get('STUDY_RESUME_SHA', '')
        if bool(tag) != bool(expected):
            raise ValueError('resume requires both locator and trusted inventory SHA')
        if tag:
            recovery, _ = github.manifest(tag, sha(expected), BASE / 'transfers/recovery-manifest.json')
            if recovery['kind'] != 'recovery' or recovery['identity'] != campaign_identity(spec):
                raise ValueError('recovery identity mismatch before allocation')
            github.verify_lineage(recovery, spec)
            budget_parent(spec, minutes, recovery)
        if 'control' not in spec:
            github.reserve_allocation(spec, minutes)
    # Aggregation uses an explicit period input locator for frozen metadata only;
    # never obtains raw archives merely to assemble reports.
    locator_index = index if index is not None else next(
        (number for number in spec['expected_membership'][phase] if str(number) in spec['inputs']), None)
    if locator_index is None:
        raise ValueError('aggregation requires a declared phase metadata inventory')
    locator = spec['inputs'][str(locator_index)]
    value, assets = github.manifest(locator['release_tag'], locator['manifest_sha256'], BASE / 'transfers/input-manifest.json')
    if value['kind'] != 'inputs':
        raise ValueError('input locator does not name an input bundle')
    for key in ('study_manifest_sha256', 'coverage_manifest_sha256', 'producer_revision',
                'study_file_sha256', 'coverage_file_sha256'):
        if value['identity'].get(key) != spec[key]:
            raise ValueError('input inventory identity differs from trusted campaign')
    if index is not None and (value['identity']['period']['study_period_index'] != index
                              or value['identity']['period']['phase'] != phase):
        raise ValueError('input inventory is for a different exact period/phase')
    for row in value['files']:
        if (not row['path'].startswith(('manifests/', 'inputs/'))
                or row['path'].startswith('manifests/') and row['path'] not in metadata_paths(phase)):
            raise ValueError('input inventory declares an unauthorized root or later-phase metadata')
    selected = subset(value, lambda row: row['path'].startswith('manifests/') if command == 'metadata'
                      else row['path'].startswith('inputs/') and operation != 'aggregate')
    limits(spec, selected)
    github.fetch_parts(selected, assets, BASE / 'transfers/input-parts')
    stage = BASE / 'transfers' / ('metadata-staging' if command == 'metadata' else 'input-staging')
    with events.span('input-reconstruction'):
        unpack_files(selected, BASE / 'transfers/input-parts', stage, max_bytes=spec['limits']['max_uncompressed_bytes'],
                     headroom_bytes=spec['limits']['headroom_bytes'], check=deadline.check)
    with events.span('input-install'):
        install_files(selected, stage)
    shutil.rmtree(stage)
    # Metadata assets cannot share raw partitions in v2; callers no longer need
    # the installed transfer copies. v1 individual assets have the same property.
    shutil.rmtree(BASE / 'transfers/input-parts')


def restore():
    spec, operation, phase, index, minutes, start = settings()
    tag = os.environ.get('STUDY_RESUME_GENERATION', '')
    expected = os.environ.get('STUDY_RESUME_SHA', '')
    if bool(tag) != bool(expected):
        raise ValueError('resume requires both exact generation and manifest SHA')
    if not tag:
        budget_parent(spec, minutes)
        if 'control' in spec and operation == 'aggregate':
            restore_finalized(spec, github=None)
        return
    sha(expected)
    deadline = Deadline(start + (minutes - 15) * 60)
    github = GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), deadline)
    value, assets = github.manifest(tag, expected, BASE / 'transfers/recovery-manifest.json')
    if value['kind'] != 'recovery' or value['identity'] != campaign_identity(spec):
        raise ValueError('recovery is incompatible with exact runtime/campaign/input/lock identities')
    if value['metadata']['generation'] != tag:
        raise ValueError('generation locator does not match sealed manifest')
    github.verify_lineage(value, spec)
    budget_parent(spec, minutes, value)
    manifest, coverage = frozen_inputs(spec)
    prerequisites(spec, manifest, coverage, phase, os.environ.get('STUDY_ALLOW_TEST') == 'true')
    included = classify_recovery_inventory(value, manifest, spec['campaign_id'], spec['expected_membership'])
    if 'control' in spec:
        record = checked_claim(spec, remote=True)
        if value['metadata']['task_receipt']['task_id'] != record['handoff']['task']['id']:
            raise ValueError('active recovery names a different declared task')
        if operation != 'aggregate' and any(p.study_period_index != index or p.phase != phase for p in included):
            raise ValueError('active recovery contains an earlier finalized period')
        if operation == 'aggregate' and any('/checkpoints/' in row['path'] for row in value['files']):
            raise ValueError('aggregate recovery must not restore replay spools')
    phases = {item.phase for item in included}
    if ('test' in phases and phase != 'test'
            or 'validation' in phases and phase == 'development'):
        raise ValueError('recovery contains later-phase evidence; dispatch its explicit authorized phase')
    limits(spec, value)
    github.fetch_parts(value, assets, BASE / 'transfers/recovery-parts')
    with events.span('recovery-reconstruction'):
        unpack_files(value, BASE / 'transfers/recovery-parts', BASE / 'transfers/recovery-staging',
                     max_bytes=spec['limits']['max_uncompressed_bytes'], headroom_bytes=spec['limits']['headroom_bytes'],
                     check=deadline.check)


def batch_arguments(operation, spec, phase, index, deadline_epoch, events_path):
    arguments = [sys.executable, '-m', 'market_analysis.historical_study_batch', operation,
                 '--campaign-spec', str(BASE / 'transfers/campaign.json'), '--phase', phase,
                 '--max-new-stages', str(spec.get('max_new_stages', 1)),
                 '--compute-deadline-epoch', str(deadline_epoch), '--events-path', str(events_path)]
    if index is not None:
        arguments += ['--period-index', str(index)]
    if os.environ.get('STUDY_ALLOW_TEST') == 'true':
        arguments += ['--allow-test']
    if operation != 'validate-parents' and (BASE / 'transfers/recovery-manifest.json').exists():
        arguments += ['--restore-manifest', str(BASE / 'transfers/recovery-manifest.json'),
                      '--restore-sha', os.environ['STUDY_RESUME_SHA'],
                      '--restore-staging', str(BASE / 'transfers/recovery-staging')]
    return arguments


def token_free_environment():
    return {**{key: os.environ[key] for key in ('PATH', 'LANG', 'TZ') if key in os.environ},
            'PYTHONPATH': str(REPO / 'python'), 'PYTHONUNBUFFERED': '1'}


def relay(path, offset):
    """Read a bounded append-only channel, never private stdout/stderr."""
    if not path.exists():
        return offset
    with path.open('rb') as handle:
        handle.seek(offset)
        for _ in range(512):
            begin = handle.tell()
            raw = handle.readline(events.MAX_EVENT_BYTES + 1)
            if not raw:
                return handle.tell()
            if not raw.endswith(b'\n'):
                return begin  # writer has not finished a line yet
            if len(raw) <= events.MAX_EVENT_BYTES:
                try:
                    row = json.loads(raw)
                    if isinstance(row, dict) and row.get('version') == events.VERSION:
                        print('study-event ' + json.dumps(events.allowlisted(row), sort_keys=True), flush=True)
                except (ValueError, UnicodeError):
                    pass
        return handle.tell()


def verified_tree(spec, manifest, coverage, recovery, deadline):
    root = BASE / 'campaigns' / spec['campaign_id']
    with ExitStack() as stack:
        stack.enter_context(owned_run_directory(root, cleanup=False))
        for period_root in sorted((root / 'checkpoints').glob('period-*')):
            stack.enter_context(owned_run_directory(period_root))
        with events.span('recovery-validation'):
            files, work = verify_recovery_tree(BASE, spec['campaign_id'], manifest, coverage,
                campaign_identity(spec), check=deadline.check, locks_held=True)
        deadline.check()
        prior_work = (recovery['metadata']['local_completed_work'] if 'control' in spec else recovery['metadata']['completed_work']) if recovery else []
        if not set(prior_work).issubset(work):
            raise ValueError('verified recovery lost previously committed work')
        return files, work



def stop_group(child):
    """Bounded unwind, then kill/reap every scientific group member."""
    try:
        os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def supervise(validate_only=False):
    spec, operation, phase, index, minutes, start = settings()
    epoch = start + (minutes - 15) * 60
    deadline = Deadline(epoch)
    root = BASE / 'campaigns' / spec['campaign_id']
    operations = root / 'operations'
    operations.mkdir(parents=True, exist_ok=True)
    recovery = (read_bundle(BASE / 'transfers/recovery-manifest.json', os.environ['STUDY_RESUME_SHA'])
                if (BASE / 'transfers/recovery-manifest.json').exists() else None)
    budget_parent(spec, minutes, recovery)
    status_store = None
    last_status = 0
    if 'control' in spec:
        checked_claim(spec)
        if not validate_only:
            from github_campaign import Store, Conflict
            status_store = Store(spec)
    if deadline.remaining() <= 30:
        raise OperationalInterruption('SETUP_BUDGET')
    log_name = ('parents' if validate_only else 'batch') + '-' + identifier(os.environ['GITHUB_RUN_ID']) + '-' + identifier(os.environ['GITHUB_RUN_ATTEMPT'])
    event_path = operations / (log_name + '.events.jsonl')
    arguments = batch_arguments('validate-parents' if validate_only else operation, spec, phase, index, epoch, event_path)
    termination = None
    supervision_failure = None
    if not validate_only:
        checked_claim(spec, remote=True)
        if deadline.remaining() <= 30:
            raise OperationalInterruption('SETUP_BUDGET')
        (BASE / 'transfers/science-started').write_text('started\n')
    offset = 0
    with (operations / (log_name + '.log')).open('wb') as log:
        child = subprocess.Popen(arguments, cwd=REPO / 'python', env=token_free_environment(),
                                 start_new_session=True, stdout=log, stderr=log, close_fds=True)
        try:
            while True:
                offset = relay(event_path, offset)
                if status_store is not None and not validate_only and time.monotonic() - last_status >= 120:
                    try:
                        status_store.api.end = time.monotonic() + 12
                        status_store.api.timeout = 2
                        remote, _ = status_store.get()
                        observations = list(current_events(root))
                        activity = {'last_heartbeat_epoch': time.time(), 'observations': observations[-8:],
                                    'computed_replay': next((r for r in reversed(observations) if r.get('category') in ('REPLAY_CURRENT_PROGRESS', 'REPLAY_RESUMED')), None)}
                        status_path = operations / 'status.json'
                        if status_path.exists():
                            local_status = execution._read_json(status_path)
                            activity['science'] = {k: local_status.get(k) for k in ('scientific_state', 'current_stage', 'newly_completed_stages', 'reused_stages', 'last_verified_unit')}
                            declared = ['v1', *execution._candidate_stage_selectors(), 'event-context', 'outcomes'] if operation == 'execute-period' else []
                            done = {r['stage_id'] for r in local_status.get('newly_completed_stages', []) + local_status.get('reused_stages', [])}
                            activity['remaining_stages'] = [stage for stage in declared if stage not in done]
                        activity['timings_seconds'] = {r.get('stage', r.get('operation', 'operation')) + ':' + r.get('category', ''): r['duration_seconds'] for r in observations if 'duration_seconds' in r}
                        status_store.view(remote, activity)
                    except (InterruptedError, ValueError, KeyError):
                        raise # Cancellation and authoritative integrity are never optional.
                    except (OSError, RuntimeError, TimeoutError, Conflict):
                        events.emit('STATUS_VIEW_UNAVAILABLE')
                    last_status = time.monotonic()
                already_exited = child.poll()
                if already_exited is not None:
                    result = already_exited
                    break
                remaining = deadline.remaining()
                if remaining <= 0:
                    termination = 'compute-deadline'
                    stop_group(child)
                    result = 1
                    break
                try:
                    result = child.wait(timeout=min(10, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
        except (InterruptedError, KeyboardInterrupt) as exc:
            termination = 'compute-deadline' if isinstance(exc, OperationalInterruption) and exc.code == 'SETUP_BUDGET' else 'external-cancellation'
            stop_group(child)
            result = 1
        except BaseException as exc:
            termination = 'supervision-error'
            if validate_only:
                from github_campaign import safe_error
                supervision_failure = safe_error(exc, 'validate-parents')['category']
            stop_group(child)
            result = 1
        finally:
            if child.poll() is None:
                stop_group(child)
            # A crashed parent may have left a worker holding a directory lease.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            offset = relay(event_path, offset)
    status_path = operations / 'status.json'
    if validate_only and result:
        # Validation has no scientific recovery tree. Preserve a failure already
        # reported by the child even if cancellation/deadline followed it.
        status = execution._read_json(status_path) if status_path.exists() else {}
        prior_failure = status.get('failure_category')
        genuine = prior_failure not in (None, 'InterruptedError')
        category = ('INTEGRITY' if prior_failure in ('ValueError', 'KeyError') else 'PROCESS' if genuine
                    else supervision_failure or ('SETUP_BUDGET' if termination == 'compute-deadline'
                    else 'CANCELLED' if termination == 'external-cancellation' or prior_failure == 'InterruptedError' else 'PROCESS'))
        retain_outcome({'version': 'historical-campaign-diagnostic-v1', 'operation': 'validate-parents',
                        'category': category, 'http_status': None, 'exit_code': result})
    if not validate_only:
        status = execution._read_json(status_path) if status_path.exists() else {}
        prior_failure = status.get('failure_category')
        if termination or not status_path.exists() or (result and (status.get('scientific_state') != 'FAILED' or not status.get('failure_category'))):
            status.update(version=STATUS_VERSION, identity=campaign_identity(spec), scientific_state='FAILED',
                          failure_category=termination or 'ProcessFailure', returncode=result, remote_published=False)
            if termination in ('compute-deadline', 'external-cancellation'):
                try:
                    if prior_failure in (None, 'InterruptedError') and (BASE / 'transfers/recovery-staging').exists():
                        # Installation/validation was interrupted before launch.
                        # Retain the already verified exact remote receipt instead
                        # of calling an uninstalled parent a scientific failure.
                        (BASE / 'transfers/retain-parent-only').write_text('retained\n')
                        retain_outcome({'version': 'historical-campaign-diagnostic-v1', 'operation': 'run',
                            'category': 'SETUP_BUDGET' if termination == 'compute-deadline' else 'CANCELLED', 'http_status': None, 'exit_code': result})
                        status.update(scientific_state='NOT_STARTED', yield_reason='setup-budget' if termination == 'compute-deadline' else 'external-cancellation', failure_category=None)
                        execution._replace_atomic(status_path, execution._canonical(status))
                        return 0 if termination == 'compute-deadline' else 1
                    manifest, coverage = frozen_inputs(spec)
                    files, work = verified_tree(spec, manifest, coverage, recovery, Deadline(min(epoch + 90, time.time() + 90)))
                    last = last_verified_unit(files, work, index)
                    if last is not None:
                        status['last_verified_unit'] = last
                    # A failure that happened before our budget signal stays a
                    # scientific failure even when earlier recovery is intact.
                    if prior_failure not in (None, 'InterruptedError'):
                        status['failure_category'] = prior_failure
                    if prior_failure in (None, 'InterruptedError'):
                        status.update(scientific_state='YIELDED', yield_reason='supervisor-budget-termination' if termination == 'compute-deadline' else 'external-cancellation', failure_category=None)
                        result = 0 if termination == 'compute-deadline' else 1
                except BaseException as exc:
                    status.update(scientific_state='FAILED', failure_category=prior_failure if prior_failure not in (None, 'InterruptedError') else type(exc).__name__,
                                  verification_failure_category=type(exc).__name__, failure_operation='recovery-verification')
                    execution._replace_atomic(operations / 'supervisor-failure.json', execution._canonical({'category': 'INTEGRITY', 'operation': 'recovery-verification'}))
            execution._replace_atomic(status_path, execution._canonical(status))
        if status.get('scientific_state') == 'FAILED' or termination == 'external-cancellation':
            retain_outcome({'version': 'historical-campaign-diagnostic-v1', 'operation': 'run',
                'category': 'PROCESS' if status.get('scientific_state') == 'FAILED' else 'CANCELLED', 'http_status': None, 'exit_code': result})
        events.emit('SCIENTIFIC_STOP', scientific_state=status.get('scientific_state'), reason=termination, returncode=result)
    return result


def snapshot():
    spec, operation, phase, index, minutes, start = settings()
    deadline = Deadline(start + minutes * 60 - 120)
    if 'control' in spec and (not (BASE / 'transfers/science-started').exists() or (BASE / 'transfers/retain-parent-only').exists()):
        # Never fabricate a new saved tree from uninstalled staged parent work.
        # The finalizer retains the exact remote parent in the authoritative intent.
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('ready=false\n')
        return
    manifest, coverage = frozen_inputs(spec)
    prerequisites(spec, manifest, coverage, phase, os.environ.get('STUDY_ALLOW_TEST') == 'true')
    recovery = (read_bundle(BASE / 'transfers/recovery-manifest.json', os.environ['STUDY_RESUME_SHA'])
                if (BASE / 'transfers/recovery-manifest.json').exists() else None)
    old = budget_parent(spec, minutes, recovery)
    root = BASE / 'campaigns' / spec['campaign_id']
    # Keep campaign ownership without broad cleanup. Each exclusively owned
    # period cleans only known abandoned runtime temporaries before validation.
    with ExitStack() as stack:
        stack.enter_context(owned_run_directory(root, cleanup=False))
        for period_root in sorted((root / 'checkpoints').glob('period-*')):
            stack.enter_context(owned_run_directory(period_root))
        with events.span('recovery-validation'):
            files, work = verify_recovery_tree(BASE, spec['campaign_id'], manifest, coverage,
                                             campaign_identity(spec), check=deadline.check, locks_held=True)
        status_path = root / 'operations/status.json'
        status = execution._read_json(status_path) if status_path.exists() else {'scientific_state': 'FAILED'}
        previous_work = (recovery['metadata']['local_completed_work'] if 'control' in spec else recovery['metadata']['completed_work']) if recovery else []
        local_work = work
        record = checked_claim(spec, allow_stop=True) if 'control' in spec else None
        finalized = [r for r in record['receipts'] if r['task_complete']] if record else []
        if not set(previous_work).issubset(local_work):
            raise ValueError('snapshot cannot discard parent committed work after failed restore')
        last = last_verified_unit(files, work, index)
        if last is not None:
            status['last_verified_unit'] = last
        status['verified_committed_work_count'] = len(work)
        status['verified_new_work_count'] = len(set(work) - set(previous_work))
        execution._replace_atomic(status_path, execution._canonical(status))
        files.add(status_path)
        lineage = {'reserved_minutes': record['reserved_minutes'] if record else execution._read_json(BASE / 'transfers/allocation-total.json')['reserved_minutes'], 'run_count': old['run_count'] + 1,
                   'no_progress_runs': old['no_progress_runs'] + 1 if work == previous_work else 0,
                   'sampled_cumulative_runner_minutes': old['sampled_cumulative_runner_minutes'] + (time.time() - start) / 60}
        tag = f"recovery-{spec['campaign_id'][:20]}-{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}"
        identifier(tag)
        metadata = {'generation': tag, 'parent': {'generation': os.environ.get('STUDY_RESUME_GENERATION'),
                    'manifest_sha256': os.environ.get('STUDY_RESUME_SHA')} if recovery else None,
                    'lineage': lineage, 'completed_work': work, 'scientific_state': status['scientific_state'],
                    'source_identities': coverage['source_identities'], 'disk': disk_sample(root),
                    'runner_time_sampling_boundary': 'before pack/upload; allocation covers full requested job'}
        if record:
            task = record['handoff']['task']
            period = None
            if index is not None:
                period, = execution.select_execution_periods(manifest, phase, period_index=index, allow_test=phase == 'test')
            complete = False
            if status['scientific_state'] != 'FAILED':
                if operation == 'preflight':
                    complete = status.get('preflight_passed') is True
                elif operation == 'execute-period':
                    required_evidence = {period_root(spec['campaign_id'], period) / 'prepared-replay.json',
                                         period_root(spec['campaign_id'], period) / 'study-points/manifest.json',
                                         *(period_root(spec['campaign_id'], period) / 'post-replay' / (stage + '.json') for stage in ['v1', *execution._candidate_stage_selectors(), 'event-context', 'outcomes'])}
                    complete = status['scientific_state'] == 'FINALIZED_PERIOD' and (root / 'outputs' / execution.PERIOD_DIRECTORY / execution._period_filename(period)) in files and required_evidence.issubset(files)
                else:
                    aggregate_result = status.get('aggregation', {})
                    complete = status['scientific_state'] == 'FINALIZED_PERIOD' and aggregate_result.get('expected_membership') == spec['expected_membership'][phase] and (root / 'outputs' / execution.EXECUTION_INDEX_FILENAME) in files
            from market_analysis.historical_campaign_control import digest, RECEIPT, COMPACT_LINEAGE, accounting_reference
            parent = record['handoff']['parent']
            progress = complete or parent is None and bool(local_work) or parent is not None and digest(local_work) != parent['work_sha256']
            lineage['no_progress_runs'] = 0 if progress else record['no_progress_runs'] + 1
            metadata.update(completed_work=local_work, local_completed_work=local_work,
                measured_minutes=(time.time() - start) / 60,
                task_receipt={'receipt_contract': RECEIPT, 'task_id': task['id'], 'handoff_key': record['handoff']['key'],
                              'operation': task['operation'], 'run_id': record['handoff']['run_id'], 'attempt': record['handoff']['attempt'],
                              'task_complete': complete, 'campaign_complete': complete and all(state == 'DONE' or task_id == task['id'] for task_id, state in record['tasks'].items()),
                              'verified_outputs': task['expected_outputs'] if complete else [],
                              'setup_outcome': os.environ.get('STUDY_SETUP_OUTCOME', 'unknown'),
                              'scientific_state': status['scientific_state'],
                              'termination_reason': 'external-cancellation' if status.get('yield_reason') == 'external-cancellation' else 'budget-yield' if status['scientific_state'] == 'YIELDED' else 'completed' if complete else 'failed'},
                consolidation={'version': COMPACT_LINEAGE, 'finalized_receipts': finalized,
                               'parent_receipt': parent, 'accounting': accounting_reference(record, execution._read_json(BASE / 'transfers/control-claim-reference.json'))},
                presentation={'phase': phase, 'period_index': index, 'operation': operation,
                              'date': period.utc_date.isoformat() if period else None,
                              'symbols': sorted({symbol for packages in planned_packages(period).values() for _, symbol in packages}) if period else [],
                              'sources': sorted({row['facts']['source'] for row in execution._read_json(BASE / 'transfers/input-manifest.json')['files'] if 'source' in row.get('facts', {})}), 'sequence': record['sequence']})
        value = pack_files([(str(path.relative_to(BASE)), path, {'role': 'committed-recovery', **({'partition_version': 'period-results-v1'} if record else {})}) for path in files],
            BASE / 'transfers/publication', kind='recovery', identity=campaign_identity(spec),
            metadata=metadata,
            check=deadline.check, max_bytes=spec['limits']['max_uncompressed_bytes'])
        if regular(BASE / 'transfers/publication/manifest.json').st_size > 8 * BLOCK:
            raise ValueError('sealed ancestry inventory exceeds bounded manifest size')
        if value['transfer_bytes'] > spec['limits']['max_transfer_bytes']:
            raise BundleFootprintError('recovery-transfer-limit', transfer_bytes=value['transfer_bytes'],
                                       limit_bytes=spec['limits']['max_transfer_bytes'])
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('ready=true\n')


def publish():
    spec, operation, phase, index, minutes, start = settings()
    deadline = Deadline(start + minutes * 60 - 60)
    github = GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), deadline)
    path = BASE / 'transfers/publication/manifest.json'
    value = execution._read_json(path)
    expected = value['bundle_sha256']
    read_bundle(path, expected)
    result = github.publish(value['metadata']['generation'], path.parent)
    receipt = {'version': 'historical-study-publication-receipt-v1', 'remote_published': True,
               'generation': result['tag_name'], 'manifest_sha256': expected,
               'scientific_state': value['metadata']['scientific_state'],
               'transfer_bytes': value['transfer_bytes'], 'uncompressed_bytes': value['uncompressed_bytes'],
               'reserved_campaign_minutes': value['metadata']['lineage']['reserved_minutes'],
               'sampled_job_elapsed_minutes': (time.time() - start) / 60}
    execution._replace_atomic(BASE / 'transfers/publication-receipt.json', execution._canonical(receipt))
    events.emit("PUBLICATION_VERIFIED", publication_state="VERIFIED")
    return 0



def record_receipt():
    """Control acceptance is distinct from verified remote publication."""
    spec, _, _, _, _, _ = settings()
    if 'control' not in spec:
        return
    from github_campaign import publish_receipt
    receipt = execution._read_json(BASE / 'transfers/publication-receipt.json')
    value = read_bundle(BASE / 'transfers/publication/manifest.json', receipt['manifest_sha256'])
    from market_analysis.historical_campaign_control import receipt_reference
    reference = receipt_reference(value, value['metadata']['measured_minutes'])
    publish_receipt(spec, reference)

def dependency_key():
    import platform
    pins = {name: file_sha(REPO / 'python' / name) for name in ('requirements.txt', 'requirements-research.txt')}
    key = 'research-pip-' + platform.system() + '-' + platform.machine() + '-' + '.'.join(map(str, sys.version_info[:3])) + '-' + execution._digest(pins)
    destination = os.environ.get('GITHUB_OUTPUT')
    if destination:
        with open(destination, 'a') as output:
            output.write('key=' + key + '\n')


def cache_request(spec, index):
    from market_analysis.historical_prepared_core import cache_identity, identity_key
    inventory = read_bundle(BASE / 'transfers/input-manifest.json', spec['inputs'][str(index)]['manifest_sha256'])
    identity = cache_identity(spec, inventory)
    key = identity_key(identity)
    return identity, key, 'prepared-core-' + key[:48]


def cache_inventory(github, tag, identity, key, directory):
    release = github.release(tag)
    assets = github.assets(release['id'])
    metadata = directory / 'manifest.json'
    if assets['manifest.json']['size'] > 8 * BLOCK:
        raise ValueError('cache inventory exceeds metadata limit')
    github.download(assets['manifest.json'], metadata, assets['manifest.json']['size'])
    value = execution._read_json(metadata)
    read_bundle(metadata, value['bundle_sha256'])
    prefix = 'prepared-cache/' + key + '/'
    if (value['kind'] != 'prepared-core' or value['identity'] != identity
            or {r['path'] for r in value['files']} != {prefix + 'manifest.json', prefix + 'index.sqlite3'}
            or any(r['absent'] for r in value['files'])):
        raise ValueError('optional cache content-address identity mismatch')
    return value, assets


def cache_restore():
    spec, operation, phase, index, minutes, start = settings()
    options = spec.get('prepared_cache', {})
    if options.get('enabled') is not True or operation != 'execute-period':
        return
    # The sealed, authorized recovery inventory establishes that a complete
    # prepared replay will be installed; never download an unnecessary index.
    recovery_path = BASE / 'transfers/recovery-manifest.json'
    if recovery_path.exists():
        from market_analysis.historical_study_bundles import period_root
        manifest, coverage = frozen_inputs(spec)
        period = manifest.selected_periods[index]
        prefix = str(period_root(spec['campaign_id'], period).relative_to(BASE)) + '/'
        recovered = read_bundle(recovery_path, os.environ['STUDY_RESUME_SHA'])
        paths = {row['path'] for row in recovered['files'] if not row['absent']}
        if prefix + 'prepared-replay.json' in paths and prefix + f'checkpoint-{period.end_boundary_time_ms}.json' in paths:
            events.emit('CACHE_NOT_NEEDED', reason='completed-replay')
            return
    manifest, coverage = frozen_inputs(spec)
    prerequisites(spec, manifest, coverage, phase, os.environ.get('STUDY_ALLOW_TEST') == 'true')
    identity, key, tag = cache_request(spec, index)
    directory = BASE / 'transfers/cache-restore'
    directory.mkdir(parents=True, exist_ok=False)
    try:
        deadline = Deadline(start + (minutes - 15) * 60)
        github = GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), deadline)
        with events.span('prepared-cache-transfer'):
            value, assets = cache_inventory(github, tag, identity, key, directory)
            if (value['transfer_bytes'] > options['max_bytes'] or value['uncompressed_bytes'] > options['max_bytes']
                    or shutil_free() < value['transfer_bytes'] + value['uncompressed_bytes'] + options['headroom_bytes']):
                events.emit('CACHE_MISS', reason='download-headroom-or-size', cache_identity=key)
                return
            github.fetch_parts(value, assets, directory / 'parts')
            unpack_files(value, directory / 'parts', directory / 'staging', max_bytes=options['max_bytes'],
                         headroom_bytes=options['headroom_bytes'], check=deadline.check)
            # Install the complete sealed local candidate atomically. Python
            # validates SQLite/typed metadata before granting a scientific hit.
            candidate = directory / 'staging' / 'prepared-cache' / key
            destination = BASE / 'prepared-cache' / key
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                raise ValueError('optional immutable cache already exists')
            os.rename(candidate, destination)
            events.emit('CACHE_DOWNLOADED', cache_identity=key)
    except InterruptedError:
        raise
    except (OSError, ValueError, RuntimeError, KeyError, TimeoutError):
        events.emit('CACHE_MISS', reason='optional-cache-unavailable', cache_identity=key)
    finally:
        shutil.rmtree(directory)


def cache_publish():
    spec, operation, phase, index, minutes, start = settings()
    options = spec.get('prepared_cache', {})
    receipt = BASE / 'transfers/publication-receipt.json'
    if options.get('enabled') is not True or operation != 'execute-period' or not receipt.exists():
        return
    if execution._read_json(receipt).get('remote_published') is not True:
        return
    identity, key, tag = cache_request(spec, index)
    local = BASE / 'prepared-cache' / key
    deadline = Deadline(min(start + minutes * 60 - 45, time.time() + options['upload_max_seconds']))
    directory = BASE / 'transfers/cache-publication'
    try:
        if not local.exists() or deadline.remaining() < 15:
            events.emit('CACHE_UPLOAD_SKIPPED', reason='absent-or-reserve', cache_identity=key)
            return
        # Only successfully sealed caches built/restored for this exact identity
        # are eligible. The child reports validation; never upload a rejected hit.
        if not cache_was_validated(key):
            events.emit('CACHE_UPLOAD_SKIPPED', reason='not-validated', cache_identity=key)
            return
        with events.span('prepared-cache-upload'):
            github = GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), deadline)
            # Content-addressed immutable Releases. Existing publication is never
            # replaced; ambiguous drafts are left for owner inspection.
            try:
                existing = BASE / 'transfers/cache-existing'
                existing.mkdir(parents=True, exist_ok=True)
                cache_inventory(github, tag, identity, key, existing)
            except RuntimeError:
                pass
            else:
                events.emit('CACHE_UPLOAD_REUSED', cache_identity=key)
                return
            value = pack_files([(f'prepared-cache/{key}/' + name, local / name, {'role': 'optional-prepared-core'})
                                for name in ('manifest.json', 'index.sqlite3')], directory,
                                kind='prepared-core', identity=identity, check=deadline.check, max_bytes=options['max_bytes'])
            if value['transfer_bytes'] > options['max_bytes']:
                raise ValueError('optional upload size limit')
            github.publish(tag, directory)
            events.emit('CACHE_UPLOADED', cache_identity=key)
    except (OSError, ValueError, RuntimeError, KeyError, TimeoutError):
        events.emit('CACHE_UPLOAD_SKIPPED', reason='optional-upload-failure', cache_identity=key)
    finally:
        if directory.exists():
            shutil.rmtree(directory)
        existing = BASE / 'transfers/cache-existing'
        if existing.exists():
            shutil.rmtree(existing)


def current_events(root):
    suffix = '-' + identifier(os.environ['GITHUB_RUN_ID']) + '-' + identifier(os.environ['GITHUB_RUN_ATTEMPT']) + '.events.jsonl'
    for path in sorted((root / 'operations').glob('*' + suffix)):
        with path.open('rb') as handle:
            read_bytes = 0
            while read_bytes < events.MAX_LOG_BYTES:
                raw = handle.readline(events.MAX_EVENT_BYTES + 1)
                if not raw:
                    break
                read_bytes += len(raw)
                if len(raw) > events.MAX_EVENT_BYTES or not raw.endswith(b'\n'):
                    continue
                try:
                    row = json.loads(raw)
                    if isinstance(row, dict) and row.get('version') == events.VERSION:
                        yield events.allowlisted(row)
                except (ValueError, UnicodeError):
                    continue


def cache_was_validated(key):
    spec, *_ = settings()
    root = BASE / 'campaigns' / spec['campaign_id']
    return any(row.get('cache_identity') == key and row.get('category') in ('CACHE_BUILT', 'CACHE_HIT')
               for row in current_events(root))


def summary():
    destination = os.environ.get('GITHUB_STEP_SUMMARY')
    if not destination:
        return
    status, receipt, measurements = {}, {}, {}
    try:
        spec, _, _, _, _, start = settings()
        root = BASE / 'campaigns' / spec['campaign_id']
        path = root / 'operations/status.json'
        status = execution._read_json(path) if path.exists() else {}
        path = BASE / 'transfers/publication-receipt.json'
        receipt = execution._read_json(path) if path.exists() else {}
        timings, counters, resources, unfinished = {}, {}, {}, {}
        latest_replay = status.get('computed_replay_progress', {})
        for row in current_events(root):
            if row.get('category') in ('REPLAY_CURRENT_PROGRESS', 'REPLAY_RESUMED'):
                latest_replay = row
            if 'duration_seconds' in row and row.get('category') == 'HEARTBEAT':
                unfinished[row.get('stage', 'operation')] = row['duration_seconds']
            elif 'duration_seconds' in row:
                label = row.get('stage', row.get('operation', 'operation')) + ':' + row.get('category', 'unknown')
                timings[label] = round(timings.get(label, 0) + row['duration_seconds'], 3)
            for key in ('requests_completed', 'assets_completed', 'downloaded_bytes', 'retries'):
                counters[key] = counters.get(key, 0) + row.get(key, 0)
            for key in ('parent_rss_bytes', 'worker_rss_bytes', 'parent_peak_rss_bytes', 'worker_peak_rss_bytes', 'parent_cpu_seconds', 'worker_cpu_seconds', 'used_disk_bytes'):
                resources[key] = max(resources.get(key, 0), row.get(key, 0))
            if 'free_disk_bytes' in row:
                resources['minimum_free_disk_bytes'] = min(resources.get('minimum_free_disk_bytes', row['free_disk_bytes']), row['free_disk_bytes'])
        status['computed_replay_progress'] = latest_replay
        measurements = {'sampled_job_elapsed_seconds': round(time.time() - start, 3),
            'timings_seconds': timings, 'last_live_unit_elapsed_seconds': unfinished,
            'transfer': counters, 'sampled_resources': resources,
            'sampled_disk_high_water_bytes': status.get('sampled_disk_high_water_bytes'),
            'last_disk_sample': status.get('disk', {}).get('free_bytes')}
    except (OSError, ValueError, KeyError):
        pass  # A setup failure must still have a bounded public summary.
    state = status.get('scientific_state', 'NOT_STARTED')
    public = {'scientific_state': state if state in ('NOT_STARTED', 'FAILED', 'YIELDED', 'FINALIZED_PERIOD') else 'FAILED',
              'publication_state': 'VERIFIED' if receipt.get('remote_published') is True else 'FAILED' if os.environ.get('STUDY_PUBLICATION_OUTCOME') == 'failure' else 'NOT_VERIFIED',
              'scientific_step': os.environ.get('STUDY_SCIENTIFIC_OUTCOME', 'unknown'),
              'snapshot_step': os.environ.get('STUDY_SNAPSHOT_OUTCOME', 'unknown'),
              'publication_step': os.environ.get('STUDY_PUBLICATION_OUTCOME', 'unknown'),
              'new_stages': len(status.get('newly_completed_stages', [])),
              'reused_stages': len(status.get('reused_stages', [])),
              'verified_committed_units': status.get('verified_committed_work_count'),
              'verified_new_units': status.get('verified_new_work_count'), **measurements}
    if 'control' in locals().get('spec', {}):
        claim_path = BASE / 'transfers/control-claim.json'
        if claim_path.exists():
            control_record = execution._read_json(claim_path)
            public['campaign'] = {'campaign_id': spec['campaign_id'], 'task': control_record['handoff']['task'],
                'sequence': control_record['sequence'], 'reserved_minutes': control_record['reserved_minutes'],
                'budget': spec['budget'], 'private_status_path': 'campaigns/' + spec['campaign_id'] + '/status.json',
                'result_location': 'private data repository Releases; exact references in control.json'}
    try:
        from github_campaign import safe_diagnostic
        path = BASE / 'transfers/operation-outcomes.json'
        details = execution._read_json(path) if path.exists() else []
        public['diagnostics'] = [safe_diagnostic(d) for d in details[-8:] if isinstance(d, dict)]
        if 'spec' in locals():
            public['diagnostic_location'] = 'campaigns/' + identifier(spec['campaign_id']) + '/diagnostics/' + identifier(os.environ['GITHUB_RUN_ID']) + '-' + identifier(os.environ['GITHUB_RUN_ATTEMPT']) + '-<operation>.json'
    except (OSError, ValueError, KeyError):
        pass
    public.update(events.allowlisted({'reason': status.get('yield_reason'), 'category': status.get('failure_category')}))
    progress = status.get('computed_replay_progress', {})
    public['computed_replay'] = events.allowlisted(progress)
    last = status.get('last_verified_unit') or {}
    public['committed_progress'] = events.allowlisted({**last, 'period': last.get('study_period_index'), 'durable_boundaries': last.get('completed_output_boundaries')})
    # Exact receipt values are validated before rendering, never arbitrary strings.
    if receipt.get('remote_published') is True:
        public['continuation'] = {'resume_generation': identifier(receipt['generation']),
                                  'resume_manifest_sha': sha(receipt['manifest_sha256'])}
    with open(destination, 'a') as output:
        if 'campaign' in public:
            campaign_view = public['campaign']
            output.write('Campaign **' + campaign_view['campaign_id'] + '** — task `' + campaign_view['task']['id'] + '`, slice ' + str(campaign_view['sequence']) + '.\n\n')
            output.write('Scientific state: **' + public['scientific_state'] + '**; publication: **' + public['publication_state'] + '**. New/reused stages: ' + str(public['new_stages']) + '/' + str(public['reused_stages']) + '. Reserved budget: ' + str(campaign_view['reserved_minutes']) + '/' + str(spec['budget']['ceiling_minutes']) + ' minutes.\n\n')
            output.write('Private live view: `campaigns/' + spec['campaign_id'] + '/status.md`. Finalized outputs and exact receipts are in `control.json` and private Releases.\n\n')
        output.write('Historical slice (operational observations, not whole-campaign completion).\n\n')
        output.write('Replay counters describe computed boundaries; committed progress describes verified durable units. Stage counts do not imply period or campaign completion.\n\n```json\n')
        output.write(json.dumps(public, sort_keys=True, indent=2)[:24000] + '\n```\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bootstrap', 'validate-settings', 'metadata', 'inputs', 'restore',
                                            'validate-parents', 'install-dependencies', 'dependency-key', 'cache-restore', 'cache-publish', 'summary', 'run', 'snapshot', 'publish', 'record-receipt'])
    args = parser.parse_args()
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError('platform cancellation')))
    try:
        if args.command == 'summary':
            summary()
            return 0
        minutes = int(os.environ['STUDY_JOB_MINUTES'])
        if not 16 <= minutes <= 350:
            raise ValueError('job budget must be 16..350 minutes')
        reserve = 30 if args.command in ('summary', 'cache-publish') else 60 if args.command in ('publish', 'record-receipt') else 120 if args.command == 'snapshot' else 780 if args.command in ('run', 'validate-parents') else 900
        remaining = float(os.environ['STUDY_JOB_STARTED_EPOCH']) + minutes * 60 - reserve - time.time()
        if remaining <= 0:
            if args.command not in ('snapshot', 'publish', 'record-receipt', 'cache-publish'):
                raise OperationalInterruption('SETUP_BUDGET')
            raise TimeoutError('no operational time remains')
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(OperationalInterruption('SETUP_BUDGET') if args.command not in ('run', 'snapshot', 'publish', 'record-receipt', 'cache-publish') else TimeoutError('operational deadline')))
        signal.setitimer(signal.ITIMER_REAL, remaining)
        if args.command != 'bootstrap':
            spec, operation, phase, index, _, start = settings()
            if 'control' in spec and args.command != 'validate-settings':
                checked_claim(spec, allow_stop=True)
            event_path = BASE / 'campaigns' / spec['campaign_id'] / 'operations' / (args.command + '-' + identifier(os.environ['GITHUB_RUN_ID']) + '-' + identifier(os.environ['GITHUB_RUN_ATTEMPT']) + '.events.jsonl')
            events.configure(event_path, operation=args.command, phase=phase, period=index, deadline_epoch=start + (minutes - 15) * 60, public=True)
        if args.command == 'bootstrap':
            bootstrap()
        elif args.command == 'validate-settings':
            spec, _, _, _, minutes, _ = settings()
            budget_parent(spec, minutes)
        elif args.command in ('metadata', 'inputs'):
            with events.span(args.command):
                fetch_inputs(args.command)
        elif args.command == 'restore':
            with events.span('recovery-transfer'):
                restore()
        elif args.command == 'dependency-key':
            dependency_key()
        elif args.command == 'cache-restore':
            cache_restore()
        elif args.command == 'cache-publish':
            cache_publish()
        elif args.command == 'summary':
            summary()
        elif args.command == 'install-dependencies':
            _, _, _, _, minutes, start = settings()
            remaining = start + (minutes - 15) * 60 - time.time()
            if remaining <= 0:
                raise OperationalInterruption('SETUP_BUDGET')
            with events.span('dependency-install'):
                subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(REPO / 'python/requirements.txt'),
                                '-r', str(REPO / 'python/requirements-research.txt')], check=True,
                               timeout=remaining, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif args.command in ('run', 'validate-parents'):
            return supervise(args.command == 'validate-parents')
        elif args.command == 'snapshot':
            snapshot()
        elif args.command == 'record-receipt':
            record_receipt()
        else:
            with events.span('publication'):
                return publish()
        return 0
    except BaseException as exc:
        from github_campaign import safe_error
        details = safe_error(exc, args.command)
        if isinstance(exc, OperationalInterruption):
            details['category'] = exc.code
        elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) and (args.command not in ('run', 'snapshot', 'publish', 'record-receipt', 'cache-publish') or args.command == 'run' and not (BASE / 'transfers/science-started').exists()):
            details['category'] = 'SETUP_BUDGET'
        if args.command == 'validate-parents' and 'spec' in locals():
            # Cleanup/relay may itself be cancelled after the validator has
            # already established a genuine failure. Do not erase that evidence.
            try:
                status_path = BASE / 'campaigns' / spec['campaign_id'] / 'operations/status.json'
                status = execution._read_json(status_path) if status_path.exists() else {}
                prior_failure = status.get('failure_category')
                if prior_failure not in (None, 'InterruptedError'):
                    details['category'] = 'INTEGRITY' if prior_failure in ('ValueError', 'KeyError') else 'PROCESS'
            except (OSError, ValueError, KeyError):
                details['category'] = 'INTEGRITY'
        retain_outcome(details)
        if events._sink is not None:
            events.emit('OPERATION_FAILED', reason=details['category'])
        print('Historical platform halted: ' + json.dumps(details, sort_keys=True))
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
