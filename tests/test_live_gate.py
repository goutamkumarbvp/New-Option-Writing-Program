"""Per-order cancel and the guarded live-gate runner, in simulated live mode with a fake broker."""
import asyncio
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from conftest import TOKEN, FakeBroker, angel_master_rows, expiry_in, make_tick

from app.clock import IST
from app.main import create_app

spec = importlib.util.spec_from_file_location('live_gate', Path(__file__).resolve().parents[1] / 'scripts' / 'live_gate.py')
live_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live_gate)

SYMBOL = 'NIFTYXX25000CE'
OPEN_TIME = datetime(2026, 10, 6, 10, 0, tzinfo=IST)   # Tuesday, NSE open
NIGHT = datetime(2026, 10, 6, 2, 0, tzinfo=IST)


class GateBroker(FakeBroker):
    """Fake broker whose cancel really cancels the resting order, like a live one."""

    async def cancel(self, bo):
        self.cancelled.append(bo)
        for o in self.remote_orders:
            if o['broker_order_id'] == bo:
                o['status'], o['raw_status'] = 'CANCELLED', 'cancelled'
        return {'status': 'CANCEL_REQUESTED'}


@pytest.fixture
def env(make_terminal, live, settings_override, tmp_path):
    settings_override(data_stale_ms=60000)
    t, fb = make_terminal(broker=GateBroker('ANGEL'))
    t.instruments.load_rows('ANGEL', angel_master_rows(expiry_in(7)))
    for tok, px in (('111', 100.0), ('112', 60.0), ('113', 80.0), ('26000', 25000.0)):
        asyncio.run(t.handle_tick(make_tick('ANGEL', tok, ltp=px)))
    return t, fb, create_app(t, run_background=False)


def gate_run(t, app, tmp_path, coro_fn, now=OPEN_TIME, confirm=SYMBOL, token=TOKEN):
    out = []

    async def hook():
        await t.order_monitor.run_once()

    async def nosleep(_):
        return None

    async def go():
        await t.risk_monitor.run_once()
        async with live_gate.Gate('http://test', token, 'ANGEL', out_dir=tmp_path / 'reports', transport=httpx.ASGITransport(app=app),
                                  now=lambda: now, confirm=lambda prompt: confirm, sleep=nosleep, poll_hook=hook,
                                  poll_timeout=2, out=out.append) as g:
            return await coro_fn(g)
    return asyncio.run(go()), out


def evidence(tmp_path):
    return json.loads(next((tmp_path / 'reports').glob('live_gate_*.json')).read_text())


