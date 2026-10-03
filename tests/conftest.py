import asyncio
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import timedelta

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'backend'))
os.environ.setdefault('DATABASE_URL', 'sqlite:///:memory:')
os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='iort-test-'))
os.environ.setdefault('REQUIRE_DURABLE_EVENT_BUS', 'false')
for k in ('LIVE_TRADING', 'OPERATOR_API_TOKEN', 'AUDIT_HMAC_KEY', 'ALLOWED_ORIGINS'):
    os.environ.pop(k, None)

from app.brokers import BaseBroker, BrokerRegistry  # noqa: E402
from app.clock import now_ist  # noqa: E402
from app.config import settings  # noqa: E402
from app.ledger import Ledger  # noqa: E402
from app.models import Tick  # noqa: E402

TOKEN = 'operator-token-0123456789abcdef'


@contextmanager
def override(**kw):
    old = {k: getattr(settings, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(settings, k, v)
    try:
        yield settings
    finally:
        for k, v in old.items():
            object.__setattr__(settings, k, v)


@pytest.fixture
def settings_override():
    stack = []

    def apply(**kw):
        cm = override(**kw)
        cm.__enter__()
        stack.append(cm)
    yield apply
    while stack:
        stack.pop().__exit__(None, None, None)


def expiry_in(days=7):
    return (now_ist() + timedelta(days=days)).date().isoformat()


class FakeBroker(BaseBroker):
    """In-memory broker returning already-normalised data."""

    def __init__(self, name='ANGEL'):
        super().__init__()
        self.name = name
        self.pos = []
        self.native_day = False
        self.margin_value = {'used': 10000.0, 'available': 990000.0, 'pct': 1.0}
        self.remote_orders = []
        self.remote_trades = []
        self.placed = []
        self.cancelled = []
        self.place_delay = 0.0
        self.place_result = None
        self.place_exc = None
        self.margin_exc = None
        self.next_id = 1000

    def configured(self):
        return True

    async def health(self):
        return {'broker': self.name, 'status': 'LIVE'}

    async def orders(self):
        return list(self.remote_orders)

    async def trades(self):
        return list(self.remote_trades)

    async def positions(self):
        return [dict(p) for p in self.pos], self.native_day

    async def margin(self):
        if self.margin_exc:
            raise self.margin_exc
        return dict(self.margin_value)

    async def place(self, o):
        self.placed.append(o)
        if self.place_delay:
            await asyncio.sleep(self.place_delay)
        if self.place_exc:
            raise self.place_exc
        if self.place_result:
            return dict(self.place_result)
        self.next_id += 1
        bo = f'B{self.next_id}'
        self.remote_orders.append({'broker': self.name, 'broker_order_id': bo, 'status': 'OPEN', 'raw_status': 'open',
                                   'filled_qty': 0, 'qty': o['qty'], 'avg_price': 0.0, 'symbol': o['symbol'],
                                   'exchange': o['exchange'], 'side': o['side'], 'tag': o.get('tag', ''), 'token': o['instrument_token'], 'raw': {}})
        return {'status': 'SUBMITTED', 'broker': self.name, 'broker_order_id': bo}

    async def cancel(self, bo):
        self.cancelled.append(bo)
        return {'status': 'CANCEL_REQUESTED'}


def angel_master_rows(expiry):
    return [
        {'token': '111', 'symbol': 'NIFTYXX25000CE', 'name': 'NIFTY', 'expiry': expiry, 'strike': '2500000', 'lotsize': '75',
         'instrumenttype': 'OPTIDX', 'exch_seg': 'NFO', 'tick_size': '5'},
        {'token': '112', 'symbol': 'NIFTYXX25500CE', 'name': 'NIFTY', 'expiry': expiry, 'strike': '2550000', 'lotsize': '75',
         'instrumenttype': 'OPTIDX', 'exch_seg': 'NFO', 'tick_size': '5'},
        {'token': '113', 'symbol': 'NIFTYXX24500PE', 'name': 'NIFTY', 'expiry': expiry, 'strike': '2450000', 'lotsize': '75',
         'instrumenttype': 'OPTIDX', 'exch_seg': 'NFO', 'tick_size': '5'},
        {'token': '26000', 'symbol': 'NIFTY', 'name': 'NIFTY', 'expiry': '', 'strike': '-1', 'lotsize': '1',
         'instrumenttype': 'AMXIDX', 'exch_seg': 'NSE', 'tick_size': '5'},
    ]


def make_tick(broker='ANGEL', token='111', ltp=100.0, bid=None, ask=None, oi=1000, age_ms=0, seq=None):
    now = int(time.time() * 1000)
    return Tick(broker=broker, exchange='NFO', instrument_token=token, symbol=token, ltp=ltp,
                bid=bid if bid is not None else round(ltp - 0.05, 2), ask=ask if ask is not None else round(ltp + 0.05, 2),
                oi=oi, sequence=seq, exchange_ts_ms=now - age_ms, receive_ts_ms=now)


class FakeStreams:
    workers = [object()]

    def status(self):
        return []


@pytest.fixture
def make_terminal(tmp_path):
    """Build an isolated Terminal with one fake broker and an in-memory ledger."""
    from app.main import Terminal

    def build(broker=None, db_url='sqlite:///:memory:'):
        fb = broker or FakeBroker('ANGEL')
        t = Terminal(brokers=BrokerRegistry({fb.name: fb}), ledger=Ledger(db_url), data_dir=str(tmp_path / 'data'))
        t.readiness.stream_manager = FakeStreams()
        return t, fb
    return build


@pytest.fixture
def live(settings_override):
    """Live-mode settings that readiness accepts, with the ledger still on SQLite."""
    settings_override(live_trading=True, operator_api_token=TOKEN, live_production_ack='I_UNDERSTAND_REAL_ORDERS',
                      database_url='postgresql+psycopg://placeholder', require_durable_event_bus=False,
                      audit_hmac_key='test-audit-key', require_authoritative_daily_pnl=False, require_live_ltp_for_exposure=True,
                      max_orders_per_second=1000, reconciliation_max_age_sec=3600, risk_snapshot_max_age_sec=3600)
    return settings
