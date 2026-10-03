import asyncio,uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI,WebSocket,Header
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from .config import settings
from .models import Tick,OrderRequest
from .market import MarketDataGateway
from .ledger import Ledger
from .brokers import BrokerRegistry
from .watchdog import Watchdog,KillSwitch
from .reconcile import Reconciler
from .option_chain import OptionChain
from .engine import DecisionPipeline,EngineContext
from .instruments import InstrumentMaster
from .routing import SmartOrderRouter
from .eventbus import EventBus
from .stream_manager import StreamManager
from .readiness import ProductionReadiness
from .order_monitor import OrderMonitor
from .version import VERSION
from .live_risk import snapshot_from_positions
from .operator_auth import require_live_operator
from .audit_chain import PersistentAuditChain
from .dashboard import build_dashboard_state
feed=MarketDataGateway();ledger=Ledger();brokers=BrokerRegistry();audit_chain=PersistentAuditChain(ledger);watchdog=Watchdog();kill=KillSwitch();reconciler=Reconciler();chain=OptionChain();pipeline=DecisionPipeline();instruments=InstrumentMaster();router=SmartOrderRouter(brokers);bus=EventBus();stream_manager=None;order_monitor=None;readiness=None;ws_clients=set()
async def handle_tick(t):
    watchdog.beat();r=feed.ingest(t)
    if r['accepted']:
        chain.update(t);event={'type':'MARKET_TICK','tick':t.model_dump()};await emit_ui_event(event)
    else: ledger.event('MARKET_TICK_REJECTED',{'tick':t.model_dump(),'reason':r.get('reason')})
    return r
async def emit_ui_event(event):
    # UI delivery is non-authoritative telemetry. It must never change an order result.
    try:
        await bus.publish(event)
    except Exception as exc:
        ledger.event(event.get('type','EVENT'), event)
        ledger.event('UI_EVENT_BUS_ERROR', {'type': event.get('type'), 'error': str(exc)})
    for q in list(ws_clients):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            try:
                q.get_nowait()
                q.put_nowait({'type':'UI_EVENT_DROPPED','reason':'CLIENT_QUEUE_FULL'})
                q.put_nowait(event)
            except Exception:
                pass

async def audit_bus():
    while True:
        e=await bus.next();
        if e.get('type') not in ('MARKET_TICK',):ledger.event(e.get('type','EVENT'),e)
@asynccontextmanager
async def lifespan(app):
    global stream_manager,order_monitor,readiness
    async def stream_event(e):
        await emit_ui_event(e)
    await bus.connect()
    try:
        import json as _json
        urls=_json.loads(settings.instrument_master_urls_json or '{}')
        for source,url in urls.items():
            if url:
                try: await instruments.load_url(url,source)
                except Exception as exc: ledger.event('INSTRUMENT_MASTER_LOAD_ERROR',{'source':source,'error':str(exc)})
    except Exception as exc:
        ledger.event('INSTRUMENT_MASTER_CONFIG_ERROR',{'error':str(exc)})
    persisted_kill=ledger.latest_kill()
    if persisted_kill:
        kill.trigger(str(persisted_kill.get('reason') or 'PERSISTED_KILL_SWITCH'))
    stream_manager=StreamManager(handle_tick,stream_event,subscriptions=None)
    readiness=ProductionReadiness(feed,brokers,watchdog,kill,instruments,stream_manager,ledger,bus)
    await stream_manager.start();order_monitor=OrderMonitor(brokers,ledger);tasks=[asyncio.create_task(order_monitor.run(),name='order-monitor'),asyncio.create_task(audit_bus(),name='event-audit')]
    yield
    order_monitor.stop=True
    for t in tasks:t.cancel()
    await asyncio.gather(*tasks,return_exceptions=True);await stream_manager.stop()
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Cache-Control']='no-store'
        return response
app=FastAPI(title='Institutional Options Risk Terminal',version=VERSION,lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)
@app.get('/readyz')
async def readyz():
    r=await readiness.check() if readiness else {'ready':False,'reasons':['STARTING']}
    return r