# ------------------------------------------------------------------ cancel endpoint
def post(app, path, headers=None, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            return await c.post(path, headers=headers or {}, **kw)
    return asyncio.run(go())


def test_cancel_endpoint_cancels_one_working_order_and_audits(env):
    t, fb, app = env
    h = {'X-IORT-Operator-Token': TOKEN}
    r = post(app, '/orders', h, json={'broker': 'ANGEL', 'exchange': 'NFO', 'symbol': SYMBOL, 'side': 'BUY', 'qty': 75, 'price': 99.0,
                                      'order_type': 'LIMIT', 'instrument_token': '111', 'client_order_id': 'C-1'}).json()
    assert r['status'] == 'SUBMITTED'
    assert post(app, '/orders/C-1/cancel').status_code == 401
    assert post(app, '/orders/NOPE/cancel', h).status_code == 404
    c = post(app, '/orders/C-1/cancel', h)
    assert c.status_code == 200 and c.json()['result'] == 'CANCEL_REQUESTED' and fb.cancelled == [r['broker_order_id']]
    assert t.ledger.audit_records()[-1]['action'] == 'ORDER_CANCEL_REQUEST'
    asyncio.run(t.order_monitor.run_once())
    assert t.ledger.get_order('C-1')['status'] == 'CANCELLED'
    again = post(app, '/orders/C-1/cancel', h)
    assert again.status_code == 409 and again.json()['reason'] == 'ORDER_NOT_WORKING'


# ------------------------------------------------------------------ Gate A
def test_preflight_passes_in_a_ready_live_terminal(env, tmp_path):
    t, fb, app = env
    (ok, checks, contract, _), out = gate_run(t, app, tmp_path, lambda g: g.preflight('111', max_premium=10000))
    failed = [c for c in checks if not c['ok']]
    assert ok, failed
    assert contract['symbol'] == SYMBOL and contract['lot_size'] == 75
    assert any(line.startswith('  MANUAL') for line in out) and evidence(tmp_path)['steps'][-1]['status'] == 'PASS'


def test_preflight_fails_closed_at_night_and_on_premium_and_without_token(env, tmp_path):
    t, fb, app = env
    (ok, checks, _, _), _ = gate_run(t, app, tmp_path, lambda g: g.preflight('111', max_premium=2000), now=NIGHT)
    bad = {c['check'] for c in checks if not c['ok']}
    assert not ok and 'NSE session open (holidays not modelled)' in bad and 'One lot costs at most Rs 2,000' in bad
    (ok, checks, _, _), _ = gate_run(t, app, tmp_path, lambda g: g.preflight('111', max_premium=10000), token='')
    assert not ok and 'Operator token available to this script' in {c['check'] for c in checks if not c['ok']}


# ------------------------------------------------------------------ Gate B
def test_order_cancel_sends_one_resting_buy_then_cancels_and_reconciles(env, tmp_path):
    t, fb, app = env
    status, out = gate_run(t, app, tmp_path, lambda g: g.order_cancel('111', True, max_premium=10000))
    assert status == 'PASS', out
    assert len(fb.placed) == 1
    o = fb.placed[0]
    assert (o['side'], o['qty'], o['order_type'], o['price']) == ('BUY', 75, 'LIMIT', 96.0)  # 4% under LTP, below the bid
    assert fb.cancelled == [fb.remote_orders[0]['broker_order_id']]
    steps = {s['step']: s for s in evidence(tmp_path)['steps']}
    assert steps['B.acknowledged']['broker_order_id'] and steps['B.reconciled']['status'] == 'PASS'
    assert steps['B.order_cancel']['final_status'] == 'CANCELLED'


@pytest.mark.parametrize('kw,why', [
    ({'acknowledged': False}, '--i-understand-real-orders'),
    ({'confirm': 'WRONG'}, 'confirmation did not match'),
    ({'now': NIGHT}, 'preflight failed'),
    ({'max_premium': 2000}, 'preflight failed'),
])
def test_order_cancel_refuses_and_sends_nothing(env, tmp_path, kw, why):
    t, fb, app = env
    ack, mp = kw.get('acknowledged', True), kw.get('max_premium', 10000)
    status, out = gate_run(t, app, tmp_path, lambda g: g.order_cancel('111', ack, max_premium=mp),
                           now=kw.get('now', OPEN_TIME), confirm=kw.get('confirm', SYMBOL))
    assert status == 'BLOCKED' and not fb.placed and any(why in line for line in out)


# ------------------------------------------------------------------ Gate C
def test_kill_switch_and_reject_paths_never_reach_the_broker(env, tmp_path):
    t, fb, app = env
    assert gate_run(t, app, tmp_path, lambda g: g.kill_test('111'))[0] == 'PASS'
    assert not t.kill.triggered
    assert gate_run(t, app, tmp_path, lambda g: g.reject_test('111'))[0] == 'PASS'
    assert not fb.placed


def test_recovery_kill_state_survives_restart(make_terminal, live, settings_override, tmp_path):
    settings_override(data_stale_ms=60000)
    url = f'sqlite:///{tmp_path}/gate.db'
    t1, _ = make_terminal(broker=GateBroker('ANGEL'), db_url=url)
    assert gate_run(t1, create_app(t1, run_background=False), tmp_path, lambda g: g.recovery_arm())[0] == 'PASS'
    t2, _ = make_terminal(broker=GateBroker('ANGEL'), db_url=url)  # simulated restart on the same database
    assert t2.kill.triggered
    assert gate_run(t2, create_app(t2, run_background=False), tmp_path, lambda g: g.recovery_verify())[0] == 'PASS'
    assert not t2.kill.triggered


def test_report_summarises_gates(env, tmp_path):
    t, fb, app = env
    gate_run(t, app, tmp_path, lambda g: g.preflight('111', max_premium=10000))
    gate_run(t, app, tmp_path, lambda g: g.order_cancel('111', True, max_premium=10000))
    gate_run(t, app, tmp_path, lambda g: g.kill_test('111'))
    summary, out = gate_run(t, app, tmp_path, lambda g: asyncio.sleep(0, g.report()))
    assert summary['gates'] == {'A': 'PASS', 'B': 'PASS', 'C': 'INCOMPLETE'} and not summary['automated_complete']
    assert summary['operator_signoff'] == {'name': None, 'date': None, 'signature': None}
