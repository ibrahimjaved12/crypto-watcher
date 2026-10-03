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
import uuid
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
    def __init__(self, repository, token, *, private=False, allowance=240):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository or '') or not token:
            raise ValueError('scoped repository credential required')
        self.repository, self.token = repository, token
        self.prefix = 'https://api.github.com/repos/' + repository
        self.opener = urllib.request.build_opener(NoRedirect())
        self.end = time.monotonic() + allowance
        self.timeout = 30
        details = self.request('')
        if private and (details.get('private') is not True or repository.lower() == os.environ['GITHUB_REPOSITORY'].lower()):
            raise ValueError('separate private data repository required')
        self.branch = details['default_branch']

    def request(self, path, method='GET', body=None, missing=False):
        if time.monotonic() >= self.end:
            raise TransportError('DEADLINE', method)
        if (path and not path.startswith('/')) or '://' in path:
            raise ValueError('repository-relative API path required')
        raw = control.canonical(body).encode() if body is not None else None
        request = urllib.request.Request(self.prefix + path, data=raw, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
            'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'crypto-campaign-control'})
        try:
            with self.opener.open(request, timeout=max(1, min(self.timeout, self.end - time.monotonic()))) as response:
                payload = response.read(MAX + 1)
                try:
                    return read(payload) if payload else None
                except ValueError:
                    if method != 'GET':
                        raise TransportError('AMBIGUOUS_WRITE', method, response.status) from None
                    raise
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            if code == 404 and missing:
                return None
            if code in (409, 422):
                raise Conflict(code) from None
            raise TransportError('HTTP', method, code) from None
        except InterruptedError:
            raise
        except (urllib.error.URLError, OSError):
            raise TransportError('AMBIGUOUS_WRITE' if method != 'GET' else 'NETWORK', method) from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class TransportError(RuntimeError):
    def __init__(self, code, operation, status=None):
        self.code, self.operation, self.status = code, operation, status
        super().__init__(code)


class Conflict(Exception):
    def __init__(self, status=None):
        self.status = status


def invocation(key, operation):
    return control.digest({'key': key, 'operation': operation, 'run_id': os.environ['GITHUB_RUN_ID'],
                           'attempt': os.environ['GITHUB_RUN_ATTEMPT'], 'nonce': uuid.uuid4().hex})


DIAGNOSTIC_OPERATIONS = {'claim', 'dispatch', 'start', 'stop', 'resume', 'run', 'metadata', 'inputs', 'restore',
    'snapshot', 'publish', 'record-receipt', 'install-dependencies', 'validate-parents', 'finalize-study',
    'diagnostic', 'bootstrap', 'validate', 'validate-settings', 'retention-plan', 'retention-apply',
    'cache-restore', 'cache-publish', 'dependency-key', 'setup'}
DIAGNOSTIC_CATEGORIES = {'OWNER_STOP', 'SETUP_BUDGET', 'CANCELLED', 'DEADLINE', 'HTTP', 'NETWORK',
    'AMBIGUOUS_WRITE', 'CAS_LIMIT', 'INTEGRITY', 'PROCESS', 'CONTROL', 'CONFLICT', 'CAPTURE_UNAVAILABLE', 'EVIDENCE_UNAVAILABLE'}


def safe_diagnostic(details):
    status, code = details.get('http_status'), details.get('exit_code')
    return {'version': 'historical-campaign-diagnostic-v1',
            'operation': details.get('operation') if details.get('operation') in DIAGNOSTIC_OPERATIONS else 'setup',
            'category': details.get('category') if details.get('category') in DIAGNOSTIC_CATEGORIES else 'CONTROL',
            'http_status': status if type(status) is int and 100 <= status <= 599 else None,
            'exit_code': code if type(code) is int and -255 <= code <= 255 else None}


def safe_error(exc, operation):
    return safe_diagnostic({'version': 'historical-campaign-diagnostic-v1',
            'operation': operation if operation in DIAGNOSTIC_OPERATIONS else 'setup',
            'category': exc.code if isinstance(exc, TransportError) else 'CONFLICT' if isinstance(exc, Conflict) else 'CANCELLED' if isinstance(exc, (InterruptedError, KeyboardInterrupt)) else 'DEADLINE' if isinstance(exc, TimeoutError) else 'INTEGRITY' if isinstance(exc, (ValueError, KeyError)) else 'PROCESS' if isinstance(exc, subprocess.CalledProcessError) else 'CONTROL',
            'http_status': getattr(exc, 'status', None),
            'exit_code': exc.returncode if isinstance(exc, subprocess.CalledProcessError) else None})


def diagnostic(spec, details):
    """Only allowlisted typed data; never exception messages or worker output."""
    locator = None
    binding = {k: details[k] for k in ('handoff_key', 'claim_mutation_id')
               if re.fullmatch('[0-9a-f]{64}', str(details.get(k, '')))}
    details = safe_diagnostic(details)
    try:
        run = control.identifier(os.environ['GITHUB_RUN_ID'])
        attempt = control.identifier(os.environ['GITHUB_RUN_ATTEMPT'])
        operation = details['operation']
        control.identifier(operation)
        suffix = '-' + binding['claim_mutation_id'] if operation in ('claim', 'validate-parents') and 'claim_mutation_id' in binding else ''
        locator = 'campaigns/' + control.identifier(spec['campaign_id']) + '/diagnostics/' + run + '-' + attempt + '-' + operation + suffix + '.json'
        api = API(os.environ.get('RESEARCH_DATA_REPOSITORY'), os.environ.get('RESEARCH_DATA_TOKEN'), private=True, allowance=6)
        api.timeout = 2
        path = '/contents/' + locator
        old = api.request(path + '?ref=' + urllib.parse.quote(api.branch, safe=''), missing=True)
        body = {'message': 'Bounded private campaign diagnostic', 'branch': api.branch,
                'content': base64.b64encode((control.canonical({**details, **binding, 'run_id': run, 'attempt': attempt}) + '\n').encode()).decode()}
        if old:
            body['sha'] = old['sha']
        api.request(path, 'PUT', body)
    except (InterruptedError, KeyboardInterrupt):
        raise
    except Exception:
        # Signals and KeyboardInterrupt inherit BaseException and propagate.
        print('Campaign warning: DIAGNOSTIC_UNAVAILABLE', file=sys.stderr)
    return locator



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
        self.control_commit, self.control_blob_sha = history[0]['sha'], row['sha']
        return current, row['sha']

    def update(self, mutate, *, create=False, mutation_id=None):
        previous, candidate, result, last_write_error = None, None, None, None
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
            if record['version'] != control.VERSION:
                raise ValueError('legacy authority requires its original pinned writer; no migration')
            if mutation_id:
                committed = next((e for e in record['ledger'] if e.get('mutation_id') == mutation_id), None)
                if committed is not None:
                    return record, committed['result']
            previous = deepcopy(record)
            result = mutate(record)
            candidate = record
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
            except Conflict as exc:
                last_write_error = exc
                continue
            except TransportError as exc:
                if exc.code == 'HTTP' and exc.status is not None and exc.status < 500:
                    raise
                last_write_error = exc
                # An ambiguous Contents write is reconciled by rereading. A claim
                # caller can prove its own exact run identity before entering.
                continue
        # Even the last ambiguous PUT receives one bounded reconciliation read.
        current, _ = self.get()
        if current is not None:
            control.validate(current, self.spec, previous)
            if mutation_id:
                committed = next((e for e in current['ledger'] if e.get('mutation_id') == mutation_id), None)
                if committed:
                    return current, committed['result']
            if candidate is not None and current == candidate:
                return current, result
        raise TransportError('CAS_LIMIT', 'PUT', getattr(last_write_error, 'status', None))

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
                  'active_run': 'https://github.com/' + os.environ.get('GITHUB_REPOSITORY', '') + '/actions/runs/' + h['run_id'] if h and h['run_id'] and os.environ.get('GITHUB_REPOSITORY') else None}
        # Derive a useful view first; all optional I/O and rendering is isolated.
        view_api = object.__new__(API)
        view_api.__dict__ = self.api.__dict__.copy()
        view_api.end, view_api.timeout = time.monotonic() + 6, 2
        try:
            self.write_view(view_api, status, record, activity)
        except (InterruptedError, KeyboardInterrupt):
            raise
        except (OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, Conflict):
            status['activity'] = activity
            print('Campaign warning: STATUS_VIEW_UNAVAILABLE', file=sys.stderr)
        return status

    def write_view(self, view_api, status, record, activity):
        path = self.path.replace('control.json', 'status.json')
        old = view_api.request(path + '?ref=' + urllib.parse.quote(self.api.branch, safe=''), missing=True)
        if activity is None and old and old.get('encoding') == 'base64':
            old_status = read(base64.b64decode(old['content'], validate=True))
            activity = old_status.get('activity')
            if old_status.get('identity') != record['identity'] or len(control.canonical(activity)) > 24000:
                raise ValueError('invalid optional activity metadata')
            status['activity'] = activity
        body = {'message': 'Campaign status view', 'branch': self.api.branch,
                'content': base64.b64encode((control.canonical(status) + '\n').encode()).decode()}
        if old:
            body['sha'] = old['sha']
        try:
            view_api.request(path, 'PUT', body)
        except (Conflict, RuntimeError):
            print('Campaign warning: STATUS_VIEW_UNAVAILABLE', file=sys.stderr)
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
        old_view = view_api.request(view_path + '?ref=' + urllib.parse.quote(self.api.branch, safe=''), missing=True)
        view_body = {'message': 'Readable campaign status view', 'branch': self.api.branch, 'content': base64.b64encode(markdown.encode()).decode()}
        if old_view:
            view_body['sha'] = old_view['sha']
        try:
            view_api.request(view_path, 'PUT', view_body)
        except (Conflict, RuntimeError):
            print('Campaign warning: STATUS_VIEW_UNAVAILABLE', file=sys.stderr)


