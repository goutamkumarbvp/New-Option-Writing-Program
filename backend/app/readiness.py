from .config import settings
class ProductionReadiness:
    def __init__(self,feed,brokers,watchdog,kill,instruments,stream_manager,ledger,bus):
        self.feed=feed;self.brokers=brokers;self.watchdog=watchdog;self.kill=kill;self.instruments=instruments
        self.stream_manager=stream_manager;self.ledger=ledger;self.bus=bus
    async def check(self,target_broker=None,instrument_token=None):
        broker_states=await self.brokers.health(); by={x.get('broker'):x for x in broker_states}; reasons=[]
        if settings.live_trading and settings.live_production_ack!='I_UNDERSTAND_REAL_ORDERS': reasons.append('LIVE_PRODUCTION_ACK_REQUIRED')
        if settings.live_trading and settings.require_operator_auth and not settings.operator_api_token: reasons.append('OPERATOR_API_TOKEN_REQUIRED')
        if settings.live_trading and settings.database_url.startswith('sqlite'): reasons.append('POSTGRES_REQUIRED_FOR_LIVE')
        if settings.live_trading and settings.require_durable_event_bus and not self.bus.redis_ok: reasons.append('DURABLE_EVENT_BUS_NOT_READY')
        if settings.live_trading and not self.stream_manager: reasons.append('STREAM_MANAGER_NOT_STARTED')
        if settings.live_trading and self.stream_manager and not self.stream_manager.workers: reasons.append('NO_LIVE_FEED_WORKER')
        if settings.live_trading and not self.watchdog.healthy(): reasons.append('WATCHDOG_UNHEALTHY')
        if settings.live_trading and settings.require_instrument_master and not self.instruments.rows: reasons.append('INSTRUMENT_MASTER_EMPTY')
        if self.kill.triggered: reasons.append('KILL_SWITCH_ACTIVE')
        if target_broker:
            b=str(target_broker).upper()
            if by.get(b,{}).get('status')!='LIVE': reasons.append(f'TARGET_BROKER_NOT_AUTHENTICATED:{b}')
            if not self.feed.live(b): reasons.append(f'TARGET_BROKER_FEED_NOT_LIVE:{b}')
        if instrument_token and not self.feed.live_instrument(instrument_token): reasons.append('INSTRUMENT_DATA_UNAVAILABLE')
        return {'ready':not reasons,'reasons':sorted(set(reasons)),'live_trading':settings.live_trading,
                'target_broker':target_broker,'instrument_token':instrument_token,'brokers':broker_states,'ledger':self.ledger.snapshot(),
                'durable_event_bus':self.bus.redis_ok}
