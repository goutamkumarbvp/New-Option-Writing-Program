"""Broker adapters.

Every adapter returns normalised data (see normalize.py) and classifies each
order submission into exactly one of:

  SUBMITTED  broker acknowledged and returned an order id
  REJECTED   broker refused, or the request provably never left this process
  UNKNOWN    the request may have reached the broker but no acknowledgement was
             read (timeout after send, 5xx, SDK exception). The order must be
             treated as possibly live until reconciliation resolves it.
"""
import asyncio
import hashlib
import time

import httpx

from .config import settings
from .normalize import (EvidenceUnavailable, WORKING, normalize_margin, normalize_orders,
                        normalize_positions, normalize_trades)
from .totp import totp

TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def order_tag(client_order_id):
    """Short alphanumeric tag (fits every broker's limit) that links a broker
    order back to its client_order_id even when the acknowledgement was lost."""
    return 'IO' + hashlib.sha1(client_order_id.encode()).hexdigest()[:16].upper()


def classify_exception(exc):
    """Was the request possibly delivered? Connect-phase failures never are."""
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, httpx.UnsupportedProtocol)):
        return 'REJECTED', 'NOT_SENT:' + type(exc).__name__
    return 'UNKNOWN', 'AMBIGUOUS:' + type(exc).__name__


def classify_http(code):
    if 200 <= code < 300:
        return None
    if code >= 500 or code in (408, 429):
        # 429 is a refusal, but some gateways return it after forwarding; stay conservative.
        return 'UNKNOWN' if code != 429 else 'REJECTED'
    return 'REJECTED'


class BaseBroker:
    name = 'UNKNOWN'
    base = ''

    def __init__(self, transport=None):
        self._client = None
        self._transport = transport

    def configured(self):
        return False

    def client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base, timeout=TIMEOUT, transport=self._transport)
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def headers(self):
        return {}

    async def _get(self, path, **kw):
        r = await self.client().get(path, headers=self.headers(), **kw)
        if r.status_code >= 400:
            raise RuntimeError(f'{self.name}_HTTP_{r.status_code}:{path}')
        return r.json()

    async def health(self):
        return {'broker': self.name, 'status': 'UNCONFIGURED'}

    async def raw_orders(self):
        raise NotImplementedError

    async def raw_trades(self):
        raise NotImplementedError

    async def raw_positions(self):
        raise NotImplementedError

    async def raw_margin(self):
        raise EvidenceUnavailable('MARGIN_DATA_UNAVAILABLE')

    async def orders(self):
        return normalize_orders(self.name, await self.raw_orders())

    async def trades(self):
        return normalize_trades(self.name, await self.raw_trades())

    async def positions(self):
        return normalize_positions(self.name, await self.raw_positions())

    async def margin(self):
        return normalize_margin(self.name, await self.raw_margin())

    async def order_margin(self, order):
        """Broker-computed margin for one order, or None when the broker API is not wired."""
        return None

    async def place(self, order):
        return {'status': 'REJECTED', 'reason': 'NOT_IMPLEMENTED', 'broker': self.name}

    async def cancel(self, broker_order_id):
        return {'status': 'CANCEL_REJECTED', 'reason': 'NOT_IMPLEMENTED'}

    async def cancel_all(self):
        results = []
        for o in await self.orders():
            if o['status'] in WORKING:
                results.append({'broker_order_id': o['broker_order_id'], **(await self.cancel(o['broker_order_id']))})
        return {'status': 'CANCEL_REQUESTED', 'results': results}

    async def _submit(self, method, url, ok, **kw):
        """Send one order request and classify the outcome. ``ok(json)`` returns the order id or None."""
        if not settings.live_trading:
            return {'status': 'REJECTED', 'reason': 'LIVE_TRADING_DISABLED', 'broker': self.name}
        try:
            r = await self.client().request(method, url, headers=self.headers(), **kw)
        except Exception as exc:  # noqa: BLE001 - classified, never swallowed
            status, reason = classify_exception(exc)
            return {'status': status, 'reason': reason, 'broker': self.name}
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {'non_json_body': r.text[:500]}
        bad = classify_http(r.status_code)
        if bad:
            return {'status': bad, 'reason': f'HTTP_{r.status_code}', 'broker': self.name, 'http': r.status_code, 'response': body}
        oid = ok(body) if isinstance(body, dict) else None
        if not oid:
            return {'status': 'REJECTED', 'reason': 'BROKER_LOGICAL_REJECT', 'broker': self.name, 'http': r.status_code, 'response': body}
        return {'status': 'SUBMITTED', 'broker': self.name, 'broker_order_id': str(oid), 'http': r.status_code, 'response': body}


