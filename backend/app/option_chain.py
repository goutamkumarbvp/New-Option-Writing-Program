"""Option chain: latest quote per strike and the strike ladder the terminal renders.

Live ticks are enriched from the instrument master before they reach this object;
the original received bare broker ticks and therefore stayed empty in live mode.

The ladder never invents a market. Without a usable spot price it omits ATM,
model IV and Greeks instead of guessing, and stale legs are flagged, not hidden.
"""
import json
import math
import time
from collections import defaultdict
from datetime import datetime

from .clock import IST, expired, trading_date, years_to_expiry
from .config import settings
from .greeks import black_scholes, implied_vol


def _now_ms():
    return int(time.time() * 1000)


def _mid(row):
    """Two-sided mid, else LTP, else None."""
    bid, ask = row.get('bid') or 0, row.get('ask') or 0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    ltp = row.get('ltp') or 0
    return ltp if ltp > 0 else None


def _stale(row, now_ms):
    return now_ms - int(row.get('receive_ts_ms') or 0) > settings.data_stale_ms


def _r(v, n):
    return None if v is None else round(v, n)


def spot_tokens():
    """UNDERLYING_SPOT_TOKENS_JSON: {underlying: {broker: token}}. Malformed config yields {}."""
    try:
        m = json.loads(settings.underlying_spot_tokens_json or '{}')
    except ValueError:
        return {}
    return m if isinstance(m, dict) else {}


def resolve_spot(feed, underlying, expiry, rows, r=None, now_ms=None):
    """Spot for the ladder: a live spot-token tick, else a put-call parity estimate, else None.

    Parity (European, no dividends): C - P = S - K*exp(-rT), taken at the strike where the
    fresh call and put mids are closest, which is where the estimate is least sensitive to
    spreads. For dividend-paying underlyings it yields the dividend-adjusted spot, which is
    the spot consistent with the option prices themselves.
    """
    r = settings.risk_free_rate if r is None else r
    now_ms = now_ms or _now_ms()
    by_broker = spot_tokens().get(underlying)
    if isinstance(by_broker, dict) and feed is not None:
        for broker, token in by_broker.items():
            if token and feed.live_instrument(broker, token):
                tk = feed.tick(broker, token)
                if tk and tk.ltp > 0:
                    return {'value': tk.ltp, 'source': 'SPOT_TOKEN', 'broker': str(broker).upper(), 'strike': None,
                            'symbol': str(token)}
    # No configured token: any live tick of the underlying itself (a streamed index such as
    # Kotak "Nifty 50" -> NIFTY, or an instrument-master-enriched cash instrument).
    tk = feed.live_spot(underlying) if feed is not None and hasattr(feed, 'live_spot') else None
    if tk:
        return {'value': tk.ltp, 'source': 'SPOT_TOKEN', 'broker': tk.broker.upper(), 'strike': None, 'symbol': tk.instrument_token}
    try:
        T = years_to_expiry(expiry, datetime.fromtimestamp(now_ms / 1000, tz=IST))
    except (TypeError, ValueError):
        T = None
    if T:
        legs = defaultdict(dict)
        for x in rows:
            if not _stale(x, now_ms):
                legs[x['strike']][x['option_type']] = x
        best = None
        for k, pair in legs.items():
            c, p = pair.get('CE'), pair.get('PE')
            cm, pm = (_mid(c), _mid(p)) if c and p else (None, None)
            if cm and pm and (best is None or abs(cm - pm) < best[0]):
                best = (abs(cm - pm), k, cm - pm + k * math.exp(-r * T))
        if best and best[2] > 0:
            return {'value': round(best[2], 2), 'source': 'PUT_CALL_PARITY', 'broker': None, 'strike': best[1]}
    return {'value': None, 'source': None, 'broker': None, 'strike': None}


