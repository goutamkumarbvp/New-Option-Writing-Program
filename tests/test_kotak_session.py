"""Kotak session self-repair: expiry detection, one re-login and retry, honest health, daily
session, TOTP window reuse, clock-drift hint and pre-open login."""
import asyncio
from datetime import datetime

import httpx
import pytest

from app.brokers import AUTH_LIKELY, AUTH_PROVEN, Kotak, kotak_auth_failure
from app.clock import IST
from app.clockcheck import clock_hint, clock_offset, clock_status

CREDS = dict(kotak_api_key='key', kotak_mobile='+919876543210', kotak_client_code='UCC', kotak_mpin='000000',
             kotak_totp='', kotak_totp_secret='JBSWY3DPEHPK3PXP', kotak_login_backoff_sec=30, kotak_login_max_rejections=2,
             kotak_relogin_after_errors=3, broker_health_ttl_sec=10, kotak_prelogin_ist='08:50')
FAULT = {'fault': {'code': 900901, 'message': 'Invalid Credentials', 'description': 'Access failure for API'}}
POSITIONS = {'stat': 'Ok', 'data': []}


class ApiErr(Exception):
    def __init__(self, status):
        super().__init__(f'({status})')
        self.status = status


class Rig:
    """A fake NeoAPI whose method results are scripted per call, plus controllable clocks."""

    def __init__(self, settings_override, calls=None, logins=None, **extra):
        settings_override(**{**CREDS, **extra})
        self.calls = dict(calls or {})     # method -> list of results (an Exception is raised)
        self.logins = list(logins or [])   # per login: 'ok', 'reject_totp' or an Exception
        self.clients = []
        self.codes = []
        self.mono = [1000.0]
        self.wall = [1_759_660_210.0]      # 10 s into a 30 s TOTP window
        self.slept = []
        rig = self

        class Neo:
            def __init__(self, consumer_key, environment):
                self.outcome = rig.logins.pop(0) if rig.logins else 'ok'
                self.n = len(rig.clients) + 1
                rig.clients.append(self)

            def totp_login(self, mobile_number, ucc, totp):
                rig.codes.append((totp, int(rig.wall[0] // 30)))
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                if self.outcome == 'reject_totp':
                    return {'error': [{'code': '424', 'message': 'Invalid TOTP'}]}
                return {'data': {'token': 'view', 'sid': f's{self.n}'}}

            def totp_validate(self, mpin):
                return {'data': {'token': 'trade'}}

            def __getattr__(self, name):
                script = rig.calls.get(name)
                if script is None:
                    raise AttributeError(name)

                def call(**kw):
                    r = script.pop(0) if len(script) > 1 else script[0]
                    if isinstance(r, Exception):
                        raise r
                    return r(self) if callable(r) else r
                return call

        async def sleep(sec):
            rig.slept.append(round(sec, 2))
            rig.wall[0] += sec

        self.probe_result = None

        async def probe():
            return self.probe_result

        self.k = Kotak(client_factory=Neo, clock=lambda: self.mono[0], wall=lambda: self.wall[0], sleep=sleep, clock_probe=probe)

    def run(self, coro):
        return asyncio.run(coro)


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize('obj,want', [
    (ApiErr(401), AUTH_PROVEN), (ApiErr(403), AUTH_PROVEN), (ApiErr(500), None), (ApiErr(0), None), (TimeoutError(), None),
    (FAULT, AUTH_PROVEN), ({'code': '900902', 'message': 'Missing Credentials'}, AUTH_PROVEN),
    ({'Error Message': 'Complete the 2fa process before accessing this application'}, AUTH_PROVEN),
    ({'error': [{'code': '401', 'message': 'Unauthorized'}]}, AUTH_PROVEN),
    ({'stat': 'Not_Ok', 'emsg': 'Session Expired, please login again', 'stCode': 1009}, AUTH_LIKELY),
    ({'Error': 'Invalid JWT token'}, AUTH_LIKELY),
    ({'stat': 'Not_Ok', 'emsg': 'No Data', 'stCode': 5203}, None),
    ({'stat': 'Ok', 'data': [{'tok': '48201', 'trdSym': 'NIFTY', 'nOrdNo': '1'}]}, None),
    ({'Error': 'Connection aborted'}, None), ('text', None), (None, None),
])
def test_auth_failure_classification(obj, want):
    assert kotak_auth_failure(obj) == want


# ------------------------------------------------------------------ re-login and retry
def test_expired_session_is_replaced_once_and_the_call_repeated(settings_override):
    rig = Rig(settings_override, calls={'positions': [lambda c: FAULT if c.n == 1 else POSITIONS]})
    assert rig.run(rig.k.raw_positions()) == POSITIONS
    st = rig.k.login_state()
    assert len(rig.clients) == 2 and st['relogins'] == 1 and not st['session_expired'] and st['last_error'] is None
    assert st['last_ok_age_sec'] == 0


def test_second_auth_failure_raises_and_leaves_the_session_dropped(settings_override):
    rig = Rig(settings_override, calls={'positions': [FAULT]})
    rig.wall[0] += 0  # each login lands in a new TOTP window only if time moves; static KOTAK_TOTP skips the wait
    with pytest.raises(RuntimeError, match='KOTAK_POSITIONS_AUTH_ERROR'):
        rig.run(rig.k.raw_positions())
    st = rig.k.login_state()
    assert len(rig.clients) == 2 and st['session_expired'] and not st['logged_in']
    assert st['last_error'].startswith('SESSION_EXPIRED')


def test_http_401_exception_is_also_an_expiry(settings_override):
    rig = Rig(settings_override, calls={'limits': [ApiErr(401), {'stat': 'Ok', 'data': {'Net': 1}}]})
    assert rig.run(rig.k.raw_margin())['stat'] == 'Ok' and len(rig.clients) == 2


def test_plain_errors_keep_the_session_until_three_in_a_row(settings_override):
    rig = Rig(settings_override, calls={'positions': [{'Error': 'boom'}, {'Error': 'boom'}, POSITIONS,
                                                      {'Error': 'boom'}, {'Error': 'boom'}, {'Error': 'boom'}, POSITIONS]})
    for _ in range(2):
        with pytest.raises(RuntimeError, match='KOTAK_POSITIONS_ERROR'):
            rig.run(rig.k.raw_positions())
    assert rig.run(rig.k.raw_positions()) == POSITIONS and len(rig.clients) == 1  # a success resets the count
    for _ in range(3):
        with pytest.raises(RuntimeError, match='KOTAK_POSITIONS_ERROR'):
            rig.run(rig.k.raw_positions())
    assert rig.k.login_state()['session_expired']
    assert rig.run(rig.k.raw_positions()) == POSITIONS and len(rig.clients) == 2


def test_a_session_from_an_earlier_day_is_replaced(settings_override):
    rig = Rig(settings_override, calls={'positions': [POSITIONS]})
    rig.run(rig.k.raw_positions())
    rig.k._session_day = '2000-01-01'
    rig.run(rig.k.raw_positions())
    assert len(rig.clients) == 2 and rig.k.login_state()['relogins'] == 1


def test_relogin_is_still_bound_by_the_halt(settings_override):
    rig = Rig(settings_override, calls={'positions': [FAULT]}, logins=['ok', 'reject_totp', 'reject_totp'])
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_REJECTED'):
        rig.run(rig.k.raw_positions())
    rig.mono[0] += 30
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_HALTED'):
        rig.run(rig.k.raw_positions())
    rig.mono[0] += 10 ** 6
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_HALTED'):
        rig.run(rig.k.raw_positions())
    assert len(rig.clients) == 3


# ------------------------------------------------------------------ TOTP and clock
def test_a_totp_window_is_never_used_twice(settings_override):
    rig = Rig(settings_override, calls={'positions': [lambda c: FAULT if c.n == 1 else POSITIONS]},
              kotak_totp_secret='JBSWY3DPEHPK3PXP', kotak_totp='')
    assert rig.run(rig.k.raw_positions()) == POSITIONS
    (code1, step1), (code2, step2) = rig.codes
    assert step2 == step1 + 1 and rig.slept == [20.25]  # waited out the rest of the first window


def test_code_near_the_end_of_its_window_waits_for_the_next(settings_override):
    rig = Rig(settings_override, calls={'positions': [POSITIONS]}, kotak_totp_secret='JBSWY3DPEHPK3PXP', kotak_totp='')
    rig.wall[0] += 18.5  # 1.5 s left in the window
    rig.run(rig.k.raw_positions())
    assert rig.slept == [1.75]


def test_totp_rejection_measures_clock_drift_and_says_how_to_fix_it(settings_override):
    rig = Rig(settings_override, logins=['reject_totp'], kotak_totp_secret='JBSWY3DPEHPK3PXP', kotak_totp='')
    rig.probe_result = -42.0
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_REJECTED'):
        rig.run(rig.k.session())
    st = rig.k.login_state()
    assert st['clock_offset_sec'] == -42.0 and 'ahead of Kotak' in st['last_error'] and 'w32tm /resync' in st['last_error']
    rig.k.reset_login()
    assert rig.k.login_state()['clock_offset_sec'] is None


def test_static_totp_never_probes_the_clock(settings_override):
    rig = Rig(settings_override, logins=['reject_totp'], kotak_totp='123456', kotak_totp_secret='')
    rig.probe_result = 99.0
    with pytest.raises(RuntimeError):
        rig.run(rig.k.session())
    assert rig.k.login_state()['clock_offset_sec'] is None


def test_clock_offset_from_the_date_header():
    server = datetime(2026, 10, 5, 4, 0, 30, tzinfo=IST).timestamp()

    def handler(request):
        return httpx.Response(200, headers={'Date': 'Sun, 04 Oct 2026 22:30:30 GMT'})
    off = asyncio.run(clock_offset('https://example.test', transport=httpx.MockTransport(handler), wall=lambda: server - 20))
    assert off == 20.5 and clock_status(off) == 'BAD' and 'behind' in clock_hint(off)
    assert clock_status(2.0) == 'OK' and clock_hint(2.0) is None and clock_status(None) == 'UNKNOWN'

    def nodate(request):
        return httpx.Response(200)
    assert asyncio.run(clock_offset('https://example.test', transport=httpx.MockTransport(nodate))) is None


# ------------------------------------------------------------------ health
def test_health_is_live_only_after_a_real_call_succeeds(settings_override):
    rig = Rig(settings_override, calls={'limits': [{'stat': 'Ok', 'data': {'Net': 1}}]})
    assert rig.run(rig.k.health())['status'] == 'NOT_LOGGED_IN' and not rig.clients  # health never logs in
    rig.run(rig.k.raw_margin())
    h = rig.run(rig.k.health())
    assert h['status'] == 'LIVE' and h['login']['last_ok_age_sec'] == 0
    rig.calls['limits'] = [FAULT]
    rig.wall[0] += 31  # older than 3 x BROKER_HEALTH_TTL_SEC: probe again
    h = rig.run(rig.k.health())
    assert h['status'] == 'SESSION_EXPIRED' and 'KOTAK_SESSION_EXPIRED' in h['error'] and len(rig.clients) == 1
    assert rig.run(rig.k.health())['status'] == 'SESSION_EXPIRED'  # reported, not repaired by health itself


def test_health_does_not_probe_while_calls_are_succeeding(settings_override):
    rig = Rig(settings_override, calls={'positions': [POSITIONS], 'limits': [AssertionError('probe not expected')]})
    rig.run(rig.k.raw_positions())
    rig.wall[0] += 5
    assert rig.run(rig.k.health())['status'] == 'LIVE'


# ------------------------------------------------------------------ pre-open login
def test_prelogin_runs_on_weekdays_after_the_configured_time(settings_override):
    rig = Rig(settings_override)
    tue = datetime(2026, 10, 6, 8, 49, tzinfo=IST)
    assert rig.run(rig.k.prelogin(tue)) == 'NOT_DUE' and not rig.clients
    assert rig.run(rig.k.prelogin(datetime(2026, 10, 10, 9, 0, tzinfo=IST))) == 'NOT_DUE'  # Saturday
    assert rig.run(rig.k.prelogin(tue.replace(minute=50))) == 'LOGGED_IN' and len(rig.clients) == 1
    assert rig.run(rig.k.prelogin(tue.replace(hour=9))) == 'FRESH' and len(rig.clients) == 1
    settings_override(kotak_prelogin_ist='')
    assert rig.run(rig.k.prelogin(tue.replace(hour=9))) == 'DISABLED'


# ------------------------------------------------------------------ orders
def test_place_on_a_dead_session_is_not_sent_and_drops_the_session(settings_override):
    rig = Rig(settings_override, calls={'place_order': [ApiErr(401)]}, live_trading=True)
    o = {'exchange': 'nse_fo', 'product': 'NRML', 'price': 10.0, 'order_type': 'LIMIT', 'qty': 75, 'symbol': 'NIFTY',
         'side': 'BUY', 'trigger_price': 0, 'client_order_id': 'C-1'}
    r = rig.run(rig.k.place(o))
    assert r == {'status': 'REJECTED', 'reason': 'NOT_SENT:KOTAK_SESSION', 'broker': 'KOTAK'}
    assert rig.k.login_state()['session_expired']
    rig.calls['place_order'] = [{'Error': 'Invalid JWT token'}]
    r = rig.run(rig.k.place(o))
    assert r['status'] == 'UNKNOWN' and rig.k.login_state()['session_expired']  # ambiguous text: never called REJECTED


def test_aclose_scrubs_the_session(settings_override):
    rig = Rig(settings_override, calls={'positions': [POSITIONS], 'logout': [None]})
    rig.run(rig.k.raw_positions())
    rig.run(rig.k.aclose())
    assert rig.k.peek_session() is None


# ------------------------------------------------------------------ login reply classification and guards
from app.brokers import kotak_mobile, login_reply_kind  # noqa: E402


@pytest.mark.parametrize('resp,kind', [
    ({'error': [{'code': '424', 'message': 'Invalid TOTP'}]}, 'REJECTED'),
    ({'error': [{'code': '401', 'message': 'Invalid MPIN'}]}, 'REJECTED'),
    ({'error': [{'code': '500', 'message': 'Internal Server Error'}]}, 'TRANSPORT'),
    ({'error': [{'code': '429', 'message': 'Too many requests'}]}, 'TRANSPORT'),
    ({'Error': 'Unexpected response format: <html>Service under maintenance</html>'}, 'TRANSPORT'),
    ({'fault': {'code': 900901, 'message': 'Invalid Credentials'}}, 'REJECTED'),
    ({'Error': 'Missing required field: mobile_number'}, 'CONFIG'),
    ({'stat': 'Not_Ok', 'emsg': 'something odd'}, 'REJECTED'),  # unknown counts: the lockout is the costlier mistake
    ('<html>', 'TRANSPORT'),
])
def test_login_reply_kind(resp, kind):
    assert login_reply_kind(resp) == kind


def test_maintenance_replies_back_off_but_never_halt(settings_override):
    rig = Rig(settings_override, kotak_login_max_rejections=2, kotak_login_transport_backoff_max_sec=1)
    maint = {'Error': 'Unexpected response format: <html>maintenance</html>'}

    class Down:
        def __init__(self, consumer_key, environment):
            rig.clients.append(self)

        def totp_login(self, **kw):
            return maint
    rig.k._factory = Down
    for _ in range(4):
        with pytest.raises(RuntimeError, match='KOTAK_LOGIN_TRANSPORT'):
            rig.run(rig.k.session())
        rig.mono[0] += 60
    st = rig.k.login_state()
    assert not st['halted'] and st['consecutive_rejections'] == 0 and len(rig.clients) == 4


@pytest.mark.parametrize('raw,want', [('+919876543210', '+919876543210'), ('9876543210', '+919876543210'),
                                      ('91 98765 43210', '+919876543210'), ('+91-98765-43210', '+919876543210'),
                                      ('+910000000000', None), ('12345', None), ('', None)])
def test_mobile_normalisation(raw, want):
    assert kotak_mobile(raw) == want


def test_bad_login_fields_are_config_errors_never_sent(settings_override):
    rig = Rig(settings_override, kotak_mpin='12 34')
    with pytest.raises(RuntimeError, match='KOTAK_CONFIG_INVALID:KOTAK_MPIN'):
        rig.run(rig.k.session())
    assert not rig.clients and rig.k.login_state()['consecutive_failures'] == 0


def test_login_sends_the_normalised_mobile(settings_override):
    rig = Rig(settings_override, kotak_mobile='98765 43210')
    seen = []
    orig = rig.k._factory

    def factory(consumer_key, environment):
        c = orig(consumer_key, environment)
        real = c.totp_login
        c.totp_login = lambda mobile_number, ucc, totp: (seen.append(mobile_number), real(mobile_number, ucc, totp))[1]
        return c
    rig.k._factory = factory
    rig.run(rig.k.session())
    assert seen == ['+919876543210']


def test_one_time_totp_is_used_once(settings_override):
    rig = Rig(settings_override, calls={'positions': [FAULT, POSITIONS]}, kotak_totp='123456', kotak_totp_secret='')
    with pytest.raises(RuntimeError, match='KOTAK_TOTP_SECRET_REQUIRED'):
        rig.run(rig.k.raw_positions())  # the session expired; the used code is never resent
    assert len(rig.clients) == 1


def test_relogin_budget_stops_a_login_storm(settings_override):
    rig = Rig(settings_override, calls={'positions': [FAULT]}, kotak_relogin_budget=2)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            rig.run(rig.k.raw_positions())
    assert len(rig.clients) == 3  # first login + 2 budgeted re-logins, then no more
    assert 'RELOGIN_BUDGET_EXHAUSTED' in rig.k.login_state()['last_error']
    rig.wall[0] += 901  # the budget refills by itself: the dead session is replaced again (once per call)
    with pytest.raises(RuntimeError):
        rig.run(rig.k.raw_positions())
    assert len(rig.clients) == 4


def test_totp_is_computed_on_kotak_time_when_the_clock_drifts(settings_override):
    from app.totp import totp
    rig = Rig(settings_override)
    rig.probe_result = 42.0  # this machine is 42 s behind Kotak
    rig.run(rig.k.session())
    code, _ = rig.codes[0]
    assert code == totp('JBSWY3DPEHPK3PXP', at=rig.wall[0] + 42.0) and rig.k.login_state()['clock_offset_sec'] == 42.0


def test_a_cancelled_caller_never_abandons_a_login(settings_override):
    rig = Rig(settings_override)
    gate = __import__('threading').Event()
    orig = rig.k._factory

    def slow(consumer_key, environment):
        c = orig(consumer_key, environment)
        real = c.totp_login
        c.totp_login = lambda **kw: (gate.wait(5), real(**kw))[1]
        return c
    rig.k._factory = slow

    async def go():
        first = asyncio.create_task(rig.k.session())
        await asyncio.sleep(0.05)
        assert (await rig.k.health())['status'] == 'LOGGING_IN'
        first.cancel()  # e.g. a manual feed reconnect cancels the connect in progress
        second = asyncio.create_task(rig.k.session())
        await asyncio.sleep(0.05)
        gate.set()
        c = await asyncio.wait_for(second, 5)
        return c
    c = rig.run(go())
    assert c is rig.k.peek_session() and len(rig.clients) == 1  # one login, shared; no second TOTP sent


def test_health_names_the_kind_of_login_problem(settings_override):
    rig = Rig(settings_override, logins=[ApiErr(0)])
    with pytest.raises(RuntimeError, match='KOTAK_LOGIN_TRANSPORT'):
        rig.run(rig.k.session())
    assert rig.run(rig.k.health())['status'] == 'UNREACHABLE'  # a network block, not a wrong password
    rig2 = Rig(settings_override, kotak_mobile='12345')
    with pytest.raises(RuntimeError, match='KOTAK_CONFIG_INVALID:KOTAK_MOBILE'):
        rig2.run(rig2.k.session())
    h = rig2.run(rig2.k.health())
    assert h['status'] == 'CONFIG_ERROR' and 'KOTAK_MOBILE' in h['error'] and '12345' not in h['error']
