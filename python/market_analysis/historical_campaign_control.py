"""Versioned, stdlib-only campaign decisions. No network, shell or scientific imports.

Operational records are not scientific proof. Callers supply independently verified
sealed receipts; immutable ledger entries and reservations share one CAS record.
"""
import hashlib
import json
import math
import re

LEGACY_VERSION = 'historical-campaign-control-v1'
VERSION = 'historical-campaign-control-v2'
RECEIPT = 'historical-campaign-task-receipt-v2'
COMPACT_LINEAGE = 'historical-campaign-consolidation-v2'
LINEAGE = 'historical-campaign-consolidation-v1'
PLAN = 'historical-campaign-plan-v1'
OPERATIONS = {'preflight': ['preflight'], 'execute-period': ['period-report', 'period-evidence'],
              'aggregate': ['execution-index']}
TERMINAL = {'COMPLETED', 'SCIENTIFIC_FAILED', 'PUBLICATION_FAILED', 'INTEGRITY_FAILED', 'SETUP_FAILED'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('invalid control identifier')
    return value


def identity(spec):
    return {'campaign_id': spec['campaign_id'], 'campaign_spec_sha256': digest(spec),
            **{key: spec[key] for key in ('runtime_sha', 'orchestration_sha', 'producer_revision',
               'study_manifest_sha256', 'coverage_manifest_sha256', 'dependency_locks',
               'expected_membership', 'inputs')}, 'layout_version': 'crypto-study-absolute-layout-v1'}


def plan(spec):
    if spec.get('version') != 'historical-study-campaign-v1':
        raise ValueError('unsupported scientific campaign schema')
    identifier(spec['campaign_id'])
    for key in ('runtime_sha', 'orchestration_sha', 'producer_revision'):
        if not isinstance(spec[key], str) or not re.fullmatch('[0-9a-f]{40}', spec[key]):
            raise ValueError('full implementation pin required')
    for key in ('study_manifest_sha256', 'coverage_manifest_sha256', 'study_file_sha256', 'coverage_file_sha256'):
        if not isinstance(spec[key], str) or not re.fullmatch('[0-9a-f]{64}', spec[key]):
            raise ValueError('frozen manifest pin required')
    if set(spec['dependency_locks']) != {'requirements.txt', 'requirements-research.txt'} or any(not isinstance(v, str) or not re.fullmatch('[0-9a-f]{64}', v) for v in spec['dependency_locks'].values()):
        raise ValueError('exact dependency lock pins required')
    if set(spec['expected_membership']) != {'development', 'validation', 'test'}:
        raise ValueError('explicit phase membership required')
    for indices in spec['expected_membership'].values():
        if not isinstance(indices, list) or any(type(i) is not int or not 0 <= i <= 29 for i in indices) or indices != sorted(set(indices)):
            raise ValueError('sorted unique exact membership required')
    for locator in spec['inputs'].values():
        identifier(locator['release_tag'])
        if not isinstance(locator['manifest_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', locator['manifest_sha256']):
            raise ValueError('sealed input locator required')
    if spec.get('publication_reserve_minutes') != 15 or type(spec.get('max_new_stages')) is not int or spec['max_new_stages'] <= 0:
        raise ValueError('supported publication reserve and stage allowance required')
    value = spec['control']
    if value.get('version') != PLAN or spec.get('template_only'):
        raise ValueError('unsupported or non-dispatchable campaign plan')
    if type(value.get('allow_test')) is not bool:
        raise ValueError('pinned test authorization must be boolean')
    tasks = value['tasks']
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 200:
        raise ValueError('bounded explicit task plan required')
    seen, executed, aggregated = set(), set(), set()
    for task in tasks:
        if set(task) != {'id', 'operation', 'phase', 'period_index', 'depends_on', 'expected_outputs'}:
            raise ValueError('task contract fields must be exact; arbitrary commands forbidden')
        identifier(task['id'])
        op, phase, index = task['operation'], task['phase'], task['period_index']
        if task['id'] in seen or op not in OPERATIONS or phase not in ('development', 'validation', 'test'):
            raise ValueError('unsupported or duplicate task')
        dependencies = task['depends_on']
        if (not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies)
                or not set(dependencies).issubset(seen)):
            raise ValueError('dependencies must precede task: missing dependency or cycle')
        if task['expected_outputs'] != OPERATIONS[op]:
            raise ValueError('unsupported expected output contract')
        if phase == 'test' and value.get('allow_test') is not True:
            raise ValueError('explicit pinned test authorization required')
        if op == 'aggregate':
            if index is not None or phase in aggregated:
                raise ValueError('aggregate must have unique phase and no period')
            needed = {t['id'] for t in tasks if t['operation'] == 'execute-period' and t['phase'] == phase}
            membership = {t['period_index'] for t in tasks if t['id'] in needed}
            if membership != set(spec['expected_membership'][phase]) or not needed.issubset(dependencies):
                raise ValueError('aggregate dependencies must name exact declared finalized membership')
            aggregated.add(phase)
        else:
            if type(index) is not int or index not in spec['expected_membership'][phase] or str(index) not in spec['inputs']:
                raise ValueError('undeclared exact period')
            if op == 'execute-period':
                if (phase, index) in executed:
                    raise ValueError('duplicate period execution')
                executed.add((phase, index))
        seen.add(task['id'])
    if aggregated != {phase for phase, _ in executed}:
        raise ValueError('executed phases require declared terminal aggregates')
    if spec.get('retention') != {'version': 'historical-retention-v1', 'redundant_recovery': 'verified-consolidation-only',
                                 'abandoned_drafts': 'retain-unless-proven', 'inputs_and_results': 'retain'}:
        raise ValueError('explicit conservative retention policy required')
    for key in ('ceiling_minutes', 'max_runs', 'no_progress_cap'):
        if type(spec['budget'][key]) is not int or spec['budget'][key] <= 0:
            raise ValueError('positive pinned campaign budget required')
    if spec['budget']['ceiling_minutes'] > 120 and spec.get('expanded_budget_authorized') is not True:
        raise ValueError('expanded budget lacks owner authorization')
    if spec['job_minutes'] not in (16, 20, 30, 50, 60, 120, 180, 240, 300, 350):
        raise ValueError('unsupported whole-job allocation')
    return tasks


def append(record, kind, **fields):
    entry = {'index': len(record['ledger']), 'previous': record['ledger'][-1]['sha256'] if record['ledger'] else None,
             'kind': kind, **fields}
    entry['sha256'] = digest(entry)
    record['ledger'].append(entry)


def validate(record, spec, previous=None):
    tasks = plan(spec)
    if type(record['reserved_minutes']) is not int or record['reserved_minutes'] < 0 or type(record['run_count']) is not int or record['run_count'] < 0:
        raise ValueError('invalid reservation/run counters')
    if type(record['stop_requested']) is not bool or type(record['no_progress_runs']) is not int or record['no_progress_runs'] < 0 or type(record['sequence']) is not int or record['sequence'] < 0:
        raise ValueError('invalid operational state counters')
    if record['version'] not in (VERSION, LEGACY_VERSION) or record['identity'] != identity(spec):
        raise ValueError('campaign control identity changed')
    ledger = record['ledger']
    if len(ledger) > (1536 if record['version'] == VERSION else 10000):
        raise ValueError('bounded ledger exhausted')
    for index, entry in enumerate(ledger):
        if record['version'] == VERSION and len(canonical(entry).encode()) > 512:
            raise ValueError('bounded compact ledger entry exceeded')
        if entry['kind'] not in {'CONTROL', 'CLAIM', 'INTENT', 'RECEIPT', 'DISPATCH_ATTEMPT', 'DISPATCH_CONFIRMED', 'DISPATCH_OUTCOME', 'INTERRUPTED', 'MEASURED_CONTROL', 'RETENTION_INTENT', 'RETENTION_CONFIRMED', 'STUDY_OUTCOME'}:
            raise ValueError('unsupported ledger entry contract')
        body = {k: v for k, v in entry.items() if k != 'sha256'}
        if (entry['index'] != index or entry['previous'] != (ledger[index - 1]['sha256'] if index else None)
                or entry['sha256'] != digest(body)):
            raise ValueError('ledger seal/order mismatch')
    if previous and (ledger[:len(previous['ledger'])] != previous['ledger']
                     or record['receipts'][:len(previous['receipts'])] != previous['receipts']):
        raise ValueError('committed accounting or receipts removed/modified')
    if previous and previous['state'] in TERMINAL and record['state'] != previous['state']:
        raise ValueError('terminal campaign cannot be revived')
    reservations = [entry for entry in ledger if entry['kind'] in ('CONTROL', 'CLAIM')]
    controls = [e for e in ledger if e['kind'] == 'CONTROL']
    if len({e['run_id'] for e in controls}) != len(controls) or any(e['minutes'] != 5 for e in controls):
        raise ValueError('duplicate/invalid short-job reservation')
    claims = [e for e in ledger if e['kind'] == 'CLAIM']
    if any(e['minutes'] != spec['job_minutes'] + 5 for e in claims):
        raise ValueError('claim differs from pinned whole-job allocation')
    if record['version'] == VERSION and len(controls) > 256:
        raise ValueError('bounded manual control allowance exhausted')
    if record['version'] == VERSION and any(not re.fullmatch('[0-9a-f]{64}', e.get('mutation_id', '')) or not e.get('attempt') for e in claims):
        raise ValueError('claim invocation identity missing')
    interruptions = [e['key'] for e in ledger if e['kind'] == 'INTERRUPTED']
    if len(interruptions) != len(set(interruptions)):
        raise ValueError('duplicate interruption accounting')
    if len({e['key'] for e in claims}) != len(claims) or any(type(e['minutes']) is not int or e['minutes'] <= 0 for e in reservations):
        raise ValueError('duplicate claim or invalid reserved minutes')
    if record['reserved_minutes'] != sum(e['minutes'] for e in reservations):
        raise ValueError('reservation accounting mismatch')
    if record['run_count'] != sum(e['kind'] == 'CLAIM' for e in ledger):
        raise ValueError('slice accounting mismatch')
    if set(record['tasks']) != {t['id'] for t in tasks}:
        raise ValueError('control task membership mismatch')
    if record['state'] not in TERMINAL | {'READY', 'STOP_REQUESTED', 'STOPPED', 'HANDOFF_PENDING', 'RUNNING', 'BUDGET_EXHAUSTED', 'NO_PROGRESS_LIMIT', 'OUTCOME_UNRESOLVED'}:
        raise ValueError('unsupported campaign state')
    if record['version'] == VERSION and len(record['receipts']) > 128:
        raise ValueError('bounded compact receipt allowance exhausted')
    if len({r['generation'] for r in record['receipts']}) != len(record['receipts']):
        raise ValueError('duplicate campaign receipt')
    for task_id, state in record['tasks'].items():
        done = any(r['task_id'] == task_id and r['task_complete'] for r in record['receipts'])
        if state != ('DONE' if done else 'PENDING'):
            raise ValueError('task state differs from verified receipt ledger')
    h = record['handoff']
    if record['version'] == VERSION and h and len(canonical(h).encode()) > 4096:
        raise ValueError('bounded compact handoff exceeded')
    if h:
        if h['state'] not in {'INTENT', 'DISPATCHED', 'AMBIGUOUS', 'CLAIMED', 'FINISHED', 'INTERRUPTED'}:
            raise ValueError('invalid handoff state')
        binding = handoff_binding(h)
        if (h.get('identity_sha256') != digest(record['identity']) if record['version'] == VERSION else h['identity'] != record['identity']) or h['task'] not in tasks or h['sequence'] != record['sequence'] or h['allocation_minutes'] != spec['job_minutes'] + 5 or h['key'] != digest(binding):
            raise ValueError('handoff deterministic binding mismatch')
        if not any(e['kind'] == 'INTENT' and e['key'] == h['key'] and e['binding_sha256'] == digest(binding) for e in ledger):
            raise ValueError('handoff lacks committed intent')
    if record['version'] == VERSION:
        if set(record) != {'version', 'identity', 'state', 'stop_requested', 'tasks', 'sequence', 'handoff', 'receipts', 'ledger', 'reserved_minutes', 'measured_minutes', 'run_count', 'no_progress_runs', 'reason'} or record['reason'] is not None and (not isinstance(record['reason'], str) or len(record['reason']) > 256):
            raise ValueError('invalid compact authority fields/reason')
        if h and h['parent'] != next((r for r in reversed(record['receipts']) if r['task_id'] == h['task']['id'] and r['handoff_key'] != h['key']), None):
            raise ValueError('handoff lost latest exact active-task parent')
        if spec['budget']['max_runs'] > 128 or record['run_count'] > spec['budget']['max_runs'] or len(canonical(record['identity']).encode()) > 32768 or len(canonical(record['tasks']).encode()) > 16384:
            raise ValueError('compact campaign storage/run envelope exceeded')
        if h and h['state'] == 'CLAIMED' and not any(e['kind'] == 'CLAIM' and e['key'] == h['key'] and e['run_id'] == h['run_id'] and e['attempt'] == h.get('attempt') and e['mutation_id'] == h.get('mutation_id') for e in ledger):
            raise ValueError('claimed handoff lacks exact invocation reservation')
        no_progress = 0
        for e in ledger:
            if e['kind'] == 'INTERRUPTED':
                no_progress += 1
            elif e['kind'] == 'RECEIPT':
                if type(e['progress']) is not bool or not any(digest(r) == e['receipt_sha256'] and r['handoff_key'] == e['key'] for r in record['receipts']):
                    raise ValueError('receipt accounting effect lacks sealed reference')
                no_progress = 0 if e['progress'] else no_progress + 1
        if no_progress != record['no_progress_runs']:
            raise ValueError('no-progress accounting differs from committed outcomes')
    for receipt in record['receipts']:
        if record['version'] == VERSION:
            validate_reference(receipt, record, spec)
            continue
        if receipt.get('receipt_contract') != 'historical-campaign-task-receipt-v1':
            raise ValueError('unsupported task receipt contract')
        if type(receipt['task_complete']) is not bool or not isinstance(receipt['completed_work'], list) or receipt['completed_work'] != sorted(set(receipt['completed_work'])) or any(not re.fullmatch('[0-9a-f]{64}', u) for u in receipt['completed_work']):
            raise ValueError('invalid verified committed-work receipt')
        if receipt['identity'] != record['identity'] or receipt['task_id'] not in record['tasks']:
            raise ValueError('receipt identity/task mismatch')
    measured = sum(r['measured_minutes'] for r in record['receipts']) + sum(e['minutes'] for e in ledger if e['kind'] == 'MEASURED_CONTROL')
    if not math.isfinite(measured) or measured < 0 or not math.isclose(record['measured_minutes'], measured, rel_tol=1e-12, abs_tol=1e-9):
        raise ValueError('measured accounting mismatch')
    return record


def initial(spec):
    tasks = plan(spec)
    if any(len(canonical(t).encode()) > 2048 for t in tasks) or spec['budget']['max_runs'] > 128 or len(canonical(identity(spec)).encode()) > 32768 or len(canonical({t['id']: 'PENDING' for t in tasks}).encode()) > 16384:
        raise ValueError('new compact campaign exceeds declared storage envelope')
    return {'version': VERSION, 'identity': identity(spec), 'state': 'READY', 'stop_requested': False,
            'tasks': {t['id']: 'PENDING' for t in tasks}, 'sequence': 0, 'handoff': None,
            'receipts': [], 'ledger': [], 'reserved_minutes': 0, 'measured_minutes': 0,
            'run_count': 0, 'no_progress_runs': 0, 'reason': None}


def reserve_control(record, spec, action, run_id):
    validate(record, spec)
    if record['state'] in TERMINAL:
        return
    if not any(e['kind'] == 'CONTROL' and e['run_id'] == run_id for e in record['ledger']):
        # Short control jobs reserve their full five minutes, including failed dispatch.
        append(record, 'CONTROL', run_id=run_id, minutes=5, action=action)
        record['reserved_minutes'] += 5


def control_action(record, spec, action, run_id):
    if action not in ('start', 'stop', 'resume', 'retention', 'handoff-rerun'):
        raise ValueError('unsupported control operation')
    validate(record, spec)
    if record['state'] in TERMINAL:
        return
    reserve_control(record, spec, action, run_id)
    if action in ('retention', 'handoff-rerun'):
        return
    if action == 'stop':
        record['stop_requested'] = True
        if record['state'] not in TERMINAL:
            record.update(state='STOP_REQUESTED', reason='owner-safe-stop')
        return
    if record['state'] in TERMINAL:
        return
    if action == 'start' and record['state'] in {'STOPPED', 'STOP_REQUESTED', 'OUTCOME_UNRESOLVED'}:
        return
    if action == 'resume':
        if record['handoff'] and record['handoff']['state'] == 'CLAIMED':
            record['reason'] = 'active-claimed-run; wait for safe publication or explicitly cancel first'
            return
        record['stop_requested'] = False
        if record['state'] in ('STOPPED', 'STOP_REQUESTED') and record['handoff'] and record['handoff']['state'] in ('INTENT', 'DISPATCHED', 'AMBIGUOUS'):
            record['state'] = 'HANDOFF_PENDING'
    if record['stop_requested']:
        return
    schedule(record, spec)


def schedule(record, spec):
    validate(record, spec)
    if record['stop_requested'] or record['state'] in TERMINAL | {'OUTCOME_UNRESOLVED'}:
        return
    h = record['handoff']
    if h and h['state'] in ('INTENT', 'DISPATCHED', 'AMBIGUOUS', 'CLAIMED'):
        return
    if record['no_progress_runs'] >= spec['budget']['no_progress_cap']:
        record.update(state='NO_PROGRESS_LIMIT', reason='verified-work-did-not-advance')
        return
    if all(v == 'DONE' for v in record['tasks'].values()):
        record.update(state='COMPLETED', reason=None)
        return
    # Dispatch job is separately charged, even when its response is lost.
    allocation = spec['job_minutes'] + 5
    if record['reserved_minutes'] + allocation > spec['budget']['ceiling_minutes'] or record['run_count'] >= spec['budget']['max_runs']:
        record.update(state='BUDGET_EXHAUSTED', reason='whole-job-plus-dispatch-reservation-exceeds-cap')
        return
    task = next((t for t in plan(spec) if record['tasks'][t['id']] != 'DONE'
                 and all(record['tasks'][d] == 'DONE' for d in t['depends_on'])), None)
    if task is None:
        if all(v == 'DONE' for v in record['tasks'].values()):
            record.update(state='COMPLETED', reason=None)
        else:
            record.update(state='INTEGRITY_FAILED', reason='no-ready-declared-task')
        return
    parent = next((r for r in reversed(record['receipts']) if r['task_id'] == task['id']), None)
    if h and h['state'] == 'INTERRUPTED' and h['task'] == task:
        parent = h['parent']
    sequence = record['sequence'] + 1
    binding = {'identity_sha256': digest(record['identity']), 'task': task, 'sequence': sequence,
               'parent': parent, 'allocation_minutes': allocation}
    key = digest(binding)
    record.update(sequence=sequence, state='HANDOFF_PENDING', reason=None,
                  handoff={'key': key, **binding, 'state': 'INTENT', 'run_id': None})
    append(record, 'INTENT', key=key, sequence=sequence, binding_sha256=digest(binding))


def handoff_binding(h):
    return {k: h[k] for k in (('identity_sha256' if 'identity_sha256' in h else 'identity'),
                              'task', 'sequence', 'parent', 'allocation_minutes')}


def entry_bindings(record, spec, key, bindings):
    validate(record, spec)
    h = record['handoff']
    if not h or h['key'] != key:
        raise ValueError('stale handoff identity')
    t, parent = h['task'], h['parent']
    expected = {'STUDY_OPERATION': t['operation'], 'STUDY_PHASE': t['phase'],
                'STUDY_JOB_MINUTES': str(spec['job_minutes']),
                'STUDY_PERIOD_INDEX': str(t['period_index'] if t['period_index'] is not None else 0),
                'STUDY_ALLOW_TEST': str(t['phase'] == 'test' and spec['control']['allow_test']).lower(),
                'STUDY_RESUME_GENERATION': parent['generation'] if parent else '',
                'STUDY_RESUME_SHA': parent['manifest_sha256'] if parent else ''}
    if any(bindings.get(k, '') != v for k, v in expected.items()):
        raise ValueError('dispatch bindings differ from pinned intent')
    if any(record['tasks'][d] != 'DONE' for d in t['depends_on']):
        raise ValueError('declared dependencies not complete')
    return h


def claim(record, spec, key, run_id, attempt, mutation_id, bindings):
    if not re.fullmatch('[0-9]{1,20}', run_id) or not re.fullmatch('[0-9]{1,6}', attempt) or not re.fullmatch('[0-9a-f]{64}', mutation_id):
        raise ValueError('invalid invocation identity')
    h = entry_bindings(record, spec, key, bindings)
    if record['state'] in TERMINAL | {'OUTCOME_UNRESOLVED'} or record['stop_requested']:
        return False
    if h['state'] == 'CLAIMED':
        return (h.get('mutation_id') == mutation_id and h['run_id'] == run_id
                and h.get('attempt') == attempt)
    if h['state'] not in ('INTENT', 'DISPATCHED', 'AMBIGUOUS'):
        return False
    if record['no_progress_runs'] >= spec['budget']['no_progress_cap']:
        record.update(state='NO_PROGRESS_LIMIT', reason='child-entry-no-progress-cap')
        return False
    if record['reserved_minutes'] + h['allocation_minutes'] > spec['budget']['ceiling_minutes'] or record['run_count'] >= spec['budget']['max_runs']:
        record.update(state='BUDGET_EXHAUSTED', reason='child-entry-budget-cap')
        return False
    if record['state'] != 'HANDOFF_PENDING':
        return False
    h.update(state='CLAIMED', run_id=run_id, attempt=attempt, mutation_id=mutation_id)
    append(record, 'CLAIM', key=key, run_id=run_id, attempt=attempt,
           mutation_id=mutation_id, result=True, minutes=h['allocation_minutes'])
    record['reserved_minutes'] += h['allocation_minutes']
    record['run_count'] += 1
    record.update(state='RUNNING', reason=None)
    return True


def dispatch_permitted(record, spec, key):
    validate(record, spec)
    h = record['handoff']
    if record['state'] != 'HANDOFF_PENDING' or record['stop_requested'] or not h or h['key'] != key or h['state'] not in ('INTENT', 'AMBIGUOUS', 'DISPATCHED'):
        return False
    if record['no_progress_runs'] >= spec['budget']['no_progress_cap']:
        record.update(state='NO_PROGRESS_LIMIT', reason='dispatch-no-progress-cap')
        return False
    if record['reserved_minutes'] + h['allocation_minutes'] > spec['budget']['ceiling_minutes'] or record['run_count'] >= spec['budget']['max_runs']:
        record.update(state='BUDGET_EXHAUSTED', reason='dispatch-budget-cap')
        return False
    if any(record['tasks'][d] != 'DONE' for d in h['task']['depends_on']):
        raise ValueError('dispatch dependency changed')
    return True


def dispatch_attempt(record, spec, key, run_id, attempt, mutation_id):
    if not dispatch_permitted(record, spec, key) or record['handoff']['state'] != 'INTENT':
        return False
    h = record['handoff']
    h['state'] = 'AMBIGUOUS'
    append(record, 'DISPATCH_ATTEMPT', key=key, mutation_id=mutation_id, result=True,
           run_id=run_id, attempt=attempt)
    return True


def failure_state(operation, reason):
    return ('INTEGRITY_FAILED' if reason == 'INTEGRITY' else
            'PUBLICATION_FAILED' if operation in ('publish', 'snapshot') else
            'SCIENTIFIC_FAILED' if operation in ('run', 'validate-parents') else 'SETUP_FAILED')


def study_outcome(record, spec, key, conclusion, steps, *, run_id, attempt):
    """Trusted transport supplies only the completed owned study job/step outcomes."""
    validate(record, spec)
    h = record['handoff']
    if not h or h['key'] != key or h['state'] not in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS', 'INTENT') or h['run_id'] != run_id or h.get('attempt') != attempt or record['state'] in TERMINAL:
        return
    outcome = h.get('outcome', {})
    operation, reason = outcome.get('failure_operation'), outcome.get('reason')
    state = None
    if operation in ('snapshot', 'publish') or steps.get('snapshot') == 'failure' or steps.get('publication') == 'failure':
        state = 'PUBLICATION_FAILED'
    elif operation == 'record-receipt':
        state = 'INTEGRITY_FAILED' if reason == 'INTEGRITY' else 'OUTCOME_UNRESOLVED'
    elif operation:
        state = failure_state(operation, reason)
    elif steps.get('parents') == 'failure' and reason in ('PROCESS', 'INTEGRITY', 'CONTROL'):
        state = 'SCIENTIFIC_FAILED'
    elif steps.get('parents') == 'failure' and reason not in ('SETUP_BUDGET', 'CANCELLED'):
        state = 'OUTCOME_UNRESOLVED'
    elif steps.get('entry') == 'failure' and reason == 'INTEGRITY':
        state = 'INTEGRITY_FAILED'
    elif steps.get('science') == 'failure' and reason not in ('OWNER_STOP', 'SETUP_BUDGET', 'CANCELLED') and conclusion not in ('cancelled', 'timed_out'):
        state = 'SCIENTIFIC_FAILED'
    if state:
        record.update(state=state, reason='owned-study-unclassified-failure' if state == 'OUTCOME_UNRESOLVED' else 'owned-study-established-failure')
    elif reason in ('OWNER_STOP', 'SETUP_BUDGET', 'CANCELLED', 'CAPTURE_UNAVAILABLE') or record['stop_requested'] or conclusion in ('cancelled', 'timed_out'):
        termination = 'setup-budget' if reason == 'SETUP_BUDGET' else 'owner-stop' if record['stop_requested'] or reason == 'OWNER_STOP' else 'claim-capture-unavailable' if reason == 'CAPTURE_UNAVAILABLE' else 'external-cancellation'
        typed = {'operation': h['task']['operation'], 'setup': outcome.get('setup', 'unknown'),
                 'scientific': outcome.get('scientific', 'not-started'), 'publication': outcome.get('publication', 'not-verified'),
                 'job_conclusion': conclusion, 'reason': termination}
        interrupt(record, spec, key, typed, automatic=termination == 'setup-budget')
    else:
        record.update(state='OUTCOME_UNRESOLVED', reason='owned-study-ended-without-verified-receipt-or-typed-interruption')


def interrupt(record, spec, key, outcome, automatic=False):
    validate(record, spec)
    h = record['handoff']
    if not h or h['key'] != key or h['state'] not in ('CLAIMED', 'DISPATCHED', 'AMBIGUOUS', 'INTENT'):
        return
    if record['state'] in TERMINAL:
        return
    if any(e['kind'] == 'INTERRUPTED' and e['key'] == key for e in record['ledger']):
        return
    append(record, 'INTERRUPTED', key=key, run_id=h['run_id'], attempt=h.get('attempt'),
           outcome_sha256=digest(outcome), reason=outcome['reason'])
    h.update(state='INTERRUPTED', outcome=outcome)
    record['no_progress_runs'] += 1
    record.update(state='STOPPED', reason=outcome['reason'])
    if automatic and not record['stop_requested']:
        record['state'] = 'READY'
        schedule(record, spec)


def validate_reference(receipt, record, spec):
    if len(canonical(receipt).encode()) > 1024:
        raise ValueError('bounded compact receipt exceeded')
    if receipt.get('receipt_contract') != RECEIPT or receipt['task_id'] not in record['tasks']:
        raise ValueError('compact receipt contract/task mismatch')
    task = next(t for t in plan(spec) if t['id'] == receipt['task_id'])
    if receipt['operation'] != task['operation'] or receipt['verified_outputs'] != (task['expected_outputs'] if receipt['task_complete'] else []):
        raise ValueError('compact receipt declared output/operation mismatch')
    if not re.fullmatch('[0-9]{1,20}', receipt['run_id']) or not re.fullmatch('[0-9]{1,6}', receipt['attempt']):
        raise ValueError('compact receipt child identity invalid')
    identifier(receipt['generation'])
    for key in ('manifest_sha256', 'handoff_key', 'work_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', receipt[key]):
            raise ValueError('exact sealed proof reference required')
    if type(receipt['work_count']) is not int or receipt['work_count'] < 0 or type(receipt['task_complete']) is not bool:
        raise ValueError('invalid compact proof counters')
    if receipt['setup_outcome'] not in ('success', 'unknown') or receipt['publication_outcome'] != 'VERIFIED':
        raise ValueError('invalid independently recorded receipt outcomes')
    if receipt['scientific_state'] not in ('FAILED', 'YIELDED', 'FINALIZED_PERIOD', 'NOT_STARTED') or receipt['termination_reason'] not in ('external-cancellation', 'owner-stop', 'setup-budget', 'budget-yield', 'completed', 'failed'):
        raise ValueError('invalid typed receipt outcome')
    if receipt['task_complete'] and receipt['scientific_state'] == 'FAILED':
        raise ValueError('failed receipt cannot finalize a task')
    if not math.isfinite(receipt['measured_minutes']) or receipt['measured_minutes'] < 0:
        raise ValueError('invalid receipt measurement')


def receipt_reference(value, measured_minutes):
    metadata = value['metadata']
    if measured_minutes != metadata['measured_minutes']:
        raise ValueError('receipt measurement differs from sealed sampling boundary')
    work = metadata['local_completed_work']
    return {**metadata['task_receipt'], 'publication_outcome': 'VERIFIED', 'generation': metadata['generation'],
            'manifest_sha256': value['bundle_sha256'], 'work_count': len(work),
            'work_sha256': digest(work), 'measured_minutes': measured_minutes}


def accept(record, spec, key, run_id, receipt, proof=None, interruption=None, accounting=None):
    """Receipt must already have passed remote inventory/asset and result checks."""
    validate(record, spec)
    existing = next((r for r in record['receipts'] if r['generation'] == receipt['generation']), None)
    if existing is not None:
        if existing != receipt:
            raise ValueError('committed receipt cannot be modified')
        return
    h = record['handoff']
    if not h or h['key'] != key or h['run_id'] != run_id or h['state'] != 'CLAIMED':
        raise ValueError('stale publication cannot advance campaign')
    if (record['version'] == LEGACY_VERSION and receipt['identity'] != record['identity']) or receipt['task_id'] != h['task']['id'] or receipt['handoff_key'] != key:
        raise ValueError('verified receipt does not bind claimed task')
    prior = h['parent']
    if record['version'] == VERSION:
        validate_reference(receipt, record, spec)
        if proof is None or receipt_reference(proof, receipt['measured_minutes']) != receipt:
            raise ValueError('accept requires independently verified exact sealed proof')
        closure = consolidation(proof, record['identity'])
        if accounting is None:
            raise ValueError('immutable accounting proof required at acceptance')
        validate(accounting, spec)
        owned = accounting['handoff']
        if (record['ledger'][:len(accounting['ledger'])] != accounting['ledger'] or owned['key'] != key
                or owned['run_id'] != run_id or owned.get('attempt') != h.get('attempt') or owned.get('mutation_id') != h.get('mutation_id')):
            raise ValueError('publication does not bind retained owned accounting prefix')
        if closure['parent_receipt'] != prior or closure['accounting']['receipts_sha256'] != digest(record['receipts']):
            raise ValueError('proof does not retain current exact campaign/parent references')
        progress = receipt['task_complete'] or prior is None and receipt['work_count'] > 0 or prior is not None and receipt['work_sha256'] != prior['work_sha256']
    else:
        if prior and not set(prior['completed_work']).issubset(receipt['completed_work']):
            raise ValueError('verified receipt dropped committed work')
        previous_work = record['receipts'][-1]['completed_work'] if record['receipts'] else []
        if not set(previous_work).issubset(receipt['completed_work']):
            raise ValueError('campaign receipt dropped prior work')
        progress = previous_work != receipt['completed_work'] or receipt['task_complete']
    record['no_progress_runs'] = 0 if progress else record['no_progress_runs'] + 1
    failed_state = record['state'] if record['state'] in TERMINAL else None
    record['receipts'].append(receipt)
    record['measured_minutes'] += receipt['measured_minutes']
    append(record, 'RECEIPT', key=key, receipt_sha256=digest(receipt), progress=progress)
    h['state'] = 'FINISHED'
    if failed_state:
        if receipt['task_complete']:
            record['tasks'][h['task']['id']] = 'DONE'
        return # Verified late evidence cannot revive execution.
    if receipt['scientific_state'] == 'FAILED':
        record.update(state='SCIENTIFIC_FAILED', reason='scientific-failure-retained-diagnostics')
        return
    if receipt['task_complete']:
        if receipt['verified_outputs'] != h['task']['expected_outputs']:
            raise ValueError('declared output proof missing')
        record['tasks'][h['task']['id']] = 'DONE'
    if interruption or receipt.get('termination_reason') in ('external-cancellation', 'owner-stop'):
        record.update(state='STOPPED', reason=interruption or receipt['termination_reason'])
        return
    if not receipt['task_complete'] and h['task']['operation'] != 'execute-period' and receipt.get('termination_reason') != 'setup-budget':
        record.update(state='NO_PROGRESS_LIMIT', reason='incomplete-preflight-or-aggregate-requires-owner')
        return
    record['state'] = 'STOPPED' if record['stop_requested'] else 'READY'
    if not record['stop_requested']:
        schedule(record, spec)


def legacy_consolidation(value, expected_identity, depth=0):
    """Sealed ancestry is embedded; deletion never depends on mutable status."""
    if depth >= 200:
        raise ValueError('bounded sealed ancestry exceeded')
    metadata = value['metadata']
    closure = metadata['consolidation']
    if closure['version'] != LINEAGE or value['identity'] != expected_identity:
        raise ValueError('consolidation identity/version mismatch')
    local = set(metadata['local_completed_work'])
    external = set()
    for ref in closure['finalized_receipts']:
        if ref['identity'] != expected_identity or not ref['task_complete']:
            raise ValueError('external finalized receipt mismatch')
        external.update(ref['completed_work'])
    if set(metadata['completed_work']) != local | external:
        raise ValueError('externalized committed-work contract mismatch')
    parent = closure['sealed_parent']
    locator = metadata['parent']
    if bool(parent) != bool(locator) or parent and (locator['generation'] != parent['metadata']['generation'] or locator['manifest_sha256'] != parent['bundle_sha256']):
        raise ValueError('sealed parent locator mismatch')
    if parent:
        expected = parent['bundle_sha256']
        body = {k: v for k, v in parent.items() if k != 'bundle_sha256'}
        # Scientific bundle seals hash canonical JSON without a trailing newline.
        if digest(body) != expected:
            raise ValueError('embedded sealed parent hash mismatch')
        if parent['identity'] != expected_identity or not set(parent['metadata']['completed_work']).issubset(metadata['completed_work']):
            raise ValueError('consolidation loses parent identity/work')
    proof = closure['accounting']
    if closure['finalized_receipts'] != [r for r in proof['receipts'] if r['task_complete']]:
        raise ValueError('externalized final references differ from sealed accounting')
    if proof['identity'] != expected_identity:
        raise ValueError('sealed accounting identity mismatch')
    # Ledger seals are independently validated; CAS authority remains separate.
    for i, entry in enumerate(proof['ledger']):
        if entry['sha256'] != digest({k: v for k, v in entry.items() if k != 'sha256'}) or entry['index'] != i or entry['previous'] != (proof['ledger'][i-1]['sha256'] if i else None):
            raise ValueError('sealed accounting ancestry mismatch')
    if parent and 'consolidation' in parent['metadata']:
        prior = legacy_consolidation(parent, expected_identity, depth + 1)['accounting']
        if proof['ledger'][:len(prior['ledger'])] != prior['ledger'] or proof['receipts'][:len(prior['receipts'])] != prior['receipts']:
            raise ValueError('consolidation removed/changed committed accounting')
    if metadata['lineage']['reserved_minutes'] != proof['reserved_minutes'] or metadata['lineage']['run_count'] != proof['run_count']:
        raise ValueError('sealed lineage/accounting counters mismatch')
    reservations = [e for e in proof['ledger'] if e['kind'] in ('CONTROL', 'CLAIM')]
    if sum(e['minutes'] for e in reservations) != proof['reserved_minutes'] or sum(e['kind'] == 'CLAIM' for e in proof['ledger']) != proof['run_count']:
        raise ValueError('sealed reservation totals mismatch')
    claimed = proof['handoff']
    typed = metadata['task_receipt']
    if typed.get('receipt_contract') != 'historical-campaign-task-receipt-v1':
        raise ValueError('unsupported sealed task receipt contract')
    if typed['task_id'] != claimed['task']['id'] or typed['handoff_key'] != claimed['key']:
        raise ValueError('sealed task/accounting binding mismatch')
    prior_work = proof['receipts'][-1]['completed_work'] if proof['receipts'] else []
    expected_no_progress = proof['no_progress_runs'] + 1 if prior_work == metadata['completed_work'] and not typed['task_complete'] else 0
    if metadata['lineage']['no_progress_runs'] != expected_no_progress:
        raise ValueError('sealed no-progress transition mismatch')
    return closure


def accounting_reference(record, locator):
    return {'version': 'historical-campaign-accounting-reference-v2', **locator,
            'identity_sha256': digest(record['identity']),
            'ledger_length': len(record['ledger']), 'ledger_sha256': record['ledger'][-1]['sha256'],
            'receipts_sha256': digest(record['receipts']),
            'reserved_minutes': record['reserved_minutes'], 'run_count': record['run_count'],
            'no_progress_runs': record['no_progress_runs'],
            'handoff_key': record['handoff']['key'], 'task_id': record['handoff']['task']['id'],
            'run_id': record['handoff']['run_id'], 'attempt': record['handoff']['attempt'],
            'operation': record['handoff']['task']['operation']}


def consolidation(value, expected_identity, depth=0):
    metadata = value['metadata']
    closure = metadata['consolidation']
    if closure['version'] == LINEAGE:
        return legacy_consolidation(value, expected_identity, depth)
    if closure['version'] != COMPACT_LINEAGE or value['identity'] != expected_identity:
        raise ValueError('compact consolidation identity/version mismatch')
    proof = closure['accounting']
    if proof['version'] != 'historical-campaign-accounting-reference-v2' or proof['identity_sha256'] != digest(expected_identity):
        raise ValueError('compact accounting identity mismatch')
    for field, length in (('control_commit', 40), ('control_blob_sha', 40), ('ledger_sha256', 64), ('receipts_sha256', 64)):
        if not re.fullmatch('[0-9a-f]{%d}' % length, proof[field]):
            raise ValueError('exact immutable accounting reference required')
    local = metadata['local_completed_work']
    if local != sorted(set(local)) or metadata['completed_work'] != local:
        raise ValueError('compact inventory must contain exact local work only')
    if any(not re.fullmatch('[0-9a-f]{64}', unit) for unit in local):
        raise ValueError('invalid local work proof')
    parent = closure['parent_receipt']
    if metadata['parent'] != ({k: parent[k] for k in ('generation', 'manifest_sha256')} if parent else None):
        raise ValueError('compact exact parent reference mismatch')
    typed = metadata['task_receipt']
    if typed['receipt_contract'] != RECEIPT or any(typed[k] != proof[k] for k in ('task_id', 'handoff_key', 'run_id', 'attempt', 'operation')):
        raise ValueError('compact task binding mismatch')
    progress = typed['task_complete'] or parent is None and bool(local) or parent is not None and digest(local) != parent['work_sha256']
    expected_no_progress = 0 if progress else proof['no_progress_runs'] + 1
    if (metadata['lineage']['reserved_minutes'] != proof['reserved_minutes'] or metadata['lineage']['run_count'] != proof['run_count']
            or metadata['lineage']['no_progress_runs'] != expected_no_progress):
        raise ValueError('compact lineage accounting mismatch')
    return closure


def retention_candidates(record, manifests):
    """Dry-run only. Retain unless terminal closure and exact evidence are proven."""
    protected = {r['generation'] for r in record['receipts'] if r['task_complete']}
    if record['handoff'] and record['handoff']['parent']:
        protected.add(record['handoff']['parent']['generation'])
    if record['state'] != 'COMPLETED':
        return []
    if record['version'] == VERSION:
        # Compact ancestry is remote and exact. Retain it: no deletion is safe
        # until every retained proof's transitive asset closure is available.
        return []
    covered = set().union(*(set(r['completed_work']) for r in record['receipts'] if r['task_complete']))
    candidates = []
    for receipt in record['receipts']:
        value = manifests.get(receipt['generation'])
        if value is None or receipt['generation'] in protected or value['bundle_sha256'] != receipt['manifest_sha256']:
            continue
        consolidation(value, record['identity'])
        if set(receipt['completed_work']).issubset(covered):
            candidates.append({'generation': receipt['generation'], 'manifest_sha256': receipt['manifest_sha256'],
                               'reason': 'verified-terminal-final-closure-covers-work'})
    return candidates
