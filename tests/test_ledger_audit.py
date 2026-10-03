import json

from app.audit_chain import PersistentAuditChain
from app.ledger import AuditLedger, Ledger


def order(cid='c1'):
    return {'client_order_id': cid, 'broker': 'ANGEL', 'exchange': 'NFO', 'symbol': 'X', 'side': 'BUY', 'qty': 75, 'tag': 'IO1'}


def test_write_ahead_is_atomic_idempotency():
    l = Ledger('sqlite:///:memory:')
    assert l.create_pending_order(order()) is True
    assert l.create_pending_order(order()) is False


def test_state_machine_enforced_on_every_path():
    l = Ledger('sqlite:///:memory:')
    l.create_pending_order(order())
    assert l.transition('c1', 'SUBMITTED', broker_order_id='B1')[0]
    assert l.transition('c1', 'FILLED', filled_qty=75)[0]
    applied, prev = l.transition('c1', 'OPEN')
    assert not applied and prev == 'FILLED'
    assert l.get_order('c1')['status'] == 'FILLED'


def test_filled_qty_never_decreases():
    l = Ledger('sqlite:///:memory:')
    l.create_pending_order(order())
    l.transition('c1', 'PARTIAL', filled_qty=50)
    l.transition('c1', 'PARTIAL', filled_qty=25)
    assert l.get_order('c1')['filled_qty'] == 50


def test_fill_idempotency():
    l = Ledger('sqlite:///:memory:')
    f = {'client_order_id': 'c', 'broker_order_id': 'b', 'trade_id': 'T1', 'qty': 1, 'price': 100}
    assert l.record_fill(f) is True and l.record_fill(f) is False and l.snapshot()['fills'] == 1


def test_kill_state_and_baseline_persist():
    l = Ledger('sqlite:///:memory:')
    l.set_control('kill_switch', {'active': True, 'reason': 'X'})
    assert l.latest_kill()['reason'] == 'X'
    assert l.set_daily_baseline('ANGEL', '2026-01-01', -100) is True
    assert l.set_daily_baseline('ANGEL', '2026-01-01', -999) is False
    assert l.daily_baseline('ANGEL', '2026-01-01')['total_pnl'] == -100
    assert l.schema_drift() == []


def test_keyed_audit_chain_detects_tampering():
    l = Ledger('sqlite:///:memory:')
    a = PersistentAuditChain(l, key='secret')
    a.append('ORDER', 'OPERATOR', {'id': '1', 'px': 1.5})
    a.append('FILL', 'SYSTEM', {'qty': 75})
    assert a.verify() == {'ok': True, 'records': 2, 'keyed': True}
    with l.Session() as s:
        row = s.get(AuditLedger, 1)
        row.payload = json.dumps({'id': 'tampered', 'px': 1.5})
        s.commit()
    assert a.verify()['ok'] is False


def test_unkeyed_chain_can_be_recomputed_but_keyed_cannot():
    l = Ledger('sqlite:///:memory:')
    PersistentAuditChain(l, key='secret').append('X', 'A', {})
    # A verifier without the key cannot validate (or forge) keyed records.
    assert PersistentAuditChain(l, key='wrong').verify()['ok'] is False
