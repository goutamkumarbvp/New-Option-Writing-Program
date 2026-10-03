import asyncio
from app.institutional import *

def test_fix_replay_and_no_transport():
    g=FixGateway(); r=asyncio.run(g.send_order({'symbol':'NIFTY','side':'BUY','qty':1})); assert r['status']=='FIX_TRANSPORT_UNAVAILABLE'
    assert g.accept_execution_report({'34':1,'17':'x'})['accepted']; assert not g.accept_execution_report({'34':1,'17':'x'})['accepted']
def test_multileg_parent_child():
    o=MultiLegOMS(); p=o.stage('BULL_CALL',[{'symbol':'A','side':'BUY','qty':1},{'symbol':'B','side':'SELL','qty':1}]); cs=o.release(p,'ZERODHA'); assert len(cs)==2
    for c in cs:c.status='FILLED'
    assert o.aggregate(p)=='FILLED'
def test_hierarchical_risk():
    r=HierarchicalRiskBook();r.set_limit('FIRM','ALL',100);r.set_limit('ACCOUNT','A',50)
    assert r.reserve([('FIRM','ALL'),('ACCOUNT','A')],40)['approved'];assert not r.reserve([('FIRM','ALL'),('ACCOUNT','A')],20)['approved']
def test_allocation_and_tca():
    a=AllocationEngine().pro_rata(10,[{'account':'A','requested_qty':1},{'account':'B','requested_qty':1}]);assert sum(x['qty'] for x in a)==10
    assert TCAEngine().analyze('BUY',1,100,101)['arrival_slippage_bps']>0
def test_surveillance_exception_dr():
    assert SurveillanceEngine().inspect([{'type':'NEW','self_trade':True}])[0]['severity']=='HIGH'
    e=ExceptionManager();x=e.open('REJECT',{});assert e.resolve(x['id'],'MANUAL_REVIEW')['status']=='RESOLVED'
    d=DRController();d.update(False,True);assert d.failover()['active']=='SECONDARY'
def test_release_evidence_fails_closed(): assert not ReleaseEvidence().certify({'unit_tests':True})['certified']
def test_derivatives_risk_and_algos():
    assert VolSurfaceEngine().fit_smile(100,[VolPoint(90,.3),VolPoint(100,.2),VolPoint(110,.25)])['ok']
    assert PortfolioGreeks().aggregate([{'qty':2,'delta':.5}])['delta']==1
    assert ScenarioRiskEngine().run([{'qty':1,'spot':100,'delta':1}])['scenarios']
    assert not SelfTradePrevention().check({'symbol':'X','side':'BUY','broker':'A','price':101},[{'symbol':'X','side':'SELL','broker':'A','price':100}])['approved']
    assert AlgoOrderPlanner().iceberg(10,3)['children']==[3,3,3,1]
    assert len(AlgoOrderPlanner().twap(10,2,0,100)['children'])==2
    assert not HAReadiness().assess({})['ready']
