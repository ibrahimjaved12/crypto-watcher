"""Strategy capability declarations (#374): which markets and data each strategy needs.

Every ``ta_strategies.STRATEGIES`` name must be declared in ``TA_CAPABILITIES``; adding a strategy
without a declaration makes this module fail at import. Checks fail closed: an unknown strategy id
has no capabilities, so ``check_compatibility`` refuses it with ``no_capabilities_declared``.

The six baseline TA rules read only close/high/low and the candle count (no volume, taker flow, mark
or funding), so they need OHLC only and run on both markets. The doji variants are paused, were
calibrated on crypto and are marked unvalidated, so they are declared crypto-only.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from ..benchmark import ta_strategies as ta
from ..instruments import (Compatibility, DataField, Instrument, Market, StrategyCapabilities,
                           check_compatibility)
from .signals import FORWARD_STRATEGIES

_BOTH = StrategyCapabilities(markets=frozenset({Market.CRYPTO, Market.FOREX}),
                             requires=frozenset({DataField.OHLC}))
_CRYPTO_ONLY = StrategyCapabilities(markets=frozenset({Market.CRYPTO}), requires=frozenset({DataField.OHLC}))

_DECLARED = {
    "ema_cross_20_50": _BOTH,
    "ema_cross_50_200": _BOTH,
    "rsi_14_reversion": _BOTH,
    "macd_12_26_9": _BOTH,
    "bollinger_20_2_reversion": _BOTH,
    "keltner_breakout_20_14_2": _BOTH,
    "doji_continuation_v1": _CRYPTO_ONLY,
    "doji_reversal_v1": _CRYPTO_ONLY,
}


def _build() -> Dict[str, StrategyCapabilities]:
    missing = sorted(set(ta.STRATEGIES) - set(_DECLARED))
    if missing:
        raise RuntimeError(f"strategies without declared capabilities: {', '.join(missing)}")
    return {name: _DECLARED[name] for name in ta.STRATEGIES}


TA_CAPABILITIES: Dict[str, StrategyCapabilities] = _build()


def capabilities_for(strategy_id: str) -> Optional[StrategyCapabilities]:
    """Capabilities of a forward strategy id; a placebo inherits its matched strategy's (its emission
    is random, its data needs are the matched rule's). Unknown id -> ``None``."""
    strategy = FORWARD_STRATEGIES.get(strategy_id)
    return None if strategy is None else TA_CAPABILITIES.get(strategy.name)


def compatibility_for(strategy_id: str, instrument: Instrument) -> Compatibility:
    return check_compatibility(capabilities_for(strategy_id), instrument)


def compatible_strategy_ids(instrument: Instrument, strategy_ids: Optional[Iterable[str]] = None
                            ) -> Tuple[List[str], Dict[str, Tuple[str, ...]]]:
    """(sorted compatible ids, {incompatible id: reasons}); default ids = every forward strategy."""
    ids = sorted(FORWARD_STRATEGIES) if strategy_ids is None else sorted(set(strategy_ids))
    ok, refused = [], {}
    for strategy_id in ids:
        result = compatibility_for(strategy_id, instrument)
        if result.ok:
            ok.append(strategy_id)
        else:
            refused[strategy_id] = result.reasons
    return ok, refused


__all__ = ["TA_CAPABILITIES", "capabilities_for", "compatibility_for", "compatible_strategy_ids"]
