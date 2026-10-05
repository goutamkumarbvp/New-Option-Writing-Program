#!/usr/bin/env python3
"""Guarded runner for the live-money gate (Gates A-C of scripts/CONTROLLED_LIVE_TEST.md).

Drives a RUNNING terminal over its HTTP API, so the evidence covers the deployed system.

Only `order-cancel` can reach a broker. It sends ONE BUY of ONE lot of the option you
name, as a LIMIT below the market inside the price collar (so it rests), then cancels it.
It needs --i-understand-real-orders AND typing the contract symbol at the prompt, and it
refuses outside NSE hours, above --max-premium, or when preflight fails. It never sells.
Every other step never reaches a broker: the terminal blocks those orders itself.

Evidence (no secrets) is appended to reports/live_gate_<IST date>.json. The operator token
comes from OPERATOR_API_TOKEN (environment or .env) and is never printed.

  python scripts/live_gate.py preflight      --broker KOTAK --token 48201
  python scripts/live_gate.py order-cancel   --broker KOTAK --token 48201 --i-understand-real-orders
  python scripts/live_gate.py kill-test      --broker KOTAK --token 48201
  python scripts/live_gate.py reject-test    --broker KOTAK --token 48201
  python scripts/live_gate.py recovery-arm   --broker KOTAK      # then restart the terminal
  python scripts/live_gate.py recovery-verify --broker KOTAK
  python scripts/live_gate.py report         --broker KOTAK
"""
import argparse
import asyncio
import json
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))
WORKING = ('SUBMITTED', 'OPEN', 'PARTIAL')
FINAL = ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED')
MANUAL_ITEMS = [
    'Broker app is logged in on another device for a manual emergency exit',
    'This machine\'s static IP is registered with the broker for API orders',
    'The contract is liquid, current and approved for the test',
    'The operator has read scripts/CONTROLLED_LIVE_TEST.md',
]


def load_dotenv(path=ROOT / '.env'):
    """Read .env with the terminal's own parser (backend/app/config.py), so this script sees exactly the
    values the terminal does. The process environment wins."""
    if not path.exists():
        return
    import sys
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.config import parse_env_line
    for raw in path.read_text(encoding='utf-8-sig', errors='ignore').splitlines():
        kv = parse_env_line(raw)
        if kv:
            os.environ.setdefault(*kv)


def nse_open(now):
    t = now.astimezone(IST)
    return t.weekday() < 5 and (9 * 60 + 15) <= t.hour * 60 + t.minute < (15 * 60 + 30)


def floor_tick(px, tick=0.05):
    return round(math.floor(px / tick + 1e-9) * tick, 2)


