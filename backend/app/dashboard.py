import time
from datetime import datetime, timezone

from .clock import trading_date
from .version import VERSION


def _running(t, name, fallback):
    """Whether a background loop is actually running, from the supervisor; the fallback applies only
    before the terminal has started its loops (tests, or the first instant of startup)."""
    h = getattr(t, 'task_health', {}).get(name)
    return bool(h['running']) if h else fallback


def _last_ticks(feed, stale_ms, n=50):
    """Newest ticks first, each flagged stale when older than DATA_STALE_MS, so the terminal can blank them."""
    now = int(time.time() * 1000)
    ticks = sorted(feed.last.values(), key=lambda v: v.receive_ts_ms or 0, reverse=True)[:n]
    return [{**v.model_dump(), 'stale': now - int(v.receive_ts_ms or 0) > stale_ms} for v in ticks]


async def build_dashboard_state(t):
    """Single authoritative dashboard payload. The original called the async
    broker health function without awaiting it and imported 'backend.app.version',
    which does not exist inside the container, so this endpoint always failed."""
    s = t.settings
    health = await t.health.get()
    snaps = {b: snap.to_dict() for b, snap in (t.risk_monitor.snapshots.items() if t.risk_monitor else [])}
    for v in snaps.values():
        v['positions'] = v['positions'][:200]
    # Today's orders, plus any order from an earlier day that may still be working at the broker.
    orders = t.ledger.orders(limit=100, trading_day=trading_date())
    seen = {o['client_order_id'] for o in orders}
    orders += [o for o in t.ledger.working_orders() if o['client_order_id'] not in seen][:50]
    filled = [o for o in orders if o['status'] in ('FILLED', 'PARTIAL') and o['avg_price'] and o['arrival_price']]
    tca = [{'client_order_id': o['client_order_id'], 'side': o['side'], 'arrival': o['arrival_price'], 'avg_fill': o['avg_price'],
            'slippage_bps': round((1 if o['side'] == 'BUY' else -1) * (o['avg_price'] - o['arrival_price']) / o['arrival_price'] * 10000, 2)}
           for o in filled]
    ledger_counts = t.ledger.snapshot()
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'version': VERSION,
        'system': {
            'market': 'LIVE' if t.feed.live() else 'DATA_UNAVAILABLE',
            'watchdog': t.watchdog.healthy(),
            'kill_switch': t.kill.triggered,
            'kill': t.kill.state(),
            'live_trading': s.live_trading,
            'auto_trading': s.auto_trading_enabled,
            'durable_event_bus': bool(t.bus.redis_ok),
            'event_bus': t.bus.status() if hasattr(t.bus, 'status') else None,
            'tasks': getattr(t, 'task_health', {}),
            'event_queue_depth': t.bus.q.qsize(),
            'workers': t.stream_manager.status() if t.stream_manager else [],
            'order_monitor': {'running': _running(t, 'order-monitor', bool(t.order_monitor and not t.order_monitor.stop)),
                              'errors': t.order_monitor.errors if t.order_monitor else {}},
            'risk_monitor': {'running': _running(t, 'risk-monitor', t.risk_monitor is not None),
                             'last_eval': t.risk_monitor.last_eval if t.risk_monitor else None},
        },
        'brokers': health,
        'ledger': ledger_counts,
        'orders': orders,
        'exceptions': t.ledger.ambiguous_orders(),
        'market': {'last_ticks': _last_ticks(t.feed, s.data_stale_ms), 'stats': {**t.feed.stats(), 'data_stale_ms': s.data_stale_ms},
                   'rejected_ticks': sum(t.feed.reject_counts.values())},
        'option_chain': {'chains': t.chain.chains()[:100]},
        'instruments': t.instruments.summary(),
        'instrument_loader': t.instrument_loader.status() if getattr(t, 'instrument_loader', None) else None,
        'chain_subscriber': t.chain_subscriber.state if getattr(t, 'chain_subscriber', None) else None,
        'static_instrument_refresh': t.static_refresher.status() if getattr(t, 'static_refresher', None) else None,
        'risk': {
            'limits': {'hard_sl': s.portfolio_hard_sl, 'soft_sl': s.portfolio_soft_sl, 'max_daily_loss': s.max_daily_loss,
                       'max_exposure': s.max_net_exposure, 'max_short_option_notional': s.max_short_option_notional,
                       'max_order_value': s.max_order_value, 'max_position_qty': s.max_position_qty,
                       'max_margin_pct': s.max_margin_utilization_pct},
            'aggregate': t.risk_monitor.aggregate() if t.risk_monitor else None,
            'snapshot_max_age_sec': s.risk_snapshot_max_age_sec,
            'snapshots': snaps,
        },
        'controls': {
            'operator_auth': s.require_operator_auth, 'market_orders_allowed': s.allow_market_orders,
            'price_collar_bps': s.max_price_deviation_bps, 'naked_short_options_blocked': not s.allow_naked_short_options,
            'scenario_gate': s.require_scenario_risk, 'ai_gate_mode': s.ai_gate_mode,
            'self_trade_prevention': True, 'write_ahead_order_log': True, 'per_broker_pretrade_lock': True,
            'order_rate_limit_per_sec': s.max_orders_per_second, 'auto_flatten_on_hard_stop': s.auto_flatten_on_hard_stop,
            'audit_chain_keyed': bool(s.audit_hmac_key),
        },
        'execution': {'smart_routing': s.smart_routing_enabled, 'approval_required': s.require_human_approval_auto,
                      'operator_auth_required_live': True},
        'reconciliation': {b.name: t.ledger.last_reconciliation(b.name) for b in t.brokers.configured()},
        'tca': tca,
        'audit': {'records': ledger_counts['audit_records'], 'latest_hash': t.ledger.latest_audit_hash()},
        'readiness': await t.readiness.check() if t.readiness else {'ready': False, 'reasons': ['STARTING']},
    }
