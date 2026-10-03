import asyncio
import json

from .config import settings


class EventBus:
    """Redis Streams are the durable copy in production; the in-memory queue feeds
    the local audit consumer. In paper mode a missing Redis degrades to in-memory
    only (reported on the dashboard); in live mode it is a startup error."""

    def __init__(self, maxsize=10000):
        self.q = asyncio.Queue(maxsize=maxsize)
        self.subscribers = []
        self.redis = None
        self.redis_ok = False
        self.overflow = 0

    async def connect(self):
        if not settings.require_durable_event_bus:
            return True
        import redis.asyncio as redis
        self.redis = redis.from_url(settings.redis_url, decode_responses=True)
        try:
            await self.redis.ping()
            self.redis_ok = True
            return True
        except Exception:
            if settings.live_trading:
                raise
            self.redis_ok = False
            return False

    async def publish(self, event):
        if settings.live_trading and settings.require_durable_event_bus and not self.redis_ok:
            raise RuntimeError('DURABLE_EVENT_BUS_UNAVAILABLE')
        if self.redis_ok:
            await self.redis.xadd('iort:events', {'payload': json.dumps(event, default=str)}, maxlen=200000, approximate=True)
        if self.q.full():
            self.overflow += 1
            raise RuntimeError('EVENT_BUS_OVERFLOW')
        self.q.put_nowait(event)
        for fn in list(self.subscribers):
            await fn(event)

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
