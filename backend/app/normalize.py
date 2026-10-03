"""Broker payload normalisation.

Every broker returns orders, trades, positions and margins in its own shape.
This module converts them into one canonical shape so that risk, the order
monitor and reconciliation never read raw broker fields directly.

Field names come from each broker's published API / SDK:
  * Zerodha Kite Connect v3 REST
  * Upstox API v2 REST
  * Angel One SmartAPI REST
  * Kotak Neo (kotakneoapi SDK 3.x). Kotak position and limit fields are NOT
    documented in the SDK; those mappings raise ``EvidenceUnavailable`` when the
    expected keys are absent so risk fails closed instead of guessing.
"""


class EvidenceUnavailable(RuntimeError):
    """Raised when a broker payload lacks the fields needed for a risk decision."""


TERMINAL = {'FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED'}
WORKING = {'PENDING_SUBMIT', 'SUBMITTED', 'OPEN', 'PARTIAL', 'UNKNOWN'}

_STATUS = {
    'COMPLETE': 'FILLED', 'COMPLETED': 'FILLED', 'TRADED': 'FILLED', 'FILLED': 'FILLED', 'EXECUTED': 'FILLED',
    'REJECTED': 'REJECTED', 'REJECT': 'REJECTED',
    'CANCELLED': 'CANCELLED', 'CANCELED': 'CANCELLED',
    'PARTIAL': 'PARTIAL', 'PARTIALLY FILLED': 'PARTIAL', 'PARTIALLY_FILLED': 'PARTIAL',
    'OPEN': 'OPEN', 'TRIGGER PENDING': 'OPEN', 'OPEN PENDING': 'OPEN', 'VALIDATION PENDING': 'OPEN',
    'PUT ORDER REQ RECEIVED': 'OPEN', 'MODIFY PENDING': 'OPEN', 'MODIFY VALIDATION PENDING': 'OPEN',
    'MODIFIED': 'OPEN', 'AFTER MARKET ORDER REQ RECEIVED': 'OPEN', 'AMO REQ RECEIVED': 'OPEN',
    'NOT MODIFIED': 'OPEN', 'NOT CANCELLED': 'OPEN', 'CANCEL PENDING': 'OPEN', 'MODIFY ORDER REQ RECEIVED': 'OPEN',
    'CANCEL ORDER REQ RECEIVED': 'OPEN',
}


def canonical_status(raw, filled_qty=0, qty=0):
    s = _STATUS.get(str(raw or '').strip().upper(), 'UNKNOWN')
    if s == 'OPEN' and filled_qty and filled_qty > 0:
        return 'PARTIAL'
    if s == 'FILLED' and qty and filled_qty and 0 < filled_qty < qty:
        return 'PARTIAL'
    return s


