"""Live broker stream workers. All outputs are canonical Tick objects or explicit stream errors."""
import asyncio,time,json,struct
from .config import settings
from .models import Tick

def _now(): return int(time.time()*1000)
class StreamWorker:
 def __init__(self,broker,handler):self.broker=broker;self.handler=handler;self.stop=False
 async def run_forever(self):
  backoff=1
  while not self.stop:
   try: await self.run_once();backoff=1
   except asyncio.CancelledError:raise
   except Exception as e: await self.handler({'type':'STREAM_ERROR','broker':self.broker,'error':str(e)});await asyncio.sleep(backoff);backoff=min(30,backoff*2)
 async def run_once(self):raise NotImplementedError
class ZerodhaStreamWorker(StreamWorker):
 def __init__(self,handler,tokens):super().__init__('ZERODHA',handler);self.tokens=tokens
 async def run_once(self):
  if not settings.zerodha_api_key or not settings.zerodha_access_token:raise RuntimeError('ZERODHA_CREDENTIALS_MISSING')
  from kiteconnect import KiteTicker
  q=asyncio.Queue(maxsize=10000)
  def on_ticks(ws,ticks):
   for x in ticks:q.put_nowait(x)
  def on_connect(ws,response):ws.subscribe(self.tokens);ws.set_mode(ws.MODE_FULL,self.tokens)
  ticker=KiteTicker(settings.zerodha_api_key,settings.zerodha_access_token);ticker.on_ticks=on_ticks;ticker.on_connect=on_connect
  loop=asyncio.get_running_loop();await loop.run_in_executor(None,lambda:ticker.connect(threaded=True))
  while not self.stop:
   raw=await q.get();ts=raw.get('exchange_timestamp');ms=int(ts.timestamp()*1000) if ts else _now();depth=raw.get('depth') or {};buy=(depth.get('buy') or [{}])[0];sell=(depth.get('sell') or [{}])[0]
   await self.handler(Tick(broker='ZERODHA',exchange=raw.get('exchange',''),instrument_token=str(raw['instrument_token']),symbol=raw.get('tradingsymbol',''),ltp=float(raw.get('last_price',0)),bid=float(buy.get('price',0)),ask=float(sell.get('price',0)),volume=int(raw.get('volume_traded',0)),oi=int(raw.get('oi',0)),iv=(float(raw.get('iv')) if raw.get('iv') not in (None,'') else None),exchange_ts_ms=ms,receive_ts_ms=_now()))
  ticker.close()
class UpstoxStreamWorker(StreamWorker):
 def __init__(self,handler,keys):super().__init__('UPSTOX',handler);self.keys=keys
 async def run_once(self):
  if not settings.upstox_access_token:raise RuntimeError('UPSTOX_ACCESS_TOKEN_MISSING')
  # Use the broker's official Python SDK/protobuf implementation.  Do not hand-build
  # the V3 wire schema: Upstox can add fields while retaining compatibility.
  import upstox_client
  q=asyncio.Queue()
  configuration=upstox_client.Configuration();configuration.access_token=settings.upstox_access_token
  api_client=upstox_client.ApiClient(configuration)
  streamer=upstox_client.MarketDataStreamerV3(api_client,self.keys,'full')
  def on_open(_ws):
   pass
  def on_message(message):
   try:q.put_nowait(message)
   except asyncio.QueueFull:pass
  def on_error(_ws,error):
   try:q.put_nowait({'__stream_error__':str(error)})
   except asyncio.QueueFull:pass
  streamer.on('open',on_open);streamer.on('message',on_message);streamer.on('error',on_error)
  loop=asyncio.get_running_loop()
  await loop.run_in_executor(None,streamer.connect)
  try:
   while not self.stop:
    msg=await q.get()
    if isinstance(msg,dict) and '__stream_error__' in msg:raise RuntimeError(msg['__stream_error__'])
    feeds=msg.get('feeds',{}) if isinstance(msg,dict) else {}
    ts=int(msg.get('currentTs') or _now()) if isinstance(msg,dict) else _now()
    for key,feed in feeds.items():
     ff=(feed.get('fullFeed') or {}) if isinstance(feed,dict) else {}
     market=ff.get('marketFF') or ff.get('indexFF') or {}
     lt=market.get('ltpc') or {}
     level=market.get('marketLevel') or {};quotes=level.get('bidAskQuote') or []
     q0=quotes[0] if quotes else {}
     if not lt:continue
     await self.handler(Tick(broker='UPSTOX',exchange=key.split('|')[0] if '|' in key else '',instrument_token=key,symbol=key,ltp=float(lt.get('ltp',0) or 0),bid=float(q0.get('bidP',0) or 0),ask=float(q0.get('askP',0) or 0),volume=int(market.get('vtt',0) or 0),oi=int(market.get('oi',0) or 0),iv=(float(market.get('iv')) if market.get('iv') not in (None,'') else None),exchange_ts_ms=int(lt.get('ltt') or ts),receive_ts_ms=_now()))
  finally:
   try:streamer.disconnect()
   except Exception:pass

