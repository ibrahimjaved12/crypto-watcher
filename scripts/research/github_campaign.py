#!/usr/bin/env python3
"""Small authenticated campaign transport. Stdlib only; no scientific imports.

Private Contents CAS is authoritative. Dispatch is bounded and may be ambiguous;
children must claim before acquiring data. This module never runs scientific CLI.
"""
import argparse
import base64
from datetime import datetime
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / 'python/market_analysis'))
import historical_campaign_control as control
MAX = 8 * 1024 * 1024


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate JSON field')
        result[key] = value
    return result


def read(raw):
    if len(raw) > MAX:
        raise ValueError('bounded control metadata exceeded')
    return json.loads(raw, object_pairs_hook=pairs)


def spec_path():
    path = os.environ['STUDY_CAMPAIGN_SPEC']
    if not re.fullmatch(r'research/campaigns/[A-Za-z0-9_.-]+\.json', path):
        raise ValueError('trusted campaign path required')
    return REPO / path


def pinned_spec():
    transferred = Path('/tmp/crypto-study/transfers/campaign.json')
    spec = read(transferred.read_bytes() if transferred.exists() else spec_path().read_bytes())
    control.plan(spec)
    for field in ('runtime_sha', 'orchestration_sha', 'producer_revision'):
        if not re.fullmatch('[0-9a-f]{40}', spec[field] or ''):
            raise ValueError('full revision pin required')
    if spec['runtime_sha'] != spec['orchestration_sha']:
        raise ValueError('one runtime/orchestration pin required')
    head = subprocess.run(['git', '-C', str(REPO), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True).stdout.strip()
    dirty = subprocess.run(['git', '-C', str(REPO), 'status', '--porcelain', '--untracked-files=all', '--', 'python/market_analysis', 'scripts/research', '.github/workflows'], check=True, capture_output=True, text=True).stdout.strip()
    if head != spec['runtime_sha'] or dirty:
        raise ValueError('control must execute at exact pinned runtime')
    for name, expected in spec['dependency_locks'].items():
        if name not in ('requirements.txt', 'requirements-research.txt') or hashlib.sha256((REPO / 'python' / name).read_bytes()).hexdigest() != expected:
            raise ValueError('actual dependency bytes differ from pins')
    return spec


def bootstrap():
    raw = spec_path().read_bytes()
    spec = read(raw)
    control.plan(spec)
    for pin in (spec['runtime_sha'], spec['orchestration_sha']):
        if not re.fullmatch('[0-9a-f]{40}', pin or ''):
            raise ValueError('full trusted pin required')
        subprocess.run(['git', '-C', str(REPO), 'merge-base', '--is-ancestor', pin, 'origin/main'], check=True)
    if spec['runtime_sha'] != spec['orchestration_sha']:
        raise ValueError('control/runtime pins differ')
    transfer = Path('/tmp/crypto-study/transfers')
    transfer.mkdir(parents=True, exist_ok=True)
    (transfer / 'campaign.json').write_bytes(raw)
    subprocess.run(['git', '-C', str(REPO), 'checkout', '--detach', spec['runtime_sha']], check=True)
    subprocess.run([sys.executable, str(REPO / 'scripts/research/github_campaign.py'), 'validate'], check=True)


class API:
    def __init__(self, repository, token, *, private=False):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or '') or not token:
            raise ValueError('scoped repository credential required')
        self.repository, self.token = repository, token
        self.prefix = 'https://api.github.com/repos/' + repository
        self.opener = urllib.request.build_opener(NoRedirect())
        self.end = time.monotonic() + 240
        self.timeout = 30
        details = self.request('')
        if private and (details.get('private') is not True or repository.lower() == os.environ['GITHUB_REPOSITORY'].lower()):
            raise ValueError('separate private data repository required')
        self.branch = details['default_branch']

    def request(self, path, method='GET', body=None, missing=False):
        if time.monotonic() >= self.end:
            raise TimeoutError('bounded control deadline')
        if (path and not path.startswith('/')) or '://' in path:
            raise ValueError('repository-relative API path required')
        raw = control.canonical(body).encode() if body is not None else None
        request = urllib.request.Request(self.prefix + path, data=raw, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'crypto-campaign-control'})
        try:
            with self.opener.open(request, timeout=max(1, min(self.timeout, self.end - time.monotonic()))) as response:
                payload = response.read(MAX + 1)
                return read(payload) if payload else None
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 and missing:
                return None
            if code in (409, 422):
                raise Conflict() from None
            raise RuntimeError('control API HTTP %d' % code) from None
        except (urllib.error.URLError, OSError):
            raise RuntimeError('control API response ambiguous') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Conflict(Exception):
    pass


