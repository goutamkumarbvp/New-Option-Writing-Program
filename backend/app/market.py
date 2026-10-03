import time
from .models import Tick
from .config import settings
class MarketDataGateway:
 def __init__(self):self.last={};self.prev={};self.heartbeat={};self.rejected=[]
 def ingest(self,t:Tick):
  now=int(time.time()*1000);r=[]
  if not t.exchange_ts_ms or now-t.exchange_ts_ms>settings.data_stale_ms:r.append('STALE_OR_MISSING_EXCHANGE_TIMESTAMP')
  if t.receive_ts_ms and now-t.receive_ts_ms>settings.data_stale_ms:r.append('STALE_TICK')
  if t.bid>0 and t.ask>0 and t.ask<t.bid:r.append('CROSSED_BOOK')
  old=self.last.get(t.instrument_token)
  if old and t.sequence is not None and old.sequence is not None and t.sequence<=old.sequence:r.append('OUT_OF_ORDER_SEQUENCE')
  if t.ltp<=0:r.append('INVALID_LTP')
  if r:self.rejected.append({'tick':t.model_dump(),'reasons':r});return {'accepted':False,'status':'DATA_UNAVAILABLE','reasons':r}
  self.prev[t.instrument_token]=old;self.last[t.instrument_token]=t;self.heartbeat[t.broker.upper()]=now;return {'accepted':True,'status':'LIVE'}
 def live(self,broker=None):
  if not self.last:return False
  now=int(time.time()*1000);ks=[broker.upper()] if broker else list(self.heartbeat)
  return any(now-self.heartbeat.get(k,0)<=settings.data_stale_ms for k in ks)
 def live_instrument(self,token):
  t=self.last.get(str(token))
  if not t:return False
  now=int(time.time()*1000)
  return now-int(t.receive_ts_ms or now)<=settings.data_stale_ms
 def flow(self,token):
  p,c=self.prev.get(token),self.last.get(token)
  if not p or not c:return {'classification':'INSUFFICIENT_DATA','confidence':0}
  doi=c.oi-p.oi;dp=c.ltp-p.ltp
  labels={(doi>0 and dp<0):'POSSIBLE_WRITING',(doi<0 and dp>0):'POSSIBLE_SHORT_COVERING',(doi>0 and dp>0):'POSSIBLE_LONG_BUILDUP',(doi<0 and dp<0):'POSSIBLE_LONG_UNWINDING'}
  label=next((v for k,v in labels.items() if k),'MIXED');return {'classification':label,'oi_delta':doi,'premium_delta':dp,'confidence':0.8 if label!='MIXED' else 0.3}
