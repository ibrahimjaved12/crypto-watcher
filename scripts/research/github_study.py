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
)
from market_analysis.historical_run_directory import owned_run_directory


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
                        return response
                    except urllib.error.HTTPError as exc:
                        if exc.code in (301, 302, 303, 307, 308) and method == 'GET':
                            current = urllib.parse.urljoin(current, exc.headers.get('Location', ''))
                            exc.close()
                            continue
                        code = exc.code
                        exc.close()
                        # Mutations are not blindly retried: an ambiguous draft or
                        # upload must remain uncommitted, not silently overwrite.
                        if method != 'GET' or code not in (429, 500, 502, 503, 504):
                            raise RuntimeError('GitHub request failed (HTTP %d)' % code) from None
                        break
                else:
                    raise ValueError('too many download redirects')
            except (urllib.error.URLError, TimeoutError, OSError):
                if method != 'GET':
                    raise RuntimeError('GitHub mutation interrupted; draft is not verified recovery') from None
            finally:
                if upload:
                    upload.close()
            if attempt < 2:
                time.sleep(min(2 ** attempt, max(0, self.deadline.remaining())))
        raise RuntimeError('GitHub read failed after bounded retries')

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
                count = 0
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
                    output.flush()
                    os.fsync(output.fileno())
                if count != size:
                    raise OSError('truncated download')
                actual = file_sha(temporary, self.deadline.check)
                if digest and actual != digest:
                    raise ValueError('downloaded asset SHA mismatch')
                if asset.get('digest') and asset['digest'] != 'sha256:' + actual:
                    raise ValueError('GitHub asset digest mismatch')
                os.replace(temporary, destination)
                return
            except (OSError, TimeoutError):
                temporary.unlink(missing_ok=True)
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
        for row in value['files']:
            for part in row['parts']:
                path = directory / part['asset']
                if path.exists() and regular(path).st_size == part['size'] and file_sha(path, self.deadline.check) == part['sha256']:
                    continue
                self.download(assets[part['asset']], path, part['size'], part['sha256'])

    def verify_lineage(self, value, spec):
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
        release = self.json(self.prefix + '/releases', 'POST', {
            'tag_name': tag, 'target_commitish': self.branch, 'name': tag,
            'draft': True, 'prerelease': True, 'make_latest': 'false',
            'body': 'Private operational recovery; use exact locator and sealed inventory hash.'})
        for path in sorted(Path(directory).iterdir()):
            if path.name != 'manifest.json':
                self.upload_verified(release, path)
        self.upload_verified(release, Path(directory) / 'manifest.json')
        result = self.json(self.prefix + '/releases/' + str(release['id']), 'PATCH', {'draft': False})
        if result.get('draft') is not False:
            raise ValueError('remote generation publication was not verified')
        return result


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
    old = recovery['metadata']['lineage'] if recovery else {'reserved_minutes': 0, 'run_count': 0, 'no_progress_runs': 0,
                                                          'sampled_cumulative_runner_minutes': 0}
    if (old['reserved_minutes'] + minutes > spec['budget']['ceiling_minutes']
            or old['run_count'] + 1 > spec['budget']['max_runs']
            or old['no_progress_runs'] >= spec['budget']['no_progress_cap']):
        raise ValueError('campaign allocation/retry/no-progress cap reached; owner must explicitly revise the campaign')
    return old


def subset(value, predicate):
    result = {**value, 'files': [row for row in value['files'] if predicate(row)]}
    result['uncompressed_bytes'] = result['transfer_bytes'] = sum(row['size'] for row in result['files'])
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
    github = GitHub(os.environ.get('RESEARCH_DATA_REPOSITORY'), deadline)
    if command == 'metadata':
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
        if not row['path'].startswith(('manifests/', 'inputs/')):
            raise ValueError('input inventory declares an unauthorized installation root')
    selected = subset(value, lambda row: row['path'].startswith('manifests/') if command == 'metadata'
                      else row['path'].startswith('inputs/') and operation != 'aggregate')
    limits(spec, selected)
    github.fetch_parts(selected, assets, BASE / 'transfers/input-parts')
    stage = BASE / 'transfers' / ('metadata-staging' if command == 'metadata' else 'input-staging')
    unpack_files(selected, BASE / 'transfers/input-parts', stage, max_bytes=spec['limits']['max_uncompressed_bytes'],
                 headroom_bytes=spec['limits']['headroom_bytes'], check=deadline.check)
    install_files(selected, stage)


