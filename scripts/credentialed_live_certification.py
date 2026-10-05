#!/usr/bin/env python3
"""Credentialed, READ-ONLY broker certification harness.

Reads credentials from the environment / .env. Never prints secrets and never
places, modifies or cancels an order. For every configured broker it checks
authentication and that each read endpoint parses through the SAME normalisers
the risk engine uses. A FAIL on positions or margin means live risk will block
orders on that broker, which is the field-mapping evidence this harness exists
to collect (Kotak positions/limits/trades mappings are unverified until it passes).
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))


def load_dotenv(path=ROOT / '.env'):
    """Read .env with the terminal's own parser (backend/app/config.py), so this script sees exactly the
    values the terminal does. The process environment wins."""
    if not path.exists():
        return False
    import sys
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.config import parse_env_line
    for raw in path.read_text(encoding='utf-8-sig', errors='ignore').splitlines():
        kv = parse_env_line(raw)
        if kv:
            os.environ.setdefault(*kv)
    return True


async def check(label, fn):
    t = time.perf_counter()
    try:
        out = await fn()
        detail = {}
        if label == 'positions':
            rows, native = out
            detail = {'rows': len(rows), 'native_day_pnl': native}
        elif label == 'margin':
            detail = {'utilisation_pct': round(out['pct'], 2)}
        else:
            detail = {'rows': len(out)}
        return {'check': label, 'status': 'PASS', 'latency_ms': round((time.perf_counter() - t) * 1000, 1), **detail}
    except Exception as e:  # noqa: BLE001 - report type and message, never credentials
        return {'check': label, 'status': 'FAIL', 'error': type(e).__name__, 'message': str(e)[:160]}


async def main():
    env_loaded = load_dotenv()
    os.environ['LIVE_TRADING'] = 'false'  # belt and braces: adapters refuse to place when not live
    from app.brokers import BrokerRegistry
    from app.version import VERSION
    registry = BrokerRegistry()
    required = [x.strip().upper() for x in (os.getenv('CERT_REQUIRED_BROKERS') or os.getenv('ROUTING_BROKERS') or 'KOTAK').split(',') if x.strip()]
    results = []
    for name in required:
        if name not in registry.items:
            results.append({'broker': name, 'status': 'UNKNOWN_BROKER'})
            continue
        b = registry.get(name)
        health = await b.health()
        entry = {'broker': name, 'health': health, 'checks': []}
        if health.get('status') == 'LIVE':
            for label, fn in (('positions', b.positions), ('margin', b.margin), ('orders', b.orders), ('trades', b.trades)):
                entry['checks'].append(await check(label, fn))
        results.append(entry)
    await registry.aclose()
    passed = all(r.get('health', {}).get('status') == 'LIVE' and all(c['status'] == 'PASS' for c in r['checks']) for r in results)
    report = {
        'certificate_type': 'CREDENTIALED_READ_ONLY_BROKER_CONNECTIVITY_AND_FIELD_MAPPING',
        'application_version': VERSION,
        'timestamp_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'env_file_loaded': env_loaded,
        'required_brokers': required,
        'broker_results': results,
        'live_order_test_executed': False,
        'status': 'PASS' if passed else 'NOT_CERTIFIED',
        'honest_scope': 'Read-only. Does not certify execution, fills, slippage, kill-switch timing or live-money safety.',
    }
    out = Path(os.getenv('CERT_OUTPUT_DIR') or ROOT / 'reports')
    out.mkdir(exist_ok=True)
    (out / 'credentialed_live_certification.json').write_text(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(json.dumps(report, indent=2, default=str))
    return 0 if passed else 2


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
