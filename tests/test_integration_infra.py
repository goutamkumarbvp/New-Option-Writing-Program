"""Real PostgreSQL / Redis integration. Skipped unless IORT_TEST_PG_URL / IORT_TEST_REDIS_URL are set, e.g.
IORT_TEST_PG_URL=postgresql+psycopg://iort:pw@127.0.0.1:5432/iort IORT_TEST_REDIS_URL=redis://127.0.0.1:6390/0"""
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import override
from sqlalchemy import create_engine, text

from app.audit_chain import PersistentAuditChain
from app.eventbus import EventBus
from app.ledger import Base, Ledger

PG = os.getenv('IORT_TEST_PG_URL')
REDIS = os.getenv('IORT_TEST_REDIS_URL')


@pytest.fixture
def pg():
    if not PG:
        pytest.skip('IORT_TEST_PG_URL not set')
    eng = create_engine(PG)
    Base.metadata.drop_all(eng)
    yield PG
    Base.metadata.drop_all(eng)


def test_postgres_idempotency_is_atomic_under_thread_concurrency(pg):
    l = Ledger(pg)
    o = {'client_order_id': 'RACE', 'broker': 'ANGEL', 'exchange': 'NFO', 'symbol': 'X', 'side': 'BUY', 'qty': 75}
    with ThreadPoolExecutor(16) as ex:
        results = list(ex.map(lambda _: l.create_pending_order(dict(o)), range(32)))
    assert results.count(True) == 1
    assert l.transition('RACE', 'SUBMITTED', broker_order_id='B1')[0]
    assert l.transition('RACE', 'FILLED')[0] and not l.transition('RACE', 'OPEN')[0]
    a = PersistentAuditChain(l, key='k')
    a.append('X', 'Y', {'n': 1.25})
    assert a.verify()['ok'] and l.schema_drift() == []
    assert l.last_reconciliation('ANGEL') is None
    l.record_reconciliation('ANGEL', True, [])
    assert l.last_reconciliation('ANGEL')['age_sec'] < 60


def test_postgres_old_v301_schema_is_detected_not_silently_used(pg):
    eng = create_engine(pg)
    with eng.begin() as c:  # the v3.0.1 orders table, which create_all() will not alter
        c.execute(text('CREATE TABLE orders (client_order_id VARCHAR PRIMARY KEY, broker_order_id VARCHAR, broker VARCHAR, '
                       'exchange VARCHAR, symbol VARCHAR, side VARCHAR, qty INTEGER, filled_qty INTEGER, status VARCHAR, '
                       'avg_price FLOAT, raw TEXT, created_at TIMESTAMPTZ, updated_at TIMESTAMPTZ)'))
    drift = Ledger(pg).schema_drift()
    assert 'orders.tag' in drift and 'orders.trading_date' in drift


def test_redis_streams_durable_publish():
    if not REDIS:
        pytest.skip('IORT_TEST_REDIS_URL not set')
    with override(require_durable_event_bus=True, redis_url=REDIS):
        async def go():
            bus = EventBus()
            assert await bus.connect() is True
            before = await bus.redis.xlen('iort:events')
            await bus.publish({'type': 'TEST', 'n': 1})
            after = await bus.redis.xlen('iort:events')
            await bus.close()
            return after - before
        assert asyncio.run(go()) == 1
