"""Option-book analytics shared by the portfolio risk view and the strategy builder.

A leg is a dict: qty (signed units), price (current mark or entry), kind ('OPT', 'FUT',
'EQ'), and for options option_type ('CE'/'PE'), strike, expiry (ISO date) and iv
(annualised, 0.15 = 15 vol points). Black-Scholes, European, no dividends.

Everything that cannot be valued is reported, never valued at zero.
"""
import math
from datetime import datetime

from .clock import IST, expiry_close, years_to_expiry
from .config import settings
from .greeks import _cdf, black_scholes, bs_price, implied_vol

YEAR_SEC = 365.0 * 24 * 3600


def _now(now=None):
    return now or datetime.now(IST)


def leg_iv(leg, spot, r=None, now=None):
    """Leg IV: given, else implied from its price. None when it cannot be implied."""
    if leg.get('iv'):
        return leg['iv']
    if leg.get('kind', 'OPT') != 'OPT' or not spot or not leg.get('price'):
        return None
    r = settings.risk_free_rate if r is None else r
    return implied_vol(leg['price'], spot, leg['strike'], years_to_expiry(leg['expiry'], _now(now)), r, leg['option_type'])


def leg_greeks(leg, spot, r=None, now=None):
    """Position Greeks (qty-weighted): delta (units of underlying), gamma, theta (Rs/day),
    vega (Rs per vol point), delta_notional (Rs). None values when unavailable."""
    qty = leg['qty']
    kind = leg.get('kind', 'OPT')
    if kind != 'OPT':
        px = leg.get('price') or 0
        return {'delta': float(qty), 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0, 'delta_notional': qty * px, 'iv': None}
    r = settings.risk_free_rate if r is None else r
    iv = leg_iv(leg, spot, r, now)
    if not spot or not iv:
        return {'delta': None, 'gamma': None, 'theta': None, 'vega': None, 'delta_notional': None, 'iv': iv}
    g = black_scholes(spot, leg['strike'], years_to_expiry(leg['expiry'], _now(now)), r, iv, leg['option_type'])
    return {'delta': qty * g['delta'], 'gamma': qty * g['gamma'], 'theta': qty * g['theta'], 'vega': qty * g['vega'],
            'delta_notional': qty * g['delta'] * spot, 'iv': iv}


def gamma_pnl(gamma, spot, move=0.01):
    """Second-order P&L (Rs) of a `move` spot change: 0.5 * gamma * (move * spot)^2."""
    return None if gamma is None or not spot else 0.5 * gamma * (move * spot) ** 2


def _value_at(leg, S, at, spot0, r):
    """Leg value at spot S on datetime `at` (intrinsic once expired). None if unpriceable."""
    kind = leg.get('kind', 'OPT')
    if kind != 'OPT':
        return leg['price'] * S / spot0 if spot0 else None
    t = (expiry_close(leg['expiry']) - at).total_seconds() / YEAR_SEC
    if t <= 1e-9:
        return max(S - leg['strike'], 0.0) if leg['option_type'] == 'CE' else max(leg['strike'] - S, 0.0)
    if not leg.get('iv'):
        return None
    return bs_price(S, leg['strike'], t, r, leg['iv'], leg['option_type'])


def payoff_curves(legs, spot, r=None, now=None, points=121, span=0.10, base_pnl=0.0):
    """P&L curves over spot in [spot*(1-span), spot*(1+span)].

    'expiry' is valued at the nearest option expiry (legs expiring later keep their time
    value at their IV); 'today' is valued now. P&L = base_pnl + sum qty * (value - price),
    so with base_pnl = current P&L and price = current mark, 'today' passes through the
    current P&L at the current spot.
    """
    r = settings.risk_free_rate if r is None else r
    now = _now(now)
    opt = [leg for leg in legs if leg.get('kind', 'OPT') == 'OPT']
    target = min((leg['expiry'] for leg in opt), default=None)
    at_exp = expiry_close(target) if target else now
    lo, hi = spot * (1 - span), spot * (1 + span)
    xs = [lo + (hi - lo) * i / (points - 1) for i in range(points)]
    curves, complete = {'expiry': [], 'today': []}, True
    for name, at in (('expiry', at_exp), ('today', now)):
        for S in xs:
            total = base_pnl
            for leg in legs:
                v = _value_at(leg, S, at, spot, r)
                if v is None:
                    complete = False
                    total = None
                    break
                total += leg['qty'] * (v - leg['price'])
            curves[name].append(None if total is None else round(total, 2))
    return {'spots': [round(x, 2) for x in xs], 'expiry': curves['expiry'], 'today': curves['today'],
            'target_expiry': target, 'complete': complete, 'breakevens': crossings(xs, curves['expiry'])}


