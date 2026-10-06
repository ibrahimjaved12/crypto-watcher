"""Optional immutable prepared-core cache. Raw inputs remain authoritative.

No authenticated transport, pickle, cross-job verification flags or scientific
hash changes. Complete prepared replay is always tried before this module.
"""
from dataclasses import replace
from decimal import Decimal
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import sys
import tempfile

from . import historical_operational_events as events
from .historical_study_runtime import encode, decode
from .historical_study_bundles import file_sha, regular, sha
from .historical_replay_runtime import _canonical_bytes, _read_json_bytes, _sha

VERSION = 'historical-prepared-core-v1'
MAX_METADATA = 256 * 1024 ** 2
_settings = None


def cache_identity(spec, inventory):
    from .historical_market_state_study import study_replay_config, study_universe
    # Exact campaign and original source inventory include absences, pins and
    # frozen selection/configuration. Native compatibility is conservative.
    return {'version': VERSION, 'campaign_spec_sha256': _sha(_canonical_bytes(spec)),
        'input_bundle_sha256': inventory['bundle_sha256'], 'input_identity': inventory['identity'],
        'core_files': [row for row in inventory['files'] if row['path'].startswith('inputs/core/')],
        'runtime_sha': spec['runtime_sha'], 'dependency_locks': spec['dependency_locks'],
        'replay_config': encode(study_replay_config(date.fromisoformat(inventory['identity']['period']['utc_date']))),
        'universe': encode(study_universe()),
        'python': list(sys.version_info[:3]), 'sqlite': sqlite3.sqlite_version,
        'machine': platform.machine(), 'system': platform.system(), 'byteorder': sys.byteorder,
        'numeric_version': 'taker-buy-sell-imbalance-v2-exact-sign', 'schema': VERSION}


def identity_key(identity):
    return _sha(_canonical_bytes(identity))


def enabled():
    return _settings is not None


def configure(root, spec, inventory):
    global _settings
    options = spec.get('prepared_cache', {})
    _settings = (Path(root), cache_identity(spec, inventory), options) if options.get('enabled') is True else None


def _strict_metadata(value):
    """Narrow the shared codec to the immutable core's known type vocabulary."""
    allowed = {
        'binance_historical_archive': {'BinanceBoundedHistoricalReplayDataset', 'BinanceArchiveBundleManifest',
            'BinanceArchiveFileIdentity', 'HistoricalReplayTradeStreamManifest', 'BinanceArchiveDiagnostics'},
        'historical_replay': {'HistoricalReplayDatasetManifest', 'HistoricalReplayInstrument',
            'HistoricalReplaySourceInterval', 'HistoricalReplayMovementCandle', 'HistoricalReplayConfig'},
        'movement_metrics': {'MarketUniverseInput', 'MarketMovementConfig'},
        'historical_ohlc_evidence': {'BinanceTradeOHLCEvidence', 'CompletedTradeOHLCCandle'},
        'historical_taker_flow_evidence': {'HistoricalTakerFlowEvidence', 'HistoricalTakerFlowSymbolBuckets',
            'HistoricalTakerFlowBucket'},
    }
    def visit(item, native_config=False):
        if isinstance(item, dict):
            if 'dataclass' in item:
                module, name = item['dataclass']
                if not module.startswith('market_analysis.') or name not in allowed.get(module[16:], set()):
                    raise ValueError('prepared metadata type is not allowlisted')
            if set(item) & {'namespace', 'stream', 'path', 'point_window', 'point_sources'}:
                raise ValueError('prepared metadata cannot contain executable/path references')
            native_config = native_config or item.get('dataclass') == ['market_analysis.movement_metrics', 'MarketMovementConfig']
            for child in item.values():
                visit(child, native_config)
        elif isinstance(item, list):
            for child in item:
                visit(child, native_config)
        elif type(item) is float and (not native_config or not math.isfinite(item)):
            raise ValueError('prepared metadata forbids lossy numeric values')
    visit(value)
    result = decode(value)
    if encode(result) != value:
        raise ValueError('prepared metadata is not canonical typed data')
    return result


