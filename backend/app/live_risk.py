"""Broker-authoritative portfolio snapshot.

Built from normalised broker positions and margins, valued with fresh feed prices
where available. Anything that cannot be valued is reported as an issue and makes
the snapshot unusable for pre-trade approval (fail closed). Nothing unknown is
silently valued at zero.
"""
import time
from dataclasses import asdict, dataclass, field

from .clock import trading_date
from .config import settings
from .instruments import _EXCH

@dataclass
class LiveRiskSnapshot:
    broker: str
    ts: float
    positions: list = field(default_factory=list)
    total_pnl: float | None = None
    day_pnl: float | None = None
    day_pnl_source: str | None = None
    baseline_captured_at: str | None = None
    premium_exposure: float = 0.0
    short_option_notional: float = 0.0
    margin: dict | None = None
    margin_error: str | None = None
    issues: list = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self):
        return self.error is None and not self.issues

    def age(self):
        return time.time() - self.ts

    def position(self, token):
        return next((p for p in self.positions if p['token'] == str(token)), None)

    def to_dict(self):
        d = asdict(self)
        d['ok'] = self.ok
        d['age_sec'] = round(self.age(), 2)
        return d


async def build_snapshot(broker, feed, instruments, ledger):
    snap = LiveRiskSnapshot(broker=broker.name, ts=time.time())
    try:
        positions, native_day = await broker.positions()
    except Exception as exc:  # noqa: BLE001
        snap.error = f'POSITIONS_UNAVAILABLE:{str(exc)[:160]}'
        return snap
    try:
        snap.margin = await broker.margin()
    except Exception as exc:  # noqa: BLE001
        snap.margin_error = str(exc)[:160]

    prices = feed.price_lookup(broker.name, fresh_only=True)
    total, day, day_complete = 0.0, 0.0, native_day
    for p in positions:
        rec = instruments.get(broker.name, p['token']) or {}
        p.update({k: rec.get(k) for k in ('underlying', 'expiry', 'strike', 'option_type', 'lot_size')})
        p['exchange'] = _EXCH.get(str(p.get('exchange', '')).upper(), str(p.get('exchange', '')).upper())
        qty = p['qty']
        px, src = prices.get(p['token']), 'FEED'
        if px is None:
            px, src = p.get('last_price'), 'BROKER'
        p['price'], p['price_source'] = px, src
        if qty != 0:
            if not px or px <= 0:
                snap.issues.append(f"VALUATION_UNAVAILABLE:{p['symbol'] or p['token']}")
                continue
            if settings.require_live_ltp_for_exposure and src != 'FEED':
                snap.issues.append(f"LIVE_LTP_REQUIRED:{p['symbol'] or p['token']}")
            snap.premium_exposure += abs(qty) * px
            if p.get('option_type') and qty < 0:
                if not p.get('strike'):
                    snap.issues.append(f"STRIKE_UNKNOWN:{p['symbol'] or p['token']}")
                else:
                    snap.short_option_notional += abs(qty) * p['strike']
        pnl = p.get('pnl')
        if pnl is None and 'cash_flow' in p and px:
            pnl = p['cash_flow'] + qty * px
            p['pnl'] = pnl
        if pnl is None:
            snap.issues.append(f"PNL_UNAVAILABLE:{p['symbol'] or p['token']}")
        else:
            total += pnl
        if native_day:
            if p.get('day_pnl') is None:
                day_complete = False
            else:
                day += p['day_pnl']
    snap.positions = positions
    if snap.issues:
        return snap
    snap.total_pnl = total
    today = trading_date()
    if native_day and day_complete:
        snap.day_pnl, snap.day_pnl_source = day, 'BROKER'
    else:
        ledger.set_daily_baseline(broker.name, today, total)
        base = ledger.daily_baseline(broker.name, today)
        snap.day_pnl = total - base['total_pnl']
        snap.day_pnl_source = 'SOD_BASELINE'
        snap.baseline_captured_at = base['captured_at'].isoformat() if base['captured_at'] else None
    return snap


def snapshot_from_positions(broker_name, payload, margin_payload=None, price_lookup=None):
    """Compatibility helper for the analytics/certification tooling: synchronous
    snapshot from raw broker payloads. Uses the same normalisers as live risk."""
    from .normalize import normalize_margin, normalize_positions
    positions, _ = normalize_positions(broker_name, payload)
    pnl = sum(p['pnl'] or 0 for p in positions)
    exposure = 0.0
    for p in positions:
        px = (price_lookup or {}).get(p['token']) or p.get('last_price')
        if p['qty'] and (not px or px <= 0):
            raise RuntimeError('LIVE_EXPOSURE_PRICE_UNAVAILABLE')
        exposure += abs(p['qty']) * (px or 0)
    margin = normalize_margin(broker_name, margin_payload)['pct'] if margin_payload else None
    return {'broker': broker_name.upper(), 'net_pnl': pnl, 'exposure': exposure, 'positions_count': len(positions),
            'margin_pct': margin}
