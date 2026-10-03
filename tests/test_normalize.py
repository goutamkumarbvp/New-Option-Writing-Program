import pytest

from app.normalize import (EvidenceUnavailable, canonical_status, normalize_margin, normalize_orders, normalize_positions,
                           normalize_trades)

ZERODHA_POSITIONS = {'status': 'success', 'data': {
    'net': [{'tradingsymbol': 'NIFTY25OCT25000CE', 'instrument_token': 111, 'exchange': 'NFO', 'quantity': -75,
             'last_price': 120.0, 'pnl': -9000.0, 'm2m': -3000.0, 'product': 'NRML'}],
    'day': []}}
ZERODHA_MARGINS = {'status': 'success', 'data': {
    'equity': {'enabled': True, 'net': 50000.0, 'available': {'cash': 200000.0}, 'utilised': {'debits': 150000.0}},
    'commodity': {'enabled': False, 'net': 0, 'available': {}, 'utilised': {'debits': 0}}}}


def test_zerodha_positions_are_read_from_net_book():
    # Original read data={'net','day'} as a list of two strings: P&L and exposure became 0.
    rows, native = normalize_positions('ZERODHA', ZERODHA_POSITIONS)
    assert native is True
    assert rows[0]['qty'] == -75 and rows[0]['pnl'] == -9000.0 and rows[0]['day_pnl'] == -3000.0
    assert rows[0]['token'] == '111' and rows[0]['product'] == 'NRML'


def test_zerodha_margin_utilisation_is_not_read_as_zero():
    # Original reported 0% here (fail-open); true utilisation is 150000 / 200000.
    assert normalize_margin('ZERODHA', ZERODHA_MARGINS)['pct'] == pytest.approx(75.0)


def test_upstox_and_angel_margins():
    up = {'status': 'success', 'data': {'equity': {'used_margin': 30.0, 'available_margin': 70.0}}}
    assert normalize_margin('UPSTOX', up)['pct'] == pytest.approx(30.0)
    an = {'status': True, 'data': {'net': '75000', 'utiliseddebits': '25000'}}
    assert normalize_margin('ANGEL', an)['pct'] == pytest.approx(25.0)


def test_missing_margin_fields_fail_closed():
    with pytest.raises(EvidenceUnavailable):
        normalize_margin('KOTAK', {'stat': 'Ok'})
    with pytest.raises(EvidenceUnavailable):
        normalize_margin('ZERODHA', {'data': {}})


def test_angel_and_kotak_orders_are_recognised():
    angel = normalize_orders('ANGEL', {'status': True, 'data': [
        {'orderid': 'A1', 'orderstatus': 'complete', 'filledshares': '75', 'averageprice': 10, 'quantity': '75',
         'tradingsymbol': 'X', 'transactiontype': 'SELL', 'ordertag': 'IOABC'}]})
    assert angel[0]['broker_order_id'] == 'A1' and angel[0]['status'] == 'FILLED' and angel[0]['tag'] == 'IOABC'
    kotak = normalize_orders('KOTAK', {'stat': 'Ok', 'data': [
        {'nOrdNo': 'K1', 'ordSt': 'open', 'fldQty': 25, 'avgPrc': '10.5', 'qty': 75, 'trnsTp': 'S', 'GuiOrdId': 'IOX'}]})
    assert kotak[0]['status'] == 'PARTIAL' and kotak[0]['side'] == 'SELL' and kotak[0]['filled_qty'] == 25


def test_status_mapping():
    assert canonical_status('TRIGGER PENDING') == 'OPEN'
    assert canonical_status('put order req received') == 'OPEN'
    assert canonical_status('COMPLETE', 50, 75) == 'PARTIAL'
    assert canonical_status('something new') == 'UNKNOWN'


def test_trades_keep_distinct_fills_without_trade_id():
    t = normalize_trades('KOTAK', {'data': [{'nOrdNo': '1', 'fldQty': 75, 'avgPrc': 10, 'flTm': 'a'},
                                           {'nOrdNo': '1', 'fldQty': 75, 'avgPrc': 10, 'flTm': 'b'}]})
    assert len({x['trade_id'] for x in t}) == 2
    a = normalize_trades('ANGEL', {'data': [{'fillid': 'F1', 'orderid': 'A1', 'fillsize': 75, 'fillprice': 10}]})
    assert a[0]['trade_id'] == 'F1'


def test_kotak_positions_without_documented_fields_fail_closed():
    with pytest.raises(EvidenceUnavailable):
        normalize_positions('KOTAK', {'data': [{'tok': '1', 'trdSym': 'X'}]})
    rows, _ = normalize_positions('KOTAK', {'data': [{'tok': '1', 'trdSym': 'X', 'flBuyQty': '0', 'flSellQty': '75', 'cfBuyQty': '0',
                                                       'cfSellQty': '0', 'buyAmt': '0', 'sellAmt': '7500', 'cfBuyAmt': '0',
                                                       'cfSellAmt': '0'}]})
    assert rows[0]['qty'] == -75 and rows[0]['cash_flow'] == 7500
