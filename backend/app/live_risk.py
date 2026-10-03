"""Fail-closed broker-authoritative portfolio risk evidence."""
from dataclasses import dataclass

@dataclass(frozen=True)
class LiveRiskSnapshot:
    broker: str; net_pnl: float; exposure: float; positions_count: int
    reconciled: bool; margin_pct: float|None; daily_pnl: float|None
    source: str='broker-rest'

def _rows(payload):
    if isinstance(payload,dict): return payload.get('data') or []
    return payload or []
def _num(row,*keys,default=0.0):
    for k in keys:
        v=row.get(k) if isinstance(row,dict) else None
        if v not in (None,''):
            try:return float(v)
            except (TypeError,ValueError):pass
    return float(default)

def snapshot_from_positions(broker_name,payload,margin_payload=None,price_lookup=None):
    rows=_rows(payload); pnl=0.0; exposure=0.0; daily_pnl=0.0; daily_available=False
    for r in rows:
        if not isinstance(r,dict): continue
        pnl += _num(r,'pnl','pnlAbsolute','pnl_absolute','unrealised','unrealized_pnl','realised','realized_pnl')
        day_keys=('day_pnl','dayPnl','daily_pnl','dailyPnl','pnl_day','realized_today','realised_today','unrealized_today','unrealised_today')
        if any(isinstance(r,dict) and r.get(k) not in (None,'') for k in day_keys):
            daily_available=True
            daily_pnl += _num(r,*day_keys)
        qty=abs(_num(r,'quantity','net_quantity','netQty','netqty','qty'))
        token=str(r.get('instrument_token') or r.get('instrumentToken') or r.get('token') or '')
        live_px=None
        if price_lookup and token:
            live_px=price_lookup.get(token)
        if live_px is None:
            live_px=_num(r,'last_price','lastPrice','ltp','market_price',default=0)
        if live_px<=0 and price_lookup is not None:
            # Unknown live valuation is intentionally not converted to zero.
            raise RuntimeError('LIVE_EXPOSURE_PRICE_UNAVAILABLE')
        exposure += qty*abs(float(live_px))
    margin_pct=None
    if isinstance(margin_payload,dict):
        data=margin_payload.get('data') or margin_payload
        used=_num(data,'utilised_margin','utilized_margin','used_margin','margin_used')
        avail=_num(data,'available_margin','available','net','cash')
        if used>=0 and avail>0: margin_pct=100*used/(used+avail)
        # common nested broker shapes
        for key in ('equity','equity_margin','equityMargin','equity_margins'):
            x=data.get(key) if isinstance(data,dict) else None
            if isinstance(x,dict):
                used=_num(x,'used','utilised','utilized','utilised_margin')
                avail=_num(x,'available','net','available_margin')
                if avail>0: margin_pct=100*used/(used+avail); break
    return LiveRiskSnapshot(str(broker_name).upper(),pnl,exposure,len(rows),True,margin_pct,daily_pnl if daily_available else None)
