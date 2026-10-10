"""Forward vs label-engine parity on real bars (comparison logic of ``forward-parity.yml``).

The forward harness and the label engine must agree row for row. ``test_forward_engine`` proves it on
synthetic bars; this module compares them on a real month: for every forward setup it looks up the label
engine's row for the same decision (``labels.row_factory``, full-history bars, expanding hcal calibration)
and records what differs. Pure functions over plain dicts so the rule is unit-testable without data.

Pass rule (pre-declared in ``.github/workflows/forward-parity.yml`` before any run; a failed rule is a
finding, never something to fix by loosening the rule):
  (i)   0 unmatched forward setups;
  (ii)  among setups with identical geometry (same tick, stop and target in ticks), 100 % identical status,
        exit offset, exit time and net R;
  (iii) >= 95 % of setups have identical geometry OR |delta sigma| / sigma <= 3 %;
  (iv)  median |delta c_h| / c_h <= 3 %.
"""
from __future__ import annotations

from collections import Counter
from math import ceil

from ..benchmark.canonical import exact_from_str, exact_to_str
from .bars_adapter import MINUTE_MS

PASS_RULE = {"unmatched_max": 0, "identical_geometry_status_exit_share_min": 1.0,
             "geometry_or_sigma_share_min": 0.95, "sigma_tolerance": 0.03, "median_c_h_tolerance": 0.03}
SCALE = 10 ** 8


def rows_from_bars(bars, first_ms: int, end_ms: int) -> list[dict]:
    """Collector-style 1-minute rows for [first_ms, end_ms) from lake bars (missing minutes are skipped,
    exactly as a collector gap is). Prices are floats whose shortest repr is the 10**8 integer."""
    rows = []
    first = max(0, (first_ms - bars.start_ms) // MINUTE_MS)
    last = min(bars.minutes, (end_ms - bars.start_ms) // MINUTE_MS)
    for i in range(first, last):
        if bars.open[i] < 0 or bars.high[i] < 0 or bars.low[i] < 0 or bars.close[i] < 0:
            continue
        open_ms = bars.start_ms + i * MINUTE_MS
        rows.append({"open_time_ms": open_ms, "close_time_ms": open_ms + MINUTE_MS - 1,
                     "open": bars.open[i] / SCALE, "high": bars.high[i] / SCALE, "low": bars.low[i] / SCALE,
                     "close": bars.close[i] / SCALE, "volume": max(0, bars.volume[i]) / SCALE,
                     "transport": "rest", "source_event_at_ms": None})
    return rows


def funding_events(funding) -> list[dict]:
    return [{"calc_time_ms": t, "interval_hours": h, "rate": str(r)}
            for t, h, r in zip(funding.calc_time_ms, funding.interval_hours, funding.rate)]


def _rel(forward: int, reference: int) -> float:
    return abs(forward - reference) / abs(reference) if reference else (0.0 if forward == reference else float("inf"))


def quantile(values: list, q: float):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, ceil(q * len(ordered)) - 1))
    return ordered[index]


def median(values: list):
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2


