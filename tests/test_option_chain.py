import asyncio
import time

import pytest
from conftest import angel_master_rows, expiry_in, make_tick
from starlette.testclient import TestClient

from app.clock import years_to_expiry
from app.config import settings
from app.greeks import bs_price
from app.main import create_app
from app.market import MarketDataGateway
from app.models import Tick
from app.option_chain import OptionChain, resolve_spot

S0, SIGMA = 25000.0, 0.14


def opt_tick(strike, typ, price, exp, oi=1000, volume=0, iv=None, age_ms=0, spread=0.1):
    now = int(time.time() * 1000)
    return Tick(broker='ANGEL', exchange='NFO', instrument_token=f'{typ}{int(strike)}', symbol=f'NIFTYX{int(strike)}{typ}',
                underlying='NIFTY', expiry=exp, strike=strike, option_type=typ, ltp=price, bid=round(price - spread / 2, 2),
                ask=round(price + spread / 2, 2), volume=volume, oi=oi, iv=iv, exchange_ts_ms=now - age_ms, receive_ts_ms=now - age_ms)


def bs_chain(exp, strikes, S=S0, types=('CE', 'PE')):
    """Chain priced exactly by Black-Scholes, so recovered spot and IV are known."""
    ch, T = OptionChain(), years_to_expiry(exp)
    for k in strikes:
        for typ in types:
            ch.update(opt_tick(k, typ, round(bs_price(S, k, T, settings.risk_free_rate, SIGMA, typ), 2), exp,
                               oi=1000 + int(k) % 700, volume=10))
    return ch


def test_ladder_pairs_strikes_and_takes_atm_from_spot_token(settings_override):
    settings_override(underlying_spot_tokens_json='{"NIFTY": {"ANGEL": "26000"}}')
    exp = expiry_in(7)
    ch = bs_chain(exp, [24800.0, 24900.0, 25000.0, 25100.0, 25200.0])
    feed = MarketDataGateway()
    assert feed.ingest(make_tick('ANGEL', '26000', ltp=25040.0))['accepted']
    spot = resolve_spot(feed, 'NIFTY', exp, ch.snapshot('NFO', 'NIFTY', exp))
    assert spot['source'] == 'SPOT_TOKEN' and spot['value'] == 25040.0 and spot['broker'] == 'ANGEL'
    lad = ch.ladder('NFO', 'NIFTY', exp, spot)
    assert [s['strike'] for s in lad['strikes']] == [24800.0, 24900.0, 25000.0, 25100.0, 25200.0]
    assert lad['atm_strike'] == 25000.0 and lad['live'] and lad['stale_legs'] == 0
    for row in lad['strikes']:
        assert row['CE']['itm'] is (row['strike'] < 25040) and row['PE']['itm'] is (row['strike'] > 25040)
        assert row['pcr'] == pytest.approx(row['PE']['oi'] / row['CE']['oi'])


def test_put_call_parity_spot_when_no_spot_token():
    exp = expiry_in(10)
    ch = bs_chain(exp, [24800.0, 24900.0, 25000.0, 25100.0, 25200.0])
    spot = resolve_spot(MarketDataGateway(), 'NIFTY', exp, ch.snapshot('NFO', 'NIFTY', exp))
    assert spot['source'] == 'PUT_CALL_PARITY' and spot['strike'] == 25000.0
    assert spot['value'] == pytest.approx(S0, abs=1.0)


def test_model_iv_recovers_sigma_and_greeks_have_correct_signs():
    exp = expiry_in(14)
    ch = bs_chain(exp, [24500.0, 25000.0, 25500.0])
    lad = ch.ladder('NFO', 'NIFTY', exp, {'value': S0, 'source': 'SPOT_TOKEN', 'broker': 'ANGEL', 'strike': None})
    for row in lad['strikes']:
        ce, pe = row['CE'], row['PE']
        assert ce['iv_source'] == pe['iv_source'] == 'MODEL'
        assert ce['iv'] == pytest.approx(SIGMA, abs=0.003) and pe['iv'] == pytest.approx(SIGMA, abs=0.003)
        assert 0 < ce['delta'] < 1 and -1 < pe['delta'] < 0
        assert ce['gamma'] > 0 and ce['vega'] > 0 and ce['theta'] < 0
    s = lad['summary']
    assert s['atm_iv'] == pytest.approx(SIGMA, abs=0.003)
    atm = next(r for r in lad['strikes'] if r['strike'] == 25000.0)
    assert s['atm_straddle'] == pytest.approx(atm['CE']['mid'] + atm['PE']['mid'], abs=0.01)
    assert s['expected_move_pct'] == pytest.approx(s['atm_straddle'] / S0 * 100, abs=0.01)
    assert lad['days_to_expiry'] > 13