def _validate_dataset(dataset, request, expected_archive):
    from .binance_historical_archive import (_content_sha256, _check_ohlc_movement_parity,
        BINANCE_ARCHIVE_ADAPTER_VERSION, TRADE_STREAM_ORDERING_VERSION, TRADE_STREAM_DUPLICATE_VERSION)
    from .historical_replay import _canonical_inputs
    if (dataset.config != request.replay_config or dataset.universe != request.universe
            or dataset.archive_manifest != expected_archive
            or _content_sha256(dataset.archive_manifest.archive_files) != expected_archive.content_sha256):
        raise ValueError('prepared metadata differs from exact scientific inputs')
    stream = dataset.trade_stream_manifest
    if (stream.adapter_version != BINANCE_ARCHIVE_ADAPTER_VERSION
            or stream.ordering_policy_version != TRADE_STREAM_ORDERING_VERSION
            or stream.duplicate_policy_version != TRADE_STREAM_DUPLICATE_VERSION):
        raise ValueError('prepared normalized stream policy mismatch')
    view = type('CacheValidationView', (), {})()
    for name in ('config', 'universe', 'instruments', 'candles', 'source_intervals'):
        setattr(view, name, getattr(dataset, name))
    view.trades = ()
    _canonical_inputs(view)
    candles = {(c.symbol, c.open_time_ms): c for c in dataset.candles}
    if len(candles) != len(dataset.candles) or len(candles) != len(dataset.ohlc_evidence.candles):
        raise ValueError('prepared candle membership mismatch')
    for candle in dataset.ohlc_evidence.candles:
        _check_ohlc_movement_parity(candle, candles[(candle.symbol, candle.open_time_ms)])
    flow = dataset.taker_flow_evidence
    if (flow.algorithm_version != 'taker-buy-sell-imbalance-v2-exact-sign'
            or flow.engine_start_boundary_time_ms != dataset.config.engine_start_boundary_time_ms
            or flow.output_end_boundary_time_ms != dataset.config.output_end_boundary_time_ms
            or flow.finalization_grace_ms != dataset.config.finalization_grace_ms):
        raise ValueError('prepared flow configuration mismatch')
    intervals = tuple((s.symbol, s.start_boundary_time_ms, s.end_boundary_time_ms, s.source_state) for s in dataset.source_intervals)
    expected = tuple((symbol, dataset.config.engine_start_boundary_time_ms,
                      dataset.config.output_end_boundary_time_ms, 'LIVE') for symbol in dataset.universe.symbols)
    if intervals != expected:
        raise ValueError('prepared source intervals mismatch')
    diagnostics = dataset.diagnostics
    for name in ('archive_file_count', 'verified_archive_file_count', 'aggtrade_archive_count',
                 'kline_archive_count', 'aggtrade_row_count', 'kline_row_count',
                 'duplicate_aggtrade_count', 'missing_kline_minute_count'):
        if type(getattr(diagnostics, name)) is not int or getattr(diagnostics, name) < 0:
            raise ValueError('prepared diagnostics require nonnegative integer counters')
    if (type(stream.unique_replayable_row_count) is not int or type(stream.duplicate_row_count) is not int
            or stream.duplicate_row_count < 0):
        raise ValueError('prepared normalized row counters are invalid')
    if (diagnostics.archive_file_count != len(expected_archive.archive_files)
            or diagnostics.verified_archive_file_count != diagnostics.archive_file_count
            or diagnostics.aggtrade_archive_count + diagnostics.kline_archive_count != diagnostics.archive_file_count
            or diagnostics.duplicate_aggtrade_count != dataset.trade_stream_manifest.duplicate_row_count
            or dataset.trade_stream_manifest.unique_replayable_row_count <= 0
            or dataset.trade_stream_manifest.unique_replayable_row_count > diagnostics.aggtrade_row_count - diagnostics.duplicate_aggtrade_count):
        raise ValueError('prepared diagnostics/normalized counts mismatch')


