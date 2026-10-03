from dataclasses import dataclass, field
from enum import Enum
import time, uuid, hashlib, json

class FixMsgType(str, Enum):
    NEW='D'; CANCEL='F'; REPLACE='G'; EXEC_REPORT='8'; HEARTBEAT='0'

@dataclass
class FixEnvelope:
    msg_type:str; sender:str; target:str; seq:int; fields:dict; ts_ns:int=field(default_factory=time.time_ns)
    def canonical(self):
        body={'35':self.msg_type,'49':self.sender,'56':self.target,'34':self.seq,**self.fields}
        return '|'.join(f'{k}={body[k]}' for k in sorted(body))
    def checksum(self): return hashlib.sha256(self.canonical().encode()).hexdigest()

class FixGateway:
    """Protocol boundary with sequence/idempotency checks. Network session adapter is injected."""
    def __init__(self, sender='IORT', target='BROKER', transport=None):
        self.sender=sender; self.target=target; self.transport=transport; self.out_seq=0; self.in_seq=0; self.seen_exec=set()
    async def send_order(self, order):
        self.out_seq+=1
        clid=order.get('client_order_id') or str(uuid.uuid4())
        env=FixEnvelope(FixMsgType.NEW.value,self.sender,self.target,self.out_seq,{'11':clid,'55':order['symbol'],'54':'1' if order['side'].upper()=='BUY' else '2','38':order['qty'],'40':order.get('order_type','2'),'44':order.get('price','')})
        if not self.transport: return {'status':'FIX_TRANSPORT_UNAVAILABLE','client_order_id':clid,'fix':env.canonical()}
        return await self.transport.send(env)
    def accept_execution_report(self, report):
        seq=int(report['34']); exec_id=str(report.get('17',''))
        if seq<=self.in_seq: return {'accepted':False,'reason':'FIX_SEQUENCE_REPLAY'}
        if exec_id and exec_id in self.seen_exec: return {'accepted':False,'reason':'DUPLICATE_EXEC_REPORT'}
        self.in_seq=seq
        if exec_id:self.seen_exec.add(exec_id)
        return {'accepted':True}

@dataclass
class ChildOrder:
    child_id:str; parent_id:str; leg_id:str; broker:str; symbol:str; side:str; qty:int; price:float|None=None; status:str='NEW'
@dataclass
class ParentOrder:
    parent_id:str; strategy:str; legs:list[dict]; status:str='STAGED'; children:list[ChildOrder]=field(default_factory=list)

class MultiLegOMS:
    """Parent/child lifecycle for option spreads; never treats leg submission as strategy fill."""
    def stage(self, strategy, legs): return ParentOrder(str(uuid.uuid4()),strategy,legs)
    def release(self, parent, broker):
        if parent.status not in {'STAGED','PAUSED'}: raise ValueError('PARENT_NOT_RELEASABLE')
        parent.children=[ChildOrder(str(uuid.uuid4()),parent.parent_id,str(i),broker,l['symbol'],l['side'],int(l['qty']),l.get('price')) for i,l in enumerate(parent.legs)]
        parent.status='RELEASED'; return parent.children
    def aggregate(self,parent):
        states={c.status for c in parent.children}
        if states and states=={'FILLED'}: parent.status='FILLED'
        elif 'REJECTED' in states: parent.status='EXCEPTION'
        elif 'PARTIAL' in states or ('FILLED' in states and len(states)>1): parent.status='PARTIAL'
        return parent.status

class HierarchicalRiskBook:
    def __init__(self, limits=None): self.limits=limits or {}; self.usage={}
    def set_limit(self, level,key,limit): self.limits[(level,key)]=float(limit)
    def reserve(self, path, amount):
        failures=[]
        for level,key in path:
            lim=self.limits.get((level,key))
            used=self.usage.get((level,key),0.0)
            if lim is not None and used+amount>lim: failures.append(f'{level}:{key}:LIMIT')
        if failures:return {'approved':False,'reasons':failures}
        for item in path:self.usage[item]=self.usage.get(item,0.0)+amount
        return {'approved':True,'reasons':[]}

class DropCopyService:
    def __init__(self, ledger): self.ledger=ledger; self.seen=set()
    def ingest(self, execution):
        eid=str(execution.get('exec_id') or execution.get('trade_id') or '')
        if not eid:return {'accepted':False,'reason':'EXEC_ID_REQUIRED'}
        if eid in self.seen:return {'accepted':False,'reason':'DUPLICATE'}
        self.seen.add(eid); self.ledger.event('DROP_COPY',execution); return {'accepted':True}

