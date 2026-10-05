"""Live broker stream workers. Output is canonical Tick objects or STREAM_ERROR events.

Zerodha, Upstox and Angel SDKs deliver callbacks on their own threads. The
original pushed into asyncio.Queue directly from those threads; asyncio queues
are not thread-safe, so the event loop was not woken and ticks sat unread until
some unrelated event arrived. ThreadBridge hands items over with
loop.call_soon_threadsafe and drops the OLDEST item when full (newest market
state wins), counting every drop.
"""
import asyncio
import contextlib
import json
import re
import time

from .clock import in_session
from .config import settings
from .models import Tick

# Seconds before reconnect attempt n, where n counts failed attempts since the last healthy connection.
# Attempt 0 (a healthy connection just dropped, or the operator pressed Reconnect) goes immediately.
RECONNECT_DELAYS = (0, 1, 2, 5, 10, 20)


def reconnect_delay(attempt, cap=None):
    cap = settings.stream_reconnect_max_sec if cap is None else cap
    return float(min(RECONNECT_DELAYS[attempt] if attempt < len(RECONNECT_DELAYS) else cap, cap))


def _now():
    return int(time.time() * 1000)


def _ms(v):
    """Epoch seconds or milliseconds -> milliseconds."""
    try:
        v = int(v)
    except (TypeError, ValueError):
        return 0
    return v * 1000 if 0 < v < 10**11 else v


class StreamError(RuntimeError):
    pass


_REFUSED = re.compile(r'invalid|unauthori[sz]ed|expired|denied|refused|forbidden|\b40[13]\b', re.I)
_NOT_REFUSAL = re.compile(r'time ?out|timed out|connection|closed|reset|unexpected auth response: none', re.I)


def token_refused(message):
    """Did the feed's auth handshake refuse the session token? The SDK raises AuthenticationError for
    network faults during the handshake too; those must not trigger a fresh broker login."""
    return bool(_REFUSED.search(message or '')) and not _NOT_REFUSAL.search(message or '')


class ThreadBridge:
    def __init__(self, loop, maxsize=20000):
        self.loop = loop
        self.q = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0

    def put(self, item):
        """Safe to call from any thread."""
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:  # loop closed during shutdown
            pass

    def _put(self, item):
        if self.q.full():
            try:
                self.q.get_nowait()
                self.dropped += 1
            except asyncio.QueueEmpty:
                pass
        self.q.put_nowait(item)

    async def get(self, timeout):
        return await asyncio.wait_for(self.q.get(), timeout)


