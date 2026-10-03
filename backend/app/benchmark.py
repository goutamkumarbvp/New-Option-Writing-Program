from dataclasses import dataclass
from math import log
@dataclass(frozen=True)
class VolPoint: strike:float; iv:float; weight:float=1.0
class VolSurfaceEngine:
 def fit_smile(self,spot,points):
  p=[x for x in points if x.strike>0 and x.iv>0 and x.weight>0]
  if spot<=0 or len(p)<3:return {'ok':False,'reason':'INSUFFICIENT_VOL_POINTS'}
  # linear interpolation is deliberately observable and stable; retain observed smile points.
  return {'ok':True,'model':'observed_piecewise_linear','spot':spot,'points':sorted([{'strike':x.strike,'iv':x.iv} for x in p],key=lambda x:x['strike'])}
 def iv(self,surface,spot,strike):
  if not surface.get('ok'):return None
  p=surface['points'];
  if strike<=p[0]['strike']:return p[0]['iv']
  if strike>=p[-1]['strike']:return p[-1]['iv']
  for a,b in zip(p,p[1:]):
   if a['strike']<=strike<=b['strike']:
    w=(strike-a['strike'])/(b['strike']-a['strike']);return a['iv']+w*(b['iv']-a['iv'])
class ScenarioRiskEngine:
 def run(self,positions,spot_shocks=(-.05,0,.05),vol_shocks=(-.10,0,.10),days=1):
  if not positions:return {'ok':False,'reason':'NO_POSITIONS','scenarios':[]}
  out=[]
  for ds in spot_shocks:
   for dv in vol_shocks:
    pnl=0
    for p in positions:
     q=float(p.get('qty',0))*float(p.get('multiplier',1));dS=float(p.get('spot',0))*ds
     pnl+=q*(float(p.get('delta',0))*dS+.5*float(p.get('gamma',0))*dS*dS+float(p.get('vega',0))*dv+float(p.get('theta',0))*days)
    out.append({'spot_shock':ds,'vol_shock':dv,'pnl':pnl})
  return {'ok':True,'scenarios':out,'worst_pnl':min(x['pnl'] for x in out)}
class PortfolioGreeks:
 def aggregate(self,positions):
  return {k:sum(float(p.get('qty',0))*float(p.get('multiplier',1))*float(p.get(k,0)) for p in positions) for k in ('delta','gamma','vega','theta')}
class PreTradePortfolioRisk:
 def assess(self,order,portfolio,scenarios,limits):
  reasons=[];v=abs(float(order.get('qty',0)))*float(order.get('price') or portfolio.get('mark_price',0) or 0)
  if v>float(limits.get('max_order_value',float('inf'))):reasons.append('ORDER_VALUE_LIMIT')
  if float(scenarios.get('worst_pnl',0)) < -abs(float(limits.get('max_scenario_loss',float('inf')))):reasons.append('SCENARIO_LOSS_LIMIT')
  if v+float(portfolio.get('risk_reserve',0))>float(portfolio.get('buying_power',0)):reasons.append('BUYING_POWER_LIMIT')
  return {'approved':not reasons,'reasons':reasons,'projected_order_value':v}
class SelfTradePrevention:
 def check(self,o,working):
  for w in working:
   if w.get('symbol')==o.get('symbol') and w.get('broker')==o.get('broker') and str(w.get('side')).upper()!=str(o.get('side')).upper():
    op=float(o.get('price') or 0);wp=float(w.get('price') or 0);buy=op if str(o.get('side')).upper()=='BUY' else wp;sell=op if str(o.get('side')).upper()=='SELL' else wp
    if op>0 and wp>0 and buy>=sell:return {'approved':False,'reason':'SELF_TRADE_RISK'}
  return {'approved':True}
class AutoHedger:
 def delta_hedge(self,net_delta,hedge_delta,lot_size=1):
  if hedge_delta==0 or lot_size<=0:return {'ok':False,'reason':'INVALID_HEDGE_INSTRUMENT'}
  lots=int(round(-net_delta/(hedge_delta*lot_size)));return {'ok':True,'lots':abs(lots),'side':'BUY' if lots>0 else 'SELL','residual_delta':net_delta+lots*hedge_delta*lot_size}
class AlgoOrderPlanner:
 def iceberg(self,qty,disclosed):
  if qty<=0 or disclosed<=0:return {'ok':False}
  c=[]
  while qty>0:
   q=min(qty,disclosed);c.append(q);qty-=q
  return {'ok':True,'children':c}
 def twap(self,qty,slices,start_ms,end_ms):
  if qty<=0 or slices<=0 or end_ms<=start_ms:return {'ok':False}
  b,r=divmod(qty,slices);step=(end_ms-start_ms)/slices
  return {'ok':True,'children':[{'qty':b+(1 if i<r else 0),'due_ms':int(start_ms+i*step)} for i in range(slices) if b+(1 if i<r else 0)>0]}
class ExecutionTCA:
 def summarize(self,fills,arrival_price,side):
  q=sum(float(x.get('qty',0)) for x in fills)
  if not fills or arrival_price<=0 or q<=0:return {'ok':False}
  v=sum(float(x['qty'])*float(x['price']) for x in fills)/q;sgn=1 if side.upper()=='BUY' else -1
  return {'ok':True,'vwap':v,'implementation_shortfall_bps':sgn*(v-arrival_price)/arrival_price*10000}
class HAReadiness:
 def assess(self,c):
  req=('market_feed_primary','market_feed_secondary','risk_monitor_stream','risk_monitor_rest','database','broker_route');m=[x for x in req if not c.get(x)];return {'ready':not m,'missing':m}
