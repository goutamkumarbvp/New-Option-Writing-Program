"""Prometheus text exposition (format 0.0.4) of terminal state for monitoring and alerting.

Read-only. Every metric family is emitted once, with its HELP/TYPE header and all of its
samples together, as the format requires. Values that are unknown are omitted rather
than reported as 0.
"""
from .config import settings
from .version import VERSION


def _esc(v):
    return str(v).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n')


def _num(v):
    if isinstance(v, bool):
        return '1' if v else '0'
    return repr(float(v))


class Exposition:
    def __init__(self):
        self.families = {}

    def add(self, name, value, help_, kind='gauge', **labels):
        if value is None:
            return
        fam = self.families.setdefault(name, {'help': help_, 'kind': kind, 'samples': []})
        lab = ','.join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        fam['samples'].append(f'{name}{{{lab}}} {_num(value)}' if lab else f'{name} {_num(value)}')

    def render(self):
        out = []
        for name, fam in self.families.items():
            out += [f'# HELP {name} {fam["help"]}', f'# TYPE {name} {fam["kind"]}', *fam['samples']]
        return '\n'.join(out) + '\n'


async def render_metrics(t):
    x = Exposition()
    x.add('iort_info', 1, 'Build information', version=VERSION)
    x.add('iort_live_trading', settings.live_trading, 'Live order routing enabled (1) or paper mode (0)')
    x.add('iort_kill_switch', t.kill.triggered, 'Persisted kill switch engaged')
    x.add('iort_market_live', t.feed.live(), 'At least one broker feed is live')
    x.add('iort_watchdog_healthy', t.watchdog.healthy(), 'Market-data watchdog healthy')

    st = t.feed.stats()
    x.add('iort_ticks_accepted_total', st['accepted'], 'Ticks accepted by the quality gate', 'counter')
    for reason, n in sorted(st['rejected_by_reason'].items()):
        x.add('iort_ticks_rejected_total', n, 'Ticks rejected by the quality gate', 'counter', reason=reason)
    x.add('iort_feed_instruments', st['instruments'], 'Instruments with a last tick')
    for b, ms in sorted(st['median_skew_ms'].items()):
        x.add('iort_feed_clock_skew_ms', ms, 'Median receive minus exchange timestamp', broker=b)

    for h in await t.health.get():
        x.add('iort_broker_up', h.get('status') == 'LIVE', 'Broker authenticated and healthy', broker=h.get('broker'))
        x.add('iort_broker_status', 1, 'Broker health status', broker=h.get('broker'), status=h.get('status'))

    for w in (t.stream_manager.status() if t.stream_manager else []):
        x.add('iort_stream_healthy', w['healthy'], 'Stream worker receiving data', broker=w['broker'])
        x.add('iort_stream_reconnects_total', w['reconnects'], 'Stream worker reconnects', 'counter', broker=w['broker'])
        x.add('iort_stream_dropped_total', w['dropped'], 'Stream items dropped by back-pressure', 'counter', broker=w['broker'])
        x.add('iort_stream_dynamic_tokens', w.get('dynamic_tokens', 0), 'Runtime-managed subscriptions', broker=w['broker'])

    for status, n in sorted(t.ledger.order_status_counts().items()):
        x.add('iort_orders', n, 'Orders in the ledger by status', status=status)
    x.add('iort_ambiguous_orders', len(t.ledger.ambiguous_orders()), 'Orders that may be live at a broker (UNKNOWN/PENDING)')
    counts = t.ledger.snapshot()
    x.add('iort_fills_total', counts['fills'], 'Fills recorded', 'counter')
    x.add('iort_audit_records_total', counts['audit_records'], 'Audit-chain records', 'counter')

    agg = t.risk_monitor.aggregate()
    x.add('iort_risk_total_pnl', agg['total_pnl'], 'Firm total P&L (INR) from fresh usable broker snapshots')
    x.add('iort_risk_day_pnl', agg['day_pnl'], 'Firm day P&L (INR)')
    x.add('iort_risk_premium_exposure', agg['premium_exposure'], 'Gross premium exposure (INR)')
    x.add('iort_risk_short_option_notional', agg['short_option_notional'], 'Short option notional (INR, strike x qty)')
    x.add('iort_risk_complete', agg['complete'], 'Every configured broker has a fresh usable snapshot')
    for b, snap in sorted(t.risk_monitor.snapshots.items()):
        x.add('iort_risk_snapshot_ok', snap.ok, 'Broker snapshot usable for pre-trade approval', broker=b)
        x.add('iort_risk_snapshot_age_seconds', round(snap.age(), 3), 'Age of the broker snapshot', broker=b)
    lim = settings
    x.add('iort_limit_hard_sl', lim.portfolio_hard_sl, 'Hard portfolio stop (INR)')
    x.add('iort_limit_max_daily_loss', lim.max_daily_loss, 'Daily loss limit (INR)')

    for b, info in sorted(t.instruments.summary().items()):
        x.add('iort_instrument_master_rows', info['count'], 'Instrument-master records', broker=b)
        x.add('iort_instrument_master_fresh', info['fresh_today'], 'Instrument master is today\'s', broker=b)
    if getattr(t, 'instrument_loader', None):
        x.add('iort_instrument_loader_failures', t.instrument_loader.failures, 'Consecutive Kotak master load failures')
    if getattr(t, 'chain_subscriber', None):
        x.add('iort_chain_subscribed_tokens', t.chain_subscriber.state.get('subscribed', 0), 'Option tokens auto-subscribed')
    x.add('iort_event_queue_depth', t.bus.q.qsize(), 'Events waiting for the durable bus')
    x.add('iort_ws_clients', len(t.ws_clients), 'Connected dashboard sockets')
    return x.render()
