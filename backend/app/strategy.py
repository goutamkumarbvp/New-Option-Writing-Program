"""Turns agent evidence into a directional VIEW (BULLISH / BEARISH / NO_VIEW).

The original produced BUY_CE/BUY_PE and then approved whatever order the
operator had submitted, even a SELL that contradicted the signal. The view is
now compared with the order's own direction in approval.py.
"""
import uuid
from dataclasses import dataclass


@dataclass
class StrategyProposal:
    strategy_id: str
    symbol: str
    action: str
    confidence: float
    max_loss: float
    evidence: dict
    order: object = None
    expected_edge: float = 0.0
    estimated_cost: float = 0.0


class StrategyEngine:
    def propose(self, ctx):
        m = ctx.get('market', {})
        sid = str(uuid.uuid4())
        symbol = m.get('symbol', 'UNKNOWN')
        order = m.get('order')
        if not m.get('live'):
            return StrategyProposal(sid, symbol, 'NO_VIEW', 0.0, 0.0, {'reason': 'DATA_UNAVAILABLE'}, order)
        by = {x['name']: x for x in ctx.get('agents', [])}
        if any(by.get(n, {}).get('state') != 'LIVE' for n in ('DIRECTION', 'FLOW', 'EXECUTION', 'RISK')):
            return StrategyProposal(sid, symbol, 'NO_VIEW', 0.0, 0.0, {'reason': 'AGENT_EVIDENCE_INCOMPLETE'}, order)
        d, f = float(by['DIRECTION']['score']), float(by['FLOW']['score'])
        ex, risk = float(by['EXECUTION']['score']), float(by['RISK']['score'])
        view = 'BULLISH' if d >= .70 and f >= .50 else 'BEARISH' if d <= .30 and f <= .50 else 'NO_VIEW'
        confidence = abs(d - .5) * 2 * ex * risk
        score = abs(d - .5) * 2 * f * ex * risk
        cost = max(0.0, float(m.get('spread_bps') or 0) / 10000)
        return StrategyProposal(sid, symbol, view, round(confidence, 4), float(m.get('max_loss', 0) or 0),
                                {'agents': ctx['agents'], 'heuristic_score': score, 'spread_cost_fraction': cost,
                                 'note': 'heuristic score, not an expected return'}, order, score, cost)
