import asyncio,json
from .broker_streams import ZerodhaStreamWorker,UpstoxStreamWorker,AngelStreamWorker,KotakStreamWorker
from .config import settings
class StreamManager:
    def __init__(self,on_tick,on_event,subscriptions=None):
        self.on_tick=on_tick; self.on_event=on_event; self.tasks=[]; self.workers=[]
        try:self.subscriptions= json.loads(settings.subscription_json or '{}') if subscriptions is None else subscriptions
        except Exception:self.subscriptions={}
    def build(self):
        self.workers=[]
        if settings.zerodha_api_key and settings.zerodha_access_token:self.workers.append(ZerodhaStreamWorker(self.on_tick,self.subscriptions.get('ZERODHA',[])))
        if settings.upstox_access_token:self.workers.append(UpstoxStreamWorker(self.on_tick,self.subscriptions.get('UPSTOX',[])))
        if settings.angel_access_token and settings.angel_api_key:self.workers.append(AngelStreamWorker(self.on_tick,self.subscriptions.get('ANGEL',[])))
        if settings.kotak_api_key:self.workers.append(KotakStreamWorker(self.on_tick,self.subscriptions.get('KOTAK',[])))
    async def start(self):
        self.build();self.tasks=[asyncio.create_task(w.run_forever(),name=f'feed-{w.broker}') for w in self.workers]
        await self.on_event({'type':'STREAM_MANAGER_STARTED','brokers':[w.broker for w in self.workers]})
    async def stop(self):
        for w in self.workers:w.stop=True
        for t in self.tasks:t.cancel()
        if self.tasks:await asyncio.gather(*self.tasks,return_exceptions=True)
        await self.on_event({'type':'STREAM_MANAGER_STOPPED'})
