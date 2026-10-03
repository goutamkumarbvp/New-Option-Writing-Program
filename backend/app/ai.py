"""Rule-based evidence scorers ("agents").

These are transparent heuristics over a trailing window of feed data. They are
NOT a predictive model and carry no validated edge; the terminal therefore uses
them only as a configurable gate (see approval.py), never as a reason to trade.
An agent whose inputs are missing reports state UNAVAILABLE with score 0.
"""
from dataclasses import dataclass


@dataclass
class AgentResult:
    name: str
    score: float
    evidence: dict
    state: str


def _clip(x):
    return max(0.0, min(1.0, x))


class Agent:
    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        m = ctx.get('market', {})
        p = ctx.get('portfolio', {})
        live = bool(m.get('live'))
        price = m.get('price_change_pct')
        flow = (m.get('flow') or {}).get('classification', 'INSUFFICIENT_DATA')
        spread = m.get('spread_bps')
        iv_chg = m.get('iv_change_pct')
        n = self.name
        if not live:
            return AgentResult(n, 0.0, {'reason': 'DATA_UNAVAILABLE'}, 'UNAVAILABLE')
        if n == 'REGIME':
            if price is None:
                return AgentResult(n, 0.0, {'reason': 'NO_WINDOW'}, 'UNAVAILABLE')
            return AgentResult(n, _clip(.5 + price * .05), {'price_change_pct': price, 'regime': 'TREND' if abs(price) >= .5 else 'RANGE'}, 'LIVE')
        if n == 'FLOW':
            if flow == 'INSUFFICIENT_DATA':
                return AgentResult(n, 0.0, {'classification': flow}, 'UNAVAILABLE')
            score = {'POSSIBLE_LONG_BUILDUP': .8, 'POSSIBLE_SHORT_COVERING': .75, 'POSSIBLE_WRITING': .25,
                     'POSSIBLE_LONG_UNWINDING': .2}.get(flow, .5)
            return AgentResult(n, score, {'classification': flow}, 'LIVE')
        if n == 'VOLATILITY':
            if iv_chg is None:
                return AgentResult(n, 0.0, {'reason': 'IV_NOT_IN_FEED'}, 'UNAVAILABLE')
            return AgentResult(n, _clip(.5 - iv_chg * .03), {'iv_change_pct': iv_chg}, 'LIVE')
        if n == 'DIRECTION':
            if price is None:
                return AgentResult(n, 0.0, {'reason': 'NO_WINDOW'}, 'UNAVAILABLE')
            tilt = .15 if flow in ('POSSIBLE_LONG_BUILDUP', 'POSSIBLE_SHORT_COVERING') else -.15 if flow in ('POSSIBLE_WRITING', 'POSSIBLE_LONG_UNWINDING') else 0
            return AgentResult(n, _clip(.5 + price * .08 + tilt), {'price_change_pct': price, 'flow': flow}, 'LIVE')
        if n == 'STRUCTURE':
            ok = spread is not None and spread <= 50
            return AgentResult(n, .75 if ok else .3, {'spread_bps': spread}, 'LIVE')
        if n == 'EXECUTION':
            if spread is None:
                return AgentResult(n, 0.0, {'reason': 'NO_TWO_SIDED_QUOTE'}, 'UNAVAILABLE')
            return AgentResult(n, _clip(1 - spread / 100), {'spread_bps': spread}, 'LIVE')
        if n == 'RISK':
            blocked = bool(p.get('risk_blocked'))
            return AgentResult(n, 0.0 if blocked else 1.0, {'risk_blocked': blocked}, 'LIVE')
        anomaly = bool(m.get('anomaly'))
        return AgentResult(n, 0.0 if anomaly else .9, {'anomaly': anomaly}, 'LIVE')


class AgentOrchestrator:
    NAMES = ['REGIME', 'DIRECTION', 'FLOW', 'VOLATILITY', 'STRUCTURE', 'EXECUTION', 'RISK', 'ANOMALY']

    def __init__(self):
        self.agents = [Agent(n) for n in self.NAMES]

    def run(self, ctx):
        return [a.run(ctx).__dict__ for a in self.agents]