def _open_index(path, dataset):
    from .binance_historical_archive import _AggTradeDuplicateIndex, _decimal_identity, _agg_identity, _AggRow
    from .historical_replay import HistoricalReplayTrade
    connection = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    index = _AggTradeDuplicateIndex(replay=True)
    index._connection = connection
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('PRAGMA cache_size=-8192')
        if connection.execute('PRAGMA integrity_check').fetchone() != ('ok',):
            raise ValueError('prepared SQLite integrity failure')
        expected_columns = {
            'aggregate_trade_ids': [('symbol', 'TEXT'), ('aggregate_trade_id', 'TEXT'), ('identity', 'BLOB')],
            'replay_trades': [('symbol', 'TEXT'), ('symbol_index', 'INTEGER'), ('first_seen_at_ms', 'INTEGER'),
                ('trade_time_ms', 'INTEGER'), ('aggregate_trade_id', 'TEXT'), ('id_length', 'INTEGER'),
                ('price', 'TEXT'), ('quantity', 'TEXT')],
        }
        objects = connection.execute("SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
        if set(objects) != {('table', 'aggregate_trade_ids'), ('table', 'replay_trades'), ('index', 'replay_canonical_order')}:
            raise ValueError('unexpected prepared SQLite objects')
        for table, columns in expected_columns.items():
            info = connection.execute(f'PRAGMA table_info({table})').fetchall()
            if [(row[1], row[2]) for row in info] != columns or any(row[3] != 1 for row in info):
                raise ValueError('prepared SQLite schema mismatch')
            expected_pk = [1 if name == 'symbol' else 2 if name == 'aggregate_trade_id' else 0 for name, _ in columns]
            if [row[5] for row in info] != expected_pk:
                raise ValueError('prepared SQLite primary key mismatch')
        if [row[2] for row in connection.execute('PRAGMA index_info(replay_canonical_order)')] != ['first_seen_at_ms', 'symbol_index', 'trade_time_ms', 'id_length', 'aggregate_trade_id']:
            raise ValueError('prepared SQLite ordering index mismatch')
        if connection.execute('SELECT COUNT(*) FROM aggregate_trade_ids').fetchone()[0] != dataset.diagnostics.aggtrade_row_count - dataset.diagnostics.duplicate_aggtrade_count:
            raise ValueError('prepared duplicate index row count mismatch')
        digest = hashlib.sha256()
        count = 0
        first = previous = None
        symbols = dataset.universe.symbols
        # Check exact source identity and numeric IDs through bounded row reads.
        for symbol, symbol_index, seen, trade_time, trade_id, length, price, quantity, identity in connection.execute(
            'SELECT r.*, a.identity FROM replay_trades r LEFT JOIN aggregate_trade_ids a '
            'ON r.symbol=a.symbol AND r.aggregate_trade_id=a.aggregate_trade_id '
            'ORDER BY first_seen_at_ms, symbol_index, trade_time_ms, id_length, r.aggregate_trade_id'):
            if (type(symbol_index) is not int or not 0 <= symbol_index < len(symbols)
                    or symbols[symbol_index] != symbol or not trade_id.isdecimal()
                    or str(int(trade_id)) != trade_id or length != len(trade_id)
                    or seen != trade_time or not dataset.config.engine_start_boundary_time_ms - 5000 < trade_time <= dataset.config.output_end_boundary_time_ms):
                raise ValueError('prepared replay row identity/order mismatch')
            p, q = Decimal(price), Decimal(quantity)
            original = json.loads(identity)
            if (not isinstance(original, list) or len(original) != 6
                    or any(type(original[k]) is not int or original[k] < 0 for k in (2, 3, 4))
                    or original[3] < original[2] or type(original[5]) is not bool
                    or not p.is_finite() or p <= 0 or not q.is_finite() or q <= 0):
                raise ValueError('prepared source trade identity mismatch')
            row = _AggRow(int(trade_id), p, q, original[2], original[3], trade_time, original[5])
            if _agg_identity(row) != identity:
                raise ValueError('prepared duplicate identity conflict')
            HistoricalReplayTrade(symbol, f"binance-usdm:{symbol}", p, q, trade_time, trade_time, int(trade_id), seen)
            key = (seen, symbol_index, trade_time, int(trade_id))
            if previous is not None and key <= previous:
                raise ValueError('prepared normalized stream order mismatch')
            if first is None:
                first = key
            previous = key
            normalized = (symbol, *key, _decimal_identity(p), _decimal_identity(q))
            digest.update(json.dumps(normalized, separators=(',', ':'), ensure_ascii=True).encode('ascii') + b'\n')
            count += 1
        manifest = dataset.trade_stream_manifest
        if (count != manifest.unique_replayable_row_count or first != manifest.first_canonical_trade_key
                or previous != manifest.last_canonical_trade_key or digest.hexdigest() != manifest.normalized_row_stream_sha256):
            raise ValueError('prepared normalized stream identity mismatch')
        return index
    except BaseException:
        connection.close()
        raise


def restore(request, expected_archive):
    if _settings is None:
        return None
    root, identity, options = _settings
    key = identity_key(identity)
    path = root / key
    events.emit('CACHE_LOOKUP', cache_identity=key)
    try:
        with events.span('prepared-cache-validation'):
            manifest_path = path / 'manifest.json'
            if regular(manifest_path).st_size > MAX_METADATA:
                raise ValueError('prepared metadata size limit')
            raw = manifest_path.read_bytes()
            value = _read_json_bytes(raw)
            body = {k: v for k, v in value.items() if k != 'content_sha256'}
            if (set(value) != {'version', 'identity', 'database_sha256', 'database_size', 'dataset', 'content_sha256'}
                    or value['version'] != VERSION or value['identity'] != identity
                    or raw != _canonical_bytes(value) or value['content_sha256'] != _sha(_canonical_bytes(body))):
                raise ValueError('prepared cache identity/seal mismatch')
            database = path / 'index.sqlite3'
            sha(value['database_sha256'])
            if (regular(database).st_size != value['database_size'] or value['database_size'] > options['max_bytes']
                    or file_sha(database) != value['database_sha256']):
                raise ValueError('prepared database seal/limit mismatch')
            dataset = _strict_metadata(value['dataset'])
            _validate_dataset(dataset, request, expected_archive)
            index = _open_index(database, dataset)
            events.emit('CACHE_HIT', cache_identity=key)
            return replace(dataset, _index=index)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, ArithmeticError, sqlite3.Error):
        events.emit('CACHE_MISS', cache_identity=key, reason='absent-incompatible-or-corrupt')
        return None


