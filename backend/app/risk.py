"""Second-line risk check over two evidence domains (feed health, broker book).

Both monitors run in this process on data gathered by this process, so they are
two evidence domains, not two independent risk systems. Any failure vetoes.
"""
from .config import settings


class StreamRiskMonitor:
    name = 'STREAM'

    def evaluate(self, order, portfolio, market):
        r = []
        if not market.get('live'):
            r.append('STREAM_NOT_LIVE')
        if market.get('stale') or market.get('feed_gap'):
            r.append('STREAM_HEALTH_FAILURE')
        if market.get('anomaly'):
            r.append('MARKET_ANOMALY')
        return sorted(set(r))


class PortfolioRiskMonitor:
    name = 'PORTFOLIO'

    def evaluate(self, order, portfolio, market):
        r = []
        if not portfolio.get('broker_reconciled'):
            r.append('BROKER_RECONCILIATION_REQUIRED')
        margin = portfolio.get('margin_pct')
        if margin is None:
            if settings.require_margin_evidence:
                r.append('MARGIN_EVIDENCE_UNAVAILABLE')
        elif margin > settings.max_margin_utilization_pct:
            r.append('MARGIN_LIMIT')
        if abs(portfolio.get('net_exposure', 0)) > settings.max_net_exposure:
            r.append('NET_EXPOSURE_LIMIT')
        if portfolio.get('daily_pnl', 0) <= -settings.max_daily_loss:
            r.append('DAILY_LOSS_LIMIT')
        if portfolio.get('net_pnl', 0) <= -settings.portfolio_soft_sl:
            r.append('SOFT_PORTFOLIO_STOP')
        if portfolio.get('net_pnl', 0) <= -settings.portfolio_hard_sl:
            r.append('HARD_PORTFOLIO_STOP')
        if order is not None and order.qty > settings.max_position_qty:
            r.append('POSITION_LIMIT')
        if order is not None and order.price and order.price * order.qty > settings.max_order_value:
            r.append('ORDER_VALUE_LIMIT')
        if portfolio.get('slippage_bps', 0) > settings.max_slippage_bps:
            r.append('SLIPPAGE_LIMIT')
        return sorted(set(r))


class ProductionDualRiskEngine:
    def __init__(self):
        self.stream = StreamRiskMonitor()
        self.portfolio = PortfolioRiskMonitor()

    def evaluate_context(self, ctx, p):
        a = self.stream.evaluate(p.order, dict(ctx.portfolio), dict(ctx.market))
        b = self.portfolio.evaluate(p.order, dict(ctx.portfolio), dict(ctx.market))
        reasons = sorted(set(a + b))
        if not ctx.broker_state.get('reconciled', ctx.portfolio.get('broker_reconciled', False)):
            reasons.append('BROKER_STATE_NOT_RECONCILED')
        reasons = sorted(set(reasons))
        net = ctx.portfolio.get('net_pnl', 0)
        return {'approved': not reasons, 'reasons': reasons, 'risk_state': 'APPROVED' if not reasons else 'BLOCKED',
                'soft_sl_distance': max(0, settings.portfolio_soft_sl + net), 'hard_sl_distance': max(0, settings.portfolio_hard_sl + net),
                'monitor_agreement': (not a) == (not b), 'monitor_a': a, 'monitor_b': b}
