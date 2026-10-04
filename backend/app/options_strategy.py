"""Option-writing strategy builder: templates from the live chain and full strategy analytics.

Read-only. Legs are priced at executable quotes (SELL at bid, BUY at ask) from the live
ladder unless a price is given. Execution stays with POST /orders, one leg at a time;
`execution_order` lists hedges (BUY legs) first because the order path blocks a new short
option until an equal long of the same underlying, expiry and type is in the book.
"""
from .analytics import expiry_payoff_stats, leg_greeks, payoff_curves, probability_of_profit
from .clock import years_to_expiry
from .config import settings

TEMPLATES = {
    'short_straddle': 'Sell ATM call and put',
    'short_strangle': 'Sell OTM call and put at the target delta',
    'iron_condor': 'Short strangle at the target delta plus long wings',
    'iron_fly': 'Short ATM straddle plus long wings',
    'bull_put_spread': 'Sell a put at the target delta, buy a lower put',
    'bear_call_spread': 'Sell a call at the target delta, buy a higher call',
}


class StrategyError(ValueError):
    pass


def _strikes(ladder):
    return [r['strike'] for r in ladder['strikes']]


def _leg_at(ladder, strike, typ):
    row = next((r for r in ladder['strikes'] if r['strike'] == strike), None)
    return row and row.get(typ)


def _by_delta(ladder, typ, target):
    """OTM strike of `typ` whose |delta| is closest to target (needs model Greeks)."""
    atm = ladder['atm_strike']
    cands = [(r['strike'], r[typ]) for r in ladder['strikes'] if r.get(typ) and r[typ].get('delta') is not None
             and (r['strike'] > atm if typ == 'CE' else r['strike'] < atm)]
    if not cands:
        raise StrategyError('NO_DELTAS_SPOT_UNAVAILABLE')
    return min(cands, key=lambda c: (abs(abs(c[1]['delta']) - target), c[0]))[0]


def _offset(ladder, strike, n):
    ks = _strikes(ladder)
    i = ks.index(strike) + n
    if not 0 <= i < len(ks):
        raise StrategyError('WING_OUTSIDE_LISTED_STRIKES')
    return ks[i]


def template(name, ladder, lots=1, delta=0.20, wing=4):
    """Leg specs for a named template, picked from the live ladder around ATM."""
    if name not in TEMPLATES:
        raise StrategyError('UNKNOWN_TEMPLATE')
    if ladder.get('atm_strike') is None:
        raise StrategyError('ATM_UNAVAILABLE_NO_SPOT')
    atm = ladder['atm_strike']
    lots, wing = max(1, int(lots)), max(1, int(wing))
    if name == 'short_straddle':
        spec = [('SELL', atm, 'CE'), ('SELL', atm, 'PE')]
    elif name == 'iron_fly':
        spec = [('SELL', atm, 'CE'), ('SELL', atm, 'PE'), ('BUY', _offset(ladder, atm, wing), 'CE'),
                ('BUY', _offset(ladder, atm, -wing), 'PE')]
    elif name == 'short_strangle':
        spec = [('SELL', _by_delta(ladder, 'CE', delta), 'CE'), ('SELL', _by_delta(ladder, 'PE', delta), 'PE')]
    elif name == 'iron_condor':
        c, p = _by_delta(ladder, 'CE', delta), _by_delta(ladder, 'PE', delta)
        spec = [('SELL', c, 'CE'), ('SELL', p, 'PE'), ('BUY', _offset(ladder, c, wing), 'CE'), ('BUY', _offset(ladder, p, -wing), 'PE')]
    elif name == 'bull_put_spread':
        p = _by_delta(ladder, 'PE', delta)
        spec = [('SELL', p, 'PE'), ('BUY', _offset(ladder, p, -wing), 'PE')]
    else:  # bear_call_spread
        c = _by_delta(ladder, 'CE', delta)
        spec = [('SELL', c, 'CE'), ('BUY', _offset(ladder, c, wing), 'CE')]
    return [{'side': side, 'strike': k, 'option_type': typ, 'lots': lots} for side, k, typ in spec]


