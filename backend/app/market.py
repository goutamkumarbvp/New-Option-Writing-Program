"""Market-data gateway: quality gates, per-broker state and bounded history.

Ticks are keyed by (broker, token). Different brokers use overlapping numeric
token spaces, so the original token-only key let one broker's tick masquerade
as another broker's instrument.
"""
import time
from collections import Counter, defaultdict, deque

from .config import settings
from .models import Tick


def now_ms():
    return int(time.time() * 1000)


def key(broker, token):
    return f'{str(broker).upper()}:{token}'


class MarketDataGateway:
    def __init__(self, history=600):
        self.last = {}
        self.prev = {}
        self.history = defaultdict(lambda: deque(maxlen=history))  # key -> (receive_ms, ltp, oi)
        self.heartbeat = {}
        self.rejected = deque(maxlen=500)
        self.reject_counts = Counter()
        self.accepted = 0
        self.skew = defaultdict(lambda: deque(maxlen=200))  # broker -> receive - exchange (ms)

    def ingest(self, t: Tick):
        now = now_ms()
        reasons = []
        if not t.exchange_ts_ms:
            reasons.append('MISSING_EXCHANGE_TIMESTAMP')
        else:
            if t.exchange_ts_ms - now > settings.max_clock_skew_ms:
                reasons.append('CLOCK_SKEW_FUTURE_TIMESTAMP')
            if now - t.exchange_ts_ms > settings.data_stale_ms:
                reasons.append('STALE_TICK')
        if t.receive_ts_ms and now - t.receive_ts_ms > settings.data_stale_ms:
            reasons.append('STALE_RECEIVE')
        if t.bid > 0 and t.ask > 0 and t.ask < t.bid:
            reasons.append('CROSSED_BOOK')
        k = key(t.broker, t.instrument_token)
        old = self.last.get(k)
        if old and t.sequence is not None and old.sequence is not None and t.sequence <= old.sequence:
            reasons.append('OUT_OF_ORDER_SEQUENCE')
        if t.ltp <= 0:
            reasons.append('INVALID_LTP')
        if t.exchange_ts_ms:
            self.skew[t.broker.upper()].append((t.receive_ts_ms or now) - t.exchange_ts_ms)
        if reasons:
            self.reject_counts.update(reasons)
            self.rejected.append({'broker': t.broker, 'token': t.instrument_token, 'reasons': reasons, 'at': now})
            return {'accepted': False, 'status': 'DATA_UNAVAILABLE', 'reasons': reasons}
        self.prev[k] = old
        self.last[k] = t
        self.history[k].append((t.receive_ts_ms or now, t.ltp, t.oi))
        self.heartbeat[t.broker.upper()] = now
        self.accepted += 1
        return {'accepted': True, 'status': 'LIVE'}

    def tick(self, broker, token):
        return self.last.get(key(broker, token))

    def live(self, broker=None):
        now = now_ms()
        brokers = [broker.upper()] if broker else list(self.heartbeat)
        return any(now - self.heartbeat.get(b, 0) <= settings.data_stale_ms for b in brokers)

    def live_instrument(self, broker, token):
        t = self.tick(broker, token)
        return bool(t) and now_ms() - int(t.receive_ts_ms or 0) <= settings.data_stale_ms

    def live_spot(self, underlying, fresh_only=True):
        """Freshest tick of the underlying itself: an index or cash instrument carrying this
        underlying name with no expiry, strike or option type. Futures and options are
        excluded. fresh_only=False also returns a stale tick (good enough to centre a strike
        window, never to price). None when there is no such tick."""
        u = str(underlying or '').upper()
        if not u:
            return None
        best = None
        for t in self.last.values():
            if (str(t.underlying or '').upper() == u and t.expiry is None and t.strike is None and t.option_type is None
                    and t.ltp > 0 and (not fresh_only or self.live_instrument(t.broker, t.instrument_token))
                    and (best is None or (t.receive_ts_ms or 0) > (best.receive_ts_ms or 0))):
                best = t
        return best

    def price_lookup(self, broker, fresh_only=True):
        b = broker.upper()
        out = {}
        for k, t in self.last.items():
            if k.startswith(b + ':') and (not fresh_only or self.live_instrument(b, t.instrument_token)):
                out[t.instrument_token] = t.ltp
        return out

    def window_change(self, broker, token, window_ms):
        """(price_change_pct, oi_change) over the trailing window, or (None, None)."""
        h = self.history.get(key(broker, token))
        if not h or len(h) < 2:
            return None, None
        end_ts, end_px, end_oi = h[-1]
        start = next((x for x in h if end_ts - x[0] <= window_ms), h[0])
        if start is h[-1] or start[1] <= 0:
            return None, None
        return (end_px - start[1]) / start[1] * 100.0, end_oi - start[2]

    def flow(self, broker, token, window_ms):
        dp, doi = self.window_change(broker, token, window_ms)
        if dp is None:
            return {'classification': 'INSUFFICIENT_DATA', 'confidence': 0}
        if doi > 0 and dp > 0:
            label = 'POSSIBLE_LONG_BUILDUP'
        elif doi > 0 and dp < 0:
            label = 'POSSIBLE_WRITING'
        elif doi < 0 and dp > 0:
            label = 'POSSIBLE_SHORT_COVERING'
        elif doi < 0 and dp < 0:
            label = 'POSSIBLE_LONG_UNWINDING'
        else:
            label = 'MIXED'
        return {'classification': label, 'oi_delta': doi, 'premium_change_pct': dp, 'confidence': 0.3 if label == 'MIXED' else 0.6}

    def clock_skew_ms(self, broker):
        s = sorted(self.skew.get(broker.upper(), []))
        return s[len(s) // 2] if s else None

    def stats(self):
        return {'accepted': self.accepted, 'rejected_by_reason': dict(self.reject_counts), 'instruments': len(self.last),
                'live_brokers': [b for b in self.heartbeat if self.live(b)],
                'median_skew_ms': {b: self.clock_skew_ms(b) for b in self.skew}}
