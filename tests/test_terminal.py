import sys,os,time,asyncio
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','backend'))
os.environ['DATABASE_URL']='sqlite:///:memory:'
from app.ledger import Ledger
from app.market import MarketDataGateway
from app.models import Tick,OrderRequest
from app.risk import ProductionDualRiskEngine
from app.engine import DecisionPipeline,EngineContext
from app.execution import SmartOrderRouter
class FakeBroker:
 name='TEST'
 async def place(self,o):return {'status':'SUBMITTED','broker':'TEST','broker_order_id':'B1'}
 async def orders(self):return [{'order_id':'B1','status':'OPEN','filled_quantity':0}]
 async def trades(self):return [{'order_id':'B1','quantity':1,'price':100}]
 async def positions(self):return []
async def aroute():
 class R: pass
 r=R();r.items={'TEST':FakeBroker()};r.get=lambda n:r.items[n]
 out=await SmartOrderRouter(r).route({'broker':'TEST','qty':1});assert out['status']=='SUBMITTED'
def tick(token='1',seq=1,age=0):
 now=int(time.time()*1000);return Tick(broker='TEST',exchange='NSE',instrument_token=token,symbol='NIFTY',ltp=100,bid=99,ask=101,oi=1000,sequence=seq,exchange_ts_ms=now-age,receive_ts_ms=now)
def test_live_tick():g=MarketDataGateway();assert g.ingest(tick())['accepted'] and g.live('TEST')
def test_stale_rejected():g=MarketDataGateway();assert not g.ingest(tick(age=5000))['accepted']
def test_out_of_order_rejected():g=MarketDataGateway();g.ingest(tick(seq=2));assert not g.ingest(tick(seq=1))['accepted']
def test_ledger_persists():l=Ledger();l.upsert_order({'client_order_id':'c1','broker':'TEST','status':'SUBMITTED','qty':1});l.record_fill({'client_order_id':'c1','broker_order_id':'b1','qty':1,'price':100});l.upsert_position({'broker':'TEST','exchange':'NSE','symbol':'NIFTY','qty':1,'avg_price':100,'pnl':0});assert l.snapshot()=={'orders':1,'fills':1,'positions':1,'events':0,'reconciliations':0,'audit_records':0}
def test_risk_fail_closed():p=type('P',(),{'order':OrderRequest(broker='TEST',exchange='NSE',symbol='NIFTY',side='BUY',qty=1)})();ctx=EngineContext(market={'live':False},portfolio={'broker_reconciled':True},mode='MANUAL');assert not ProductionDualRiskEngine().evaluate_context(ctx,p)['approved']
def test_pipeline_data_unavailable():o=OrderRequest(broker='TEST',exchange='NSE',symbol='NIFTY',side='BUY',qty=1);ctx=EngineContext(market={'live':False,'order':o},portfolio={'broker_reconciled':True},mode='MANUAL');assert not DecisionPipeline().evaluate(ctx)['approved']
def test_pipeline_rejects_unreconciled():o=OrderRequest(broker='TEST',exchange='NSE',symbol='NIFTY',side='BUY',qty=1);ctx=EngineContext(market={'live':True,'order':o,'liquid':True},portfolio={'broker_reconciled':False},mode='MANUAL');assert not DecisionPipeline().evaluate(ctx)['approved']
def test_auto_requires_approval():o=OrderRequest(broker='TEST',exchange='NSE',symbol='NIFTY',side='BUY',qty=1,mode='AUTO');ctx=EngineContext(market={'live':True,'order':o,'liquid':True,'price_change_pct':.8,'flow':{'classification':'POSSIBLE_WRITING'}},portfolio={'broker_reconciled':True},mode='AUTO');assert not DecisionPipeline().evaluate(ctx)['approved']
def test_kill_switch():from app.watchdog import KillSwitch;k=KillSwitch();k.trigger('TEST');assert k.triggered
def test_router_registry():asyncio.run(aroute())
def test_dashboard_file_exists():assert os.path.exists(os.path.join(os.path.dirname(__file__),'..','frontend','index.html'))
