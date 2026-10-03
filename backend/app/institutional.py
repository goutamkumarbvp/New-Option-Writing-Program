from dataclasses import dataclass, field
from enum import Enum
import time, uuid, hashlib

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

# Derivatives analytics live in benchmark.py; re-exported here for older callers.
# These institutional classes are library components. Only the analytics are
# exposed (as stateless calculators); FIX, multi-leg OMS, drop copy, allocation and
# DR control are NOT wired into the live order path.
from .benchmark import (AlgoOrderPlanner, AutoHedger, ExecutionTCA, HAReadiness, PortfolioGreeks,  # noqa: F401,E402
                        PreTradePortfolioRisk, ScenarioRiskEngine, SelfTradePrevention, VolPoint, VolSurfaceEngine)

__all__ = ['FixMsgType', 'FixEnvelope', 'FixGateway', 'ChildOrder', 'ParentOrder', 'MultiLegOMS', 'HierarchicalRiskBook',
           'DropCopyService', 'AllocationEngine', 'TCAEngine', 'SurveillanceEngine', 'ExceptionManager', 'LatencyMonitor',
           'DRController', 'ReleaseEvidence', 'AlgoOrderPlanner', 'AutoHedger', 'ExecutionTCA', 'HAReadiness', 'PortfolioGreeks',
           'PreTradePortfolioRisk', 'ScenarioRiskEngine', 'SelfTradePrevention', 'VolPoint', 'VolSurfaceEngine']
