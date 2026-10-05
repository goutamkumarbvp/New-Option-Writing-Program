import asyncio
import json

from .broker_streams import AngelStreamWorker, KotakStreamWorker, UpstoxStreamWorker, ZerodhaStreamWorker
from .chain_subscriber import auto_chain_config
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
        # Kotak also runs with only KOTAK_AUTO_CHAIN_JSON: the chain subscriber feeds it tokens at runtime.
        if self.registry and 'KOTAK' in self.registry.items and self.registry.get('KOTAK').configured() and (s.get('KOTAK') or auto_chain_config()):
            k = self.registry.get('KOTAK')
            self.workers.append(KotakStreamWorker(self.on_tick, s.get('KOTAK') or [], k.session, self.on_event,
                                                  session_peek=getattr(k, 'peek_session', None)))

    def get(self, broker):
        key = str(broker).upper()
        return next((w for w in self.workers if w.broker == key), None)

    def request_reconnect(self, broker, relogin=False, reason='MANUAL_RECONNECT'):
        """Reconnect one broker's feed now. None when that broker has no stream worker."""
        w = self.get(broker)
        return w.request_reconnect(relogin=relogin, reason=reason) if w else None

    def set_auto_reconnect(self, broker, enabled):
        w = self.get(broker)
        return w.set_auto_reconnect(enabled) if w else None

    async def start(self):
        self.build()
        self.tasks = [asyncio.create_task(self._keep(w), name=f'feed-{w.broker}') for w in self.workers]
        await self.on_event({'type': 'STREAM_MANAGER_STARTED', 'brokers': [w.broker for w in self.workers],
                             'config_error': self.config_error})

    async def _keep(self, w):
        """Restart a worker whose loop crashed on an unexpected error (run_forever handles every
        connection failure itself; this catches bugs), so a feed is never silently left dead."""
        delay = 1.0
        while not w.stop:
            try:
                await w.run_forever()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                w.last_error = f'WORKER_CRASHED:{type(exc).__name__}:{str(exc)[:120]}'
                try:
                    await self.on_event({'type': 'STREAM_WORKER_RESTART', 'broker': w.broker, 'error': w.last_error})
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    async def stop(self):
        for w in self.workers:
            w.stop = True
            w.wake()
        for t in self.tasks:
            t.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.on_event({'type': 'STREAM_MANAGER_STOPPED'})

    def status(self):
        return [w.status() for w in self.workers]