class StreamWorker:
    """One broker feed, kept connected by a small state machine.

    IDLE → CONNECTING → CONNECTED → (drop) → RECONNECTING → CONNECTED …
    PAUSED: auto-reconnect is off and the feed dropped; LOGIN_HALTED: the broker login needs an
    operator reset; STOPPED: shut down. A drop after a healthy connection retries at once, then
    1, 2, 5, 10, 20 s, capped by STREAM_RECONNECT_MAX_SEC. A manual reconnect, a login reset or
    turning auto-reconnect on wakes any wait immediately.
    """
    commodity = False

    def __init__(self, broker, handler, on_event=None):
        self.broker = broker
        self.handler = handler
        self.on_event = on_event
        self.stop = False
        self.healthy = False
        self.running = False
        self.last_error = None
        self.reconnects = 0          # recoveries: a connection re-established after a drop
        self.failures = 0            # drops plus failed connection attempts
        self.bridge = None
        self.state = 'IDLE'
        self.auto_reconnect = settings.stream_auto_reconnect
        self.attempt = 0             # failed attempts since the last healthy connection
        self.connected_since = None  # epoch seconds
        self.down_since = None
        self.last_disconnect_at = None
        self.last_disconnect_reason = None
        self.last_message_at = None
        self.next_retry_at = None
        self.downtime_total_sec = 0.0
        self._ever_connected = False
        self._got_data = False
        self._wake = asyncio.Event()
        self._kick = None            # reason of a pending manual reconnect
        self._relogin = False        # force a fresh broker login before the next connect
        self._conn_task = None
        self.handler_errors = 0
        self.last_handler_error = None

    async def _event(self, e):
        if self.on_event:
            try:
                await self.on_event(e)
            except Exception:  # noqa: BLE001 - telemetry must never take the feed down
                pass

    async def _deliver(self, tick):
        """Hand a tick to the terminal. A failure downstream (for example the ledger being briefly
        unavailable) is counted, never allowed to drop a healthy broker connection."""
        try:
            await self.handler(tick)
        except Exception as exc:  # noqa: BLE001
            self.handler_errors += 1
            self.last_handler_error = f'{type(exc).__name__}: {str(exc)[:120]}'

    # -------------------------------------------------------------- operator controls
    def request_reconnect(self, relogin=False, reason='MANUAL_RECONNECT'):
        """Drop the current connection (if any) and connect again now, skipping any backoff wait."""
        self._relogin = self._relogin or bool(relogin)
        self.attempt = 0
        task = self._conn_task
        if task is not None and not task.done():
            self._kick = reason
            task.cancel()
        self._wake.set()
        return {'broker': self.broker, 'state': self.state, 'requested': reason, 'relogin': self._relogin}

    def set_auto_reconnect(self, enabled):
        self.auto_reconnect = bool(enabled)
        if self.auto_reconnect and self.state in ('PAUSED', 'RECONNECTING'):
            self.attempt = 0
            self._wake.set()
        return {'broker': self.broker, 'auto_reconnect': self.auto_reconnect, 'state': self.state}

    def status(self):
        now = time.time()
        return {'broker': self.broker, 'state': self.state, 'running': self.running, 'healthy': self.state == 'CONNECTED',
                'auto_reconnect': self.auto_reconnect, 'attempt': self.attempt,
                'next_retry_in': round(max(0.0, self.next_retry_at - now), 1) if self.next_retry_at else None,
                'connected_since': self.connected_since,
                'uptime_sec': round(now - self.connected_since) if self.connected_since and self.state == 'CONNECTED' else None,
                'downtime_sec': round(now - self.down_since) if self.down_since else None,
                'downtime_total_sec': round(self.downtime_total_sec + (now - self.down_since if self.down_since else 0), 1),
                'last_disconnect_at': self.last_disconnect_at, 'last_disconnect_reason': self.last_disconnect_reason,
                'last_message_age_sec': round(now - self.last_message_at, 1) if self.last_message_at else None,
                'last_error': self.last_error, 'reconnects': self.reconnects, 'failures': self.failures,
                'dropped': self.bridge.dropped if self.bridge else 0, 'dynamic_tokens': len(getattr(self, 'dynamic', ())),
                'handler_errors': self.handler_errors, 'last_handler_error': self.last_handler_error}

    # -------------------------------------------------------------- state transitions
    async def _connected(self):
        """Subclasses call this once the socket is open and subscribed; the first data item also counts."""
        if self.state == 'CONNECTED':
            return
        now = time.time()
        downtime = now - self.down_since if self.down_since else 0.0
        recovered = self._ever_connected
        self.downtime_total_sec += downtime
        self.state, self.connected_since, self.down_since, self.next_retry_at = 'CONNECTED', now, None, None
        self.healthy, self.last_error, self._ever_connected = True, None, True
        if recovered:
            self.reconnects += 1
        await self._event({'type': 'STREAM_CONNECTED', 'broker': self.broker, 'recovered': recovered,
                           'downtime_sec': round(downtime, 1)})

    async def _on_data(self):
        self.last_message_at = time.time()
        self._got_data = True
        if self.state != 'CONNECTED':
            await self._connected()

    async def _wait(self, delay):
        """Sleep up to `delay` seconds (None: until woken). A reconnect request or stop wakes it early."""
        if not self._wake.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                if delay is None:
                    await self._wake.wait()
                elif delay > 0:
                    await asyncio.wait_for(self._wake.wait(), delay)
        self._wake.clear()

    def wake(self):
        self._wake.set()

    def wake_if_waiting(self, reason='LOGIN_RESET'):
        """Retry now if the feed is waiting (backoff, pause or login halt); a connected feed is left alone."""
        if self.state in ('RECONNECTING', 'PAUSED', 'LOGIN_HALTED', 'IDLE'):
            self.attempt = 0
            self._wake.set()
            return True
        return False

    async def run_forever(self):
        self.running = True
        try:
            while not self.stop:
                if self.state != 'RECONNECTING':
                    self.state = 'CONNECTING'
                self._kick, self._got_data = None, False
                self._wake.clear()  # a wake left over from an earlier toggle must not skip the next wait or pause
                reason = None
                try:
                    self._conn_task = asyncio.ensure_future(self.run_once())
                    await self._conn_task
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if self._kick is None or (task is not None and task.cancelling()):
                        raise  # the worker itself is being shut down
                    reason = self._kick
                except Exception as e:  # noqa: BLE001 - every failure is reported and retried
                    reason = str(e)[:200] or type(e).__name__
                    if type(e).__name__ == 'AuthenticationError' and token_refused(str(e)):
                        self._relogin = True  # the feed refused the session token: log in again first
                finally:
                    self._conn_task = None
                if self.stop:
                    break
                if reason is None:
                    if self.state == 'CONNECTED':
                        reason = 'STREAM_ENDED'
                    else:
                        continue  # nothing to stream yet (run_once waited); not a failure
                await self._after_drop(reason)
        finally:
            self.running = False
            self.state = 'STOPPED'

    async def _after_drop(self, reason):
        now = time.time()
        manual = reason == self._kick and self._kick is not None
        was_connected = self.state == 'CONNECTED'
        healthy = was_connected and (self._got_data or now - (self.connected_since or now) >= settings.stream_healthy_reset_sec)
        self.healthy = False
        self.failures += 1
        self.last_error = reason
        if was_connected:
            self.down_since = now
            self.last_disconnect_at, self.last_disconnect_reason = now, reason
        elif self.down_since is None:
            self.down_since = now
        self.attempt = 0 if (healthy or manual) else self.attempt + 1
        if was_connected:
            await self._event({'type': 'STREAM_DISCONNECTED', 'broker': self.broker, 'reason': reason,
                               'uptime_sec': round(now - (self.connected_since or now))})
        self.connected_since = None
        if 'LOGIN_HALTED' in reason:
            self.state, self.next_retry_at = 'LOGIN_HALTED', None
            await self._event({'type': 'STREAM_ERROR', 'broker': self.broker, 'error': reason, 'next_retry_in': None})
            await self._wait(300)  # a login reset wakes this at once
            self.state = 'RECONNECTING'
            return
        if not self.auto_reconnect and not manual:
            self.state, self.next_retry_at = 'PAUSED', None
            await self._event({'type': 'STREAM_ERROR', 'broker': self.broker, 'error': reason, 'next_retry_in': None})
            await self._wait(None)  # Reconnect now, or turning auto-reconnect back on
            self.state = 'RECONNECTING'
            return
        delay = 0.0 if manual else reconnect_delay(self.attempt)
        self.state, self.next_retry_at = 'RECONNECTING', now + delay
        if not was_connected:
            await self._event({'type': 'STREAM_ERROR', 'broker': self.broker, 'error': reason, 'attempt': self.attempt,
                               'next_retry_in': delay})
        await self._wait(delay)
        self.next_retry_at = None

    async def next_item(self):
        """Next SDK item; raises on SDK error or on a stall during session hours."""
        while True:
            try:
                item = await self.bridge.get(settings.stream_stall_sec)
            except asyncio.TimeoutError:
                if in_session(self.commodity):
                    raise StreamError('STREAM_STALLED')
                continue
            if isinstance(item, dict) and '__stream_error__' in item:
                raise StreamError(item['__stream_error__'])
            self.healthy = True
            await self._on_data()
            return item

    async def run_once(self):
        raise NotImplementedError


