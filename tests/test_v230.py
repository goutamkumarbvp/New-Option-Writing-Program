from app.enterprise_controls import RBAC,ComplianceGuard,TamperEvidentAudit,HealthBudget,DRRunbook

def test_rbac_and_compliance():
    assert RBAC().allowed('TRADER','trade')
    assert not RBAC().allowed('VIEWER','trade')
    c=ComplianceGuard(); assert not c.validate({'symbol':'NIFTY','qty':1},role='VIEWER',live=True)['approved']
    assert c.validate({'symbol':'NIFTY','qty':1},role='TRADER',live=True)['approved']

def test_audit_chain():
    a=TamperEvidentAudit(); a.append('ORDER','TRADER',{'id':'1'}); a.append('FILL','SYSTEM',{'qty':1}); assert a.verify()['ok']
    a.records[0]['payload']['id']='tampered'; assert not a.verify()['ok']

def test_readiness_and_dr():
    h=HealthBudget(); assert not h.assess({'db':True,'feed':False})['ready']
    d=DRRunbook(); assert d.failover(False,True)['mode']=='SECONDARY'; assert d.failover(False,False)['mode']=='HALT'