def test_feed_iv_is_fallback_only_and_no_greeks_without_spot():
    exp = expiry_in(7)
    ch = OptionChain()
    ch.update(opt_tick(25000.0, 'CE', 120.0, exp, iv=14.0))
    lad = ch.ladder('NFO', 'NIFTY', exp, None)
    leg = lad['strikes'][0]['CE']
    assert leg['iv_source'] == 'FEED' and leg['iv'] == pytest.approx(0.14)
    assert leg['delta'] is None and leg['itm'] is None and lad['atm_strike'] is None


def test_one_sided_chain_without_spot_fails_closed():
    exp = expiry_in(7)
    ch = bs_chain(exp, [24900.0, 25000.0, 25100.0], types=('CE',))
    spot = resolve_spot(MarketDataGateway(), 'NIFTY', exp, ch.snapshot('NFO', 'NIFTY', exp))
    assert spot['value'] is None and spot['source'] is None
    lad = ch.ladder('NFO', 'NIFTY', exp, spot)
    assert lad['atm_strike'] is None and len(lad['strikes']) == 3
    assert all(r['CE']['iv'] is None and r['CE']['delta'] is None and r['PE'] is None for r in lad['strikes'])
    assert lad['summary']['atm_iv'] is None and lad['summary']['expected_move'] is None


def test_oi_change_measured_from_first_nonzero_tick_today():
    exp = expiry_in(7)
    ch = OptionChain()
    ch.update(opt_tick(25000.0, 'CE', 120.0, exp, oi=0))       # LTP-only packet: no baseline yet
    assert ch.ladder('NFO', 'NIFTY', exp)['strikes'][0]['CE']['oi_change'] is None
    ch.update(opt_tick(25000.0, 'CE', 121.0, exp, oi=1000))
    ch.update(opt_tick(25000.0, 'CE', 119.0, exp, oi=1500))
    ch.update(opt_tick(25000.0, 'PE', 90.0, exp, oi=800))
    lad = ch.ladder('NFO', 'NIFTY', exp)
    assert lad['strikes'][0]['CE']['oi_change'] == 500 and lad['strikes'][0]['PE']['oi_change'] == 0
    assert lad['summary']['ce_oi_change'] == 500 and lad['summary']['pe_oi_change'] == 0


def test_depth_window_and_stale_legs_are_flagged_not_hidden():
    exp = expiry_in(7)
    ch = bs_chain(exp, [24000.0 + 100 * i for i in range(21)])
    ch.update(opt_tick(25100.0, 'PE', 150.0, exp, age_ms=settings.data_stale_ms + 5000))
    spot = {'value': S0, 'source': 'SPOT_TOKEN', 'broker': 'ANGEL', 'strike': None}
    lad = ch.ladder('NFO', 'NIFTY', exp, spot, depth=2)
    assert [r['strike'] for r in lad['strikes']] == [24800.0, 24900.0, 25000.0, 25100.0, 25200.0]
    stale = next(r for r in lad['strikes'] if r['strike'] == 25100.0)['PE']
    assert stale['stale'] and stale['age_ms'] >= settings.data_stale_ms and lad['stale_legs'] == 1 and lad['live']
    assert len(ch.ladder('NFO', 'NIFTY', exp, spot)['strikes']) == 21


def test_index_lists_unexpired_expiries_only():
    ch = OptionChain()
    live_exp, old_exp = expiry_in(7), expiry_in(-3)
    ch.update(opt_tick(25000.0, 'CE', 120.0, live_exp))
    ch.update(opt_tick(25000.0, 'CE', 1.0, old_exp))
    assert ch.index() == [{'exchange': 'NFO', 'underlying': 'NIFTY', 'expiries': [live_exp]}]


def test_option_chain_endpoints(make_terminal):
    t, _ = make_terminal()
    exp = expiry_in(7)
    t.instruments.load_rows('ANGEL', angel_master_rows(exp))
    asyncio.run(t.handle_tick(make_tick('ANGEL', '111', ltp=100.0)))
    asyncio.run(t.handle_tick(make_tick('ANGEL', '113', ltp=80.0)))
    c = TestClient(create_app(t, run_background=False))
    idx = c.get('/option-chain').json()
    assert idx['chains'] == [{'exchange': 'NFO', 'underlying': 'NIFTY', 'expiries': [exp]}]
    assert idx['live_trading'] is False and idx['naked_short_options_blocked'] is True
    lad = c.get(f'/option-chain/NFO/NIFTY/{exp}/ladder', params={'depth': 500}).json()
    ce = next(r for r in lad['strikes'] if r['strike'] == 25000.0)['CE']
    assert ce['lot_size'] == 75 and ce['instrument_token'] == '111' and ce['broker'] == 'ANGEL'
    assert c.get(f'/option-chain/NFO/NIFTY/{exp}').json()['summary']['rows'] == 2  # original endpoint unchanged
    empty = c.get('/option-chain/NFO/NIFTY/not-a-date/ladder')
    assert empty.status_code == 200 and empty.json()['strikes'] == []


def test_frontend_has_option_chain_tab(make_terminal):
    t, _ = make_terminal()
    html = TestClient(create_app(t, run_background=False)).get('/').text
    assert 'id="chain"' in html and "'Option Chain'" in html and '/option-chain' in html
