#!/usr/bin/env python3
"""Credentialed, read-only broker certification harness.

Reads credentials from the local environment/.env. It never prints secrets and never
places/modifies/cancels an order. A certificate is produced only when every required
broker authenticates and the read-only account endpoints are reachable.
"""
import asyncio, json, os, re, sys, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKEND=ROOT/'backend'
sys.path.insert(0,str(BACKEND))

def load_dotenv(path=ROOT/'.env'):
    if not path.exists(): return False
    for raw in path.read_text(errors='ignore').splitlines():
        line=raw.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1); k=k.strip(); v=v.strip()
        if len(v)>=2 and v[0]==v[-1] and v[0] in "'\"": v=v[1:-1]
        os.environ.setdefault(k,v)
    return True

def redact(v):
    if not v: return ''
    return v[:2]+'***'+v[-2:] if len(v)>4 else '***'

async def main():
    env_loaded=load_dotenv()
    from app.brokers import BrokerRegistry
    from app.config import settings
    from app.version import VERSION
    registry=BrokerRegistry()
    required=[x.strip().upper() for x in os.getenv('CERT_REQUIRED_BROKERS','ZERODHA,UPSTOX,ANGEL,KOTAK').split(',') if x.strip()]
    results=[]
    for name in required:
        if name not in registry.items:
            results.append({'broker':name,'status':'UNKNOWN_BROKER'}); continue
        started=time.perf_counter()
        try:
            h=await registry.get(name).health()
            results.append({'broker':name,'health':h,'latency_ms':round((time.perf_counter()-started)*1000,1)})
        except Exception as e:
            results.append({'broker':name,'health':{'status':'ERROR','error':type(e).__name__},'latency_ms':round((time.perf_counter()-started)*1000,1)})
    # Read-only account endpoint smoke tests. No orders are submitted.
    account=[]
    for name in required:
        item=registry.get(name)
        try:
            h=next(x for x in results if x['broker']==name).get('health',{})
            if h.get('status')!='LIVE':
                account.append({'broker':name,'status':'SKIPPED','reason':'AUTH_NOT_LIVE'}); continue
            for label,fn in [('positions',item.positions),('orders',item.orders),('trades',item.trades),('margin',item.margin)]:
                t=time.perf_counter(); out=await fn()
                account.append({'broker':name,'check':label,'status':'PASS','latency_ms':round((time.perf_counter()-t)*1000,1),'response_type':type(out).__name__})
        except Exception as e:
            account.append({'broker':name,'status':'FAIL','check':'account_read','error':type(e).__name__})
    all_auth=all(x.get('health',{}).get('status')=='LIVE' for x in results)
    all_account=all(x['status'] in ('PASS','SKIPPED') and not (x['status']=='FAIL') for x in account)
    report={
      'certificate_type':'CREDENTIALED_READ_ONLY_BROKER_CONNECTIVITY',
      'application_version':VERSION,
      'timestamp_utc':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),
      'env_file_loaded':env_loaded,
      'required_brokers':required,
      'broker_results':results,
      'account_smoke_tests':account,
      'live_order_test_executed':False,
      'status':'PASS' if all_auth and all_account else 'NOT_CERTIFIED',
      'honest_scope':'This certificate does not certify exchange execution, fills, slippage, kill-switch timing, broker-side risk controls, or live-money safety. Those require a controlled live test.'
    }
    out=ROOT/'reports'; out.mkdir(exist_ok=True)
    (out/'credentialed_live_certification.json').write_text(json.dumps(report,indent=2,sort_keys=True))
    print(json.dumps(report,indent=2))
    return 0 if report['status']=='PASS' else 2

if __name__=='__main__': raise SystemExit(asyncio.run(main()))