class Zerodha(BaseBroker):
    name = 'ZERODHA'
    base = 'https://api.kite.trade'

    def configured(self):
        return bool(settings.zerodha_api_key and settings.zerodha_access_token)

    def headers(self):
        return {'X-Kite-Version': '3', 'Authorization': f'token {settings.zerodha_api_key}:{settings.zerodha_access_token}'}

    async def health(self):
        if not self.configured():
            return {'broker': self.name, 'status': 'UNCONFIGURED'}
        try:
            r = await self.client().get('/user/profile', headers=self.headers())
            return {'broker': self.name, 'status': 'LIVE' if r.status_code == 200 else 'AUTH_ERROR', 'http': r.status_code}
        except Exception as exc:  # noqa: BLE001
            return {'broker': self.name, 'status': 'AUTH_ERROR', 'error': type(exc).__name__}

    async def raw_orders(self):
        return await self._get('/orders')

    async def raw_trades(self):
        return await self._get('/trades')

    async def raw_positions(self):
        return await self._get('/portfolio/positions')

    async def raw_margin(self):
        return await self._get('/user/margins')

    def _params(self, o):
        d = {'exchange': o['exchange'], 'tradingsymbol': o['symbol'], 'transaction_type': o['side'],
             'quantity': o['qty'], 'product': o['product'], 'order_type': o['order_type'], 'validity': 'DAY',
             'tag': order_tag(o['client_order_id'])}
        if o.get('price'):
            d['price'] = o['price']
        if o.get('trigger_price'):
            d['trigger_price'] = o['trigger_price']
        return d

    async def order_margin(self, o):
        p = self._params(o)
        body = [{'exchange': p['exchange'], 'tradingsymbol': p['tradingsymbol'], 'transaction_type': p['transaction_type'],
                 'variety': 'regular', 'product': p['product'], 'order_type': p['order_type'], 'quantity': p['quantity'],
                 'price': p.get('price', 0), 'trigger_price': p.get('trigger_price', 0)}]
        try:
            r = await self.client().post('/margins/orders', headers=self.headers(), json=body)
            data = r.json().get('data') if r.status_code == 200 else None
            return float(data[0]['total']) if data else None
        except Exception:  # noqa: BLE001 - an unavailable estimate is reported as None
            return None

    async def place(self, o):
        return await self._submit('POST', '/orders/regular', lambda b: (b.get('data') or {}).get('order_id'),
                                  data=self._params(o))

    async def cancel(self, broker_order_id):
        r = await self.client().delete(f'/orders/regular/{broker_order_id}', headers=self.headers())
        return {'status': 'CANCEL_REQUESTED' if r.status_code < 300 else 'CANCEL_REJECTED', 'http': r.status_code}


class Upstox(BaseBroker):
    name = 'UPSTOX'
    base = 'https://api.upstox.com'
    PRODUCT = {'NRML': 'D', 'CNC': 'D', 'MIS': 'I'}

    def configured(self):
        return bool(settings.upstox_access_token)

    def headers(self):
        return {'Accept': 'application/json', 'Authorization': f'Bearer {settings.upstox_access_token}'}

    async def health(self):
        if not self.configured():
            return {'broker': self.name, 'status': 'UNCONFIGURED'}
        try:
            r = await self.client().get('/v2/user/profile', headers=self.headers())
            return {'broker': self.name, 'status': 'LIVE' if r.status_code == 200 else 'AUTH_ERROR', 'http': r.status_code}
        except Exception as exc:  # noqa: BLE001
            return {'broker': self.name, 'status': 'AUTH_ERROR', 'error': type(exc).__name__}

    async def raw_orders(self):
        return await self._get('/v2/order/retrieve-all')

    async def raw_trades(self):
        return await self._get('/v2/order/trades/get-trades-for-day')

    async def raw_positions(self):
        return await self._get('/v2/portfolio/short-term-positions')

    async def raw_margin(self):
        return await self._get('/v2/user/get-funds-and-margin')

    async def place(self, o):
        body = {'quantity': o['qty'], 'product': self.PRODUCT[o['product']], 'validity': 'DAY',
                'price': o.get('price') or 0, 'tag': order_tag(o['client_order_id']),
                'instrument_token': o['instrument_token'], 'order_type': o['order_type'],
                'transaction_type': o['side'], 'disclosed_quantity': 0, 'trigger_price': o.get('trigger_price') or 0,
                'is_amo': False, 'slice': False}

        def ok(b):
            d = b.get('data') or {}
            ids = d.get('order_ids') or ([d['order_id']] if d.get('order_id') else [])
            return ids[0] if ids and b.get('status') == 'success' else None
        return await self._submit('POST', 'https://api-hft.upstox.com/v3/order/place', ok, json=body)

    async def cancel(self, broker_order_id):
        r = await self.client().delete('https://api-hft.upstox.com/v2/order/cancel', headers=self.headers(),
                                       params={'order_id': broker_order_id})
        return {'status': 'CANCEL_REQUESTED' if r.status_code < 300 else 'CANCEL_REJECTED', 'http': r.status_code}