@app.get('/health')
async def health():
    return {'status':'OK','market':'LIVE' if feed.live() else 'DATA_UNAVAILABLE','kill_switch':kill.triggered,'watchdog':watchdog.healthy(),
            'live_trading':settings.live_trading,'auto_trading':settings.auto_trading_enabled,
            'durable_event_bus':bool(getattr(bus,'redis_ok',False)),'brokers':await brokers.health(),'ledger':ledger.snapshot()}

@app.get('/dashboard/state')
async def dashboard_state():
    state=build_dashboard_state(feed=feed,brokers=brokers,ledger=ledger,watchdog=watchdog,kill=kill,bus=bus,
                                stream_manager=stream_manager,order_monitor=order_monitor,readiness=readiness,
                                chain=chain,instruments=instruments,settings=settings)
    state['readiness']=await readiness.check() if readiness else {'ready':False,'reasons':['STARTING']}
    return state

@app.get('/dashboard/orders')
def dashboard_orders(broker: str|None=None):
    return {'orders':ledger.orders(broker)}

@app.get('/dashboard/audit')
def dashboard_audit(limit:int=100):
    rows=ledger.audit_records(); return {'records':rows[-max(1,min(limit,1000)):], 'count':len(rows)}

@app.post('/market/tick')
async def tick(t:Tick, x_iort_internal: str|None=Header(default=None)):
    # External tick injection is forbidden in live mode. Production ticks must
    # originate from authenticated broker stream workers, not an HTTP caller.
    if settings.live_trading:
        return {'status':'BLOCKED','reason':'EXTERNAL_TICK_INGEST_DISABLED_IN_LIVE'}
    return await handle_tick(t)
@app.get('/option-chain/{exchange}/{underlying}/{expiry}')
def oc(exchange,underlying,expiry):return {'summary':chain.summary(exchange,underlying,expiry),'rows':chain.snapshot(exchange,underlying,expiry)}
def _decision(o,portfolio,approval_token_valid=False):
    tok=str(o.instrument_token or '');cur=feed.last.get(tok);prev=feed.prev.get(tok)
    if not cur:return {'approved':False,'agents':[],'proposal':{'action':'NO_TRADE','evidence':{'reason':'INSTRUMENT_DATA_UNAVAILABLE'}},'approval':{'approved':False,'reasons':['INSTRUMENT_DATA_UNAVAILABLE']},'risk':{'approved':False,'reasons':['INSTRUMENT_DATA_UNAVAILABLE']}}
    pxchg=((cur.ltp-prev.ltp)/prev.ltp*100) if prev and prev.ltp else 0.0
    spread_bps=((cur.ask-cur.bid)/cur.ltp*10000) if cur.ltp and cur.bid>0 and cur.ask>0 else 999999.0
    m={'live':feed.live(o.broker) and feed.live_instrument(tok),'symbol':o.symbol,'order':o,'liquid':spread_bps<=settings.max_slippage_bps,
       'flow':feed.flow(tok),'price_change_pct':pxchg,'iv_change_pct':0,'spread_bps':spread_bps,
       'broker_reconciled':bool(portfolio.get('broker_reconciled')),'net_exposure':portfolio.get('net_exposure',0)}
    ctx=EngineContext(market=m,portfolio=portfolio,broker_state={'reconciled':bool(portfolio.get('broker_reconciled'))},mode=o.mode.value)
    return pipeline.evaluate(ctx,approval_token_valid)

@app.post('/decision')
def decision(o:OrderRequest,approval_token:str|None=None):
    return {'status':'READINESS_REQUIRED','message':'Use /orders for broker-authoritative live evidence; /decision does not authorize live execution.'}
