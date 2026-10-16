"""Multi-hour market movement descriptor ("Longer moves": 1 h, 4 h, 12 h), #30 / #373.

A separate, versioned extension next to the V1 1m/5m/15m movement engine; V1 is untouched. It DESCRIBES the
current market state; it is not a predictor, trade rule or profitability claim, and it creates no alerts. Its
thresholds are provisional and unvalidated (counted as an unvalidated descriptor).

Pure and deterministic, standard library only. Input per symbol: N = 672 grid prices at 15-minute steps back
from the evaluation boundary B (g_k = close of the completed 1m candle that closed at B - 15 min x k, or None
when that candle is missing). Nothing is filled and nothing after B is used.

For horizon H minutes (h = H / 15 grid steps):
  r_j = ln(g_j / g_(j+h)) when both prices exist
  current r0 = r_0; reference = r_j for j = h .. h + 575 (576 samples, 6 days, disjoint from the current window)
  eligible when r0 exists and at least 288 reference samples exist (else "collecting_history")
  m = median(reference), s = 1.4826 x MAD(reference); s <= 0 or not finite -> "unavailable"
  z = (r0 - m) / s
Reference samples overlap each other (15-minute step, H-minute windows); that is acceptable for a robust scale
estimate but means they are not independent observations.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from statistics import median
from typing import Iterable, Optional, Sequence

SLOW_MOVEMENT_ALGORITHM_VERSION = "slow-movement-v1"
SLOW_MOVEMENT_CONFIG_VERSION = "slow-config-v1"

GRID_STEP_MS = 15 * 60_000
GRID_POINTS = 672  # 7 days at 15 minutes
HORIZONS_MIN = (60, 240, 720)
MAD_SCALE = 1.4826


@dataclass(frozen=True)
class SlowMovementConfig:
    """Every threshold of slow-config-v1. Provisional and unvalidated; any change is a new config identity."""

    version: str = SLOW_MOVEMENT_CONFIG_VERSION
    grid_points: int = GRID_POINTS
    reference_samples: int = 576  # 6 days at 15 minutes
    min_reference_samples: int = 288  # 3 days
    z_threshold: float = 2.0
    coordinated_share: float = 0.70
    min_included: int = 5


DEFAULT_SLOW_CONFIG = SlowMovementConfig()


def config_hash(config: SlowMovementConfig = DEFAULT_SLOW_CONFIG) -> str:
    """Short SHA-256 of the canonical config JSON (sorted keys, no whitespace)."""
    canonical = json.dumps(asdict(config), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _log_return(grid: Sequence[Optional[float]], j: int, h: int) -> Optional[float]:
    if j + h >= len(grid):
        return None
    a, b = grid[j], grid[j + h]
    if a is None or b is None:
        return None
    return math.log(a / b)


def _symbol_horizon(grid: Sequence[Optional[float]], h: int, config: SlowMovementConfig) -> dict:
    r0 = _log_return(grid, 0, h)
    reference = [
        r for r in (_log_return(grid, j, h) for j in range(h, h + config.reference_samples)) if r is not None
    ]
    base = {"returnPct": None if r0 is None else (math.exp(r0) - 1) * 100, "z": None,
            "logReturn": r0, "referenceSamples": len(reference)}
    if r0 is None or len(reference) < config.min_reference_samples:
        return {**base, "status": "collecting_history"}
    m = median(reference)
    s = MAD_SCALE * median(abs(r - m) for r in reference)
    if not math.isfinite(s) or s <= 0:
        return {**base, "status": "unavailable"}
    z = (r0 - m) / s
    if not math.isfinite(z):
        return {**base, "status": "unavailable"}
    return {**base, "z": z, "status": "ok"}


def _aggregate(rows: Iterable[dict], config: SlowMovementConfig) -> dict:
    ok = [row for row in rows if row["status"] == "ok"]
    n = len(ok)
    if n == 0:
        return {"included": 0, "medianReturnPct": None, "medianZ": None, "up": 0, "down": 0, "neutral": 0,
                "upShare": None, "downShare": None, "coordinated": None}
    median_r = median(row["logReturn"] for row in ok)
    median_z = median(row["z"] for row in ok)
    up = sum(1 for row in ok if row["z"] >= config.z_threshold)
    down = sum(1 for row in ok if row["z"] <= -config.z_threshold)
    up_share, down_share = up / n, down / n
    coordinated = None
    if n >= config.min_included:
        if up_share >= config.coordinated_share and median_z >= config.z_threshold:
            coordinated = "rise"
        elif down_share >= config.coordinated_share and median_z <= -config.z_threshold:
            coordinated = "fall"
    return {"included": n, "medianReturnPct": (math.exp(median_r) - 1) * 100, "medianZ": median_z,
            "up": up, "down": down, "neutral": n - up - down, "upShare": up_share, "downShare": down_share,
            "coordinated": coordinated}


def evaluate_slow_movement(
    boundary_ms: int,
    horizons_min: Sequence[int],
    symbols: Sequence[tuple[str, Sequence[Optional[float]]]],
    config: SlowMovementConfig = DEFAULT_SLOW_CONFIG,
) -> dict:
    """Evaluate every requested horizon. ``symbols`` is a sequence of (symbol, grid) with grid[k] the price at
    boundary - 15 min x k or None. Output order follows the request; aggregates do not depend on symbol order."""
    if boundary_ms % GRID_STEP_MS:
        raise ValueError("boundary must be a multiple of 15 minutes")
    horizons = list(horizons_min)
    if not horizons or len(set(horizons)) != len(horizons) or any(h not in HORIZONS_MIN for h in horizons):
        raise ValueError("horizons must be a non-empty subset of 60, 240, 720")
    names = [name for name, _ in symbols]
    if len(names) != len(set(names)):
        raise ValueError("symbols must be unique")
    for _, grid in symbols:
        if len(grid) != config.grid_points:
            raise ValueError(f"each grid must have {config.grid_points} points")
        for price in grid:
            if price is not None and not (math.isfinite(price) and price > 0):
                raise ValueError("grid prices must be positive and finite")
    result_horizons = []
    for horizon in sorted(horizons):
        h = horizon * 60_000 // GRID_STEP_MS
        rows = []
        for name, grid in symbols:
            row = _symbol_horizon(grid, h, config)
            rows.append({"symbol": name, "returnPct": row["returnPct"], "z": row["z"], "status": row["status"],
                         "referenceSamples": row["referenceSamples"], "logReturn": row["logReturn"]})
        aggregate = _aggregate(rows, config)
        for row in rows:
            del row["logReturn"]
        result_horizons.append({"horizonMin": horizon, **aggregate, "symbols": rows})
    return {
        "algorithmVersion": SLOW_MOVEMENT_ALGORITHM_VERSION,
        "configVersion": config.version,
        "configHash": config_hash(config),
        "boundaryMs": boundary_ms,
        "config": {
            "gridPoints": config.grid_points,
            "referenceSamples": config.reference_samples,
            "minReferenceSamples": config.min_reference_samples,
            "zThreshold": config.z_threshold,
            "coordinatedShare": config.coordinated_share,
            "minIncluded": config.min_included,
        },
        "horizons": result_horizons,
    }
