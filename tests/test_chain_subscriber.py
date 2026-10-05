"""Option chain auto-subscription around spot (Kotak)."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from conftest import expiry_in

from app.broker_streams import KotakStreamWorker
from app.chain_subscriber import BAND, ChainSubscriber, auto_chain_config, center_spot, plan_chain
from app.instruments import InstrumentMaster
from app.market import MarketDataGateway
from app.models import Tick
from app.option_chain import OptionChain
from app.stream_manager import StreamManager

pytest.importorskip('neo_api_client.websocket.feed')
from neo_api_client.websocket.feed import WsToken  # noqa: E402

E1, E2, E3 = expiry_in(3), expiry_in(10), expiry_in(17)
STRIKES = [24000 + 50 * i for i in range(41)]  # 24000 .. 26000


def records(expiries=(E1, E2, E3), underlying='NIFTY', exchange='NFO'):
    out, n = [], 100000
    for e in expiries:
        for k in STRIKES:
            for typ in ('CE', 'PE'):
                n += 1
                out.append({'token': str(n), 'symbol': f'{underlying}{k}{typ}', 'exchange': exchange, 'underlying': underlying,
                            'expiry': e, 'strike': float(k), 'option_type': typ, 'lot_size': 75})
    return out


def strikes_of(pairs, recs):
    by = {r['token']: r for r in recs}
    return sorted({by[t]['strike'] for _, t in pairs})


def test_config_is_validated_and_clamped(settings_override):
    settings_override(kotak_auto_chain_json='{"nifty": {"expiries": 9, "strikes": 500}, "BANKNIFTY": {}, "BAD": {"strikes": "x"}}')
    assert auto_chain_config() == {'NIFTY': {'expiries': 6, 'strikes': 60}, 'BANKNIFTY': {'expiries': 1, 'strikes': 15}}
    settings_override(kotak_auto_chain_json='not json')
    assert auto_chain_config() == {}


def test_plan_takes_nearest_expiries_and_strikes_around_atm():
    recs = records()
    pairs, meta = plan_chain(recs, 'NIFTY', 25012.0, expiries=2, strikes=2)
    assert meta['expiries'] == [E1, E2] and meta['atm'] == {E1: 25000.0, E2: 25000.0}
    assert strikes_of(pairs, recs) == [24900.0, 24950.0, 25000.0, 25050.0, 25100.0]
    assert len(pairs) == 5 * 2 * 2 and all(seg == 'nse_fo' for seg, _ in pairs)
    assert min(pairs.values()) == 0 and max(pairs.values()) == 2


def test_hysteresis_keeps_recently_subscribed_strikes():
    recs = records(expiries=(E1,))
    by_strike = {(r['strike'], r['option_type']): ('nse_fo', r['token']) for r in recs}
    near, far = by_strike[(25000.0 + 50 * (2 + BAND), 'CE')], by_strike[(25000.0 + 50 * (3 + BAND), 'CE')]
    pairs, _ = plan_chain(recs, 'NIFTY', 25012.0, 1, 2, current=frozenset({near, far}))
    assert near in pairs and far not in pairs
    assert by_strike[(25000.0 + 50 * 3, 'CE')] not in plan_chain(recs, 'NIFTY', 25012.0, 1, 2)[0]


def test_expired_and_foreign_contracts_are_ignored():
    recs = records(expiries=(expiry_in(-2), E1)) + records(expiries=(E1,), underlying='BANKNIFTY')
    pairs, meta = plan_chain(recs, 'NIFTY', 25012.0, 3, 1)
    assert meta['expiries'] == [E1] and len(pairs) == 3 * 2


def spot_tick(ltp, age_ms=0):
    now = int(time.time() * 1000)
    return Tick(broker='KOTAK', exchange='NSE', instrument_token='Nifty 50', symbol='NIFTY', underlying='NIFTY', ltp=ltp,
                exchange_ts_ms=now - age_ms, receive_ts_ms=now - age_ms)


def test_centre_prefers_live_index_then_last_index_then_median(settings_override):
    recs, chain = records(expiries=(E1,)), OptionChain()
    feed = MarketDataGateway()
    assert center_spot(feed, chain, 'NIFTY', recs) == (25000.0, 'MEDIAN_STRIKE')
    feed.ingest(spot_tick(25111.0))
    assert center_spot(feed, chain, 'NIFTY', recs) == (25111.0, 'LIVE_SPOT')
    settings_override(data_stale_ms=1)
    time.sleep(0.01)
    assert center_spot(feed, chain, 'NIFTY', recs) == (25111.0, 'LAST_SPOT')
    assert center_spot(MarketDataGateway(), chain, 'NIFTY', []) == (None, None)


class FakeWorker:
    broker = 'KOTAK'

    def __init__(self):
        self.dynamic, self.calls = set(), []

    async def set_dynamic(self, pairs):
        add, remove = pairs - self.dynamic, self.dynamic - pairs
        self.dynamic = set(pairs)
        self.calls.append((len(add), len(remove)))
        return {'added': len(add), 'removed': len(remove), 'total': len(pairs), 'connected': True}


def subscriber(tmp_path, positions=()):
    im = InstrumentMaster(tmp_path / 'd')
    recs = records(expiries=(E1, E2))
    im._install('KOTAK', recs, time.time())
    feed, worker = MarketDataGateway(), FakeWorker()
    feed.ingest(spot_tick(25012.0))
    snap = SimpleNamespace(positions=[{'token': t, 'qty': q, 'exchange': 'NFO'} for t, q in positions])
    rm = SimpleNamespace(snapshots={'KOTAK': snap})
    sm = SimpleNamespace(workers=[worker])
    return ChainSubscriber(im, feed, OptionChain(), lambda: sm, rm), worker, recs


def test_subscriber_sends_window_plus_positions_and_reports_state(tmp_path, settings_override):
    settings_override(kotak_auto_chain_json='{"NIFTY": {"expiries": 1, "strikes": 2}}')
    far_put = next(r for r in records(expiries=(E1, E2)) if r['expiry'] == E2 and r['strike'] == 24000.0 and r['option_type'] == 'PE')
    sub, worker, recs = subscriber(tmp_path, positions=[(far_put['token'], -75), ('999999', 0)])
    change = asyncio.run(sub.run_once())
    assert change['added'] == 5 * 2 + 1 and ('nse_fo', far_put['token']) in worker.dynamic
    st = sub.state
    assert st['enabled'] and st['subscribed'] == 11 and st['positions'] == 1 and not st['capped']
    assert st['underlyings']['NIFTY']['atm'] == {E1: 25000.0} and st['underlyings']['NIFTY']['spot_source'] == 'LIVE_SPOT'
    assert asyncio.run(sub.run_once())['added'] == 0 and worker.calls[-1] == (0, 0)


def test_cap_keeps_positions_first_then_nearest_strikes(tmp_path, settings_override):
    settings_override(kotak_auto_chain_json='{"NIFTY": {"expiries": 2, "strikes": 10}}', kotak_auto_chain_max_tokens=5)
    far = next(r for r in records(expiries=(E1, E2)) if r['strike'] == 26000.0 and r['option_type'] == 'CE' and r['expiry'] == E1)
    sub, worker, recs = subscriber(tmp_path, positions=[(far['token'], 75)])
    asyncio.run(sub.run_once())
    assert len(worker.dynamic) == 5 and ('nse_fo', far['token']) in worker.dynamic and sub.state['capped']
    by = {r['token']: r for r in recs}
    assert all(by[t]['strike'] in (25000.0, 26000.0) for _, t in worker.dynamic)  # ATM legs fill the rest


def test_disabled_or_without_stream_does_nothing(tmp_path, settings_override):
    sub, worker, _ = subscriber(tmp_path)
    assert asyncio.run(sub.run_once()) is None and worker.calls == [] and sub.state['enabled'] is False
    settings_override(kotak_auto_chain_json='{"NIFTY": {}}')
    sub.stream_manager_provider = lambda: None
    assert asyncio.run(sub.run_once()) is None and sub.state['error'] == 'KOTAK_STREAM_NOT_RUNNING'


class FakeWS:
    def __init__(self, fail_subscribe=False):
        self.sub, self.unsub, self.idx, self.fail = [], [], [], fail_subscribe

    async def subscribe_scrips(self, toks):
        if self.fail:
            raise RuntimeError('limit')
        self.sub.append(list(toks))

    async def unsubscribe_scrips(self, toks):
        self.unsub.append(list(toks))

    async def subscribe_index(self, toks):
        self.idx.append(list(toks))


def test_worker_sends_only_differences_and_retries_failures():
    w = KotakStreamWorker(None, ['nse_cm|2885', 'nse_cm|Nifty 50'], None)
    asyncio.run(w.set_dynamic({('nse_fo', '1'), ('nse_fo', '2')}))  # disconnected: stored only
    assert w.dynamic == {('nse_fo', '1'), ('nse_fo', '2')}
    w.ws = FakeWS()
    r = asyncio.run(w.set_dynamic({('nse_fo', '2'), ('nse_fo', '3'), ('nse_cm', '2885')}))
    assert r == {'added': 1, 'removed': 1, 'total': 2, 'connected': True}
    assert w.ws.unsub == [[WsToken('nse_fo', '1')]] and w.ws.sub == [[WsToken('nse_fo', '3')]]  # static 2885 untouched
    w.ws = FakeWS(fail_subscribe=True)
    with pytest.raises(RuntimeError):
        asyncio.run(w.set_dynamic({('nse_fo', '2'), ('nse_fo', '3'), ('nse_fo', '4')}))
    assert ('nse_fo', '4') not in w.dynamic  # still pending: the next call sends it again
    w.ws = FakeWS()
    assert asyncio.run(w.set_dynamic({('nse_fo', '2'), ('nse_fo', '3'), ('nse_fo', '4')}))['added'] == 1


def test_worker_connect_subscribes_static_and_dynamic_together():
    w = KotakStreamWorker(None, ['nse_cm|2885', 'nse_cm|Nifty 50'], None)
    w.dynamic = {('nse_fo', '7')}
    ws = FakeWS()

    class Conn:
        async def __aenter__(self):
            return ws

        async def __aexit__(self, *a):
            return False

    async def session():
        return SimpleNamespace(create_websocket=lambda **kw: Conn())

    async def consume(*a):
        assert w.ws is ws
    w.session_provider, w._consume = session, consume
    asyncio.run(w.run_once())
    assert ws.sub == [[WsToken('nse_cm', '2885'), WsToken('nse_fo', '7')]] and ws.idx == [[WsToken('nse_cm', 'Nifty 50')]]
    assert w.ws is None


def test_stream_manager_builds_kotak_worker_for_auto_chain_alone(settings_override):
    settings_override(kotak_auto_chain_json='{"NIFTY": {}}')
    reg = SimpleNamespace(items={'KOTAK': object()}, get=lambda n: SimpleNamespace(configured=lambda: True, session=None))
    sm = StreamManager(None, None, subscriptions={}, registry=reg)
    sm.build()
    assert [w.broker for w in sm.workers] == ['KOTAK'] and sm.workers[0].tokens == []