class ZerodhaStreamWorker(StreamWorker):
    def __init__(self, handler, tokens, on_event=None):
        super().__init__('ZERODHA', handler, on_event)
        self.tokens = [int(t) for t in tokens]

    async def run_once(self):
        if not settings.zerodha_api_key or not settings.zerodha_access_token:
            raise StreamError('ZERODHA_CREDENTIALS_MISSING')
        from kiteconnect import KiteTicker
        self.bridge = ThreadBridge(asyncio.get_running_loop())
        b = self.bridge
        ticker = KiteTicker(settings.zerodha_api_key, settings.zerodha_access_token)

        def on_ticks(ws, ticks):
            for x in ticks:
                b.put(x)

        def on_connect(ws, response):
            ws.subscribe(self.tokens)
            ws.set_mode(ws.MODE_FULL, self.tokens)

        ticker.on_ticks = on_ticks
        ticker.on_connect = on_connect
        ticker.on_error = lambda ws, code, reason: b.put({'__stream_error__': f'KITE_ERROR:{code}:{reason}'})
        ticker.on_close = lambda ws, code, reason: b.put({'__stream_error__': f'KITE_CLOSED:{code}:{reason}'})
        ticker.on_noreconnect = lambda ws: b.put({'__stream_error__': 'KITE_NO_RECONNECT'})
        ticker.connect(threaded=True)
        try:
            while not self.stop:
                raw = await self.next_item()
                ts = raw.get('exchange_timestamp')
                depth = raw.get('depth') or {}
                buy = (depth.get('buy') or [{}])[0]
                sell = (depth.get('sell') or [{}])[0]
                await self._deliver(Tick(broker='ZERODHA', exchange='', instrument_token=str(raw['instrument_token']), symbol='',
                                        ltp=float(raw.get('last_price') or 0), bid=float(buy.get('price') or 0),
                                        ask=float(sell.get('price') or 0), volume=int(raw.get('volume_traded') or 0),
                                        oi=int(raw.get('oi') or 0), exchange_ts_ms=int(ts.timestamp() * 1000) if ts else 0,
                                        receive_ts_ms=_now()))
        finally:
            try:
                ticker.close()  # close(), never stop(): Twisted's reactor cannot be restarted
            except Exception:  # noqa: BLE001
                pass