class Gate:
    def __init__(self, base_url, token, broker, out_dir=ROOT / 'reports', transport=None, now=None, confirm=input,
                 sleep=asyncio.sleep, poll_hook=None, poll_timeout=30.0, out=print):
        self.base_url, self.token, self.broker = base_url, token, broker.upper()
        self.out_dir, self.transport = Path(out_dir), transport
        self.now = now or (lambda: datetime.now(IST))
        self.confirm, self.sleep, self.poll_hook, self.poll_timeout, self.out = confirm, sleep, poll_hook, poll_timeout, out
        self.c = None

    # ---------------------------------------------------------------- plumbing
    async def __aenter__(self):
        headers = {'X-IORT-Operator-Token': self.token} if self.token else {}
        self.c = httpx.AsyncClient(base_url=self.base_url, transport=self.transport, timeout=20, headers=headers)
        return self

    async def __aexit__(self, *exc):
        await self.c.aclose()

    @property
    def evidence_path(self):
        return self.out_dir / f'live_gate_{self.now().astimezone(IST).date().isoformat()}.json'

    def load(self):
        p = self.evidence_path
        return json.loads(p.read_text()) if p.exists() else {'broker': self.broker, 'steps': [], 'manual_confirmations': {}}

    def record(self, step, status, **details):
        ev = self.load()
        ev['steps'].append({'step': step, 'status': status, 'at': self.now().astimezone(IST).isoformat(timespec='seconds'), **details})
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(json.dumps(ev, indent=2, sort_keys=True, default=str))
        self.out(f'[{status}] {step}' + (f"  {details.get('note', '')}" if details.get('note') else ''))
        return status

    async def state(self):
        r = await self.c.get('/dashboard/state')
        r.raise_for_status()
        return r.json()

    async def find_contract(self, token):
        idx = (await self.c.get('/option-chain')).json()
        for ch in idx.get('chains', []):
            for exp in ch['expiries']:
                lad = (await self.c.get(f"/option-chain/{ch['exchange']}/{ch['underlying']}/{exp}/ladder")).json()
                for row in lad['strikes']:
                    for typ in ('CE', 'PE'):
                        leg = row.get(typ)
                        if leg and str(leg['instrument_token']) == str(token) and leg['broker'] == self.broker:
                            return {'exchange': ch['exchange'], 'underlying': ch['underlying'], 'expiry': exp, 'strike': row['strike'],
                                    'option_type': typ, **{k: leg[k] for k in ('symbol', 'lot_size', 'bid', 'ask', 'ltp', 'stale',
                                                                                'instrument_token')}}
        return None

    async def poll(self, cid, done):
        deadline, o = time.monotonic() + self.poll_timeout, None
        while time.monotonic() < deadline:
            if self.poll_hook:
                await self.poll_hook()
            r = await self.c.get(f'/orders/{cid}')
            o = r.json().get('order') if r.status_code == 200 else None
            if o and done(o):
                return o
            await self.sleep(1)
        return o

    def gate_price(self, c, collar_bps):
        """A BUY limit that rests: below the bid, but inside the terminal's price collar."""
        lo = c['ltp'] * (1 - collar_bps / 10000)
        px = floor_tick(c['ltp'] * (1 - 0.8 * collar_bps / 10000))
        if c['bid'] and px >= c['bid']:
            px = floor_tick(c['bid'] - 0.05)
        return px if px >= max(lo, 0.05) else None

    def order_body(self, c, price, cid):
        return {'broker': self.broker, 'exchange': c['exchange'], 'symbol': c['symbol'], 'side': 'BUY', 'qty': c['lot_size'],
                'price': price, 'order_type': 'LIMIT', 'product': 'NRML', 'instrument_token': str(c['instrument_token']),
                'client_order_id': cid}

    def cid(self, kind):
        return f"GATE-{kind}-{self.now().astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    # ---------------------------------------------------------------- Gate A
    async def preflight(self, token=None, max_premium=2000.0, quiet=False):
        checks = []

        def chk(name, ok, detail=''):
            checks.append({'check': name, 'ok': bool(ok), 'detail': detail})
        try:
            health = (await self.c.get('/health')).json()
        except httpx.HTTPError as exc:
            chk('Terminal reachable', False, str(exc)[:120])
            return False, checks, None, None
        chk('Terminal reachable', True, 'version ' + str(health.get('version')))
        chk('Operator token available to this script', bool(self.token), 'set OPERATOR_API_TOKEN')
        if self.token:
            rec = await self.c.post(f'/reconcile/{self.broker}')
            body = rec.json() if rec.headers.get('content-type', '').startswith('application/json') else {}
            chk('Broker reconciliation matched', rec.status_code == 200 and body.get('ok'),
                body.get('error') or f"mismatches: {len(body.get('mismatches') or [])}" if rec.status_code == 200 else f'HTTP {rec.status_code}')
        st = await self.state()
        sysd = st['system']
        chk('Live trading enabled', sysd.get('live_trading'), 'LIVE_TRADING=true and LIVE_PRODUCTION_ACK set deliberately')
        chk('Production readiness', st['readiness']['ready'], ', '.join(st['readiness']['reasons']) or 'ready')
        b = next((x for x in st['brokers'] if x.get('broker') == self.broker), {})
        chk(f'{self.broker} authenticated', b.get('status') == 'LIVE', b.get('status', 'NOT CONFIGURED') + (' ' + b.get('error', '') if b.get('error') else ''))
        chk(f'{self.broker} feed live', self.broker in st['market']['stats'].get('live_brokers', []))
        inst = st['instruments'].get(self.broker) or {}
        chk(f'{self.broker} instrument master is today\'s', inst.get('fresh_today'), f"{inst.get('count', 0)} contracts, file date {inst.get('source_date')}")
        chk('Kill switch clear', not sysd.get('kill_switch'), (sysd.get('kill') or {}).get('reason') or '')
        amb = [o['client_order_id'] for o in st.get('exceptions', []) if o.get('broker') == self.broker]
        chk('No ambiguous orders', not amb, ', '.join(amb))
        snap = (st['risk']['snapshots'] or {}).get(self.broker) or {}
        chk('Broker risk snapshot usable', snap.get('ok'), snap.get('error') or ', '.join(snap.get('issues') or []) or 'no snapshot yet' if not snap.get('ok') else f"age {snap.get('age_sec')}s")
        chk('NSE session open (holidays not modelled)', nse_open(self.now()), self.now().astimezone(IST).strftime('%a %H:%M IST'))
        contract = None
        if token:
            contract = await self.find_contract(token)
            chk('Contract found in a live chain', contract is not None, str(token))
            if contract:
                chk('Two-sided fresh quote', contract['bid'] > 0 and contract['ask'] > 0 and not contract['stale'],
                    f"bid {contract['bid']} ask {contract['ask']}{' STALE' if contract['stale'] else ''}")
                chk('Lot size known', bool(contract['lot_size']), str(contract['lot_size']))
                premium = (contract['ask'] or 0) * (contract['lot_size'] or 0)
                chk(f'One lot costs at most Rs {max_premium:,.0f}', 0 < premium <= max_premium, f'~Rs {premium:,.2f} at the ask')
        ok = all(c['ok'] for c in checks)
        if not quiet:
            for c in checks:
                self.out(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['check']}" + (f"  ({c['detail']})" if c['detail'] else ''))
            self.out('  MANUAL ' + '\n  MANUAL '.join(MANUAL_ITEMS))
        self.record('A.preflight', 'PASS' if ok else 'FAIL', checks=checks, contract=contract)
        return ok, checks, contract, st

    # ---------------------------------------------------------------- Gate B
    async def order_cancel(self, token, acknowledged, max_premium=2000.0):
        if not acknowledged:
            return self.record('B.order_cancel', 'BLOCKED', note='--i-understand-real-orders not given; nothing sent')
        ok, _, c, st = await self.preflight(token, max_premium)
        if not ok or not c:
            return self.record('B.order_cancel', 'BLOCKED', note='preflight failed; nothing sent')
        price = self.gate_price(c, st['controls'].get('price_collar_bps') or 500)
        if price is None:
            return self.record('B.order_cancel', 'BLOCKED', note='no resting price below the bid inside the collar; pick a liquid contract')
        cost = price * c['lot_size']
        if cost > max_premium:
            return self.record('B.order_cancel', 'BLOCKED', note=f'order value Rs {cost:,.2f} above --max-premium')
        self.out(f"\nREAL ORDER: BUY {c['lot_size']} x {c['symbol']} LIMIT @ {price:.2f} on {self.broker} (max cost Rs {cost:,.2f}).")
        self.out('It rests below the bid and is cancelled immediately. If it fills you hold +1 lot; close it yourself.')
        if self.confirm(f"Type the contract symbol {c['symbol']} to send it: ").strip() != c['symbol']:
            return self.record('B.order_cancel', 'BLOCKED', note='confirmation did not match; nothing sent')
        cid = self.cid('B')
        body = self.order_body(c, price, cid)
        r = await self.c.post('/orders', json=body)
        sub = r.json()
        if sub.get('status') != 'SUBMITTED':
            return self.record('B.order_cancel', 'FAIL', note=f"not submitted: {sub.get('status')} {sub.get('reason', '')}", order=body, response=sub)
        ack = await self.poll(cid, lambda o: o['status'] in FINAL or (o['status'] in WORKING and o.get('broker_order_id')))
        self.record('B.acknowledged', 'PASS' if ack and ack.get('broker_order_id') else 'FAIL',
                    client_order_id=cid, broker_order_id=(ack or {}).get('broker_order_id'), order_status=(ack or {}).get('status'))
        final = ack
        if ack and ack['status'] in WORKING:
            cr = await self.c.post(f'/orders/{cid}/cancel')
            self.record('B.cancel_requested', 'PASS' if cr.status_code == 200 else 'FAIL', response=cr.json())
            final = await self.poll(cid, lambda o: o['status'] in FINAL)
        status = (final or {}).get('status')
        rec = (await self.c.post(f'/reconcile/{self.broker}')).json()
        self.record('B.reconciled', 'PASS' if rec.get('ok') else 'FAIL', mismatches=rec.get('mismatches'))
        if status == 'FILLED':
            return self.record('B.order_cancel', 'PASS', note='order FILLED: you now hold +1 lot; close it in the broker app or the terminal',
                               client_order_id=cid, final_status=status)
        good = status == 'CANCELLED' and rec.get('ok')
        return self.record('B.order_cancel', 'PASS' if good else 'FAIL', client_order_id=cid, final_status=status,
                           note='' if good else 'check the order in the broker app now')

    # ---------------------------------------------------------------- Gate C
    async def kill_test(self, token):
        c = await self.find_contract(token)
        if not c:
            return self.record('C.kill_switch', 'BLOCKED', note='contract not found in a live chain')
        k = await self.c.post('/kill', params={'reason': 'LIVE_GATE_KILL_TEST'})
        engaged = k.status_code == 200 and (await self.c.get('/health')).json().get('kill_switch')
        cid = self.cid('KILL')
        resp = (await self.c.post('/orders', json=self.order_body(c, floor_tick(c['ltp']), cid))).json()
        never_written = (await self.c.get(f'/orders/{cid}')).status_code == 404
        reset = await self.c.post('/kill/reset', params={'reason': 'live gate kill-switch test complete'})
        cleared = reset.status_code == 200 and not (await self.c.get('/health')).json().get('kill_switch')
        ok = engaged and resp.get('reason') == 'KILL_SWITCH' and never_written and cleared
        return self.record('C.kill_switch', 'PASS' if ok else 'FAIL', engaged=engaged, order_response=resp,
                           order_never_written=never_written, reset=cleared)

    async def reject_test(self, token):
        c = await self.find_contract(token)
        if not c:
            return self.record('C.reject_path', 'BLOCKED', note='contract not found in a live chain')
        cid = self.cid('REJ')
        resp = (await self.c.post('/orders', json=self.order_body(c, max(floor_tick(c['ltp'] * 0.5), 0.05), cid))).json()
        never_written = (await self.c.get(f'/orders/{cid}')).status_code == 404
        ok = resp.get('status') == 'BLOCKED' and resp.get('reason') == 'PRICE_COLLAR_BREACH' and never_written
        return self.record('C.reject_path', 'PASS' if ok else 'FAIL', order_response=resp, order_never_written=never_written,
                           note='' if ok else 'expected PRICE_COLLAR_BREACH (needs live trading on)')

    async def recovery_arm(self):
        r = await self.c.post('/kill', params={'reason': 'LIVE_GATE_RECOVERY'})
        st = self.record('C.recovery_armed', 'PASS' if r.status_code == 200 else 'FAIL',
                         note='now restart the terminal (docker compose restart terminal), then run recovery-verify')
        return st

    async def recovery_verify(self):
        kill = (await self.state())['system'].get('kill') or {}
        persisted = kill.get('active') and kill.get('reason') == 'LIVE_GATE_RECOVERY'
        reset = await self.c.post('/kill/reset', params={'reason': 'live gate recovery test complete'})
        cleared = reset.status_code == 200 and not (await self.c.get('/health')).json().get('kill_switch')
        return self.record('C.recovery', 'PASS' if persisted and cleared else 'FAIL', kill_persisted_across_restart=persisted,
                           reset=cleared, note='' if persisted else 'kill state did not survive the restart (or recovery-arm was not run)')

    def report(self):
        ev = self.load()
        last = {}
        for s in ev['steps']:
            last[s['step']] = s['status']
        gates = {'A': last.get('A.preflight'), 'B': last.get('B.order_cancel'),
                 'C': 'PASS' if all(last.get(k) == 'PASS' for k in ('C.kill_switch', 'C.reject_path', 'C.recovery')) else
                      ('INCOMPLETE' if not all(k in last for k in ('C.kill_switch', 'C.reject_path', 'C.recovery')) else 'FAIL')}
        ev['summary'] = {'gates': gates, 'automated_complete': all(v == 'PASS' for v in gates.values()),
                         'not_covered_here': ['partial fill path', 'WebSocket disconnect/reconnect', 'REST fallback',
                                              'stale data and watchdog failure under live load'],
                         'manual_confirmations_required': MANUAL_ITEMS, 'operator_signoff': {'name': None, 'date': None, 'signature': None}}
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_path.write_text(json.dumps(ev, indent=2, sort_keys=True, default=str))
        for g, v in gates.items():
            self.out(f'Gate {g}: {v or "NOT RUN"}')
        self.out(f'Evidence: {self.evidence_path}')
        return ev['summary']


