"""Continuous portfolio risk monitor.

The original evaluated the hard stop only when somebody submitted a NEW order,
so an open short-option book could lose without limit between orders. This loop
re-values every configured broker on a fixed interval and, on a hard breach,
engages the persisted kill switch, cancels working orders and (if enabled)
flattens positions.
"""
import asyncio
import time

from .config import settings
from .live_risk import build_snapshot


class RiskMonitor:
    def __init__(self, registry, feed, instruments, ledger, kill, emit, executor=None):
        self.registry = registry
        self.feed = feed
        self.instruments = instruments
        self.ledger = ledger
        self.kill = kill
        self.emit = emit
        self.executor = executor  # OrderService, for cancel-all / flatten
        self.snapshots = {}
        self.soft_alerted = False
        self.stop = False
        self.last_eval = None

    async def refresh(self, broker):
        snap = await build_snapshot(broker, self.feed, self.instruments, self.ledger)
        self.snapshots[broker.name] = snap
        return snap

    def fresh(self, broker_name):
        s = self.snapshots.get(broker_name.upper())
        return s if s and s.age() <= settings.risk_snapshot_max_age_sec else None

    def aggregate(self):
        snaps = [s for s in self.snapshots.values() if s.age() <= settings.risk_snapshot_max_age_sec]
        usable = [s for s in snaps if s.ok]
        return {
            'brokers': sorted(s.broker for s in snaps),
            'usable': len(usable),
            'complete': bool(snaps) and len(usable) == len(snaps) and len(snaps) == len(self.registry.configured()),
            'total_pnl': sum(s.total_pnl or 0 for s in usable),
            'day_pnl': sum(s.day_pnl or 0 for s in usable),
            'premium_exposure': sum(s.premium_exposure for s in usable),
            'short_option_notional': sum(s.short_option_notional for s in usable),
        }

    async def evaluate(self):
        agg = self.aggregate()
        self.last_eval = {'at': time.time(), **agg}
        breach = None
        if agg['total_pnl'] <= -settings.portfolio_hard_sl:
            breach = 'HARD_PORTFOLIO_STOP'
        elif agg['day_pnl'] <= -settings.max_daily_loss:
            breach = 'DAILY_LOSS_LIMIT'
        if breach and not self.kill.triggered:
            self.kill.trigger(breach, 'RISK_MONITOR')
            await self.emit({'type': 'KILL_SWITCH', 'payload': {'reason': breach, 'actor': 'RISK_MONITOR', **agg}})
            if self.executor:
                await self.executor.cancel_all_brokers('RISK_MONITOR')
                if settings.auto_flatten_on_hard_stop:
                    await self.executor.flatten_all('RISK_MONITOR')
        soft = agg['total_pnl'] <= -settings.portfolio_soft_sl
        if soft and not self.soft_alerted:
            await self.emit({'type': 'RISK_ALERT', 'payload': {'reason': 'SOFT_PORTFOLIO_STOP', **agg}})
        self.soft_alerted = soft
        return breach

    async def run_once(self):
        brokers = self.registry.configured()
        await asyncio.gather(*(self.refresh(b) for b in brokers), return_exceptions=True)
        return await self.evaluate()

    async def run(self):
        while not self.stop:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive; failures surface as stale snapshots
                self.ledger.event('RISK_MONITOR_ERROR', {'error': str(exc)[:300]})
            await asyncio.sleep(settings.risk_monitor_interval_sec)
