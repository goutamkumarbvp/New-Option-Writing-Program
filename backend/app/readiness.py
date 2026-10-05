from .config import settings
from .netcheck import PublicIP, static_ip_reasons


class ProductionReadiness:
    def __init__(self, feed, health, watchdog, kill, instruments, stream_manager, ledger, bus, risk_monitor=None,
                 order_monitor=None):
        self.feed = feed
        self.health = health
        self.watchdog = watchdog
        self.kill = kill
        self.instruments = instruments
        self.stream_manager = stream_manager
        self.ledger = ledger
        self.bus = bus
        self.risk_monitor = risk_monitor
        self.order_monitor = order_monitor
        self.public_ip = PublicIP()
        self._schema = None

    def schema_drift(self):
        if self._schema is None:
            self._schema = self.ledger.schema_drift()
        return self._schema

    async def check(self, target_broker=None, instrument_token=None):
        states = await self.health.get()
        by = {x.get('broker'): x for x in states}
        reasons = list(settings.validation_errors())
        live = settings.live_trading
        if self.schema_drift():
            reasons.append('DATABASE_SCHEMA_MIGRATION_REQUIRED')
        if live and settings.live_production_ack != 'I_UNDERSTAND_REAL_ORDERS':
            reasons.append('LIVE_PRODUCTION_ACK_REQUIRED')
        if live and settings.require_operator_auth and not settings.operator_api_token:
            reasons.append('OPERATOR_API_TOKEN_REQUIRED')
        if live and settings.database_url.startswith('sqlite'):
            reasons.append('POSTGRES_REQUIRED_FOR_LIVE')
        if live and settings.require_durable_event_bus and not self.bus.redis_ok:
            reasons.append('DURABLE_EVENT_BUS_NOT_READY')
        if live and not self.stream_manager:
            reasons.append('STREAM_MANAGER_NOT_STARTED')
        if live and self.stream_manager and not self.stream_manager.workers:
            reasons.append('NO_LIVE_FEED_WORKER')
        if live and not self.watchdog.healthy():
            reasons.append('WATCHDOG_UNHEALTHY')
        if live and self.risk_monitor is None:
            reasons.append('RISK_MONITOR_NOT_RUNNING')
        if live and settings.audit_chain_required and not settings.audit_hmac_key:
            reasons.append('AUDIT_HMAC_KEY_REQUIRED')
        if live and settings.require_static_ip_match:
            # Kotak refuses API orders from an unregistered IP; block here with a clear reason instead.
            reasons.extend(static_ip_reasons(await self.public_ip.get() if settings.registered_static_ip.strip() else None))
        if self.kill.triggered:
            reasons.append('KILL_SWITCH_ACTIVE')
        tasks = getattr(self, 'task_health', None) or {}
        if live:
            for name in ('order-monitor', 'risk-monitor', 'reconcile'):
                h = tasks.get(name)
                if h is not None and not h.get('running'):
                    reasons.append(f'BACKGROUND_TASK_DOWN:{name}')
        if target_broker:
            b = str(target_broker).upper()
            if by.get(b, {}).get('status') != 'LIVE':
                reasons.append(f'TARGET_BROKER_NOT_AUTHENTICATED:{b}')
            if not self.feed.live(b):
                reasons.append(f'TARGET_BROKER_FEED_NOT_LIVE:{b}')
            skew = self.feed.clock_skew_ms(b)
            if skew is not None and abs(skew) > settings.max_clock_skew_ms:
                reasons.append(f'CLOCK_SKEW:{b}')
            if settings.require_instrument_master and not self.instruments.fresh_today(b):
                reasons.append(f'INSTRUMENT_MASTER_NOT_LOADED_TODAY:{b}')
            if self.order_monitor and self.order_monitor.errors.get(b):
                reasons.append(f'ORDER_MONITOR_FAILING:{b}')
            if instrument_token and not self.feed.live_instrument(b, instrument_token):
                reasons.append('INSTRUMENT_DATA_UNAVAILABLE')
        return {'ready': not reasons, 'reasons': sorted(set(reasons)), 'live_trading': live, 'target_broker': target_broker,
                'instrument_token': instrument_token, 'brokers': states, 'durable_event_bus': self.bus.redis_ok}
