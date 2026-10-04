"""Live broker stream workers. Output is canonical Tick objects or STREAM_ERROR events.

Zerodha, Upstox and Angel SDKs deliver callbacks on their own threads. The
original pushed into asyncio.Queue directly from those threads; asyncio queues
are not thread-safe, so the event loop was not woken and ticks sat unread until
some unrelated event arrived. ThreadBridge hands items over with
loop.call_soon_threadsafe and drops the OLDEST item when full (newest market
state wins), counting every drop.
"""
import asyncio
import json
import time

from .clock import in_session
from .config import settings
from .models import Tick


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
    commodity = False

    def __init__(self, broker, handler, on_event=None):
        self.broker = broker
        self.handler = handler
        self.on_event = on_event
        self.stop = False
        self.healthy = False
        self.running = False
        self.last_error = None
        self.reconnects = 0
        self.bridge = None

    async def _event(self, e):
        if self.on_event:
            await self.on_event(e)

    async def run_forever(self):
        backoff = 1
        self.running = True
        while not self.stop:
            try:
                await self.run_once()
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - every failure is reported and retried with backoff
                self.healthy = False
                self.last_error = str(e)[:200]
                self.reconnects += 1
                await self._event({'type': 'STREAM_ERROR', 'broker': self.broker, 'error': self.last_error})
                await asyncio.sleep(backoff)
                backoff = min(30, backoff * 2)
        self.running = False

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
                await self.handler(Tick(broker='ZERODHA', exchange='', instrument_token=str(raw['instrument_token']), symbol='',
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
                feed_ts = _ms(msg.get('currentTs')) or _now()
                for key, feed in (msg.get('feeds') or {}).items():
                    ff = (feed.get('fullFeed') or {}) if isinstance(feed, dict) else {}
                    market = ff.get('marketFF') or ff.get('indexFF') or {}
                    lt = market.get('ltpc') or {}
                    if not lt:
                        continue
                    quotes = (market.get('marketLevel') or {}).get('bidAskQuote') or []
                    q0 = quotes[0] if quotes else {}
                    iv = market.get('iv')
                    await self.handler(Tick(broker='UPSTOX', exchange=key.split('|')[0] if '|' in key else '', instrument_token=key,
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
                await self.handler(Tick(broker='ANGEL', exchange=str(raw.get('exchange_type') or ''), instrument_token=str(raw.get('token', '')),
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

    def __init__(self, handler, tokens, session_provider, on_event=None):
        super().__init__('KOTAK', handler, on_event)
        self.tokens = tokens
        self.session_provider = session_provider  # Kotak adapter: reuse its authenticated session
        self.dynamic = set()   # (segment, token) pairs managed at runtime (option chain around spot)
        self.ws = None         # live websocket while connected
        self._sub_lock = asyncio.Lock()

    def _static_scrips(self):
        return set(parse_kotak_subscriptions(self.tokens)[0])

    async def set_dynamic(self, pairs):
        """Replace the runtime-managed scrip set. While connected, only the difference is
        sent; a failed (un)subscribe leaves that part pending for the next call. While
        disconnected the set is stored and subscribed in full on the next connect."""
        from neo_api_client.websocket.feed import WsToken
        pairs, static = set(pairs), self._static_scrips()
        async with self._sub_lock:
            add = sorted(pairs - self.dynamic - static)
            remove = sorted(self.dynamic - pairs - static)
            ws = self.ws
            if ws is None:
                self.dynamic = pairs
            else:
                if remove:
                    await ws.unsubscribe_scrips([WsToken(seg, tok) for seg, tok in remove])
                    self.dynamic -= set(remove)
                if add:
                    await ws.subscribe_scrips([WsToken(seg, tok) for seg, tok in add])
                    self.dynamic |= set(add)
                self.dynamic &= pairs | static
        return {'added': len(add), 'removed': len(remove), 'total': len(self.dynamic), 'connected': ws is not None}

    async def run_once(self):
        from neo_api_client.websocket.feed import SFeedIndex, SFeedScrip, WsToken
        scrips, indices = parse_kotak_subscriptions(self.tokens)
        if not scrips and not indices and not self.dynamic:
            await asyncio.sleep(1)  # nothing to stream yet (auto chain waits for the instrument master)
            return
        names = {(seg, _norm(tok)): tok for seg, tok in indices}  # packet name -> subscribed name
        underlyings = kotak_index_underlyings()
        c = await self.session_provider()
        async with c.create_websocket() as ws:
            async with self._sub_lock:
                wanted = sorted(set(scrips) | self.dynamic)
                if wanted:
                    await ws.subscribe_scrips([WsToken(seg, tok) for seg, tok in wanted])
                if indices:
                    await ws.subscribe_index([WsToken(seg, tok) for seg, tok in indices])
                self.ws = ws
            try:
                await self._consume(ws, names, underlyings, SFeedIndex, SFeedScrip)
            finally:
                self.ws = None

    async def _consume(self, ws, names, underlyings, SFeedIndex, SFeedScrip):
        it = ws.__aiter__()
        while not self.stop:
            try:
                m = await asyncio.wait_for(it.__anext__(), settings.stream_stall_sec)
            except asyncio.TimeoutError:
                if in_session(self.commodity):
                    raise StreamError('STREAM_STALLED')
                continue
            if isinstance(m, SFeedIndex):
                self.healthy = True
                sub = names.get((str(m.exchange_segment).lower(), _norm(m.name))) or \
                    names.get((str(m.exchange_segment).lower(), _norm(m.trading_symbol)))
                await self.handler(kotak_index_tick(m, sub, underlyings))
            elif isinstance(m, SFeedScrip):
                self.healthy = True
                bid = m.buy[0].price if m.buy else 0.0
                ask = m.sell[0].price if m.sell else 0.0
                await self.handler(Tick(broker='KOTAK', exchange=m.exchange_segment, instrument_token=str(m.instrument_token),
                                        symbol=m.trading_symbol or str(m.instrument_token), ltp=float(m.last_traded_price),
                                        bid=float(bid), ask=float(ask), volume=int(m.volume_traded_today or 0),
                                        oi=int(m.open_interest or 0),
                                        exchange_ts_ms=_ms(m.last_update_time) or _ms(m.last_trade_time),
                                        receive_ts_ms=_now()))
