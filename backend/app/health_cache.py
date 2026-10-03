"""Cached broker health. The original called every broker's auth endpoint (and a
full Kotak TOTP login) on each dashboard poll, readiness check and order."""
import asyncio
import time

from .config import settings


class HealthCache:
    def __init__(self, registry):
        self.registry = registry
        self.value = None
        self.at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, force=False):
        if not force and self.value is not None and time.time() - self.at < settings.broker_health_ttl_sec:
            return self.value
        async with self._lock:
            if not force and self.value is not None and time.time() - self.at < settings.broker_health_ttl_sec:
                return self.value
            self.value = await self.registry.health()
            self.at = time.time()
            return self.value

    async def status(self, broker):
        for h in await self.get():
            if h.get('broker') == str(broker).upper():
                return h.get('status')
        return 'UNKNOWN'
