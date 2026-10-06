"""Bounded operational telemetry, deliberately outside scientific identities."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time

VERSION = 'historical-study-operational-event-v1'
MAX_EVENT_BYTES = 4096
MAX_LOG_BYTES = 16 * 1024 * 1024
TEXT_FIELDS = {'kind', 'operation', 'phase', 'stage', 'category', 'reason', 'checkpoint_sha256',
               'stage_result_sha256', 'cache_identity', 'scientific_state', 'publication_state'}
NUMBER_FIELDS = {'period', 'remaining_compute_seconds', 'elapsed_seconds', 'duration_seconds',
    'requests_completed', 'assets_completed', 'downloaded_bytes', 'retries', 'selected_bytes',
    'transfer_bytes', 'completed_output_boundaries', 'total_output_boundaries', 'resumed_boundaries',
    'durable_boundaries', 'boundary_time_ms', 'parent_rss_bytes', 'worker_rss_bytes', 'parent_peak_rss_bytes', 'worker_peak_rss_bytes',
    'parent_cpu_seconds', 'worker_cpu_seconds', 'free_disk_bytes', 'used_disk_bytes', 'returncode', 'new_stages', 'reused_stages'}
_sink = None
_public = False
_started = time.monotonic()
_deadline = None
_context = {}
_lock = threading.Lock()


def configure(path, *, operation, phase, period=None, deadline_epoch=None, public=False):
    global _sink, _started, _deadline, _context, _public
    _public = public
    _sink = Path(path)
    _sink.parent.mkdir(parents=True, exist_ok=True)
    _started = time.monotonic()
    _deadline = (time.monotonic() + deadline_epoch - time.time() if deadline_epoch else None)
    _context = {'operation': operation, 'phase': phase, 'period': period}


def allowlisted(row):
    """No free text, paths, URLs or nested worker payloads enter public output."""
    result = {'version': VERSION}
    for key in TEXT_FIELDS:
        item = row.get(key)
        if isinstance(item, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', item):
            result[key] = item
    for key in NUMBER_FIELDS:
        item = row.get(key)
        if type(item) in (int, float) and -1e18 <= item <= 1e18 and math.isfinite(item):
            result[key] = item
    utc = row.get('utc')
    if isinstance(utc, str) and re.fullmatch(r'[0-9T:.+Z-]{1,40}', utc):
        result['utc'] = utc
    return result


def process_sample(pid, prefix):
    try:
        # /proc stat's comm may contain spaces; fields begin after its last ')'.
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return {prefix + '_rss_bytes': int(fields[21]) * os.sysconf('SC_PAGE_SIZE'),
                prefix + '_cpu_seconds': (int(fields[11]) + int(fields[12])) / os.sysconf('SC_CLK_TCK')}
    except (OSError, ValueError, IndexError):
        return {}


def resources(worker=None):
    result = process_sample(os.getpid(), 'parent')
    if worker:
        result.update(process_sample(worker, 'worker'))
    disk = shutil.disk_usage('/tmp')
    result['free_disk_bytes'] = disk.free
    result['used_disk_bytes'] = disk.used
    return result


def emit(category, **details):
    if _sink is None:
        return
    row = allowlisted({**_context, **details, 'category': category,
        'utc': datetime.now(timezone.utc).isoformat(), 'elapsed_seconds': time.monotonic() - _started,
        'remaining_compute_seconds': max(0, _deadline - time.monotonic()) if _deadline else None})
    raw = (json.dumps(row, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()
    if len(raw) > MAX_EVENT_BYTES:
        return
    with _lock:
        if _sink.exists() and _sink.stat().st_size + len(raw) > MAX_LOG_BYTES:
            return
        with _sink.open('ab') as handle:
            handle.write(raw)
            handle.flush()
    if _public:
        print("study-event " + raw.decode().rstrip(), flush=True)


@contextmanager
def span(stage):
    started = time.monotonic()
    emit('STARTED', stage=stage)
    stop = threading.Event()
    def pulse():
        while not stop.wait(45):
            emit('HEARTBEAT', stage=stage, duration_seconds=time.monotonic() - started, **resources())
    thread = threading.Thread(target=pulse, daemon=True)
    thread.start()
    try:
        yield
    except BaseException:
        emit('INTERRUPTED', stage=stage, duration_seconds=time.monotonic() - started)
        raise
    else:
        emit('COMPLETED', stage=stage, duration_seconds=time.monotonic() - started)
    finally:
        stop.set()
        thread.join(timeout=1)
