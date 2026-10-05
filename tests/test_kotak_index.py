"""Kotak index streaming (NIFTY spot) and spot resolution from live index ticks."""
import asyncio
import time
from datetime import date, datetime

import pytest
from conftest import expiry_in

from app.broker_streams import (KotakStreamWorker, StreamError, kotak_index_tick, kotak_index_underlyings,
                                parse_kotak_subscriptions)
from app.clock import IST, years_to_expiry
from app.config import settings
from app.greeks import bs_price
from app.instruments import KOTAK_FO_EPOCH_OFFSET
from app.market import MarketDataGateway
from app.models import Tick
from app.option_chain import resolve_spot

feed_models = pytest.importorskip('neo_api_client.websocket.feed')
SFeedIndex, SFeedScrip, WsToken = feed_models.SFeedIndex, feed_models.SFeedScrip, feed_models.WsToken


def index_msg(name='Nifty 50', ltp=25012.35, seg='nse_cm', ts=None, token='26000'):
    return SFeedIndex(exchange_segment=seg, instrument_token=token, name=name, last_traded_price=ltp, open_price=ltp,
                      high_price=ltp, low_price=ltp, close_price=ltp, change=0.0, net_change_percent=0.0, yearly_high=ltp,
                      yearly_low=ltp, last_trade_time=int(time.time()) if ts is None else ts, precision=2, multiplier=1.0)


def scrip_msg(token='48201', ltp=120.0):
    now = int(time.time())
    return SFeedScrip(exchange_segment='nse_fo', instrument_token=token, trading_symbol='NIFTYX', level=4, last_traded_price=ltp,
                      open_price=ltp, high_price=ltp, low_price=ltp, close_price=ltp, average_trade_price=ltp, last_trade_time=now,
                      last_update_time=now, last_trade_qty=75, total_buy_quantity=0, total_sell_quantity=0, volume_traded_today=10,
                      open_interest=1000, net_change=0.0, net_change_percent=0.0, upper_circuit_limit=0.0, lower_circuit_limit=0.0,
                      yearly_high=0.0, yearly_low=0.0, total_traded_value=0.0, market_lot=75, precision=2, multiplier=1)


def test_subscriptions_split_scrips_and_named_indices():
    scrips, indices = parse_kotak_subscriptions([
        'nse_fo|48201', '35001', 'nse_cm|2885', 'nse_cm|Nifty 50', 'Nifty Bank', 'bse_cm | SENSEX',
        {'segment': 'nse_cm', 'token': 'Nifty Fin Service', 'index': True}, {'segment': 'nse_cm', 'token': '11536'}])
    assert scrips == [('nse_fo', '48201'), ('nse_fo', '35001'), ('nse_cm', '2885'), ('nse_cm', '11536')]
    assert indices == [('nse_cm', 'Nifty 50'), ('nse_cm', 'Nifty Bank'), ('bse_cm', 'SENSEX'), ('nse_cm', 'Nifty Fin Service')]
    with pytest.raises(StreamError, match='KOTAK_INDEX_NEEDS_CASH_SEGMENT'):
        parse_kotak_subscriptions(['nse_fo|Nifty 50'])
    with pytest.raises(StreamError, match='INVALID_KOTAK_SUBSCRIPTION'):
        parse_kotak_subscriptions(['xyz|1'])


def test_index_tick_maps_name_to_underlying_and_keeps_feed_timestamp(settings_override):
    t = kotak_index_tick(index_msg(name='NIFTY 50', ts=1759650000), subscribed_name='Nifty 50', now_ms=1)
    assert (t.broker, t.exchange, t.instrument_token, t.symbol, t.underlying) == ('KOTAK', 'NSE', 'Nifty 50', 'NIFTY', 'NIFTY')
    assert t.ltp == 25012.35 and t.bid == 0 and t.ask == 0 and t.expiry is None and t.exchange_ts_ms == 1759650000000
    assert kotak_index_tick(index_msg(name='SENSEX', seg='bse_cm')).exchange == 'BSE'
    assert kotak_index_tick(index_msg(name='Nifty IT')).underlying is None  # unknown index: streamed, not used as spot
    settings_override(kotak_index_underlyings_json='{"Nifty IT": "niftyit"}')
    assert kotak_index_underlyings()['nifty it'] == 'NIFTYIT' and kotak_index_tick(index_msg(name='Nifty IT')).underlying == 'NIFTYIT'
    assert kotak_index_tick(index_msg(ts=0)).exchange_ts_ms == 0  # no feed time: the quality gate rejects it


