"""Kotak login backoff/halt and scrip-master expiry decoding."""
import asyncio
import time
from datetime import date, datetime

import pytest
from conftest import TOKEN, expiry_in, override
from starlette.testclient import TestClient

from app.brokers import Kotak, login_failure_kind, totp_wait
from app.clock import IST
from app.instruments import KOTAK_FO_EPOCH_OFFSET, kotak_expiry, normalize_row, symbol_expiry_hint
from app.main import create_app
from app.models import Tick

CREDS = dict(kotak_api_key='key', kotak_mobile='+910000000000', kotak_client_code='UCC', kotak_mpin='000000',
             kotak_totp='123456', kotak_totp_secret='')


class ApiErr(Exception):
    """Shape of neo_api_client.exceptions.ApiException: only .status matters."""

    def __init__(self, status):
        super().__init__(f'({status})')
        self.status = status


def neo_factory(script):
    """Fake NeoAPI. Each construction consumes one outcome: 'ok', 'reject_totp', 'reject_mpin' or an exception."""
    calls = []

    class Neo:
        def __init__(self, consumer_key, environment):
            calls.append(consumer_key)
            self.outcome = script.pop(0) if script else 'ok'

        def totp_login(self, mobile_number, ucc, totp):
            if isinstance(self.outcome, Exception):
                raise self.outcome
            if self.outcome == 'reject_totp':
                return {'error': [{'code': '401', 'message': 'Invalid TOTP'}]}
            return {'data': {'token': 'view', 'sid': 's'}}

        def totp_validate(self, mpin):
            if self.outcome == 'reject_mpin':
                return {'error': [{'code': '401', 'message': 'Invalid MPIN'}]}
            return {'data': {'token': 'trade'}}
    return Neo, calls


def kotak(script, settings_override, **extra):
    settings_override(**CREDS, **extra)
    neo, calls = neo_factory(script)
    now = [1000.0]
    return Kotak(client_factory=neo, clock=lambda: now[0]), calls, now


def login(k, force=False):
    return asyncio.run(k.session(force=force))


# ------------------------------------------------------------------ login backoff
def test_transport_failures_back_off_exponentially_with_cap(settings_override):
    k, calls, now = kotak([ApiErr(0), ApiErr(0), ApiErr(503), ApiErr(0)], settings_override,
                          kotak_login_backoff_sec=30, kotak_login_backoff_max_sec=100)
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_TRANSPORT:retry_in=30s'):
        login(k)
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_BACKOFF'):
        login(k)
    assert len(calls) == 1  # the backoff refused without touching the network
    now[0] += 30
    with pytest.raises(RuntimeError, match='retry_in=60s'):
        login(k)
    now[0] += 59
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_BACKOFF'):
        login(k)
    now[0] += 1
    with pytest.raises(RuntimeError, match='retry_in=100s'):  # 120 capped at 100
        login(k)
    now[0] += 100
    with pytest.raises(RuntimeError, match='retry_in=100s'):
        login(k)
    assert len(calls) == 4 and not k.login_state()['halted'] and k.login_state()['consecutive_rejections'] == 0
    now[0] += 100
    assert login(k) is not None
    assert k.login_state() == {'halted': False, 'consecutive_failures': 0, 'consecutive_rejections': 0,
                               'retry_in_sec': 0, 'last_error': None}


def test_credential_rejections_halt_until_operator_reset(settings_override):
    k, calls, now = kotak(['reject_mpin', 'reject_totp'], settings_override, kotak_login_max_rejections=2)
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_REJECTED:retry_in=30s:MPIN'):
        login(k)
    now[0] += 30
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_HALTED:2_REJECTIONS'):
        login(k)
    h = asyncio.run(k.health())
    assert h['status'] == 'LOGIN_HALTED' and h['login']['halted']
    now[0] += 10 ** 6
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_HALTED'):
        login(k, force=True)  # force never bypasses the halt
    assert len(calls) == 2
    k.reset_login()
    assert login(k) is not None and len(calls) == 3


def test_transport_failure_breaks_a_rejection_streak(settings_override):
    k, calls, now = kotak([ApiErr(401), ApiErr(0), ApiErr(403)], settings_override, kotak_login_max_rejections=2,
                          kotak_login_backoff_max_sec=1)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            login(k)
        now[0] += 1
    assert not k.login_state()['halted'] and k.login_state()['consecutive_rejections'] == 1


