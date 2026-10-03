import os,sys,asyncio,time
sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','backend'))
os.environ['DATABASE_URL']='sqlite:///:memory:'
from app.models import Tick,OrderRequest
from app.market import MarketDataGateway
from app.risk import ProductionDualRiskEngine
from app.engine import DecisionPipeline,EngineContext
from app.ledger import Ledger

def order(mode='MANUAL'):
    return OrderRequest(broker='TEST',exchange='NSE',symbol='NIFTY',side='BUY',qty=1,mode=mode)

def test_soft_stop_is_enforced():
    ctx=EngineContext(market={'live':True,'liquid':True},portfolio={'broker_reconciled':True,'net_pnl':-2000},broker_state={'reconciled':True},mode='MANUAL')
    p=type('P',(),{'order':order()})()
    assert not ProductionDualRiskEngine().evaluate_context(ctx,p)['approved']

def test_risk_disagreement_veto():
    ctx=EngineContext(market={'live':False},portfolio={'broker_reconciled':True},broker_state={'reconciled':True},mode='MANUAL')
    p=type('P',(),{'order':order()})()
    out=ProductionDualRiskEngine().evaluate_context(ctx,p)
    assert not out['approved']

def test_fill_idempotency():
    l=Ledger(); f={'client_order_id':'c','broker_order_id':'b','qty':1,'price':100}
    assert l.record_fill(f) is True
    assert l.record_fill(f) is False
    assert l.snapshot()['fills']==1

def test_data_quality_crossed_book():
    g=MarketDataGateway(); now=int(time.time()*1000)
    t=Tick(broker='TEST',exchange='NSE',instrument_token='1',symbol='NIFTY',ltp=100,bid=101,ask=100,exchange_ts_ms=now,receive_ts_ms=now)
    assert not g.ingest(t)['accepted']

def test_data_unavailable_never_strategy():
    o=order();ctx=EngineContext(market={'live':False,'order':o},portfolio={'broker_reconciled':True},broker_state={'reconciled':True},mode='MANUAL')
    assert DecisionPipeline().evaluate(ctx)['approved'] is False
