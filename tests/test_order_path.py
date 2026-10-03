"""End-to-end order path over HTTP in simulated live mode with a fake broker."""
import asyncio

import httpx
import pytest
from conftest import TOKEN, angel_master_rows, expiry_in, make_tick

from app.main import create_app

H = {'X-IORT-Operator-Token': TOKEN}


def order(**kw):
    o = {'broker': 'ANGEL', 'exchange': 'NFO', 'symbol': 'NIFTYXX25000CE', 'side': 'BUY', 'qty': 75, 'price': 100.0,
         'order_type': 'LIMIT', 'instrument_token': '111'}
    o.update(kw)
    return o


@pytest.fixture
def env(make_terminal, live):
    t, fb = make_terminal()
    t.instruments.load_rows('ANGEL', angel_master_rows(expiry_in(7)))
    for tok, px in (('111', 100.0), ('112', 60.0), ('113', 80.0), ('26000', 25000.0)):
        asyncio.run(t.handle_tick(make_tick('ANGEL', tok, ltp=px)))
    app = create_app(t, run_background=False)
    return t, fb, app


def post(app, path, json=None, headers=H, **kw):
    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            return await c.post(path, json=json, headers=headers, **kw)
    return asyncio.run(go())


def submit(app, **kw):
    return post(app, '/orders', order(**kw)).json()


def test_happy_path_writes_ahead_and_records_broker_id(env):
    t, fb, app = env
    r = submit(app, client_order_id='OK-1')
    assert r['status'] == 'SUBMITTED', r
    o = t.ledger.get_order('OK-1')
    assert o['broker_order_id'] == r['broker_order_id'] and o['tag'] and o['arrival_price'] == pytest.approx(100.05)
    assert fb.placed[0]['tag'] == o['tag']
    assert t.audit.verify()['ok']


def test_concurrent_duplicate_client_order_id_places_once(env):
    t, fb, app = env
    fb.place_delay = 0.2

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as c:
            return await asyncio.gather(*(c.post('/orders', json=order(client_order_id='DUP-1'), headers=H) for _ in range(3)))
    statuses = sorted(r.json()['status'] for r in asyncio.run(go()))
    assert len(fb.placed) == 1, statuses  # original: one key -> several live orders
    assert statuses.count('SUBMITTED') == 1


def test_ambiguous_timeout_marks_unknown_and_blocks_broker(env):
    t, fb, app = env
    fb.place_exc = httpx.ReadTimeout('slow')
    r = submit(app, client_order_id='AMB-1')
    assert r['status'] == 'UNKNOWN' and 'action_required' in r
    fb.place_exc = None
    assert submit(app)['reason'] == 'AMBIGUOUS_ORDERS_UNRESOLVED'


@pytest.mark.parametrize('kw,reason', [
    ({'order_type': 'MARKET', 'price': None}, 'MARKET_ORDERS_DISABLED'),
    ({'price': 150.0}, 'PRICE_COLLAR_BREACH'),
    ({'qty': 50}, 'LOT_SIZE_VIOLATION'),
    ({'symbol': 'BANKNIFTYXX'}, 'INSTRUMENT_SYMBOL_MISMATCH'),
    ({'instrument_token': '999'}, 'INSTRUMENT_MASTER_TOKEN_NOT_FOUND'),
    ({'qty': 7500}, 'ORDER_VALUE_LIMIT'),
])
def test_pretrade_rejections(env, kw, reason):
    t, fb, app = env
    r = submit(app, **kw)
    assert r['status'] == 'BLOCKED' and r['reason'] == reason, r
    assert not fb.placed


def test_naked_short_option_blocked_but_hedged_spread_allowed(env):
    t, fb, app = env
    r = submit(app, side='SELL', price=99.95)
    assert r['reason'] == 'NAKED_SHORT_OPTION_BLOCKED', r
    fb.pos = [{'token': '112', 'symbol': 'NIFTYXX25500CE', 'exchange': 'NFO', 'qty': 75, 'last_price': 60.0, 'pnl': 0.0,
               'day_pnl': None, 'product': 'NRML'}]
    asyncio.run(t.handle_tick(make_tick('ANGEL', '112', ltp=60.0)))
    t.risk_monitor.snapshots.clear()
    r = submit(app, side='SELL', price=99.95)
    assert r['status'] == 'SUBMITTED', r


