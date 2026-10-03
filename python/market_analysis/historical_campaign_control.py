"""Versioned, stdlib-only campaign decisions. No network, shell or scientific imports.

Operational records are not scientific proof. Callers supply independently verified
sealed receipts; immutable ledger entries and reservations share one CAS record.
"""
import hashlib
import json
import math
import re

VERSION = 'historical-campaign-control-v1'
LINEAGE = 'historical-campaign-consolidation-v1'
PLAN = 'historical-campaign-plan-v1'
OPERATIONS = {'preflight': ['preflight'], 'execute-period': ['period-report', 'period-evidence'],
              'aggregate': ['execution-index']}
TERMINAL = {'COMPLETED', 'SCIENTIFIC_FAILED', 'PUBLICATION_FAILED', 'INTEGRITY_FAILED'}


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
    if type(record['stop_requested']) is not bool or type(record['no_progress_runs']) is not int or record['no_progress_runs'] < 0 or type(record['sequence']) is not int or record['sequence'] < 0:
        raise ValueError('invalid operational state counters')
    if record['version'] != VERSION or record['identity'] != identity(spec):
        raise ValueError('campaign control identity changed')
    ledger = record['ledger']
    if len(ledger) > 10000:
        raise ValueError('bounded ledger exhausted')
    for index, entry in enumerate(ledger):
        if entry['kind'] not in {'CONTROL', 'CLAIM', 'INTENT', 'RECEIPT', 'DISPATCH_ATTEMPT', 'DISPATCH_CONFIRMED', 'DISPATCH_OUTCOME', 'INTERRUPTED', 'MEASURED_CONTROL', 'RETENTION_INTENT', 'RETENTION_CONFIRMED'}:
            raise ValueError('unsupported ledger entry contract')
        body = {k: v for k, v in entry.items() if k != 'sha256'}
        if (entry['index'] != index or entry['previous'] != (ledger[index - 1]['sha256'] if index else None)
                or entry['sha256'] != digest(body)):
            raise ValueError('ledger seal/order mismatch')
    if previous and (ledger[:len(previous['ledger'])] != previous['ledger']
                     or record['receipts'][:len(previous['receipts'])] != previous['receipts']):
        raise ValueError('committed accounting or receipts removed/modified')
    reservations = [entry for entry in ledger if entry['kind'] in ('CONTROL', 'CLAIM')]
    controls = [e for e in ledger if e['kind'] == 'CONTROL']
    if len({e['run_id'] for e in controls}) != len(controls) or any(e['minutes'] != 5 for e in controls):
        raise ValueError('duplicate/invalid short-job reservation')
    claims = [e for e in ledger if e['kind'] == 'CLAIM']
    if any(e['minutes'] != spec['job_minutes'] + 5 for e in claims):
        raise ValueError('claim differs from pinned whole-job allocation')
    if len({e['key'] for e in claims}) != len(claims) or any(type(e['minutes']) is not int or e['minutes'] <= 0 for e in reservations):
        raise ValueError('duplicate claim or invalid reserved minutes')
    if record['reserved_minutes'] != sum(e['minutes'] for e in reservations):
        raise ValueError('reservation accounting mismatch')
    if record['run_count'] != sum(e['kind'] == 'CLAIM' for e in ledger):
        raise ValueError('slice accounting mismatch')
    if set(record['tasks']) != {t['id'] for t in tasks}:
        raise ValueError('control task membership mismatch')
    if record['state'] not in TERMINAL | {'READY', 'STOP_REQUESTED', 'STOPPED', 'HANDOFF_PENDING', 'RUNNING', 'BUDGET_EXHAUSTED', 'NO_PROGRESS_LIMIT'}:
        raise ValueError('unsupported campaign state')
    if len({r['generation'] for r in record['receipts']}) != len(record['receipts']):
        raise ValueError('duplicate campaign receipt')
    for task_id, state in record['tasks'].items():
        done = any(r['task_id'] == task_id and r['task_complete'] for r in record['receipts'])
        if state != ('DONE' if done else 'PENDING'):
            raise ValueError('task state differs from verified receipt ledger')
    h = record['handoff']
    if h:
        binding = {k: h[k] for k in ('identity', 'task', 'sequence', 'parent', 'allocation_minutes')}
        if h['identity'] != record['identity'] or h['task'] not in tasks or h['sequence'] != record['sequence'] or h['allocation_minutes'] != spec['job_minutes'] + 5 or h['key'] != digest(binding):
            raise ValueError('handoff deterministic binding mismatch')
        if not any(e['kind'] == 'INTENT' and e['key'] == h['key'] and e['binding_sha256'] == digest(binding) for e in ledger):
            raise ValueError('handoff lacks committed intent')
    for receipt in record['receipts']:
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
    return {'version': VERSION, 'identity': identity(spec), 'state': 'READY', 'stop_requested': False,
            'tasks': {t['id']: 'PENDING' for t in tasks}, 'sequence': 0, 'handoff': None,
            'receipts': [], 'ledger': [], 'reserved_minutes': 0, 'measured_minutes': 0,
            'run_count': 0, 'no_progress_runs': 0, 'reason': None}


