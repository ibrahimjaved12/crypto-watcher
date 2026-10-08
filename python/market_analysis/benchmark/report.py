"""Canonical report-v1 and Markdown; no individual trade or USDT amounts.

report_hash covers every other JSON field. Rounding/undefined-value conventions
are inherited from C1. Breakdowns describe selected trades (including X counts),
not non-trade grid rows. DSR belongs to the experiment's best trial only.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from fractions import Fraction
import json

from .baselines import tercile_boundaries, volatility_tercile
from .canonical import canonical_bytes, content_hash
from .evaluate import daily_series, decimal_text, metrics
from .scan import UR


def _share(count, total):
    return decimal_text(Fraction(count, total)) if total else None


def variant_details(selection, daily, rows, pool, segment) -> dict:
    measured = [row for row in selection if row.outcome != "X"]
    stats = metrics(selection, daily)
    gross = (Fraction(sum(row.net_ur + row.cost_ur - row.fund_ur for row in measured), len(measured) * UR)
             if measured else None)
    grid = {str(m): {"net_mean_r": stats["cost_grid_mean_net_r"][str(m)],
                     "gross_mean_r": decimal_text(gross), "funding_mean_r": stats["mean_funding_r"]}
            for m in range(4)}
    boundaries = tercile_boundaries(pool)
    groups = {name: defaultdict(list) for name in (
        "direction", "symbol", "horizon", "quarter", "weekday_weekend", "volatility_tercile", "funding_sign")}
    for row in rows:
        trade = row.trade
        utc = datetime.fromtimestamp(trade.signal_ms // 1000, timezone.utc)
        key = trade.symbol, trade.geometry.horizon_min, trade.geometry.k
        labels = {
            "direction": "long" if trade.geometry.side == 1 else "short", "symbol": trade.symbol,
            "horizon": str(trade.geometry.horizon_min), "quarter": f"{utc.year}-Q{(utc.month - 1) // 3 + 1}",
            "weekday_weekend": "weekend" if utc.weekday() >= 5 else "weekday",
            "volatility_tercile": str(volatility_tercile(row, boundaries)) if key in boundaries else "unknown",
            "funding_sign": "unknown" if trade.outcome == "X" else "positive" if trade.fund_ur > 0
                            else "negative" if trade.fund_ur < 0 else "zero",
        }
        for dimension, label in labels.items():
            groups[dimension][label].append(trade)
    breakdowns = {}
    for dimension, buckets in groups.items():
        breakdowns[dimension] = {}
        for label, trades in sorted(buckets.items()):
            bucket = metrics(trades, daily_series(trades, segment))
            breakdowns[dimension][label] = {name: bucket[name] for name in (
                "trades", "usable_trades", "X_count", "mean_net_r", "win_rate", "ambiguity_share",
                "cost_grid_mean_net_r", "mean_funding_r", "outcome_shares")}
    return {"metrics": stats, "coverage": {
                "grid_share": _share(selection.signals - selection.not_in_grid - selection.purged, selection.signals),
                "trade_share": _share(len(selection), selection.signals),
                "measured_share": _share(len(measured), selection.signals)},
            "cost_grid": grid, "breakdowns": breakdowns}


def headline(body: dict) -> dict:
    """Decision summary derived from the full variant entries (never set independently)."""
    rows = []
    for variant in body["variants"]:
        stats = variant["metrics"]
        rows.append({
            "variant_id": variant["variant_id"], "strategy_id": variant["strategy_id"],
            "verdict": variant["verdict"], "trades": stats["trades"],
            "mean_net_r_1x": stats["cost_grid_mean_net_r"]["1"], "mean_net_r_2x": stats["cost_grid_mean_net_r"]["2"],
            "t_statistic": variant["bootstrap_ci"].get("t_statistic"), "required_t": variant.get("required_t"),
            "p_placebo": variant["baselines"]["placebo"]["p_placebo"],
            "x_share": stats["outcome_shares"]["X"], "ambiguity_share": stats["ambiguity_share"],
            "stepm_p_value": variant["stepm_p_value"], "power_passes": variant["power"]["passes"]})
    return {"plan_id": body.get("plan_id"), "B_stats": body.get("B_stats"), "B_placebo": body.get("B_placebo"),
            "required_t": body.get("required_t"), "variants": rows}


def make_report(experiment: dict) -> dict:
    body = {"schema": "report-v1", **experiment}
    if body["schema"] != "report-v1" or "report_hash" in body or "headline" in body:
        raise ValueError("unexpected report schema/hash/headline")
    body["headline"] = headline(body)
    body["caveats"] = ["Survivorship: today's six majors only.",
                       "score is a ranking, not a probability",
                       f"Cost model version: {body['cost_model_version']}",
                       f"Params identity: {body['params_identity']}"]
    # Check/copy through canonical JSON: floats are rejected, caller mutation
    # cannot change a report after its content-derived hash has been assigned.
    body = json.loads(canonical_bytes(body))
    return {**body, "report_hash": content_hash(body)}


def report_hash(report: dict) -> str:
    return content_hash({key: value for key, value in report.items() if key != "report_hash"})


def canonical_json(report: dict) -> bytes:
    if report.get("report_hash") != report_hash(report):
        raise ValueError("report_hash mismatch")
    return canonical_bytes(report)


def _escape(value) -> str:
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(
        ">", "&gt;").replace("|", "&#124;").replace("\n", " ").replace("\r", " ").replace("`", "&#96;")


def markdown(report: dict) -> str:
    """Readable variant/baseline tables plus full deterministic evidence details."""
    canonical_json(report)
    lines = [f"# Benchmark {_escape(report['question_id'])}", "",
             f"Segment: {_escape(report['segment'])}. Variants tried: {report['n_trials']}.", "",
             f"Report hash: `{report['report_hash']}`", "",
             "All returns are R units. Daily P&L is attributed to the entry day.", ""]
    top = report["headline"]
    lines += [f"Plan: {_escape(top['plan_id'] or 'none (not hidden)')}. B_stats: {_escape(top['B_stats'])}. "
              f"B_placebo: {_escape(top['B_placebo'])}. Required t: {_escape(top['required_t'])}.", "",
              "| Variant | Trades | Mean net R 1x | Mean net R 2x | t | Required t | Placebo p | X share "
              "| Ambiguity share | StepM p | Power gate | Verdict |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
    for row in top["variants"]:
        lines.append("| " + " | ".join(_escape(value) for value in (
            row["variant_id"], row["trades"], row["mean_net_r_1x"], row["mean_net_r_2x"], row["t_statistic"],
            row["required_t"], row["p_placebo"], row["x_share"], row["ambiguity_share"], row["stepm_p_value"],
            "pass" if row["power_passes"] else "fail", row["verdict"])) + " |")
    for variant in report["variants"]:
        lines += ["", f"## {_escape(variant['strategy_id'])} / {_escape(variant['variant_id'])}", "",
                  "| Baseline | Mean net R / rate | Detail |", "| --- | ---: | --- |"]
        baselines = variant["baselines"]
        rw = baselines["random_walk"]
        lines.append(f"| Random walk target rate | {_escape(rw['expected_target_share'])} | "
                     f"Observed among T/S: {_escape(rw['observed_target_share'])} |")
        for name in ("always_long", "always_short"):
            item = baselines[name]
            lines.append(f"| {name} | {_escape(item['mean_net_r'])} | Trades: {item['trades']} |")
        placebo = baselines["placebo"]
        lines.append(f"| Matched placebo median | {_escape(placebo['quantiles']['50'])} | "
                     f"p: {_escape(placebo['p_placebo'])}; fallbacks: {placebo['fallback_count']} |")
        # Every count, reason, ratio, cost grid, quantile and breakdown is exposed,
        # not silently omitted from Markdown when the JSON schema expands.
        for name in ("metrics", "coverage", "cost_grid", "power", "bootstrap_ci", "baselines", "breakdowns", "dsr"):
            encoded = json.dumps(variant[name], sort_keys=True, indent=2, ensure_ascii=True)
            lines += ["", f"### {name}", "", "```json", encoded, "```"]
    for name in ("spa", "stepm", "best_trial_dsr"):
        lines += ["", f"## {name}", "", "```json", json.dumps(report[name], sort_keys=True, indent=2), "```"]
    lines += ["", *report["caveats"], ""]
    return "\n".join(lines)