@app.post('/orders')
async def orders(o:OrderRequest, x_iort_operator_token: str|None=Header(default=None)):
    operator_valid=require_live_operator(x_iort_operator_token)
    if settings.live_trading and not operator_valid:
        return {'status':'BLOCKED','reason':'OPERATOR_AUTH_REQUIRED'}
    if kill.triggered:return {'status':'BLOCKED','reason':'KILL_SWITCH'}
    if not settings.live_trading:return {'status':'BLOCKED','reason':'LIVE_TRADING_DISABLED'}
    if o.mode.value=='AUTO' and not settings.auto_trading_enabled:
        return {'status':'BLOCKED','reason':'AUTO_TRADING_DISABLED'}
    if o.side not in ('BUY','SELL'):return {'status':'BLOCKED','reason':'INVALID_SIDE'}
    if o.client_order_id:
        existing=next((x for x in ledger.orders() if x.get('client_order_id')==o.client_order_id),None)
        if existing:return {'status':'IDEMPOTENT_REPLAY','order':existing}
    selected_broker,route_meta=await router.select(o.broker,feed) if settings.smart_routing_enabled else (o.broker.upper(),{'mode':'DISABLED'})
    if not selected_broker:return {'status':'BLOCKED','reason':'NO_HEALTHY_ROUTE','routing':route_meta}
    if selected_broker!=o.broker.upper():
        o=o.model_copy(update={'broker':selected_broker})
    if o.qty<=0 or o.qty>settings.max_position_qty:return {'status':'BLOCKED','reason':'POSITION_LIMIT'}
    if o.order_type!='MARKET' and (o.price is None or o.price<=0):return {'status':'BLOCKED','reason':'LIMIT_PRICE_REQUIRED'}
    estimated_px=o.price
    if estimated_px is None:
        cur=feed.last.get(str(o.instrument_token or ''))
        estimated_px=cur.ltp if cur else None
    if estimated_px is None or estimated_px<=0:
        return {'status':'BLOCKED','reason':'MARKET_PRICE_EVIDENCE_UNAVAILABLE'}
    if estimated_px*o.qty>settings.max_order_value:
        return {'status':'BLOCKED','reason':'ORDER_VALUE_LIMIT','estimated_value':estimated_px*o.qty}
    cur_for_slippage=feed.last.get(str(o.instrument_token or ''))
    if cur_for_slippage and cur_for_slippage.ltp>0 and cur_for_slippage.bid>0 and cur_for_slippage.ask>0:
        spread_bps=(cur_for_slippage.ask-cur_for_slippage.bid)/cur_for_slippage.ltp*10000
        if spread_bps>settings.max_slippage_bps:
            return {'status':'BLOCKED','reason':'SLIPPAGE_LIMIT','spread_bps':spread_bps}
    if not o.instrument_token:return {'status':'BLOCKED','reason':'INSTRUMENT_TOKEN_REQUIRED'}
    if settings.require_instrument_master and not instruments.find(token=o.instrument_token):
        return {'status':'BLOCKED','reason':'INSTRUMENT_MASTER_TOKEN_NOT_FOUND'}
    gate=await readiness.check(o.broker,o.instrument_token)
    if not gate['ready']: return {'status':'BLOCKED','reason':'PRODUCTION_READINESS_FAILED','readiness':gate}
    try:
        broker=brokers.get(o.broker)
        remote_positions=await broker.positions()
        remote_margin=await broker.margin()
        snap=snapshot_from_positions(o.broker,remote_positions,remote_margin, {str(k):v.ltp for k,v in feed.last.items()})
        recon=await reconciler.reconcile(broker,ledger)
        if not recon.get('ok'):return {'status':'BLOCKED','reason':'BROKER_RECONCILIATION_REQUIRED','reconciliation':recon}
        if settings.require_margin_evidence and snap.margin_pct is None:
            return {'status':'BLOCKED','reason':'MARGIN_EVIDENCE_UNAVAILABLE'}
        if snap.net_pnl<=-settings.portfolio_hard_sl:
            kill.trigger('HARD_PORTFOLIO_STOP');ledger.event('KILL_SWITCH',{'reason':'HARD_PORTFOLIO_STOP'});audit_chain.append('KILL_SWITCH','RISK_ENGINE',{'reason':'HARD_PORTFOLIO_STOP'})
            return {'status':'BLOCKED','reason':'HARD_PORTFOLIO_STOP','net_pnl':snap.net_pnl}
        if snap.net_pnl<=-settings.portfolio_soft_sl:return {'status':'BLOCKED','reason':'SOFT_PORTFOLIO_STOP','net_pnl':snap.net_pnl}
        if snap.daily_pnl is None and settings.max_daily_loss > 0:
            return {'status':'BLOCKED','reason':'DAILY_PNL_EVIDENCE_UNAVAILABLE'}
        if snap.daily_pnl is not None and snap.daily_pnl<=-settings.max_daily_loss:
            return {'status':'BLOCKED','reason':'DAILY_LOSS_LIMIT','daily_pnl':snap.daily_pnl}
        if snap.exposure>settings.max_net_exposure:return {'status':'BLOCKED','reason':'NET_EXPOSURE_LIMIT','exposure':snap.exposure}
        portfolio={'net_pnl':snap.net_pnl,'daily_pnl':snap.daily_pnl if snap.daily_pnl is not None else 0,'margin_pct':snap.margin_pct if snap.margin_pct is not None else 999999,
                   'slippage_bps':0,'net_exposure':snap.exposure,'broker_reconciled':True,'risk_blocked':False}
        if settings.require_scenario_risk:
            rows=[]
            for p in (remote_positions.get('data',[]) if isinstance(remote_positions,dict) else remote_positions or []):
                if not isinstance(p,dict): continue
                rows.append({'qty':p.get('quantity',p.get('net_quantity',0)),'lot_size':p.get('lot_size',1),'spot':p.get('spot',p.get('last_price',0)),
                             'delta':p.get('delta',0),'gamma':p.get('gamma',0),'vega':p.get('vega',0),'theta':p.get('theta',0)})
            if not rows:
                return {'status':'BLOCKED','reason':'SCENARIO_RISK_EVIDENCE_UNAVAILABLE'}
            scen=scenario_risk.run(rows)
            scenario_gate=pretrade_portfolio.assess({'delta_impact':0,'vega_impact':0},portfolio,scen,{'max_scenario_loss':settings.max_scenario_loss,'max_abs_delta':settings.max_abs_delta,'max_vega':settings.max_abs_vega})
            if not scenario_gate['approved']: return {'status':'BLOCKED','reason':'SCENARIO_RISK_LIMIT','details':scenario_gate}
        d=_decision(o,portfolio,operator_valid)
        if not d['approved']:return {'status':'BLOCKED','decision':d}
        client_id=o.client_order_id or str(uuid.uuid4());req=o.model_dump();req['client_order_id']=client_id
        result=await router.route(req)
        status=result.get('status','UNKNOWN')
        ledger.upsert_order({'client_order_id':client_id,'broker':o.broker,'exchange':o.exchange,'symbol':o.symbol,'side':o.side,'qty':o.qty,
                             'status':status,'broker_order_id':result.get('broker_order_id'),'filled_qty':0,'avg_price':0})
        payload={'client_order_id':client_id,'result':result,'portfolio':portfolio,'decision':d,'routing':route_meta}
        ledger.event('ORDER_SUBMISSION',payload); audit_chain.append('ORDER_SUBMISSION', 'OPERATOR' if operator_valid else 'SYSTEM', {'client_order_id':client_id,'broker':o.broker,'status':status})
        await emit_ui_event({'type':'ORDER_SUBMISSION','payload':payload})
        return {'client_order_id':client_id,**result}
    except Exception as e:
        # LIVE_RISK_SNAPSHOT_FAILED is intentionally fail-closed; no order is submitted.
        ledger.event('ORDER_GATE_ERROR',{'broker':o.broker,'error':str(e)})
        return {'status':'BLOCKED','reason':'LIVE_RISK_EVIDENCE_FAILED','error':str(e)}

