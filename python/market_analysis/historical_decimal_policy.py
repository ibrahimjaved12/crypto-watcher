"""Keys for bounded arithmetic memoization under the caller's Decimal policy."""
from decimal import Context, getcontext


def decimal_policy(precision):
    context = getcontext()
    return (precision, context.rounding, context.Emin, context.Emax,
            context.capitals, context.clamp, tuple(context.traps.items()))


def policy_context(policy):
    precision, rounding, emin, emax, capitals, clamp, traps = policy
    return Context(prec=precision, rounding=rounding, Emin=emin, Emax=emax,
                   capitals=capitals, clamp=clamp, traps=dict(traps))