class UpstoxStreamWorker(StreamWorker):
    def __init__(self, handler, keys, on_event=None):
        super().__init__('UPSTOX', handler, on_event)
        self.keys = list(keys)

    async def run_once(self):
        if not settings.upstox_access_token:
            raise StreamError('UPSTOX_ACCESS_TOKEN_MISSING')
        import upstox_client
        self.bridge = ThreadBridge(asyncio.get_running_loop())
        b = self.bridge
        cfg = upstox_client.Configuration()
        cfg.access_token = settings.upstox_access_token
        streamer = upstox_client.MarketDataStreamerV3(upstox_client.ApiClient(cfg), self.keys, 'full')
        # Streamer.emit(event, *args): OPEN -> (), MESSAGE -> (dict,), ERROR -> (err,), CLOSE -> (code, msg).
        # The original handlers took one argument too many for OPEN and ERROR and raised TypeError.
        streamer.on('open', lambda: None)
        streamer.on('message', b.put)
        streamer.on('error', lambda err: b.put({'__stream_error__': f'UPSTOX_ERROR:{err}'}))
        streamer.on('close', lambda code, msg: b.put({'__stream_error__': f'UPSTOX_CLOSED:{code}:{msg}'}))
        await asyncio.get_running_loop().run_in_executor(None, streamer.connect)
        try:
            while not self.stop:
                msg = await self.next_item()
                if not isinstance(msg, dict):
                    continue
                feed_ts = _ms(msg.get('currentTs'))  # no feed timestamp: 0, and the quality gate rejects the tick
                for key, feed in (msg.get('feeds') or {}).items():
                    ff = (feed.get('fullFeed') or {}) if isinstance(feed, dict) else {}
                    market = ff.get('marketFF') or ff.get('indexFF') or {}
                    lt = market.get('ltpc') or {}
                    if not lt:
                        continue
                    quotes = (market.get('marketLevel') or {}).get('bidAskQuote') or []
                    q0 = quotes[0] if quotes else {}
                    iv = market.get('iv')
                    await self._deliver(Tick(broker='UPSTOX', exchange=key.split('|')[0] if '|' in key else '', instrument_token=key,
                                            symbol=key, ltp=float(lt.get('ltp') or 0), bid=float(q0.get('bidP') or 0),
                                            ask=float(q0.get('askP') or 0), volume=int(float(market.get('vtt') or 0)),
                                            oi=int(float(market.get('oi') or 0)), iv=float(iv) if iv not in (None, '') else None,
                                            # Feed publication time, not last-trade time: illiquid strikes with an
                                            # old last trade still have a live quote.
                                            exchange_ts_ms=feed_ts, receive_ts_ms=_now()))
        finally:
            try:
                streamer.disconnect()
            except Exception:  # noqa: BLE001
                pass