class AngelStreamWorker(StreamWorker):
 def __init__(self,handler,tokens):super().__init__('ANGEL',handler);self.tokens=tokens
 async def run_once(self):
  if not all([settings.angel_access_token,settings.angel_api_key,settings.angel_client_code,settings.angel_feed_token]):raise RuntimeError('ANGEL_STREAM_CREDENTIALS_MISSING')
  from SmartApi.smartWebSocketV2 import SmartWebSocketV2
  q=asyncio.Queue(); sws=SmartWebSocketV2(settings.angel_access_token,settings.angel_api_key,settings.angel_client_code,settings.angel_feed_token,max_retry_attempt=10,retry_strategy=1)
  def on_data(ws,msg):q.put_nowait(msg)
  sws.on_data=on_data;exmap={'NSE':1,'NSE_FO':2,'BSE':3,'BSE_FO':4,'MCX':5,'MCX_FO':5}
  grouped={}
  for x in self.tokens:
   if isinstance(x,dict):
    seg=str(x.get('segment','NSE_FO')).upper(); tok=str(x.get('token',''))
   else:
    raw=str(x); seg,tok=(raw.split('|',1) if '|' in raw else ('NSE_FO',raw))
   grouped.setdefault(exmap.get(seg,2),[]).append(tok)
  exchange_tokens=[{'exchangeType':k,'tokens':v} for k,v in grouped.items()]
  def on_open(ws):sws.subscribe('IORT',sws.SNAP_QUOTE,exchange_tokens)
  sws.on_open=on_open;loop=asyncio.get_running_loop();connect_future=loop.run_in_executor(None,sws.connect)
  while not self.stop:
   raw=await q.get();
   if not isinstance(raw,dict) or 'last_traded_price' not in raw:continue
   await self.handler(Tick(broker='ANGEL',exchange=str(raw.get('exchange_type') or raw.get('exchangeType') or 'NSE'),instrument_token=str(raw.get('token','')),symbol=str(raw.get('token','')),ltp=float(raw['last_traded_price'])/100,bid=float(raw.get('best_5_buy_data',[{}])[0].get('price',0) or 0)/100,ask=float(raw.get('best_5_sell_data',[{}])[0].get('price',0) or 0)/100,volume=int(raw.get('volume_trade_for_the_day',0) or 0),oi=int(raw.get('open_interest',0) or 0),iv=(float(raw.get('implied_volatility')) if raw.get('implied_volatility') not in (None,'') else None),sequence=int(raw.get('sequence_number',0) or 0),exchange_ts_ms=int(raw.get('exchange_timestamp',_now())),receive_ts_ms=_now()))
class KotakStreamWorker(StreamWorker):
 def __init__(self,handler,tokens):super().__init__('KOTAK',handler);self.tokens=tokens
 async def run_once(self):
  if not settings.kotak_api_key:raise RuntimeError('KOTAK_CREDENTIALS_MISSING')
  from neo_api_client import NeoAPI
  from neo_api_client.websocket.feed import WsToken,SFeedScrip
  c=NeoAPI(consumer_key=settings.kotak_api_key,environment='prod');await asyncio.to_thread(c.totp_login,mobile_number=settings.kotak_mobile,ucc=settings.kotak_client_code,totp=settings.kotak_totp);await asyncio.to_thread(c.totp_validate,mpin=settings.kotak_mpin)
  async with c.create_websocket() as ws:
   kt=[]
   for x in self.tokens:
    if isinstance(x,dict): seg=str(x.get('segment','nse_fo')).lower(); tok=str(x.get('token',''))
    else:
     raw=str(x); seg,tok=(raw.split('|',1) if '|' in raw else ('nse_fo',raw))
    if seg not in {'nse_cm','bse_cm','nse_fo','bse_fo','mcx_fo'}: raise RuntimeError('INVALID_KOTAK_SEGMENT')
    kt.append(WsToken(seg,tok))
   await ws.subscribe_scrips(kt)
   async for m in ws:
    if self.stop:return
    if isinstance(m,SFeedScrip):await self.handler(Tick(broker='KOTAK',exchange=m.exchange_segment,instrument_token=str(m.instrument_token),symbol=m.trading_symbol or str(m.instrument_token),ltp=float(m.last_traded_price),bid=float(getattr(m,'buy',{}).get('price',0) if isinstance(getattr(m,'buy',{}),dict) else 0),ask=float(getattr(m,'sell',{}).get('price',0) if isinstance(getattr(m,'sell',{}),dict) else 0),volume=int(getattr(m,'volume_traded',0) or 0),oi=int(getattr(m,'open_interest',0) or 0),exchange_ts_ms=_now(),receive_ts_ms=_now()))
