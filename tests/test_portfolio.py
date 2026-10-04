"""Option-book analytics and the firm portfolio risk view."""
import time

import pytest
from conftest import expiry_in
from starlette.testclient import TestClient

from app.analytics import (expiry_payoff_stats, gamma_pnl, leg_greeks, payoff_curves, probability_above,
                           probability_of_profit)
from app.clock import years_to_expiry
from app.config import settings
from app.greeks import bs_price
from app.live_risk import LiveRiskSnapshot
from app.main import create_app
from app.market import MarketDataGateway
from app.models import OrderRequest, Tick
from app.option_chain import OptionChain
from app.portfolio import build_portfolio

S, IV = 25000.0, 0.14


def px(k, typ, exp, s=S, iv=IV):
    return round(bs_price(s, k, years_to_expiry(exp), settings.risk_free_rate, iv, typ), 2)


def leg(qty, k, typ, exp, price=None, iv=None):
    return {'qty': qty, 'price': price if price is not None else px(k, typ, exp), 'kind': 'OPT', 'option_type': typ,
            'strike': float(k), 'expiry': exp, 'iv': iv}


# ------------------------------------------------------------------ analytics
def test_short_call_greeks_have_writer_signs():
    exp = expiry_in(10)
    g = leg_greeks(leg(-75, 25000, 'CE', exp), S)
    assert g['iv'] == pytest.approx(IV, abs=0.002)
    assert g['delta'] < 0 and g['gamma'] < 0 and g['theta'] > 0 and g['vega'] < 0
    assert g['delta_notional'] == pytest.approx(g['delta'] * S)
    assert gamma_pnl(g['gamma'], S) == pytest.approx(0.5 * g['gamma'] * (0.01 * S) ** 2)
    assert leg_greeks(leg(-75, 25000, 'CE', exp), None)['delta'] is None
    fut = leg_greeks({'qty': 50, 'price': 25100.0, 'kind': 'FUT'}, S)
    assert fut['delta'] == 50 and fut['delta_notional'] == 50 * 25100.0


def test_short_straddle_payoff_breakevens_and_stats():
    exp = expiry_in(7)
    legs = [leg(-75, 25000, 'CE', exp, price=200.0), leg(-75, 25000, 'PE', exp, price=180.0)]
    stats = expiry_payoff_stats(legs)
    assert stats['max_profit'] == 75 * 380 and stats['unbounded_loss'] and stats['max_loss'] is None
    assert stats['breakevens'] == [24620.0, 25380.0]
    curves = payoff_curves(legs, S, points=201, span=0.05)
    assert curves['target_expiry'] == exp and curves['complete'] is False  # no IV given: 'today' cannot be valued
    assert curves['breakevens'] == pytest.approx([24620.0, 25380.0], abs=0.01)
    assert max(v for v in curves['expiry'] if v is not None) == pytest.approx(28500.0)


def test_iron_condor_is_bounded_both_ways():
    exp = expiry_in(7)
    legs = [leg(-75, 24500, 'PE', exp, 60.0), leg(75, 24300, 'PE', exp, 35.0),
            leg(-75, 25500, 'CE', exp, 55.0), leg(75, 25700, 'CE', exp, 30.0)]
    stats = expiry_payoff_stats(legs)
    credit = 60 - 35 + 55 - 30
    assert stats['max_profit'] == 75 * credit and stats['max_loss'] == -75 * (200 - credit)
    assert not stats['unbounded_loss'] and not stats['unbounded_profit'] and len(stats['breakevens']) == 2


def test_today_curve_passes_through_current_pnl_and_pop_matches_breakevens():
    exp = expiry_in(14)
    T = years_to_expiry(exp)
    # Exact model prices: a 2-decimal rounded mark would offset T+0 by up to 75 x 0.005 per leg.
    exact = {typ: bs_price(S, 25000, T, settings.risk_free_rate, IV, typ) for typ in ('CE', 'PE')}
    legs = [leg(-75, 25000, 'CE', exp, price=exact['CE'], iv=IV), leg(-75, 25000, 'PE', exp, price=exact['PE'], iv=IV)]
    c = payoff_curves(legs, S, points=121, base_pnl=1234.0)
    assert c['complete'] and c['today'][60] == pytest.approx(1234.0, abs=0.05)
    pop = probability_of_profit(legs, S, IV, T)
    lo, hi = expiry_payoff_stats(legs)['breakevens']
    assert pop == pytest.approx(probability_above(S, lo, IV, T) - probability_above(S, hi, IV, T), abs=1e-4)
    assert 0.2 < pop < 0.8
    assert expiry_payoff_stats(legs + [leg(75, 25500, 'CE', expiry_in(21), 50.0)]) is None  # mixed expiries


# -------------------------------------------------------------- portfolio
def snapshot(positions, broker='KOTAK', total=5000.0, day=1200.0):
    return LiveRiskSnapshot(broker=broker, ts=time.time(), positions=positions, total_pnl=total, day_pnl=day,
                            day_pnl_source='SOD_BASELINE')


