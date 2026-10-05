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
import re
import time

import httpx

from .clock import IST, now_ist, trading_date
from .clockcheck import clock_hint, clock_offset
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


def login_failure_kind(exc):
    """REJECTED when the broker answered and refused (4xx other than timeout/rate limit),
    which counts toward account lockout; TRANSPORT for network, proxy, timeout, rate-limit
    and server errors, which never reached credential checks. The Kotak SDK reports
    connection-level failures (including a proxy refusing the tunnel) with status 0."""
    status = getattr(exc, 'status', None)
    if isinstance(status, int) and 400 <= status < 500 and status not in (408, 429):
        return 'REJECTED'
    return 'TRANSPORT'


def _reply_parts(resp):
    """(numeric codes, joined text) from an SDK error body in any of its shapes."""
    codes, texts = [], []
    items = []
    for key in ('error', 'errors'):
        v = resp.get(key)
        items += v if isinstance(v, list) else [v] if v else []
    if isinstance(resp.get('fault'), dict):
        items.append(resp['fault'])
    items.append(resp)
    for it in items:
        if isinstance(it, dict):
            for ck in ('code', 'stCode', 'status'):
                try:
                    codes.append(int(str(it.get(ck)).strip()))
                except (TypeError, ValueError):
                    pass
            texts += [str(it.get(k)) for k in ('message', 'description', 'emsg', 'errMsg', 'Error', 'Error Message') if it.get(k)]
        elif it:
            texts.append(str(it))
    return codes, ' '.join(texts)


def login_reply_kind(resp):
    """How to treat a login step's reply that is not a success.

    REJECTED: Kotak answered and refused (wrong TOTP, MPIN, mobile, UCC; account locked). It counts toward
    the lockout halt. TRANSPORT: gateway, maintenance, rate-limit, timeout or server error; it backs off but
    never counts, so a Kotak maintenance window cannot halt the login. CONFIG: the SDK's own check found a
    missing or malformed field; nothing reached Kotak. Anything unrecognised counts as REJECTED, because
    the lockout is the costlier mistake."""
    if not isinstance(resp, dict):
        return 'TRANSPORT'
    codes, text = _reply_parts(resp)
    if _CONFIG_TEXT.search(text):
        return 'CONFIG'
    if any(c == 0 or c >= 500 or c in (408, 429) for c in codes if c < 1000) or _TRANSPORT_TEXT.search(text):
        return 'TRANSPORT'
    return 'REJECTED'


def kotak_mobile(value):
    """+91 followed by 10 digits, from the common ways people write an Indian mobile number; None if invalid."""
    digits = re.sub(r'[\s\-()]', '', str(value or ''))
    if re.fullmatch(r'\d{10}', digits):
        digits = '+91' + digits
    elif re.fullmatch(r'91\d{10}', digits):
        digits = '+' + digits
    return digits if re.fullmatch(r'\+91[6-9]\d{9}', digits) else None


def kotak_login_fields():
    """Normalised (mobile, ucc, mpin) or raise KOTAK_CONFIG_INVALID:<FIELD>. Checked before any network
    call, so a typo is reported as a configuration error and never sent to Kotak as a refused login."""
    mobile = kotak_mobile(settings.kotak_mobile)
    if not mobile:
        raise RuntimeError('KOTAK_CONFIG_INVALID:KOTAK_MOBILE:write +91 followed by the 10-digit number')
    ucc = str(settings.kotak_client_code or '').strip()
    if not re.fullmatch(r'[A-Za-z0-9]{3,15}', ucc):
        raise RuntimeError('KOTAK_CONFIG_INVALID:KOTAK_CLIENT_CODE:letters and digits only')
    mpin = str(settings.kotak_mpin or '').strip()
    if not re.fullmatch(r'\d{4,6}', mpin):
        raise RuntimeError('KOTAK_CONFIG_INVALID:KOTAK_MPIN:digits only (the 6-digit MPIN)')
    return mobile, ucc, mpin