def run_name(h, campaign_id):
    t = h['task']
    return 'Campaign ' + campaign_id + ' / ' + t['id'] + ' / slice ' + str(h['sequence']) + ' / ' + t['phase'] + ' ' + str(t['period_index'] if t['period_index'] is not None else 0) + ' / ' + h['key']


def reconcile(store, actions, h):
    matches = []
    for page in range(1, 4):
        rows = actions.request('/actions/workflows/historical-study.yml/runs?event=workflow_dispatch&per_page=100&page=' + str(page))['workflow_runs']
        matches.extend(r for r in rows if r.get('display_title') == run_name(h, store.spec['campaign_id']))
        if len(rows) < 100:
            break
    if len(matches) > 1:
        raise ValueError('duplicate dispatches found; children remain claim guarded')
    return matches[0] if matches else None


def observe_dispatch(store, h, found):
    """Bind the first dispatched attempt without changing Stop or ownership."""
    def confirmed(r):
        current = r['handoff']
        if (not current or current['key'] != h['key'] or current['state'] not in ('INTENT', 'DISPATCHED', 'AMBIGUOUS')
                or r['state'] in control.TERMINAL or current.get('run_id') not in (None, str(found['id']))):
            return
        current.update(state='DISPATCHED', run_id=str(found['id']), attempt='1')
        if not any(e['kind'] == 'DISPATCH_CONFIRMED' and e['key'] == h['key'] and e['run_id'] == str(found['id']) for e in r['ledger']):
            control.append(r, 'DISPATCH_CONFIRMED', key=h['key'], run_id=str(found['id']))
    record, _ = store.update(confirmed)
    return record


