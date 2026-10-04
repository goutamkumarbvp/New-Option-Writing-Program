"""Application wiring.

``Terminal`` owns every long-lived component; ``create_app`` exposes it over HTTP
and WebSocket. Tests build their own Terminal with fake brokers and an in-memory
ledger instead of patching module globals.
"""
import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import Body, FastAPI, Header, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from .audit_chain import PersistentAuditChain
from .brokers import BrokerRegistry
from .chain_subscriber import ChainSubscriber
from .config import settings
from .dashboard import build_dashboard_state
from .engine import DecisionPipeline
from .eventbus import EventBus
from .health_cache import HealthCache
from .instrument_loader import KotakInstrumentLoader
from .instruments import InstrumentMaster
from .ledger import Ledger
from .market import MarketDataGateway
from .models import OrderRequest, Tick
from .oms import OrderService
from .operator_auth import require_control_operator, require_live_operator
from .option_chain import OptionChain, resolve_spot
from .order_monitor import OrderMonitor
from .options_strategy import TEMPLATES, StrategyError, analyze, template
from .portfolio import build_portfolio
from .readiness import ProductionReadiness
from .reconcile import Reconciler
from .risk_monitor import RiskMonitor
from .routing import SmartOrderRouter
from .security import SecurityMiddleware, origin_allowed
from .stream_manager import StreamManager
from .version import VERSION
from .watchdog import KillSwitch, Watchdog


class Terminal:
    def __init__(self, brokers=None, ledger=None, data_dir=None):
        self.settings = settings
        self.ledger = ledger or Ledger()
        self.audit = PersistentAuditChain(self.ledger)
        self.brokers = brokers or BrokerRegistry()
        self.health = HealthCache(self.brokers)
        self.feed = MarketDataGateway()
        self.watchdog = Watchdog(settings.watchdog_timeout_sec)
        self.kill = KillSwitch(self.ledger, self.audit)
        self.kill.load()
        self.chain = OptionChain()
        self.instruments = InstrumentMaster(data_dir)
        self.pipeline = DecisionPipeline()
        self.router = SmartOrderRouter(self.brokers, self.health)
        self.reconciler = Reconciler()
        self.bus = EventBus()
        self.stream_manager = None
        self.order_monitor = OrderMonitor(self.brokers, self.ledger, self.emit)
        self.risk_monitor = RiskMonitor(self.brokers, self.feed, self.instruments, self.ledger, self.kill, self.emit)
        self.oms = OrderService(self)
        self.instrument_loader = KotakInstrumentLoader(self.instruments, self.brokers, self.ledger, self.emit)
        self.chain_subscriber = ChainSubscriber(self.instruments, self.feed, self.chain, lambda: self.stream_manager,
                                                self.risk_monitor, self.ledger)
        self.risk_monitor.executor = self.oms
        self.readiness = ProductionReadiness(self.feed, self.health, self.watchdog, self.kill, self.instruments, None, self.ledger,
                                             self.bus, self.risk_monitor, self.order_monitor)
        self.ws_clients = set()
        self.tasks = []
        self._bus_error_at = 0.0

    # ------------------------------------------------------------- events
    async def emit(self, event):
        """Fan an event out to Redis (durable copy) and dashboard sockets.
        UI delivery is non-authoritative telemetry: it never changes an order result."""
        try:
            await self.bus.publish(event)
        except Exception as exc:  # noqa: BLE001
            if time.time() - self._bus_error_at > 60:
                self._bus_error_at = time.time()
                self.ledger.event('UI_EVENT_BUS_ERROR', {'type': event.get('type'), 'error': str(exc)[:200]})
        for q in list(self.ws_clients):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:  # noqa: BLE001
                    pass

    async def handle_tick(self, t):
        rec = self.instruments.get(t.broker, t.instrument_token)
        if rec:
            t = t.model_copy(update={'symbol': rec['symbol'] or t.symbol, 'exchange': rec['exchange'] or t.exchange,
                                     'underlying': rec['underlying'], 'expiry': rec['expiry'], 'strike': rec['strike'],
                                     'option_type': rec['option_type']})
        r = self.feed.ingest(t)
        if r['accepted']:
            self.watchdog.beat()
            self.chain.update(t)
            await self.emit({'type': 'MARKET_TICK', 'tick': t.model_dump()})
        return r

    def chain_ladder(self, exchange, underlying, expiry, depth=None):
        """Read-only strike ladder for the terminal's Option Chain tab."""
        rows = self.chain.snapshot(exchange, underlying, expiry)
        spot = resolve_spot(self.feed, underlying, expiry, rows)
        return self.chain.ladder(exchange, underlying, expiry, spot, lookup=self.instruments.get, depth=depth)

    # ---------------------------------------------------------- lifecycle
    async def _drain_bus(self):
        while True:
            await self.bus.next()

    async def _reconcile_loop(self):
        while True:
            for b in self.brokers.configured():
                try:
                    await self.reconciler.reconcile(b, self.ledger)
                except Exception as exc:  # noqa: BLE001
                    self.ledger.event('RECONCILE_LOOP_ERROR', {'broker': b.name, 'error': str(exc)[:200]})
            await asyncio.sleep(settings.reconcile_interval_sec)

    async def start(self):
        await self.bus.connect()
        try:
            urls = json.loads(settings.instrument_master_urls_json or '{}')
        except ValueError as exc:
            urls = {}
            self.ledger.event('INSTRUMENT_MASTER_CONFIG_ERROR', {'error': str(exc)})
        for broker, url in urls.items():
            if url:
                try:
                    await self.instruments.load_url(url, broker)
                except Exception as exc:  # noqa: BLE001
                    self.ledger.event('INSTRUMENT_MASTER_LOAD_ERROR', {'broker': broker, 'error': str(exc)[:300]})
        self.stream_manager = StreamManager(self.handle_tick, self.emit, registry=self.brokers)
        self.readiness.stream_manager = self.stream_manager
        await self.stream_manager.start()
        for name, coro in (('order-monitor', self.order_monitor.run()), ('risk-monitor', self.risk_monitor.run()),
                           ('reconcile', self._reconcile_loop()), ('bus-drain', self._drain_bus()),
                           ('instrument-loader', self.instrument_loader.run()), ('chain-subscriber', self.chain_subscriber.run())):
            self.tasks.append(asyncio.create_task(coro, name=name))

    async def stop(self):
        self.order_monitor.stop = True
        self.risk_monitor.stop = True
        self.instrument_loader.stop = True
        self.chain_subscriber.stop = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.stream_manager:
            await self.stream_manager.stop()
        await self.brokers.aclose()
        await self.bus.close()


