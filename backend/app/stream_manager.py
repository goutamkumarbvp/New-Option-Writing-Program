import asyncio
import json

from .broker_streams import AngelStreamWorker, KotakStreamWorker, UpstoxStreamWorker, ZerodhaStreamWorker
from .config import settings


class StreamManager:
    def __init__(self, on_tick, on_event, subscriptions=None, registry=None):
        self.on_tick = on_tick
        self.on_event = on_event
        self.registry = registry
        self.tasks = []
        self.workers = []
        self.config_error = None
        if subscriptions is not None:
            self.subscriptions = subscriptions
        else:
            try:
                self.subscriptions = json.loads(settings.subscription_json or '{}')
            except ValueError as exc:
                self.subscriptions, self.config_error = {}, f'SUBSCRIPTION_JSON_INVALID:{exc}'

    def build(self):
        s = self.subscriptions
        self.workers = []
        if settings.zerodha_api_key and settings.zerodha_access_token and s.get('ZERODHA'):
            self.workers.append(ZerodhaStreamWorker(self.on_tick, s['ZERODHA'], self.on_event))
        if settings.upstox_access_token and s.get('UPSTOX'):
            self.workers.append(UpstoxStreamWorker(self.on_tick, s['UPSTOX'], self.on_event))
        if settings.angel_access_token and settings.angel_api_key and s.get('ANGEL'):
            self.workers.append(AngelStreamWorker(self.on_tick, s['ANGEL'], self.on_event))
        if self.registry and 'KOTAK' in self.registry.items and self.registry.get('KOTAK').configured() and s.get('KOTAK'):
            self.workers.append(KotakStreamWorker(self.on_tick, s['KOTAK'], self.registry.get('KOTAK').session, self.on_event))

    async def start(self):
        self.build()
        self.tasks = [asyncio.create_task(w.run_forever(), name=f'feed-{w.broker}') for w in self.workers]
        await self.on_event({'type': 'STREAM_MANAGER_STARTED', 'brokers': [w.broker for w in self.workers],
                             'config_error': self.config_error})

    async def stop(self):
        for w in self.workers:
            w.stop = True
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.on_event({'type': 'STREAM_MANAGER_STOPPED'})

    def status(self):
        return [{'broker': w.broker, 'running': w.running, 'healthy': w.healthy, 'last_error': w.last_error,
                 'reconnects': w.reconnects, 'dropped': w.bridge.dropped if w.bridge else 0} for w in self.workers]