@app.post('/kill')
async def ks(reason='MANUAL_PANIC', x_iort_operator_token: str|None=Header(default=None)):
    if settings.live_trading and not require_live_operator(x_iort_operator_token):
        return {'status':'BLOCKED','reason':'OPERATOR_AUTH_REQUIRED'}
    kill.trigger(reason);payload={'reason':reason};ledger.event('KILL_SWITCH',payload);audit_chain.append('KILL_SWITCH','OPERATOR',payload);await emit_ui_event({'type':'KILL_SWITCH','payload':payload});return {'status':'TRIGGERED','reason':reason}

@app.post('/emergency-stop/{broker}')
async def emergency_stop(broker:str, flatten:bool=False, x_iort_operator_token: str|None=Header(default=None)):
    if not require_live_operator(x_iort_operator_token):
        return {'status':'BLOCKED','reason':'OPERATOR_AUTH_REQUIRED'}
    kill.trigger('EMERGENCY_STOP')
    b=brokers.get(broker.upper())
    result={'broker':broker.upper(),'kill_switch':True}
    try:
        result['cancel']=await b.cancel_all()
        if flatten:
            if not settings.emergency_flatten_enabled:return {'status':'BLOCKED','reason':'EMERGENCY_FLATTEN_DISABLED'}
            result['flatten']='NOT_AUTOMATED_IN_THIS_BUILD'
        ledger.event('EMERGENCY_STOP',result)
        return {'status':'TRIGGERED',**result}
    except Exception as e:
        ledger.event('EMERGENCY_STOP_ERROR',{'broker':broker,'error':str(e)})
        return {'status':'TRIGGERED_WITH_ERRORS','broker':broker.upper(),'error':str(e)}
