import httpx,asyncio
from .config import settings
class BaseBroker:
 name='UNKNOWN'
 async def health(self):return {'broker':self.name,'status':'UNCONFIGURED'}
 async def positions(self):return []
 async def orders(self):return []
 async def margin(self):raise RuntimeError('MARGIN_DATA_UNAVAILABLE')
 async def place(self,o):return {'status':'BLOCKED','reason':'NOT_IMPLEMENTED'}
 async def cancel(self,broker_order_id): return {'status':'UNSUPPORTED'}
 async def cancel_all(self): return {'status':'UNSUPPORTED'}
class Zerodha(BaseBroker):
 name='ZERODHA';base='https://api.kite.trade'
 def headers(self):return {'X-Kite-Version':'3','Authorization':f'token {settings.zerodha_api_key}:{settings.zerodha_access_token}'}
 async def health(self):
  if not settings.zerodha_api_key or not settings.zerodha_access_token:return {'broker':self.name,'status':'UNCONFIGURED'}
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+'/user/profile',headers=self.headers());return {'broker':self.name,'status':'LIVE' if r.status_code==200 else 'AUTH_ERROR','http':r.status_code}
 async def positions(self):return await self._get('/portfolio/positions')
 async def orders(self):return await self._get('/orders')
 async def trades(self):return await self._get('/trades')
 async def margin(self):return await self._get('/user/margins')
 async def _get(self,p):
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+p,headers=self.headers());return r.json()
 async def cancel(self,broker_order_id):
  async with httpx.AsyncClient(timeout=10) as c:
   r=await c.delete(self.base+f'/orders/regular/{broker_order_id}',headers=self.headers())
   return {'status':'CANCELLED' if r.status_code<300 else 'CANCEL_REJECT','http':r.status_code,'response':r.json() if r.content else {}}
 async def cancel_all(self):
  out=[]
  for o in (await self.orders()).get('data',[]):
   st=str(o.get('status','')).upper()
   if st in {'OPEN','TRIGGER PENDING','OPEN PENDING'} and o.get('order_id'):
    out.append(await self.cancel(o['order_id']))
  return {'status':'CANCELLED_OPEN_ORDERS','results':out}
 async def place(self,o):
  if not settings.live_trading:return {'status':'BLOCKED','reason':'LIVE_TRADING_DISABLED'}
  d={'exchange':o['exchange'],'tradingsymbol':o['symbol'],'transaction_type':o['side'],'quantity':o['qty'],'product':o.get('product','NRML'),'order_type':o.get('order_type','MARKET'),'validity':'DAY','price':o.get('price') or 0,'trigger_price':o.get('trigger_price') or 0,'tag':o.get('tag','IORT')}
  async with httpx.AsyncClient(timeout=10) as c:r=await c.post(self.base+'/orders/regular',headers={**self.headers(),'Content-Type':'application/x-www-form-urlencoded'},data=d);resp=r.json() if r.content else {}; ok=r.status_code<300; return {'status':'SUBMITTED' if ok else 'BROKER_REJECT','broker':self.name,'http':r.status_code,'broker_order_id':(resp.get('data') or {}).get('order_id') if isinstance(resp,dict) else None,'response':resp}
