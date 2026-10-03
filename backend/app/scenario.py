"""Full-revaluation portfolio scenarios for option books.

Each option is re-priced with Black-Scholes under every (spot shock, vol shock)
pair instead of a delta-gamma-vega Taylor expansion, so short-gamma books show
their true convex loss. Vol shocks are ABSOLUTE vol points (5 = +5 points).
Inputs that cannot be priced make the run fail closed instead of contributing 0.
"""
from .clock import years_to_expiry
from .config import settings
from .greeks import bs_price, implied_vol

SPOT_SHOCKS = (-0.07, -0.05, -0.03, -0.01, 0.0, 0.01, 0.03, 0.05, 0.07)
VOL_SHOCKS = (-5.0, 0.0, 5.0, 10.0)


def revalue(positions, spots, spot_shocks=SPOT_SHOCKS, vol_shocks=VOL_SHOCKS, r=None):
    """positions: dicts with qty (signed), price, and for options option_type, strike,
    expiry, underlying. spots: underlying -> spot. Returns worst loss and grid."""
    r = settings.risk_free_rate if r is None else r
    legs, errors = [], []
    for p in positions:
        qty = int(p.get('qty') or 0)
        if qty == 0:
            continue
        if p.get('option_type') in ('CE', 'PE'):
            S = spots.get(p.get('underlying'))
            if not S or not p.get('strike') or not p.get('expiry') or not p.get('price'):
                errors.append(f"UNPRICEABLE:{p.get('symbol') or p.get('token')}")
                continue
            T = years_to_expiry(p['expiry'])
            iv = p.get('iv') or implied_vol(p['price'], S, p['strike'], T, r, p['option_type'])
            if not iv:
                errors.append(f"IV_UNAVAILABLE:{p.get('symbol') or p.get('token')}")
                continue
            legs.append(('OPT', qty, p['price'], S, p['strike'], T, iv, p['option_type'], p.get('underlying')))
        else:
            if not p.get('price'):
                errors.append(f"UNPRICEABLE:{p.get('symbol') or p.get('token')}")
                continue
            legs.append(('LIN', qty, p['price']))
    if errors:
        return {'ok': False, 'reason': 'SCENARIO_INPUTS_INCOMPLETE', 'errors': errors}
    grid = []
    for ds in spot_shocks:
        for dv in vol_shocks:
            pnl = 0.0
            for leg in legs:
                if leg[0] == 'OPT':
                    _, qty, px, S, K, T, iv, typ, _u = leg
                    new = bs_price(S * (1 + ds), K, T, r, max(iv + dv / 100.0, 0.01), typ)
                    pnl += qty * (new - px)
                else:
                    _, qty, px = leg
                    pnl += qty * px * ds
            grid.append({'spot_shock': ds, 'vol_shock_pts': dv, 'pnl': round(pnl, 2)})
    return {'ok': True, 'worst_pnl': min(g['pnl'] for g in grid), 'scenarios': grid, 'legs': len(legs)}
