from .config import settings

class StreamRiskMonitor:
    name='STREAM'
    def evaluate(self, order, portfolio, market):
        r=[]
        if not market.get('live'): r.append('STREAM_NOT_LIVE')
        if market.get('stale') or market.get('feed_gap'): r.append('STREAM_HEALTH_FAILURE')
        if market.get('anomaly'): r.append('MARKET_ANOMALY')
        return sorted(set(r))

class PortfolioRiskMonitor:
    name='PORTFOLIO'
    def evaluate(self, order, portfolio, market):
        r=[]
        if not portfolio.get('broker_reconciled'): r.append('BROKER_RECONCILIATION_REQUIRED')
        if portfolio.get('margin_pct',0)>settings.max_margin_utilization_pct: r.append('MARGIN_LIMIT')
        if abs(portfolio.get('net_exposure',0))>settings.max_net_exposure: r.append('NET_EXPOSURE_LIMIT')
        if portfolio.get('daily_pnl',0)<=-settings.max_daily_loss: r.append('DAILY_LOSS_LIMIT')
        if portfolio.get('net_pnl',0)<=-settings.portfolio_soft_sl: r.append('SOFT_PORTFOLIO_STOP')
        if portfolio.get('net_pnl',0)<=-settings.portfolio_hard_sl: r.append('HARD_PORTFOLIO_STOP')
        if order and order.qty>settings.max_position_qty: r.append('POSITION_LIMIT')
        if order and order.price and order.price*order.qty>settings.max_order_value: r.append('ORDER_VALUE_LIMIT')
        if portfolio.get('slippage_bps',0)>settings.max_slippage_bps: r.append('SLIPPAGE_LIMIT')
        return sorted(set(r))

class ProductionDualRiskEngine:
    """Two different evidence domains: feed health and broker/portfolio state.
    Any disagreement or failure is a veto. No optimistic merge is allowed.
    """
    def __init__(self):
        self.stream=StreamRiskMonitor(); self.portfolio=PortfolioRiskMonitor()
    def evaluate_context(self,ctx,p):
        stream_market=dict(ctx.market)
        broker_portfolio=dict(ctx.portfolio)
        a=self.stream.evaluate(p.order, broker_portfolio, stream_market)
        b=self.portfolio.evaluate(p.order, broker_portfolio, stream_market)
        # Agreement means both monitors independently observe the same safe state,
        # not merely that they produced identical Python lists.
        safe_a=(len(a)==0); safe_b=(len(b)==0)
        agreement=(safe_a==safe_b)
        reasons=sorted(set(a+b))
        if not agreement: reasons.append('RISK_MONITOR_DISAGREEMENT')
        if safe_a and safe_b and not ctx.broker_state.get('reconciled', broker_portfolio.get('broker_reconciled',False)):
            reasons.append('BROKER_STATE_NOT_RECONCILED')
        return {'approved':not reasons,'reasons':sorted(set(reasons)),
                'risk_state':'APPROVED' if not reasons else 'BLOCKED',
                'soft_sl_distance':max(0,settings.portfolio_soft_sl+ctx.portfolio.get('net_pnl',0)),
                'hard_sl_distance':max(0,settings.portfolio_hard_sl+ctx.portfolio.get('net_pnl',0)),
                'monitor_agreement':agreement,'monitor_a':a,'monitor_b':b}
