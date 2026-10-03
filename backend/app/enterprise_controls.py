from dataclasses import dataclass, field
from time import time_ns
import hashlib, json

@dataclass
class AuditRecord:
    action: str
    actor: str
    payload: dict
    ts_ns: int = field(default_factory=time_ns)

class RBAC:
    ROLES = {
        'VIEWER': {'view'},
        'TRADER': {'view','trade'},
        'RISK': {'view','risk','kill'},
        'ADMIN': {'view','trade','risk','kill','config','reconcile'},
    }
    def allowed(self, role, action):
        return action in self.ROLES.get(str(role).upper(), set())

class ComplianceGuard:
    def validate(self, order, *, role='TRADER', kill=False, live=False):
        reasons=[]
        if not self.RBAC.allowed(role,'trade'): reasons.append('ROLE_NOT_AUTHORIZED')
        if kill: reasons.append('KILL_SWITCH_ACTIVE')
        if not live: reasons.append('LIVE_TRADING_DISABLED')
        if int(order.get('qty',0)) <= 0: reasons.append('INVALID_QTY')
        if not order.get('symbol'): reasons.append('MISSING_SYMBOL')
        return {'approved': not reasons, 'reasons': reasons}
    RBAC=RBAC()

class TamperEvidentAudit:
    def __init__(self): self.prev='GENESIS'; self.records=[]
    def append(self, action, actor, payload):
        body={'prev':self.prev,'action':action,'actor':actor,'payload':payload}
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()
        self.prev=digest; self.records.append({'hash':digest,**body}); return digest
    def verify(self):
        prev='GENESIS'
        for r in self.records:
            body={'prev':prev,'action':r['action'],'actor':r['actor'],'payload':r['payload']}
            h=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()
            if h!=r['hash']: return {'ok':False,'reason':'AUDIT_CHAIN_BROKEN'}
            prev=h
        return {'ok':True,'records':len(self.records)}

class HealthBudget:
    def assess(self, components):
        bad=[k for k,v in components.items() if not bool(v)]
        return {'ready':not bad,'failed_components':bad}

class DRRunbook:
    def __init__(self): self.mode='PRIMARY'
    def failover(self, primary_ok, secondary_ok):
        if primary_ok: self.mode='PRIMARY'
        elif secondary_ok: self.mode='SECONDARY'
        else: self.mode='HALT'
        return {'mode':self.mode,'trading_allowed':self.mode!='HALT'}