def restore():
    spec, operation, phase, index, minutes, start = settings()
    tag = os.environ.get('STUDY_RESUME_GENERATION', '')
    expected = os.environ.get('STUDY_RESUME_SHA', '')
    if bool(tag) != bool(expected):
        raise ValueError('resume requires both exact generation and manifest SHA')
    if not tag:
        budget_parent(spec, minutes)
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
    for row in value['files']:
        if not row['path'].startswith('campaigns/' + spec['campaign_id'] + '/'):
            raise ValueError('recovery inventory escapes campaign root')
    if (any('-test' in row['path'] for row in value['files']) and phase != 'test'
            or any('-validation' in row['path'] for row in value['files']) and phase == 'development'):
        raise ValueError('recovery contains later-phase evidence; dispatch its explicit authorized phase')
    limits(spec, value)
    github.fetch_parts(value, assets, BASE / 'transfers/recovery-parts')
    unpack_files(value, BASE / 'transfers/recovery-parts', BASE / 'transfers/recovery-staging',
                 max_bytes=spec['limits']['max_uncompressed_bytes'], headroom_bytes=spec['limits']['headroom_bytes'],
                 check=deadline.check)


def batch_arguments(operation, spec, phase, index, seconds):
    arguments = [sys.executable, '-m', 'market_analysis.historical_study_batch', operation,
                 '--campaign-spec', str(BASE / 'transfers/campaign.json'), '--phase', phase,
                 '--max-new-stages', str(spec.get('max_new_stages', 1)),
                 '--max-elapsed-seconds', str(max(1, int(seconds)))]
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


def supervise(validate_only=False):
    spec, operation, phase, index, minutes, start = settings()
    deadline = Deadline(start + (minutes - 15) * 60)
    root = BASE / 'campaigns' / spec['campaign_id']
    operations = root / 'operations'
    operations.mkdir(parents=True, exist_ok=True)
    recovery = (read_bundle(BASE / 'transfers/recovery-manifest.json', os.environ['STUDY_RESUME_SHA'])
                if (BASE / 'transfers/recovery-manifest.json').exists() else None)
    budget_parent(spec, minutes, recovery)
    deadline.check()
    if deadline.remaining() <= 30:
        raise TimeoutError('insufficient launch time; preceding remote generation remains authoritative')
    arguments = batch_arguments('validate-parents' if validate_only else operation, spec, phase, index, deadline.remaining() - 30)
    log_name = ('parents' if validate_only else 'batch') + '-' + identifier(os.environ['GITHUB_RUN_ID']) + '-' + identifier(os.environ['GITHUB_RUN_ATTEMPT']) + '.log'
    with (operations / log_name).open('wb') as log:
        child = subprocess.Popen(arguments, cwd=REPO / 'python', env=token_free_environment(),
                                 start_new_session=True, stdout=log, stderr=log, close_fds=True)
        interrupted = False
        try:
            result = child.wait(timeout=max(1, deadline.remaining()))
        except BaseException:
            interrupted = True
            try:
                child.terminate()  # Python unwinds its owned worker and dataset.
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            finally:
                # Stop the whole scientific process group, including a surviving
                # interpreter after uncatchable parent failure, before snapshot.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            result = 1
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        # A crashed parent may have left a worker. Never snapshot it alive.
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    status_path = operations / 'status.json'
    if not validate_only and (interrupted or not status_path.exists()):
        execution._replace_atomic(status_path, execution._canonical({
            'version': STATUS_VERSION, 'identity': campaign_identity(spec), 'scientific_state': 'FAILED',
            'failure_category': 'SupervisorDeadline' if interrupted else 'ValidationOrProcessFailure',
            'remote_published': False, 'last_verified_unit': None,
            'disk': disk_sample(root)}))
    return result


