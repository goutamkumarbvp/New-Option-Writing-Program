import asyncio
import json
import time

from .config import settings


class EventBus:
    """Redis Streams are the durable copy in production; the in-memory queue feeds the local
    consumers. A missing or lost Redis never stops the terminal: events keep flowing in memory,
    `redis_ok` turns false (live readiness then blocks new orders with DURABLE_EVENT_BUS_NOT_READY)
    and `maintain()` reconnects every few seconds until Redis is back."""
    RECONNECT_SEC = 5.0

    def __init__(self, maxsize=10000):
        self.q = asyncio.Queue(maxsize=maxsize)
        self.subscribers = []
        self.redis = None
        self.redis_ok = False
        self.overflow = 0
        self.last_error = None
        self.reconnects = 0
        self._last_try = 0.0

    async def connect(self):
        if not settings.require_durable_event_bus:
            return True
        try:
            if self.redis is None:
                import redis.asyncio as redis
                # Short timeouts: a stalled Redis must never freeze the terminal (it is retried by maintain()).
                self.redis = redis.from_url(settings.redis_url, decode_responses=True, socket_connect_timeout=2,
                                            socket_timeout=2, health_check_interval=15)
            await asyncio.wait_for(self.redis.ping(), 3)
        except Exception as exc:  # noqa: BLE001 - reported and retried by maintain()
            self.redis_ok, self.last_error = False, f'{type(exc).__name__}: {str(exc)[:160]}'
            return False
        if not self.redis_ok and self._last_try:
            self.reconnects += 1
        self.redis_ok, self.last_error = True, None
        return True

    async def maintain(self):
        """Background loop: reconnect a lost Redis."""
        while True:
            if settings.require_durable_event_bus and not self.redis_ok and time.monotonic() - self._last_try >= self.RECONNECT_SEC:
                self._last_try = time.monotonic()
                await self.connect()
            await asyncio.sleep(1.0)

    DURABLE_SKIP = {'MARKET_TICK', 'HEARTBEAT'}  # high-volume telemetry: never makes a tick wait on Redis

    async def publish(self, event):
        if self.redis_ok and event.get('type') not in self.DURABLE_SKIP:
            try:
                await asyncio.wait_for(self.redis.xadd('iort:events', {'payload': json.dumps(event, default=str)},
                                                       maxlen=200000, approximate=True), 2)
            except Exception as exc:  # noqa: BLE001 - Redis lost: keep the local copy, reconnect in maintain()
                self.redis_ok, self.last_error = False, f'{type(exc).__name__}: {str(exc)[:160]}'
                self._last_try = time.monotonic()
        if self.q.full():
            self.overflow += 1
            raise RuntimeError('EVENT_BUS_OVERFLOW')
        self.q.put_nowait(event)
        for fn in list(self.subscribers):
            await fn(event)
        if settings.live_trading and settings.require_durable_event_bus and not self.redis_ok:
            raise RuntimeError('DURABLE_EVENT_BUS_UNAVAILABLE')

    def status(self):
        return {'required': settings.require_durable_event_bus, 'connected': self.redis_ok, 'last_error': self.last_error,
                'reconnects': self.reconnects, 'queue_depth': self.q.qsize(), 'overflow': self.overflow}

    def subscribe(self, fn):
        self.subscribers.append(fn)
        return fn

    async def next(self):
        return await self.q.get()

    async def close(self):
        if self.redis:
            await self.redis.aclose()
            self.redis = None
            self.redis_ok = False
