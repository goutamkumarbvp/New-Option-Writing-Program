"""Infrastructure resilience: kill switch survives a database blip, liveness endpoint, readiness sees
dead loops, ticks never wait on Redis, emit never raises."""
import asyncio

from conftest import TOKEN, override
from starlette.testclient import TestClient

from app.eventbus import EventBus
from app.main import create_app
from app.watchdog import KillSwitch


class FlakyLedger:
    def __init__(self):
        self.down, self.controls, self.events = True, {}, []

    def set_control(self, k, v):
        if self.down:
            raise ConnectionError('database down')
        self.controls[k] = v

    def event(self, topic, payload):
        if self.down:
            raise ConnectionError('database down')
        self.events.append(topic)


def test_kill_switch_holds_in_memory_and_persists_later():
    led = FlakyLedger()
    k = KillSwitch(led)
    k.trigger('HARD_PORTFOLIO_STOP', 'RISK_MONITOR')  # must not raise while the database is down
    assert k.triggered and k.state()['persist_pending'] and 'database down' in k.state()['persist_error']
    assert not k.retry_persist()
    led.down = False
    assert k.retry_persist() and led.controls['kill_switch']['active'] and led.events == ['KILL_SWITCH']
    assert not k.state()['persist_pending']


def test_livez_touches_nothing(make_terminal):
    t, _ = make_terminal()

    async def boom(*a, **k):
        raise AssertionError('livez must not call brokers')
    t.health.get = boom
    r = TestClient(create_app(t, run_background=False)).get('/livez')
    assert r.status_code == 200 and r.json()['status'] == 'UP'


def test_live_readiness_blocks_while_a_critical_loop_is_down(make_terminal, live):
    t, _ = make_terminal()
    t.task_health['order-monitor'] = {'running': False, 'restarts': 3, 'last_error': 'x', 'last_crash_at': 0}
    t.task_health['risk-monitor'] = {'running': True, 'restarts': 0, 'last_error': None, 'last_crash_at': None}
    reasons = asyncio.run(t.readiness.check())['reasons']
    assert 'BACKGROUND_TASK_DOWN:order-monitor' in reasons and 'BACKGROUND_TASK_DOWN:risk-monitor' not in reasons


def test_ticks_never_go_to_the_durable_stream(settings_override):
    settings_override(require_durable_event_bus=True, live_trading=False)

    class R:
        added = []

        async def ping(self):
            return True

        async def xadd(self, *a, **k):
            R.added.append(a[1]['payload'][:30])

        async def aclose(self):
            pass
    bus = EventBus()
    bus.redis = R()

    async def go():
        await bus.connect()
        await bus.publish({'type': 'MARKET_TICK'})
        await bus.publish({'type': 'HEARTBEAT'})
        await bus.publish({'type': 'KILL_SWITCH'})
    asyncio.run(go())
    assert len(R.added) == 1 and 'KILL_SWITCH' in R.added[0] and bus.q.qsize() == 3


def test_emit_never_raises_with_bus_and_database_down(make_terminal):
    t, _ = make_terminal()

    async def bad_publish(e):
        raise RuntimeError('bus down')
    t.bus.publish = bad_publish
    t.ledger.event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db down'))
    q = asyncio.Queue()
    t.ws_clients.add(q)
    asyncio.run(t.emit({'type': 'RISK_ALERT'}))
    assert q.get_nowait()['type'] == 'RISK_ALERT' and t.bus_errors == 1


def test_health_endpoint_does_not_wait_for_a_kotak_login(make_terminal, settings_override):
    from app.brokers import BrokerRegistry, Kotak
    settings_override(kotak_api_key='key', kotak_mobile='+919876543210', kotak_client_code='UCC', kotak_mpin='123456',
                      kotak_totp_secret='JBSWY3DPEHPK3PXP', kotak_totp='')
    k = Kotak()
    t, _ = make_terminal()
    t.brokers = BrokerRegistry({'KOTAK': k})
    t.health.registry = t.brokers
    with override(operator_api_token=TOKEN):
        h = TestClient(create_app(t, run_background=False)).get('/health').json()
    assert h['brokers'][0]['status'] == 'NOT_LOGGED_IN'  # reported at once; no login was attempted


def test_kill_switch_keeps_every_engage_and_reset_in_order():
    led = FlakyLedger()
    k = KillSwitch(led)
    k.trigger('DAILY_LOSS_LIMIT', 'RISK_MONITOR')  # database down: queued
    led.down = False
    k.reset('OPERATOR', 'reviewed')  # writes the queued engage first, then the reset
    assert led.events == ['KILL_SWITCH', 'KILL_SWITCH_RESET'] and not k.state()['persist_pending']


def test_a_failed_public_ip_lookup_blocks_only_briefly():
    import httpx
    from app.netcheck import PublicIP
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectTimeout('blip')
        return httpx.Response(200, text='203.0.113.7')
    now = [100.0]
    p = PublicIP(ttl=300, fail_ttl=15, transport=httpx.MockTransport(handler), clock=lambda: now[0])
    assert asyncio.run(p.get()) is None
    now[0] += 10
    assert asyncio.run(p.get()) is None and len(calls) == 1  # still within the short failure window
    now[0] += 6
    assert asyncio.run(p.get()) == '203.0.113.7' and len(calls) == 2


def test_login_reset_never_cuts_a_streaming_feed(make_terminal):
    from app.broker_streams import StreamWorker
    from app.stream_manager import StreamManager
    t, _ = make_terminal()
    sm = StreamManager(None, None, subscriptions={})
    w = StreamWorker('ANGEL', None)
    w.state = 'CONNECTED'
    sm.workers = [w]
    t.stream_manager = sm
    assert not w.wake_if_waiting() and not w._wake.is_set()
    w.state = 'LOGIN_HALTED'
    assert w.wake_if_waiting() and w._wake.is_set()
