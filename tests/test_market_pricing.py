import math
import time

import pytest
from conftest import expiry_in, make_tick, override

from app.clock import years_to_expiry
from app.greeks import bs_price, implied_vol
from app.market import MarketDataGateway
from app.option_chain import OptionChain
from app.scenario import revalue


def test_quality_gates():
    g = MarketDataGateway()
    assert g.ingest(make_tick())['accepted']
    assert not g.ingest(make_tick(age_ms=5000))['accepted']
    assert not g.ingest(make_tick(bid=101, ask=100))['accepted']
    future = make_tick()
    future.exchange_ts_ms += 60000
    assert 'CLOCK_SKEW_FUTURE_TIMESTAMP' in g.ingest(future)['reasons']


def test_tokens_are_isolated_per_broker():
    # Original keyed ticks by token only: Angel token 111 overwrote Zerodha token 111.
    g = MarketDataGateway()
    g.ingest(make_tick('ZERODHA', '111', ltp=100))
    g.ingest(make_tick('ANGEL', '111', ltp=5))
    assert g.tick('ZERODHA', '111').ltp == 100 and g.tick('ANGEL', '111').ltp == 5


def test_sequence_and_bounded_rejects():
    g = MarketDataGateway()
    g.ingest(make_tick(seq=2))
    assert not g.ingest(make_tick(seq=1))['accepted']
    for _ in range(5000):
        g.ingest(make_tick(age_ms=9999))
    assert len(g.rejected) == 500 and g.reject_counts['STALE_TICK'] >= 5000


def test_window_change_uses_history_not_last_two_ticks():
    g = MarketDataGateway()
    g.ingest(make_tick(ltp=100, oi=1000))
    g.ingest(make_tick(ltp=101, oi=1100))
    g.ingest(make_tick(ltp=102, oi=1200))
    dp, doi = g.window_change('ANGEL', '111', 60000)
    assert dp == pytest.approx(2.0) and doi == 200
    assert g.flow('ANGEL', '111', 60000)['classification'] == 'POSSIBLE_LONG_BUILDUP'


def test_put_call_parity_and_implied_vol():
    S, K, T, r, s = 25000, 25200, 30 / 365, 0.065, 0.14
    c, p = bs_price(S, K, T, r, s, 'CE'), bs_price(S, K, T, r, s, 'PE')
    assert c - p == pytest.approx(S - K * math.exp(-r * T), abs=1e-6)
    assert implied_vol(c, S, K, T, r, 'CE') == pytest.approx(s, abs=1e-5)
    assert implied_vol(0.0001, S, K, T, r, 'CE') is None or implied_vol(0.0001, S, K, T, r, 'CE') < 0.02


def test_full_revaluation_fails_closed_without_spot():
    exp = expiry_in(7)
    px = bs_price(25000, 25000, years_to_expiry(exp), 0.065, 0.12, 'CE')
    leg = {'qty': -75, 'price': px, 'option_type': 'CE', 'strike': 25000, 'expiry': exp, 'underlying': 'NIFTY'}
    assert revalue([leg], {})['ok'] is False
    r = revalue([leg], {'NIFTY': 25000})
    assert r['ok'] and r['worst_pnl'] < -50000


def test_max_pain_uses_option_payoffs():
    rows = [{'strike': 100.0, 'option_type': 'CE', 'oi': 10}, {'strike': 120.0, 'option_type': 'CE', 'oi': 1000},
            {'strike': 100.0, 'option_type': 'PE', 'oi': 10}, {'strike': 110.0, 'option_type': 'PE', 'oi': 0}]
    assert OptionChain.max_pain(rows) == 100.0  # original returned 120


def test_stale_threshold_is_configurable():
    g = MarketDataGateway()
    with override(data_stale_ms=10000):
        assert g.ingest(make_tick(age_ms=5000))['accepted']