class ResolveRequest(BaseModel):
    status: Literal['REJECTED', 'CANCELLED', 'FILLED', 'EXPIRED']
    reason: str = Field(min_length=3, max_length=300)
    broker_order_id: Optional[str] = None


def _frontend():
    here = Path(__file__).resolve()
    for p in (here.parents[1] / 'frontend' / 'index.html', here.parents[2] / 'frontend' / 'index.html'):
        if p.exists():
            return p
    return None


def create_app(t: Terminal, run_background=True):
    @asynccontextmanager
    async def lifespan(app):
        if run_background:
            await t.start()
        yield
        if run_background:
            await t.stop()

    app = FastAPI(title='Institutional Options Risk Terminal', version=VERSION, lifespan=lifespan)
    app.add_middleware(SecurityMiddleware)
    app.state.terminal = t

    def denied():
        return JSONResponse({'status': 'BLOCKED', 'reason': 'OPERATOR_AUTH_REQUIRED'}, status_code=401)

    def broker_or_404(name):
        try:
            return t.brokers.get(name)
        except KeyError:
            return None

    @app.get('/readyz')
    async def readyz():
        return await t.readiness.check()

    @app.get('/health')
    async def health():
        return {'status': 'OK', 'version': VERSION, 'market': 'LIVE' if t.feed.live() else 'DATA_UNAVAILABLE',
                'kill_switch': t.kill.triggered, 'watchdog': t.watchdog.healthy(), 'live_trading': settings.live_trading,
                'auto_trading': settings.auto_trading_enabled, 'durable_event_bus': t.bus.redis_ok,
                'brokers': await t.health.get(), 'ledger': t.ledger.snapshot()}

    @app.get('/dashboard/state')
    async def dashboard_state():
        return await build_dashboard_state(t)

    @app.get('/dashboard/orders')
    def dashboard_orders(broker: Optional[str] = None, limit: int = 200):
        return {'orders': t.ledger.orders(broker, limit=max(1, min(limit, 5000)))}

    @app.get('/dashboard/audit')
    def dashboard_audit(limit: int = 100):
        rows = t.ledger.audit_records()
        return {'records': rows[-max(1, min(limit, 1000)):], 'count': len(rows)}

    @app.get('/risk/snapshot')
    def risk_snapshot():
        return {'aggregate': t.risk_monitor.aggregate(), 'last_eval': t.risk_monitor.last_eval,
                'snapshots': {b: s.to_dict() for b, s in t.risk_monitor.snapshots.items()}}

    @app.get('/risk/portfolio')
    def risk_portfolio():
        """Position Greeks, per-underlying and firm aggregates, scenario grid and payoff curves (read-only)."""
        return build_portfolio(t.risk_monitor.snapshots, t.feed, t.chain)

    @app.post('/market/tick')
    async def tick(tk: Tick):
        # Production ticks come only from authenticated broker streams.
        if settings.live_trading:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'EXTERNAL_TICK_INGEST_DISABLED_IN_LIVE'}, status_code=403)
        return await t.handle_tick(tk)

    @app.get('/option-chain')
    def option_chain_index():
        # Order policy is included so the terminal's ticket can show what the order path will refuse.
        return {'chains': t.chain.index(), 'live_trading': settings.live_trading,
                'market_orders_allowed': settings.allow_market_orders,
                'naked_short_options_blocked': not settings.allow_naked_short_options}

    @app.get('/option-chain/{exchange}/{underlying}/{expiry}')
    def option_chain(exchange: str, underlying: str, expiry: str):
        return {'summary': t.chain.summary(exchange, underlying, expiry), 'rows': t.chain.snapshot(exchange, underlying, expiry)}

    @app.get('/option-chain/{exchange}/{underlying}/{expiry}/ladder')
    def option_chain_ladder(exchange: str, underlying: str, expiry: str, depth: Optional[int] = None):
        return t.chain_ladder(exchange, underlying, expiry, max(1, min(depth, 100)) if depth else None)

    @app.get('/strategy/templates')
    def strategy_templates():
        return {'templates': [{'name': k, 'description': v} for k, v in TEMPLATES.items()]}

    @app.get('/strategy/template/{exchange}/{underlying}/{expiry}/{name}')
    def strategy_template(exchange: str, underlying: str, expiry: str, name: str, lots: int = 1, delta: float = 0.20, wing: int = 4):
        """Pick a named option-writing structure from the live chain and analyse it (read-only)."""
        ladder = t.chain_ladder(exchange, underlying, expiry)
        try:
            specs = template(name, ladder, max(1, min(lots, 100)), max(0.02, min(delta, 0.5)), max(1, min(wing, 20)))
            return {'template': name, **analyze(ladder, specs)}
        except StrategyError as exc:
            return JSONResponse({'status': 'UNAVAILABLE', 'reason': str(exc)}, status_code=422)

    @app.post('/strategy/analyze')
    def strategy_analyze(payload: dict = Body(...)):
        """Analyse legs [{side, lots, strike, option_type | token, price?}] against the live chain (read-only)."""
        ladder = t.chain_ladder(str(payload.get('exchange', '')), str(payload.get('underlying', '')), str(payload.get('expiry', '')))
        try:
            return analyze(ladder, list(payload.get('legs') or [])[:12])
        except (StrategyError, TypeError, ValueError) as exc:
            return JSONResponse({'status': 'UNAVAILABLE', 'reason': str(exc)}, status_code=422)

    @app.post('/decision')
    def decision(o: OrderRequest):
        return {'status': 'READINESS_REQUIRED', 'message': 'Use /orders; /decision does not authorize live execution.'}

    @app.post('/orders')
    async def orders(o: OrderRequest, x_iort_operator_token: Optional[str] = Header(default=None)):
        return await t.oms.submit(o, require_live_operator(x_iort_operator_token))

    @app.get('/orders/{client_order_id}')
    def get_order(client_order_id: str):
        o = t.ledger.get_order(client_order_id)
        if not o:
            return JSONResponse({'status': 'NOT_FOUND'}, status_code=404)
        return {'order': o, 'fills': t.ledger.fills_for(client_order_id)}

    @app.post('/orders/{client_order_id}/resolve')
    def resolve_order(client_order_id: str, body: ResolveRequest, x_iort_operator_token: Optional[str] = Header(default=None)):
        """Operator resolution of an ambiguous order after checking the broker terminal."""
        if not require_control_operator(x_iort_operator_token):
            return denied()
        o = t.ledger.get_order(client_order_id)
        if not o or o['status'] not in ('UNKNOWN', 'PENDING_SUBMIT'):
            return JSONResponse({'status': 'BLOCKED', 'reason': 'ORDER_NOT_AMBIGUOUS'}, status_code=409)
        applied, prev = t.ledger.transition(client_order_id, body.status, reason='OPERATOR:' + body.reason,
                                            broker_order_id=body.broker_order_id)
        t.audit.append('ORDER_RESOLVED', 'OPERATOR', {'client_order_id': client_order_id, 'from': prev, 'to': body.status,
                                                      'reason': body.reason})
        return {'status': 'RESOLVED' if applied else 'REJECTED_TRANSITION', 'from': prev, 'to': body.status}

    @app.post('/kill')
    async def kill(reason: str = 'MANUAL_PANIC', x_iort_operator_token: Optional[str] = Header(default=None)):
        if settings.live_trading and not require_live_operator(x_iort_operator_token):
            return denied()
        t.kill.trigger(reason, 'OPERATOR')
        await t.emit({'type': 'KILL_SWITCH', 'payload': t.kill.state()})
        return {'status': 'TRIGGERED', **t.kill.state()}

    @app.post('/kill/reset')
    async def kill_reset(reason: str, x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return denied()
        if len(reason.strip()) < 3:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'RESET_REASON_REQUIRED'}, status_code=400)
        t.kill.reset('OPERATOR', reason)
        await t.emit({'type': 'KILL_SWITCH_RESET', 'payload': t.kill.state()})
        return {'status': 'RESET', **t.kill.state()}

    @app.post('/emergency-stop/{broker}')
    async def emergency_stop(broker: str, flatten: bool = False, x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return denied()
        b = broker_or_404(broker)
        if b is None:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'UNKNOWN_BROKER'}, status_code=404)
        if flatten and not settings.emergency_flatten_enabled:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'EMERGENCY_FLATTEN_DISABLED'}, status_code=400)
        t.kill.trigger('EMERGENCY_STOP', 'OPERATOR')  # persisted: survives restart (original did not persist it)
        result = {'broker': b.name, 'kill_switch': True}
        try:
            result['cancel'] = await b.cancel_all()
        except Exception as exc:  # noqa: BLE001
            result['cancel'] = {'status': 'CANCEL_ALL_FAILED', 'error': str(exc)[:200]}
        if flatten:
            result['flatten'] = await t.oms.flatten_broker(b, 'OPERATOR')
        t.ledger.event('EMERGENCY_STOP', result)
        t.audit.append('EMERGENCY_STOP', 'OPERATOR', {'broker': b.name, 'flatten': flatten})
        await t.emit({'type': 'EMERGENCY_STOP', 'payload': result})
        return {'status': 'TRIGGERED', **result}

    @app.post('/flatten/{broker}')
    async def flatten(broker: str, x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return denied()
        if not settings.emergency_flatten_enabled:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'EMERGENCY_FLATTEN_DISABLED'}, status_code=400)
        b = broker_or_404(broker)
        if b is None:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'UNKNOWN_BROKER'}, status_code=404)
        return await t.oms.flatten_broker(b, 'OPERATOR')

    @app.post('/reconcile/{broker}')
    async def reconcile(broker: str, x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return denied()
        b = broker_or_404(broker)
        if b is None:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'UNKNOWN_BROKER'}, status_code=404)
        result = await t.reconciler.reconcile(b, t.ledger)
        await t.emit({'type': 'RECONCILIATION', 'broker': b.name, 'payload': result})
        return result

    @app.post('/brokers/{broker}/login-reset')
    async def broker_login_reset(broker: str, x_iort_operator_token: Optional[str] = Header(default=None)):
        """Clear a broker's login halt/backoff after the operator has fixed the credentials."""
        if not require_control_operator(x_iort_operator_token):
            return denied()
        b = broker_or_404(broker)
        if b is None:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'UNKNOWN_BROKER'}, status_code=404)
        if not hasattr(b, 'reset_login'):
            return JSONResponse({'status': 'BLOCKED', 'reason': 'LOGIN_RESET_UNSUPPORTED'}, status_code=400)
        before = b.login_state()
        after = b.reset_login()
        t.health.at = 0.0  # next health read reflects the reset instead of the cached halt
        t.ledger.event('BROKER_LOGIN_RESET', {'broker': b.name, 'before': before})
        t.audit.append('BROKER_LOGIN_RESET', 'OPERATOR', {'broker': b.name, 'was_halted': before['halted']})
        return {'status': 'RESET', 'broker': b.name, 'before': before, 'login': after}

    @app.post('/instruments/refresh/{broker}')
    async def refresh_instruments(broker: str, x_iort_operator_token: Optional[str] = Header(default=None)):
        """Fetch and load today's Kotak scrip master now, ignoring the backoff and freshness checks."""
        if not require_control_operator(x_iort_operator_token):
            return denied()
        if str(broker).upper() != t.instrument_loader.broker_name:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'AUTO_REFRESH_ONLY_FOR_KOTAK'}, status_code=400)
        if not t.instrument_loader.enabled():
            return JSONResponse({'status': 'BLOCKED', 'reason': 'KOTAK_AUTO_LOAD_DISABLED_OR_UNCONFIGURED'}, status_code=400)
        r = await t.instrument_loader.run_once(force=True)
        return JSONResponse(r, status_code=502 if r and r.get('status') == 'ERROR' else 200)

    @app.post('/instruments/load/{broker}')
    async def load_instruments(broker: str, url: str = Body(embed=True), x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return denied()
        if broker_or_404(broker) is None:
            return JSONResponse({'status': 'BLOCKED', 'reason': 'UNKNOWN_BROKER'}, status_code=404)
        return await t.instruments.load_url(url, broker)

    @app.websocket('/ws/events')
    async def ws(w: WebSocket):
        if not origin_allowed(w.headers.get('origin'), w.headers.get('host')):
            await w.close(code=1008)
            return
        await w.accept()
        q = asyncio.Queue(maxsize=256)
        t.ws_clients.add(q)
        try:
            await w.send_json({'type': 'STATE', 'market': 'LIVE' if t.feed.live() else 'DATA_UNAVAILABLE',
                               'watchdog': t.watchdog.healthy(), 'kill_switch': t.kill.triggered})
            while True:
                try:
                    event = await asyncio.wait_for(q.get(), timeout=2)
                    await w.send_json(event)
                except asyncio.TimeoutError:
                    await w.send_json({'type': 'HEARTBEAT', 'market': 'LIVE' if t.feed.live() else 'DATA_UNAVAILABLE',
                                       'watchdog': t.watchdog.healthy(), 'kill_switch': t.kill.triggered})
        except Exception:  # noqa: BLE001 - client went away
            pass
        finally:
            t.ws_clients.discard(q)

    @app.get('/ops/metrics')
    def ops_metrics():
        return {'version': VERSION, 'market_live': t.feed.live(), 'feed': t.feed.stats(), 'ledger': t.ledger.snapshot(),
                'watchdog': t.watchdog.healthy(), 'kill_switch': t.kill.triggered,
                'stream_workers': t.stream_manager.status() if t.stream_manager else []}

    @app.get('/')
    def dashboard():
        p = _frontend()
        return FileResponse(p) if p else JSONResponse({'status': 'FRONTEND_NOT_PACKAGED'}, status_code=404)

    _register_calculators(app, t)
    return app


def _register_calculators(app, t):
    """Stateless analytics calculators. They do not touch orders or broker state."""
    from .benchmark import (AlgoOrderPlanner, AutoHedger, ExecutionTCA, HAReadiness, PortfolioGreeks, PreTradePortfolioRisk,
                            ScenarioRiskEngine, SelfTradePrevention, VolPoint, VolSurfaceEngine)
    from .enterprise_controls import ComplianceGuard, DRRunbook, HealthBudget
    from .scenario import revalue
    vol, scen, greeks, pre = VolSurfaceEngine(), ScenarioRiskEngine(), PortfolioGreeks(), PreTradePortfolioRisk()
    stp, hedger, algo, tca, ha = SelfTradePrevention(), AutoHedger(), AlgoOrderPlanner(), ExecutionTCA(), HAReadiness()
    compliance, dr, budget = ComplianceGuard(), DRRunbook(), HealthBudget()

    app.post('/analytics/vol-surface')(lambda payload=Body(...): vol.fit_smile(
        float(payload.get('spot', 0)), [VolPoint(float(x['strike']), float(x['iv']), float(x.get('weight', 1))) for x in payload.get('points', [])]))
    app.post('/risk/scenarios')(lambda payload=Body(...): scen.run(
        payload.get('positions', []), tuple(payload.get('spot_shocks', [-.05, 0, .05])),
        tuple(payload.get('vol_shocks', [-.10, 0, .10])), float(payload.get('days', 1))))
    app.post('/risk/scenarios/full')(lambda payload=Body(...): revalue(payload.get('positions', []), payload.get('spots', {})))
    app.post('/risk/portfolio-greeks')(lambda payload=Body(...): greeks.aggregate(payload.get('positions', [])))
    app.post('/risk/pretrade-portfolio')(lambda payload=Body(...): pre.assess(
        payload.get('order', {}), payload.get('portfolio', {}), payload.get('scenarios', {}), payload.get('limits', {})))
    app.post('/risk/self-trade-check')(lambda payload=Body(...): stp.check(payload.get('order', {}), payload.get('working_orders', [])))
    app.post('/hedge/delta')(lambda payload=Body(...): hedger.delta_hedge(
        float(payload.get('net_delta', 0)), float(payload.get('hedge_delta', 0)), int(payload.get('lot_size', 1))))
    app.post('/algo/iceberg')(lambda payload=Body(...): algo.iceberg(int(payload.get('qty', 0)), int(payload.get('disclosed', 0))))
    app.post('/algo/twap')(lambda payload=Body(...): algo.twap(int(payload.get('qty', 0)), int(payload.get('slices', 0)),
                                                              int(payload.get('start_ms', 0)), int(payload.get('end_ms', 0))))
    app.post('/analytics/tca')(lambda payload=Body(...): tca.summarize(payload.get('fills', []), float(payload.get('arrival_price', 0)),
                                                                      str(payload.get('side', 'BUY'))))
    app.post('/ops/ha-readiness')(lambda payload=Body(...): ha.assess(payload))
    app.get('/enterprise/readiness')(lambda: budget.assess({'database': True, 'event_bus': t.bus is not None,
                                                            'stream_manager': t.stream_manager is not None,
                                                            'order_monitor': not t.order_monitor.stop, 'risk_monitor': not t.risk_monitor.stop}))

    @app.post('/enterprise/compliance-check')
    def enterprise_compliance(payload: dict = Body(...)):
        # Calculator only: role comes from the caller and is NOT an authorization decision.
        return compliance.validate(payload.get('order', {}), role=payload.get('role', 'TRADER'), kill=t.kill.triggered,
                                   live=settings.live_trading)

    @app.post('/enterprise/dr-failover')
    def enterprise_dr(payload: dict = Body(...), x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return JSONResponse({'status': 'BLOCKED', 'reason': 'OPERATOR_AUTH_REQUIRED'}, status_code=401)
        return dr.failover(bool(payload.get('primary_ok')), bool(payload.get('secondary_ok')))

    @app.post('/enterprise/audit')
    def enterprise_audit(payload: dict = Body(...), x_iort_operator_token: Optional[str] = Header(default=None)):
        if not require_control_operator(x_iort_operator_token):
            return JSONResponse({'status': 'BLOCKED', 'reason': 'OPERATOR_AUTH_REQUIRED'}, status_code=401)
        return {'hash': t.audit.append(str(payload.get('action', 'UNKNOWN')), 'OPERATOR', payload.get('payload', {}))}

    app.get('/enterprise/audit/verify')(lambda: t.audit.verify())


terminal = Terminal()
app = create_app(terminal)