class AllocationEngine:
    def pro_rata(self, fill_qty, accounts):
        total=sum(int(a['requested_qty']) for a in accounts)
        if total<=0: raise ValueError('NO_ALLOCATION_WEIGHT')
        out=[]; assigned=0
        for i,a in enumerate(accounts):
            q=fill_qty-assigned if i==len(accounts)-1 else int(fill_qty*int(a['requested_qty'])/total)
            assigned+=q; out.append({'account':a['account'],'qty':q})
        return out

class TCAEngine:
    def analyze(self, side, qty, arrival, avg_fill, vwap=None, decision=None):
        sign=1 if side.upper()=='BUY' else -1
        slippage_bps=sign*(avg_fill-arrival)/arrival*10000 if arrival else 0
        vwap_bps=sign*(avg_fill-vwap)/vwap*10000 if vwap else None
        decision_bps=sign*(avg_fill-decision)/decision*10000 if decision else None
        return {'qty':qty,'arrival_slippage_bps':slippage_bps,'vwap_slippage_bps':vwap_bps,'decision_slippage_bps':decision_bps}

class SurveillanceEngine:
    """Participant-side controls; alerts only, never fabricates regulatory conclusions."""
    def inspect(self, events):
        alerts=[]
        cancels=sum(1 for e in events if e.get('type')=='CANCEL'); news=sum(1 for e in events if e.get('type')=='NEW')
        if news>=10 and cancels/news>0.8: alerts.append({'scenario':'HIGH_CANCEL_RATIO','severity':'MEDIUM'})
        for e in events:
            if e.get('self_trade'): alerts.append({'scenario':'POTENTIAL_SELF_TRADE','severity':'HIGH'})
        return alerts

class ExceptionManager:
    def __init__(self): self.items={}
    def open(self, kind, payload):
        i=str(uuid.uuid4()); self.items[i]={'id':i,'kind':kind,'payload':payload,'status':'OPEN','created_ns':time.time_ns()}; return self.items[i]
    def resolve(self, item_id, action): self.items[item_id]['status']='RESOLVED';self.items[item_id]['action']=action;return self.items[item_id]

