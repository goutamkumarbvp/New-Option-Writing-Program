import asyncio

import httpx
import pytest
from conftest import TOKEN, FakeBroker, angel_master_rows, expiry_in, make_tick, override
from starlette.testclient import TestClient

from app.ledger import Ledger
from app.main import create_app


def test_risk_monitor_hard_stop_kills_cancels_and_flattens_shorts_first(make_terminal, live, settings_override):
    settings_override(auto_flatten_on_hard_stop=True)
    t, fb = make_terminal()
    t.instruments.load_rows('ANGEL', angel_master_rows(expiry_in(7)))
    for tok, px in (('111', 100.0), ('112', 60.0)):
        asyncio.run(t.handle_tick(make_tick('ANGEL', tok, ltp=px)))
    fb.pos = [{'token': '112', 'symbol': 'NIFTYXX25500CE', 'exchange': 'NFO', 'qty': 75, 'last_price': 60.0, 'pnl': 0.0, 'day_pnl': None, 'product': 'NRML'},
              {'token': '111', 'symbol': 'NIFTYXX25000CE', 'exchange': 'NFO', 'qty': -75, 'last_price': 100.0, 'pnl': -4500.0, 'day_pnl': None, 'product': 'NRML'}]
    fb.remote_orders = [{'broker': 'ANGEL', 'broker_order_id': 'W1', 'status': 'OPEN', 'raw_status': 'open', 'filled_qty': 0, 'qty': 75,
                         'avg_price': 0, 'symbol': 'X', 'exchange': 'NFO', 'side': 'BUY', 'tag': '', 'token': '111', 'raw': {}}]
    breach = asyncio.run(t.risk_monitor.run_once())
    assert breach == 'HARD_PORTFOLIO_STOP' and t.kill.triggered and t.ledger.latest_kill()
    assert fb.cancelled == ['W1']
    sides = [o['side'] for o in fb.placed]
    assert sides == ['BUY', 'SELL']  # buy back the short before selling the hedge
    assert all(o['order_type'] == 'LIMIT' for o in fb.placed)


def test_soft_stop_alerts_once(make_terminal, live):
    t, fb = make_terminal()
    events = []

    async def emit(e):
        events.append(e['type'])
    t.risk_monitor.emit = emit
    fb.pos = [{'token': '9', 'symbol': 'S', 'exchange': 'NFO', 'qty': 0, 'last_price': 1.0, 'pnl': -2500.0, 'day_pnl': None, 'product': 'NRML'}]
    for _ in range(3):
        asyncio.run(t.risk_monitor.run_once())
    assert events.count('RISK_ALERT') == 1 and not t.kill.triggered


def client(t):
    return TestClient(create_app(t, run_background=False))


def test_dashboard_state_works(make_terminal):
    # Original returned HTTP 500 on every call (un-awaited coroutine + wrong import path).
    t, _ = make_terminal()
    r = client(t).get('/dashboard/state')
    assert r.status_code == 200 and r.json()['controls']['naked_short_options_blocked'] is True


def test_frontend_is_served(make_terminal):
    t, _ = make_terminal()
    r = client(t).get('/')
    assert r.status_code == 200 and 'IORT' in r.text


def test_cross_origin_post_and_websocket_are_rejected(make_terminal):
    t, _ = make_terminal()
    c = client(t)
    assert c.post('/kill', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert not t.kill.triggered
    with pytest.raises(Exception):
        with c.websocket_connect('/ws/events', headers={'Origin': 'https://evil.example'}):
            pass
    with c.websocket_connect('/ws/events', headers={'Origin': 'http://testserver'}) as ws:
        assert ws.receive_json()['type'] == 'STATE'


def test_emergency_stop_kill_survives_restart_and_needs_reset(tmp_path, make_terminal):
    url = f'sqlite:///{tmp_path}/l.db'
    with override(operator_api_token=TOKEN):
        t, fb = make_terminal(db_url=url)
        c = client(t)
        assert c.post('/emergency-stop/ANGEL').status_code == 401
        r = c.post('/emergency-stop/ANGEL', headers={'X-IORT-Operator-Token': TOKEN})
        assert r.json()['status'] == 'TRIGGERED'
        assert c.post('/emergency-stop/NOPE', headers={'X-IORT-Operator-Token': TOKEN}).status_code == 404
        t2, _ = make_terminal(broker=FakeBroker(), db_url=url)  # simulated restart on the same database
        assert t2.kill.triggered and t2.kill.reason == 'EMERGENCY_STOP'
        c2 = client(t2)
        assert c2.post('/kill/reset', params={'reason': 'reviewed'}).status_code == 401
        assert c2.post('/kill/reset', params={'reason': 'reviewed after incident'}, headers={'X-IORT-Operator-Token': TOKEN}).json()['status'] == 'RESET'
        t3, _ = make_terminal(broker=FakeBroker(), db_url=url)
        assert not t3.kill.triggered
        assert [r['action'] for r in Ledger(url).audit_records()][-2:] == ['EMERGENCY_STOP', 'KILL_SWITCH_RESET']


def test_tick_injection_blocked_in_live(make_terminal, live):
    t, _ = make_terminal()
    r = client(t).post('/market/tick', json=make_tick().model_dump())
    assert r.status_code == 403


def test_option_chain_populated_from_enriched_ticks(make_terminal):
    t, _ = make_terminal()
    exp = expiry_in(7)
    t.instruments.load_rows('ANGEL', angel_master_rows(exp))
    asyncio.run(t.handle_tick(make_tick('ANGEL', '111', ltp=100.0)))
    asyncio.run(t.handle_tick(make_tick('ANGEL', '113', ltp=80.0)))
    body = client(t).get(f'/option-chain/NFO/NIFTY/{exp}').json()
    assert body['summary']['rows'] == 2


def test_calculator_endpoints(make_terminal):
    t, _ = make_terminal()
    c = client(t)
    assert c.post('/algo/iceberg', json={'qty': 10, 'disclosed': 3}).json()['children'] == [3, 3, 3, 1]
    assert c.post('/hedge/delta', json={'net_delta': 100, 'hedge_delta': 1, 'lot_size': 25}).json()['ok']
    assert c.post('/risk/scenarios/full', json={'positions': [], 'spots': {}}).json()['ok'] is True
    assert c.get('/enterprise/audit/verify').json()['ok']


def test_paper_mode_never_reaches_a_broker(make_terminal):
    t, fb = make_terminal()
    r = client(t).post('/orders', json={'broker': 'ANGEL', 'exchange': 'NFO', 'symbol': 'X', 'side': 'BUY', 'qty': 75,
                                        'price': 1.0, 'instrument_token': '111'})
    assert r.json()['reason'] == 'LIVE_TRADING_DISABLED' and not fb.placed