@app.post('/reconcile/{broker}')
async def reconcile(broker:str, x_iort_operator_token: str|None=Header(default=None)):
    if settings.live_trading and not require_live_operator(x_iort_operator_token):
        return {'status':'BLOCKED','reason':'OPERATOR_AUTH_REQUIRED'}
    result=await reconciler.reconcile(brokers.get(broker),ledger)
    await emit_ui_event({'type':'RECONCILIATION','broker':broker.upper(),'payload':result})
    return result

@app.websocket('/ws/events')
async def ws(w:WebSocket):
    await w.accept();q=asyncio.Queue(maxsize=256);ws_clients.add(q)
    try:
        await w.send_json({'type':'STATE','market':'LIVE' if feed.live() else 'DATA_UNAVAILABLE','watchdog':watchdog.healthy(),'kill_switch':kill.triggered})
        while True:
            try:event=await asyncio.wait_for(q.get(),timeout=2);await w.send_json(event)
            except asyncio.TimeoutError: await w.send_json({'type':'HEARTBEAT','market':'LIVE' if feed.live() else 'DATA_UNAVAILABLE','watchdog':watchdog.healthy(),'kill_switch':kill.triggered})
    except Exception: pass
    finally: ws_clients.discard(q)

@app.get('/ops/metrics')
def ops_metrics(): return {'version':VERSION,'market_live':feed.live(),'rejected_ticks':len(feed.rejected),'ledger':ledger.snapshot(),'watchdog':watchdog.healthy(),'kill_switch':kill.triggered,'stream_workers':[w.broker for w in (stream_manager.workers if stream_manager else [])]}
@app.get('/')
def dashboard():return FileResponse('/app/frontend/index.html')

# Institutional benchmark analytics (v2.2)
from .benchmark import VolSurfaceEngine,ScenarioRiskEngine,PortfolioGreeks,PreTradePortfolioRisk,SelfTradePrevention,AutoHedger,AlgoOrderPlanner,ExecutionTCA,HAReadiness
vol_surface=VolSurfaceEngine(); scenario_risk=ScenarioRiskEngine(); portfolio_greeks=PortfolioGreeks(); pretrade_portfolio=PreTradePortfolioRisk(); stp=SelfTradePrevention(); autohedger=AutoHedger(); algo_planner=AlgoOrderPlanner(); tca=ExecutionTCA(); ha=HAReadiness()
@app.post('/analytics/vol-surface')
def analytics_vol_surface(payload:dict):
    from .benchmark import VolPoint
    pts=[VolPoint(float(x['strike']),float(x['iv']),float(x.get('weight',1))) for x in payload.get('points',[])]
    return vol_surface.fit_smile(float(payload.get('spot',0)),pts)
