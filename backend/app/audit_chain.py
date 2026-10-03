import hashlib,json
from datetime import datetime,timezone
class PersistentAuditChain:
    """Hash-chained audit records persisted in the authoritative ledger."""
    def __init__(self, ledger): self.ledger=ledger
    def append(self, action, actor, payload):
        prev=self.ledger.latest_audit_hash() or 'GENESIS'
        body={'prev':prev,'action':action,'actor':actor,'payload':payload}
        digest=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()
        self.ledger.audit_record(digest,body)
        return digest
    def verify(self):
        prev='GENESIS'
        for r in self.ledger.audit_records():
            body={'prev':prev,'action':r['action'],'actor':r['actor'],'payload':r['payload']}
            h=hashlib.sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()
            if h!=r['hash']: return {'ok':False,'reason':'AUDIT_CHAIN_BROKEN','at':r['hash']}
            prev=h
        return {'ok':True,'records':len(self.ledger.audit_records())}