class LatencyMonitor:
    def __init__(self): self.samples=[]
    def observe(self, stage, start_ns, end_ns=None): self.samples.append((stage,(end_ns or time.time_ns())-start_ns))
    def snapshot(self):
        out={}
        for stage in {s for s,_ in self.samples}:
            xs=sorted(v for s,v in self.samples if s==stage); out[stage]={'count':len(xs),'p50_us':xs[len(xs)//2]/1000,'p99_us':xs[min(len(xs)-1,int(len(xs)*.99))]/1000}
        return out

class DRController:
    def __init__(self): self.primary_healthy=True; self.secondary_ready=False; self.active='PRIMARY'
    def update(self, primary_healthy, secondary_ready): self.primary_healthy=primary_healthy;self.secondary_ready=secondary_ready
    def failover(self):
        if self.primary_healthy:return {'changed':False,'active':self.active}
        if not self.secondary_ready:return {'changed':False,'active':self.active,'reason':'SECONDARY_NOT_READY'}
        self.active='SECONDARY';return {'changed':True,'active':self.active}

class ReleaseEvidence:
    """Evidence-based release gate. No evidence => no institutional-equivalence claim."""
    REQUIRED={'unit_tests','integration_tests','broker_contract_tests','load_test','soak_test','failover_test','recovery_test','security_scan','dependency_scan','latency_report','reconciliation_test','backup_restore_test'}
    def certify(self,evidence):
        missing=sorted(self.REQUIRED-set(k for k,v in evidence.items() if v is True))
        return {'certified':not missing,'missing':missing,'scope':'INTERNAL_ENGINEERING_RELEASE'}

@dataclass
class VolPoint:
    strike:float; iv:float; weight:float=1.0
class VolSurfaceEngine:
    def fit_smile(self,spot,points):
        if spot<=0 or len(points)<3:return {'status':'INSUFFICIENT_DATA'}
        pts=sorted(points,key=lambda p:p.strike); atm=min(pts,key=lambda p:abs(p.strike-spot))
        left=[p.iv for p in pts if p.strike<spot];right=[p.iv for p in pts if p.strike>spot]
        return {'status':'OK','atm_iv':atm.iv,'put_skew':(sum(left)/len(left)-atm.iv) if left else None,'call_skew':(sum(right)/len(right)-atm.iv) if right else None,'points':len(pts)}
class PortfolioGreeks:
    def aggregate(self,positions):
        keys=('delta','gamma','theta','vega');out={k:0.0 for k in keys}
        for p in positions:
            mult=float(p.get('qty',0))*float(p.get('lot_size',1))
            for k in keys:out[k]+=float(p.get(k,0))*mult
        return out
class ScenarioRiskEngine:
    def run(self,positions,spot_shocks=(-.05,0,.05),vol_shocks=(-.10,0,.10),days=1):
        rows=[]; worst=0.0
        for ss in spot_shocks:
            for vs in vol_shocks:
                pnl=0.0
                for p in positions:
                    q=float(p.get('qty',0))*float(p.get('lot_size',1)); spot=float(p.get('spot',0));
                    pnl+=q*(float(p.get('delta',0))*spot*ss + .5*float(p.get('gamma',0))*(spot*ss)**2 + float(p.get('vega',0))*vs + float(p.get('theta',0))*days)
                rows.append({'spot_shock':ss,'vol_shock':vs,'pnl':pnl});worst=min(worst,pnl)
        return {'worst_pnl':worst,'scenarios':rows}
class PreTradePortfolioRisk:
    def assess(self,order,portfolio,scenarios,limits):
        reasons=[]
        if float(scenarios.get('worst_pnl',0)) < -float(limits.get('max_scenario_loss',1e18)):reasons.append('SCENARIO_LOSS_LIMIT')
        if abs(float(portfolio.get('net_delta',0))+float(order.get('delta_impact',0)))>float(limits.get('max_abs_delta',1e18)):reasons.append('DELTA_LIMIT')
        if float(portfolio.get('vega',0))+float(order.get('vega_impact',0))>float(limits.get('max_vega',1e18)):reasons.append('VEGA_LIMIT')
        return {'approved':not reasons,'reasons':reasons}
class SelfTradePrevention:
    def check(self,order,working_orders):
        for w in working_orders:
            if w.get('symbol')==order.get('symbol') and w.get('side')!=order.get('side') and w.get('account')==order.get('account'):
                return {'approved':False,'reason':'SELF_TRADE_RISK','conflicting_order':w.get('client_order_id')}
        return {'approved':True}
class AutoHedger:
    def delta_hedge(self,net_delta,hedge_delta,lot_size=1):
        if not hedge_delta or lot_size<=0:return {'status':'NO_VALID_HEDGE'}
        lots=round(-net_delta/(hedge_delta*lot_size));return {'status':'PROPOSED','lots':lots,'qty':lots*lot_size,'requires_risk_approval':True}
class AlgoOrderPlanner:
    def iceberg(self,qty,disclosed):
        if qty<=0 or disclosed<=0: return {'status':'INVALID'}
        chunks=[];remaining=qty
        while remaining: q=min(disclosed,remaining);chunks.append(q);remaining-=q
        return {'status':'PLANNED','chunks':chunks}
    def twap(self,qty,slices,start_ms,end_ms):
        if qty<=0 or slices<=0 or end_ms<=start_ms:return {'status':'INVALID'}
        base=qty//slices;rem=qty%slices;step=(end_ms-start_ms)/slices
        return {'status':'PLANNED','orders':[{'qty':base+(1 if i<rem else 0),'due_ms':int(start_ms+i*step)} for i in range(slices) if base+(1 if i<rem else 0)>0]}
class ExecutionTCA:
    def summarize(self,fills,arrival_price,side):
        qty=sum(float(f.get('qty',0)) for f in fills)
        if qty<=0 or arrival_price<=0:return {'status':'INSUFFICIENT_DATA'}
        avg=sum(float(f['qty'])*float(f['price']) for f in fills)/qty
        return {'status':'OK','qty':qty,'avg_fill':avg,**TCAEngine().analyze(side,qty,arrival_price,avg)}
class HAReadiness:
    REQUIRED=('primary_feed','secondary_feed','primary_broker','secondary_broker','db_healthy','clock_synced','secondary_ready')
    def assess(self,state):
        missing=[k for k in self.REQUIRED if not state.get(k,False)];return {'ready':not missing,'missing':missing}
# v2.2 derivatives benchmark capabilities
try:
 from .benchmark import VolSurfaceEngine,VolPoint,ScenarioRiskEngine,PortfolioGreeks,PreTradePortfolioRisk,SelfTradePrevention,AutoHedger,AlgoOrderPlanner,ExecutionTCA,HAReadiness
except ImportError:
 from benchmark import VolSurfaceEngine,VolPoint,ScenarioRiskEngine,PortfolioGreeks,PreTradePortfolioRisk,SelfTradePrevention,AutoHedger,AlgoOrderPlanner,ExecutionTCA,HAReadiness
