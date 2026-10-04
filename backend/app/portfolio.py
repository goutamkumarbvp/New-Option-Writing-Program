"""Firm portfolio risk view built from broker-authoritative positions.

For every open position (from the risk monitor's broker snapshots, already enriched with
the instrument master and live prices): model IV and position Greeks against a live spot,
aggregated per underlying and firm-wide in rupee terms (delta notional, 1% gamma P&L,
theta per day, vega per vol point), plus a full-revaluation spot x vol scenario grid and
payoff curves per underlying.

Read-only analytics: nothing here changes orders or limits. Positions that cannot be
priced are listed with the reason and make the affected totals 'complete: false'.
"""
import time

from .analytics import gamma_pnl, leg_greeks, payoff_curves
from .clock import expired
from .option_chain import resolve_spot
from .scenario import revalue

SUMMABLE = ('delta_notional', 'gamma_pnl_1pct', 'theta', 'vega')


def _kind(p):
    if p.get('option_type') in ('CE', 'PE'):
        return 'OPT'
    return 'FUT' if p.get('expiry') else 'EQ'


def _spot(feed, chain, underlying, exchange, expiry, cache):
    key = (underlying, expiry)
    if key not in cache:
        tk = feed.live_spot(underlying) if underlying else None
        if tk:
            cache[key] = (tk.ltp, 'LIVE_SPOT')
        elif underlying and expiry:
            s = resolve_spot(feed, underlying, expiry, chain.snapshot(exchange, underlying, expiry))
            cache[key] = (s.get('value'), s.get('source'))
        else:
            cache[key] = (None, None)
    return cache[key]


def build_portfolio(snapshots, feed, chain, now_ms=None):
    now_ms = now_ms or int(time.time() * 1000)
    brokers, positions, cache = {}, [], {}
    for name, snap in sorted(snapshots.items()):
        brokers[name] = {'ok': snap.ok, 'age_sec': round(snap.age(), 2), 'error': snap.error, 'issues': snap.issues[:20],
                         'total_pnl': snap.total_pnl, 'day_pnl': snap.day_pnl, 'day_pnl_source': snap.day_pnl_source}
        for p in snap.positions:
            if not p.get('qty'):
                continue
            kind, und = _kind(p), p.get('underlying') or (p.get('symbol') if _kind(p) == 'EQ' else None)
            row = {'broker': name, 'token': p['token'], 'symbol': p.get('symbol'), 'exchange': p.get('exchange'), 'kind': kind,
                   'underlying': und, 'expiry': p.get('expiry'), 'strike': p.get('strike'), 'option_type': p.get('option_type'),
                   'qty': p['qty'], 'lots': (p['qty'] / p['lot_size']) if p.get('lot_size') else None, 'price': p.get('price'),
                   'price_source': p.get('price_source'), 'pnl': p.get('pnl'), 'day_pnl': p.get('day_pnl'), 'product': p.get('product'),
                   'spot': None, 'spot_source': None, 'iv': None, 'delta': None, 'gamma': None, 'theta': None, 'vega': None,
                   'delta_notional': None, 'gamma_pnl_1pct': None, 'issue': None}
            if kind == 'OPT' and expired(p.get('expiry')):
                row['issue'] = 'EXPIRED_CONTRACT'
            elif not p.get('price'):
                row['issue'] = 'NO_PRICE'
            elif kind == 'OPT' and not (p.get('strike') and p.get('expiry') and und):
                row['issue'] = 'INSTRUMENT_METADATA_MISSING'
            else:
                spot, src = _spot(feed, chain, und, p.get('exchange'), p.get('expiry'), cache) if kind == 'OPT' else (p.get('price'), 'OWN_PRICE')
                row['spot'], row['spot_source'] = spot, src
                g = leg_greeks({'qty': p['qty'], 'price': p['price'], 'kind': kind, 'option_type': p.get('option_type'),
                                'strike': p.get('strike'), 'expiry': p.get('expiry')}, spot)
                row.update({k: g[k] for k in ('iv', 'delta', 'gamma', 'theta', 'vega', 'delta_notional')})
                row['gamma_pnl_1pct'] = gamma_pnl(g['gamma'], spot)
                if g['delta'] is None:
                    row['issue'] = 'SPOT_UNAVAILABLE' if not spot else 'IV_UNAVAILABLE'
            positions.append(row)

    groups = {}
    for row in positions:
        groups.setdefault(row['underlying'] or row['symbol'], []).append(row)
    underlyings, spots = [], {}
    for und, rows in sorted(groups.items(), key=lambda kv: str(kv[0])):
        complete = all(r['issue'] is None for r in rows)
        agg = {k: (sum(r[k] for r in rows if r[k] is not None) if any(r[k] is not None for r in rows) else None)
               for k in ('delta', 'gamma', 'theta', 'vega', 'delta_notional', 'gamma_pnl_1pct', 'pnl')}
        spot = next((r['spot'] for r in rows if r['kind'] == 'OPT' and r['spot']), None)
        if spot:
            spots[und] = spot
        legs = [{'qty': r['qty'], 'price': r['price'], 'kind': r['kind'], 'option_type': r['option_type'], 'strike': r['strike'],
                 'expiry': r['expiry'], 'iv': r['iv']} for r in rows if r['issue'] is None]
        payoff = payoff_curves(legs, spot, base_pnl=sum(r['pnl'] or 0 for r in rows if r['issue'] is None)) if spot and legs else None
        underlyings.append({'underlying': und, 'spot': spot,
                            'spot_source': next((r['spot_source'] for r in rows if r['kind'] == 'OPT' and r['spot']), None),
                            'positions': len(rows), 'complete': complete, **agg,
                            'short_option_qty': sum(-r['qty'] for r in rows if r['kind'] == 'OPT' and r['qty'] < 0),
                            'long_option_qty': sum(r['qty'] for r in rows if r['kind'] == 'OPT' and r['qty'] > 0),
                            'payoff': payoff})

    totals = {k: sum(u[k] or 0 for u in underlyings) for k in SUMMABLE}
    totals.update(positions=len(positions), complete=all(u['complete'] for u in underlyings) and all(b['ok'] for b in brokers.values()),
                  total_pnl=sum(b['total_pnl'] or 0 for b in brokers.values()),
                  day_pnl=sum(b['day_pnl'] or 0 for b in brokers.values()))
    priced = [{'qty': r['qty'], 'price': r['price'], 'option_type': r['option_type'], 'strike': r['strike'], 'expiry': r['expiry'],
               'underlying': r['underlying'], 'iv': r['iv'], 'symbol': r['symbol'], 'token': r['token']} for r in positions]
    scenarios = revalue(priced, spots) if positions else {'ok': True, 'worst_pnl': 0.0, 'scenarios': [], 'legs': 0}
    return {'as_of_ms': now_ms, 'brokers': brokers, 'positions': positions, 'underlyings': underlyings, 'totals': totals,
            'scenarios': scenarios}