class AngelStreamWorker(StreamWorker):
    EXCHANGE_TYPE = {'NSE': 1, 'NSE_CM': 1, 'NFO': 2, 'NSE_FO': 2, 'BSE': 3, 'BSE_CM': 3, 'BFO': 4, 'BSE_FO': 4, 'MCX': 5, 'MCX_FO': 5}

    def __init__(self, handler, tokens, on_event=None):
        super().__init__('ANGEL', handler, on_event)
        self.tokens = tokens

    async def run_once(self):
        if not all([settings.angel_access_token, settings.angel_api_key, settings.angel_client_code, settings.angel_feed_token]):
            raise StreamError('ANGEL_STREAM_CREDENTIALS_MISSING')
        from SmartApi.smartWebSocketV2 import SmartWebSocketV2
        loop = asyncio.get_running_loop()
        self.bridge = ThreadBridge(loop)
        b = self.bridge
        sws = SmartWebSocketV2(settings.angel_access_token, settings.angel_api_key, settings.angel_client_code,
                               settings.angel_feed_token, max_retry_attempt=3)
        grouped = {}
        for x in self.tokens:
            if isinstance(x, dict):
                seg, tok = str(x.get('segment', 'NFO')).upper(), str(x.get('token', ''))
            else:
                raw = str(x)
                seg, tok = raw.split('|', 1) if '|' in raw else ('NFO', raw)
            grouped.setdefault(self.EXCHANGE_TYPE.get(seg.upper(), 2), []).append(tok)
        exchange_tokens = [{'exchangeType': k, 'tokens': v} for k, v in grouped.items()]
        sws.on_data = lambda ws, msg: b.put(msg)
        sws.on_open = lambda ws: sws.subscribe('IORT', sws.SNAP_QUOTE, exchange_tokens)
        sws.on_error = lambda *a: b.put({'__stream_error__': 'ANGEL_ERROR:' + ':'.join(str(x) for x in a)})
        sws.on_close = lambda *a: b.put({'__stream_error__': 'ANGEL_CLOSED'})
        connect = loop.run_in_executor(None, sws.connect)  # blocks until the socket closes
        connect.add_done_callback(lambda f: b.put({'__stream_error__': 'ANGEL_SOCKET_ENDED'}))
        try:
            while not self.stop:
                raw = await self.next_item()
                if not isinstance(raw, dict) or 'last_traded_price' not in raw:
                    continue
                buy = (raw.get('best_5_buy_data') or [{}])[0]
                sell = (raw.get('best_5_sell_data') or [{}])[0]
                await self._deliver(Tick(broker='ANGEL', exchange=str(raw.get('exchange_type') or ''), instrument_token=str(raw.get('token', '')),
                                        symbol=str(raw.get('token', '')), ltp=float(raw['last_traded_price']) / 100,
                                        bid=float(buy.get('price') or 0) / 100, ask=float(sell.get('price') or 0) / 100,
                                        volume=int(raw.get('volume_trade_for_the_day') or 0), oi=int(raw.get('open_interest') or 0),
                                        sequence=int(raw.get('sequence_number') or 0) or None,
                                        exchange_ts_ms=_ms(raw.get('exchange_timestamp')), receive_ts_ms=_now()))
        finally:
            try:
                sws.close_connection()
            except Exception:  # noqa: BLE001
                pass


# Kotak streams indices by name on the cash segments (WsToken("nse_cm", "Nifty 50")).
# Index ticks carry the F&O underlying name so the option chain can use them as spot.
KOTAK_INDEX_UNDERLYINGS = {
    'nifty 50': 'NIFTY', 'nifty bank': 'BANKNIFTY', 'nifty fin service': 'FINNIFTY',
    'nifty mid select': 'MIDCPNIFTY', 'nifty next 50': 'NIFTYNXT50', 'sensex': 'SENSEX', 'bankex': 'BANKEX',
}
KOTAK_SEGMENTS = {'nse_cm', 'bse_cm', 'nse_fo', 'bse_fo', 'mcx_fo'}
KOTAK_INDEX_SEGMENTS = {'nse_cm', 'bse_cm'}
_INDEX_EXCHANGE = {'nse_cm': 'NSE', 'bse_cm': 'BSE'}


def _norm(name):
    return ' '.join(str(name or '').lower().split())


def kotak_index_underlyings():
    """Built-in index-name -> underlying map, extended or overridden by KOTAK_INDEX_UNDERLYINGS_JSON."""
    out = dict(KOTAK_INDEX_UNDERLYINGS)
    try:
        extra = json.loads(settings.kotak_index_underlyings_json or '{}')
    except ValueError:
        extra = {}
    if isinstance(extra, dict):
        out.update({_norm(k): str(v).upper() for k, v in extra.items() if v})
    return out


