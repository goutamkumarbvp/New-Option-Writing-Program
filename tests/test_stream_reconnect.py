"""Feed reconnect state machine: immediate retry after a healthy drop, backoff reset, manual
reconnect and auto-reconnect toggle, login-halt pause, events, and the operator API."""
import asyncio
import time
from types import SimpleNamespace

import pytest
from conftest import TOKEN, override
from starlette.testclient import TestClient

from app.broker_streams import KotakStreamWorker, StreamError, StreamWorker, reconnect_delay
from app.main import create_app
from app.stream_manager import StreamManager


class ScriptedWorker(StreamWorker):
    """run_once follows a script: 'ok' connects and waits until kicked, 'data' connects, delivers
    one item and drops, 'fail' raises before connecting, 'drop' connects and drops, 'halt' raises a
    login halt, 'idle' returns without connecting."""

    def __init__(self, script, **kw):
        self.events = []

        async def on_event(e):
            self.events.append(e)
        super().__init__('KOTAK', None, on_event)
        self.script = list(script)
        self.waits = []
        self.connects = 0

    async def _wait(self, delay):
        self.waits.append(delay)
        if not self.script:
            self.stop = True
        if delay is None or delay > 0:
            await asyncio.sleep(0)  # yield; tests drive time by the script, not the clock
        self._wake.clear()

    async def run_once(self):
        if not self.script:
            self.stop = True
            return
        step = self.script.pop(0)
        if step == 'fail':
            raise ConnectionError('refused')
        if step == 'halt':
            raise RuntimeError('KOTAK_LOGIN_HALTED:2_REJECTIONS')
        if step == 'idle':
            return
        self.connects += 1
        await self._connected()
        if step == 'data':
            await self._on_data()
            raise StreamError('SERVER_CLOSED')
        if step == 'drop':
            raise StreamError('SERVER_CLOSED')
        await asyncio.sleep(3600)  # 'ok': stay connected until cancelled


def run(w):
    asyncio.run(asyncio.wait_for(w.run_forever(), 5))


def types(w):
    return [e['type'] for e in w.events]


def test_delay_schedule():
    assert [reconnect_delay(n, 30) for n in range(8)] == [0, 1, 2, 5, 10, 20, 30, 30]
    assert reconnect_delay(5, 8) == 8


def test_healthy_drop_retries_immediately_and_failures_back_off():
    w = ScriptedWorker(['data', 'fail', 'fail', 'fail', 'data'])
    run(w)
    # data -> drop: attempt 0 (immediate); three failures: 1, 2, 5 s; data again resets to 0
    assert w.waits[:5] == [0.0, 1.0, 2.0, 5.0, 0.0]
    assert types(w)[:3] == ['STREAM_CONNECTED', 'STREAM_DISCONNECTED', 'STREAM_ERROR']
    rec = [e for e in w.events if e['type'] == 'STREAM_CONNECTED']
    assert [e['recovered'] for e in rec] == [False, True] and w.reconnects == 1 and w.failures == 5
    assert w.state == 'STOPPED' and not w.running


def test_backoff_resets_after_a_connection_stays_up(settings_override):
    settings_override(stream_healthy_reset_sec=0)  # up "long enough" at once, even without data
    w = ScriptedWorker(['fail', 'fail', 'drop', 'drop'])
    run(w)
    assert w.waits[:4] == [1.0, 2.0, 0.0, 0.0]


def test_drop_without_data_or_uptime_keeps_backing_off(settings_override):
    settings_override(stream_healthy_reset_sec=3600)
    w = ScriptedWorker(['drop', 'drop', 'drop'])
    run(w)
    assert w.waits[:3] == [1.0, 2.0, 5.0]


def test_idle_return_is_not_a_failure():
    w = ScriptedWorker(['idle', 'idle', 'data'])
    run(w)
    assert w.failures == 1 and w.waits[0] == 0.0


def test_auto_reconnect_off_pauses_until_reconnect_requested(settings_override):
    settings_override(stream_auto_reconnect=False)
    w = ScriptedWorker(['data', 'data'])
    run(w)
    assert w.waits[0] is None  # paused: waits for an operator, not a timer
    assert any(e['type'] == 'STREAM_ERROR' and e['next_retry_in'] is None for e in w.events)


def test_login_halt_waits_for_a_reset_instead_of_spinning():
    w = ScriptedWorker(['halt'])
    run(w)
    assert w.waits == [300] and w.state == 'STOPPED'


def test_manual_reconnect_cancels_a_live_connection_and_reconnects_at_once():
    async def scenario():
        w = ScriptedWorker(['ok', 'ok'])
        task = asyncio.create_task(w.run_forever())
        for _ in range(50):
            await asyncio.sleep(0)
            if w.state == 'CONNECTED':
                break
        r = w.request_reconnect(relogin=True)
        assert r['requested'] == 'MANUAL_RECONNECT' and r['relogin']
        for _ in range(50):
            await asyncio.sleep(0)
            if w.connects == 2 and w.state == 'CONNECTED':
                break
        assert w.connects == 2 and w.waits == [0.0] and w.last_disconnect_reason == 'MANUAL_RECONNECT'
        w.stop = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert w.state == 'STOPPED'
    asyncio.run(scenario())


