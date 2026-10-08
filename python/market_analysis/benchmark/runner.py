"""Deterministic strategy x (k, reward target, horizon) benchmark experiments.

Symbols/directions belong to a strategy's signal universe, not extra variants.
Input geometries declare that universe. Both sides are loaded for baselines.
The single-writer ledger receives one append_many call with final verdicts;
joint SPA/StepM is computed first so no provisional trial records are needed.
DSR uses the counted question-wide trials after that append, including history.

The hidden segment is bound to its pre-registered plan: the token must belong
to this question, every evaluated (strategy_id, strategy_version) must be in
the plan and K must not exceed plan.variants, all checked before any label read
or ledger write. Bootstrap statistics (CI, SPA, StepM) use B_stats replicates;
the matched placebo uses B_placebo.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import json
from statistics import NormalDist

import numpy as np

from .baselines import (PoolRow, always_side, manifest_ticks, matched_placebo,
                        random_walk_comparison, trade_key, volatility_from_columns)
from .best_trial import best_trial_dsr
from .canonical import canonical_bytes, content_hash, exact_to_str
from .evaluate import bootstrap_ci, daily_series, decimal_text, metrics, net_at, select
from .experiment_log import TrialRecord, TrialStatus
from .hidden_guard import HiddenGuardError, HiddenStretchLocked, require_months
from .label_store import Geometry, load_geometry_columns
from .power import n_independent_greedy, power_gate
from .report import make_report, variant_details
from .rng import u64_words
from .segments import eligible, segment_bounds_ms, segment_months, worst_case_window_end_ms
from .spa import spa_test
from .stepm import stepm, stepm_p_values

ALPHA = Fraction(1, 20)
MIN_B_STATS_HIDDEN = 1000
MIN_B_PLACEBO = 99


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


def required_t(n_trials: int) -> float:
    """max(3, z) with z = Phi^-1(1 - alpha / (2 n)): Bonferroni over n trials, two-sided."""
    if type(n_trials) is not int or n_trials < 1:
        raise ValueError("n_trials must be a positive integer")
    return max(3.0, NormalDist().inv_cdf(1 - float(ALPHA) / (2 * n_trials)))


def verdict(selection, power, ci, rejected: bool, *, p_placebo, required_t) -> str:
    """NOT_ENOUGH_EVIDENCE > FRAGILE > PASS > FAIL; financial comparisons are exact.

    p_placebo is the matched placebo's p at 1x cost (None when undefined);
    required_t is the multiplicity-adjusted threshold (float or its repr).
    """
    if not power["passes"]:
        return "NOT_ENOUGH_EVIDENCE"
    usable = [row for row in selection if row.outcome != "X"]
    one = sum(net_at(row) for row in usable)
    two = sum(net_at(row, 2) for row in usable)
    opt = sum(net_at(row, policy="opt") for row in usable)
    n = len(selection)
    if ((one > 0 and two <= 0) or one * opt < 0
            or (n and 10 * sum(row.amb for row in selection) > n)
            or (n and Fraction(n - len(usable), n) > ALPHA)):
        return "FRAGILE"
    if (rejected and ci["lower"] is not None and Fraction(ci["lower"]) > 0
            and ci["t_statistic"] is not None and Fraction(ci["t_statistic"]) >= Fraction(required_t)
            and two > 0 and p_placebo is not None and Fraction(p_placebo) <= ALPHA):
        return "PASS"
    return "FAIL"


def run_experiment(question_id, specs, geometries, segment, label_dir, params, log, *, token=None, gate=None,
                   B_stats: int = 2000, B_placebo: int = 200, seed, code_commit, data_snapshot_id,
                   now_utc) -> dict:
    """Run all variants and return a hashed report-v1 record; no wall-clock reads.

    Existing successful identities append REPLAY lines via ExperimentLog and do
    not increase counted N. Thus identical inputs/history counts yield identical
    reports, even when the audit ledger records a repeated invocation.
    """
    if type(B_stats) is not int or B_stats < 2:
        raise ValueError("B_stats must be at least 2")
    if segment == "hidden" and B_stats < MIN_B_STATS_HIDDEN:
        raise ValueError(f"B_stats must be at least {MIN_B_STATS_HIDDEN} on the hidden segment")
    if type(B_placebo) is not int or B_placebo < MIN_B_PLACEBO:
        raise ValueError(f"B_placebo must be at least {MIN_B_PLACEBO}")
    months = segment_months(segment)
    require_months(months, token, gate)  # before metadata reads or ledger mutation
    plan = None
    if segment == "hidden":
        if token is None or token.question_id != question_id:
            raise HiddenStretchLocked("hidden token was opened for another question")
        plan = gate.plan_for(token)
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
    K = len(templates)
    if plan is not None:
        allowed = {(item["strategy_id"], item["strategy_version"]) for item in plan.strategies}
        for spec in specs:
            if (spec.strategy_id, spec.strategy_version) not in allowed:
                raise HiddenGuardError(f"strategy {spec.strategy_id}/{spec.strategy_version} is not in the plan")
        if K > plan.variants:
            raise HiddenGuardError(f"{K} variants exceed the plan's pre-registered {plan.variants}")
    # Question-wide N after this run's append: history plus this run's identities
    # (replayed identities are already in the history). Known before any verdict.
    n_trials = max(K, len(set(log.counted_trial_ids(question_id=question_id))
                          | {trial.trial_id for trial, _, _ in templates}))
    # Hidden: the pre-registered finalists are the family; history is the search.
    threshold = required_t(K if plan is not None else n_trials)
    # Add both directions solely for baselines, without adding statistical trials.
    expanded = sorted({g._replace(side=side) for g in geometries for side in (-1, 1)})
    columns, volatility = {}, {}
    for symbol in sorted({g.symbol for g in expanded}):
        requested = [g for g in expanded if g.symbol == symbol]
        ticks = manifest_ticks(label_dir, symbol, months, params)  # cheap check before parsing labels
        loaded = load_geometry_columns(label_dir, symbol, months, requested, params,
                                       token=token, gate=gate, segment=segment, metadata=True)
        columns.update(loaded)
        volatility.update(volatility_from_columns(loaded, ticks))
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
        ci = bootstrap_ci(daily, B=B_stats, seed=seed, stream_prefix=prefix + "/ci")
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
                        "required_t": repr(threshold),
                        "baselines": {"random_walk": random_walk_comparison(selected, params.rr_grid[variant.rr_index]),
                                      "always_long": long_metrics, "always_short": short_metrics,
                                      "placebo": matched_placebo(rows, pool, B=B_placebo, seed=seed,
                                                                 stream_prefix=prefix + "/placebo")}})
        entries.append(details)
        selections.append(selected)
        daily_columns.append(daily)
    matrix = np.column_stack([np.asarray(daily, dtype=np.int64) for daily in daily_columns])
    joint_stream = "benchmark/" + content_hash([question_id, [item["variant_id"] for item in entries]]) + "/joint"
    spa = spa_test(matrix, B=B_stats, seed=seed, stream_prefix=joint_stream)
    step = stepm(matrix, B=B_stats, seed=seed, stream_prefix=joint_stream)
    adjusted = [decimal_text(value) for value in
                stepm_p_values(matrix, B=B_stats, seed=seed, stream_prefix=joint_stream)]
    records = []
    for index, (entry, selected, (trial, _, _)) in enumerate(zip(entries, selections, templates)):
        p_placebo = entry["baselines"]["placebo"]["p_placebo"]
        entry["verdict"] = verdict(selected, entry["power"], entry["bootstrap_ci"], index in step.rejected,
                                   p_placebo=p_placebo, required_t=threshold)
        entry["stepm_p_value"] = adjusted[index]
        entry["stepm_rejected"] = index in step.rejected
        entry["spa_p_value"] = repr(spa.p_consistent)
        summary = {"n_trades": len(selected), "mean_net_r": entry["metrics"]["mean_net_r"] or "undefined",
                   "t_stat": entry["bootstrap_ci"]["t_statistic"] or "undefined",
                   "required_t": repr(threshold), "p_placebo": p_placebo or "undefined",
                   "B_stats": B_stats, "B_placebo": B_placebo, "verdict": entry["verdict"]}
        records.append(replace(trial, result_summary=summary, result_hash=content_hash(entry)))
    log.append_many(records)
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
                        "plan_id": plan.plan_id if plan is not None else None,
                        "code_commit": code_commit, "data_snapshot_id": data_snapshot_id,
                        "params_identity": params.identity(), "cost_model_version": params.cost_model.version,
                        "cost_model_identity": params.cost_model.identity(), "B_stats": B_stats,
                        "B_placebo": B_placebo, "seed": seed, "n_trials": n_trials, "K": K,
                        "required_t": repr(threshold), "variants": entries,
                        "spa": spa.to_record(), "stepm": {**step.to_record(), "adjusted_p_values": adjusted},
                        "best_trial_dsr": dsr})