class Upstox(BaseBroker):
 name='UPSTOX';base='https://api.upstox.com/v2'
 def headers(self):return {'Accept':'application/json','Authorization':f'Bearer {settings.upstox_access_token}'}
 async def health(self):
  if not settings.upstox_access_token:return {'broker':self.name,'status':'UNCONFIGURED'}
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+'/user/profile',headers=self.headers());return {'broker':self.name,'status':'LIVE' if r.status_code==200 else 'AUTH_ERROR','http':r.status_code}
 async def positions(self):
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+'/portfolio/short-term-positions',headers=self.headers());return r.json()
 async def orders(self):
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+'/order/retrieve-all',headers=self.headers());return r.json()
 async def trades(self):
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+'/order/trades/get-trades-for-day',headers=self.headers());return r.json()
 async def cancel(self,broker_order_id):
  async with httpx.AsyncClient(timeout=10) as c:
   r=await c.delete('https://api-hft.upstox.com/v2/order/cancel',headers=self.headers(),params={'order_id':broker_order_id})
   return {'status':'CANCELLED' if r.status_code<300 else 'CANCEL_REJECT','http':r.status_code,'response':r.json() if r.content else {}}
 async def cancel_all(self):
  out=[]
  for o in (await self.orders()).get('data',[]):
   if str(o.get('status','')).upper() in {'OPEN','TRIGGER PENDING','OPEN PENDING'} and o.get('order_id'):
    out.append(await self.cancel(o['order_id']))
  return {'status':'CANCELLED_OPEN_ORDERS','results':out}
 async def place(self,o):
  if not settings.live_trading:return {'status':'BLOCKED','reason':'LIVE_TRADING_DISABLED'}
  p={'quantity':o['qty'],'product':o.get('product','D'),'validity':'DAY','price':o.get('price') or 0,'tag':o.get('tag','IORT'),'instrument_token':o.get('instrument_token',''),'order_type':o.get('order_type','MARKET'),'transaction_type':o['side'],'disclosed_quantity':0,'trigger_price':o.get('trigger_price') or 0,'is_amo':False,'slice':bool(o.get('slice',False)),'market_protection':int(o.get('market_protection',-1))}
  async with httpx.AsyncClient(timeout=10) as c:r=await c.post('https://api-hft.upstox.com/v3/order/place',headers={**self.headers(),'Content-Type':'application/json'},json=p);resp=r.json() if r.content else {}; ok=r.status_code<300; return {'status':'SUBMITTED' if ok else 'BROKER_REJECT','broker':self.name,'http':r.status_code,'broker_order_id':(resp.get('data') or {}).get('order_id') if isinstance(resp,dict) else None,'response':resp}
class Angel(BaseBroker):
 name='ANGEL';base='https://apiconnect.angelone.in'
 def headers(self):return {'Authorization':f'Bearer {settings.angel_access_token}','Content-Type':'application/json','Accept':'application/json','X-PrivateKey':settings.angel_api_key,'X-UserType':'USER','X-SourceID':'WEB'}
 async def health(self):
  if not all([settings.angel_access_token,settings.angel_api_key,settings.angel_client_code]): return {'broker':self.name,'status':'UNCONFIGURED'}
  try:
   r=await self._get('/rest/secure/angelbroking/order/v1/getOrderBook')
   return {'broker':self.name,'status':'LIVE' if isinstance(r,dict) and r.get('status') is True else 'AUTH_ERROR'}
  except Exception as e:return {'broker':self.name,'status':'AUTH_ERROR','error':str(e)}
 async def positions(self):return await self._get('/rest/secure/angelbroking/order/v1/getPosition')
 async def orders(self):return await self._get('/rest/secure/angelbroking/order/v1/getOrderBook')
 async def trades(self):return await self._get('/rest/secure/angelbroking/order/v1/getTradeBook')
 async def margin(self):return await self._get('/rest/secure/angelbroking/user/v1/getRMS')
 async def _get(self,p):
  async with httpx.AsyncClient(timeout=10) as c:r=await c.get(self.base+p,headers=self.headers());return r.json()
 async def cancel(self,broker_order_id):
  async with httpx.AsyncClient(timeout=10) as c:
   r=await c.post(self.base+'/rest/secure/angelbroking/order/v1/cancelOrder',headers=self.headers(),json={'variety':'NORMAL','orderid':str(broker_order_id)})
   return {'status':'CANCELLED' if r.status_code<300 else 'CANCEL_REJECT','http':r.status_code,'response':r.json() if r.content else {}}
 async def cancel_all(self):
  out=[]
  for o in (await self.orders()).get('data',[]):
   if str(o.get('orderstatus',o.get('status',''))).upper() in {'OPEN','TRIGGER PENDING','OPEN PENDING'} and o.get('orderid'):
    out.append(await self.cancel(o['orderid']))
  return {'status':'CANCELLED_OPEN_ORDERS','results':out}
 async def place(self,o):
  if not settings.live_trading:return {'status':'BLOCKED','reason':'LIVE_TRADING_DISABLED'}
  p={'variety':'NORMAL','tradingsymbol':o['symbol'],'symboltoken':o.get('instrument_token',''),'transactiontype':o['side'],'exchange':o['exchange'],'ordertype':o.get('order_type','MARKET'),'producttype':o.get('product','INTRADAY'),'duration':'DAY','price':str(o.get('price') or 0),'triggerprice':str(o.get('trigger_price') or 0),'quantity':str(o['qty']),'squareoff':'0','stoploss':'0','ordertag':o.get('tag','IORT')}
  async with httpx.AsyncClient(timeout=10) as c:r=await c.post(self.base+'/rest/secure/angelbroking/order/v1/placeOrder',headers=self.headers(),json=p);resp=r.json() if r.content else {}; ok=r.status_code<300; return {'status':'SUBMITTED' if ok else 'BROKER_REJECT','broker':self.name,'http':r.status_code,'broker_order_id':(resp.get('data') or {}).get('order_id') if isinstance(resp,dict) else None,'response':resp}
