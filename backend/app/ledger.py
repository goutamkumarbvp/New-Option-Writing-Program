import os,json
from datetime import datetime,timezone
from sqlalchemy import create_engine,Column,Integer,String,Float,Text,DateTime,UniqueConstraint,Index
from sqlalchemy.orm import declarative_base,sessionmaker
from .config import settings
Base=declarative_base()
class OrderLedger(Base):
    __tablename__='orders'; client_order_id=Column(String,primary_key=True); broker_order_id=Column(String,index=True); broker=Column(String,index=True); exchange=Column(String); symbol=Column(String); side=Column(String); qty=Column(Integer); filled_qty=Column(Integer,default=0); status=Column(String,index=True); avg_price=Column(Float,default=0); raw=Column(Text); created_at=Column(DateTime(timezone=True)); updated_at=Column(DateTime(timezone=True))
class FillLedger(Base):
    __tablename__='fills'
    id=Column(Integer,primary_key=True,autoincrement=True)
    client_order_id=Column(String,index=True); broker_order_id=Column(String,index=True)
    broker_trade_id=Column(String,unique=True,index=True,nullable=True)
    qty=Column(Integer); price=Column(Float); ts=Column(DateTime(timezone=True)); raw=Column(Text)
class PositionLedger(Base):
    __tablename__='positions'; id=Column(Integer,primary_key=True,autoincrement=True); broker=Column(String); exchange=Column(String); symbol=Column(String); qty=Column(Integer); avg_price=Column(Float); pnl=Column(Float); updated_at=Column(DateTime(timezone=True)); __table_args__=(UniqueConstraint('broker','exchange','symbol',name='uq_position'),)
class EventLedger(Base):
    __tablename__='events'; id=Column(Integer,primary_key=True,autoincrement=True); topic=Column(String,index=True); payload=Column(Text); ts=Column(DateTime(timezone=True))
class AuditLedger(Base):
    __tablename__='audit_chain'
    id=Column(Integer,primary_key=True,autoincrement=True); hash=Column(String,unique=True,index=True); prev=Column(String); action=Column(String); actor=Column(String); payload=Column(Text); ts=Column(DateTime(timezone=True))
class ReconcileLedger(Base):
    __tablename__='reconciliation'; id=Column(Integer,primary_key=True,autoincrement=True); broker=Column(String); ok=Column(Integer); mismatches=Column(Text); ts=Column(DateTime(timezone=True))