def crossings(xs, ys):
    """Spots where a curve crosses zero (linear interpolation)."""
    out = []
    for i in range(1, len(xs)):
        a, b = ys[i - 1], ys[i]
        if a is None or b is None:
            continue
        if a == 0:
            out.append(round(xs[i - 1], 2))
        elif (a < 0 < b) or (b < 0 < a):
            out.append(round(xs[i - 1] + (xs[i] - xs[i - 1]) * (-a) / (b - a), 2))
    return out


def expiry_payoff_stats(legs):
    """Exact expiry P&L extremes for legs that all expire together (piecewise linear in S).

    Returns {'max_profit', 'max_loss', 'unbounded_profit', 'unbounded_loss', 'breakevens'},
    or None when option legs have different expiries (use the curves instead).
    """
    opt = [leg for leg in legs if leg.get('kind', 'OPT') == 'OPT']
    if len({leg['expiry'] for leg in opt}) > 1:
        return None
    lin = [leg for leg in legs if leg.get('kind', 'OPT') != 'OPT']
    if lin:
        return None  # futures/equity payoff depends on their own price path; curves cover it

    def pnl(S):
        return sum(leg['qty'] * ((max(S - leg['strike'], 0.0) if leg['option_type'] == 'CE' else max(leg['strike'] - S, 0.0))
                                 - leg['price']) for leg in opt)
    kinks = sorted({0.0} | {float(leg['strike']) for leg in opt})
    vals = [pnl(k) for k in kinks]
    slope_up = sum(leg['qty'] for leg in opt if leg['option_type'] == 'CE')
    top = kinks[-1] * 2 + 1
    xs, ys = kinks + [top], vals + [pnl(top)]
    return {'max_profit': None if slope_up > 0 else round(max(vals), 2), 'max_loss': None if slope_up < 0 else round(min(vals), 2),
            'unbounded_profit': slope_up > 0, 'unbounded_loss': slope_up < 0, 'breakevens': crossings(xs, ys)}


def probability_above(spot, level, iv, T, r=None):
    """Risk-neutral lognormal P(S_T > level)."""
    r = settings.risk_free_rate if r is None else r
    if level <= 0:
        return 1.0
    if not spot or not iv or T <= 0:
        return 1.0 if spot > level else 0.0
    d2 = (math.log(spot / level) + (r - 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    return _cdf(d2)


def probability_of_profit(legs, spot, iv, T, r=None):
    """Model probability that the expiry P&L is positive, from the breakevens of the exact
    single-expiry payoff. None when the payoff is not single-expiry or inputs are missing."""
    stats = expiry_payoff_stats(legs)
    if stats is None or not spot or not iv or T <= 0:
        return None
    opt = [leg for leg in legs if leg.get('kind', 'OPT') == 'OPT']

    def pnl(S):
        return sum(leg['qty'] * ((max(S - leg['strike'], 0.0) if leg['option_type'] == 'CE' else max(leg['strike'] - S, 0.0))
                                 - leg['price']) for leg in opt)
    edges = [0.0] + sorted(stats['breakevens']) + [float('inf')]
    p = 0.0
    for a, b in zip(edges, edges[1:]):
        mid = (a + b) / 2 if b != float('inf') else (a * 1.5 + 1)
        if pnl(mid) > 0:
            p += probability_above(spot, a, iv, T, r) - (probability_above(spot, b, iv, T, r) if b != float('inf') else 0.0)
    return round(min(max(p, 0.0), 1.0), 4)
