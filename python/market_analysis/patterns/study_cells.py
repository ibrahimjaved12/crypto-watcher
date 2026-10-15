"""Pre-registered cells of the doji event study (docs/pattern-study.md, "Doji event study").

FROZEN: these definitions are not changed once results exist; any change is a new ``CELLS_VERSION``
(and ``EVENT_VERSION`` for the event/control shapes). The direction ``s`` of a signed cell is fixed
by its context rule and never by the outcome.

All functions take a *frame*: a dict of equal-length numpy arrays named like candle-table columns
(floats with NaN for empty cells; booleans as 1.0/0.0; text columns as object arrays). A missing
input never satisfies a condition (NaN comparisons are false), so such rows fall outside a context.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

CELLS_VERSION = "doji-cells-v1"
EVENT_VERSION = "doji-geo-v2"
BODY_MAX = 0.10
BODY_MAX_STRICT = 0.05      # descriptive sensitivity only
RANGE_ATR_MIN = 0.5
RANGE_ATR_LARGE = 1.0       # descriptive sensitivity only
ADX_TREND = 20.0
EXTENSION_CALM = 1.5
EXTENSION_STRETCHED = 2.0
LEVEL_CLEAR_ATR = 0.5
REL_VOLUME_SPIKE = 2.0
RET3_SHOCK_SIGMA = 2.0


def _is(frame, name: str, value: float) -> np.ndarray:
    return frame[name] == value


def _side(values) -> np.ndarray:
    """True where the value is exactly +1 or -1 (NaN and 0 are not a side)."""
    return np.isin(values, (-1.0, 1.0))


def population(frame) -> np.ndarray:
    """Segment rows with status ok and a valid one-candle outcome window."""
    return (frame["status"] == "ok") & _is(frame, "fwd_ok_1", 1.0)


def sized(frame, range_atr_min: float = RANGE_ATR_MIN) -> np.ndarray:
    """Rows of the regression sample's size class: range > 0 and range_atr >= the minimum."""
    return (frame["range"] > 0) & (frame["range_atr"] >= range_atr_min)


def event(frame, body_max: float = BODY_MAX, range_atr_min: float = RANGE_ATR_MIN) -> np.ndarray:
    """E (doji-geo-v2): body_ratio <= 0.10, range > 0, range_atr >= 0.5 (status checked by population)."""
    return sized(frame, range_atr_min) & (frame["body_ratio"] <= body_max)


def control(frame, range_atr_min: float = RANGE_ATR_MIN) -> np.ndarray:
    """Same size, other shape: range_atr >= 0.5 and body_ratio > 0.10."""
    return sized(frame, range_atr_min) & (frame["body_ratio"] > BODY_MAX)


@dataclass(frozen=True)
class Cell:
    id: str
    signed: bool
    description: str
    rule: Callable  # rule(frame, intraday) -> (context mask, direction s as float array; 0 where unsigned)


def _all(frame, intraday):
    n = len(frame["status"])
    return np.ones(n, bool), np.zeros(n)


def _c1(frame, intraday):
    trend = frame["trend_dir"]
    mask = ((frame["adx_prev"] >= ADX_TREND) & _side(trend) & (np.abs(frame["extension_prev_atr"]) < EXTENSION_CALM)
            & _is(frame, "any_touch", 0.0) & (frame["nearest_level_dist_atr"] > LEVEL_CLEAR_ATR))
    return mask, np.where(mask, trend, 0.0)


def _c2(frame, intraday):
    trend = frame["trend_dir"]
    with np.errstate(invalid="ignore"):
        stretched = frame["extension_prev_atr"] * trend >= EXTENSION_STRETCHED
    at_extreme = np.where(trend > 0, frame["high"] >= frame["prior20_high"],
                          frame["low"] <= frame["prior20_low"])
    mask = (frame["adx_prev"] >= ADX_TREND) & _side(trend) & stretched & at_extreme
    return mask, np.where(mask, -trend, 0.0)


def _c3(frame, intraday):
    up, down = _is(frame, "sweep_any_up", 1.0), _is(frame, "sweep_any_dn", 1.0)
    mask = (up ^ down) & _is(frame, "sweep_both", 0.0) & _side(frame["sweep_dir"])
    return mask, np.where(mask, frame["sweep_dir"], 0.0)


def _c4(frame, intraday):
    side = frame["touch_approach_side"]
    mask = _is(frame, "any_touch", 1.0) & _side(side)
    return mask, np.where(mask, -side, 0.0)


def _c5(frame, intraday):
    return _is(frame, "squeeze_on_prev", 1.0), np.zeros(len(frame["status"]))


def _c6(frame, intraday):
    trend = frame["trend_dir"]
    mask = (frame["adx_prev"] < ADX_TREND) & _side(trend)
    return mask, np.where(mask, trend, 0.0)


def _c7(frame, intraday):
    flush = frame["flush_dir"]
    mask = _side(flush)
    return mask, np.where(mask, -flush, 0.0)


def _c8(frame, intraday):
    volume = frame["rel_volume_slot"] if intraday else frame["rel_volume_prior20"]
    ret3 = frame["ret3_prev_sigma"]
    mask = (volume >= REL_VOLUME_SPIKE) & (np.abs(ret3) >= RET3_SHOCK_SIGMA)
    return mask, np.where(mask, -np.sign(ret3), 0.0)


CELLS = (
    Cell("A", False, "all candles (unsigned: next-candle range)", _all),
    Cell("C1", True, "adx_prev >= 20, trend_dir != 0, |extension_prev_atr| < 1.5, no level touch and the nearest "
                     "level > 0.5 ATR away -> trend_dir (pause in a clean trend)", _c1),
    Cell("C2", True, "adx_prev >= 20, extension_prev_atr * trend_dir >= 2 and the candle's trend-side extreme at or "
                     "beyond the prior-20 extreme -> -trend_dir (exhaustion)", _c2),
    Cell("C3", True, "exactly one side swept (sweep_both false) -> sweep_dir (rejection of the sweep)", _c3),
    Cell("C4", True, "any level touched -> -touch_approach_side (rejection at the level)", _c4),
    Cell("C5", False, "squeeze on at i-1 (unsigned: next-candle range)", _c5),
    Cell("C6", True, "adx_prev < 20, trend_dir != 0 -> trend_dir (negative control, expected null)", _c6),
    Cell("C7", True, "flush proxy fired -> -flush_dir (reversal after a flush)", _c7),
    Cell("C8", True, "relative volume >= 2 (intraday: same-slot; daily/weekly: prior-20) and |ret3_prev_sigma| >= 2 "
                     "-> -sign(ret3_prev_sigma) (climax reversal)", _c8),
)
CELL_IDS = tuple(cell.id for cell in CELLS)
K_PRIMARY = len(CELLS)