def test_post_trade_margin_is_checked(env, settings_override):
    t, fb, app = env
    settings_override(allow_naked_short_options=True, max_short_option_notional=10**9, max_order_value=10**9)
    fb.margin_value = {'used': 600000.0, 'available': 400000.0, 'pct': 60.0}
    r = submit(app, side='SELL', price=99.95, qty=750)
    # 18% of 25000 x 750 = 3.375 lakh more margin -> 93.75% post-trade > 70%
    assert r['reason'] == 'MARGIN_LIMIT' and r['post_trade_margin_pct'] > 70, r


def test_position_limit_counts_working_orders(env, settings_override):
    t, fb, app = env
    settings_override(max_position_qty=100, max_order_value=10**9)
    assert submit(app, qty=75)['status'] == 'SUBMITTED'
    assert submit(app, qty=75)['reason'] == 'POSITION_LIMIT'


def test_self_trade_against_own_working_order(env, settings_override):
    t, fb, app = env
    settings_override(allow_naked_short_options=True)
    assert submit(app, price=100.0)['status'] == 'SUBMITTED'
    r = submit(app, side='SELL', price=99.95)
    assert r['reason'] == 'SELF_TRADE_RISK', r


def test_hard_stop_engages_persisted_kill(env):
    t, fb, app = env
    fb.pos = [{'token': '111', 'symbol': 'NIFTYXX25000CE', 'exchange': 'NFO', 'qty': 0, 'last_price': 100.0, 'pnl': -5000.0,
               'day_pnl': None, 'product': 'NRML'}]
    r = submit(app)
    assert r['reason'] == 'HARD_PORTFOLIO_STOP'
    assert t.kill.triggered and t.ledger.latest_kill()['reason'] == 'HARD_PORTFOLIO_STOP'
    assert submit(app)['reason'] == 'KILL_SWITCH'


def test_sod_baseline_daily_pnl_blocked_when_authoritative_required(env, settings_override):
    t, fb, app = env
    settings_override(require_authoritative_daily_pnl=True)
    r = submit(app)
    assert r['reason'] == 'DAILY_PNL_NOT_AUTHORITATIVE', r


def test_ai_gate_vetoes_order_contradicting_confident_signal(env):
    t, fb, app = env
    for px, oi in ((100, 1000), (98, 1100), (96, 1200), (95, 1300)):  # falling premium + rising OI = writing (bearish for CE)
        asyncio.run(t.handle_tick(make_tick('ANGEL', '111', ltp=px, oi=oi)))
    r = submit(app, price=95.0)
    assert r['reason'] == 'DECISION_BLOCKED' and 'AI_SIGNAL_CONTRADICTS_ORDER' in r['approval'], r


def test_operator_token_required_in_live(env):
    t, fb, app = env
    r = post(app, '/orders', order(), headers={})
    assert r.json()['reason'] == 'OPERATOR_AUTH_REQUIRED'


def test_missing_margin_evidence_blocks(env):
    t, fb, app = env
    fb.margin_exc = RuntimeError('MARGIN_DATA_UNAVAILABLE')
    t.risk_monitor.snapshots.clear()
    assert submit(app)['reason'] == 'MARGIN_EVIDENCE_UNAVAILABLE'


def test_unvalued_position_blocks(env):
    t, fb, app = env
    fb.pos = [{'token': '555', 'symbol': 'UNSUBSCRIBED', 'exchange': 'NFO', 'qty': -75, 'last_price': 10.0, 'pnl': 0.0,
               'day_pnl': None, 'product': 'NRML'}]
    r = submit(app)
    assert r['reason'] == 'LIVE_RISK_EVIDENCE_FAILED' and any('LIVE_LTP_REQUIRED' in i for i in r['issues'])


def test_validation_rejects_bad_vocabulary(env):
    t, fb, app = env
    assert post(app, '/orders', order(side='HOLD')).status_code == 422
    assert post(app, '/orders', order(exchange='NYSE')).status_code == 422
    assert post(app, '/orders', order(order_type='LIMIT', price=None)).status_code == 422


def test_kill_engaged_during_pretrade_stops_the_send(env):
    t, fb, app = env
    original = t.oms._pretrade

    async def pretrade_then_kill(*a, **k):
        ctx = await original(*a, **k)
        t.kill.trigger('RISK_MONITOR_RACE', 'RISK_MONITOR')
        return ctx
    t.oms._pretrade = pretrade_then_kill
    assert submit(app)['reason'] == 'KILL_SWITCH' and not fb.placed
