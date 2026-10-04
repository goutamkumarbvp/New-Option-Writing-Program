"""Keeps the option strikes around spot subscribed on the Kotak stream.

Option contract tokens change every expiry, so a fixed SUBSCRIPTION_JSON cannot follow
the chain. For each underlying in KOTAK_AUTO_CHAIN_JSON ({"NIFTY": {"expiries": 2,
"strikes": 15}}) the subscriber takes the nearest unexpired expiries from the daily
instrument master and the CE/PE strikes within +/-N of spot, adds every open position's
token (risk valuation needs a live price for each), and hands the set to the Kotak stream
worker, which subscribes the difference on the live connection.

- Hysteresis: a subscribed strike stays until it is more than N + BAND strikes from ATM,
  so spot hovering at a strike boundary does not churn subscriptions.
- Centre: live index/cash tick, else a stale one, else put-call parity from chain ticks,
  else (bootstrap only) the median listed strike. The source is reported.
- Cap: KOTAK_AUTO_CHAIN_MAX_TOKENS; positions are kept first, then strikes nearest ATM.
"""
import asyncio
import json
import time

from .clock import expired
from .config import settings
from .option_chain import resolve_spot

BAND = 3
_SEGMENT = {'NFO': 'nse_fo', 'BFO': 'bse_fo', 'MCX': 'mcx_fo', 'NSE': 'nse_cm', 'BSE': 'bse_cm'}


def auto_chain_config():
    """Validated {UNDERLYING: {'expiries': 1..6, 'strikes': 1..60}}; malformed entries are skipped."""
    try:
        cfg = json.loads(settings.kotak_auto_chain_json or '{}')
    except ValueError:
        return {}
    out = {}
    for und, v in (cfg.items() if isinstance(cfg, dict) else []):
        v = v if isinstance(v, dict) else {}
        try:
            out[str(und).strip().upper()] = {'expiries': max(1, min(int(v.get('expiries', 1)), 6)),
                                             'strikes': max(1, min(int(v.get('strikes', 15)), 60))}
        except (TypeError, ValueError):
            continue
    return out


def segment_of(rec):
    return _SEGMENT.get(str(rec.get('exchange') or '').upper())


def plan_chain(records, underlying, spot, expiries, strikes, current=frozenset(), now=None):
    """Tokens to subscribe for one underlying.

    Returns ({(segment, token): distance_in_strikes_from_atm}, {'expiries': [...], 'atm': {expiry: strike}}).
    """
    u = str(underlying).upper()
    opts = [r for r in records if str(r.get('underlying') or '').upper() == u and r.get('option_type') in ('CE', 'PE')
            and r.get('expiry') and r.get('strike') and segment_of(r) and not expired(r['expiry'], now)]
    exps = sorted({r['expiry'] for r in opts})[:expiries]
    out, atms = {}, {}
    for e in exps:
        legs = [r for r in opts if r['expiry'] == e]
        ks = sorted({r['strike'] for r in legs})
        i = min(range(len(ks)), key=lambda j: (abs(ks[j] - spot), ks[j]))
        atms[e] = ks[i]
        pos = {k: j for j, k in enumerate(ks)}
        for r in legs:
            pair, dist = (segment_of(r), str(r['token'])), abs(pos[r['strike']] - i)
            if dist <= strikes or (dist <= strikes + BAND and pair in current):
                out[pair] = min(dist, out.get(pair, dist))
    return out, {'expiries': exps, 'atm': atms}