def control_action(record, spec, action, run_id):
    if action not in ('start', 'stop', 'resume', 'retention', 'handoff-rerun'):
        raise ValueError('unsupported control operation')
    validate(record, spec)
    if not any(e['kind'] == 'CONTROL' and e['run_id'] == run_id for e in record['ledger']):
        # Short control jobs reserve their full five minutes, including failed dispatch.
        append(record, 'CONTROL', run_id=run_id, minutes=5, action=action)
        record['reserved_minutes'] += 5
    if action in ('retention', 'handoff-rerun'):
        return
    if action == 'stop':
        record['stop_requested'] = True
        if record['state'] not in TERMINAL:
            record.update(state='STOP_REQUESTED', reason='owner-safe-stop')
        return
    if record['state'] in TERMINAL:
        return
    if action == 'resume':
        if record['handoff'] and record['handoff']['state'] == 'CLAIMED':
            record['reason'] = 'active-claimed-run; wait for safe publication or explicitly cancel first'
            return
        record['stop_requested'] = False
    if record['stop_requested']:
        return
    schedule(record, spec)


def schedule(record, spec):
    if record['stop_requested'] or record['state'] in TERMINAL:
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
    sequence = record['sequence'] + 1
    binding = {'identity': record['identity'], 'task': task, 'sequence': sequence,
               'parent': parent, 'allocation_minutes': allocation}
    key = digest(binding)
    record.update(sequence=sequence, state='HANDOFF_PENDING', reason=None,
                  handoff={'key': key, **binding, 'state': 'INTENT', 'run_id': None})
    append(record, 'INTENT', key=key, sequence=sequence, binding_sha256=digest(binding))


def claim(record, spec, key, run_id):
    validate(record, spec)
    h = record['handoff']
    if record['stop_requested'] or not h or h['key'] != key:
        return False
    if h['state'] == 'CLAIMED':
        # Workflow reruns and duplicate children refuse entry as well.
        return False
    if h['state'] not in ('INTENT', 'DISPATCHED', 'AMBIGUOUS'):
        return False
    if record['no_progress_runs'] >= spec['budget']['no_progress_cap']:
        record.update(state='NO_PROGRESS_LIMIT', reason='child-entry-no-progress-cap')
        return False
    if record['reserved_minutes'] + h['allocation_minutes'] > spec['budget']['ceiling_minutes'] or record['run_count'] >= spec['budget']['max_runs']:
        record.update(state='BUDGET_EXHAUSTED', reason='child-entry-budget-cap')
        return False
    h.update(state='CLAIMED', run_id=run_id)
    append(record, 'CLAIM', key=key, run_id=run_id, minutes=h['allocation_minutes'])
    record['reserved_minutes'] += h['allocation_minutes']
    record['run_count'] += 1
    record.update(state='RUNNING', reason=None)
    return True


def accept(record, spec, key, run_id, receipt):
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
    if receipt['identity'] != record['identity'] or receipt['task_id'] != h['task']['id'] or receipt['handoff_key'] != key:
        raise ValueError('verified receipt does not bind claimed task')
    prior = h['parent']
    if prior and not set(prior['completed_work']).issubset(receipt['completed_work']):
        raise ValueError('verified receipt dropped committed work')
    previous_work = record['receipts'][-1]['completed_work'] if record['receipts'] else []
    if not set(previous_work).issubset(receipt['completed_work']):
        raise ValueError('campaign receipt dropped externalized prior committed work')
    record['no_progress_runs'] = (record['no_progress_runs'] + 1 if
        previous_work == receipt['completed_work'] and not receipt['task_complete'] else 0)
    record['receipts'].append(receipt)
    record['measured_minutes'] += receipt['measured_minutes']
    append(record, 'RECEIPT', key=key, generation=receipt['generation'], manifest_sha256=receipt['manifest_sha256'])
    h['state'] = 'FINISHED'
    if receipt['scientific_state'] == 'FAILED':
        record.update(state='SCIENTIFIC_FAILED', reason='scientific-failure-retained-diagnostics')
        return
    if receipt['task_complete']:
        if receipt['verified_outputs'] != h['task']['expected_outputs']:
            raise ValueError('declared output proof missing')
        record['tasks'][h['task']['id']] = 'DONE'
    elif h['task']['operation'] != 'execute-period':
        record.update(state='NO_PROGRESS_LIMIT', reason='incomplete-preflight-or-aggregate-requires-owner')
        return
    record['state'] = 'STOPPED' if record['stop_requested'] else 'READY'
    if not record['stop_requested']:
        schedule(record, spec)


def consolidation(value, expected_identity, depth=0):
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
        prior = consolidation(parent, expected_identity, depth + 1)['accounting']
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


def retention_candidates(record, manifests):
    """Dry-run only. Retain unless terminal closure and exact evidence are proven."""
    protected = {r['generation'] for r in record['receipts'] if r['task_complete']}
    if record['handoff'] and record['handoff']['parent']:
        protected.add(record['handoff']['parent']['generation'])
    if record['state'] != 'COMPLETED':
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