async def main(argv=None):
    load_dotenv()
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('command', choices=['preflight', 'order-cancel', 'kill-test', 'reject-test', 'recovery-arm', 'recovery-verify', 'report'])
    p.add_argument('--broker', default='KOTAK')
    p.add_argument('--token', help='instrument token of a liquid option on that broker')
    p.add_argument('--url', default=os.getenv('IORT_URL', 'http://127.0.0.1:8000'))
    p.add_argument('--max-premium', type=float, default=2000.0, help='cap on the one-lot order value (Rs)')
    p.add_argument('--i-understand-real-orders', action='store_true', help='required for order-cancel')
    a = p.parse_args(argv)
    if a.command in ('order-cancel', 'kill-test', 'reject-test') and not a.token:
        p.error('--token is required')
    async with Gate(a.url, os.getenv('OPERATOR_API_TOKEN', ''), a.broker) as g:
        if a.command == 'preflight':
            ok = (await g.preflight(a.token, a.max_premium))[0]
            return 0 if ok else 2
        if a.command == 'report':
            return 0 if g.report()['automated_complete'] else 1
        run = {'order-cancel': lambda: g.order_cancel(a.token, a.i_understand_real_orders, a.max_premium),
               'kill-test': lambda: g.kill_test(a.token), 'reject-test': lambda: g.reject_test(a.token),
               'recovery-arm': g.recovery_arm, 'recovery-verify': g.recovery_verify}[a.command]
        return {'PASS': 0, 'BLOCKED': 2}.get(await run(), 1)


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