@app.post('/risk/scenarios')
def risk_scenarios(payload:dict):return scenario_risk.run(payload.get('positions',[]),tuple(payload.get('spot_shocks',[-.05,0,.05])),tuple(payload.get('vol_shocks',[-.10,0,.10])),float(payload.get('days',1)))
@app.post('/risk/portfolio-greeks')
def risk_portfolio_greeks(payload:dict):return portfolio_greeks.aggregate(payload.get('positions',[]))
@app.post('/risk/pretrade-portfolio')
def risk_pretrade(payload:dict):return pretrade_portfolio.assess(payload.get('order',{}),payload.get('portfolio',{}),payload.get('scenarios',{}),payload.get('limits',{}))
@app.post('/risk/self-trade-check')
def risk_self_trade(payload:dict):return stp.check(payload.get('order',{}),payload.get('working_orders',[]))
@app.post('/hedge/delta')
def hedge_delta(payload:dict):return autohedger.delta_hedge(float(payload.get('net_delta',0)),float(payload.get('hedge_delta',0)),int(payload.get('lot_size',1)))
@app.post('/algo/iceberg')
def algo_iceberg(payload:dict):return algo_planner.iceberg(int(payload.get('qty',0)),int(payload.get('disclosed',0)))
@app.post('/algo/twap')
def algo_twap(payload:dict):return algo_planner.twap(int(payload.get('qty',0)),int(payload.get('slices',0)),int(payload.get('start_ms',0)),int(payload.get('end_ms',0)))
@app.post('/analytics/tca')
def analytics_tca(payload:dict):return tca.summarize(payload.get('fills',[]),float(payload.get('arrival_price',0)),str(payload.get('side','BUY')))
@app.post('/ops/ha-readiness')
def ops_ha(payload:dict):return ha.assess(payload)
from .enterprise_controls import RBAC,ComplianceGuard,TamperEvidentAudit,HealthBudget,DRRunbook
rbac=RBAC(); compliance=ComplianceGuard(); tamper_audit=TamperEvidentAudit(); health_budget=HealthBudget(); dr_runbook=DRRunbook()
@app.get('/enterprise/readiness')
def enterprise_readiness():
    return health_budget.assess({'database':ledger.snapshot() is not None,'event_bus':bus is not None,'stream_manager':stream_manager is not None,'order_monitor':order_monitor is not None,'broker_registry':brokers is not None})
@app.post('/enterprise/compliance-check')
def enterprise_compliance(payload:dict, x_iort_operator_token: str|None=Header(default=None)):
    if settings.live_trading and not require_live_operator(x_iort_operator_token):
        return {'approved':False,'reasons':['OPERATOR_AUTH_REQUIRED']}
    return compliance.validate(payload.get('order',{}),role=payload.get('role','TRADER'),kill=kill.triggered,live=settings.live_trading)
@app.post('/enterprise/dr-failover')
def enterprise_dr(payload:dict, x_iort_operator_token: str|None=Header(default=None)):
    if settings.live_trading and not require_live_operator(x_iort_operator_token):
        return {'status':'BLOCKED','reason':'OPERATOR_AUTH_REQUIRED'}
    return dr_runbook.failover(bool(payload.get('primary_ok')),bool(payload.get('secondary_ok')))
@app.post('/enterprise/audit')
def enterprise_audit(payload:dict, x_iort_operator_token: str|None=Header(default=None)):
    if settings.live_trading and not require_live_operator(x_iort_operator_token):
        return {'status':'BLOCKED','reason':'OPERATOR_AUTH_REQUIRED'}
    return {'hash':audit_chain.append(str(payload.get('action','UNKNOWN')),str(payload.get('actor','SYSTEM')),payload.get('payload',{}))}
@app.get('/enterprise/audit/verify')
def enterprise_audit_verify(): return audit_chain.verify()
