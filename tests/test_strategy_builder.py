"""Option-writing strategy builder: templates from the live chain and analytics."""
import time

import pytest
from conftest import expiry_in
from starlette.testclient import TestClient

from app.clock import years_to_expiry
from app.config import settings
from app.greeks import bs_price
from app.main import create_app
from app.models import Tick

S = 25010.0
STRIKES = [24000.0 + 50 * i for i in range(41)]


def iv_for(k):
    m = (k - S) / S
    return 0.13 - 0.3 * m + 2.0 * m * m


def setup(make_terminal, spot=True):
    t, _ = make_terminal()
    exp = expiry_in(9)
    T, now = years_to_expiry(exp), int(time.time() * 1000)
    recs = []
    for k in STRIKES:
        for typ in ('CE', 'PE'):
            p = max(round(bs_price(S, k, T, settings.risk_free_rate, iv_for(k), typ), 2), 0.1)
            tok = f'{typ}{int(k)}'
            t.chain.update(Tick(broker='ANGEL', exchange='NFO', instrument_token=tok, symbol=f'NIFTYX{int(k)}{typ}', underlying='NIFTY',
                                expiry=exp, strike=k, option_type=typ, ltp=p, bid=round(p - 0.1, 2), ask=round(p + 0.1, 2), oi=1000,
                                exchange_ts_ms=now, receive_ts_ms=now))
            recs.append({'token': tok, 'symbol': f'NIFTYX{int(k)}{typ}', 'exchange': 'NFO', 'underlying': 'NIFTY', 'expiry': exp,
                         'strike': k, 'option_type': typ, 'lot_size': 75})
    t.instruments._install('ANGEL', recs, time.time())
    if spot:
        t.feed.ingest(Tick(broker='KOTAK', exchange='NSE', instrument_token='Nifty 50', symbol='NIFTY', underlying='NIFTY', ltp=S,
                           exchange_ts_ms=now, receive_ts_ms=now))
    return TestClient(create_app(t, run_background=False)), exp


def get(c, exp, name, **params):
    return c.get(f'/strategy/template/NFO/NIFTY/{exp}/{name}', params=params)


def test_templates_are_listed(make_terminal):
    c, _ = setup(make_terminal)
    names = {x['name'] for x in c.get('/strategy/templates').json()['templates']}
    assert names == {'short_straddle', 'short_strangle', 'iron_condor', 'iron_fly', 'bull_put_spread', 'bear_call_spread'}


def test_short_straddle_is_unbounded_and_flagged_naked(make_terminal):
    c, exp = setup(make_terminal)
    r = get(c, exp, 'short_straddle', lots=2).json()
    ce, pe = r['legs']
    assert (ce['side'], ce['strike'], ce['option_type']) == ('SELL', 25000.0, 'CE') and pe['option_type'] == 'PE'
    assert ce['qty'] == -150 and ce['price'] == ce['bid']  # executable: sell at bid
    assert r['net_credit'] == pytest.approx(150 * (ce['bid'] + pe['bid']), abs=0.01)
    assert r['stats']['unbounded_loss'] and r['stats']['max_profit'] == pytest.approx(r['net_credit'], abs=0.01)
    assert r['margin_basis'] == 'SHORT_OPTION_MARGIN_PCT_ESTIMATE'
    assert r['margin_estimate'] == pytest.approx(settings.short_option_margin_pct / 100 * 25000 * 300)
    assert 'NAKED_SHORTS_WILL_BE_BLOCKED:CE=150,PE=150' in r['warnings'] and r['execution_order'] == [0, 1]
    assert r['greeks']['theta'] > 0 and r['greeks']['vega'] < 0 and len(r['stats']['breakevens']) == 2


def test_iron_condor_picks_target_delta_and_is_defined_risk(make_terminal):
    c, exp = setup(make_terminal)
    r = get(c, exp, 'iron_condor', delta=0.2, wing=4).json()
    sc, sp, lc, lp = r['legs']
    assert (sc['side'], sp['side'], lc['side'], lp['side']) == ('SELL', 'SELL', 'BUY', 'BUY')
    assert sc['strike'] > S > sp['strike'] and abs(abs(sc['delta_unit']) - 0.2) < 0.06 and abs(abs(sp['delta_unit']) - 0.2) < 0.06
    assert lc['strike'] == sc['strike'] + 200 and lp['strike'] == sp['strike'] - 200 and lc['price'] == lc['ask']
    st = r['stats']
    assert not st['unbounded_loss'] and st['max_loss'] < 0 < st['max_profit'] and st['max_profit'] == pytest.approx(r['net_credit'], abs=0.01)
    assert r['margin_basis'] == 'MAX_LOSS_DEFINED_RISK' and r['margin_estimate'] == pytest.approx(-st['max_loss'])
    assert r['return_on_margin'] == pytest.approx(st['max_profit'] / r['margin_estimate'], rel=1e-3)
    assert r['execution_order'] == [2, 3, 0, 1]  # hedges first
    assert not any(w.startswith('NAKED') for w in r['warnings']) and 0 < r['probability_of_profit'] < 1
    assert r['curves']['complete'] and len(r['curves']['spots']) == 161


def test_spreads_and_iron_fly(make_terminal):
    c, exp = setup(make_terminal)
    bps = get(c, exp, 'bull_put_spread').json()
    assert [leg['option_type'] for leg in bps['legs']] == ['PE', 'PE'] and bps['stats']['max_loss'] < 0 and bps['net_credit'] > 0
    bcs = get(c, exp, 'bear_call_spread').json()
    assert [leg['side'] for leg in bcs['legs']] == ['SELL', 'BUY'] and bcs['legs'][1]['strike'] > bcs['legs'][0]['strike']
    fly = get(c, exp, 'iron_fly', wing=2).json()
    assert {leg['strike'] for leg in fly['legs']} == {24900.0, 25000.0, 25100.0} and fly['stats']['max_loss'] < 0
    assert get(c, exp, 'iron_condor', wing=20, delta=0.05).status_code == 422  # wing past the listed strikes


def test_analyze_custom_legs_by_token_and_price_override(make_terminal):
    c, exp = setup(make_terminal)
    body = {'exchange': 'NFO', 'underlying': 'NIFTY', 'expiry': exp,
            'legs': [{'side': 'SELL', 'token': 'CE25500', 'lots': 1}, {'side': 'BUY', 'strike': 25700, 'option_type': 'CE', 'price': 12.5}]}
    r = c.post('/strategy/analyze', json=body).json()
    assert r['legs'][0]['strike'] == 25500.0 and r['legs'][1]['price'] == 12.5 and r['stats']['max_loss'] < 0
    bad = c.post('/strategy/analyze', json={**body, 'legs': [{'side': 'SELL', 'strike': 99999, 'option_type': 'CE'}]})
    assert bad.status_code == 422 and 'NOT_IN_CHAIN' in bad.json()['reason']
    assert c.post('/strategy/analyze', json={**body, 'legs': []}).status_code == 422
    assert get(c, exp, 'butterfly_of_doom').status_code == 422


def test_templates_need_a_spot(make_terminal):
    c, exp = setup(make_terminal, spot=False)
    assert get(c, exp, 'short_strangle').status_code == 200  # no index tick: put-call parity still gives a spot
    assert c.get(f'/strategy/template/NFO/NIFTY/{expiry_in(30)}/short_straddle').json()['reason'] == 'ATM_UNAVAILABLE_NO_SPOT'
