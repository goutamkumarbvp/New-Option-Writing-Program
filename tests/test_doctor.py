"""Machine diagnostics and the static-IP order gate."""
import asyncio
from email.utils import formatdate

import httpx
import pytest
from conftest import TOKEN, override
from starlette.testclient import TestClient

from app.doctor import credential_checks, run_doctor
from app.main import create_app
from app.netcheck import PublicIP, static_ip_reasons

GOOD = dict(kotak_api_key='consumer-key-123', kotak_mobile='+919876543210', kotak_client_code='AB1234', kotak_mpin='123456',
            kotak_totp_secret='JBSWY3DPEHPK3PXP', kotak_totp='')


def kotak_up(request):
    if request.url.host == 'api.ipify.org':
        return httpx.Response(200, text='203.0.113.7')
    return httpx.Response(403, headers={'Date': formatdate(usegmt=True)})  # any HTTP answer proves reachability


def statuses(report):
    return {c['name']: c['status'] for c in report['checks']}


def test_credential_checks_report_format_never_values(settings_override):
    settings_override(**GOOD)
    out = credential_checks()
    assert {n: s for n, s, *_ in out} == {k: 'OK' for k in ('KOTAK_API_KEY', 'KOTAK_MOBILE', 'KOTAK_CLIENT_CODE', 'KOTAK_MPIN', 'KOTAK_TOTP_SECRET')}
    settings_override(kotak_mobile='9876543210', kotak_mpin='12345', kotak_totp_secret='nope!', kotak_api_key='')
    bad = {n: (s, d) for n, s, d, _ in credential_checks()}
    assert bad['KOTAK_MOBILE'][0] == bad['KOTAK_MPIN'][0] == bad['KOTAK_TOTP_SECRET'][0] == 'FAIL'
    assert bad['KOTAK_API_KEY'] == ('FAIL', 'not set')
    text = repr(out) + repr(bad)
    for secret in ('9876543210', '123456', 'JBSWY3DPEHPK3PXP', 'consumer-key-123', 'AB1234'):
        assert secret not in text  # no credential value ever appears in the report


def test_doctor_all_reachable(make_terminal, settings_override):
    settings_override(**GOOD, registered_static_ip='203.0.113.7')
    t, _ = make_terminal()

    async def probe():
        return 0.4
    r = asyncio.run(run_doctor(t, transport=httpx.MockTransport(kotak_up), probe_clock=probe))
    s = statuses(r)
    assert s['mis.kotaksecurities.com'] == s['sfeed.kotaksecurities.com'] == 'OK'
    assert s['PC clock vs Kotak'] == 'OK' and s['Public IP'] == 'OK' and s['Database'] == 'OK'
    assert s['Order routing'] == 'INFO' and r['summary'] in ('OK', 'WARN')


def test_doctor_blocked_network_and_drifted_clock(make_terminal, settings_override):
    settings_override(**GOOD, registered_static_ip='')
    t, _ = make_terminal()

    def blocked(request):
        raise httpx.ProxyError('403 Forbidden')

    async def probe():
        return -42.0
    r = asyncio.run(run_doctor(t, transport=httpx.MockTransport(blocked), probe_clock=probe))
    c = {x['name']: x for x in r['checks']}
    assert c['mis.kotaksecurities.com']['status'] == 'FAIL' and 'proxy' in c['mis.kotaksecurities.com']['detail']
    assert c['PC clock vs Kotak']['status'] == 'FAIL' and 'w32tm' in c['PC clock vs Kotak']['fix']
    assert c['Public IP']['status'] == 'WARN' and 'no static IP registered' in c['Public IP']['detail']
    assert not r['ok'] and r['summary'] == 'FAIL'


def test_static_ip_reasons(settings_override):
    settings_override(require_static_ip_match=True, registered_static_ip='')
    assert static_ip_reasons('1.2.3.4') == ['STATIC_IP_NOT_CONFIGURED']
    settings_override(registered_static_ip='203.0.113.7')
    assert static_ip_reasons(None) == ['PUBLIC_IP_UNKNOWN']
    assert static_ip_reasons('198.51.100.1') == ['PUBLIC_IP_NOT_REGISTERED']
    assert static_ip_reasons('203.0.113.7') == []
    settings_override(require_static_ip_match=False)
    assert static_ip_reasons(None) == []


def test_public_ip_is_cached_and_validated():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, text='203.0.113.7\n' if len(calls) == 1 else '<html>error</html>')
    now = [0.0]
    p = PublicIP(ttl=300, transport=httpx.MockTransport(handler), clock=lambda: now[0] + 1)
    assert asyncio.run(p.get()) == '203.0.113.7' and asyncio.run(p.get()) == '203.0.113.7' and len(calls) == 1
    now[0] += 301
    assert asyncio.run(p.get()) is None and p.error  # not an IP address: unknown, never guessed


def test_live_readiness_blocks_orders_from_an_unregistered_ip(make_terminal, live, settings_override):
    settings_override(require_static_ip_match=True, registered_static_ip='203.0.113.7')
    t, _ = make_terminal()

    async def other_ip(force=False):
        return '198.51.100.1'
    t.readiness.public_ip.get = other_ip
    r = asyncio.run(t.readiness.check())
    assert 'PUBLIC_IP_NOT_REGISTERED' in r['reasons'] and not r['ready']
    settings_override(registered_static_ip='')
    assert 'STATIC_IP_NOT_CONFIGURED' in asyncio.run(t.readiness.check())['reasons']


def test_doctor_endpoint(make_terminal, settings_override, monkeypatch):
    settings_override(**GOOD)
    t, _ = make_terminal()

    async def fake(t_, public_ip=None, **kw):
        return {'ok': True, 'summary': 'OK', 'checks': [], 'took_sec': 0.0}
    monkeypatch.setattr('app.main.run_doctor', fake)
    with override(operator_api_token=TOKEN):
        assert TestClient(create_app(t, run_background=False)).get('/ops/doctor').json()['summary'] == 'OK'


@pytest.mark.parametrize('flag', ['--help'])
def test_doctor_cli_help(flag, capsys):
    from app.doctor import main
    with pytest.raises(SystemExit):
        main([flag])
    assert '--login' in capsys.readouterr().out