class Angel(BaseBroker):
    name = 'ANGEL'
    base = 'https://apiconnect.angelone.in'
    PRODUCT = {'NRML': 'CARRYFORWARD', 'MIS': 'INTRADAY', 'CNC': 'DELIVERY'}
    ORDER_TYPE = {'MARKET': 'MARKET', 'LIMIT': 'LIMIT', 'SL': 'STOPLOSS_LIMIT', 'SL-M': 'STOPLOSS_MARKET'}

    def configured(self):
        return bool(settings.angel_access_token and settings.angel_api_key and settings.angel_client_code)

    def headers(self):
        return {'Authorization': f'Bearer {settings.angel_access_token}', 'Content-Type': 'application/json',
                'Accept': 'application/json', 'X-PrivateKey': settings.angel_api_key, 'X-UserType': 'USER',
                'X-SourceID': 'WEB'}

    async def _get_ok(self, path):
        body = await self._get(path)
        if not (isinstance(body, dict) and body.get('status') is True):
            raise RuntimeError(f'ANGEL_API_ERROR:{(body or {}).get("errorcode") if isinstance(body, dict) else "?"}')
        return body

    async def health(self):
        if not self.configured():
            return {'broker': self.name, 'status': 'UNCONFIGURED'}
        try:
            await self._get_ok('/rest/secure/angelbroking/user/v1/getProfile')
            return {'broker': self.name, 'status': 'LIVE'}
        except Exception as exc:  # noqa: BLE001
            return {'broker': self.name, 'status': 'AUTH_ERROR', 'error': str(exc)[:120]}

    async def raw_orders(self):
        return await self._get_ok('/rest/secure/angelbroking/order/v1/getOrderBook')

    async def raw_trades(self):
        return await self._get_ok('/rest/secure/angelbroking/order/v1/getTradeBook')

    async def raw_positions(self):
        return await self._get_ok('/rest/secure/angelbroking/order/v1/getPosition')

    async def raw_margin(self):
        return await self._get_ok('/rest/secure/angelbroking/user/v1/getRMS')

    async def place(self, o):
        variety = 'STOPLOSS' if o['order_type'] in ('SL', 'SL-M') else 'NORMAL'
        body = {'variety': variety, 'tradingsymbol': o['symbol'], 'symboltoken': o['instrument_token'],
                'transactiontype': o['side'], 'exchange': o['exchange'], 'ordertype': self.ORDER_TYPE[o['order_type']],
                'producttype': self.PRODUCT[o['product']], 'duration': 'DAY', 'price': str(o.get('price') or 0),
                'triggerprice': str(o.get('trigger_price') or 0), 'quantity': str(o['qty']), 'squareoff': '0',
                'stoploss': '0', 'ordertag': order_tag(o['client_order_id'])}

        def ok(b):
            return (b.get('data') or {}).get('orderid') if b.get('status') is True else None
        return await self._submit('POST', '/rest/secure/angelbroking/order/v1/placeOrder', ok, json=body)

    async def cancel(self, broker_order_id):
        r = await self.client().post('/rest/secure/angelbroking/order/v1/cancelOrder', headers=self.headers(),
                                     json={'variety': 'NORMAL', 'orderid': str(broker_order_id)})
        body = r.json() if r.content else {}
        ok = r.status_code < 300 and isinstance(body, dict) and body.get('status') is True
        return {'status': 'CANCEL_REQUESTED' if ok else 'CANCEL_REJECTED', 'http': r.status_code}


