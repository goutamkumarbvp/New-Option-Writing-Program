from __future__ import annotations
from datetime import datetime, timezone


def build_dashboard_state(*, feed, brokers, ledger, watchdog, kill, bus, stream_manager, order_monitor, readiness, chain, instruments, settings):
    workers = []
    if stream_manager:
        workers = [
            {'broker': getattr(w, 'broker', 'UNKNOWN'), 'healthy': getattr(w, 'healthy', None), 'running': getattr(w, 'running', None)}
            for w in getattr(stream_manager, 'workers', [])
        ]
    recent_orders = ledger.orders()
    return {
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'version': getattr(__import__('backend.app.version', fromlist=['VERSION']), 'VERSION', 'UNKNOWN'),
        'system': {
            'market': 'LIVE' if feed.live() else 'DATA_UNAVAILABLE',
            'watchdog': watchdog.healthy(),
            'kill_switch': kill.triggered,
            'live_trading': settings.live_trading,
            'auto_trading': settings.auto_trading_enabled,
            'durable_event_bus': bool(getattr(bus, 'redis_ok', False)),
            'event_queue_depth': getattr(bus.q, 'qsize', lambda: 0)(),
            'workers': workers,
            'order_monitor': bool(order_monitor and not getattr(order_monitor, 'stop', False)),
        },
        'brokers': brokers.health(),
        'ledger': ledger.snapshot(),
        'orders': recent_orders[-100:],
        'market': {
            'last_ticks': [v.model_dump() for v in list(feed.last.values())[-50:]],
            'rejected_ticks': len(feed.rejected),
        },
        'option_chain': {
            'known_underlyings': list(getattr(chain, 'data', {}).keys())[:100] if hasattr(chain, 'data') else [],
        },
        'risk': {
            'hard_sl': settings.portfolio_hard_sl,
            'soft_sl': settings.portfolio_soft_sl,
            'max_daily_loss': settings.max_daily_loss,
            'max_exposure': settings.max_net_exposure,
            'max_order_value': settings.max_order_value,
            'max_position_qty': settings.max_position_qty,
            'scenario_required': settings.require_scenario_risk,
        },
        'execution': {
            'smart_routing': settings.smart_routing_enabled,
            'approval_required': settings.require_human_approval_auto,
            'operator_auth_required_live': settings.live_trading,
        },
        'reconciliation': {
            'count': ledger.snapshot().get('reconciliations', 0),
            'instrument_master_required': settings.require_instrument_master,
        },
        'audit': {
            'records': ledger.snapshot().get('audit_records', 0),
            'latest_hash': ledger.latest_audit_hash(),
        },
        'readiness': None,
    }