def test_real_wait_is_cut_short_by_a_reconnect_request():
    async def scenario():
        w = StreamWorker('KOTAK', None)
        t0 = time.monotonic()
        asyncio.get_running_loop().call_later(0.05, w.request_reconnect)
        await w._wait(10)
        assert time.monotonic() - t0 < 1
        w.request_reconnect()  # a request made before the wait starts skips the wait entirely
        t0 = time.monotonic()
        await w._wait(10)
        assert time.monotonic() - t0 < 0.5
    asyncio.run(scenario())


def test_status_fields():
    w = StreamWorker('KOTAK', None)
    s = w.status()
    for key in ('state', 'auto_reconnect', 'attempt', 'next_retry_in', 'uptime_sec', 'downtime_sec', 'downtime_total_sec',
                'last_disconnect_reason', 'last_message_age_sec', 'reconnects', 'failures', 'healthy'):
        assert key in s
    assert s['state'] == 'IDLE' and not s['healthy']


# ------------------------------------------------------------------ Kotak specifics
class FakeFeed:
    def __init__(self, msgs=None, end='stop', worker=None):
        self.msgs, self.end, self.worker = list(msgs or []), end, worker
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        self.closed = True
        return False

    async def subscribe_scrips(self, toks):
        pass

    async def subscribe_index(self, toks):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.msgs:
            return self.msgs.pop(0)
        if self.end == 'close':
            raise StopAsyncIteration
        await asyncio.sleep(3600)


def kotak_worker(feed, **kw):
    sessions = []

    async def session(force=False):
        sessions.append(force)
        return SimpleNamespace(create_websocket=lambda **o: feed)
    w = KotakStreamWorker(None, ['nse_cm|Nifty 50'], session, **kw)
    return w, sessions


def test_server_close_is_reported_as_server_closed():
    feed = FakeFeed(end='close')
    w, _ = kotak_worker(feed)
    with pytest.raises(StreamError, match='SERVER_CLOSED'):
        asyncio.run(w.run_once())
    assert feed.closed and w.ws is None


def test_replaced_session_reconnects_the_feed():
    feed = FakeFeed()
    current = [object()]
    w, _ = kotak_worker(feed, session_peek=lambda: current[0])

    async def go():
        task = asyncio.create_task(w.run_once())
        await asyncio.sleep(0.01)
        current[0] = object()  # e.g. the 08:50 pre-open login
        await asyncio.wait_for(task, 10)
    with pytest.raises(StreamError, match='SESSION_REPLACED'):
        asyncio.run(go())


def test_stall_is_detected_in_session(settings_override, monkeypatch):
    settings_override(kotak_stream_stall_sec=1)
    monkeypatch.setattr('app.broker_streams.in_session', lambda commodity=False: True)
    w, _ = kotak_worker(FakeFeed())
    with pytest.raises(StreamError, match='STREAM_STALLED'):
        asyncio.run(asyncio.wait_for(w.run_once(), 10))


def test_mcx_subscription_uses_commodity_hours():
    w, _ = kotak_worker(FakeFeed(end='close'))
    w.tokens = ['mcx_fo|2345']
    with pytest.raises(StreamError):
        asyncio.run(w.run_once())
    assert w.commodity


def test_relogin_forced_after_repeated_failures_and_on_request(settings_override):
    settings_override(stream_relogin_after_failures=3)
    w, sessions = kotak_worker(FakeFeed(end='close'))
    for attempt, relogin in ((0, False), (2, False), (3, True), (4, False)):
        w.attempt = attempt
        w._relogin = relogin
        with pytest.raises(StreamError):
            asyncio.run(w.run_once())
    assert sessions == [False, False, True, False]
    w.attempt, w._relogin = 0, True
    with pytest.raises(StreamError):
        asyncio.run(w.run_once())
    assert sessions[-1] is True and not w._relogin


def test_failed_subscribe_requests_a_reconnect():
    class BadWS:
        async def subscribe_scrips(self, toks):
            raise RuntimeError('socket gone')

        async def unsubscribe_scrips(self, toks):
            pass
    w = KotakStreamWorker(None, [], None)
    w.ws = BadWS()
    with pytest.raises(RuntimeError):
        asyncio.run(w.set_dynamic({('nse_fo', '1')}))
    assert w._wake.is_set() and w._desired == {('nse_fo', '1')}


# ------------------------------------------------------------------ operator API
def test_reconnect_and_auto_reconnect_endpoints(make_terminal):
    with override(operator_api_token=TOKEN):
        t, _ = make_terminal()
        sm = StreamManager(None, None, subscriptions={})
        w = StreamWorker('ANGEL', None)
        sm.workers = [w]
        t.stream_manager = sm
        c = TestClient(create_app(t, run_background=False))
        h = {'X-IORT-Operator-Token': TOKEN}
        assert c.post('/brokers/ANGEL/reconnect').status_code == 401
        assert c.post('/brokers/NOPE/reconnect', headers=h).status_code == 404
        r = c.post('/brokers/ANGEL/reconnect?relogin=true', headers=h).json()
        assert r['status'] == 'RECONNECTING' and r['relogin'] and w._wake.is_set()
        assert t.ledger.audit_records()[-1]['action'] == 'STREAM_RECONNECT_REQUEST'
        r = c.post('/brokers/ANGEL/auto-reconnect?enabled=false', headers=h).json()
        assert r['auto_reconnect'] is False and not w.auto_reconnect
        assert t.ledger.audit_records()[-1]['action'] == 'STREAM_AUTO_RECONNECT'
        sm.workers = []
        assert c.post('/brokers/ANGEL/reconnect', headers=h).status_code == 409