def center_spot(feed, chain, underlying, records=()):
    """(spot, source) used to centre the strike window, or (None, None)."""
    t = feed.live_spot(underlying)
    if t:
        return t.ltp, 'LIVE_SPOT'
    t = feed.live_spot(underlying, fresh_only=False)
    if t:
        return t.ltp, 'LAST_SPOT'
    for (ex, und, exp) in list(chain.data):
        if str(und).upper() == str(underlying).upper() and not expired(exp):
            s = resolve_spot(feed, und, exp, chain.snapshot(ex, und, exp))
            if s.get('value') and s.get('source') == 'PUT_CALL_PARITY':
                return s['value'], 'PUT_CALL_PARITY'
    ks = sorted({r['strike'] for r in records if str(r.get('underlying') or '').upper() == str(underlying).upper()
                 and r.get('option_type') in ('CE', 'PE') and r.get('strike')})
    if ks:
        return ks[len(ks) // 2], 'MEDIAN_STRIKE'
    return None, None


class ChainSubscriber:
    broker_name = 'KOTAK'

    def __init__(self, instruments, feed, chain, stream_manager_provider, risk_monitor=None, ledger=None):
        self.instruments = instruments
        self.feed = feed
        self.chain = chain
        self.stream_manager_provider = stream_manager_provider
        self.risk_monitor = risk_monitor
        self.ledger = ledger
        self.stop = False
        self.state = {'enabled': False, 'underlyings': {}, 'subscribed': 0, 'positions': 0, 'capped': False,
                      'last_change': None, 'error': None, 'at': None}

    def worker(self):
        sm = self.stream_manager_provider() if self.stream_manager_provider else None
        return next((w for w in getattr(sm, 'workers', []) if w.broker == self.broker_name and hasattr(w, 'set_dynamic')), None)

    def position_pairs(self):
        snap = (self.risk_monitor.snapshots.get(self.broker_name) if self.risk_monitor else None)
        out = set()
        for p in (snap.positions if snap else []):
            if p.get('qty'):
                rec = self.instruments.get(self.broker_name, p['token']) or {}
                seg = segment_of(rec) or _SEGMENT.get(str(p.get('exchange') or '').upper())
                if seg:
                    out.add((seg, str(p['token'])))
        return out

    def plan(self, cfg, current):
        records = list(self.instruments.index.get(self.broker_name, {}).values())
        ranked, info = {}, {}
        for und, c in cfg.items():
            spot, source = center_spot(self.feed, self.chain, und, records)
            if spot is None:
                info[und] = {'status': 'NO_CONTRACTS_OR_SPOT', 'tokens': 0}
                continue
            pairs, meta = plan_chain(records, und, spot, c['expiries'], c['strikes'], current)
            for pair, dist in pairs.items():
                ranked[pair] = min(dist + 1, ranked.get(pair, dist + 1))
            info[und] = {'status': 'OK' if pairs else 'NO_CONTRACTS', 'spot': spot, 'spot_source': source,
                         'expiries': meta['expiries'], 'atm': meta['atm'], 'tokens': len(pairs)}
        positions = self.position_pairs()
        for pair in positions:
            ranked[pair] = 0  # open positions first: exposure valuation needs their live prices
        cap = max(1, settings.kotak_auto_chain_max_tokens)
        chosen = sorted(ranked, key=lambda p: (ranked[p], p))[:cap]
        return set(chosen), info, len(positions), len(ranked) > cap

    async def run_once(self):
        cfg = auto_chain_config()
        self.state['enabled'] = bool(cfg)
        if not cfg:
            return None
        w = self.worker()
        if w is None:
            self.state.update(error='KOTAK_STREAM_NOT_RUNNING', at=time.time())
            return None
        desired, info, npos, capped = self.plan(cfg, frozenset(w.dynamic))
        try:
            change = await w.set_dynamic(desired)
            error = None
        except Exception as exc:  # noqa: BLE001 - retried next cycle; the worker keeps what it has
            change, error = None, str(exc)[:200]
            if self.ledger:
                self.ledger.event('CHAIN_SUBSCRIBE_ERROR', {'error': error})
        self.state.update(underlyings=info, subscribed=len(w.dynamic), positions=npos, capped=capped, error=error,
                          at=time.time(), last_change=change if change and (change['added'] or change['removed'])
                          else self.state['last_change'])
        return change

    async def run(self):
        while not self.stop:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop must survive
                self.state['error'] = str(exc)[:200]
                if self.ledger:
                    self.ledger.event('CHAIN_SUBSCRIBER_LOOP_ERROR', {'error': str(exc)[:200]})
            await asyncio.sleep(settings.kotak_auto_chain_interval_sec)
