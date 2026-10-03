from collections import defaultdict


class OptionChain:
    """Latest option quotes per (exchange, underlying, expiry). Live ticks are
    enriched from the instrument master before they reach this object; the
    original received bare broker ticks and therefore stayed empty in live mode."""

    def __init__(self):
        self.data = defaultdict(dict)

    def update(self, t):
        if t.underlying and t.expiry and t.strike is not None and t.option_type in ('CE', 'PE'):
            self.data[(t.exchange, t.underlying, t.expiry)][(t.strike, t.option_type)] = t.model_dump()

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