def dispatch(store):
    record, _ = store.get()
    if record is None or record['stop_requested'] or record['state'] != 'HANDOFF_PENDING':
        return
    h = record['handoff']
    if not h or h['state'] not in ('INTENT', 'AMBIGUOUS', 'DISPATCHED'):
        return
    if record['receipts']:
        try:
            latest_receipt = record['receipts'][-1]
            verify_receipt(store, latest_receipt)
        except (TransportError, Conflict):
            # Missing/inaccessible evidence is not proof of corrupt evidence.
            def unavailable(r):
                current = r['handoff']
                if current and current['key'] == h['key'] and current['state'] == h['state'] and r['state'] == 'HANDOFF_PENDING' and not r['stop_requested']:
                    r.update(state='STOPPED', reason='proof-verification-unavailable; explicit-Resume-required')
            store.update(unavailable)
            raise
        except (KeyError, ValueError):
            def invalid(r):
                current = r['handoff']
                if current and current['key'] == h['key'] and current['state'] == h['state'] and r['state'] == 'HANDOFF_PENDING' and not r['stop_requested']:
                    r.update(state='INTEGRITY_FAILED', reason='handoff-publication-or-finalized-closure-verification-failed')
            store.update(invalid)
            raise
    record, permitted = store.update(lambda r: control.dispatch_permitted(r, store.spec, h['key']))
    if not permitted:
        store.view(record)
        return
    actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
    found = reconcile(store, actions, h)
    if found:
        observe_dispatch(store, h, found)
        return
    if h['state'] != 'INTENT':
        def ambiguous(r):
            current = r['handoff']
            if current and current['key'] == h['key'] and current['state'] in ('DISPATCHED', 'AMBIGUOUS') and r['state'] == 'HANDOFF_PENDING':
                r.update(reason='dispatch-ambiguous; bounded reconciliation found no run; owner intervention required')
        store.update(ambiguous)
        return
    mutation_id = invocation(h['key'], 'dispatch-attempt')
    # Write attempt BEFORE POST; no blind POST retry after an uncertain response.
    def attempted(r):
        return control.dispatch_attempt(r, store.spec, h['key'], os.environ['GITHUB_RUN_ID'],
                                        os.environ['GITHUB_RUN_ATTEMPT'], mutation_id)
    _, allowed = store.update(attempted, mutation_id=mutation_id)
    if not allowed:
        return
    latest, permitted = store.update(lambda r: control.dispatch_permitted(r, store.spec, h['key']))
    if not permitted:
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
        if r['state'] != 'HANDOFF_PENDING' or r['stop_requested'] or r['handoff']['key'] != h['key'] or r['handoff']['state'] not in ('INTENT', 'AMBIGUOUS', 'DISPATCHED') or any(e['kind'] == 'DISPATCH_OUTCOME' and e['key'] == h['key'] for e in r['ledger']):
            return
        r['handoff'].update(state='DISPATCHED' if found else 'AMBIGUOUS', run_id=str(found['id']) if found else None, attempt='1' if found else None)
        control.append(r, 'DISPATCH_OUTCOME', key=h['key'], run_id=str(found['id']) if found else None,
                       outcome='confirmed' if found else 'ambiguous')
        r['reason'] = None if found else 'dispatch-ambiguous; use Resume to reconcile; no blind redispatch'
    record, _ = store.update(finish)
    store.view(record)


