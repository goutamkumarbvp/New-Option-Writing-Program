"""Self-repair: crashed background loops restart, a lost Redis reconnects, a failing tick
consumer never drops a feed, and a crashed stream worker is restarted."""
import asyncio

from app.broker_streams import StreamWorker
from app.eventbus import EventBus
from app.stream_manager import StreamManager


def test_supervisor_restarts_a_crashing_loop_and_records_it(make_terminal):
    t, _ = make_terminal()
    t.restart_base_sec = 0.0
    runs = []

    async def loop():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError(f'boom {len(runs)}')
    asyncio.run(asyncio.wait_for(t._supervise('demo-loop', loop), 5))
    h = t.task_health['demo-loop']
    assert len(runs) == 3 and h['restarts'] == 2 and h['last_error'] == 'RuntimeError: boom 2' and not h['running']
    assert [e for e in t.ledger.events() if e['topic'] == 'TASK_RESTART'][-1]['payload']['task'] == 'demo-loop'


def test_supervisor_survives_a_broken_ledger(make_terminal):
    t, _ = make_terminal()
    t.restart_base_sec = 0.0
    t.ledger.event = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('db down'))
    runs = []

    async def loop():
        runs.append(1)
        if len(runs) == 1:
            raise ValueError('x')
    asyncio.run(asyncio.wait_for(t._supervise('l', loop), 5))
    assert len(runs) == 2


class FlakyRedis:
    def __init__(self):
        self.up, self.added = False, []

    async def ping(self):
        if not self.up:
            raise ConnectionError('refused')
        return True

    async def xadd(self, *a, **k):
        if not self.up:
            raise ConnectionError('lost')
        self.added.append(a)

    async def aclose(self):
        pass


def test_event_bus_survives_redis_loss_and_reconnects(settings_override):
    settings_override(require_durable_event_bus=True, live_trading=False)
    bus, r = EventBus(), FlakyRedis()
    bus.redis = r

    async def go():
        assert await bus.connect() is False and not bus.redis_ok and 'refused' in bus.last_error
        r.up = True
        assert await bus.connect() is True and bus.redis_ok
        await bus.publish({'type': 'A'})
        r.up = False
        await bus.publish({'type': 'B'})  # Redis lost: the local copy still flows
        assert not bus.redis_ok and bus.q.qsize() == 2
        r.up = True
        bus._last_try = 0.0
        task = asyncio.create_task(bus.maintain())
        for _ in range(30):
            await asyncio.sleep(0.05)
            if bus.redis_ok:
                break
        task.cancel()
        assert bus.redis_ok and bus.reconnects == 1 and bus.status()['connected']
    asyncio.run(go())


def test_live_publish_without_redis_still_delivers_locally_but_reports(settings_override):
    settings_override(require_durable_event_bus=True, live_trading=True)
    bus = EventBus()
    bus.redis = FlakyRedis()

    async def go():
        await bus.connect()
        try:
            await bus.publish({'type': 'X'})
            raised = False
        except RuntimeError as exc:
            raised = 'DURABLE_EVENT_BUS_UNAVAILABLE' in str(exc)
        assert raised and bus.q.qsize() == 1
    asyncio.run(go())


def test_failing_tick_consumer_is_counted_not_fatal():
    async def bad(t):
        raise RuntimeError('ledger busy')
    w = StreamWorker('KOTAK', bad)
    asyncio.run(w._deliver(object()))
    assert w.handler_errors == 1 and 'ledger busy' in w.status()['last_handler_error']


def test_crashed_worker_loop_is_restarted():
    class Crashy(StreamWorker):
        calls = 0

        async def run_forever(self):
            Crashy.calls += 1
            if Crashy.calls == 1:
                raise KeyError('bug')
            self.stop = True
    events = []

    async def on_event(e):
        events.append(e)
    sm = StreamManager(None, on_event, subscriptions={})
    w = Crashy('KOTAK', None)
    asyncio.run(asyncio.wait_for(sm._keep(w), 5))
    assert Crashy.calls == 2 and events[0]['type'] == 'STREAM_WORKER_RESTART' and 'WORKER_CRASHED' in w.last_error