class FakeWS:
    def __init__(self, worker, msgs):
        self.worker, self.msgs, self.subs = worker, list(msgs), {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def subscribe_scrips(self, tokens):
        self.subs['scrips'] = tokens

    async def subscribe_index(self, tokens):
        self.subs['index'] = tokens

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.msgs:
            return self.msgs.pop(0)
        self.worker.stop = True
        return None


def test_worker_subscribes_indices_and_emits_index_and_scrip_ticks():
    ticks = []

    async def handler(t):
        ticks.append(t)
    w = KotakStreamWorker(handler, ['nse_fo|48201', 'nse_cm|Nifty 50', 'nse_cm|Nifty Bank'], None)
    ws = FakeWS(w, [index_msg(name='NIFTY 50', token='26000'), scrip_msg(), index_msg(name='Nifty Bank', ltp=56010.0, token='26009')])

    class Client:
        def create_websocket(self, **kw):
            assert kw == KotakStreamWorker.SOCKET_OPTIONS  # the SDK's own reconnect loop stays off
            return ws

    async def session():
        return Client()
    w.session_provider = session
    asyncio.run(w.run_once())
    assert ws.subs['scrips'] == [WsToken('nse_fo', '48201')]
    assert ws.subs['index'] == [WsToken('nse_cm', 'Nifty 50'), WsToken('nse_cm', 'Nifty Bank')]
    assert [(t.instrument_token, t.underlying) for t in ticks] == [('Nifty 50', 'NIFTY'), ('48201', None), ('Nifty Bank', 'BANKNIFTY')]
    assert w.healthy


def spot_tick(underlying='NIFTY', ltp=25040.0, age_ms=0, token='Nifty 50', **kw):
    now = int(time.time() * 1000)
    return Tick(broker='KOTAK', exchange='NSE', instrument_token=token, symbol=underlying, underlying=underlying, ltp=ltp,
                exchange_ts_ms=now - age_ms, receive_ts_ms=now - age_ms, **kw)


def test_live_spot_takes_only_fresh_index_or_cash_ticks(settings_override):
    g = MarketDataGateway()
    assert g.live_spot('NIFTY') is None
    g.ingest(spot_tick(token='FUT1', expiry=expiry_in(20), ltp=25100.0))                         # future: not spot
    g.ingest(spot_tick(token='OPT1', expiry=expiry_in(5), strike=25000.0, option_type='CE', ltp=120.0))  # option: not spot
    assert g.live_spot('NIFTY') is None
    g.ingest(spot_tick())
    assert g.live_spot('nifty').ltp == 25040.0
    settings_override(data_stale_ms=1)
    time.sleep(0.01)
    assert g.live_spot('NIFTY') is None  # stale index is not a spot


def test_resolve_spot_uses_streamed_index_without_configuration():
    g = MarketDataGateway()
    assert g.ingest(kotak_index_tick(index_msg(ltp=25040.5), 'Nifty 50'))['accepted']
    s = resolve_spot(g, 'NIFTY', expiry_in(7), [])
    assert s == {'value': 25040.5, 'source': 'SPOT_TOKEN', 'broker': 'KOTAK', 'strike': None, 'symbol': 'Nifty 50'}


def test_configured_spot_token_can_name_the_kotak_index(settings_override):
    settings_override(underlying_spot_tokens_json='{"NIFTY": {"KOTAK": "Nifty 50"}}')
    g = MarketDataGateway()
    g.ingest(kotak_index_tick(index_msg(ltp=24999.0), 'Nifty 50'))
    assert resolve_spot(g, 'NIFTY', expiry_in(7), [])['value'] == 24999.0


def test_kotak_nifty_index_drives_the_option_chain_atm(make_terminal):
    t, _ = make_terminal()
    d = date.fromisoformat(expiry_in(7))
    raw = int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=IST).timestamp()) - KOTAK_FO_EPOCH_OFFSET
    wk = f"NIFTY{d:%y}{'123456789OND'[d.month - 1]}{d:%d}"
    rows = [{'pSymbol': f'4820{i}', 'pTrdSymbol': f'{wk}{k}{typ}', 'pExchSeg': 'nse_fo', 'pSymbolName': 'NIFTY',
             'pExpiryDate': str(raw), 'dStrikePrice;': str(k * 100), 'pOptionType': typ, 'lLotSize': '75'}
            for i, (k, typ) in enumerate([(24950, 'CE'), (25000, 'CE'), (25050, 'CE')])]
    t.instruments.load_rows('KOTAK', rows)
    now, T = int(time.time() * 1000), years_to_expiry(d.isoformat())
    prices = [round(bs_price(25034.0, k, T, settings.risk_free_rate, 0.13, 'CE'), 2) for k in (24950, 25000, 25050)]
    for i, px in enumerate(prices):
        asyncio.run(t.handle_tick(Tick(broker='KOTAK', exchange='nse_fo', instrument_token=f'4820{i}', symbol='x', ltp=px,
                                       bid=px - 0.05, ask=px + 0.05, oi=100, exchange_ts_ms=now, receive_ts_ms=now)))
    lad = t.chain_ladder('NFO', 'NIFTY', d.isoformat())
    assert lad['spot']['value'] is None and lad['atm_strike'] is None  # calls only: no spot, no parity
    assert asyncio.run(t.handle_tick(kotak_index_tick(index_msg(ltp=25034.0), 'Nifty 50')))['accepted']
    lad = t.chain_ladder('NFO', 'NIFTY', d.isoformat())
    assert lad['spot']['value'] == 25034.0 and lad['spot']['symbol'] == 'Nifty 50' and lad['atm_strike'] == 25050.0
    assert all(r['CE']['iv'] and r['CE']['delta'] for r in lad['strikes'])