def snapshot():
    spec, operation, phase, index, minutes, start = settings()
    deadline = Deadline(start + minutes * 60 - 120)
    manifest, coverage = frozen_inputs(spec)
    prerequisites(spec, manifest, coverage, phase, os.environ.get('STUDY_ALLOW_TEST') == 'true')
    recovery = (read_bundle(BASE / 'transfers/recovery-manifest.json', os.environ['STUDY_RESUME_SHA'])
                if (BASE / 'transfers/recovery-manifest.json').exists() else None)
    old = budget_parent(spec, minutes, recovery)
    root = BASE / 'campaigns' / spec['campaign_id']
    # Ownership remains held from validation through every packed byte; no
    # cleanup is done by a snapshot and an orphan worker makes acquisition fail.
    with ExitStack() as stack:
        stack.enter_context(owned_run_directory(root, cleanup=False))
        for period_root in sorted((root / 'checkpoints').glob('period-*')):
            stack.enter_context(owned_run_directory(period_root, cleanup=False))
        files, work = verify_recovery_tree(BASE, spec['campaign_id'], manifest, coverage,
                                         campaign_identity(spec), check=deadline.check, locks_held=True)
        status_path = root / 'operations/status.json'
        status = execution._read_json(status_path) if status_path.exists() else {'scientific_state': 'FAILED'}
        previous_work = recovery['metadata']['completed_work'] if recovery else []
        if not set(previous_work).issubset(work):
            raise ValueError('snapshot cannot discard parent committed work after failed restore')
        lineage = {'reserved_minutes': execution._read_json(BASE / 'transfers/allocation-total.json')['reserved_minutes'], 'run_count': old['run_count'] + 1,
                   'no_progress_runs': old['no_progress_runs'] + 1 if work == previous_work else 0,
                   'sampled_cumulative_runner_minutes': old['sampled_cumulative_runner_minutes'] + (time.time() - start) / 60}
        tag = f"recovery-{spec['campaign_id'][:20]}-{int(os.environ['GITHUB_RUN_ID'])}-{int(os.environ['GITHUB_RUN_ATTEMPT'])}"
        identifier(tag)
        value = pack_files([(str(path.relative_to(BASE)), path, {'role': 'committed-recovery'}) for path in files],
            BASE / 'transfers/publication', kind='recovery', identity=campaign_identity(spec),
            metadata={'generation': tag, 'parent': {'generation': os.environ.get('STUDY_RESUME_GENERATION'),
                        'manifest_sha256': os.environ.get('STUDY_RESUME_SHA')} if recovery else None,
                      'lineage': lineage, 'completed_work': work, 'scientific_state': status['scientific_state'],
                      'source_identities': coverage['source_identities'], 'disk': disk_sample(root),
                      'runner_time_sampling_boundary': 'before pack/upload; allocation covers full requested job'},
            check=deadline.check, max_bytes=spec['limits']['max_uncompressed_bytes'])
        if value['transfer_bytes'] > spec['limits']['max_transfer_bytes']:
            raise BundleFootprintError('recovery-transfer-limit', transfer_bytes=value['transfer_bytes'],
                                       limit_bytes=spec['limits']['max_transfer_bytes'])


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
    # Explicitly allowlisted, small metadata only; never source/report/log bytes.
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a') as output:
            output.write('Private recovery verified.\n\n```json\n' + execution._canonical(receipt) + '\n```\n')
    return 1 if receipt['scientific_state'] == 'FAILED' else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bootstrap', 'validate-settings', 'metadata', 'inputs', 'restore',
                                            'validate-parents', 'install-dependencies', 'run', 'snapshot', 'publish'])
    args = parser.parse_args()
    previous = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(InterruptedError('platform cancellation')))
    try:
        minutes = int(os.environ['STUDY_JOB_MINUTES'])
        if not 16 <= minutes <= 350:
            raise ValueError('job budget must be 16..350 minutes')
        reserve = 60 if args.command == 'publish' else 120 if args.command == 'snapshot' else 870 if args.command in ('run', 'validate-parents') else 900
        remaining = float(os.environ['STUDY_JOB_STARTED_EPOCH']) + minutes * 60 - reserve - time.time()
        if remaining <= 0:
            raise TimeoutError('no operational time remains')
        signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(TimeoutError('operational deadline')))
        signal.setitimer(signal.ITIMER_REAL, remaining)
        if args.command == 'bootstrap':
            bootstrap()
        elif args.command == 'validate-settings':
            spec, _, _, _, minutes, _ = settings()
            budget_parent(spec, minutes)
        elif args.command in ('metadata', 'inputs'):
            fetch_inputs(args.command)
        elif args.command == 'restore':
            restore()
        elif args.command == 'install-dependencies':
            _, _, _, _, minutes, start = settings()
            remaining = start + (minutes - 15) * 60 - time.time()
            if remaining <= 0:
                raise TimeoutError('no setup time remains')
            subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', str(REPO / 'python/requirements.txt'),
                            '-r', str(REPO / 'python/requirements-research.txt')], check=True,
                           timeout=remaining)
        elif args.command in ('run', 'validate-parents'):
            return supervise(args.command == 'validate-parents')
        elif args.command == 'snapshot':
            snapshot()
        else:
            return publish()
        return 0
    except BaseException as exc:
        # Never expose urllib exceptions, signed URLs, tokens or source rows.
        if isinstance(exc, BundleFootprintError):
            print('Measured footprint rejected; revise declared limits/partitioning or disk provisioning: ' +
                  execution._canonical(exc.measurements))
        print('Historical platform operation failed: ' + type(exc).__name__ +
              '. Check configured pins, original private bundles, budget and disk limits.')
        return 1
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    raise SystemExit(main())
