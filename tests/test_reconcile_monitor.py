import asyncio

from conftest import FakeBroker

from app.clock import trading_date
from app.ledger import Ledger, OrderLedger
from app.order_monitor import OrderMonitor
from app.reconcile import Reconciler


def remote(bo, status='OPEN', tag='', filled=0, qty=75):
    return {'broker': 'ANGEL', 'broker_order_id': bo, 'status': status, 'raw_status': status.lower(), 'filled_qty': filled,
            'qty': qty, 'avg_price': 10.0 if filled else 0.0, 'symbol': 'X', 'exchange': 'NFO', 'side': 'SELL', 'tag': tag,
            'token': '111', 'raw': {}}


def seed(l, cid, status, day=None, bo=None, tag=None, age_sec=0):
    l.create_pending_order({'client_order_id': cid, 'broker': 'ANGEL', 'exchange': 'NFO', 'symbol': 'X', 'side': 'SELL',
                            'qty': 75, 'tag': tag, 'instrument_token': '111'})
    with l.Session() as s:
        x = s.get(OrderLedger, cid)
        x.status, x.broker_order_id = status, bo
        if day:
            x.trading_date = day
        if age_sec:
            from datetime import timedelta
            x.created_at = x.created_at - timedelta(seconds=age_sec)
        s.commit()


def test_previous_day_orders_do_not_block_reconciliation_forever():
    l, b = Ledger('sqlite:///:memory:'), FakeBroker()
    seed(l, 'old-filled', 'FILLED', day='2000-01-03', bo='B-OLD')
    seed(l, 'old-open', 'OPEN', day='2000-01-03', bo='B-OLD2')
    r = asyncio.run(Reconciler().reconcile(b, l))
    assert r['ok'], r  # original: MISSING_REMOTE_ORDER for every historical order, forever
    assert l.get_order('old-open')['status'] == 'EXPIRED'


def test_ambiguous_order_resolved_by_tag():
    l, b = Ledger('sqlite:///:memory:'), FakeBroker()
    seed(l, 'amb', 'UNKNOWN', tag='IOTAG1')
    b.remote_orders = [remote('B77', 'COMPLETE' and 'FILLED', tag='IOTAG1', filled=75)]
    r = asyncio.run(Reconciler().reconcile(b, l))
    o = l.get_order('amb')
    assert r['ok'] and o['status'] == 'FILLED' and o['broker_order_id'] == 'B77'


def test_ambiguous_order_absent_after_grace_is_rejected_but_pending_within_grace_blocks():
    l, b = Ledger('sqlite:///:memory:'), FakeBroker()
    seed(l, 'young', 'UNKNOWN', tag='IOY')
    r = asyncio.run(Reconciler(grace_sec=30).reconcile(b, l))
    assert not r['ok'] and r['mismatches'][0]['type'] == 'AMBIGUOUS_ORDER_PENDING'
    seed(l, 'old', 'UNKNOWN', tag='IOO', age_sec=120)
    asyncio.run(Reconciler(grace_sec=30).reconcile(b, l))
    assert l.get_order('old')['status'] == 'REJECTED'


def test_external_orders_imported_and_today_missing_flagged():
    l, b = Ledger('sqlite:///:memory:'), FakeBroker()
    b.remote_orders = [remote('MANUAL1')]
    seed(l, 'mine', 'OPEN', day=trading_date(), bo='GONE')
    r = asyncio.run(Reconciler().reconcile(b, l))
    assert l.order_by_broker_id('ANGEL', 'MANUAL1')['origin'] == 'EXTERNAL'
    assert [m['type'] for m in r['mismatches']] == ['MISSING_REMOTE_ORDER']


def test_monitor_applies_broker_state_without_regression_and_dedupes_fills():
    l, b = Ledger('sqlite:///:memory:'), FakeBroker()
    seed(l, 'c1', 'SUBMITTED', bo='B1')
    b.remote_orders = [remote('B1', 'PARTIAL', filled=25)]
    b.remote_trades = [{'broker': 'ANGEL', 'trade_id': 'F1', 'broker_order_id': 'B1', 'qty': 25, 'price': 10.0, 'raw': {}}]
    m = OrderMonitor(type('R', (), {'configured': lambda s: [b]})(), l)
    asyncio.run(m.run_once())
    asyncio.run(m.run_once())
    o = l.get_order('c1')
    assert o['status'] == 'PARTIAL' and o['filled_qty'] == 25 and l.snapshot()['fills'] == 1
    b.remote_orders = [remote('B1', 'FILLED', filled=75)]
    asyncio.run(m.run_once())
    b.remote_orders = [remote('B1', 'OPEN')]  # stale poll must not regress a terminal order
    asyncio.run(m.run_once())
    assert l.get_order('c1')['status'] == 'FILLED'
    assert any(e['topic'] == 'ORDER_STATE_CONFLICT' for e in l.events())


def test_monitor_skips_unconfigured_brokers_and_throttles_errors():
    l = Ledger('sqlite:///:memory:')
    bad = FakeBroker()

    async def boom():
        raise RuntimeError('down')
    bad.orders = boom
    m = OrderMonitor(type('R', (), {'configured': lambda s: [bad]})(), l)
    for _ in range(5):
        asyncio.run(m.run_once())
    assert len([e for e in l.events() if e['topic'] == 'ORDER_MONITOR_ERROR']) == 1
    assert m.errors['ANGEL'] == 'down'
