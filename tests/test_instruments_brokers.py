import asyncio
import threading
import time

import httpx
import pytest
from conftest import angel_master_rows, expiry_in, override

from app.broker_streams import ThreadBridge, _ms
from app.brokers import Angel, Kotak, Upstox, Zerodha, classify_exception, order_tag
from app.instruments import InstrumentMaster


def test_every_broker_keeps_its_own_master(tmp_path):
    im = InstrumentMaster(str(tmp_path))
    im.load_rows('ANGEL', angel_master_rows(expiry_in(7)))
    im.load_rows('ZERODHA', [{'instrument_token': '111', 'tradingsymbol': 'NIFTY25OCT25000CE', 'name': 'NIFTY', 'expiry': expiry_in(7),
                              'strike': '25000', 'tick_size': '0.05', 'lot_size': '75', 'instrument_type': 'CE', 'exchange': 'NFO'}])
    im.load_rows('UPSTOX', [{'instrument_key': 'NSE_FO|111', 'trading_symbol': 'NIFTY 25000 CE', 'segment': 'NSE_FO',
                             'expiry': 1893456000000, 'strike_price': 25000, 'lot_size': 75, 'instrument_type': 'CE',
                             'underlying_symbol': 'NIFTY'}])
    assert im.get('ANGEL', '111')['strike'] == 25000 and im.get('ANGEL', '111')['option_type'] == 'CE'
    assert im.get('ZERODHA', '111')['lot_size'] == 75 and im.get('ZERODHA', '111')['tick_size'] == 0.05
    assert im.get('UPSTOX', 'NSE_FO|111')['exchange'] == 'NFO'
    assert InstrumentMaster(str(tmp_path)).get('ANGEL', '113')['option_type'] == 'PE'  # persisted and reloaded
    assert im.fresh_today('ANGEL')


def test_order_tag_fits_every_broker():
    t = order_tag('client-order-id-0123456789')
    assert len(t) <= 20 and t.isalnum() and t == order_tag('client-order-id-0123456789')


def test_exception_classification():
    assert classify_exception(httpx.ConnectError('x'))[0] == 'REJECTED'
    assert classify_exception(httpx.ReadTimeout('x'))[0] == 'UNKNOWN'   # original: BROKER_EXCEPTION, no ambiguity handling
    assert classify_exception(httpx.RemoteProtocolError('x'))[0] == 'UNKNOWN'


def _broker(cls, handler):
    return cls(transport=httpx.MockTransport(handler))


ORDER = {'client_order_id': 'abc', 'exchange': 'NFO', 'symbol': 'X', 'side': 'BUY', 'qty': 75, 'product': 'NRML',
         'order_type': 'LIMIT', 'price': 10.0, 'instrument_token': '111'}


@pytest.mark.parametrize('payload,code,expect,bo', [
    ({'status': True, 'data': {'orderid': '2010', 'uniqueorderid': 'u'}}, 200, 'SUBMITTED', '2010'),
    ({'status': False, 'message': 'Invalid Token', 'errorcode': 'AG8001', 'data': None}, 200, 'REJECTED', None),
    ({'message': 'gateway'}, 502, 'UNKNOWN', None),
    ({'message': 'bad'}, 400, 'REJECTED', None),
])
def test_angel_submission_outcomes(payload, code, expect, bo):
    seen = {}

    def handler(req):
        seen['body'] = req.content
        return httpx.Response(code, json=payload)
    with override(live_trading=True):
        r = asyncio.run(_broker(Angel, handler).place(dict(ORDER)))
    assert r['status'] == expect and r.get('broker_order_id') == bo
    assert b'CARRYFORWARD' in seen['body'] and b'"ordertag"' in seen['body']


def test_timeouts_are_ambiguous_and_connect_errors_are_not():
    def timeout(req):
        raise httpx.ReadTimeout('slow', request=req)

    def refused(req):
        raise httpx.ConnectError('refused', request=req)
    with override(live_trading=True):
        assert asyncio.run(_broker(Zerodha, timeout).place(dict(ORDER)))['status'] == 'UNKNOWN'
        assert asyncio.run(_broker(Zerodha, refused).place(dict(ORDER)))['status'] == 'REJECTED'


def test_upstox_v3_order_ids_and_product_mapping():
    seen = {}

    def handler(req):
        seen['body'] = req.content
        return httpx.Response(200, json={'status': 'success', 'data': {'order_ids': ['250101000001']}})
    with override(live_trading=True):
        r = asyncio.run(_broker(Upstox, handler).place(dict(ORDER)))
    assert r['broker_order_id'] == '250101000001' and b'"product":"D"' in seen['body'].replace(b' ', b'')


def test_place_refused_when_not_live():
    assert asyncio.run(Zerodha().place(dict(ORDER)))['status'] == 'REJECTED'


def test_kotak_cancel_error_is_not_reported_as_success():
    k = Kotak()

    class Sdk:
        def cancel_order(self, order_id):
            return {'Error': 'order not found'}

    async def session(force=False):
        return Sdk()
    k.session = session
    assert asyncio.run(k.cancel('1'))['status'] == 'CANCEL_REJECTED'


def test_thread_bridge_wakes_consumer_from_foreign_thread():
    async def run():
        b = ThreadBridge(asyncio.get_running_loop(), maxsize=3)
        t0 = time.time()
        threading.Timer(0.05, lambda: b.put('tick')).start()
        item = await b.get(2.0)
        return item, time.time() - t0, b
    item, latency, b = asyncio.run(run())
    assert item == 'tick' and latency < 0.5  # original asyncio.Queue fed from a thread woke after ~2 s


def test_thread_bridge_drops_oldest_when_full():
    async def run():
        b = ThreadBridge(asyncio.get_running_loop(), maxsize=2)
        for i in range(5):
            b._put(i)
        return [b.q.get_nowait() for _ in range(2)], b.dropped
    assert asyncio.run(run()) == ([3, 4], 3)


def test_epoch_units():
    assert _ms(1700000000) == 1700000000000 and _ms(1700000000123) == 1700000000123 and _ms(None) == 0


def test_upstox_sdk_event_signatures_match_handlers():
    upstox_client = pytest.importorskip('upstox_client')
    s = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(upstox_client.Configuration()), ['NSE_FO|1'], 'full')
    got = []
    s.on('open', lambda: got.append('open'))
    s.on('message', lambda m: got.append(m))
    s.on('error', lambda e: got.append(('err', e)))
    s.on('close', lambda c, m: got.append(('close', c)))
    s.emit('open')
    s.emit('message', {'feeds': {}})
    s.emit('error', 'boom')
    s.emit('close', 1006, 'x')
    assert got == ['open', {'feeds': {}}, ('err', 'boom'), ('close', 1006)]