def rows(payload):
    """Extract the row list from the common ``{"data": [...]}`` envelope."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        data = payload.get('data')
        if isinstance(data, list):
            return data
        if data is None and 'data' not in payload:
            return []
        if isinstance(data, dict):
            return [data]
    return []


def _first(row, *keys):
    for k in keys:
        if isinstance(row, dict) and row.get(k) not in (None, ''):
            return row[k]
    return None


def _num(row, *keys, default=None):
    v = _first(row, *keys)
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _int(row, *keys, default=0):
    v = _num(row, *keys)
    return default if v is None else int(v)


_PRODUCT = {'NRML': 'NRML', 'MIS': 'MIS', 'CNC': 'CNC', 'D': 'NRML', 'I': 'MIS', 'CARRYFORWARD': 'NRML',
            'INTRADAY': 'MIS', 'DELIVERY': 'CNC', 'MARGIN': 'NRML'}
_SIDE = {'B': 'BUY', 'S': 'SELL', 'BUY': 'BUY', 'SELL': 'SELL'}

_ORDER_FIELDS = {
    'ZERODHA': dict(id=('order_id',), status=('status',), filled=('filled_quantity',), avg=('average_price',),
                    qty=('quantity',), symbol=('tradingsymbol',), exchange=('exchange',), side=('transaction_type',),
                    tag=('tag',), token=('instrument_token',)),
    'UPSTOX': dict(id=('order_id',), status=('status',), filled=('filled_quantity',), avg=('average_price',),
                   qty=('quantity',), symbol=('trading_symbol', 'tradingsymbol'), exchange=('exchange',),
                   side=('transaction_type',), tag=('tag',), token=('instrument_token',)),
    'ANGEL': dict(id=('orderid',), status=('orderstatus', 'status'), filled=('filledshares',), avg=('averageprice',),
                  qty=('quantity',), symbol=('tradingsymbol',), exchange=('exchange',), side=('transactiontype',),
                  tag=('ordertag',), token=('symboltoken',)),
    'KOTAK': dict(id=('nOrdNo',), status=('ordSt',), filled=('fldQty',), avg=('avgPrc',), qty=('qty',),
                  symbol=('trdSym', 'sym'), exchange=('exSeg',), side=('trnsTp',), tag=('GuiOrdId',), token=('tok',)),
}


def normalize_orders(broker, payload):
    f = _ORDER_FIELDS[broker.upper()]
    out = []
    for r in rows(payload):
        if not isinstance(r, dict):
            continue
        bo = _first(r, *f['id'])
        if bo is None:
            continue
        filled = _int(r, *f['filled'])
        qty = _int(r, *f['qty'])
        raw_status = _first(r, *f['status'])
        out.append({
            'broker': broker.upper(), 'broker_order_id': str(bo), 'raw_status': raw_status,
            'status': canonical_status(raw_status, filled, qty), 'filled_qty': filled, 'qty': qty,
            'avg_price': _num(r, *f['avg'], default=0.0), 'symbol': str(_first(r, *f['symbol']) or ''),
            'exchange': str(_first(r, *f['exchange']) or ''),
            'side': _SIDE.get(str(_first(r, *f['side']) or '').upper(), ''),
            'tag': str(_first(r, *f['tag']) or ''), 'token': str(_first(r, *f['token']) or ''), 'raw': r,
        })
    return out


_TRADE_FIELDS = {
    'ZERODHA': dict(id=('trade_id',), order=('order_id',), qty=('quantity',), price=('average_price', 'price'),
                    ts=('fill_timestamp', 'exchange_timestamp')),
    'UPSTOX': dict(id=('trade_id',), order=('order_id',), qty=('quantity',), price=('average_price', 'price'),
                   ts=('exchange_timestamp', 'order_timestamp')),
    'ANGEL': dict(id=('fillid',), order=('orderid',), qty=('fillsize',), price=('fillprice',), ts=('filltime',)),
    # Kotak trade-report keys are not documented in the SDK; verify with the read-only harness.
    'KOTAK': dict(id=('flId', 'fillId'), order=('nOrdNo',), qty=('fldQty', 'flQty'), price=('avgPrc', 'flPrc'),
                  ts=('flTm', 'exTm')),
}


def normalize_trades(broker, payload):
    f = _TRADE_FIELDS[broker.upper()]
    out = []
    for r in rows(payload):
        if not isinstance(r, dict):
            continue
        bo = _first(r, *f['order'])
        qty = _int(r, *f['qty'])
        price = _num(r, *f['price'])
        if bo is None or qty <= 0 or price is None:
            continue
        tid = _first(r, *f['id'])
        ts = _first(r, *f['ts'])
        if tid is None:
            # Fingerprint only when the broker exposes no trade id; the fill time keeps
            # two genuine fills of equal size and price distinct.
            tid = f'FP:{bo}:{qty}:{price}:{ts}'
        out.append({'broker': broker.upper(), 'trade_id': str(tid), 'broker_order_id': str(bo), 'qty': qty,
                    'price': price, 'raw': r})
    return out


def _zerodha_positions(payload):
    data = payload.get('data') if isinstance(payload, dict) else None
    if isinstance(data, dict):
        net = data.get('net')
        if not isinstance(net, list):
            raise EvidenceUnavailable('ZERODHA_POSITIONS_NET_MISSING')
        return net, True
    return rows(payload), True


def normalize_positions(broker, payload):
    """Return (positions, day_pnl_is_native).

    Each position: token, symbol, exchange, qty (signed net), last_price (or None),
    pnl (total, or None), day_pnl (broker-native day M2M, or None).
    """
    b = broker.upper()
    out = []
    if b == 'ZERODHA':
        src, native = _zerodha_positions(payload)
        for r in src:
            if not isinstance(r, dict):
                continue
            out.append({'token': str(_first(r, 'instrument_token') or ''), 'symbol': str(_first(r, 'tradingsymbol') or ''),
                        'exchange': str(_first(r, 'exchange') or ''), 'qty': _int(r, 'quantity'),
                        'last_price': _num(r, 'last_price'), 'pnl': _num(r, 'pnl'),
                        # Kite: m2m = mark-to-market since the previous close, i.e. today's P&L.
                        'day_pnl': _num(r, 'm2m'), 'product': _PRODUCT.get(str(_first(r, 'product') or '').upper(), 'NRML')})
        return out, native
    if b == 'UPSTOX':
        for r in rows(payload):
            if isinstance(r, dict):
                out.append({'token': str(_first(r, 'instrument_token') or ''),
                            'symbol': str(_first(r, 'trading_symbol', 'tradingsymbol') or ''),
                            'exchange': str(_first(r, 'exchange') or ''), 'qty': _int(r, 'quantity'),
                            'last_price': _num(r, 'last_price'), 'pnl': _num(r, 'pnl'), 'day_pnl': None,
                            'product': _PRODUCT.get(str(_first(r, 'product') or '').upper(), 'NRML')})
        return out, False
    if b == 'ANGEL':
        for r in rows(payload):
            if isinstance(r, dict):
                out.append({'token': str(_first(r, 'symboltoken') or ''), 'symbol': str(_first(r, 'tradingsymbol') or ''),
                            'exchange': str(_first(r, 'exchange') or ''), 'qty': _int(r, 'netqty'),
                            'last_price': _num(r, 'ltp'), 'pnl': _num(r, 'pnl'), 'day_pnl': None,
                            'product': _PRODUCT.get(str(_first(r, 'producttype') or '').upper(), 'NRML')})
        return out, False
    if b == 'KOTAK':
        need = ('flBuyQty', 'flSellQty', 'cfBuyQty', 'cfSellQty', 'buyAmt', 'sellAmt', 'cfBuyAmt', 'cfSellAmt')
        for r in rows(payload):
            if not isinstance(r, dict):
                continue
            missing = [k for k in need if k not in r]
            if missing:
                raise EvidenceUnavailable('KOTAK_POSITION_FIELDS_MISSING:' + ','.join(missing))
            qty = _int(r, 'flBuyQty') + _int(r, 'cfBuyQty') - _int(r, 'flSellQty') - _int(r, 'cfSellQty')
            cash = (_num(r, 'sellAmt', default=0) + _num(r, 'cfSellAmt', default=0)
                    - _num(r, 'buyAmt', default=0) - _num(r, 'cfBuyAmt', default=0))
            out.append({'token': str(_first(r, 'tok') or ''), 'symbol': str(_first(r, 'trdSym', 'sym') or ''),
                        'exchange': str(_first(r, 'exSeg') or ''), 'qty': qty, 'last_price': _num(r, 'ltp', 'lastPrice'),
                        'pnl': None, 'cash_flow': cash, 'day_pnl': None,
                        'product': _PRODUCT.get(str(_first(r, 'prod') or '').upper(), 'NRML')})
        return out, False
    raise EvidenceUnavailable(f'UNKNOWN_BROKER:{broker}')


def normalize_margin(broker, payload):
    """Return {'used', 'available', 'pct'} or raise EvidenceUnavailable."""
    b = broker.upper()
    data = payload.get('data', payload) if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise EvidenceUnavailable(f'{b}_MARGIN_PAYLOAD_INVALID')
    used = avail = None
    if b == 'ZERODHA':
        used = avail = 0.0
        found = False
        for seg in ('equity', 'commodity'):
            s = data.get(seg)
            if isinstance(s, dict) and s.get('enabled', True) and isinstance(s.get('utilised'), dict):
                used += _num(s['utilised'], 'debits', default=0.0)
                avail += _num(s, 'net', default=0.0)
                found = True
        if not found:
            raise EvidenceUnavailable('ZERODHA_MARGIN_FIELDS_MISSING')
    elif b == 'UPSTOX':
        used = avail = 0.0
        found = False
        for seg in ('equity', 'commodity'):
            s = data.get(seg)
            if isinstance(s, dict) and _num(s, 'used_margin') is not None and _num(s, 'available_margin') is not None:
                used += _num(s, 'used_margin')
                avail += _num(s, 'available_margin')
                found = True
        if not found:
            raise EvidenceUnavailable('UPSTOX_MARGIN_FIELDS_MISSING')
    elif b == 'ANGEL':
        used, avail = _num(data, 'utiliseddebits'), _num(data, 'net')
    elif b == 'KOTAK':
        used, avail = _num(data, 'MarginUsed', 'marginUsed'), _num(data, 'Net', 'net')
    if used is None or avail is None:
        raise EvidenceUnavailable(f'{b}_MARGIN_FIELDS_MISSING')
    total = used + max(avail, 0.0)
    if total <= 0:
        raise EvidenceUnavailable(f'{b}_MARGIN_TOTAL_ZERO')
    return {'used': used, 'available': avail, 'pct': 100.0 * used / total}
