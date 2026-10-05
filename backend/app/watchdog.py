import time
from datetime import datetime, timezone


class Watchdog:
    """Heartbeat fed only by ACCEPTED ticks (the original also fed it on rejected
    stale ticks, which kept a dead feed looking healthy)."""

    def __init__(self, timeout=5):
        self.timeout = timeout
        self.last = 0.0  # unhealthy until the first accepted live tick: no data, no heartbeat

    def beat(self):
        self.last = time.time()

    def healthy(self):
        return time.time() - self.last <= self.timeout


class KillSwitch:
    """Persistent kill switch. Every engage and every reset is written to the ledger
    and audit chain, and the state is restored on restart from the control table."""

    def __init__(self, ledger=None, audit=None):
        self.ledger = ledger
        self.audit = audit
        self.triggered = False
        self.reason = ''
        self.actor = ''
        self.at = None
        self.persist_pending = None   # event name not yet written (database briefly down); retried
        self.persist_error = None

    def load(self):
        if not self.ledger:
            return
        st = self.ledger.get_control('kill_switch')
        if st and st.get('active'):
            self.triggered, self.reason, self.actor, self.at = True, st.get('reason', ''), st.get('actor', ''), st.get('at')

    def _persist(self, event):
        """Write the state to the ledger and audit chain. If the database is briefly unavailable the
        in-memory state still holds (orders stay blocked) and retry_persist() writes it later; the
        hard-stop actions that follow a trigger are never skipped because a write failed."""
        state = {'active': self.triggered, 'reason': self.reason, 'actor': self.actor, 'at': self.at}
        try:
            if self.ledger:
                self.ledger.set_control('kill_switch', state)
                self.ledger.event(event, state)
            if self.audit:
                self.audit.append(event, self.actor or 'SYSTEM', state)
            self.persist_pending, self.persist_error = None, None
        except Exception as exc:  # noqa: BLE001
            self.persist_pending, self.persist_error = event, f'{type(exc).__name__}: {str(exc)[:120]}'

    def retry_persist(self):
        if self.persist_pending:
            self._persist(self.persist_pending)
        return self.persist_pending is None

    def trigger(self, reason, actor='SYSTEM'):
        self.triggered, self.reason, self.actor = True, str(reason)[:200], actor
        self.at = datetime.now(timezone.utc).isoformat()
        self._persist('KILL_SWITCH')

    def reset(self, actor, reason):
        self.triggered, self.reason, self.actor = False, str(reason)[:200], actor
        self.at = datetime.now(timezone.utc).isoformat()
        self._persist('KILL_SWITCH_RESET')

    def state(self):
        return {'active': self.triggered, 'reason': self.reason, 'actor': self.actor, 'at': self.at,
                'persist_pending': bool(self.persist_pending), 'persist_error': self.persist_error}