Index('ix_order_broker_order','orders','broker_order_id')
ALLOWED_TRANSITIONS={
 'NEW':{'SUBMITTED','REJECTED','CANCELLED'},
 'SUBMITTED':{'OPEN','PARTIAL','FILLED','REJECTED','CANCELLED','UNKNOWN'},
 'OPEN':{'PARTIAL','FILLED','CANCELLED','REJECTED','UNKNOWN'},
 'PARTIAL':{'PARTIAL','FILLED','CANCELLED','REJECTED','UNKNOWN'},
 'FILLED':set(), 'REJECTED':set(), 'CANCELLED':set(), 'UNKNOWN':{'OPEN','PARTIAL','FILLED','REJECTED','CANCELLED','UNKNOWN'}
}
class Ledger:
    def __init__(self):
        if settings.database_url.startswith('sqlite:///'):
            path=settings.database_url.replace('sqlite:///','',1)
            if path and path!=':memory:': os.makedirs(os.path.dirname(path) or '.',exist_ok=True)
        self.engine=create_engine(settings.database_url,future=True)
        Base.metadata.create_all(self.engine); self.Session=sessionmaker(bind=self.engine)
    def event(self,topic,payload):
        with self.Session() as s:s.add(EventLedger(topic=topic,payload=json.dumps(payload,default=str),ts=datetime.now(timezone.utc)));s.commit()
    def upsert_order(self,o):
        with self.Session() as s:
            now=datetime.now(timezone.utc); x=s.get(OrderLedger,o['client_order_id']) or OrderLedger(client_order_id=o['client_order_id'],created_at=now)
            incoming=o.get('status')
            if incoming and x.status and incoming != x.status and incoming not in ALLOWED_TRANSITIONS.get(str(x.status).upper(),set()):
                # Never allow a terminal order to move backwards due to stale broker polling.
                return
            for k in ('broker_order_id','broker','exchange','symbol','side','qty','filled_qty','status','avg_price'):
                if k in o and o[k] is not None:setattr(x,k,o[k])
            x.raw=json.dumps(o,default=str);x.updated_at=now;s.add(x);s.commit()
    def record_fill(self,f):
        # Broker trade ID is authoritative when supplied; the fallback fingerprint
        # is used only by brokers that do not expose a trade ID.
        trade_id=str(f.get('trade_id') or f.get('broker_trade_id') or '')
        with self.Session() as s:
            if trade_id and s.query(FillLedger).filter_by(broker_trade_id=trade_id).first():
                return False
            existing=s.query(FillLedger).filter_by(client_order_id=f['client_order_id'],broker_order_id=f.get('broker_order_id'),qty=int(f['qty']),price=float(f['price'])).first()
            if existing and not trade_id:
                return False
            s.add(FillLedger(client_order_id=f['client_order_id'],broker_order_id=f.get('broker_order_id'),
                             broker_trade_id=trade_id or None,qty=int(f['qty']),price=float(f['price']),
                             raw=json.dumps(f,default=str),ts=datetime.now(timezone.utc)))
            s.commit();return True
    def upsert_position(self,p):
        with self.Session() as s:
            q=s.query(PositionLedger).filter_by(broker=p['broker'],exchange=p['exchange'],symbol=p['symbol']).first() or PositionLedger(broker=p['broker'],exchange=p['exchange'],symbol=p['symbol'])
            for k in ('qty','avg_price','pnl'):setattr(q,k,p.get(k,0));q.updated_at=datetime.now(timezone.utc);s.add(q);s.commit()
    def update_order_by_broker_id(self,bo,status,filled_qty=None,avg_price=None,raw=None):
        with self.Session() as s:
            x=s.query(OrderLedger).filter_by(broker_order_id=str(bo)).first()
            if not x:return False
            x.status=status
            if filled_qty is not None:x.filled_qty=int(filled_qty)
            if avg_price is not None:x.avg_price=float(avg_price)
            if raw is not None:x.raw=json.dumps(raw,default=str)
            x.updated_at=datetime.now(timezone.utc);s.add(x);s.commit();return True
    def order_by_broker_id(self,bo):
        with self.Session() as s:
            x=s.query(OrderLedger).filter_by(broker_order_id=str(bo)).first();return None if not x else {'client_order_id':x.client_order_id,'broker_order_id':x.broker_order_id,'status':x.status,'filled_qty':x.filled_qty,'avg_price':x.avg_price,'broker':x.broker,'symbol':x.symbol,'qty':x.qty}
    def orders(self,broker=None):
        with self.Session() as s:
            q=s.query(OrderLedger); q=q.filter_by(broker=broker) if broker else q
            return [{'client_order_id':x.client_order_id,'broker_order_id':x.broker_order_id,'broker':x.broker,'exchange':x.exchange,'symbol':x.symbol,'side':x.side,'qty':x.qty,'filled_qty':x.filled_qty,'status':x.status,'avg_price':x.avg_price} for x in q.all()]
    def latest_kill(self):
        with self.Session() as s:
            rows=s.query(EventLedger).filter_by(topic='KILL_SWITCH').order_by(EventLedger.id.desc()).limit(1).all()
            if not rows:return None
            try:return json.loads(rows[0].payload)
            except Exception:return {'reason':'PERSISTED_KILL_SWITCH'}
    def snapshot(self):
        with self.Session() as s:return {'orders':s.query(OrderLedger).count(),'fills':s.query(FillLedger).count(),'positions':s.query(PositionLedger).count(),'events':s.query(EventLedger).count(),'reconciliations':s.query(ReconcileLedger).count(),'audit_records':s.query(AuditLedger).count()}
    def audit_record(self,digest,body):
        with self.Session() as s:
            s.add(AuditLedger(hash=digest,prev=body['prev'],action=body['action'],actor=body['actor'],payload=json.dumps(body['payload'],default=str),ts=datetime.now(timezone.utc)));s.commit()
    def latest_audit_hash(self):
        with self.Session() as s:
            x=s.query(AuditLedger).order_by(AuditLedger.id.desc()).first();return x.hash if x else None
    def audit_records(self):
        with self.Session() as s:
            rows=s.query(AuditLedger).order_by(AuditLedger.id.asc()).all();return [{'hash':x.hash,'prev':x.prev,'action':x.action,'actor':x.actor,'payload':json.loads(x.payload)} for x in rows]
    def record_reconciliation(self,broker,ok,mismatches):
        with self.Session() as s:s.add(ReconcileLedger(broker=broker,ok=int(ok),mismatches=json.dumps(mismatches,default=str),ts=datetime.now(timezone.utc)));s.commit()