def pos(token, qty, k, typ, exp, pnl=0.0, price=None):
    return {'token': token, 'symbol': f'NIFTY{int(k)}{typ}', 'exchange': 'NFO', 'qty': qty, 'price': price or px(k, typ, exp),
            'price_source': 'FEED', 'pnl': pnl, 'day_pnl': None, 'underlying': 'NIFTY', 'expiry': exp, 'strike': float(k),
            'option_type': typ, 'lot_size': 75, 'product': 'NRML'}


def index_feed(ltp=S):
    g = MarketDataGateway()
    now = int(time.time() * 1000)
    g.ingest(Tick(broker='KOTAK', exchange='NSE', instrument_token='Nifty 50', symbol='NIFTY', underlying='NIFTY', ltp=ltp,
                  exchange_ts_ms=now, receive_ts_ms=now))
    return g


def hedged_strangle(exp):
    return [pos('1', -75, 24500, 'PE', exp, 900.0), pos('2', 75, 24000, 'PE', exp, -300.0),
            pos('3', -75, 25500, 'CE', exp, 800.0), pos('4', 75, 26000, 'CE', exp, -200.0), pos('5', 0, 25000, 'CE', exp)]


def test_portfolio_greeks_aggregates_scenarios_and_payoff():
    exp = expiry_in(10)
    pf = build_portfolio({'KOTAK': snapshot(hedged_strangle(exp))}, index_feed(), OptionChain())
    assert len(pf['positions']) == 4 and all(p['issue'] is None and p['iv'] for p in pf['positions'])
    assert {p['lots'] for p in pf['positions']} == {-1.0, 1.0}
    u = pf['underlyings'][0]
    assert u['underlying'] == 'NIFTY' and u['spot'] == S and u['spot_source'] == 'LIVE_SPOT' and u['complete']
    assert u['theta'] > 0 and u['vega'] < 0 and u['gamma'] < 0 and u['short_option_qty'] == 150 and u['long_option_qty'] == 150
    assert u['pnl'] == 1200.0 and u['payoff']['complete'] and len(u['payoff']['breakevens']) == 2
    t = pf['totals']
    assert t['complete'] and t['positions'] == 4 and t['total_pnl'] == 5000.0 and t['day_pnl'] == 1200.0
    assert t['theta'] == pytest.approx(u['theta']) and t['delta_notional'] == pytest.approx(u['delta_notional'])
    sc = pf['scenarios']
    assert sc['ok'] and len(sc['scenarios']) == 36 and sc['worst_pnl'] < 0
    flat = next(g for g in sc['scenarios'] if g['spot_shock'] == 0 and g['vol_shock_pts'] == 0)
    assert flat['pnl'] == pytest.approx(0, abs=1)


def test_unpriceable_positions_fail_closed():
    exp = expiry_in(10)
    pf = build_portfolio({'KOTAK': snapshot([pos('1', -75, 24500, 'PE', exp), pos('9', -75, 24500, 'PE', expiry_in(-3))])},
                         MarketDataGateway(), OptionChain())
    issues = {p['token']: p['issue'] for p in pf['positions']}
    assert issues == {'1': 'SPOT_UNAVAILABLE', '9': 'EXPIRED_CONTRACT'}
    assert not pf['totals']['complete'] and not pf['underlyings'][0]['complete']
    assert pf['scenarios']['ok'] is False and pf['underlyings'][0]['payoff'] is None


def test_portfolio_endpoint(make_terminal):
    t, _ = make_terminal()
    exp = expiry_in(10)
    t.risk_monitor.snapshots['ANGEL'] = snapshot(hedged_strangle(exp), broker='ANGEL')
    t.feed = index_feed()
    body = TestClient(create_app(t, run_background=False)).get('/risk/portfolio').json()
    assert body['totals']['positions'] == 4 and body['brokers']['ANGEL']['ok'] and body['scenarios']['ok']


def test_scenario_gate_uses_streamed_index_when_no_spot_token(make_terminal):
    t, _ = make_terminal()
    exp = expiry_in(10)
    rec = {'symbol': 'NIFTYX25000CE', 'exchange': 'NFO', 'underlying': 'NIFTY', 'expiry': exp, 'strike': 25000.0,
           'option_type': 'CE', 'lot_size': 75}
    o = OrderRequest(broker='ANGEL', exchange='NFO', symbol='NIFTYX25000CE', side='SELL', qty=75, price=100.0, instrument_token='111')
    snap = snapshot([], broker='ANGEL')
    assert t.oms._scenario(o, rec, snap, px(25000, 'CE', exp), -75)['ok'] is False
    t.feed = index_feed()
    r = t.oms._scenario(o, rec, snap, px(25000, 'CE', exp), -75)
    assert r['ok'] and r['worst_pnl'] < 0