def parse_kotak_subscriptions(tokens):
    """Split SUBSCRIPTION_JSON["KOTAK"] into (scrips, indices), each a list of (segment, token).

    Scrips have numeric tokens: "nse_fo|48201", bare "48201" (nse_fo), or {"segment", "token"}.
    Indices are named on a cash segment: "nse_cm|Nifty 50", bare "Nifty 50" (nse_cm), or
    {"segment": "nse_cm", "token": "Nifty 50", "index": true}.
    """
    scrips, indices = [], []
    for x in tokens or []:
        if isinstance(x, dict):
            tok = str(x.get('token', '')).strip()
            flag = x.get('index')
            seg = str(x.get('segment') or ('nse_cm' if flag or not tok.isdigit() else 'nse_fo')).strip().lower()
        else:
            raw = str(x).strip()
            if '|' in raw:
                seg, tok = (p.strip() for p in raw.split('|', 1))
                seg = seg.lower()
            else:
                tok = raw
                seg = 'nse_fo' if raw.isdigit() else 'nse_cm'
            flag = None
        if seg not in KOTAK_SEGMENTS or not tok:
            raise StreamError(f'INVALID_KOTAK_SUBSCRIPTION:{x}')
        is_index = flag if flag is not None else not tok.isdigit()
        if is_index:
            if seg not in KOTAK_INDEX_SEGMENTS:
                raise StreamError(f'KOTAK_INDEX_NEEDS_CASH_SEGMENT:{x}')
            indices.append((seg, tok))
        else:
            scrips.append((seg, tok))
    return scrips, indices


def kotak_index_tick(m, subscribed_name=None, underlyings=None, now_ms=None):
    """Tick for an SFeedIndex message. The tick is keyed by the name it was subscribed with
    (so UNDERLYING_SPOT_TOKENS_JSON can name it), has no bid/ask (an index is not traded),
    and keeps the feed's own timestamp: a missing one stays 0 and the quality gate rejects it."""
    name = subscribed_name or (m.name or '').strip() or m.trading_symbol or str(m.instrument_token)
    table = underlyings if underlyings is not None else kotak_index_underlyings()
    und = table.get(_norm(name)) or table.get(_norm(m.name))
    seg = str(m.exchange_segment or '').lower()
    return Tick(broker='KOTAK', exchange=_INDEX_EXCHANGE.get(seg, seg.upper()), instrument_token=name, symbol=und or name,
                underlying=und, ltp=float(m.last_traded_price), exchange_ts_ms=_ms(m.last_trade_time),
                receive_ts_ms=now_ms or _now())


