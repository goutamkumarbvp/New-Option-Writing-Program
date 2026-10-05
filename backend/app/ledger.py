"""Authoritative SQL ledger: orders, fills, positions, events, audit chain,
reconciliation runs, control state (kill switch) and daily P&L baselines.

Order state changes go through ONE function, ``transition``, which enforces the
order state machine on every path (API, order monitor, reconciliation). The
original enforced it only in ``upsert_order`` while the monitor wrote statuses
directly, so a FILLED order could regress to OPEN.
"""
import json
import os
from datetime import datetime, timezone

from sqlalchemy import (Column, DateTime, Float, Integer, String, Text, UniqueConstraint, create_engine, inspect,
                        select)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.pool import StaticPool

from .clock import trading_date
from .config import settings
from .normalize import TERMINAL

Base = declarative_base()


def utcnow():
    return datetime.now(timezone.utc)


class OrderLedger(Base):
    __tablename__ = 'orders'
    client_order_id = Column(String, primary_key=True)
    broker_order_id = Column(String, index=True)
    broker = Column(String, index=True)
    exchange = Column(String)
    symbol = Column(String)
    instrument_token = Column(String)
    side = Column(String)
    qty = Column(Integer)
    price = Column(Float)
    order_type = Column(String)
    product = Column(String)
    tag = Column(String, index=True)
    filled_qty = Column(Integer, default=0)
    status = Column(String, index=True)
    avg_price = Column(Float, default=0)
    arrival_price = Column(Float)
    trading_date = Column(String, index=True)
    origin = Column(String, default='TERMINAL')
    last_reason = Column(Text)
    raw = Column(Text)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


class FillLedger(Base):
    __tablename__ = 'fills'
    id = Column(Integer, primary_key=True, autoincrement=True)
    client_order_id = Column(String, index=True)
    broker_order_id = Column(String, index=True)
    broker_trade_id = Column(String, unique=True, index=True, nullable=True)
    qty = Column(Integer)
    price = Column(Float)
    ts = Column(DateTime(timezone=True))
    raw = Column(Text)


class PositionLedger(Base):
    __tablename__ = 'positions'
    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String)
    exchange = Column(String)
    symbol = Column(String)
    token = Column(String)
    qty = Column(Integer)
    avg_price = Column(Float)
    pnl = Column(Float)
    updated_at = Column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint('broker', 'exchange', 'symbol', name='uq_position'),)


class EventLedger(Base):
    __tablename__ = 'events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    topic = Column(String, index=True)
    payload = Column(Text)
    ts = Column(DateTime(timezone=True))


class AuditLedger(Base):
    __tablename__ = 'audit_chain'
    id = Column(Integer, primary_key=True, autoincrement=True)
    hash = Column(String, unique=True, index=True)
    prev = Column(String)
    action = Column(String)
    actor = Column(String)
    payload = Column(Text)
    ts = Column(DateTime(timezone=True))
    ts_text = Column(String)
    keyed = Column(Integer, default=0)


class ReconcileLedger(Base):
    __tablename__ = 'reconciliation'
    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String, index=True)
    ok = Column(Integer)
    mismatches = Column(Text)
    ts = Column(DateTime(timezone=True))


class ControlState(Base):
    __tablename__ = 'control_state'
    key = Column(String, primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime(timezone=True))


class DailyBaseline(Base):
    __tablename__ = 'daily_pnl_baseline'
    broker = Column(String, primary_key=True)
    trading_date = Column(String, primary_key=True)
    total_pnl = Column(Float)
    captured_at = Column(DateTime(timezone=True))


ALLOWED_TRANSITIONS = {
    'PENDING_SUBMIT': {'SUBMITTED', 'OPEN', 'PARTIAL', 'FILLED', 'REJECTED', 'CANCELLED', 'UNKNOWN'},
    'SUBMITTED': {'OPEN', 'PARTIAL', 'FILLED', 'REJECTED', 'CANCELLED', 'UNKNOWN', 'EXPIRED'},
    'OPEN': {'PARTIAL', 'FILLED', 'CANCELLED', 'REJECTED', 'UNKNOWN', 'EXPIRED'},
    'PARTIAL': {'FILLED', 'CANCELLED', 'UNKNOWN', 'EXPIRED'},
    'UNKNOWN': {'SUBMITTED', 'OPEN', 'PARTIAL', 'FILLED', 'REJECTED', 'CANCELLED', 'EXPIRED'},
    'FILLED': set(), 'REJECTED': set(), 'CANCELLED': set(), 'EXPIRED': set(),
}