class Store:
    def __init__(self, spec):
        self.spec = spec
        self.api = API(os.environ.get('RESEARCH_DATA_REPOSITORY'), os.environ.get('RESEARCH_DATA_TOKEN'), private=True)
        self.path = '/contents/campaigns/' + control.identifier(spec['campaign_id']) + '/control.json'

    def get(self):
        row = self.api.request(self.path + '?ref=' + urllib.parse.quote(self.api.branch, safe=''), missing=True)
        if row is None:
            history = self.api.request('/commits?path=' + urllib.parse.quote(self.path[len('/contents/'):], safe='') + '&sha=' + urllib.parse.quote(self.api.branch, safe='') + '&per_page=1')
            if history:
                raise ValueError('committed campaign authority was deleted; refusing accounting reset')
            return None, None
        if row['encoding'] != 'base64':
            raise ValueError('control record exceeds Contents bound')
        current = control.validate(read(base64.b64decode(row['content'])), self.spec)
        # Git commit history anchors append-only entries across separate control
        # invocations, not just within one process's conflict retry loop.
        history = self.api.request('/commits?path=' + urllib.parse.quote(self.path[len('/contents/'):], safe='') + '&sha=' + urllib.parse.quote(self.api.branch, safe='') + '&per_page=2')
        if not history:
            raise ValueError('control record lacks committed Git ancestry')
        latest = self.api.request(self.path + '?ref=' + history[0]['sha'])
        if latest['sha'] != row['sha']:
            raise Conflict('control changed while reading committed history')
        if len(history) > 1:
            prior = self.api.request(self.path + '?ref=' + history[1]['sha'])
            previous = control.validate(read(base64.b64decode(prior['content'])), self.spec)
            control.validate(current, self.spec, previous)
        return current, row['sha']

    def update(self, mutate, *, create=False):
        previous = None
        for _ in range(4):
            try:
                record, revision = self.get()
            except Conflict:
                continue
            if record is None:
                if not create:
                    raise ValueError('campaign has not been deliberately started')
                record = control.initial(self.spec)
            control.validate(record, self.spec, previous)
            previous = deepcopy(record)
            result = mutate(record)
            control.validate(record, self.spec, previous)
            if record == previous and revision:
                return record, result
            encoded = (control.canonical(record) + '\n').encode()
            if len(encoded) > 1024 * 1024:
                raise ValueError('small Contents authority exceeds one MiB; accounting cannot be truncated')
            payload = {'message': 'Campaign control ' + self.spec['campaign_id'], 'branch': self.api.branch,
                       'content': base64.b64encode(encoded).decode()}
            if revision:
                payload['sha'] = revision
            try:
                self.api.request(self.path, 'PUT', payload)
                return record, result
            except (Conflict, RuntimeError):
                # An ambiguous Contents write is reconciled by rereading. A claim
                # caller can prove its own exact run identity before entering.
                continue
        raise RuntimeError('control CAS conflict/reconciliation limit reached')

    def view(self, record, activity=None):
        h = record['handoff']
        status = {'version': 'historical-campaign-view-v1', 'identity': record['identity'],
                  'state': record['state'], 'task': h['task'] if h else None, 'sequence': record['sequence'],
                  'reason': record['reason'], 'stop_requested': record['stop_requested'],
                  'reserved_minutes': record['reserved_minutes'], 'measured_minutes': record['measured_minutes'],
                  'remaining_minutes': max(0, self.spec['budget']['ceiling_minutes'] - record['reserved_minutes']),
                  'budget': self.spec['budget'], 'tasks': record['tasks'], 'latest_verified_receipt': record['receipts'][-1] if record['receipts'] else None,
                  'finalized_results': [r for r in record['receipts'] if r['task_complete']],
                  'activity': activity, 'updated_epoch': time.time(),
                  'active_run': 'https://github.com/' + os.environ['GITHUB_REPOSITORY'] + '/actions/runs/' + h['run_id'] if h and h['run_id'] else None}
        path = self.path.replace('control.json', 'status.json')
        old = self.api.request(path + '?ref=' + urllib.parse.quote(self.api.branch, safe=''), missing=True)
        if activity is None and old and old.get('encoding') == 'base64':
            activity = read(base64.b64decode(old['content'])).get('activity')
            status['activity'] = activity
        body = {'message': 'Campaign status view', 'branch': self.api.branch,
                'content': base64.b64encode((control.canonical(status) + '\n').encode()).decode()}
        if old:
            body['sha'] = old['sha']
        try:
            self.api.request(path, 'PUT', body)
        except (Conflict, RuntimeError):
            pass # Non-authoritative view failure cannot undo durable accounting.
        task = status['task'] or {}
        receipt = status['latest_verified_receipt'] or {}
        markdown = ('# Campaign ' + self.spec['campaign_id'] + '\n\nState: **' + status['state'] + '**. Stop requested: ' + str(status['stop_requested'])
            + '\n\nTask: ' + str(task.get('id')) + '; phase: ' + str(task.get('phase')) + '; exact period: ' + str(task.get('period_index')) + '; slice: ' + str(status['sequence'])
            + '\n\nBudget: ' + str(status['reserved_minutes']) + ' reserved / ' + str(round(status['measured_minutes'], 2)) + ' sampled measured minutes; ' + str(status['remaining_minutes']) + ' unreserved minutes remain. Scientific runs: ' + str(record['run_count']) + '/' + str(self.spec['budget']['max_runs'])
            + '\n\nActive run: ' + str(status['active_run']) + '\n\nIntervention: ' + str(status['reason'])
            + '\n\nLatest verified recovery: `' + str(receipt.get('generation')) + '` / `' + str(receipt.get('manifest_sha256')) + '`'
            + '\n\nFinalized result references: ' + ', '.join('`' + r['generation'] + '`' for r in status['finalized_results'])
            + '\n\nLive observations (computed progress is separate from committed work):\n\n```json\n' + json.dumps(activity, indent=2) + '\n```\n\nThis view is informational; control.json and sealed inventories remain authoritative.\n')
        view_path = self.path.replace('control.json', 'status.md')
        old_view = self.api.request(view_path + '?ref=' + urllib.parse.quote(self.api.branch, safe=''), missing=True)
        view_body = {'message': 'Readable campaign status view', 'branch': self.api.branch, 'content': base64.b64encode(markdown.encode()).decode()}
        if old_view:
            view_body['sha'] = old_view['sha']
        try:
            self.api.request(view_path, 'PUT', view_body)
        except (Conflict, RuntimeError):
            pass
        return status


