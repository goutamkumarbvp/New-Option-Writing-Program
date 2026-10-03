"""Polls broker order books and trade books and applies them to the ledger.

All status changes go through Ledger.transition (state machine enforced), fills
are idempotent on the broker trade id, and an event is written only when
something actually changes. The original wrote one ORDER_STATUS row per order
per poll and one error row per unconfigured broker every two seconds.
"""
import asyncio
import time

from .config import settings


class OrderMonitor:
    def __init__(self, registry, ledger, emit=None, interval=None):
        self.registry = registry
        self.ledger = ledger
        self.emit = emit
        self.interval = interval
        self.stop = False
        self.errors = {}        # broker -> last error text
        self._last_error_log = {}
        self.conflicts = set()

    async def _emit(self, e):
        if self.emit:
            await self.emit(e)

    def _link(self, broker, o):
        return self.ledger.order_by_broker_id(broker, o['broker_order_id']) or self.ledger.order_by_tag(broker, o.get('tag'))

    async def poll_broker(self, broker):
        for o in await broker.orders():
            local = self._link(broker.name, o)
            if not local:
                continue  # not ours; reconciliation imports it as EXTERNAL
            if o['status'] == 'UNKNOWN':
                self.ledger.event('RAW_STATUS_UNMAPPED', {'broker': broker.name, 'broker_order_id': o['broker_order_id'],
                                                         'raw_status': o['raw_status']})
                continue
            applied, prev = self.ledger.transition(local['client_order_id'], o['status'], broker_order_id=o['broker_order_id'],
                                                   filled_qty=o['filled_qty'], avg_price=o['avg_price'] or None)
            if applied and prev != o['status']:
                payload = {'client_order_id': local['client_order_id'], 'broker': broker.name,
                           'broker_order_id': o['broker_order_id'], 'from': prev, 'to': o['status'],
                           'filled_qty': o['filled_qty'], 'avg_price': o['avg_price']}
                self.ledger.event('ORDER_STATUS', payload)
                await self._emit({'type': 'ORDER_UPDATE', 'payload': payload})
            elif not applied and prev != o['status'] and (local['client_order_id'], o['status']) not in self.conflicts:
                self.conflicts.add((local['client_order_id'], o['status']))
                self.ledger.event('ORDER_STATE_CONFLICT', {'client_order_id': local['client_order_id'], 'local': prev,
                                                           'broker': o['status'], 'raw_status': o['raw_status']})
        for t in await broker.trades():
            local = self.ledger.order_by_broker_id(broker.name, t['broker_order_id'])
            if not local:
                continue
            if self.ledger.record_fill({'client_order_id': local['client_order_id'], 'broker_order_id': t['broker_order_id'],
                                        'trade_id': t['trade_id'], 'qty': t['qty'], 'price': t['price'], 'raw': t['raw']}):
                filled = sum(f['qty'] for f in self.ledger.fills_for(local['client_order_id']))
                self.ledger.transition(local['client_order_id'], None, filled_qty=filled)
                payload = {'client_order_id': local['client_order_id'], 'broker': broker.name, 'qty': t['qty'], 'price': t['price'],
                           'trade_id': t['trade_id']}
                self.ledger.event('FILL_OBSERVED', payload)
                await self._emit({'type': 'FILL', 'payload': payload})

    async def run_once(self):
        for broker in self.registry.configured():
            try:
                await self.poll_broker(broker)
                self.errors.pop(broker.name, None)
            except Exception as exc:  # noqa: BLE001 - surfaced via errors + throttled event
                self.errors[broker.name] = str(exc)[:200]
                if time.time() - self._last_error_log.get(broker.name, 0) > 60:
                    self._last_error_log[broker.name] = time.time()
                    self.ledger.event('ORDER_MONITOR_ERROR', {'broker': broker.name, 'error': str(exc)[:300]})

    async def run(self):
        while not self.stop:
            await self.run_once()
            await asyncio.sleep(self.interval or settings.order_reconcile_interval_sec)
