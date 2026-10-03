"""Hash-chained audit log.

Each record commits to the previous record's hash, its action, actor, payload
and timestamp. When AUDIT_HMAC_KEY is set the chain uses HMAC-SHA256, so a
person with database write access but without the key cannot rewrite history
and recompute a valid chain. Without the key the chain only detects edits made
by someone who does not also recompute every later hash.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

from .config import settings


def _digest(body, key):
    blob = json.dumps(body, sort_keys=True, default=str).encode()
    if key:
        return hmac.new(key.encode(), blob, hashlib.sha256).hexdigest()
    return hashlib.sha256(blob).hexdigest()


class PersistentAuditChain:
    def __init__(self, ledger, key=None):
        self.ledger = ledger
        self.key = settings.audit_hmac_key if key is None else key

    def append(self, action, actor, payload):
        prev = self.ledger.latest_audit_hash() or 'GENESIS'
        # Round-trip the payload through JSON so the hashed form equals the stored form.
        payload = json.loads(json.dumps(payload, default=str, sort_keys=True))
        body = {'prev': prev, 'action': str(action), 'actor': str(actor), 'payload': payload,
                'ts': datetime.now(timezone.utc).isoformat()}
        digest = _digest(body, self.key)
        self.ledger.audit_record(digest, body, bool(self.key))
        return digest

    def verify(self):
        prev = 'GENESIS'
        records = self.ledger.audit_records()
        for r in records:
            if r['prev'] != prev:
                return {'ok': False, 'reason': 'AUDIT_CHAIN_LINK_BROKEN', 'at': r['hash']}
            body = {'prev': prev, 'action': r['action'], 'actor': r['actor'], 'payload': r['payload'], 'ts': r['ts']}
            if _digest(body, self.key if r['keyed'] else '') != r['hash']:
                return {'ok': False, 'reason': 'AUDIT_CHAIN_BROKEN', 'at': r['hash']}
            prev = r['hash']
        return {'ok': True, 'records': len(records), 'keyed': bool(self.key)}