def totp_secret_valid(secret):
    try:
        totp(secret)
        return len(secret.replace(' ', '')) >= 16
    except Exception:  # noqa: BLE001 - binascii.Error and friends
        return False


def totp_wait(now, step=30, guard=3.0):
    """Seconds to wait so a fresh TOTP code has at least `guard` seconds of validity left."""
    remaining = step - (now % step)
    return remaining + 0.25 if remaining < guard else 0.0


# Kotak's gateway is WSO2: an expired or revoked token answers HTTP 401 with fault code 900901
# ("Invalid Credentials") or 900902 ("Missing Credentials"). The SDK returns 4xx bodies instead of
# raising, so the body has to be read.
_AUTH_CODES = {'900900', '900901', '900902', '900903', '900904', '900905', '401', '403'}
# Only Kotak-style "your session is dead" wording counts; generic words such as 'token' or 'expired' alone
# (an expired contract, a token field) must never trigger a re-login.
_AUTH_TEXT = re.compile(r'session (has )?expired|invalid session|session.{0,20}(invalid|expired|timed? ?out|not found)|not logged in|'
                        r'login required|please log ?in|invalid credentials|invalid token|token (has )?expired|expired token|jwt|'
                        r'unauthori[sz]ed|unauthenticated|forbidden|\bsid\b', re.I)
_TRANSPORT_TEXT = re.compile(r'unexpected response|gateway|unavailable|maintenance|timed? ?out|timeout|try again later|too many|'
                             r'rate limit|server error|internal error|connection|unable to connect', re.I)
_CONFIG_TEXT = re.compile(r'missing required|required field|is required|must be (a|an|provided)', re.I)
AUTH_PROVEN = 'PROVEN'   # the gateway or the SDK refused before processing: the request was not executed
AUTH_LIKELY = 'LIKELY'   # an error message reads like a dead session; the request may have been processed