def compare(setups: list[dict], resolutions: list[dict], reference, level_of, multiplier_of, tick_of, params) -> dict:
    """Join every forward setup to its label row and measure the differences.

    ``reference(signal_ms, horizon, side, k) -> LabelRow``; ``level_of(horizon, signal_ms)`` and
    ``multiplier_of(horizon, signal_ms)`` give the reference robust level and c_h (HCAL_SCALE fixed point);
    ``tick_of(entry_ms)`` is the label engine's (monthly) tick at an entry.
    """
    by_setup = {item["setup_id"]: item for item in resolutions}
    rr_index = {exact_to_str(rr): index for index, rr in enumerate(params.rr_grid)}
    cache: dict = {}
    unmatched, status_pairs = [], Counter()
    rel_sigma, rel_ch, rel_level = [], [], []
    identical = identical_ok = sigma_ok_or_identical = 0
    resolution_diffs = []
    different = []  # (rel sigma, cause)
    comparable_sigma = 0
    for setup in setups:
        key = (setup["signal_ms"], setup["horizon_min"], setup["side"], setup["k"])
        grid = params.step(setup["horizon_min"]) * MINUTE_MS
        if setup["signal_ms"] % grid or setup["rr"] not in rr_index:
            unmatched.append(key + (setup["rr"],))
            continue
        if key not in cache:
            cache[key] = reference(setup["signal_ms"], setup["horizon_min"], setup["side"], exact_from_str(setup["k"]))
        ref = cache[key]
        status_pairs[(setup["status"], ref.status)] += 1
        if not (setup["status"] == "T" and ref.status == "T"):
            continue
        comparable_sigma += 1
        sigma_f, sigma_r = int(setup["sigma"]), ref.sigma
        d_f, d_r = setup["d_ticks"], ref.d_ticks
        rr = params.rr_grid[rr_index[setup["rr"]]]
        target_r = -(-rr.numerator * d_r // rr.denominator)
        tick_f = setup["tick"]
        target_f = abs(setup["target"] - setup["p0"]) // tick_f
        # Geometry is compared in ticks and on the entry price; a different tick (the label engine uses
        # the month's tick, the forward request the window's) shows up as different stop/target prices.
        geometry_same = (d_f == d_r and target_f == target_r and setup["p0"] == ref.p0
                         and tick_f == tick_of(setup["entry_ms"]))
        rs = _rel(sigma_f, sigma_r)
        rel_sigma.append(rs)
        level_f = int(setup["var"]) if setup.get("var") is not None else None
        c_f = int(setup["factor_weight"]) if setup.get("factor_weight") is not None else None
        level_r, c_r = level_of(setup["horizon_min"], setup["signal_ms"]), multiplier_of(setup["horizon_min"], setup["signal_ms"])
        rl = _rel(level_f, level_r) if level_f is not None and level_r else None
        rc = _rel(c_f, c_r) if c_f is not None and c_r else None
        if rl is not None:
            rel_level.append(rl)
        if rc is not None:
            rel_ch.append(rc)
        if geometry_same:
            identical += 1
            sigma_ok_or_identical += 1
            resolution = by_setup.get(setup["setup_id"])
            pair = ref.cells[rr_index[setup["rr"]]]
            expected_status = "ambiguous" if pair.opt else pair.pess.outcome
            ok = (resolution is not None and resolution["status"] == expected_status
                  and resolution.get("exit_offset") == pair.pess.exit_offset
                  and resolution.get("exit_ms") == setup["entry_ms"] + pair.pess.exit_offset * MINUTE_MS
                  and resolution.get("net_ur") == pair.pess.net_ur)
            identical_ok += bool(ok)
            if not ok:
                resolution_diffs.append((setup["setup_id"][:12], setup["signal_ms"], setup["rr"]))
        else:
            if rs <= PASS_RULE["sigma_tolerance"]:
                sigma_ok_or_identical += 1
            c_bad, l_bad = rc is not None and rc > 1e-9, rl is not None and rl > 1e-9
            cause = "both" if c_bad and l_bad else "c_h" if c_bad else "level" if l_bad else "tick_or_rounding"
            different.append((rs, cause))
    n = len(setups)
    stats = {
        "setups": n, "unmatched": len(unmatched), "unmatched_first_keys": unmatched[:5],
        "status_pairs": {f"{a}/{b}": c for (a, b), c in sorted(status_pairs.items())},
        "both_tradeable": comparable_sigma,
        "identical_geometry": identical,
        "identical_geometry_share": identical / n if n else None,
        "identical_geometry_status_exit_net_ok": identical_ok,
        "identical_geometry_status_exit_net_share": identical_ok / identical if identical else None,
        "resolution_mismatches_first": resolution_diffs[:5],
        "geometry_or_sigma_within_tolerance": sigma_ok_or_identical,
        "geometry_or_sigma_share": sigma_ok_or_identical / n if n else None,
        "abs_rel_delta_sigma": {"median": median(rel_sigma), "p95": quantile(rel_sigma, 0.95)},
        "abs_rel_delta_c_h": {"median": median(rel_ch), "p95": quantile(rel_ch, 0.95), "n": len(rel_ch)},
        "abs_rel_delta_level": {"median": median(rel_level), "p95": quantile(rel_level, 0.95), "n": len(rel_level)},
        "different_geometry": {"count": len(different), "causes": dict(Counter(c for _, c in different)),
                               "abs_rel_delta_sigma": {"median": median([r for r, _ in different]),
                                                       "p95": quantile([r for r, _ in different], 0.95),
                                                       "max": max((r for r, _ in different), default=None)}},
    }
    stats["verdict"] = verdict(stats)
    return stats


def verdict(stats: dict) -> dict:
    n = stats["setups"]
    rule_i = n > 0 and stats["unmatched"] <= PASS_RULE["unmatched_max"]
    share_ii = stats["identical_geometry_status_exit_net_share"]
    rule_ii = share_ii is None or share_ii >= PASS_RULE["identical_geometry_status_exit_share_min"]
    share_iii = stats["geometry_or_sigma_share"]
    rule_iii = share_iii is not None and share_iii >= PASS_RULE["geometry_or_sigma_share_min"]
    median_c = stats["abs_rel_delta_c_h"]["median"]
    rule_iv = median_c is not None and median_c <= PASS_RULE["median_c_h_tolerance"]
    return {"i_no_unmatched": rule_i, "ii_identical_geometry_agrees": rule_ii, "iii_geometry_or_sigma_95": rule_iii,
            "iv_median_c_h_3pct": rule_iv, "pass": bool(rule_i and rule_ii and rule_iii and rule_iv)}


def public_line(symbol: str, month: str, stats: dict) -> str:
    """Counts and verdicts only: no prices, no per-setup values."""
    v = stats["verdict"]
    return (f"{symbol} {month} setups={stats['setups']} unmatched={stats['unmatched']} "
            f"identical_geometry={stats['identical_geometry']} "
            f"rules i={_p(v['i_no_unmatched'])} ii={_p(v['ii_identical_geometry_agrees'])} "
            f"iii={_p(v['iii_geometry_or_sigma_95'])} iv={_p(v['iv_median_c_h_3pct'])} {_p(v['pass'])}")


def _p(value: bool) -> str:
    return "PASS" if value else "FAIL"