def claim(store):
    key, run_id = os.environ.get('STUDY_HANDOFF_KEY'), os.environ['GITHUB_RUN_ID']
    attempt = os.environ['GITHUB_RUN_ATTEMPT']
    record, _ = store.get()
    if record is None:
        raise ValueError('campaign has not been started')
    # Validate exact dispatch arguments before *any* authoritative mutation.
    control.entry_bindings(record, store.spec, key, os.environ)
    mutation_id = invocation(key, 'claim')
    # This route survives independently of the immutable scientific proof.
    routing = {'handoff_key': key, 'claim_mutation_id': mutation_id}
    Path('/tmp/crypto-study/transfers/entry-routing.json').write_text(control.canonical(routing) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('claim_mutation_id=' + mutation_id + '\n')
    record, entered = store.update(lambda r: control.claim(r, store.spec, key, run_id, attempt,
                                                          mutation_id, os.environ), mutation_id=mutation_id)
    if not entered:
        with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
            output.write('managed=true\nclaimed=false\n')
        raise ValueError('duplicate/stale/stopped child refused before downloads')
    # Bounded rereads use this same claim identity, never a fresh reservation.
    # get() anchors the returned record and blob to the same immutable commit.
    for capture_attempt in range(4):
        try:
            record, _ = store.get()
            break
        except (Conflict, TransportError):
            if capture_attempt == 3:
                raise
    h = record['handoff'] if record else None
    if not h or h['key'] != key or h['state'] != 'CLAIMED' or h.get('mutation_id') != mutation_id or h['run_id'] != run_id or h.get('attempt') != attempt:
        raise ValueError('claim changed before local proof capture')
    if not any(e['kind'] == 'CLAIM' and e.get('mutation_id') == mutation_id and e['key'] == key and e['run_id'] == run_id and e.get('attempt') == attempt for e in record['ledger']):
        raise ValueError('owned claim accounting missing before local proof capture')
    Path('/tmp/crypto-study/transfers/control-claim-reference.json').write_text(control.canonical({'control_commit': store.control_commit, 'control_blob_sha': store.control_blob_sha}) + '\n')
    local = Path('/tmp/crypto-study/transfers/control-claim.json')
    local.write_text(control.canonical(record) + '\n')
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('claimed=true\nmanaged=true\n')
    store.view(record)


def remote_manifest(store, asset):
    if time.monotonic() >= store.api.end:
        raise TransportError('DEADLINE', 'GET')
    request = urllib.request.Request(store.api.prefix + '/releases/assets/' + str(asset['id']), headers={
        'Authorization': 'Bearer ' + store.api.token, 'Accept': 'application/octet-stream', 'User-Agent': 'crypto-campaign-control'})
    # Standard GitHub asset redirects require an unauthenticated second request.
    try:
        try:
            response = store.api.opener.open(request, timeout=max(1, min(30, store.api.end - time.monotonic())))
        except urllib.error.HTTPError as exc:
            if exc.code not in (301, 302, 303, 307, 308):
                status = exc.code
                exc.close()
                raise TransportError('HTTP', 'GET', status) from None
            url = exc.headers.get('Location', '')
            exc.close()
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != 'https' or parsed.username or parsed.password or not (parsed.hostname or '').endswith('.githubusercontent.com'):
                raise ValueError('untrusted asset redirect')
            if time.monotonic() >= store.api.end:
                raise TransportError('DEADLINE', 'GET')
            response = store.api.opener.open(urllib.request.Request(url), timeout=max(1, min(30, store.api.end - time.monotonic())))
        with response:
            value = read(response.read(MAX + 1))
    except InterruptedError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise TransportError('HTTP', 'GET', status) from None
    except (urllib.error.URLError, OSError):
        raise TransportError('NETWORK', 'GET') from None
    return value


def release_assets(store, release_id):
    assets = {}
    for page in range(1, 12):
        rows = store.api.request('/releases/' + str(release_id) + '/assets?per_page=100&page=' + str(page))
        for row in rows:
            if row['name'] in assets:
                raise ValueError('duplicate remote asset')
            assets[row['name']] = row
        if len(rows) < 100:
            break
    else:
        raise ValueError('bounded asset inventory exceeded')
    return assets


def receipt_manifest(store, receipt):
    """Independently reread immutable publication before accepting its transition."""
    tag = control.identifier(receipt['generation'])
    release = store.api.request('/releases/tags/' + urllib.parse.quote(tag, safe=''))
    if release['draft']:
        raise ValueError('unpublished receipt')
    assets = release_assets(store, release['id'])
    value = remote_manifest(store, assets['manifest.json'])
    body = {k: v for k, v in value.items() if k != 'bundle_sha256'}
    if control.digest(body) != receipt['manifest_sha256'] or value['bundle_sha256'] != receipt['manifest_sha256']:
        raise ValueError('remote sealed inventory mismatch')
    if value.get('version') != 'historical-study-file-bundle-v2' or value.get('kind') != 'recovery' or value['metadata']['generation'] != tag:
        raise ValueError('new-campaign receipt requires exact recovery/result v2 inventory')
    # Publication helper verifies every upload digest/readback. Cross-job control
    # requires GitHub's current digests as independent immutable asset confirmation.
    for part in value['assets']:
        remote = assets[part['asset']]
        if remote['state'] != 'uploaded' or remote['size'] != part['size']:
            raise ValueError('remote asset state/size mismatch')
        if not remote.get('digest'):
            raise TransportError('EVIDENCE_UNAVAILABLE', 'GET')
        if remote['digest'] != 'sha256:' + part['sha256']:
            raise ValueError('remote asset digest mismatch')
    return value


def sealed_accounting(store, closure):
    proof = closure['accounting']
    if closure['version'] != control.COMPACT_LINEAGE:
        return control.validate(proof, store.spec)
    key = (proof['control_commit'], proof['control_blob_sha'])
    if not hasattr(store, '_accounting_cache'):
        store._accounting_cache = {}
    if key not in store._accounting_cache:
        if len(store._accounting_cache) >= store.spec['budget']['max_runs']:
            raise ValueError('bounded accounting proof closure exceeded')
        row = store.api.request(store.path + '?ref=' + proof['control_commit'])
        if row['sha'] != proof['control_blob_sha'] or row['encoding'] != 'base64':
            raise ValueError('immutable accounting blob reference mismatch')
        record = control.validate(read(base64.b64decode(row['content'], validate=True)), store.spec)
        store._accounting_cache[key] = record
    record = store._accounting_cache[key]
    expected = control.accounting_reference(record, {k: proof[k] for k in ('control_commit', 'control_blob_sha')})
    if expected != proof or closure['parent_receipt'] != record['handoff']['parent'] or closure['finalized_receipts'] != [r for r in record['receipts'] if r['task_complete']]:
        raise ValueError('sealed accounting summary/finalized references mismatch')
    return record


def verify_receipt(store, receipt, cache=None, visiting=None):
    """Validate sealed references once, including monotonic work/accounting closure."""
    cache = {} if cache is None else cache
    visiting = set() if visiting is None else visiting
    key = (receipt['generation'], receipt['manifest_sha256'])
    if key in cache:
        value = cache[key]
    else:
        if key in visiting or len(visiting) >= store.spec['budget']['max_runs'] or len(cache) > store.spec['budget']['max_runs']:
            raise ValueError('cyclic or unbounded sealed proof closure')
        visiting.add(key)
        value = receipt_manifest(store, receipt)
        closure = control.consolidation(value, control.identity(store.spec))
        accounting = sealed_accounting(store, closure)
        if closure['version'] == control.COMPACT_LINEAGE:
            parent = closure['parent_receipt']
            refs = closure['finalized_receipts'] + ([parent] if parent else [])
            for ref in refs:
                previous = verify_receipt(store, ref, cache, visiting)
                prior = sealed_accounting(store, previous['metadata']['consolidation'])
                if accounting['ledger'][:len(prior['ledger'])] != prior['ledger'] or accounting['receipts'][:len(prior['receipts'])] != prior['receipts']:
                    raise ValueError('compact proof removed committed accounting/references')
                if ref is parent and not set(previous['metadata']['local_completed_work']).issubset(value['metadata']['local_completed_work']):
                    raise ValueError('compact sealed parent work was discarded')
            if receipt != control.receipt_reference(value, receipt['measured_minutes']):
                raise ValueError('compact receipt counters/output/proof binding mismatch')
        else:
            typed = value['metadata']['task_receipt']
            if any(typed[k] != receipt[k] for k in ('task_id', 'handoff_key', 'task_complete', 'verified_outputs', 'scientific_state')) or value['metadata']['completed_work'] != receipt['completed_work']:
                raise ValueError('legacy output/work binding mismatch')
        task = accounting['handoff']['task']
        if receipt['task_complete'] and (receipt['verified_outputs'] != task['expected_outputs'] or receipt['scientific_state'] == 'FAILED'):
            raise ValueError('final output contract mismatch')
        visiting.remove(key)
        cache[key] = value
    if value['metadata']['consolidation']['version'] == control.COMPACT_LINEAGE and receipt != control.receipt_reference(value, receipt['measured_minutes']):
        raise ValueError('reused reference differs from sealed proof')
    return value



def publish_receipt(spec, receipt):
    store = Store(spec)
    proof = verify_receipt(store, receipt)
    accounting = sealed_accounting(store, proof['metadata']['consolidation'])
    record, _ = store.update(lambda r: control.accept(r, spec, os.environ['STUDY_HANDOFF_KEY'], os.environ['GITHUB_RUN_ID'], receipt, proof, accounting=accounting))
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


STUDY_STEPS = {'science': 'Run credential-free science with private supervisor status transport',
               'parents': 'Validate frozen phase parents before raw/test input access',
               'snapshot': 'Validate and snapshot committed recovery with ownership held',
               'publication': 'Publish private parts then sealed manifest and verify publication',
               'entry': 'Claim new-campaign handoff before dependencies or input acquisition'}


def study_job(actions, run_id, attempt):
    rows = actions.request('/actions/runs/' + run_id + '/attempts/' + attempt + '/jobs?per_page=100')['jobs']
    matches = [j for j in rows if j['name'] == 'study' and str(j['run_id']) == run_id and str(j.get('run_attempt', attempt)) == attempt]
    if len(matches) != 1 or matches[0]['status'] != 'completed':
        return None
    return {**matches[0], 'run_attempt': attempt}


def capture_reason(category):
    return ('CAPTURE_UNAVAILABLE' if category in {'NETWORK', 'DEADLINE', 'HTTP', 'AMBIGUOUS_WRITE',
            'CAS_LIMIT', 'CONFLICT', 'CAPTURE_UNAVAILABLE', 'EVIDENCE_UNAVAILABLE'} else category)


def finalizer(store):
    """Pinned stdlib only. Step outcomes are trusted YAML, diagnostics allowlisted."""
    record, _ = store.get()
    h = record['handoff'] if record else None
    if not h or h['key'] != os.environ.get('STUDY_HANDOFF_KEY') or h['state'] != 'CLAIMED' or h['run_id'] != os.environ['GITHUB_RUN_ID'] or h.get('attempt') != os.environ['GITHUB_RUN_ATTEMPT'] or h.get('mutation_id') != os.environ.get('STUDY_CLAIM_MUTATION_ID'):
        return
    allowed = {'success', 'failure', 'cancelled', 'skipped', 'unknown'}
    outcome = {k: os.environ.get('STUDY_' + k.upper() + '_OUTCOME', 'unknown') for k in ('entry', 'setup', 'parents', 'scientific', 'snapshot', 'publication')}
    if any(v not in allowed for v in outcome.values()):
        raise ValueError('untrusted outcome contract')
    path = Path('/tmp/crypto-study/transfers/operation-outcomes.json')
    details = read(path.read_bytes()) if path.exists() else []
    entry_path = Path('/tmp/crypto-study/transfers/entry-outcome.json')
    if entry_path.exists():
        entry = read(entry_path.read_bytes())
        if entry.get('handoff_key') == h['key'] and entry.get('claim_mutation_id') == h['mutation_id']:
            details.append(entry)
    # No arbitrary private exception strings enter control or public summaries.
    safe = [safe_diagnostic(d) for d in details if isinstance(d, dict)]
    required = [d for d in safe if d['operation'] not in ('cache-restore', 'cache-publish', 'dependency-key')]
    if outcome['entry'] == 'failure' and outcome['scientific'] == 'skipped':
        required = [{**d, 'category': capture_reason(d['category'])} if d['operation'] == 'claim' else d for d in required]
    entry_category = os.environ.get('STUDY_ENTRY_CATEGORY')
    if outcome['entry'] == 'failure' and outcome['scientific'] == 'skipped' and entry_category in DIAGNOSTIC_CATEGORIES:
        required.append(safe_diagnostic({'operation': 'claim', 'category': capture_reason(entry_category)}))
    failure = next((d for d in required if d['category'] not in {'OWNER_STOP', 'SETUP_BUDGET', 'CANCELLED', 'CAPTURE_UNAVAILABLE'}), None)
    if failure is None and not required and outcome['setup'] == 'failure':
        failure = safe_diagnostic({'operation': 'install-dependencies', 'category': 'PROCESS'})
    outcome['reason'] = failure['category'] if failure else next((d['category'] for d in reversed(required)), 'job-outcome-pending')
    outcome['failure_operation'] = failure['operation'] if failure else None
    key = h['key']
    def save(r):
        current = r['handoff']
        if not current or current['key'] != key or current['state'] != 'CLAIMED' or current['run_id'] != h['run_id'] or current.get('attempt') != h['attempt'] or current.get('mutation_id') != h.get('mutation_id'):
            return
        if current.get('outcome') == outcome:
            return
        control.append(r, 'STUDY_OUTCOME', key=key, run_id=h['run_id'], attempt=h['attempt'], outcome_sha256=control.digest(outcome))
        current['outcome'] = outcome
        if failure and failure['operation'] != 'record-receipt' and r['state'] not in control.TERMINAL:
            state = control.failure_state(failure['operation'], failure['category'])
            r.update(state=state, reason='typed-established-study-failure')
    record, _ = store.update(save)
    if failure or required:
        diagnostic(store.spec, failure or required[-1])
    store.view(record)


def reconcile_study(store, actions, record, job):
    """Only a completed owned STUDY job proves timeout/cancellation, never workflow status."""
    h = record['handoff']
    if not h or h['state'] not in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS', 'INTENT'):
        return
    key = h['key']
    if job is not None and (str(job['run_id']) != h.get('run_id') or str(job.get('run_attempt')) != h.get('attempt')):
        return
    if h['state'] == 'CLAIMED':
        tag = 'recovery-' + store.spec['campaign_id'][:20] + '-' + h['run_id'] + '-' + h['attempt']
        release = store.api.request('/releases/tags/' + control.identifier(tag), missing=True)
        if release and not release['draft']:
            # Acceptance may have been cancelled after immutable publication.
            value = receipt_manifest(store, {'generation': tag, 'manifest_sha256': release_manifest_sha(store, release)})
            receipt = control.receipt_reference(value, value['metadata']['measured_minutes'])
            proof = verify_receipt(store, receipt)
            accounting = sealed_accounting(store, proof['metadata']['consolidation'])
            if job is None and record['state'] not in control.TERMINAL:
                def unresolved(r):
                    current = r['handoff']
                    if current and current['key'] == key and current['state'] == 'CLAIMED' and current['run_id'] == h['run_id'] and current.get('attempt') == h['attempt'] and r['state'] not in control.TERMINAL:
                        r['handoff']['verified_publication'] = {k: receipt[k] for k in ('generation', 'manifest_sha256')}
                        r.update(state='OUTCOME_UNRESOLVED', reason='owned-study-job-outcome-unavailable; verified-publication-retained')
                current, _ = store.update(unresolved)
                store.view(current)
                return
            store.update(lambda r: control.accept(r, store.spec, key, h['run_id'], receipt, proof, accounting=accounting, interruption='external-cancellation' if job and job['conclusion'] in ('cancelled', 'timed_out') else None))
            return
    if job is None or record['state'] in control.TERMINAL:
        return
    remote_steps = {s['name']: s.get('conclusion') for s in job.get('steps', [])}
    steps = {key: remote_steps.get(name) for key, name in STUDY_STEPS.items()}
    entry_details = None
    validation_details = None
    diagnostic_operation = ('claim' if steps['entry'] == 'failure' and steps['science'] == 'skipped'
                            else 'validate-parents' if steps['parents'] in ('failure', 'cancelled') else None)
    if h['state'] == 'CLAIMED' and diagnostic_operation and (not h.get('outcome') or h['outcome'].get('reason') == 'job-outcome-pending'):
        # An independent stdlib diagnostic can survive failed local capture or
        # finalization. Missing diagnostic evidence must remain unresolved.
        path = '/contents/campaigns/' + control.identifier(store.spec['campaign_id']) + '/diagnostics/' + h['run_id'] + '-' + h['attempt'] + '-' + diagnostic_operation + '-' + h['mutation_id'] + '.json'
        row = store.api.request(path, missing=True)
        if row:
            details = read(base64.b64decode(row['content'], validate=True))
            if details.get('run_id') == h['run_id'] and details.get('attempt') == h['attempt'] and details.get('operation') == diagnostic_operation and details.get('handoff_key') == key and details.get('claim_mutation_id') == h.get('mutation_id'):
                if diagnostic_operation == 'claim':
                    entry_details = safe_diagnostic(details)
                else:
                    validation_details = safe_diagnostic(details)
    def completed(r):
        current = r['handoff']
        if not current or current['key'] != key or current['state'] not in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS', 'INTENT') or current['run_id'] != str(job['run_id']) or current.get('attempt') != str(job['run_attempt']):
            return
        details = entry_details or validation_details
        if details and (not current.get('outcome') or current['outcome'].get('reason') == 'job-outcome-pending'):
            reason = capture_reason(details['category']) if entry_details else details['category']
            outcome = {'reason': reason, 'failure_operation': None if reason in ('CAPTURE_UNAVAILABLE', 'CANCELLED', 'SETUP_BUDGET', 'OWNER_STOP') else details['operation'],
                       'setup': 'not-started', 'scientific': 'not-started', 'publication': 'not-verified'}
            control.append(r, 'STUDY_OUTCOME', key=key, run_id=current['run_id'], attempt=current['attempt'], outcome_sha256=control.digest(outcome))
            current['outcome'] = outcome
        control.study_outcome(r, store.spec, key, job['conclusion'], steps,
                              run_id=str(job['run_id']), attempt=str(job['run_attempt']))
    record, _ = store.update(completed)
    store.view(record)


def release_manifest_sha(store, release):
    # Exact seal is independently read, not inferred from a mutable Release body.
    assets = release_assets(store, release['id'])
    manifests = [a for a in assets.values() if a['name'] == 'manifest.json']
    if len(manifests) != 1:
        raise ValueError('publication has no unique sealed manifest')
    value = remote_manifest(store, manifests[0])
    expected = value['bundle_sha256']
    if control.digest({k: v for k, v in value.items() if k != 'bundle_sha256'}) != expected:
        raise ValueError('discovered receipt seal mismatch')
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['bootstrap', 'validate', 'start', 'stop', 'resume', 'claim', 'dispatch', 'retention-plan', 'retention-apply', 'finalize-study', 'diagnostic'])
    args = parser.parse_args()
    if args.command == 'bootstrap':
        bootstrap()
        return
    spec = pinned_spec()
    if args.command == 'validate':
        return
    if args.command == 'diagnostic':
        diagnostic(spec, {'version': 'historical-campaign-diagnostic-v1', 'operation': 'diagnostic', 'category': 'CONTROL', 'http_status': None, 'exit_code': 1})
        return
    store = Store(spec)
    if args.command == 'finalize-study':
        store.api.end = min(store.api.end, time.monotonic() + 30)
        store.api.timeout = 3
        finalizer(store)
        return
    if args.command == 'claim':
        claim(store)
    elif args.command == 'dispatch':
        if int(os.environ['GITHUB_RUN_ATTEMPT']) > 1:
            store.update(lambda r: control.control_action(r, spec, 'handoff-rerun', os.environ['GITHUB_RUN_ID'] + ':handoff:' + os.environ['GITHUB_RUN_ATTEMPT']))
        record, _ = store.get()
        if record and os.environ.get('STUDY_PARENT_RUN_ID'):
            actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
            job = study_job(actions, os.environ['STUDY_PARENT_RUN_ID'], os.environ['GITHUB_RUN_ATTEMPT'])
            h = record['handoff']
            if h and h['run_id'] == os.environ['STUDY_PARENT_RUN_ID'] and h.get('attempt') == os.environ['GITHUB_RUN_ATTEMPT']:
                reconcile_study(store, actions, record, job)
            elif h and any(r.get('run_id') == os.environ['STUDY_PARENT_RUN_ID'] and r.get('attempt') == os.environ['GITHUB_RUN_ATTEMPT'] for r in record['receipts']) and (job is None or job['conclusion'] in ('cancelled', 'timed_out')):
                def cancelled_after_acceptance(r):
                    if r['state'] not in control.TERMINAL and r['handoff'] and r['handoff']['key'] == h['key']:
                        r.update(state='STOPPED' if job else 'OUTCOME_UNRESOLVED', reason='external-cancellation-after-verified-publication; explicit-Resume-required' if job else 'owned-study-job-outcome-unavailable-after-acceptance')
                store.update(cancelled_after_acceptance)
        dispatch(store)
    elif args.command in ('retention-plan', 'retention-apply'):
        record, _ = store.update(lambda r: control.control_action(r, spec, 'retention', os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']))
        deleted_tags = {e['generation'] for e in record['ledger'] if e['kind'] == 'RETENTION_CONFIRMED'}
        proof_cache = {}
        manifests = {r['generation']: verify_receipt(store, r, proof_cache) for r in record['receipts'] if r['generation'] not in deleted_tags}
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
        # Resume reconciles the exact owned study job and publication first.
        if args.command == 'resume':
            store.update(lambda r: control.reserve_control(r, spec, 'resume', os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']))
            record, _ = store.get()
            if record and record['state'] == 'OUTCOME_UNRESOLVED' and record['handoff'] and record['handoff']['state'] in ('INTENT', 'DISPATCHED', 'AMBIGUOUS') and record['receipts']:
                prior = record['receipts'][-1]
                actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
                job = study_job(actions, prior['run_id'], prior['attempt'])
                if job and job['conclusion'] in ('success', 'cancelled', 'timed_out'):
                    pending_key = record['handoff']['key']
                    store.update(lambda r: r.update(state='HANDOFF_PENDING' if job['conclusion'] == 'success' else 'STOPPED', reason=None) if r['handoff']['key'] == pending_key and r['state'] == 'OUTCOME_UNRESOLVED' else None)
                    record, _ = store.get()
            if record and record['handoff'] and record['handoff']['state'] in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS', 'INTENT'):
                actions = API(os.environ['GITHUB_REPOSITORY'], os.environ.get('GH_TOKEN'))
                owned = record['handoff']
                if owned['state'] == 'CLAIMED':
                    job = study_job(actions, owned['run_id'], owned['attempt'])
                    reconcile_study(store, actions, record, job)
                else:
                    found = reconcile(store, actions, owned)
                    if found:
                        record = observe_dispatch(store, owned, found)
                        # A rerun is not evidence about the original dispatch.
                        job = study_job(actions, str(found['id']), '1')
                        reconcile_study(store, actions, record, job)
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
    except (Exception, KeyboardInterrupt) as exc:
        details = safe_error(exc, sys.argv[1] if len(sys.argv) > 1 else 'setup')
        if len(sys.argv) > 1 and sys.argv[1] == 'claim':
            if isinstance(exc, OSError) and not isinstance(exc, InterruptedError):
                details['category'] = 'CAPTURE_UNAVAILABLE'
            # No scientific imports or control-claim.json are needed to record
            # capture failure, even when the claim PUT committed ambiguously.
            try:
                route_path = Path('/tmp/crypto-study/transfers/entry-routing.json')
                routing = read(route_path.read_bytes()) if route_path.exists() else {}
                details.update({k: routing[k] for k in ('handoff_key', 'claim_mutation_id') if k in routing})
                Path('/tmp/crypto-study/transfers/entry-outcome.json').write_text(control.canonical(details) + '\n')
            except (OSError, ValueError, KeyError):
                pass
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('entry_category=' + details['category'] + '\n')
        locator = None
        try:
            locator = diagnostic(pinned_spec(), details)
        except Exception:
            pass # Untrusted pins never execute a finalizer or write diagnostics.
        print('Campaign control halted: ' + control.canonical({**details, 'diagnostic_locator': locator}), file=sys.stderr)
        sys.exit(1)