def kotak_auth_failure(obj):
    """Does an SDK response or exception say the Kotak session is no longer valid?

    Returns AUTH_PROVEN (HTTP 401/403, a gateway auth fault code, or the SDK's own refusal because 2FA
    is incomplete), AUTH_LIKELY (an error body whose text mentions session, token, login and the like)
    or None. A body without an error marker is never an auth failure: order and trade reports carry
    fields such as `tok`, and an empty book answers `Not_Ok` with "No Data".
    """
    if isinstance(obj, BaseException):
        return AUTH_PROVEN if getattr(obj, 'status', None) in (401, 403) else None
    if not isinstance(obj, dict):
        return None
    fault = obj.get('fault') if isinstance(obj.get('fault'), dict) else {}
    if 'Error Message' in obj:
        return AUTH_PROVEN
    if any(str(src.get('code', '')).strip() in _AUTH_CODES for src in (obj, fault)):
        return AUTH_PROVEN
    errs = obj.get('error') if isinstance(obj.get('error'), list) else []
    if any(isinstance(e, dict) and str(e.get('code', '')).strip() in {'401', '403'} for e in errs):
        return AUTH_PROVEN
    if any(k in obj for k in ('Error', 'error', 'fault')) or str(obj.get('stat', 'Ok')).lower() != 'ok':
        text = ' '.join(str(obj.get(k, '')) for k in ('Error', 'error', 'fault', 'emsg', 'errMsg', 'message', 'description'))
        if _AUTH_TEXT.search(text):
            return AUTH_LIKELY
    return None


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

    def __init__(self, transport=None, client_factory=None, clock=None, wall=None, sleep=None, clock_probe=None):
        super().__init__(transport)
        self._session = None
        self._session_at = 0.0
        self._session_day = None    # IST trading date the session was created on
        self._lock = asyncio.Lock()
        self._factory = client_factory  # tests inject a fake NeoAPI; production imports the SDK lazily
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._sleep = sleep or asyncio.sleep
        self._clock_probe = clock_probe or clock_offset  # async () -> seconds the server is ahead, or None
        self._failures = 0          # consecutive failed logins of any kind
        self._rejections = 0        # consecutive logins Kotak itself refused (credential risk)
        self._retry_at = 0.0        # monotonic time before which no login is attempted
        self._halted = False
        self._last_error = None
        self._expired = False       # a session was in use and has been found dead; not yet replaced
        self._relogins = 0          # logins forced by a detected expiry
        self._last_ok_at = 0.0      # wall time of the last authenticated call that succeeded
        self._bad_calls = 0         # consecutive failed calls with no success in between
        self._last_totp_step = -1   # 30 s TOTP window of the last code sent; never sent twice
        self._clock_offset = None   # seconds the server (Kotak) is ahead of this machine's clock
        self._offset_at = 0.0       # wall time the offset was last measured
        self._static_totp_used = False  # a one-time KOTAK_TOTP has already produced a session
        self._relogin_times = []    # wall times of logins forced by a detected expiry (re-login budget)
        self._login_task = None

    def configured(self):
        return bool(settings.kotak_api_key and settings.kotak_mobile and settings.kotak_client_code
                    and settings.kotak_mpin and (settings.kotak_totp_secret or settings.kotak_totp))

    def _now_kotak(self):
        """This machine's time corrected by the offset measured against Kotak. Docker Desktop runs the
        terminal in a WSL 2 VM whose clock drifts after sleep, independently of the Windows clock; a
        drift of more than a few seconds would make every TOTP code look wrong."""
        off = self._clock_offset
        return self._wall() + (off if off is not None and abs(off) >= 3 else 0.0)

    def _code(self):
        return totp(settings.kotak_totp_secret, at=self._now_kotak()) if settings.kotak_totp_secret else settings.kotak_totp

    # ------------------------------------------------------------- login backoff
    # The risk monitor, order monitor, reconciler, health check and stream worker all
    # call session(). Without backoff a failing login was retried about once a second,
    # and each credential rejection counts toward Kotak's account lockout.
    def login_state(self):
        now = self._clock()
        wall = self._wall()
        return {'halted': self._halted, 'consecutive_failures': self._failures, 'consecutive_rejections': self._rejections,
                'retry_in_sec': max(0, round(self._retry_at - now)) if self._retry_at > now else 0,
                'last_error': self._last_error,
                'logged_in': self._session is not None, 'session_expired': self._expired, 'relogins': self._relogins,
                'session_age_sec': round(wall - self._session_at) if self._session is not None else None,
                'last_ok_age_sec': round(wall - self._last_ok_at) if self._last_ok_at else None,
                'clock_offset_sec': self._clock_offset}

    def reset_login(self):
        """Operator action: clear the halt and backoff so the next call logs in immediately."""
        self._failures = self._rejections = 0
        self._retry_at, self._halted, self._last_error = 0.0, False, None
        self._clock_offset = None
        return self.login_state()

    def peek_session(self):
        """The cached client, or None. Never logs in: callers use it to notice a replaced session."""
        return self._session

    def _expire_session(self, why):
        """Drop a session found dead. The next session() call logs in again, subject to the backoff and halt."""
        if self._session is not None:
            self._expired = True
        self._session = None
        self._last_error = f'SESSION_EXPIRED:{why}'[:200]

    def _mark_ok(self):
        self._last_ok_at = self._wall()
        self._bad_calls = 0

    def _fresh(self):
        """A usable session: present, within the TTL, and created on today's IST trading date."""
        return bool(self._session and self._wall() - self._session_at < settings.kotak_session_ttl_sec
                    and self._session_day == trading_date())

    def _login_failed(self, kind, detail):
        self._failures += 1
        self._last_error = f'{kind}:{detail}'[:200]
        if kind == 'REJECTED':
            # Refusals count until a login succeeds: Kotak's own lockout counter is not reset by a network
            # error in between, so this one is not either.
            self._rejections += 1
            if self._rejections >= max(1, settings.kotak_login_max_rejections):
                self._halted = True
            delay = min(settings.kotak_login_backoff_sec * 2 ** (self._failures - 1), settings.kotak_login_backoff_max_sec)
        else:
            # Network, gateway and maintenance failures never reached the credential check: retry soon
            # (5, 10, 20, 40, 60 s) so the terminal is back within a minute of the network returning.
            delay = min(settings.kotak_login_transport_backoff_sec * 2 ** (self._failures - 1),
                        settings.kotak_login_transport_backoff_max_sec)
        self._retry_at = self._clock() + delay
        if self._halted:
            return RuntimeError(f'KOTAK_LOGIN_HALTED:{self._rejections}_REJECTIONS:{self._last_error}')
        return RuntimeError(f'KOTAK_LOGIN_{kind}:retry_in={round(delay)}s:{detail}'[:200])

    async def _totp_pause(self):
        """Wait so the code sent has time left in its 30 s window and was not already sent in this window.
        A repeated code is refused, and every refusal counts toward Kotak's account lockout."""
        now = self._now_kotak()
        wait = totp_wait(now)
        if int((now + wait) // 30) <= self._last_totp_step:
            wait = (self._last_totp_step + 1) * 30 - now + 0.25
        if wait > 0:
            await self._sleep(wait)

    async def _measure_clock(self):
        """Best effort: measure this machine's clock against Kotak's Date header (None when unreachable)."""
        try:
            off = await asyncio.wait_for(self._clock_probe(), 6)
        except Exception:  # noqa: BLE001
            off = None
        self._offset_at = self._wall()
        if off is not None:
            self._clock_offset = off
        return off

    async def _after_rejection(self):
        """A drifted clock is the usual reason a correct TOTP secret is refused: measure it, so the next
        code is computed on Kotak's time, and tell the operator how to fix the clock."""
        if not settings.kotak_totp_secret:
            return
        await self._measure_clock()
        hint = clock_hint(self._clock_offset)
        if hint:
            self._last_error = f'{self._last_error} | {hint}'[:400]

    def _config_error(self, msg):
        """A setting is wrong: reported (and shown on the dashboard), never sent to Kotak, never counted."""
        self._last_error = msg[:200]
        return RuntimeError(msg)

    def _logging_in(self):
        return self._lock.locked() or (self._login_task is not None and not self._login_task.done())

    async def session(self, force=False):
        async with self._lock:
            if self._login_task is not None and not self._login_task.done():
                # A login whose caller was cancelled (say, by a manual feed reconnect) is still running:
                # share it rather than start a second one with a reused TOTP code.
                return await asyncio.shield(self._login_task)
            if self._fresh() and not force:
                return self._session
            if not self.configured():
                raise RuntimeError('KOTAK_CREDENTIALS_MISSING')
            if settings.kotak_totp_secret and not totp_secret_valid(settings.kotak_totp_secret):
                # A typo in the secret is a configuration error: never send Kotak a code derived from it.
                raise self._config_error('KOTAK_TOTP_SECRET_INVALID:use the base32 text behind the TOTP QR code (A-Z, 2-7)')
            if not settings.kotak_totp_secret and self._static_totp_used:
                # A one-time code has been used; resending it is always refused and counts toward the lockout.
                raise self._config_error('KOTAK_TOTP_SECRET_REQUIRED:a one-time KOTAK_TOTP works for one login; set KOTAK_TOTP_SECRET')
            try:
                mobile, ucc, mpin = kotak_login_fields()
            except RuntimeError as exc:
                raise self._config_error(str(exc)) from None
            # Backoff applies to forced logins too: force must never bypass lockout protection.
            if self._halted:
                raise RuntimeError(f'KOTAK_LOGIN_HALTED:{self._rejections}_REJECTIONS:operator reset required:{self._last_error}')
            wait = self._retry_at - self._clock()
            if wait > 0:
                raise RuntimeError(f'KOTAK_LOGIN_BACKOFF:retry_in={round(wait)}s:{self._last_error}')
            # The login runs as its own task: a cancelled caller never abandons it halfway.
            self._login_task = asyncio.ensure_future(self._login(mobile, ucc, mpin))
            self._login_task.add_done_callback(lambda t: t.cancelled() or t.exception())  # never "never retrieved"
            return await asyncio.shield(self._login_task)

    async def _login(self, mobile, ucc, mpin):
        if True:  # keeps the original indentation of the login body
            if settings.kotak_totp_secret:
                if not self._offset_at or self._wall() - self._offset_at > 6 * 3600:
                    await self._measure_clock()
                await self._totp_pause()
            factory = self._factory
            if factory is None:
                from neo_api_client import NeoAPI as factory
            try:
                c = factory(consumer_key=settings.kotak_api_key, environment='prod')
            except Exception as exc:  # noqa: BLE001 - SDK construction failed; nothing reached Kotak's auth
                raise self._login_failed('TRANSPORT', type(exc).__name__) from exc
            for name, step in (('TOTP', lambda: c.totp_login(mobile_number=mobile, ucc=ucc, totp=self._code())),
                               ('MPIN', lambda: c.totp_validate(mpin=mpin))):
                if name == 'TOTP':
                    self._last_totp_step = int(self._now_kotak() // 30)
                try:
                    resp = await asyncio.wait_for(asyncio.to_thread(step), settings.kotak_login_step_timeout_sec)
                except asyncio.TimeoutError as exc:
                    raise self._login_failed('TRANSPORT', f'{name}:TIMEOUT') from exc
                except Exception as exc:  # noqa: BLE001
                    kind = login_failure_kind(exc)
                    err = self._login_failed(kind, f'{name}:{type(exc).__name__}:{str(exc)[:80]}')
                    if kind == 'REJECTED':
                        await self._after_rejection()
                    raise err from exc
                if not isinstance(resp, dict) or 'error' in resp or 'Error' in resp or not isinstance(resp.get('data'), dict):
                    kind = login_reply_kind(resp)
                    err = self._login_failed(kind, f'{name}:{str(resp)[:100]}')
                    if kind == 'REJECTED':
                        await self._after_rejection()
                    raise err
            had_session = self._session is not None or self._expired
            self._session, self._session_at, self._session_day = c, self._wall(), trading_date()
            self._failures = self._rejections = 0
            self._retry_at, self._last_error = 0.0, None
            self._expired, self._bad_calls = False, 0
            if not settings.kotak_totp_secret:
                self._static_totp_used = True
            if had_session:
                self._relogins += 1
            return c

    async def _call(self, fn_name, **kw):
        """Authenticated SDK call. A dead session is detected, replaced once and the call repeated once.

        Only calls that provably did nothing are repeated: an auth failure is refused by the gateway
        before any processing. Other errors never drop the session, except after
        KOTAK_RELOGIN_AFTER_ERRORS in a row with no success, which is treated as a silent expiry."""
        label = f'KOTAK_{fn_name.upper()}'
        for attempt in (1, 2):
            c = await self.session()
            try:
                resp = await asyncio.to_thread(getattr(c, fn_name), **kw)
            except Exception as exc:  # noqa: BLE001
                if kotak_auth_failure(exc):
                    if not self._relogin_allowed():
                        raise RuntimeError(f'{label}_AUTH_ERROR:RELOGIN_BUDGET_EXHAUSTED') from exc
                    self._expire_session(f'{fn_name}:{type(exc).__name__}')
                    if attempt == 1:
                        continue
                    raise RuntimeError(f'{label}_AUTH_ERROR') from exc
                self._note_bad_call()
                raise
            auth = kotak_auth_failure(resp)
            if auth == AUTH_LIKELY and attempt == 2:
                self._note_bad_call()  # still 'likely' right after a fresh login: an ordinary error, not a second expiry
                raise RuntimeError(f'{label}_ERROR')
            if auth:
                if not self._relogin_allowed():
                    raise RuntimeError(f'{label}_AUTH_ERROR:RELOGIN_BUDGET_EXHAUSTED')
                self._expire_session(f'{fn_name}:{auth}')
                if attempt == 1:
                    continue
                raise RuntimeError(f'{label}_AUTH_ERROR')
            if isinstance(resp, dict) and ('Error' in resp or 'Error Message' in resp or 'error' in resp):
                self._note_bad_call()
                raise RuntimeError(f'{label}_ERROR')
            self._mark_ok()
            return resp

    def _relogin_allowed(self):
        """At most KOTAK_RELOGIN_BUDGET expiry-driven re-logins in 15 minutes: a misread error can never
        turn into a login storm. The budget refills by itself; it is not a credential halt."""
        now = self._wall()
        self._relogin_times = [x for x in self._relogin_times if now - x < 900]
        if len(self._relogin_times) >= max(1, settings.kotak_relogin_budget):
            self._last_error = 'RELOGIN_BUDGET_EXHAUSTED:too many session expiries in 15 min; waiting'
            return False
        self._relogin_times.append(now)
        return True

    def _note_bad_call(self):
        self._bad_calls += 1
        if self._bad_calls >= max(1, settings.kotak_relogin_after_errors):
            self._expire_session(f'{self._bad_calls}_CONSECUTIVE_ERRORS')
            self._bad_calls = 0

    async def health(self):
        """LIVE only when an authenticated call has actually succeeded recently. A cached session
        object proves nothing: the old check reported LIVE for hours after Kotak had ended the session."""
        if not self.configured():
            return {'broker': self.name, 'status': 'UNCONFIGURED'}
        window = max(1.0, settings.broker_health_ttl_sec * 3)
        # Health never waits for a login (that can take most of a minute) and never starts one: the feed,
        # the monitors and the pre-open task log in. It reports what is known right now.
        if self._logging_in():
            return {'broker': self.name, 'status': 'LOGGING_IN', 'login': self.login_state()}
        if self._halted:
            return {'broker': self.name, 'status': 'LOGIN_HALTED', 'error': str(self._last_error)[:160], 'login': self.login_state()}
        if not self._fresh():
            err = str(self._last_error or '')
            status = ('SESSION_EXPIRED' if self._expired else 'UNREACHABLE' if err.startswith('TRANSPORT')
                      else 'CONFIG_ERROR' if err.startswith(('CONFIG', 'KOTAK_CONFIG', 'KOTAK_TOTP_SECRET')) else
                      'AUTH_ERROR' if err else 'NOT_LOGGED_IN')
            out = {'broker': self.name, 'status': status, 'login': self.login_state()}
            if self._last_error:
                out['error'] = str(self._last_error)[:160]
            return out
        try:
            if not self._last_ok_at or self._wall() - self._last_ok_at > window:
                await self._probe()
            return {'broker': self.name, 'status': 'LIVE', 'sdk': 'kotakneoapi', 'login': self.login_state()}
        except Exception as exc:  # noqa: BLE001
            status = 'LOGIN_HALTED' if self._halted else 'SESSION_EXPIRED' if self._expired else 'AUTH_ERROR'
            return {'broker': self.name, 'status': status, 'error': (str(exc) or type(exc).__name__)[:160], 'login': self.login_state()}

    async def _probe(self):
        """One cheap authenticated read on the current session. It never logs in: a dead session is
        dropped (the monitors and the feed log in again) and reported as expired."""
        c = self._session
        try:
            resp = await asyncio.wait_for(asyncio.to_thread(c.limits), 15)
        except asyncio.TimeoutError as exc:
            raise RuntimeError('KOTAK_PROBE_TIMEOUT') from exc
        except Exception as exc:  # noqa: BLE001
            if kotak_auth_failure(exc):
                self._expire_session(f'limits:{type(exc).__name__}')
                raise RuntimeError('KOTAK_SESSION_EXPIRED') from exc
            raise
        auth = kotak_auth_failure(resp)
        if auth:
            self._expire_session(f'limits:{auth}')
            raise RuntimeError('KOTAK_SESSION_EXPIRED')
        if isinstance(resp, dict) and ('Error' in resp or 'error' in resp):
            raise RuntimeError('KOTAK_LIMITS_ERROR')
        self._mark_ok()

    async def prelogin(self, now=None):
        """Log in before the open when the session is missing or from an earlier day, so the first
        order or feed connect of the day does not pay for (or fail on) a login. Returns what happened."""
        now = (now or now_ist()).astimezone(IST)
        try:
            hh, mm = (int(x) for x in str(settings.kotak_prelogin_ist).split(':'))
        except ValueError:
            return 'DISABLED'
        if not self.configured() or now.weekday() >= 5 or (now.hour, now.minute) < (hh, mm):
            return 'NOT_DUE'
        if self._halted:
            return 'HALTED'
        if self._fresh():
            return 'FRESH'
        try:
            await self.session()
            return 'LOGGED_IN'
        except Exception as exc:  # noqa: BLE001 - backoff and halt state are already recorded
            return f'FAILED:{str(exc)[:80]}'

    async def aclose(self):
        c, self._session = self._session, None
        if c is not None and hasattr(c, 'logout'):  # the SDK only clears its tokens locally; still scrub them
            try:
                await asyncio.wait_for(asyncio.to_thread(c.logout), 2)
            except Exception:  # noqa: BLE001
                pass
        await super().aclose()

    async def scrip_master_files(self):
        """Today's scrip-master CSV URLs. Kotak's file-paths API needs only the consumer key,
        so this never logs in and never touches the login backoff or lockout counters."""
        if not settings.kotak_api_key:
            raise RuntimeError('KOTAK_CONSUMER_KEY_MISSING')
        factory = self._factory
        if factory is None:
            from neo_api_client import NeoAPI as factory
        c = factory(consumer_key=settings.kotak_api_key, environment='prod')
        resp = await asyncio.to_thread(c.scrip_master)
        files = resp.get('filesPaths') if isinstance(resp, dict) else None
        if not isinstance(files, list) or not files:
            raise RuntimeError(f'KOTAK_SCRIP_MASTER_UNAVAILABLE:{str(resp)[:150]}')
        return [str(f) for f in files if f]

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
            if kotak_auth_failure(exc):  # HTTP 401/403: the gateway refused before any processing
                self._expire_session(f'place_order:{type(exc).__name__}')
                return {'status': 'REJECTED', 'reason': 'NOT_SENT:KOTAK_SESSION', 'broker': self.name}
            return {'status': 'UNKNOWN', 'reason': 'AMBIGUOUS:' + type(exc).__name__, 'broker': self.name}
        if not isinstance(resp, dict):
            return {'status': 'UNKNOWN', 'reason': 'NON_DICT_RESPONSE', 'broker': self.name}
        auth = kotak_auth_failure(resp)
        if auth == AUTH_PROVEN:
            # The SDK refuses before sending when the session is not 2FA-complete, and the gateway
            # refuses an invalid token before the order reaches the OMS. Either way nothing was sent.
            self._expire_session('place_order')
            return {'status': 'REJECTED', 'reason': 'NOT_SENT:KOTAK_SESSION', 'broker': self.name}
        if auth == AUTH_LIKELY:
            self._expire_session('place_order:LIKELY')  # drop the session, but the outcome stays ambiguous below
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