def test_concurrent_callers_share_one_login_attempt(settings_override):
    k, calls, _ = kotak([ApiErr(0)], settings_override)

    async def many():
        return await asyncio.gather(*(k.session() for _ in range(6)), return_exceptions=True)
    results = asyncio.run(many())
    assert all(isinstance(r, RuntimeError) for r in results) and len(calls) == 1


def test_failure_classification_and_totp_guard():
    for status in (400, 401, 403, 422):
        assert login_failure_kind(ApiErr(status)) == 'REJECTED'
    for status in (0, 408, 429, 500, 503, None):
        assert login_failure_kind(ApiErr(status)) == 'TRANSPORT'
    assert login_failure_kind(TimeoutError()) == 'TRANSPORT'
    assert totp_wait(29.0) == pytest.approx(1.25) and totp_wait(10.0) == 0.0 and totp_wait(57.5) == pytest.approx(2.75)


def test_login_reset_endpoint_needs_operator_and_is_audited(make_terminal, settings_override):
    k, calls, now = kotak(['reject_mpin'], settings_override, kotak_login_max_rejections=1)
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_HALTED'):
        login(k)
    with override(operator_api_token=TOKEN):
        t, _ = make_terminal(broker=k)
        c = TestClient(create_app(t, run_background=False))
        assert c.post('/brokers/KOTAK/login-reset').status_code == 401
        r = c.post('/brokers/KOTAK/login-reset', headers={'X-IORT-Operator-Token': TOKEN}).json()
        assert r['status'] == 'RESET' and r['before']['halted'] and not r['login']['halted']
        assert t.ledger.audit_records()[-1]['action'] == 'BROKER_LOGIN_RESET'
        assert c.post('/brokers/NOPE/login-reset', headers={'X-IORT-Operator-Token': TOKEN}).status_code == 404


# --------------------------------------------------------------- expiry decoding
TODAY = date(2026, 10, 5)


def at_ist(d, hh=14, mm=30):
    return int(datetime(d.year, d.month, d.day, hh, mm, tzinfo=IST).timestamp())


def kotak_row(symbol, seg, raw, strike_paise='2500000', opt='CE', underlying='NIFTY', **extra):
    return {'pSymbol': '48201', 'pTrdSymbol': symbol, 'pExchSeg': seg, 'pSymbolName': underlying, 'pExpiryDate': str(raw),
            'dStrikePrice;': strike_paise, 'pOptionType': opt, 'lLotSize': '75', 'pInstType': 'OPTIDX', **extra}


def test_nse_fo_uses_sdk_offset_and_monthly_symbol_agrees():
    d = date(2026, 10, 27)
    rec = normalize_row('KOTAK', kotak_row('NIFTY26OCT25000CE', 'nse_fo', at_ist(d) - KOTAK_FO_EPOCH_OFFSET), TODAY)
    assert rec['expiry'] == '2026-10-27' and rec['strike'] == 25000.0 and rec['exchange'] == 'NFO'
    assert rec['option_type'] == 'CE' and rec['lot_size'] == 75 and rec['underlying'] == 'NIFTY'


def test_weekly_symbol_must_match_the_decoded_date():
    raw = at_ist(date(2026, 10, 6)) - KOTAK_FO_EPOCH_OFFSET
    assert normalize_row('KOTAK', kotak_row('NIFTY26O0625000CE', 'nse_fo', raw), TODAY)['expiry'] == '2026-10-06'
    assert normalize_row('KOTAK', kotak_row('NIFTY26O1325000CE', 'nse_fo', raw), TODAY)['expiry'] is None


def test_bse_fo_and_mcx_fo_are_plain_unix_seconds():
    bse = kotak_row('SENSEX26O0881000CE', 'bse_fo', at_ist(date(2026, 10, 8)), strike_paise='8100000', underlying='SENSEX')
    assert normalize_row('KOTAK', bse, TODAY)['expiry'] == '2026-10-08'
    mcx = kotak_row('CRUDEOIL26NOV5500CE', 'mcx_fo', at_ist(date(2026, 11, 17), 23, 30), strike_paise='550000', underlying='CRUDEOIL')
    rec = normalize_row('KOTAK', mcx, TODAY)
    assert rec['expiry'] == '2026-11-17' and rec['exchange'] == 'MCX'