class Kotak(BaseBroker):
    """Kotak Neo via the official synchronous SDK. One authenticated session is
    cached and refreshed; the original code logged in again on every call."""
    name = 'KOTAK'
    ORDER_TYPE = {'MARKET': 'MKT', 'LIMIT': 'L', 'SL': 'SL', 'SL-M': 'SL-M'}

    def __init__(self, transport=None):
        super().__init__(transport)
        self._session = None
        self._session_at = 0.0
        self._lock = asyncio.Lock()

    def configured(self):
        return bool(settings.kotak_api_key and settings.kotak_mobile and settings.kotak_client_code
                    and settings.kotak_mpin and (settings.kotak_totp_secret or settings.kotak_totp))

    def _code(self):
        return totp(settings.kotak_totp_secret) if settings.kotak_totp_secret else settings.kotak_totp

    async def session(self, force=False):
        async with self._lock:
            fresh = self._session and time.time() - self._session_at < settings.kotak_session_ttl_sec
            if fresh and not force:
                return self._session
            if not self.configured():
                raise RuntimeError('KOTAK_CREDENTIALS_MISSING')
            from neo_api_client import NeoAPI
            c = NeoAPI(consumer_key=settings.kotak_api_key, environment='prod')
            for step in (lambda: c.totp_login(mobile_number=settings.kotak_mobile, ucc=settings.kotak_client_code, totp=self._code()),
                         lambda: c.totp_validate(mpin=settings.kotak_mpin)):
                resp = await asyncio.to_thread(step)
                if not isinstance(resp, dict) or 'error' in resp or 'Error' in resp:
                    raise RuntimeError('KOTAK_LOGIN_FAILED')
            self._session, self._session_at = c, time.time()
            return c

    async def _call(self, fn_name, **kw):
        c = await self.session()
        resp = await asyncio.to_thread(getattr(c, fn_name), **kw)
        if isinstance(resp, dict) and ('Error' in resp or 'Error Message' in resp or 'error' in resp):
            self._session = None  # force re-login next time
            raise RuntimeError(f'KOTAK_{fn_name.upper()}_ERROR')
        return resp

    async def health(self):
        if not self.configured():
            return {'broker': self.name, 'status': 'UNCONFIGURED'}
        try:
            await self.session()
            return {'broker': self.name, 'status': 'LIVE', 'sdk': 'kotakneoapi'}
        except Exception as exc:  # noqa: BLE001
            return {'broker': self.name, 'status': 'AUTH_ERROR', 'error': str(exc)[:120]}

    async def raw_orders(self):
        return await self._call('order_report')

    async def raw_trades(self):
        return await self._call('trade_report')

    async def raw_positions(self):
        return await self._call('positions')

    async def raw_margin(self):
        return await self._call('limits')

    async def place(self, o):
        if not settings.live_trading:
            return {'status': 'REJECTED', 'reason': 'LIVE_TRADING_DISABLED', 'broker': self.name}
        try:
            c = await self.session()
        except Exception as exc:  # noqa: BLE001 - login failed, nothing was sent
            return {'status': 'REJECTED', 'reason': 'NOT_SENT:' + str(exc)[:80], 'broker': self.name}
        kw = dict(exchange_segment=o['exchange'], product=o['product'], price=str(o.get('price') or 0),
                  order_type=self.ORDER_TYPE[o['order_type']], quantity=str(o['qty']), validity='DAY',
                  trading_symbol=o['symbol'], transaction_type='B' if o['side'] == 'BUY' else 'S',
                  trigger_price=str(o.get('trigger_price') or 0), tag=order_tag(o['client_order_id']))
        try:
            resp = await asyncio.to_thread(c.place_order, **kw)
        except Exception as exc:  # noqa: BLE001
            return {'status': 'UNKNOWN', 'reason': 'AMBIGUOUS:' + type(exc).__name__, 'broker': self.name}
        if not isinstance(resp, dict):
            return {'status': 'UNKNOWN', 'reason': 'NON_DICT_RESPONSE', 'broker': self.name}
        if 'Error Message' in resp:
            # SDK refuses before sending when the session is not 2FA-complete.
            return {'status': 'REJECTED', 'reason': 'NOT_SENT:KOTAK_SESSION', 'broker': self.name}
        if 'Error' in resp:
            # The SDK wraps validation errors (not sent) and transport errors (maybe sent) in the
            # same shape, so this is ambiguous. Reconciliation resolves it by order tag.
            return {'status': 'UNKNOWN', 'reason': 'KOTAK_SDK_ERROR', 'broker': self.name,
                    'response': {'error': str(resp.get('Error'))[:300]}}
        oid = resp.get('nOrdNo')
        if oid and str(resp.get('stat', 'Ok')).lower() == 'ok':
            return {'status': 'SUBMITTED', 'broker': self.name, 'broker_order_id': str(oid), 'response': resp}
        return {'status': 'REJECTED', 'reason': 'BROKER_LOGICAL_REJECT', 'broker': self.name, 'response': resp}

    async def cancel(self, broker_order_id):
        try:
            await self._call('cancel_order', order_id=str(broker_order_id))
            return {'status': 'CANCEL_REQUESTED'}
        except Exception as exc:  # noqa: BLE001
            return {'status': 'CANCEL_REJECTED', 'error': str(exc)[:120]}


class BrokerRegistry:
    def __init__(self, items=None):
        self.items = items if items is not None else {'ZERODHA': Zerodha(), 'UPSTOX': Upstox(), 'ANGEL': Angel(), 'KOTAK': Kotak()}

    def get(self, name):
        key = str(name).upper()
        if key not in self.items:
            raise KeyError(f'UNKNOWN_BROKER:{key}')
        return self.items[key]

    def configured(self):
        return [b for b in self.items.values() if b.configured()]

    async def health(self):
        return list(await asyncio.gather(*(b.health() for b in self.items.values())))

    async def aclose(self):
        for b in self.items.values():
            await b.aclose()