class Kotak(BaseBroker):
 name='KOTAK'
 async def _client(self):
  from neo_api_client import NeoAPI
  if not settings.kotak_api_key:raise RuntimeError('KOTAK_CONSUMER_KEY_MISSING')
  c=NeoAPI(consumer_key=settings.kotak_api_key,environment='prod');await asyncio.to_thread(c.totp_login,mobile_number=settings.kotak_mobile,ucc=settings.kotak_client_code,totp=settings.kotak_totp);await asyncio.to_thread(c.totp_validate,mpin=settings.kotak_mpin);return c
 async def health(self):
  try:await self._client();return {'broker':self.name,'status':'LIVE','sdk':'kotakneoapi'}
  except Exception as e:return {'broker':self.name,'status':'AUTH_ERROR','error':str(e)}
 async def positions(self):return await asyncio.to_thread((await self._client()).positions)
 async def orders(self):return await asyncio.to_thread((await self._client()).order_report)
 async def trades(self):return await asyncio.to_thread((await self._client()).trade_report)
 async def margin(self):
  c=await self._client(); fn=getattr(c,'limits',None)
  if not fn: raise RuntimeError('MARGIN_DATA_UNAVAILABLE')
  return await asyncio.to_thread(fn)
 async def cancel(self,broker_order_id):
  c=await self._client()
  resp=await asyncio.to_thread(c.cancel_order,order_id=str(broker_order_id))
  return {'status':'CANCELLED' if isinstance(resp,dict) and not resp.get('error') else 'CANCEL_REJECT','response':resp}
 async def cancel_all(self):
  out=[]
  rows=(await self.orders()).get('data',[]) if isinstance(await self.orders(),dict) else []
  for o in rows:
   if str(o.get('ordSt','')).upper() in {'OPEN','TRIGGER PENDING','OPEN PENDING'} and o.get('nOrdNo'):
    out.append(await self.cancel(o['nOrdNo']))
  return {'status':'CANCELLED_OPEN_ORDERS','results':out}
 async def place(self,o):
  if not settings.live_trading:return {'status':'BLOCKED','reason':'LIVE_TRADING_DISABLED'}
  c=await self._client();side='B' if o['side']=='BUY' else 'S'
  seg={'NSE':'nse_fo','BSE':'bse_fo','MCX':'mcx_fo'}.get(str(o['exchange']).upper(),str(o['exchange']).lower())
  if seg not in {'nse_cm','bse_cm','nse_fo','bse_fo','mcx_fo'}: return {'status':'BROKER_REJECT','broker':self.name,'reason':'INVALID_EXCHANGE_SEGMENT'}
  resp=await asyncio.to_thread(c.place_order,exchange_segment=seg,product=o.get('product','NRML'),price=str(o.get('price') or 0),order_type='MKT' if o.get('order_type','MARKET')=='MARKET' else 'L',quantity=str(o['qty']),validity='DAY',trading_symbol=o['symbol'],transaction_type=side)
  bid=(resp.get('nOrdNo') or resp.get('order_id') or resp.get('orderId')) if isinstance(resp,dict) else None
  return {'status':'SUBMITTED' if bid else 'BROKER_REJECT','broker':self.name,'broker_order_id':bid,'response':resp}
# Kotak's current SDK exposes trade_report/order_report through the authenticated client.

class BrokerRegistry:
 def __init__(self):self.items={'ZERODHA':Zerodha(),'UPSTOX':Upstox(),'ANGEL':Angel(),'KOTAK':Kotak()}
 def get(self,n):return self.items[n.upper()]
 async def health(self):return [await b.health() for b in self.items.values()]
