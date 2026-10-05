"""Machine and login diagnostics for the operator's PC ("doctor").

Each check says OK, WARN, FAIL or INFO, what it found, and how to fix it. Credentials are checked for
presence and format only: no value is ever printed or returned. Run it from the System tab
(GET /ops/doctor), or on the machine:

    docker compose exec terminal python -m app.doctor           # read-only checks of the running terminal
    docker compose exec terminal python -m app.doctor --login   # plus one Kotak login, through the running terminal
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from .brokers import MPIN_RE, UCC_RE, kotak_mobile, totp_secret_valid
from .clockcheck import clock_hint, clock_offset, clock_status
from .config import DOTENV_FILE, settings
from .netcheck import KOTAK_HOSTS, PublicIP, host_reachable

RANK = {'OK': 0, 'INFO': 0, 'WARN': 1, 'FAIL': 2}


def credential_checks():
    """(name, status, detail, fix) for each Kotak login setting, judged by the same rules the login itself
    applies (brokers.kotak_login_fields), so the two can never disagree. Values are never included."""
    out = []

    def item(key, ok, why, fix):
        value = getattr(settings, key.lower())
        if not value:
            out.append((key, 'FAIL', 'not set', fix))
        elif ok(value):
            out.append((key, 'OK', 'set, format looks right', None))
        else:
            out.append((key, 'FAIL', f'set, but {why}', fix))

    item('KOTAK_API_KEY', lambda v: len(v.strip()) >= 8 and not re.search(r'\s', v.strip()), 'not a consumer key',
         'Copy the consumer key from Kotak Neo > Trade API into section 1 of .env.')
    item('KOTAK_MOBILE', lambda v: kotak_mobile(v) is not None, 'not an Indian mobile number (+91, then 10 digits starting 6-9)',
         'Write the registered mobile as +919876543210.')
    item('KOTAK_CLIENT_CODE', lambda v: UCC_RE.fullmatch(v.strip()) is not None, 'not a client code (UCC)',
         'Use your Kotak client code (UCC), letters and digits only.')
    item('KOTAK_MPIN', lambda v: MPIN_RE.fullmatch(v.strip()) is not None, 'not digits only', 'Use the 6-digit MPIN of the Kotak Neo app.')
    if settings.kotak_totp_secret:
        ok = totp_secret_valid(settings.kotak_totp_secret)
        out.append(('KOTAK_TOTP_SECRET', 'OK' if ok else 'FAIL', 'set, valid base32' if ok else 'set, but not valid base32',
                    None if ok else 'Paste the base32 text behind the TOTP QR code (letters A-Z, digits 2-7).'))
    elif settings.kotak_totp:
        out.append(('KOTAK_TOTP_SECRET', 'WARN', 'not set; only a one-time KOTAK_TOTP code is configured',
                    'Set KOTAK_TOTP_SECRET so the terminal can log in again by itself every day and after a reconnect.'))
    else:
        out.append(('KOTAK_TOTP_SECRET', 'FAIL', 'not set (and no KOTAK_TOTP)', 'Set KOTAK_TOTP_SECRET in section 1 of .env.'))
    return out


async def run_doctor(t=None, login=False, transport=None, public_ip=None, probe_clock=None):
    started = time.perf_counter()
    checks = []

    def add(group, name, status, detail, fix=None):
        checks.append({'group': group, 'name': name, 'status': status, 'detail': detail, 'fix': fix})

    add('Config', '.env file', 'INFO', f'read {DOTENV_FILE}' if DOTENV_FILE else 'process environment only (Docker passes .env to the container)')
    for name, status, detail, fix in credential_checks():
        add('Kotak login settings', name, status, detail, fix)

    hosts = await asyncio.gather(*(host_reachable(h, transport=transport) for h in KOTAK_HOSTS))
    offset_task = probe_clock() if probe_clock else clock_offset(transport=transport)
    ip_getter = public_ip or PublicIP(ttl=0, transport=transport)
    offset, ip = await asyncio.gather(offset_task, ip_getter.get(force=True))

    for h, (ok, detail) in zip(KOTAK_HOSTS, hosts):
        add('Network', h, 'OK' if ok else 'FAIL', detail,
            None if ok else 'Allow outbound HTTPS (443) to *.kotaksecurities.com in the firewall, antivirus or proxy.')
    st = clock_status(offset)
    add('Clock', 'PC clock vs Kotak', {'OK': 'OK', 'WARN': 'WARN', 'BAD': 'FAIL', 'UNKNOWN': 'WARN'}[st],
        'unknown (Kotak not reachable)' if offset is None else f'{offset:+.1f} s',
        clock_hint(offset) or (None if offset is not None else 'Fix the network check first.'))
    want = settings.registered_static_ip.strip()
    gate = '' if settings.require_static_ip_match else ' (REQUIRE_STATIC_IP_MATCH=false: orders are not blocked on it)'
    if not want:
        add('Static IP', 'Public IP', 'WARN' if not settings.live_trading else 'FAIL',
            f'this PC is {ip or "unknown"}; no static IP registered yet (live data works; orders need one){gate}',
            'Get a static IP from your ISP, register it in Kotak Neo > Trade API, then set REGISTERED_STATIC_IP.')
    elif ip == want:
        add('Static IP', 'Public IP', 'OK', f'{ip} matches REGISTERED_STATIC_IP')
    else:
        add('Static IP', 'Public IP', 'FAIL' if settings.live_trading else 'WARN',
            f'this PC is {ip or "unknown"}, registered is {want}{gate}',
            'Connect through the registered line, or register this IP with Kotak and update REGISTERED_STATIC_IP.')

    if t is not None:
        try:
            counts = await asyncio.to_thread(t.ledger.snapshot)
            add('Infrastructure', 'Database', 'OK', f'{counts.get("orders", 0)} orders, {counts.get("audit_records", 0)} audit records')
        except Exception as exc:  # noqa: BLE001
            add('Infrastructure', 'Database', 'FAIL', f'{type(exc).__name__}: {str(exc)[:120]}',
                'Start Postgres (docker compose up -d postgres) and check DATABASE_URL and POSTGRES_PASSWORD.')
        bus = t.bus.status() if hasattr(t.bus, 'status') else {}
        if bus.get('required'):
            add('Infrastructure', 'Redis', 'OK' if bus.get('connected') else 'FAIL',
                'connected' if bus.get('connected') else f'not connected ({bus.get("last_error") or "unknown"}); retried every 5 s',
                None if bus.get('connected') else 'Start Redis (docker compose up -d redis) and check REDIS_URL.')
        inst = t.instruments.summary().get('KOTAK') or {}
        add('Market data', 'Kotak instrument master', 'OK' if inst.get('fresh_today') else 'WARN',
            f'{inst.get("count", 0)} contracts, file date {inst.get("source_date") or "—"}' if inst else 'not loaded yet',
            None if inst.get('fresh_today') else 'Needs KOTAK_API_KEY and network access; it loads by itself every day.')
        k = t.brokers.items.get('KOTAK')
        if k is not None and hasattr(k, 'login_state'):
            if login and k.configured():
                # Through the running adapter: halt, backoff, TOTP-window tracking and the re-login budget apply.
                # A working session is proof enough; no extra login is sent just to test.
                try:
                    if k._fresh():
                        add('Kotak login', 'Login test', 'OK', f'already logged in ({k.login_state()["session_age_sec"]} s ago); no extra login sent')
                    else:
                        await k.session()
                        add('Kotak login', 'Login test', 'OK', 'logged in now')
                except Exception as exc:  # noqa: BLE001
                    add('Kotak login', 'Login test', 'FAIL', str(exc)[:200], 'See the detail; fix the setting it names, then Reset KOTAK login.')
            ls = k.login_state()
            if ls['halted']:
                add('Kotak login', 'Automatic login', 'FAIL', f'halted after {ls["consecutive_rejections"]} rejections: {ls["last_error"]}',
                    'Fix the credentials, then press Reset KOTAK login on the Overview tab.')
            elif ls['logged_in']:
                add('Kotak login', 'Session', 'OK', f'logged in {ls["session_age_sec"]} s ago; last good call {ls["last_ok_age_sec"]} s ago')
            else:
                add('Kotak login', 'Session', 'WARN' if ls['last_error'] else 'INFO', ls['last_error'] or 'not logged in yet',
                    'Check the login settings and network above.' if ls['last_error'] else None)
        sm = getattr(t, 'stream_manager', None)
        for w in (sm.status() if sm else []):
            ok = w['state'] == 'CONNECTED'
            add('Market data', f'{w["broker"]} feed', 'OK' if ok else 'WARN', w['state'] + (f' · {w["last_error"]}' if w.get('last_error') else ''),
                None if ok else 'Market tab > Reconnect now. It also reconnects by itself.')
        if sm is not None and not sm.workers:
            add('Market data', 'Feed', 'WARN', 'no feed configured', 'Set the Kotak login settings and SUBSCRIPTION_JSON or KOTAK_AUTO_CHAIN_JSON.')
    add('Orders', 'Order routing', 'INFO' if not settings.live_trading else 'WARN',
        'LOCKED (LIVE_TRADING=false): every order is refused before any broker' if not settings.live_trading
        else 'LIVE: real orders can be sent after every pre-trade control')

    worst = max((RANK[c['status']] for c in checks), default=0)
    return {'ok': worst < 2, 'summary': ['OK', 'WARN', 'FAIL'][worst], 'checks': checks,
            'generated_at': datetime.now(timezone.utc).isoformat(), 'took_sec': round(time.perf_counter() - started, 2)}


def _print(report):
    mark = {'OK': '  OK ', 'INFO': ' INFO', 'WARN': ' WARN', 'FAIL': ' FAIL'}
    group = None
    for c in report['checks']:
        if c['group'] != group:
            group = c['group']
            print(f'\n{group}')
        print(f'{mark[c["status"]]}  {c["name"]}: {c["detail"]}')
        if c['fix'] and c['status'] in ('WARN', 'FAIL'):
            print(f'        fix: {c["fix"]}')
    print(f'\nResult: {report["summary"]}  ({report["took_sec"]} s)')


def _from_running_terminal(login):
    """The report from the running terminal (its own login, feed and halt state), or None if it is not up.
    The login test always goes through it, so its lockout protection applies."""
    import httpx
    url = os.getenv('IORT_URL', 'http://127.0.0.1:8000').rstrip('/')
    try:
        if login:
            r = httpx.post(f'{url}/ops/doctor/login', headers={'X-IORT-Operator-Token': settings.operator_api_token}, timeout=90)
        else:
            r = httpx.get(f'{url}/ops/doctor', timeout=60)
    except httpx.HTTPError:
        return None
    if r.status_code == 401:
        raise SystemExit('The terminal refused the login test: OPERATOR_API_TOKEN does not match the running terminal.')
    return r.json() if r.status_code == 200 else None


def main(argv=None):
    ap = argparse.ArgumentParser(description='IORT machine and Kotak login diagnostics')
    ap.add_argument('--login', action='store_true', help='also perform one real Kotak login through the running terminal')
    ap.add_argument('--json', action='store_true', help='print the report as JSON')
    args = ap.parse_args(argv)
    report = _from_running_terminal(args.login)
    if report is None and args.login:
        print('The terminal is not running: start it first (deploy\\windows\\start.ps1), so the login test respects its lockout protection.')
        return 2
    if report is None:
        report = asyncio.run(_local_report())
    print(json.dumps(report, indent=2)) if args.json else _print(report)
    return 0 if report['ok'] else 1


async def _local_report():
    """Machine checks when the terminal itself is not running. A database that is down is reported,
    never allowed to stop the other checks."""
    from .main import Terminal
    try:
        t = Terminal()
    except Exception as exc:  # noqa: BLE001 - e.g. Postgres unreachable while building the ledger
        r = await run_doctor(None)
        r['checks'].insert(0, {'group': 'Infrastructure', 'name': 'Database', 'status': 'FAIL',
                               'detail': f'{type(exc).__name__}: {str(exc)[:160]}',
                               'fix': 'Start Postgres (docker compose up -d postgres) and check DATABASE_URL and POSTGRES_PASSWORD.'})
        r['ok'], r['summary'] = False, 'FAIL'
        return r
    await t.bus.connect()
    try:
        r = await run_doctor(t)
        r['checks'].insert(0, {'group': 'Config', 'name': 'Terminal', 'status': 'WARN', 'detail': 'not running: login and feed state unknown',
                               'fix': 'Start it with deploy\\windows\\start.ps1 (or docker compose up -d).'})
        return r
    finally:
        await t.brokers.aclose()
        await t.bus.close()


if __name__ == '__main__':
    sys.exit(main())
