"""Private doji-v1 event reports; no new signals, labels or execution rules.

The label CSV reader is the same reader used by label_store. Stream once per
symbol, retaining only doji events and sufficient statistics for every label
geometry's unconditional barrier baseline. No outcome data is public output.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fractions import Fraction
from math import isfinite, log, sqrt
from pathlib import Path
from types import SimpleNamespace
import csv
import json

from .. import data_lake
from .bars import COMPROMISED_FLAGS, MISSING
from .baselines import manifest_ticks, random_walk_hit_rate
from .calibration import _render, check_segment
from .candles import build_candles
from .canonical import canonical_bytes, content_hash, exact_to_str
from .evaluate import nearest_rank, net_at
from .labels import LabelParams, read_label_csv
from .market_data import load_symbol_bars
from .scan import OUTCOMES, UR
from .segments import eligible, segment_bounds_ms, worst_case_window_end_ms
from .ta_strategies import doji_readings

SCHEMA = "doji-report-v2"
VERSION = "doji-v1"
STRATEGIES = ("doji_continuation_v1", "doji_reversal_v1")
QUANTILES = (50, 75, 90, 95, 99)
TIERS = ("weak", "strong", "very_strong")
MINUTE = data_lake.MINUTE_MS
SCALE = 10 ** data_lake.SCALE_DIGITS
SIGMA_SCALE = 10 ** 20
HEADLINE_K = HEADLINE_RR = Fraction(1)
EVENT_FIELDS = (
    "decision_time", "symbol", "strategy", "timeframe", "side", "decision_ms", "side_sign",
    "subtype", "tier", "score", "tightness_contribution", "size_contribution", "volume_contribution",
    "trend_contribution", "location_contribution", "doji_open", "doji_high", "doji_low", "doji_close",
    "rel_volume", "range_atr", "adx_prev", "adx_band", "trend_dir", "entry_time", "entry_price",
    "stop", "target", "k", "rr", "status", "outcome", "gross_r", "net_r_1x", "net_r_2x",
    "intrabar_ambiguous", "direction_correct", "gross_verdict", "net_verdict",
    "mfe_r", "mae_r", "mfe_pct", "mae_pct", "direction_unavailable", "excursion_unavailable",
)


def wilson(successes: int, n: int) -> list | None:
    """Two-sided Wilson score interval, 95%, without a continuity correction."""
    if not 0 <= successes <= n:
        raise ValueError("Wilson counts require 0 <= successes <= n")
    if not n:
        return None
    z = 1.959963984540054
    p = successes / n
    denominator = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denominator
    radius = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def verdict(outcome, value, *, status="trade"):
    if status != "trade":
        return None
    if outcome == "X":
        return "unmeasured"
    if outcome not in OUTCOMES or value is None or not isfinite(value):
        raise ValueError("measured trade requires an outcome and finite R")
    return value > 0


def sample_events(events, limit=25):
    """Nearest unique events to evenly spaced UTC times, endpoints included.

    Stable tie breakers make sampling independent of symbol/download order. It
    never inspects returns, score or verdict when choosing the ranks.
    """
    if limit < 1:
        raise ValueError("sample limit must be positive")
    ordered = sorted(events, key=lambda e: (e["decision_ms"], e["symbol"], e["strategy"],
                                           e["timeframe"], e["side"], e["k"], e["rr"]))
    if len(ordered) <= limit:
        return ordered
    times = [e["decision_ms"] for e in ordered]
    if limit == 1:
        middle = Fraction(times[0] + times[-1], 2)
        return [min(ordered, key=lambda e: abs(e["decision_ms"] - middle))]
    indices = [0]
    for j in range(1, limit - 1):
        target = times[0] + Fraction((times[-1] - times[0]) * j, limit - 1)
        low, high = indices[-1] + 1, len(ordered) - limit + j
        index = bisect_left(times, target, low, high + 1)
        candidates = {min(high, max(low, index)), min(high, max(low, index - 1))}
        indices.append(min(candidates, key=lambda i: (abs(times[i] - target), i)))
    return [ordered[i] for i in [*indices, len(ordered) - 1]]


def utc(ms):
    return datetime.fromtimestamp(ms // 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def direction(bars, signal_ms, horizon, side, segment):
    """Audit convention: decision is end of minute e-1; entry opens at signal_ms."""
    first, end = segment_bounds_ms(segment)
    e = (signal_ms - bars.start_ms) // MINUTE
    x = e + horizon
    if not first <= signal_ms or signal_ms + horizon * MINUTE >= end or e < 0 or x >= bars.minutes:
        return None, "incomplete_horizon"
    if any(bars.flags[i] & COMPROMISED_FLAGS or bars.open[i] == MISSING or bars.open[i] <= 0
           for i in (e, x)):
        return None, "compromised_endpoint"
    value = side * log(bars.open[x] / bars.open[e])
    return (True if value > 0 else False if value < 0 else "tie"), None


def excursions(bars, signal_ms, horizon, side, sigma, k):
    """[e,e+h-1] 1m extremes; positive magnitudes, k*sigma_h risk units.

    These describe the whole signal horizon, including bars after a trade exit.
    They are not a simulated leveraged wallet or a liquidation test.
    """
    e = (signal_ms - bars.start_ms) // MINUTE
    x = e + horizon
    if e < 0 or x > bars.minutes:
        return {}, "incomplete_horizon"
    if any(bars.flags[i] & COMPROMISED_FLAGS or bars.high[i] == MISSING or bars.low[i] == MISSING
           for i in range(e, x)):
        return {}, "compromised_path"
    p0 = bars.open[e]
    if p0 == MISSING or p0 <= 0:
        return {}, "entry_unavailable"
    high, low = max(bars.high[e:x]), min(bars.low[e:x])
    favorable, adverse = ((high - p0, p0 - low) if side == 1 else (p0 - low, high - p0))
    risk = Fraction(k) * sigma * p0 / SIGMA_SCALE
    if risk <= 0:
        return {}, "sigma_unavailable"
    return {"mfe_r": float(max(0, favorable) / risk), "mae_r": float(max(0, adverse) / risk),
            "mfe_pct": 100 * max(0, favorable) / p0, "mae_pct": 100 * max(0, adverse) / p0}, None


def cell_values(row, pair):
    """Recover pure gross before funding/costs; retain evaluator wallet floor at 2x."""
    cell = pair.pess
    result = {"status": "trade", "outcome": cell.outcome, "intrabar_ambiguous": pair.opt is not None,
              "gross_r": None, "net_r_1x": None, "net_r_2x": None}
    if cell.outcome != "X":
        trade = SimpleNamespace(outcome=cell.outcome, net_ur=cell.net_ur, cost_ur=cell.cost_ur,
                                wallet_ur=row.wallet_ur)
        result.update(gross_r=(cell.net_ur + cell.cost_ur - cell.fund_ur) / UR,
                      net_r_1x=float(net_at(trade) / UR), net_r_2x=float(net_at(trade, 2) / UR))
    return result


@dataclass
class TradeStats:
    signals: int = 0
    trades: int = 0
    measured: int = 0
    gross_wins: int = 0
    net_wins: int = 0
    net2_wins: int = 0
    ambiguous: int = 0
    outcomes: Counter = field(default_factory=Counter)
    non_trades: Counter = field(default_factory=Counter)
    sums: Counter = field(default_factory=Counter)

    def add(self, event):
        self.signals += 1
        if event["status"] != "trade":
            self.non_trades[event["status"]] += 1
            return
        self.trades += 1
        self.outcomes[event["outcome"]] += 1
        self.ambiguous += event["outcome"] == "X" or event.get("intrabar_ambiguous", False)
        if event["outcome"] == "X":
            return
        self.measured += 1
        self.gross_wins += event["gross_r"] > 0
        self.net_wins += event["net_r_1x"] > 0
        self.net2_wins += event["net_r_2x"] > 0
        for key in ("gross_r", "net_r_1x", "net_r_2x"):
            self.sums[key] += event[key]

    def merge(self, other):
        for name in ("signals", "trades", "measured", "gross_wins", "net_wins", "net2_wins", "ambiguous"):
            setattr(self, name, getattr(self, name) + getattr(other, name))
        self.outcomes.update(other.outcomes)
        self.non_trades.update(other.non_trades)
        self.sums.update(other.sums)

    def report(self, rr):
        n = self.trades
        return {"n_signals": self.signals, "n_trades": n, "n_measured": self.measured,
                "outcome_counts": {code: self.outcomes[code] for code in OUTCOMES},
                "outcome_shares": {code: self.outcomes[code] / n if n else None for code in OUTCOMES},
                "gross_hit_rate": self.gross_wins / n if n else None,
                "net_hit_rate": self.net_wins / n if n else None,
                "net_2x_hit_rate": self.net2_wins / n if n else None,
                "net_wilson_95": wilson(self.net_wins, n), "gross_wilson_95": wilson(self.gross_wins, n),
                "ambiguous_share": self.ambiguous / n if n else None,
                "x_share": self.outcomes["X"] / n if n else None,
                "barrier_base_rate": float(random_walk_hit_rate(rr)),
                "target_first_ts": (self.outcomes["T"] / (self.outcomes["T"] + self.outcomes["S"])
                                    if self.outcomes["T"] + self.outcomes["S"] else None),
                "non_trades": dict(sorted(self.non_trades.items())),
                **{"mean_" + key: self.sums[key] / self.measured if self.measured else None
                   for key in ("gross_r", "net_r_1x", "net_r_2x")}}


def direction_stats(values):
    counts = Counter(values)
    n = counts[True] + counts[False]
    return {"n": n, "correct": counts[True], "incorrect": counts[False], "ties": counts["tie"],
            "unavailable": counts[None], "hit_rate": counts[True] / n if n else None,
            "wilson_95": wilson(counts[True], n)}


def adx_band(value):
    return "20-<25" if value < 25 else "25-<40" if value < 40 else ">=40"


def collect_symbol(bars_dir, label_dir, symbol, segment, horizons, params):
    """Guard before opening files; no hidden history or cross-segment outcomes."""
    months = check_segment(segment)
    first, end = segment_bounds_ms(segment)
    ticks = manifest_ticks(label_dir, symbol, months, params)
    bars = load_symbol_bars(bars_dir, symbol, data_lake.FIRST_MONTH, months[-1])
    events, by_signal = [], defaultdict(list)
    aligned = {}
    direction_values = {(h, side, exact_to_str(k)): []
                        for h in horizons for side in (1, -1) for k in params.k_grid}
    for horizon in horizons:
        candles = build_candles(bars, horizon)
        aligned[horizon] = {ms for i, ms in enumerate(candles.end_ms)
                            if candles.valid[i] and eligible(segment, ms, worst_case_window_end_ms(
                                ms, horizon, params.time_limit_multiple))}
        for i, continuation, features, reading in doji_readings(candles):
            ms = candles.end_ms[i]
            if not first <= ms < end:
                continue
            for strategy in STRATEGIES:
                side = continuation if strategy == STRATEGIES[0] else -continuation
                base = {"symbol": symbol, "timeframe": horizon, "strategy": strategy,
                        "side": "long" if side == 1 else "short", "side_sign": side,
                        "decision_ms": ms, "decision_time": utc(ms), "subtype": reading.subtype,
                        "tier": reading.tier, "score": reading.score,
                        **{component.name + "_contribution": component.contribution for component in reading.components},
                        **{"doji_" + name: getattr(candles, name)[i] / SCALE for name in ("open", "high", "low", "close")},
                        "rel_volume": features.rel_volume, "range_atr": features.range_atr,
                        "adx_prev": features.adx_prev, "adx_band": adx_band(features.adx_prev),
                        "trend_dir": features.trend_dir, "entry_time": None, "entry_price": None,
                        "stop": None, "target": None, "outcome": None, "gross_r": None,
                        "net_r_1x": None, "net_r_2x": None, "intrabar_ambiguous": None,
                        "direction_correct": None, "gross_verdict": None, "net_verdict": None,
                        "mfe_r": None, "mae_r": None, "mfe_pct": None, "mae_pct": None,
                        "direction_unavailable": None, "excursion_unavailable": None}
                for k in params.k_grid:
                    for rr_index, rr in enumerate(params.rr_grid):
                        event = {**base, "k": exact_to_str(k), "rr": exact_to_str(rr), "status": "missing_label"}
                        if not eligible(segment, ms, worst_case_window_end_ms(ms, horizon, params.time_limit_multiple)):
                            event["status"] = "purged"
                        else:
                            by_signal[(ms, horizon, side, k)].append((rr_index, event))
                        events.append(event)
    baseline = defaultdict(TradeStats)
    found = set()
    # Same ordering, unique-key and worst-case-window rules as label_store.
    for month in months:
        start, stop, _ = data_lake.month_bounds_ms(month)
        previous = None
        with (Path(label_dir) / f"labels__{symbol}__{month}.csv.gz").open("rb") as stream:
            for row in read_label_csv(stream, params):
                order = (row.signal_ms, row.horizon_min, -row.side, row.k)
                if not start <= row.signal_ms < stop or (previous is not None and order <= previous):
                    raise ValueError("unordered, duplicate or out-of-month label")
                previous = order
                if row.horizon_min not in horizons or not eligible(
                        segment, row.signal_ms, worst_case_window_end_ms(
                            row.signal_ms, row.horizon_min, params.time_limit_multiple)):
                    continue
                key = (row.signal_ms, row.horizon_min, row.side, row.k)
                selected = by_signal.get(key, ())
                if selected:
                    found.add(key)
                baseline_member = row.status == "T" and row.signal_ms in aligned[row.horizon_min]
                if baseline_member:
                    direction_values[(row.horizon_min, row.side, exact_to_str(row.k))].append(
                        direction(bars, row.signal_ms, row.horizon_min, row.side, segment)[0])
                for rr_index, rr in enumerate(params.rr_grid):
                    values = cell_values(row, row.cells[rr_index]) if row.status == "T" else {"status": row.status}
                    if baseline_member:
                        baseline[(symbol, row.horizon_min, row.side, exact_to_str(row.k), exact_to_str(rr))].add(values)
                    for index, event in selected:
                        if index != rr_index:
                            continue
                        event.update(values)
                        if row.status != "T":
                            continue
                        e = bars.index_of(row.signal_ms)
                        if bars.open[e] != row.p0:
                            raise ValueError("label entry differs from downloaded bars")
                        tick = ticks[month]
                        target_ticks = -(-rr.numerator * row.d_ticks // rr.denominator)
                        event.update(entry_time=utc(row.signal_ms), entry_price=row.p0 / SCALE,
                                     stop=(row.p0 - row.side * row.d_ticks * tick) / SCALE,
                                     target=(row.p0 + row.side * target_ticks * tick) / SCALE)
                        event["direction_correct"], event["direction_unavailable"] = direction(
                            bars, row.signal_ms, row.horizon_min, row.side, segment)
                        ext, reason = excursions(bars, row.signal_ms, row.horizon_min, row.side, row.sigma, row.k)
                        event.update(ext, excursion_unavailable=reason)
                        event["gross_verdict"] = verdict(event["outcome"], event["gross_r"])
                        event["net_verdict"] = verdict(event["outcome"], event["net_r_1x"])
    if set(by_signal) != found:
        raise ValueError("signal missing from label grid; refusing a partial report")
    unconditional = {key: direction_stats(values) for key, values in direction_values.items()}
    return events, unconditional, baseline


def _pooled_direction(unconditional, symbols, horizon, side, k):
    rows = [unconditional[(symbol, horizon, side, k)] for symbol in symbols]
    n, correct = sum(r["n"] for r in rows), sum(r["correct"] for r in rows)
    return {"n": n, "correct": correct, "ties": sum(r["ties"] for r in rows),
            "unavailable": sum(r["unavailable"] for r in rows),
            "hit_rate": correct / n if n else None, "wilson_95": wilson(correct, n)}


def summarize(events, unconditional, baseline, symbols, horizons, params):
    """Primary cells are fixed before data; all slices/grid/excursions descriptive."""
    headline = [e for e in events if e["k"] == "1" and e["rr"] == "1"]

    def cell(group, strategy, horizon, side, k="1", rr="1", pool_symbols=None):
        stats = TradeStats()
        for event in group:
            stats.add(event)
        direction_result = direction_stats(e["direction_correct"] for e in group)
        direction_result["unconditional"] = _pooled_direction(
            unconditional, pool_symbols or symbols, horizon, side, k)
        trade_result = stats.report(Fraction(rr))
        always = TradeStats()
        for symbol in pool_symbols or symbols:
            always.merge(baseline.get((symbol, horizon, side, k, rr), TradeStats()))
        trade_result["unconditional_label_grid"] = always.report(Fraction(rr))
        return {"strategy": strategy, "timeframe": horizon, "side": "long" if side == 1 else "short",
                "k": k, "rr": rr, "small_n": stats.trades < 100 or direction_result["n"] < 100,
                "direction": direction_result, "trade": trade_result}

    primary = []
    for strategy in STRATEGIES:
        for horizon in horizons:
            for side in (1, -1):
                group = [e for e in headline if (e["strategy"], e["timeframe"], e["side_sign"]) ==
                         (strategy, horizon, side)]
                primary.append(cell(group, strategy, horizon, side))
    descriptive = {}
    for dimension in ("tier", "subtype", "symbol", "adx_band"):
        groups = defaultdict(list)
        for e in headline:
            groups[(e["strategy"], e["timeframe"], e["side_sign"], e[dimension])].append(e)
        descriptive[dimension] = [{**cell(group, strategy, h, side,
                                          pool_symbols=[value] if dimension == "symbol" else None), dimension: value}
                                  for (strategy, h, side, value), group in sorted(groups.items())]
    grid = []
    for strategy in STRATEGIES:
        for h in horizons:
            for side in (1, -1):
                for k in params.k_grid:
                    for rr in params.rr_grid:
                        kt, rt = exact_to_str(k), exact_to_str(rr)
                        group = [e for e in events if (e["strategy"], e["timeframe"], e["side_sign"], e["k"], e["rr"])
                                 == (strategy, h, side, kt, rt)]
                        grid.append(cell(group, strategy, h, side, kt, rt))
    tails = []
    for item in primary:
        group = [e for e in headline if (e["strategy"], e["timeframe"], e["side"]) ==
                 (item["strategy"], item["timeframe"], item["side"]) and e["status"] == "trade"]
        usable = [e for e in group if e["mae_pct"] is not None]
        tails.append({"strategy": item["strategy"], "timeframe": item["timeframe"], "side": item["side"],
                      "n": len(usable), "unavailable": len(group) - len(usable),
                      "quantiles": {key: {str(q): nearest_rank([e[key] for e in usable], Fraction(q, 100))
                                          for q in QUANTILES} for key in ("mfe_r", "mae_r", "mfe_pct", "mae_pct")},
                      "leverage_mae_share": {str(leverage): sum(e["mae_pct"] >= 100 / leverage for e in usable) /
                                             len(usable) if usable else None for leverage in (3, 5, 10, 20)},
                      "leverage_note": "Ignoring maintenance margin and funding; full-horizon path, not a wallet simulation"})
    return {"primary": primary, "descriptive": descriptive, "grid": grid, "excursions": tails}


def build_report(events, unconditional, baseline, *, segment, symbols, horizons, params,
                 code_commit, created_utc, data_snapshot_id, data_tags, label_tags):
    check_segment(segment)
    if HEADLINE_K not in params.k_grid or HEADLINE_RR not in params.rr_grid:
        raise ValueError("headline k=1 rr=1 is absent from label grid")
    report = {"schema": SCHEMA, "pattern_version": VERSION, "labels_version": params.schema,
              "label_params_identity": params.identity(), "segment": segment, "symbols": list(symbols),
              "horizons": list(horizons), "k": "1", "rr": "1", "code_commit": code_commit,
              "data_snapshot_id": data_snapshot_id,
              "data_release_tags": sorted(data_tags), "label_release_tags": sorted(label_tags),
              "events_count": sum(e["k"] == "1" and e["rr"] == "1" for e in events),
              "grid_event_cells": len(events), "notes": [
                  "Primary A direction and B trade are separate hypotheses for every strategy/timeframe/side.",
                  "Direction ties are excluded from rates and counted. Non-trades get no verdicts.",
                  "X verdicts are unmeasured; X counts as a loss in hit rates; monetary means exclude X, with n_measured shown.",
                  "Both unconditional baselines use valid candle-close decision times with label status T, "
                  "the same side/k and the same segment/worst-case eligibility window as signals. "
                  "Direction ties and unavailable endpoints are counted separately and excluded from rates.",
                  "Intrabar ambiguity retains the label engine's pessimistic cell and is also counted separately.",
                  "Barrier base rate = existing 1/(1+rr) driftless target-first reference (T/(T+S)); "
                  "net/gross profitability rates have different denominators and costs. The unconditional "
                  "same-side candle-aligned label profitability baseline is also shown.",
                  "Naive Wilson CIs are optimistic when outcomes overlap; no independence or adjusted significance is claimed.",
                  "MFE/MAE use [e,e+h-1] 1m highs/lows, in k*sigma_h units and percent of entry, including after exit.",
                  "2x cost is the existing evaluator's linear cost extrapolation with isolated-wallet floor."],
              **summarize(events, unconditional, baseline, symbols, horizons, params)}
    report = _render(report)
    report["report_hash"] = content_hash(report)
    report["created_utc"] = created_utc  # provenance, deliberately outside the content hash
    return report


def report_directory(report):
    return f"reports/doji/{report['segment']}__{report['report_hash'][:16]}"


def write_report(root, report, events):
    """Write once by content hash; keep original files and creation time on reruns."""
    directory = report_directory(report)
    dest = Path(root) / directory
    names = ("report.md", "events.csv", "summary.json")
    if dest.exists():
        if not all((dest / name).is_file() for name in names):
            raise ValueError("existing report is incomplete")
        stored = json.loads((dest / "summary.json").read_text(encoding="utf-8"))
        body = {key: value for key, value in stored.items() if key not in ("report_hash", "created_utc")}
        if stored.get("report_hash") != report["report_hash"] or content_hash(body) != report["report_hash"]:
            raise ValueError("existing report hash differs")
        return []
    dest.mkdir(parents=True, exist_ok=False)
    (dest / "report.md").write_text(markdown(report, events), encoding="utf-8")
    write_events(dest / "events.csv", [e for e in events if e["k"] == "1" and e["rr"] == "1"])
    (dest / "summary.json").write_bytes(canonical_bytes(report))
    return [f"{directory}/{name}" for name in names]


def public_lines(report, *, record_trials=True):
    """Allowlisted metadata and counts only. Never dump report dictionaries."""
    lines = [f"doji report {report['pattern_version']} segment={report['segment']} "
             f"labels={report['labels_version']} sha={report['code_commit']} k=1 rr=1",
             f"symbols={','.join(report['symbols'])} horizons={','.join(map(str, report['horizons']))}"]
    for cell in report["primary"]:
        lines.append(f"{cell['strategy']} h={cell['timeframe']} side={cell['side']} "
                     f"n_signals={cell['trade']['n_signals']} n_trades={cell['trade']['n_trades']} "
                     f"n_direction={cell['direction']['n']}")
    lines.append(f"private report path {report_directory(report)}/report.md")
    if not record_trials:
        lines.append("experiment ledger skipped")
    return lines


def write_events(path, events):
    with Path(path).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=EVENT_FIELDS)
        writer.writeheader()
        for event in sorted(events, key=lambda e: (e["decision_ms"], e["symbol"], e["strategy"],
                                                   e["timeframe"], e["side"], e["k"], e["rr"])):
            writer.writerow({key: ("true" if value else "false") if type(value) is bool else value
                             for key, value in event.items()})


def _show(value):
    return "n/a" if value is None else str(value)


def _rate(value):
    return "n/a" if value is None else f"{100 * float(value):.2f}%"


def _ci(values):
    return "n/a" if values is None else "–".join(_rate(v) for v in values)


def _mark(value):
    return "true" if value is True else "false" if value is False else _show(value)


def _table(rows, dimension=None):
    columns = ["strategy", "tf", "side"] + ([dimension] if dimension else []) + [
        "signals / trades / measured", "A direction n / ties", "A hit [Wilson95]", "A unconditional n / hit",
        "B T/S/E/L/X shares", "B net hit [Wilson95] (X loss)", "gross / net2 hit", "amb / X",
        "barrier base / T/(T+S)", "candle-aligned T-label net hit", "mean gross / net1 / net2 R", "non-trades", "n<100"]
    lines = ["| " + " | ".join(columns) + " |", "|" + " --- |" * len(columns)]
    for row in rows:
        a, b = row["direction"], row["trade"]
        base = a["unconditional"]
        values = [row["strategy"], str(row["timeframe"]), row["side"]]
        if dimension:
            values.append(str(row[dimension]))
        values += [f"{b['n_signals']} / {b['n_trades']} / {b['n_measured']}", f"{a['n']} / {a['ties']}",
                   f"{_rate(a['hit_rate'])} [{_ci(a['wilson_95'])}]", f"{base['n']} / {_rate(base['hit_rate'])}",
                   "/".join(_rate(b["outcome_shares"][code]) for code in OUTCOMES),
                   f"{_rate(b['net_hit_rate'])} [{_ci(b['net_wilson_95'])}]",
                   f"{_rate(b['gross_hit_rate'])} / {_rate(b['net_2x_hit_rate'])}",
                   f"{_rate(b['ambiguous_share'])} / {_rate(b['x_share'])}",
                   f"{_rate(b['barrier_base_rate'])} / {_rate(b['target_first_ts'])}",
                   _rate(b["unconditional_label_grid"]["net_hit_rate"]),
                   " / ".join(_show(b["mean_" + k]) for k in ("gross_r", "net_r_1x", "net_r_2x")),
                   ", ".join(f"{k}:{v}" for k, v in b["non_trades"].items()) or "none",
                   "yes" if row["small_n"] else ""]
        lines.append("| " + " | ".join(values) + " |")
    return lines


def markdown(report, events):
    lines = [f"# Doji report: {report['segment']}", "",
             f"Versions: {VERSION}; {report['labels_version']}; git `{report['code_commit']}`. "
             f"Headline k={report['k']}, rr={report['rr']}. Created {report['created_utc']}.", "",
             "Data release tags: " + ", ".join(report["data_release_tags"]), "",
             "Label release tags: " + ", ".join(report["label_release_tags"]), "",
             f"Snapshot `{report['data_snapshot_id']}`; report `{report['report_hash']}`.", "",
             *["- " + note for note in report["notes"]], "", "## Primary (pre-specified)", "",
             *_table(report["primary"]), "", "## Tier (descriptive)", "",
             *_table(report["descriptive"]["tier"], "tier"), "", "## Subtype (descriptive)", "",
             *_table(report["descriptive"]["subtype"], "subtype"), ""]
    headline = [e for e in events if e["k"] == "1" and e["rr"] == "1"]
    for tier in TIERS:
        lines += [f"## {tier}: deterministic event sample (descriptive)", "",
                  "Up to 25 nearest unique events to evenly spaced UTC times; no selection by outcome. "
                  "Prediction includes strategy, timeframe and side. Verdict order: direction / gross / net.", "",
                  "| time UTC | symbol | score | subtype | doji O/H/L/C | vol multiple | ADX | trend | prediction | "
                  "entry/stop/target | outcome | net R | verdict (X: unmeasured) |", "|" + " --- |" * 13]
        for e in sample_events([e for e in headline if e["tier"] == tier]):
            lines.append("| " + " | ".join([
                e["decision_time"], e["symbol"], f"{e['score']:.2f}", e["subtype"],
                "/".join(_show(e["doji_" + name]) for name in ("open", "high", "low", "close")),
                f"{e['rel_volume']:.3f}", f"{e['adx_prev']:.2f}", str(e["trend_dir"]),
                f"{e['strategy']} {e['timeframe']}m {e['side']}",
                "/".join(_show(e[key]) for key in ("entry_price", "stop", "target")),
                e["outcome"] or e["status"], _show(e["net_r_1x"]),
                " / ".join(_mark(e[key]) for key in ("direction_correct", "gross_verdict", "net_verdict"))]) + " |")
        lines.append("")
    for dimension in ("symbol", "adx_band"):
        lines += [f"## {dimension} (descriptive)", "", *_table(report["descriptive"][dimension], dimension), ""]
    lines += ["## Stop/target grid (descriptive)", "",
              "| strategy | tf | side | k | rr | n trades | gross hit | net1 hit | net2 hit | barrier base | "
              "candle-aligned T-label gross/net1 | mean gross/net1/net2 R |", "|" + " --- |" * 12]
    for row in report["grid"]:
        b = row["trade"]
        values = [row["strategy"], str(row["timeframe"]), row["side"], row["k"], row["rr"], str(b["n_trades"]),
                  *[_rate(b[key]) for key in ("gross_hit_rate", "net_hit_rate", "net_2x_hit_rate", "barrier_base_rate")],
                  " / ".join(_rate(b["unconditional_label_grid"][key]) for key in ("gross_hit_rate", "net_hit_rate")),
                  " / ".join(_show(b["mean_" + key]) for key in ("gross_r", "net_r_1x", "net_r_2x"))]
        lines.append("| " + " | ".join(values) + " |")
    lines += ["", "## MFE / MAE (descriptive)", "",
              "Quantiles are nearest-rank 50/75/90/95/99; excursions are nonnegative magnitudes. "
              "Leverage thresholds ignore maintenance margin and funding.", "",
              "| strategy | tf | side | n / unavailable | measure | q50/q75/q90/q95/q99 | MAE >=100/L%: L=3/5/10/20 |",
              "|" + " --- |" * 7]
    for row in report["excursions"]:
        for key, values in row["quantiles"].items():
            lines.append("| " + " | ".join([row["strategy"], str(row["timeframe"]), row["side"],
                f"{row['n']} / {row['unavailable']}", key, "/".join(_show(values[str(q)]) for q in QUANTILES),
                "/".join(_rate(row["leverage_mae_share"][str(n)]) for n in (3, 5, 10, 20))]) + " |")
    return "\n".join(lines) + "\n"
