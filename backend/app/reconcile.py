"""Broker reconciliation, scoped to the current IST trading day.

Broker order books contain only today's orders. The original compared every
order ever written to the ledger against today's book, so from the second
trading day onward every historical order became MISSING_REMOTE_ORDER and all
trading was blocked permanently.
"""
import time
from datetime import datetime

from .clock import trading_date
from .normalize import TERMINAL


class Reconciler:
    def __init__(self, grace_sec=30):
        self.grace_sec = grace_sec

    @staticmethod
    def _age(o):
        try:
            return time.time() - datetime.fromisoformat(o['created_at']).timestamp()
        except Exception:  # noqa: BLE001
            return float('inf')

    async def reconcile(self, broker, ledger):
        today = trading_date()
        try:
            remote = await broker.orders()
        except Exception as exc:  # noqa: BLE001
            result = {'ok': False, 'broker': broker.name, 'error': f'ORDER_BOOK_UNAVAILABLE:{str(exc)[:160]}', 'mismatches': []}
            ledger.record_reconciliation(broker.name, False, [result['error']])
            return result
        by_id = {o['broker_order_id']: o for o in remote}
        by_tag = {o['tag']: o for o in remote if o.get('tag')}
        mismatches, resolved, expired, imported = [], [], [], []

        # 1. Resolve ambiguous submissions (UNKNOWN / crashed PENDING_SUBMIT) by id or tag.
        for lo in ledger.ambiguous_orders(broker.name):
            ro = by_id.get(lo['broker_order_id']) if lo['broker_order_id'] else None
            ro = ro or by_tag.get(lo['tag'])
            if ro:
                status = ro['status'] if ro['status'] != 'UNKNOWN' else 'SUBMITTED'
                ledger.transition(lo['client_order_id'], status, reason='RESOLVED_BY_RECONCILIATION',
                                  broker_order_id=ro['broker_order_id'], filled_qty=ro['filled_qty'], avg_price=ro['avg_price'] or None)
                resolved.append(lo['client_order_id'])
            elif lo['trading_date'] != today:
                ledger.transition(lo['client_order_id'], 'EXPIRED', reason='AMBIGUOUS_PREVIOUS_SESSION_DAY_ORDER')
                expired.append(lo['client_order_id'])
            elif self._age(lo) > self.grace_sec:
                ledger.transition(lo['client_order_id'], 'REJECTED', reason='NOT_IN_BROKER_ORDER_BOOK_AFTER_GRACE')
                resolved.append(lo['client_order_id'])
            else:
                mismatches.append({'type': 'AMBIGUOUS_ORDER_PENDING', 'client_order_id': lo['client_order_id']})

        # 2. Import broker-side orders this terminal did not place.
        for ro in remote:
            if not ledger.order_by_broker_id(broker.name, ro['broker_order_id']) and not ledger.order_by_tag(broker.name, ro.get('tag')):
                imported.append(ledger.import_external_order(ro))

        # 3. Compare today's orders; apply broker state where the state machine allows.
        for lo in ledger.orders(broker.name, limit=100000, trading_day=today):
            bo = lo['broker_order_id']
            if not bo:
                continue
            ro = by_id.get(bo)
            if ro is None:
                mismatches.append({'type': 'MISSING_REMOTE_ORDER', 'client_order_id': lo['client_order_id'], 'broker_order_id': bo})
                continue
            if ro['status'] not in (lo['status'], 'UNKNOWN'):
                applied, prev = ledger.transition(lo['client_order_id'], ro['status'], filled_qty=ro['filled_qty'],
                                                  avg_price=ro['avg_price'] or None)
                if not applied:
                    mismatches.append({'type': 'STATE_CONFLICT', 'client_order_id': lo['client_order_id'], 'local': prev,
                                       'broker': ro['status']})

        # 4. DAY orders still working from a previous session have expired at the exchange.
        for lo in ledger.working_orders(broker.name):
            if lo['trading_date'] != today and lo['status'] not in TERMINAL:
                ledger.transition(lo['client_order_id'], 'EXPIRED', reason='DAY_ORDER_PREVIOUS_SESSION')
                expired.append(lo['client_order_id'])

        positions_count = None
        try:
            positions, _ = await broker.positions()
            positions_count = len(positions)
            for p in positions:
                ledger.upsert_position({'broker': broker.name, 'exchange': p.get('exchange', ''), 'symbol': p['symbol'],
                                        'token': p['token'], 'qty': p['qty'], 'avg_price': 0, 'pnl': p.get('pnl') or 0})
        except Exception as exc:  # noqa: BLE001 - positions are evidence for risk, not for order reconciliation
            mismatches.append({'type': 'POSITIONS_UNAVAILABLE', 'error': str(exc)[:160]})

        result = {'ok': not mismatches, 'broker': broker.name, 'trading_date': today, 'orders_count': len(remote),
                  'positions_count': positions_count, 'mismatches': mismatches, 'resolved': resolved, 'expired': expired,
                  'imported_external': imported}
        ledger.record_reconciliation(broker.name, result['ok'], mismatches)
        ledger.event('BROKER_RECONCILIATION', {k: v for k, v in result.items()})
        return result
