import asyncio, json, time
from .config import settings

class EventBus:
    """Durable-first event bus. Redis Streams are authoritative in production;
    in-memory fan-out remains the local delivery mechanism for UI consumers."""
    def __init__(self,maxsize=10000):
        self.q=asyncio.Queue(maxsize=maxsize); self.subscribers=[]; self.redis=None; self.redis_ok=False
    async def connect(self):
        if not settings.require_durable_event_bus:
            return True
        import redis.asyncio as redis
        self.redis=redis.from_url(settings.redis_url,decode_responses=True)
        try:
            await self.redis.ping(); self.redis_ok=True; return True
        except Exception:
            if settings.live_trading: raise
            self.redis_ok=False; return False
    async def publish(self,event):
        if settings.require_durable_event_bus and not self.redis_ok:
            raise RuntimeError('DURABLE_EVENT_BUS_UNAVAILABLE')
        if self.redis_ok:
            await self.redis.xadd('iort:events', {'payload':json.dumps(event,default=str)}, maxlen=200000, approximate=True)
        if self.q.full(): raise RuntimeError('EVENT_BUS_OVERFLOW')
        await self.q.put(event)
        for fn in list(self.subscribers): await fn(event)
    async def hydrate_recent(self, count=1000):
        if not self.redis_ok:return 0
        rows=await self.redis.xrevrange('iort:events','+','-',count=count)
        for _,fields in reversed(rows):
            try:self.q.put_nowait(json.loads(fields['payload']))
            except Exception:continue
        return len(rows)
    def subscribe(self,fn): self.subscribers.append(fn); return fn
    async def next(self): return await self.q.get()
    async def close(self):
        if self.redis:
            await self.redis.aclose()
            self.redis=None; self.redis_ok=False