def test_other_epoch_convention_only_with_symbol_confirmation():
    raw = at_ist(date(2026, 10, 27))  # plain Unix seconds on an nse_fo row: SDK rule lands ~10 years off
    assert kotak_expiry(raw, 'nse_fo', 'NIFTY26OCT25000CE', 'NIFTY', 25000.0, TODAY) == '2026-10-27'
    assert kotak_expiry(raw, 'nse_fo', 'UNPARSEABLE', 'NIFTY', 25000.0, TODAY) is None


def test_implausible_or_missing_expiry_fails_closed():
    assert kotak_expiry(at_ist(date(2020, 1, 30)) - KOTAK_FO_EPOCH_OFFSET, 'nse_fo', None, None, None, TODAY) is None
    for raw in (None, '', '0', '-1', 'abc'):
        assert kotak_expiry(raw, 'nse_fo', 'NIFTY26OCT25000CE', 'NIFTY', 25000.0, TODAY) is None


def test_futures_and_padded_csv_headers():
    raw = at_ist(date(2026, 10, 27)) - KOTAK_FO_EPOCH_OFFSET
    row = {' pSymbol ': '35001', 'pTrdSymbol ': 'NIFTY26OCTFUT', ' pExchSeg': 'nse_fo', 'pSymbolName': 'NIFTY',
           ' pExpiryDate ': str(raw), 'dStrikePrice;': '-1', 'pOptionType': 'XX', 'lLotSize': '75', 'pInstType': 'FUTIDX'}
    rec = normalize_row('KOTAK', row, TODAY)
    assert rec['token'] == '35001' and rec['expiry'] == '2026-10-27' and rec['option_type'] is None and rec['strike'] is None


def test_symbol_hint_parsing():
    assert symbol_expiry_hint('NIFTY26OCT25000CE', 'NIFTY', 25000.0) == ('MONTH', (2026, 10))
    assert symbol_expiry_hint('NIFTY26O0625000PE', 'NIFTY', 25000.0) == ('DATE', date(2026, 10, 6))
    assert symbol_expiry_hint('BANKNIFTY2611356000CE', 'BANKNIFTY', 56000.0) == ('DATE', date(2026, 1, 13))
    assert symbol_expiry_hint('NIFTY26O0625000PE', 'NIFTY', 24000.0) is None  # strike disagrees
    assert symbol_expiry_hint('NIFTY26XYZ25000CE', 'NIFTY', 25000.0) is None
    assert symbol_expiry_hint('RELIANCE-EQ', 'RELIANCE', None) is None


def test_kotak_option_ticks_now_reach_the_option_chain(make_terminal):
    t, _ = make_terminal()
    exp = expiry_in(7)
    d = date.fromisoformat(exp)
    weekly = f"NIFTY{d:%y}{'123456789OND'[d.month - 1]}{d:%d}"
    rows = [kotak_row(weekly + '25000CE', 'nse_fo', at_ist(d) - KOTAK_FO_EPOCH_OFFSET, pSymbol='48201'),
            kotak_row(weekly + '25000PE', 'nse_fo', at_ist(d) - KOTAK_FO_EPOCH_OFFSET, opt='PE', pSymbol='48202')]
    t.instruments.load_rows('KOTAK', rows)
    assert t.instruments.get('KOTAK', '48201')['expiry'] == exp
    now = int(time.time() * 1000)
    for tok, px in (('48201', 120.0), ('48202', 95.0)):
        r = asyncio.run(t.handle_tick(Tick(broker='KOTAK', exchange='nse_fo', instrument_token=tok, symbol=tok, ltp=px,
                                           bid=px - 0.05, ask=px + 0.05, oi=1000, exchange_ts_ms=now, receive_ts_ms=now)))
        assert r['accepted']
    assert t.chain.index() == [{'exchange': 'NFO', 'underlying': 'NIFTY', 'expiries': [exp]}]
    lad = t.chain_ladder('NFO', 'NIFTY', exp)
    assert lad['strikes'][0]['CE']['lot_size'] == 75 and lad['spot']['source'] == 'PUT_CALL_PARITY'
