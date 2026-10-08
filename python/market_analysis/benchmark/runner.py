"""Deterministic strategy x (k, reward target, horizon) benchmark experiments.

Symbols/directions belong to a strategy's signal universe, not extra variants.
Input geometries declare that universe. Both sides are loaded for baselines.
The single-writer ledger receives one append_many call with final verdicts;
joint SPA/StepM is computed first so no provisional trial records are needed.
DSR uses the counted question-wide trials after that append, including history.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import json
from math import sqrt

import numpy as np

from .baselines import (PoolRow, always_side, load_volatility, matched_placebo,
                        random_walk_comparison, trade_key)
from .best_trial import best_trial_dsr
from .canonical import canonical_bytes, content_hash, exact_to_str
from .evaluate import bootstrap_ci, daily_series, decimal_text, metrics, net_at, select
from .experiment_log import TrialRecord, TrialStatus
from .hidden_guard import require_months
from .label_store import Geometry, load_geometry_columns
from .power import n_independent_greedy, power_gate
from .report import make_report, variant_details
from .rng import u64_words
from .segments import eligible, segment_bounds_ms, segment_months, worst_case_window_end_ms
from .spa import _studentize, spa_test
from .stepm import stepm


@dataclass(frozen=True)
class StrategySpec:
    strategy_id: str
    strategy_version: str
    config: dict
    signals: tuple

    def __post_init__(self):
        for value in (self.strategy_id, self.strategy_version):
            if type(value) is not str or not value:
                raise ValueError("strategy id/version must be non-empty strings")
        if type(self.config) is not dict:
            raise ValueError("strategy config must be a dict")
        object.__setattr__(self, "config", json.loads(canonical_bytes(self.config)))
        signals = tuple(tuple(row) for row in self.signals)
        for row in signals:
            if (len(row) != 4 or type(row[0]) is not str or type(row[1]) is not int
                    or row[1] < 0 or type(row[2]) is not int or row[2] not in (-1, 1)
                    or type(row[3]) is not int or row[3] <= 0):
                raise ValueError("signals must be (symbol, integer milliseconds, side, horizon)")
        if len(set(signals)) != len(signals):
            raise ValueError("duplicate strategy signal")
        # Canonical tuple order makes stream identities independent of iterable order.
        object.__setattr__(self, "signals", tuple(sorted(signals)))


@dataclass(frozen=True)
class Variant:
    strategy: StrategySpec
    k: Fraction
    rr_index: int
    horizon_min: int

    def config(self, universe, params) -> dict:
        return {"strategy": self.strategy.config, "k": exact_to_str(self.k), "rr_index": self.rr_index,
                "horizon_min": self.horizon_min, "universe": [list(pair) for pair in sorted(universe)],
                "params_identity": params.identity(), "signals_hash": content_hash(self.strategy.signals)}


def _stepm_p_values(matrix, *, B, seed, stream_prefix) -> list[str]:
    """Adjusted upper-tail step-down p-values using StepM's exact same draws.

    Inclusive >= handles ties consistently with strict t > critical rejection.
    Tied observed t-statistics share one step. Degenerate columns have p = 1.
    Decisions remain those returned by the existing stepm() implementation.
    """
    stats = _studentize(matrix, None, B, None, seed, stream_prefix)
    centred = np.zeros_like(stats.dstar)
    centred[:, stats.valid] = (sqrt(stats.T) * (stats.dstar[:, stats.valid] - stats.dbar[stats.valid])
                              / stats.omega[stats.valid])
    active = [i for i in range(stats.K) if stats.valid[i]]
    result, previous = [Fraction(1)] * stats.K, Fraction(0)
    while active:
        observed = max(float(stats.t[i]) for i in active)
        tied = [i for i in active if float(stats.t[i]) == observed]
        maxima = centred[:, active].max(axis=1)
        value = max(previous, Fraction(int(np.count_nonzero(maxima >= observed)), B))
        for i in tied:
            result[i] = value
        previous = value
        active = [i for i in active if i not in tied]
    return [decimal_text(value) for value in result]


def verdict(selection, power, ci, rejected: bool) -> str:
    """Power first, fragility second; all financial comparisons are exact."""
    if not power["passes"]:
        return "NOT_ENOUGH_EVIDENCE"
    usable = [row for row in selection if row.outcome != "X"]
    one = sum(net_at(row) for row in usable)
    two = sum(net_at(row, 2) for row in usable)
    opt = sum(net_at(row, policy="opt") for row in usable)
    if ((one > 0 and two <= 0) or one * opt < 0
            or (len(selection) and 10 * sum(row.amb for row in selection) > len(selection))):
        return "FRAGILE"
    if (rejected and ci["lower"] is not None and Fraction(ci["lower"]) > 0
            and ci["t_statistic"] is not None and Fraction(ci["t_statistic"]) >= 3 and two > 0):
        return "PASS"
    return "FAIL"


def run_experiment(question_id, specs, geometries, segment, label_dir, params, log, *, token=None, gate=None,
                   B, seed, code_commit, data_snapshot_id, now_utc) -> dict:
    """Run all variants and return a hashed report-v1 record; no wall-clock reads.

    Existing successful identities append REPLAY lines via ExperimentLog and do
    not increase counted N. Thus identical inputs/history counts yield identical
    reports, even when the audit ledger records a repeated invocation.
    """
    months = segment_months(segment)
    require_months(months, token, gate)  # before metadata reads or ledger mutation
    if type(B) is not int or B < 2:
        raise ValueError("B must be at least 2")
    u64_words(seed, "runner", 0, 0)
    specs = list(specs)
    geometries = [Geometry(*value) for value in geometries]
    if not specs or not geometries or len(set(geometries)) != len(geometries):
        raise ValueError("need strategies and distinct geometries")
    groups = {}
    for geometry in geometries:
        groups.setdefault((geometry.horizon_min, Fraction(geometry.k), geometry.rr_index), []).append(geometry)
    universe = {(g.symbol, g.side, g.horizon_min) for g in geometries}
    for spec in specs:
        if not isinstance(spec, StrategySpec):
            raise ValueError("specs must contain StrategySpec values")
        if any((symbol, side, horizon) not in universe for symbol, _, side, horizon in spec.signals):
            raise ValueError("strategy signal outside the declared geometry universe")
    templates = []
    for spec in specs:
        for (horizon, k, rr_index), group in sorted(groups.items()):
            variant = Variant(spec, k, rr_index, horizon)
            config = variant.config({(g.symbol, g.side) for g in group}, params)
            trial = TrialRecord(question_id=question_id, family_id=spec.strategy_id,
                                strategy_id=spec.strategy_id, strategy_version=spec.strategy_version, config=config,
                                split_id=segment, data_snapshot_id=data_snapshot_id, code_commit=code_commit,
                                status=TrialStatus.OK, counts_toward_n=True, count_reason="variant evaluated",
                                result_hash=None, result_summary={}, created_utc=now_utc)
            templates.append((trial, variant, group))
    templates.sort(key=lambda item: item[0].trial_id)
    if len({trial.trial_id for trial, _, _ in templates}) != len(templates):
        raise ValueError("duplicate strategy/geometry variant")
    # Add both directions solely for baselines, without adding statistical trials.
    expanded = sorted({g._replace(side=side) for g in geometries for side in (-1, 1)})
    columns, volatility = {}, {}
    for symbol in sorted({g.symbol for g in expanded}):
        requested = [g for g in expanded if g.symbol == symbol]
        columns.update(load_geometry_columns(label_dir, symbol, months, requested, params,
                                            token=token, gate=gate, segment=segment))
        volatility.update(load_volatility(label_dir, symbol, months, requested, params, segment,
                                          token=token, gate=gate))
    first, end = segment_bounds_ms(segment)
    entries, selections, daily_columns = [], [], []
    baseline_cache = {}
    for trial, variant, group in templates:
        selected_columns = {g: columns[g] for g in group}
        signals, purged = [], 0
        membership = {(g.symbol, g.side) for g in group}
        for symbol, ms, side, horizon in variant.strategy.signals:
            if horizon != variant.horizon_min or (symbol, side) not in membership or not first <= ms < end:
                continue
            # Also purge off-grid boundary signals: never offer those for on-demand labelling.
            if eligible(segment, ms, worst_case_window_end_ms(ms, horizon, params.time_limit_multiple)):
                signals.append((symbol, ms, side, horizon))
            else:
                purged += 1
        selected = select(selected_columns, signals)
        selected.purged += purged
        selected.signals += purged
        daily = daily_series(selected, segment)
        prefix = f"benchmark/{trial.trial_id}"
        ci = bootstrap_ci(daily, B=B, seed=seed, stream_prefix=prefix + "/ci")
        power = power_gate(daily, Fraction(len(selected), len(daily)))
        cache_key = variant.horizon_min, variant.k, variant.rr_index
        if cache_key not in baseline_cache:
            symbols = {g.symbol for g in group}
            baseline_columns = {g: column for g, column in columns.items() if g.symbol in symbols
                                and (g.horizon_min, g.k, g.rr_index) == cache_key}
            longs, shorts = always_side(baseline_columns, 1), always_side(baseline_columns, -1)
            pool = [PoolRow(row, volatility[trade_key(row)]) for row in (*longs, *shorts)]
            baseline_cache[cache_key] = (pool, metrics(longs, daily_series(longs, segment)),
                                       metrics(shorts, daily_series(shorts, segment)))
        pool, long_metrics, short_metrics = baseline_cache[cache_key]
        rows = [PoolRow(row, volatility[trade_key(row)]) for row in selected]
        details = variant_details(selected, daily, rows, pool, segment)
        details.update({"variant_id": trial.trial_id, "strategy_id": trial.strategy_id,
                        "strategy_version": trial.strategy_version, "config": trial.config,
                        "geometry": {"k": decimal_text(variant.k), "rr_index": variant.rr_index,
                                     "rr": decimal_text(params.rr_grid[variant.rr_index]), "horizon_min": variant.horizon_min},
                        "power": power, "bootstrap_ci": ci, "n_independent_greedy": n_independent_greedy(selected),
                        "baselines": {"random_walk": random_walk_comparison(selected, params.rr_grid[variant.rr_index]),
                                      "always_long": long_metrics, "always_short": short_metrics,
                                      "placebo": matched_placebo(rows, pool, B=B, seed=seed, stream_prefix=prefix + "/placebo")}})
        entries.append(details)
        selections.append(selected)
        daily_columns.append(daily)
    matrix = np.column_stack([np.asarray(daily, dtype=np.int64) for daily in daily_columns])
    joint_stream = "benchmark/" + content_hash([question_id, [item["variant_id"] for item in entries]]) + "/joint"
    spa = spa_test(matrix, B=B, seed=seed, stream_prefix=joint_stream)
    step = stepm(matrix, B=B, seed=seed, stream_prefix=joint_stream)
    adjusted = _stepm_p_values(matrix, B=B, seed=seed, stream_prefix=joint_stream)
    records = []
    for index, (entry, selected, (trial, _, _)) in enumerate(zip(entries, selections, templates)):
        entry["verdict"] = verdict(selected, entry["power"], entry["bootstrap_ci"], index in step.rejected)
        entry["stepm_p_value"] = adjusted[index]
        entry["stepm_rejected"] = index in step.rejected
        entry["spa_p_value"] = repr(spa.p_consistent)
        summary = {"n_trades": len(selected), "mean_net_r": entry["metrics"]["mean_net_r"] or "undefined",
                   "t_stat": entry["bootstrap_ci"]["t_statistic"] or "undefined", "verdict": entry["verdict"]}
        records.append(replace(trial, result_summary=summary, result_hash=content_hash(entry)))
    log.append_many(records)
    K = len(entries)
    n_trials = max(K, log.trial_count(question_id=question_id))
    # The existing DSR estimator requires >=2 non-constant columns. Report this
    # limitation explicitly rather than invent a Sharpe variance for a singleton.
    nonconstant = sum(any(value != daily[0] for value in daily) for daily in daily_columns)
    if nonconstant >= 2:
        dsr = best_trial_dsr(matrix, n_trials=n_trials).to_record()
    else:
        dsr = {"best_index": None, "n_trials": n_trials, "dsr_raw": None, "dsr_effective": None,
               "reason": "need at least 2 non-constant trials"}
    for index, entry in enumerate(entries):
        entry["n_trials"] = n_trials
        is_best = dsr["best_index"] == index
        entry["dsr"] = {"scope": "best trial only", "is_best": is_best,
                        "raw": dsr["dsr_raw"] if is_best else None,
                        "effective": dsr["dsr_effective"] if is_best else None}
    return make_report({"question_id": question_id, "segment": segment, "created_utc": now_utc,
                        "code_commit": code_commit, "data_snapshot_id": data_snapshot_id,
                        "params_identity": params.identity(), "cost_model_version": params.cost_model.version,
                        "cost_model_identity": params.cost_model.identity(), "B": B, "seed": seed,
                        "n_trials": n_trials, "K": K, "variants": entries,
                        "spa": spa.to_record(), "stepm": {**step.to_record(), "adjusted_p_values": adjusted},
                        "best_trial_dsr": dsr})