def run_name(h):
    t = h['task']
    return 'Campaign ' + h['identity']['campaign_id'] + ' / ' + t['id'] + ' / slice ' + str(h['sequence']) + ' / ' + t['phase'] + ' ' + str(t['period_index'] if t['period_index'] is not None else 0) + ' / ' + h['key']


def reconcile(store, actions, h):
    matches = []
    for page in range(1, 4):
        rows = actions.request('/actions/workflows/historical-study.yml/runs?event=workflow_dispatch&per_page=100&page=' + str(page))['workflow_runs']
        matches.extend(r for r in rows if r.get('display_title') == run_name(h))
        if len(rows) < 100:
            break
    if len(matches) > 1:
        raise ValueError('duplicate dispatches found; children remain claim guarded')
    return matches[0] if matches else None


def dispatch(store):
    record, _ = store.get()
    if record is None or record['stop_requested'] or record['state'] in control.TERMINAL:
        return
    h = record['handoff']
    if not h or h['state'] not in ('INTENT', 'AMBIGUOUS', 'DISPATCHED'):
        return
    if record['receipts']:
        try:
            latest_receipt = record['receipts'][-1]
            value = verify_receipt(store, latest_receipt)
            for finalized in value['metadata']['consolidation']['finalized_receipts']:
                verify_receipt(store, finalized)
        except (KeyError, ValueError, RuntimeError):
            store.update(lambda r: r.update(state='INTEGRITY_FAILED', reason='handoff-publication-or-finalized-closure-verification-failed'))
            raise
    if record['reserved_minutes'] + h['allocation_minutes'] > store.spec['budget']['ceiling_minutes'] or record['run_count'] >= store.spec['budget']['max_runs'] or record['no_progress_runs'] >= store.spec['budget']['no_progress_cap']:
        store.update(lambda r: r.update(state='BUDGET_EXHAUSTED', reason='dispatch-transition-cap'))
        return
    actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
    found = reconcile(store, actions, h)
    if found:
        def confirmed(r):
            if r['handoff']['key'] == h['key'] and r['handoff']['state'] != 'CLAIMED':
                r['handoff'].update(state='DISPATCHED', run_id=str(found['id']))
                control.append(r, 'DISPATCH_CONFIRMED', key=h['key'], run_id=str(found['id']))
        store.update(confirmed)
        return
    if h['state'] != 'INTENT':
        store.update(lambda r: r.update(reason='dispatch-ambiguous; bounded reconciliation found no run; owner intervention required'))
        return
    # Write attempt BEFORE POST; no blind POST retry after an uncertain response.
    def attempted(r):
        if r['stop_requested'] or r['handoff']['key'] != h['key'] or r['handoff']['state'] != 'INTENT':
            return False
        r['handoff']['state'] = 'AMBIGUOUS'
        control.append(r, 'DISPATCH_ATTEMPT', key=h['key'])
        return True
    _, allowed = store.update(attempted)
    if not allowed:
        return
    latest, _ = store.get()
    if latest['stop_requested']:
        return
    task = h['task']
    parent = h['parent']
    inputs = {'campaign_spec': os.environ['STUDY_CAMPAIGN_SPEC'], 'operation': task['operation'],
              'phase': task['phase'], 'period_index': str(task['period_index'] if task['period_index'] is not None else 0),
              'allow_test': str(task['phase'] == 'test' and store.spec['control']['allow_test']).lower(),
              'job_minutes': str(store.spec['job_minutes']), 'handoff_key': h['key'],
              'task_label': store.spec['campaign_id'] + ' / ' + task['id'] + ' / slice ' + str(h['sequence']),
              'resume_generation': parent['generation'] if parent else '',
              'resume_manifest_sha': parent['manifest_sha256'] if parent else ''}
    try:
        actions.request('/actions/workflows/historical-study.yml/dispatches', 'POST', {'ref': 'main', 'inputs': inputs})
    except RuntimeError:
        pass
    # Short bounded reconciliation, never occupy a runner awaiting child completion.
    for _ in range(3):
        found = reconcile(store, actions, h)
        if found:
            break
        time.sleep(2)
    def finish(r):
        if r['handoff']['key'] != h['key'] or r['handoff']['state'] == 'CLAIMED':
            return
        r['handoff'].update(state='DISPATCHED' if found else 'AMBIGUOUS', run_id=str(found['id']) if found else None)
        control.append(r, 'DISPATCH_OUTCOME', key=h['key'], run_id=str(found['id']) if found else None,
                       outcome='confirmed' if found else 'ambiguous')
        r['reason'] = None if found else 'dispatch-ambiguous; use Resume to reconcile; no blind redispatch'
    record, _ = store.update(finish)
    store.view(record)