class KotakStreamWorker(StreamWorker):
    SEGMENTS = KOTAK_SEGMENTS

    # The SDK client must not run its own reconnect loop (5 s delay, gives up after 5 tries) or retry
    # the first connect: the worker owns the single reconnect path, so a drop is retried at once.
    SOCKET_OPTIONS = dict(max_reconnect_attempts=0, max_connect_retries=0, reconnect_delay=1, ping_interval=10)
    CLOSE_TIMEOUT = 3.0  # a dead peer would otherwise hold the close handshake for 10 s

    def __init__(self, handler, tokens, session_provider, on_event=None, session_peek=None):
        super().__init__('KOTAK', handler, on_event)
        self.tokens = tokens
        self.session_provider = session_provider  # Kotak adapter: reuse its authenticated session
        self.session_peek = session_peek          # returns the adapter's current session without logging in
        self.dynamic = set()   # (segment, token) pairs managed at runtime and currently subscribed
        self._desired = None   # the last set asked for by set_dynamic (subscribed in full on every connect)
        self.ws = None         # live websocket while connected
        self._conn_session = None
        self._sub_lock = asyncio.Lock()

    def _static_scrips(self):
        return set(parse_kotak_subscriptions(self.tokens)[0])

    async def set_dynamic(self, pairs):
        """Replace the runtime-managed scrip set. While connected, only the difference is
        sent; a failed (un)subscribe leaves that part pending and reconnects the feed, which
        subscribes the full set. While disconnected the set is stored for the next connect."""
        from neo_api_client.websocket.feed import WsToken
        pairs, static = set(pairs), self._static_scrips()
        async with self._sub_lock:
            self._desired = set(pairs)
            add = sorted(pairs - self.dynamic - static)
            remove = sorted(self.dynamic - pairs - static)
            ws = self.ws
            if ws is None:
                self.dynamic = pairs
            else:
                try:
                    if remove:
                        await ws.unsubscribe_scrips([WsToken(seg, tok) for seg, tok in remove])
                        self.dynamic -= set(remove)
                    if add:
                        await ws.subscribe_scrips([WsToken(seg, tok) for seg, tok in add])
                        self.dynamic |= set(add)
                except Exception:
                    self.request_reconnect(reason='SUBSCRIBE_FAILED')  # a fresh connect subscribes everything
                    raise
                self.dynamic &= pairs | static
        return {'added': len(add), 'removed': len(remove), 'total': len(self.dynamic), 'connected': ws is not None}

    async def run_once(self):
        from neo_api_client.websocket.feed import SFeedIndex, SFeedScrip, WsToken
        scrips, indices = parse_kotak_subscriptions(self.tokens)
        dynamic = set(self._desired) if self._desired is not None else set(self.dynamic)
        if not scrips and not indices and not dynamic:
            self.state = 'IDLE'
            await asyncio.sleep(1)  # nothing to stream yet (auto chain waits for the instrument master)
            return
        # MCX trades until 23:30: with a commodity subscription a quiet feed counts as stalled until then.
        self.commodity = any(seg == 'mcx_fo' for seg, _ in list(scrips) + list(dynamic))
        names = {(seg, _norm(tok)): tok for seg, tok in indices}  # packet name -> subscribed name
        underlyings = kotak_index_underlyings()
        relogin = self._relogin or self.attempt == max(1, settings.stream_relogin_after_failures)
        self._relogin = False
        if relogin and self.session_peek is not None and self.session_peek() not in (None, self._conn_session):
            relogin = False  # a newer session already exists: use it instead of logging in again
        c = await (self.session_provider(force=True) if relogin else self.session_provider())
        self._conn_session = c
        conn = c.create_websocket(**self.SOCKET_OPTIONS)
        try:
            ws = await conn.__aenter__()
            async with self._sub_lock:
                static = set(scrips)
                wanted = sorted(static | dynamic)
                if wanted:
                    await ws.subscribe_scrips([WsToken(seg, tok) for seg, tok in wanted])
                if indices:
                    await ws.subscribe_index([WsToken(seg, tok) for seg, tok in indices])
                self.dynamic = dynamic - static
                self.ws = ws
            await self._connected()
            await self._consume(ws, names, underlyings, SFeedIndex, SFeedScrip)
        finally:
            self.ws = None
            with contextlib.suppress(Exception):
                await asyncio.wait_for(conn.__aexit__(None, None, None), self.CLOSE_TIMEOUT)

    async def _consume(self, ws, names, underlyings, SFeedIndex, SFeedScrip):
        it = ws.__aiter__()
        stall = max(1.0, float(settings.kotak_stream_stall_sec))
        last = time.monotonic()
        while not self.stop:
            # A new login (pre-open, or after Kotak ended the session) makes this socket's token stale. A session
            # merely dropped by the REST side (None) does not: keep streaming until a replacement exists.
            if self.session_peek is not None:
                current = self.session_peek()
                if current is not None and current is not self._conn_session:
                    raise StreamError('SESSION_REPLACED')
            try:
                m = await asyncio.wait_for(it.__anext__(), min(stall, 5.0))
            except asyncio.TimeoutError:
                if time.monotonic() - last >= stall and in_session(self.commodity):
                    raise StreamError('STREAM_STALLED')
                continue
            except StopAsyncIteration:
                raise StreamError('SERVER_CLOSED') from None  # the SDK ends iteration when the socket closes
            last = time.monotonic()
            if isinstance(m, SFeedIndex):
                await self._on_data()
                sub = names.get((str(m.exchange_segment).lower(), _norm(m.name))) or \
                    names.get((str(m.exchange_segment).lower(), _norm(m.trading_symbol)))
                await self._deliver(kotak_index_tick(m, sub, underlyings))
            elif isinstance(m, SFeedScrip):
                await self._on_data()
                bid = m.buy[0].price if m.buy else 0.0
                ask = m.sell[0].price if m.sell else 0.0
                await self._deliver(Tick(broker='KOTAK', exchange=m.exchange_segment, instrument_token=str(m.instrument_token),
                                        symbol=m.trading_symbol or str(m.instrument_token), ltp=float(m.last_traded_price),
                                        bid=float(bid), ask=float(ask), volume=int(m.volume_traded_today or 0),
                                        oi=int(m.open_interest or 0),
                                        exchange_ts_ms=_ms(m.last_update_time) or _ms(m.last_trade_time),
                                        receive_ts_ms=_now()))