def _order_dict(x):
    return {'client_order_id': x.client_order_id, 'broker_order_id': x.broker_order_id, 'broker': x.broker,
            'exchange': x.exchange, 'symbol': x.symbol, 'instrument_token': x.instrument_token, 'side': x.side,
            'qty': x.qty, 'price': x.price, 'order_type': x.order_type, 'product': x.product, 'tag': x.tag,
            'filled_qty': x.filled_qty, 'status': x.status, 'avg_price': x.avg_price, 'arrival_price': x.arrival_price,
            'trading_date': x.trading_date, 'origin': x.origin, 'last_reason': x.last_reason,
            'created_at': x.created_at.isoformat() if x.created_at else None,
            'updated_at': x.updated_at.isoformat() if x.updated_at else None}


class Ledger:
    def __init__(self, url=None):
        url = url or settings.database_url
        kw = {'future': True, 'pool_pre_ping': True}
        if url.startswith('postgresql'):
            # Fail fast during a database outage instead of freezing the terminal on a stalled socket;
            # pool_pre_ping reconnects by itself once the database is back.
            kw.update(pool_timeout=5, pool_recycle=1800,
                      connect_args={'connect_timeout': 3, 'options': '-c statement_timeout=10000'})
        if url.startswith('sqlite'):
            kw['connect_args'] = {'check_same_thread': False}
            if ':memory:' in url:
                kw['poolclass'] = StaticPool
            else:
                path = url.replace('sqlite:///', '', 1)
                if path:
                    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
        self.engine = create_engine(url, **kw)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    # ---- schema --------------------------------------------------------
    def schema_drift(self):
        """Columns the models expect but the database lacks. ``create_all`` never
        alters an existing table, so an upgraded build on an old PostgreSQL schema
        must not trade until migrated."""
        insp = inspect(self.engine)
        missing = []
        for table in Base.metadata.sorted_tables:
            have = {c['name'] for c in insp.get_columns(table.name)}
            missing += [f'{table.name}.{c.name}' for c in table.columns if c.name not in have]
        return missing

    # ---- events --------------------------------------------------------
    def event(self, topic, payload):
        with self.Session() as s:
            s.add(EventLedger(topic=topic, payload=json.dumps(payload, default=str), ts=utcnow()))
            s.commit()

    def events(self, topic=None, limit=100):
        with self.Session() as s:
            q = select(EventLedger).order_by(EventLedger.id.desc()).limit(limit)
            if topic:
                q = q.where(EventLedger.topic == topic)
            return [{'topic': e.topic, 'payload': json.loads(e.payload), 'ts': e.ts.isoformat() if e.ts else None}
                    for e in s.scalars(q)]

    # ---- orders --------------------------------------------------------
    def create_pending_order(self, o):
        """Write-ahead record BEFORE the broker call. The primary key makes the
        client_order_id idempotency check atomic across concurrent requests."""
        now = utcnow()
        with self.Session() as s:
            s.add(OrderLedger(client_order_id=o['client_order_id'], broker=o['broker'], exchange=o['exchange'],
                              symbol=o['symbol'], instrument_token=o.get('instrument_token'), side=o['side'], qty=o['qty'],
                              price=o.get('price'), order_type=o.get('order_type'), product=o.get('product'),
                              tag=o.get('tag'), filled_qty=0, status='PENDING_SUBMIT', avg_price=0,
                              arrival_price=o.get('arrival_price'), trading_date=trading_date(), origin='TERMINAL',
                              raw=json.dumps(o, default=str), created_at=now, updated_at=now))
            try:
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def import_external_order(self, o):
        """Broker-side order not placed by this terminal (manual/app order)."""
        cid = f"EXTERNAL-{o['broker']}-{o['broker_order_id']}"
        now = utcnow()
        with self.Session() as s:
            if s.get(OrderLedger, cid):
                return cid
            s.add(OrderLedger(client_order_id=cid, broker_order_id=o['broker_order_id'], broker=o['broker'],
                              exchange=o.get('exchange'), symbol=o.get('symbol'), instrument_token=o.get('token'),
                              side=o.get('side'), qty=o.get('qty'), filled_qty=o.get('filled_qty', 0), status=o['status'],
                              avg_price=o.get('avg_price', 0), tag=o.get('tag'), trading_date=trading_date(),
                              origin='EXTERNAL', raw=json.dumps(o.get('raw', {}), default=str), created_at=now, updated_at=now))
            s.commit()
        return cid

    def transition(self, client_order_id, status, reason=None, **fields):
        """Apply a status change if the state machine allows it.

        Returns (applied: bool, previous_status). Same-status updates may still
        advance filled_qty / avg_price / broker_order_id. filled_qty never decreases.
        """
        with self.Session() as s:
            x = s.get(OrderLedger, client_order_id)
            if x is None:
                return False, None
            prev = x.status
            if status and status != prev and status not in ALLOWED_TRANSITIONS.get(prev, set()):
                return False, prev
            if status:
                x.status = status
            if fields.get('broker_order_id') and not x.broker_order_id:
                x.broker_order_id = str(fields['broker_order_id'])
            if fields.get('filled_qty') is not None:
                x.filled_qty = max(int(x.filled_qty or 0), int(fields['filled_qty']))
            if fields.get('avg_price'):
                x.avg_price = float(fields['avg_price'])
            if reason:
                x.last_reason = str(reason)[:500]
            if fields.get('raw') is not None:
                x.raw = json.dumps(fields['raw'], default=str)
            x.updated_at = utcnow()
            s.commit()
            return True, prev

    def get_order(self, client_order_id):
        with self.Session() as s:
            x = s.get(OrderLedger, client_order_id)
            return _order_dict(x) if x else None

    def order_by_broker_id(self, broker, bo):
        with self.Session() as s:
            x = s.scalars(select(OrderLedger).where(OrderLedger.broker == broker, OrderLedger.broker_order_id == str(bo))).first()
            return _order_dict(x) if x else None

    def order_by_tag(self, broker, tag):
        if not tag:
            return None
        with self.Session() as s:
            x = s.scalars(select(OrderLedger).where(OrderLedger.broker == broker, OrderLedger.tag == tag)).first()
            return _order_dict(x) if x else None

    def orders(self, broker=None, limit=200, statuses=None, trading_day=None):
        with self.Session() as s:
            q = select(OrderLedger).order_by(OrderLedger.created_at.desc()).limit(limit)
            if broker:
                q = q.where(OrderLedger.broker == broker.upper())
            if statuses:
                q = q.where(OrderLedger.status.in_(list(statuses)))
            if trading_day:
                q = q.where(OrderLedger.trading_date == trading_day)
            return [_order_dict(x) for x in s.scalars(q)]

    def working_orders(self, broker=None):
        return self.orders(broker, limit=10000, statuses=['PENDING_SUBMIT', 'SUBMITTED', 'OPEN', 'PARTIAL', 'UNKNOWN'])

    def ambiguous_orders(self, broker=None):
        return self.orders(broker, limit=10000, statuses=['PENDING_SUBMIT', 'UNKNOWN'])

    # ---- fills / positions --------------------------------------------
    def record_fill(self, f):
        trade_id = str(f.get('trade_id') or f.get('broker_trade_id') or '') or None
        with self.Session() as s:
            if trade_id and s.scalars(select(FillLedger).where(FillLedger.broker_trade_id == trade_id)).first():
                return False
            s.add(FillLedger(client_order_id=f['client_order_id'], broker_order_id=f.get('broker_order_id'),
                             broker_trade_id=trade_id, qty=int(f['qty']), price=float(f['price']),
                             raw=json.dumps(f.get('raw', f), default=str), ts=utcnow()))
            try:
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    def fills_for(self, client_order_id):
        with self.Session() as s:
            return [{'qty': x.qty, 'price': x.price, 'trade_id': x.broker_trade_id}
                    for x in s.scalars(select(FillLedger).where(FillLedger.client_order_id == client_order_id))]

    def upsert_position(self, p):
        with self.Session() as s:
            q = s.scalars(select(PositionLedger).where(PositionLedger.broker == p['broker'], PositionLedger.exchange == p['exchange'],
                                                       PositionLedger.symbol == p['symbol'])).first()
            q = q or PositionLedger(broker=p['broker'], exchange=p['exchange'], symbol=p['symbol'])
            q.token = p.get('token')
            q.qty = int(p.get('qty') or 0)
            q.avg_price = float(p.get('avg_price') or 0)
            q.pnl = float(p.get('pnl') or 0)
            q.updated_at = utcnow()
            s.add(q)
            s.commit()

    # ---- control state / baselines ------------------------------------
    def get_control(self, key):
        with self.Session() as s:
            x = s.get(ControlState, key)
            return json.loads(x.value) if x else None

    def set_control(self, key, value):
        with self.Session() as s:
            x = s.get(ControlState, key) or ControlState(key=key)
            x.value = json.dumps(value, default=str)
            x.updated_at = utcnow()
            s.add(x)
            s.commit()

    def latest_kill(self):
        st = self.get_control('kill_switch')
        return st if st and st.get('active') else None

    def daily_baseline(self, broker, day):
        with self.Session() as s:
            x = s.get(DailyBaseline, (broker, day))
            return None if not x else {'total_pnl': x.total_pnl, 'captured_at': x.captured_at}

    def set_daily_baseline(self, broker, day, total_pnl):
        with self.Session() as s:
            if s.get(DailyBaseline, (broker, day)):
                return False
            s.add(DailyBaseline(broker=broker, trading_date=day, total_pnl=float(total_pnl), captured_at=utcnow()))
            try:
                s.commit()
                return True
            except IntegrityError:
                s.rollback()
                return False

    # ---- audit / reconciliation ---------------------------------------
    def audit_record(self, digest, body, keyed):
        with self.Session() as s:
            s.add(AuditLedger(hash=digest, prev=body['prev'], action=body['action'], actor=body['actor'],
                              payload=json.dumps(body['payload'], default=str, sort_keys=True), ts=utcnow(),
                              ts_text=body['ts'], keyed=int(bool(keyed))))
            s.commit()

    def latest_audit_hash(self):
        with self.Session() as s:
            x = s.scalars(select(AuditLedger).order_by(AuditLedger.id.desc())).first()
            return x.hash if x else None

    def audit_records(self, limit=None):
        with self.Session() as s:
            q = select(AuditLedger).order_by(AuditLedger.id.asc())
            return [{'hash': x.hash, 'prev': x.prev, 'action': x.action, 'actor': x.actor, 'payload': json.loads(x.payload),
                     'ts': x.ts_text, 'keyed': bool(x.keyed)} for x in s.scalars(q)]

    def record_reconciliation(self, broker, ok, mismatches):
        with self.Session() as s:
            s.add(ReconcileLedger(broker=broker, ok=int(ok), mismatches=json.dumps(mismatches, default=str), ts=utcnow()))
            s.commit()

    def last_reconciliation(self, broker):
        with self.Session() as s:
            x = s.scalars(select(ReconcileLedger).where(ReconcileLedger.broker == broker)
                          .order_by(ReconcileLedger.id.desc())).first()
            if not x:
                return None
            ts = x.ts if x.ts.tzinfo else x.ts.replace(tzinfo=timezone.utc)  # SQLite drops tzinfo
            return {'ok': bool(x.ok), 'mismatches': json.loads(x.mismatches), 'ts': ts,
                    'age_sec': (utcnow() - ts).total_seconds()}

    def order_status_counts(self):
        from sqlalchemy import func
        with self.Session() as s:
            return {st: n for st, n in s.execute(select(OrderLedger.status, func.count()).group_by(OrderLedger.status))}

    def snapshot(self):
        from sqlalchemy import func
        with self.Session() as s:
            def n(model):
                return s.scalar(select(func.count()).select_from(model))
            return {'orders': n(OrderLedger), 'fills': n(FillLedger), 'positions': n(PositionLedger), 'events': n(EventLedger),
                    'reconciliations': n(ReconcileLedger), 'audit_records': n(AuditLedger)}


__all__ = ['Ledger', 'ALLOWED_TRANSITIONS', 'TERMINAL']