def claim(store):
    key, run_id = os.environ.get('STUDY_HANDOFF_KEY'), os.environ['GITHUB_RUN_ID']
    record, entered = store.update(lambda r: control.claim(r, store.spec, key, run_id))
    h = record['handoff']
    # An ambiguous successful CAS may already contain our claim. A rerun is never
    # allowed: its attempt differs; the first invocation persists a local receipt.
    local = Path('/tmp/crypto-study/transfers/control-claim.json')
    if not entered:
        raise ValueError('duplicate/stale/stopped child refused before downloads')
    t, parent = h['task'], h['parent']
    expected = {'STUDY_OPERATION': t['operation'], 'STUDY_PHASE': t['phase'],
                'STUDY_JOB_MINUTES': str(store.spec['job_minutes']),
                'STUDY_PERIOD_INDEX': str(t['period_index'] if t['period_index'] is not None else 0),
                'STUDY_ALLOW_TEST': str(t['phase'] == 'test' and store.spec['control']['allow_test']).lower(),
                'STUDY_RESUME_GENERATION': parent['generation'] if parent else '',
                'STUDY_RESUME_SHA': parent['manifest_sha256'] if parent else ''}
    if any(os.environ.get(k, '') != v for k, v in expected.items()):
        raise ValueError('dispatch inputs differ from claimed pinned task')
    local.write_text(control.canonical(record) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('claimed=true\nmanaged=true\n')
    store.view(record)


def verify_receipt(store, receipt):
    """Independently reread immutable publication before accepting its transition."""
    tag = control.identifier(receipt['generation'])
    release = store.api.request('/releases/tags/' + urllib.parse.quote(tag, safe=''))
    if release['draft']:
        raise ValueError('unpublished receipt')
    assets = {}
    for page in range(1, 12):
        rows = store.api.request('/releases/' + str(release['id']) + '/assets?per_page=100&page=' + str(page))
        for row in rows:
            if row['name'] in assets:
                raise ValueError('duplicate remote asset')
            assets[row['name']] = row
        if len(rows) < 100:
            break
    else:
        raise ValueError('bounded asset inventory exceeded')
    # Manifest only, with bearer restricted to api.github.com; no signed redirects.
    asset = assets['manifest.json']
    request = urllib.request.Request(store.api.prefix + '/releases/assets/' + str(asset['id']), headers={
        'Authorization': 'Bearer ' + store.api.token, 'Accept': 'application/octet-stream', 'User-Agent': 'crypto-campaign-control'})
    # Standard GitHub asset redirects require an unauthenticated second request.
    try:
        response = store.api.opener.open(request, timeout=30)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise RuntimeError('manifest readback failed') from None
        url = exc.headers.get('Location', '')
        exc.close()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != 'https' or parsed.username or parsed.password or not (parsed.hostname or '').endswith('.githubusercontent.com'):
            raise ValueError('untrusted asset redirect')
        response = store.api.opener.open(urllib.request.Request(url), timeout=30)
    with response:
        value = read(response.read(MAX + 1))
    body = {k: v for k, v in value.items() if k != 'bundle_sha256'}
    if control.digest(body) != receipt['manifest_sha256'] or value['bundle_sha256'] != receipt['manifest_sha256']:
        raise ValueError('remote sealed inventory mismatch')
    if value.get('version') != 'historical-study-file-bundle-v2' or value.get('kind') != 'recovery' or value['metadata']['generation'] != tag:
        raise ValueError('new-campaign receipt requires exact recovery/result v2 inventory')
    closure = control.consolidation(value, control.identity(store.spec))
    control.validate(closure['accounting'], store.spec)
    task = closure['accounting']['handoff']['task']
    if receipt['task_complete'] and (receipt['verified_outputs'] != task['expected_outputs'] or receipt['scientific_state'] == 'FAILED'):
        raise ValueError('final output contract mismatch')
    typed = value['metadata']['task_receipt']
    if any(typed[k] != receipt[k] for k in ('task_id', 'handoff_key', 'task_complete', 'verified_outputs', 'scientific_state')) or value['metadata']['completed_work'] != receipt['completed_work']:
        raise ValueError('receipt output/committed-work binding mismatch')
    # Publication helper verifies every upload digest/readback. Cross-job control
    # requires GitHub's current digests as independent immutable asset confirmation.
    for part in value['assets']:
        remote = assets[part['asset']]
        if remote['state'] != 'uploaded' or remote['size'] != part['size'] or remote.get('digest') != 'sha256:' + part['sha256']:
            raise ValueError('remote asset digest unavailable/mismatch; halt rather than infer proof')
    return value


def publish_receipt(spec, receipt):
    store = Store(spec)
    verify_receipt(store, receipt)
    record, _ = store.update(lambda r: control.accept(r, spec, os.environ['STUDY_HANDOFF_KEY'], os.environ['GITHUB_RUN_ID'], receipt))
    store.view(record)



def measure_short_job(store, command):
    if command in ('claim', 'validate', 'bootstrap'):
        return
    actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
    name = 'handoff' if command == 'dispatch' else 'control'
    rows = actions.request('/actions/runs/' + os.environ['GITHUB_RUN_ID'] + '/attempts/' + os.environ['GITHUB_RUN_ATTEMPT'] + '/jobs?per_page=100')['jobs']
    starts = [r['started_at'] for r in rows if r['name'] == name]
    if len(starts) != 1:
        raise ValueError('short-job measurement requires owned actual start')
    minutes = max(0, (time.time() - datetime.fromisoformat(starts[0].replace('Z', '+00:00')).timestamp()) / 60)
    run_key = os.environ['GITHUB_RUN_ID'] + ':' + name + ':' + os.environ['GITHUB_RUN_ATTEMPT']
    def measured(r):
        if any(e['kind'] == 'MEASURED_CONTROL' and e['run_key'] == run_key for e in r['ledger']):
            return
        control.append(r, 'MEASURED_CONTROL', run_key=run_key, minutes=minutes,
                       sampling_boundary='before final status write and job exit')
        r['measured_minutes'] += minutes
    record, _ = store.update(measured)
    store.view(record)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bootstrap', 'validate', 'start', 'stop', 'resume', 'claim', 'dispatch', 'retention-plan', 'retention-apply'])
    args = parser.parse_args()
    if args.command == 'bootstrap':
        bootstrap()
        return
    spec = pinned_spec()
    if args.command == 'validate':
        return
    store = Store(spec)
    if args.command == 'claim':
        claim(store)
    elif args.command == 'dispatch':
        if int(os.environ['GITHUB_RUN_ATTEMPT']) > 1:
            store.update(lambda r: control.control_action(r, spec, 'handoff-rerun', os.environ['GITHUB_RUN_ID'] + ':handoff:' + os.environ['GITHUB_RUN_ATTEMPT']))
        record, _ = store.get()
        if record and record['handoff'] and record['handoff']['state'] == 'CLAIMED' and record['handoff']['run_id'] == os.environ.get('STUDY_PARENT_RUN_ID'):
            # A child that never published cannot be made successful by green YAML.
            failed_key = record['handoff']['key']
            def failed(r):
                if r['handoff']['key'] != failed_key or r['handoff']['state'] != 'CLAIMED' or r['handoff']['run_id'] != os.environ.get('STUDY_PARENT_RUN_ID'):
                    return
                if r['stop_requested']:
                    r['handoff']['state'] = 'INTERRUPTED'
                    r.update(state='STOPPED', reason='stopped-before-publication; reservation-retained')
                else:
                    r.update(state='PUBLICATION_FAILED', reason='slice-ended-without-verified-publication')
            record, _ = store.update(failed)
            store.view(record)
        dispatch(store)
    elif args.command in ('retention-plan', 'retention-apply'):
        record, _ = store.update(lambda r: control.control_action(r, spec, 'retention', os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']))
        deleted_tags = {e['generation'] for e in record['ledger'] if e['kind'] == 'RETENTION_CONFIRMED'}
        manifests = {r['generation']: verify_receipt(store, r) for r in record['receipts'] if r['generation'] not in deleted_tags}
        candidates = control.retention_candidates(record, manifests)
        deleted = 0
        if args.command == 'retention-apply':
            # Only this explicit manual control operation can delete new-campaign
            # redundant recovery. No input/final/draft/legacy deletion API exists.
            for candidate in candidates:
                current, _ = store.get()
                if current != record:
                    raise ValueError('control changed during retention review')
                # Revalidate all protected terminal result/evidence assets before
                # each deletion; status.json never participates in this proof.
                for retained in current['receipts']:
                    if retained['task_complete']:
                        verify_receipt(store, retained)
                verify_receipt(store, next(r for r in current['receipts'] if r['generation'] == candidate['generation']))
                release = store.api.request('/releases/tags/' + urllib.parse.quote(candidate['generation'], safe=''))
                def intent(r):
                    control.append(r, 'RETENTION_INTENT', **candidate)
                record, _ = store.update(intent)
                store.api.request('/releases/' + str(release['id']), 'DELETE')
                record, _ = store.update(lambda r: control.append(r, 'RETENTION_CONFIRMED', **candidate))
                deleted += 1
        print(control.canonical({'dry_run_only': args.command == 'retention-plan', 'candidates': candidates,
                                 'drafts': 'retain; no proven draft closure', 'deletions_performed': deleted}))
    else:
        # Resolve an interrupted claimed child before Resume, retaining reservation.
        if args.command == 'resume':
            record, _ = store.get()
            if record and record['handoff'] and record['handoff']['state'] in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS'):
                actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
                found = reconcile(store, actions, record['handoff'])
                if found and found['status'] == 'completed' and (found['conclusion'] in ('cancelled', 'timed_out') or record['stop_requested'] and record['handoff']['state'] != 'CLAIMED') and record['state'] not in control.TERMINAL:
                    key = record['handoff']['key']
                    def interrupted(r):
                        if r['handoff']['key'] == key:
                            r['handoff']['state'] = 'INTERRUPTED'
                            r['state'] = 'STOPPED'
                            r['no_progress_runs'] += 1
                            control.append(r, 'INTERRUPTED', key=key, run_id=str(found['id']))
                    store.update(interrupted)
                elif found and found['status'] == 'completed' and record['state'] not in control.TERMINAL:
                    def missing_publication(r):
                        if r['handoff']['key'] == record['handoff']['key'] and r['handoff']['state'] in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS'):
                            r.update(state='PUBLICATION_FAILED', reason='completed-child-without-verified-receipt; Resume-cannot-clear-failure')
                    store.update(missing_publication)
        record, _ = store.update(lambda r: control.control_action(r, spec, args.command, os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']), create=args.command == 'start')
        view = store.view(record)
        if args.command != 'stop':
            dispatch(store)
        summary = os.environ.get('GITHUB_STEP_SUMMARY')
        if summary:
            with open(summary, 'a') as handle:
                handle.write('Campaign ' + spec['campaign_id'] + ': **' + view['state'] + '**. Reserved ' + str(view['reserved_minutes']) + ' min; measured ' + str(round(view['measured_minutes'], 2)) + ' min.\n\nPrivate status: `campaigns/' + spec['campaign_id'] + '/status.json`. Reason: ' + str(view['reason']) + '.\n')

    measure_short_job(store, args.command)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Campaign control halted: ' + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