def publish_local(dataset):
    if _settings is None:
        return
    root, identity, options = _settings
    key = identity_key(identity)
    try:
        root.mkdir(parents=True, exist_ok=True)
        if (root / key).exists():
            return  # Immutable, including a rejected entry: never overwrite evidence.
        index = dataset._index
        source = Path(index._temporary.name) / 'aggregate-identities.sqlite3'
        size = regular(source).st_size
        if size > options['max_bytes'] or shutil.disk_usage(root).free < size + options['headroom_bytes'] + MAX_METADATA:
            events.emit('CACHE_BUILD_SKIPPED', reason='size-or-headroom', cache_identity=key)
            return
        temporary = Path(tempfile.mkdtemp(prefix='.prepared-core-', dir=root))
    except (OSError, ValueError):
        events.emit('CACHE_BUILD_SKIPPED', reason='optional-cache-headroom-or-io', cache_identity=key)
        return
    try:
        with events.span('prepared-cache-build'):
            # Close before copying. Transient source remains owned by this loader;
            # reopening it preserves its canonical cursor and cleanup lifecycle.
            index.commit()
            index._connection.close()
            try:
                shutil.copyfile(source, temporary / 'index.sqlite3')
            finally:
                index._connection = sqlite3.connect(source)
                index._connection.execute('PRAGMA journal_mode=OFF')
                index._connection.execute('PRAGMA synchronous=OFF')
                index._connection.execute('PRAGMA temp_store=FILE')
                index._connection.execute('PRAGMA cache_size=-8192')
            database = temporary / 'index.sqlite3'
            with database.open('rb') as handle:
                os.fsync(handle.fileno())
            body = {'version': VERSION, 'identity': identity, 'database_size': size,
                    'database_sha256': file_sha(database), 'dataset': encode(dataset)}
            raw = _canonical_bytes({**body, 'content_sha256': _sha(_canonical_bytes(body))})
            if len(raw) > MAX_METADATA:
                raise ValueError('prepared metadata too large')
            with (temporary / 'manifest.json').open('xb') as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.rename(temporary, root / key)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            events.emit('CACHE_BUILT', cache_identity=key)
    except (OSError, ValueError, sqlite3.Error):
        events.emit('CACHE_BUILD_SKIPPED', reason='optional-cache-write-failure', cache_identity=key)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
