"""Pure baseline transition for replay/backtesting; mirrors the app's atomic RPC.

Input is a validated completed 1m close. Persist returned state only together with
any returned alert. This module does not connect to the application database.
"""
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext

from .core import MINUTE, percentage_change, positive
from .providers import PROVIDERS


@dataclass(frozen=True)
class Baseline:
    price: Decimal
    at_ms: int
    source: str
    threshold: Decimal
    last_observed_ms: int
    last_up_alert_ms: int | None = None
    last_down_alert_ms: int | None = None


def observe(state, price, observed_ms, source, now_ms, threshold="2", cooldown_minutes=15):
    price, threshold = positive(price), positive(threshold)
    if not Decimal("0.1") <= threshold <= 100 or not 1 <= cooldown_minutes <= 1440:
        raise ValueError("invalid threshold or cooldown")
    if (type(cooldown_minutes) is not int or type(observed_ms) is not int
            or observed_ms % MINUTE or observed_ms > now_ms
            or now_ms - observed_ms > 10 * MINUTE
            or source not in PROVIDERS):
        raise ValueError("invalid or stale completed-candle observation")
    if state is None:
        return Baseline(price, observed_ms, source, threshold, observed_ms), {"status": "initialized"}
    if observed_ms <= state.last_observed_ms:
        return state, {"status": "already_processed"}
    if source != state.source or threshold != state.threshold:
        return replace(state, price=price, at_ms=observed_ms, source=source,
                       threshold=threshold, last_observed_ms=observed_ms), {"status": "reinitialized"}
    next_state = replace(state, last_observed_ms=observed_ms)
    change = percentage_change(state.price, price)
    # Match the database's unrounded, cross-multiplied threshold comparison.
    with localcontext() as ctx:
        ctx.prec = 80
        reached = (price - state.price).copy_abs() * 100 >= state.price * threshold
    if not reached:
        return next_state, {"status": "below_threshold", "change_pct": str(change)}
    direction = "up" if price > state.price else "down"
    last_alert = state.last_up_alert_ms if direction == "up" else state.last_down_alert_ms
    if last_alert is not None and now_ms < last_alert + cooldown_minutes * MINUTE:
        return next_state, {"status": "cooldown", "direction": direction}
    next_state = replace(next_state, price=price, at_ms=observed_ms,
                         **{f"last_{direction}_alert_ms": now_ms})
    return next_state, {"status": "alerted", "direction": direction,
                        "change_pct": str(change), "baseline_price": str(state.price),
                        "baseline_at_ms": state.at_ms, "price": str(price)}