def analyze(ladder, specs):
    """Full analytics for leg specs [{side, lots, strike+option_type or token, price?}]."""
    if not specs:
        raise StrategyError('NO_LEGS')
    spot = (ladder.get('spot') or {}).get('value')
    expiry = ladder['expiry']
    legs, warnings = [], []
    for i, s in enumerate(specs):
        side = str(s.get('side', '')).upper()
        if side not in ('BUY', 'SELL'):
            raise StrategyError(f'LEG_{i}_SIDE_INVALID')
        q = None
        if s.get('token'):
            for r in ladder['strikes']:
                for typ in ('CE', 'PE'):
                    if r.get(typ) and str(r[typ]['instrument_token']) == str(s['token']):
                        q, strike, typ_found = r[typ], r['strike'], typ
            if q is None:
                raise StrategyError(f'LEG_{i}_TOKEN_NOT_IN_CHAIN')
            typ = typ_found
        else:
            strike, typ = float(s.get('strike') or 0), str(s.get('option_type', '')).upper()
            q = _leg_at(ladder, strike, typ)
            if q is None:
                raise StrategyError(f'LEG_{i}_NOT_IN_CHAIN:{strike:g}{typ}')
        lots = max(1, int(s.get('lots') or 1))
        lot = q.get('lot_size')
        if not lot:
            warnings.append(f'LOT_SIZE_UNKNOWN:{q["symbol"]}')
        quote = q['bid'] if side == 'SELL' else q['ask']
        price = float(s['price']) if s.get('price') else (quote if quote and quote > 0 else None)
        if price is None:
            raise StrategyError(f'LEG_{i}_NO_EXECUTABLE_QUOTE:{q["symbol"]}')
        if q.get('stale'):
            warnings.append(f'STALE_QUOTE:{q["symbol"]}')
        qty = lots * (lot or 1) * (1 if side == 'BUY' else -1)
        legs.append({'side': side, 'lots': lots, 'lot_size': lot, 'qty': qty, 'strike': strike, 'option_type': typ,
                     'expiry': expiry, 'kind': 'OPT', 'price': round(price, 2), 'mid': q.get('mid'), 'bid': q.get('bid'),
                     'ask': q.get('ask'), 'iv': q.get('iv'), 'delta_unit': q.get('delta'), 'broker': q['broker'],
                     'instrument_token': q['instrument_token'], 'symbol': q['symbol'], 'stale': q.get('stale')})

    credit = round(-sum(leg['qty'] * leg['price'] for leg in legs), 2)
    stats = expiry_payoff_stats(legs)
    greeks = {'delta': 0.0, 'gamma': 0.0, 'theta': 0.0, 'vega': 0.0, 'delta_notional': 0.0}
    complete_greeks = True
    for leg in legs:
        g = leg_greeks(leg, spot)
        if g['delta'] is None:
            complete_greeks = False
            continue
        for k in greeks:
            greeks[k] += g[k]
    atm_iv = (ladder.get('summary') or {}).get('atm_iv')
    T = years_to_expiry(expiry)
    pop = probability_of_profit(legs, spot, atm_iv, T) if spot and atm_iv else None
    curves = payoff_curves(legs, spot, points=161, span=0.08) if spot else None

    shorts = {typ: sum(-leg['qty'] for leg in legs if leg['option_type'] == typ and leg['qty'] < 0) for typ in ('CE', 'PE')}
    longs = {typ: sum(leg['qty'] for leg in legs if leg['option_type'] == typ and leg['qty'] > 0) for typ in ('CE', 'PE')}
    naked = {typ: max(shorts[typ] - longs[typ], 0) for typ in ('CE', 'PE')}
    if not settings.allow_naked_short_options and any(naked.values()):
        warnings.append('NAKED_SHORTS_WILL_BE_BLOCKED:' + ','.join(f'{t}={q}' for t, q in naked.items() if q))
    naked_margin = sum(settings.short_option_margin_pct / 100 * leg['strike'] * -leg['qty'] for leg in legs if leg['qty'] < 0)
    defined = stats is not None and stats['max_loss'] is not None
    margin = abs(stats['max_loss']) if defined else naked_margin
    max_profit = stats['max_profit'] if stats else None
    return {
        'exchange': ladder['exchange'], 'underlying': ladder['underlying'], 'expiry': expiry, 'spot': spot,
        'spot_source': (ladder.get('spot') or {}).get('source'), 'days_to_expiry': ladder.get('days_to_expiry'), 'atm_iv': atm_iv,
        'legs': legs, 'net_credit': credit, 'stats': stats, 'greeks': greeks if complete_greeks else None,
        'probability_of_profit': pop, 'curves': curves,
        'margin_estimate': round(margin, 2), 'margin_basis': 'MAX_LOSS_DEFINED_RISK' if defined else 'SHORT_OPTION_MARGIN_PCT_ESTIMATE',
        'return_on_margin': round(max_profit / margin, 4) if max_profit and margin else None,
        'naked_short_qty': naked, 'warnings': warnings,
        'execution_order': [i for i, leg in enumerate(legs) if leg['side'] == 'BUY'] + [i for i, leg in enumerate(legs) if leg['side'] == 'SELL'],
        'notes': ['Prices are executable quotes (SELL at bid, BUY at ask) unless overridden.',
                  'Probability of profit is a risk-neutral lognormal model at ATM IV, not a forecast.',
                  'Margin is an estimate; the broker margin API is authoritative.'],
    }