class OptionChain:
    """Latest option quotes per (exchange, underlying, expiry)."""

    def __init__(self):
        self.data = defaultdict(dict)
        # (exchange, underlying, expiry, strike, type) -> (trading_date, first non-zero OI seen that day).
        # The process does not know the exchange's previous-day OI, so OI change is measured
        # from the first tick this process saw today and is labelled that way in the UI.
        self.oi_base = {}

    def update(self, t):
        if t.underlying and t.expiry and t.strike is not None and t.option_type in ('CE', 'PE'):
            self.data[(t.exchange, t.underlying, t.expiry)][(t.strike, t.option_type)] = t.model_dump()
            if t.oi > 0:
                k = (t.exchange, t.underlying, t.expiry, t.strike, t.option_type)
                day = trading_date()
                base = self.oi_base.get(k)
                if base is None or base[0] != day:
                    self.oi_base[k] = (day, t.oi)

    def snapshot(self, exchange, underlying, expiry):
        return sorted(self.data.get((exchange, underlying, expiry), {}).values(), key=lambda x: (x['strike'], x['option_type']))

    @staticmethod
    def max_pain(rows):
        """Settlement price that minimises total option-holder payout.

        Payout at settlement K: calls pay max(K - strike, 0) * OI, puts pay max(strike - K, 0) * OI.
        The original summed |K - strike| * OI for both types, which counts out-of-the-money
        options as if they paid out.
        """
        strikes = sorted({x['strike'] for x in rows})
        if not strikes:
            return None
        def payout(k):
            return sum((max(k - x['strike'], 0) if x['option_type'] == 'CE' else max(x['strike'] - k, 0)) * x['oi'] for x in rows)
        return min(strikes, key=payout)

    def summary(self, exchange, underlying, expiry):
        r = self.snapshot(exchange, underlying, expiry)
        ce = sum(x['oi'] for x in r if x['option_type'] == 'CE')
        pe = sum(x['oi'] for x in r if x['option_type'] == 'PE')
        iv = [x['iv'] for x in r if x.get('iv') is not None and x['iv'] > 0]
        return {'rows': len(r), 'ce_oi': ce, 'pe_oi': pe, 'pcr': pe / ce if ce else None, 'max_pain': self.max_pain(r),
                'avg_iv': sum(iv) / len(iv) if iv else None}

    def chains(self):
        return [{'exchange': k[0], 'underlying': k[1], 'expiry': k[2], 'rows': len(v)} for k, v in self.data.items()]

    def index(self, now=None):
        """Underlyings with live-fed chains and their unexpired expiries, for the terminal's selectors."""
        out = defaultdict(set)
        for (ex, und, exp), legs in self.data.items():
            if legs and not expired(exp, now):
                out[(ex, und)].add(exp)
        return [{'exchange': ex, 'underlying': und, 'expiries': sorted(v)} for (ex, und), v in sorted(out.items())]

    # ------------------------------------------------------------------ ladder
    def _oi_change(self, x, day):
        base = self.oi_base.get((x['exchange'], x['underlying'], x['expiry'], x['strike'], x['option_type']))
        return x['oi'] - base[1] if base and base[0] == day else None

    def _leg(self, x, S, T, r, lookup, now_ms, day):
        mid = _mid(x)
        iv = src = None
        if S and T and mid:
            iv = implied_vol(mid, S, x['strike'], T, r, x['option_type'])
            src = 'MODEL' if iv else None
        if iv is None and x.get('iv') and x['iv'] > 0:
            # Feed IV units are not verified per broker: values above 3 are read as percentage points.
            iv, src = (x['iv'] / 100 if x['iv'] > 3 else x['iv']), 'FEED'
        g = black_scholes(S, x['strike'], T, r, iv, x['option_type']) if S and T and iv else {}
        rec = lookup(x['broker'], x['instrument_token']) if lookup else None
        bid, ask = x.get('bid') or 0, x.get('ask') or 0
        itm = None if not S else (x['strike'] < S if x['option_type'] == 'CE' else x['strike'] > S)
        return {'broker': x['broker'], 'instrument_token': x['instrument_token'], 'symbol': x['symbol'],
                'lot_size': int(rec['lot_size']) if rec and rec.get('lot_size') else None,
                'ltp': x['ltp'], 'bid': bid, 'ask': ask, 'mid': _r(mid, 2),
                'spread': round(ask - bid, 2) if bid > 0 and ask > 0 else None,
                'volume': x.get('volume') or 0, 'oi': x.get('oi') or 0, 'oi_change': self._oi_change(x, day),
                'iv': _r(iv, 6), 'iv_source': src, 'delta': _r(g.get('delta'), 6), 'gamma': _r(g.get('gamma'), 8),
                'theta': _r(g.get('theta'), 6), 'vega': _r(g.get('vega'), 6), 'itm': itm,
                'stale': _stale(x, now_ms), 'age_ms': max(now_ms - int(x.get('receive_ts_ms') or 0), 0)}

    def ladder(self, exchange, underlying, expiry, spot_info=None, lookup=None, r=None, depth=None, now_ms=None):
        """Strike ladder: CE and PE legs paired by strike, with ATM, model IV, Greeks and summary.

        spot_info comes from resolve_spot(); lookup(broker, token) returns the instrument-master
        record (for lot size); depth keeps that many strikes each side of ATM.
        """
        r = settings.risk_free_rate if r is None else r
        now_ms = now_ms or _now_ms()
        now = datetime.fromtimestamp(now_ms / 1000, tz=IST)
        rows = self.snapshot(exchange, underlying, expiry)
        spot_info = spot_info or {'value': None, 'source': None, 'broker': None, 'strike': None}
        out = {'exchange': exchange, 'underlying': underlying, 'expiry': expiry, 'generated_ms': now_ms,
               'days_to_expiry': None, 'spot': spot_info, 'atm_strike': None, 'live': False, 'stale_legs': 0,
               'summary': self.summary(exchange, underlying, expiry), 'strikes': []}
        if not rows:
            return out
        try:
            T = years_to_expiry(expiry, now)
        except (TypeError, ValueError):
            T = None
        S = spot_info.get('value') or None
        day = trading_date(now)
        pairs = defaultdict(dict)
        for x in rows:
            pairs[x['strike']][x['option_type']] = self._leg(x, S, T, r, lookup, now_ms, day)
        strikes = sorted(pairs)
        atm = min(strikes, key=lambda k: (abs(k - S), k)) if S else None
        if atm is not None and depth:
            i = strikes.index(atm)
            strikes = strikes[max(0, i - depth): i + depth + 1]
        legs = [leg for p in pairs.values() for leg in p.values()]
        ladder = []
        for k in strikes:
            ce, pe = pairs[k].get('CE'), pairs[k].get('PE')
            ladder.append({'strike': k, 'pcr': (pe['oi'] / ce['oi']) if ce and pe and ce['oi'] else None, 'CE': ce, 'PE': pe})

        ce_vol, pe_vol = _side_sum(rows, 'CE', 'volume'), _side_sum(rows, 'PE', 'volume')
        chg = {typ: [leg['oi_change'] for (k, t2), leg in _flat(pairs) if t2 == typ and leg['oi_change'] is not None]
               for typ in ('CE', 'PE')}
        atm_ce, atm_pe = (pairs[atm].get('CE'), pairs[atm].get('PE')) if atm is not None else (None, None)
        atm_ivs = [leg['iv'] for leg in (atm_ce, atm_pe) if leg and leg['iv']]
        straddle = atm_ce['mid'] + atm_pe['mid'] if atm_ce and atm_pe and atm_ce['mid'] and atm_pe['mid'] else None
        out['summary'].update({
            'ce_volume': ce_vol, 'pe_volume': pe_vol, 'pcr_volume': pe_vol / ce_vol if ce_vol else None,
            'ce_oi_change': sum(chg['CE']) if chg['CE'] else None, 'pe_oi_change': sum(chg['PE']) if chg['PE'] else None,
            'atm_iv': _r(sum(atm_ivs) / len(atm_ivs), 6) if atm_ivs else None,
            'atm_straddle': _r(straddle, 2),
            # Approximation: the ATM straddle price is roughly the market's expected move to expiry.
            'expected_move': _r(straddle, 2), 'expected_move_pct': _r(straddle / S * 100, 2) if straddle and S else None,
        })
        out.update(days_to_expiry=_r(T * 365, 2) if T else None, atm_strike=atm, strikes=ladder,
                   live=any(not leg['stale'] for leg in legs), stale_legs=sum(1 for leg in legs if leg['stale']))
        return out


def _flat(pairs):
    for k, p in pairs.items():
        for typ, leg in p.items():
            yield (k, typ), leg


def _side_sum(rows, typ, field):
    return sum(x.get(field) or 0 for x in rows if x['option_type'] == typ)
