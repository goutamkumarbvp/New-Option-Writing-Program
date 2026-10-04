"""Prometheus metrics and the daily refresher for static-URL instrument masters."""
import asyncio
import json
import re
from datetime import datetime, timedelta

import httpx
from conftest import angel_master_rows, expiry_in, make_tick
from starlette.testclient import TestClient

from app.clock import IST
from app.instrument_loader import StaticInstrumentRefresher, latest_publication
from app.instruments import InstrumentMaster
from app.main import create_app
from app.metrics import Exposition

SAMPLE = re.compile(r'^[a-zA-Z_:][a-zA-Z0-9_:]*(\{([a-zA-Z_][a-zA-Z0-9_]*="([^"\\\n]|\\.)*",?)*\})? -?[0-9.e+-]+(inf)?$')


def test_metrics_endpoint_is_valid_exposition(make_terminal):
    t, _ = make_terminal()
    t.feed.ingest(make_tick(age_ms=60000))  # one stale tick -> rejected counter
    r = TestClient(create_app(t, run_background=False)).get('/metrics')
    assert r.status_code == 200 and r.headers['content-type'].startswith('text/plain; version=0.0.4')
    seen, current = set(), None
    for line in r.text.strip().split('\n'):
        if line.startswith('# HELP '):
            name = line.split()[2]
            assert name not in seen, f'family {name} emitted twice'
            seen.add(name)
            current = name
        elif line.startswith('# TYPE '):
            assert line.split()[2] == current and line.split()[3] in ('gauge', 'counter')
        else:
            assert SAMPLE.match(line), line
            assert line.split('{')[0].split(' ')[0] == current, f'sample outside its family: {line}'
    body = r.text
    assert 'iort_info{version="3.1.0"} 1.0' in body and 'iort_kill_switch 0' in body and 'iort_live_trading 0' in body
    assert 'iort_ticks_rejected_total{reason="STALE_TICK"} 1.0' in body
    assert 'iort_broker_up{broker="ANGEL"} 1' in body and 'iort_risk_complete' in body


def test_label_values_are_escaped_and_unknowns_omitted():
    x = Exposition()
    x.add('m', 2, 'help', label='a"b\\c\nd')
    x.add('m', None, 'help', label='skip')
    assert x.render() == '# HELP m help\n# TYPE m gauge\nm{label="a\\"b\\\\c\\nd"} 2.0\n'


def test_order_status_counts(make_terminal):
    t, _ = make_terminal()
    assert t.ledger.order_status_counts() == {}


# ------------------------------------------------------ static instrument refresh
def at(h, m, day=0):
    return (datetime(2026, 10, 6, tzinfo=IST) + timedelta(days=day)).replace(hour=h, minute=m)


def test_latest_publication_follows_the_morning_cutoff(settings_override):
    assert latest_publication(at(7, 0)) == at(8, 30, -1) and latest_publication(at(9, 0)) == at(8, 30)
    settings_override(instrument_refresh_after_ist='09:15')
    assert latest_publication(at(9, 0)) == at(9, 15, -1)
    settings_override(instrument_refresh_after_ist='garbage')
    assert latest_publication(at(9, 0)) == at(8, 30)


class Server:
    def __init__(self):
        self.fail, self.hits = False, 0

    def __call__(self, request):
        self.hits += 1
        if self.fail:
            return httpx.Response(503, text='busy')
        return httpx.Response(200, text=json.dumps(angel_master_rows(expiry_in(7))))


def refresher(tmp_path, settings_override, now):
    settings_override(instrument_master_urls_json='{"ANGEL": "https://example/angel.json", "KOTAK": "https://example/kotak.csv"}',
                      kotak_api_key='key', kotak_instrument_master_auto=True)
    im, server, clock = InstrumentMaster(tmp_path / 'd'), Server(), [100.0]
    im.transport = httpx.MockTransport(server)
    return StaticInstrumentRefresher(im, clock=lambda: clock[0], now=lambda: now[0]), im, server, clock


def test_refresher_loads_missing_then_reloads_once_after_the_cutoff(tmp_path, settings_override):
    now = [datetime.now(IST).replace(hour=7, minute=0, second=0, microsecond=0)]
    r, im, server, _ = refresher(tmp_path, settings_override, now)
    assert set(r.urls()) == {'ANGEL'}  # Kotak is handled by the automatic loader
    assert asyncio.run(r.run_once()) == {'ANGEL': {'status': 'LOADED', 'count': 4}} and im.loaded('ANGEL')
    im.loaded_at['ANGEL'] = now[0].timestamp()  # loaded at 07:00 (yesterday's file at best)
    assert asyncio.run(r.run_once()) == {}
    now[0] = now[0].replace(hour=8, minute=45)
    assert asyncio.run(r.run_once())['ANGEL']['status'] == 'LOADED'
    im.loaded_at['ANGEL'] = now[0].timestamp()
    assert asyncio.run(r.run_once()) == {} and server.hits == 2


def test_refresher_backs_off_and_keeps_the_index(tmp_path, settings_override):
    now = [datetime.now(IST).replace(hour=10, minute=0, second=0, microsecond=0)]
    r, im, server, clock = refresher(tmp_path, settings_override, now)
    server.fail = True
    res = asyncio.run(r.run_once())['ANGEL']
    assert res['status'] == 'ERROR' and res['retry_in_sec'] == 60 and not im.loaded('ANGEL')
    assert asyncio.run(r.run_once()) == {} and server.hits == 1
    clock[0] += 60
    assert asyncio.run(r.run_once())['ANGEL']['retry_in_sec'] == 120
    server.fail, clock[0] = False, clock[0] + 120
    assert asyncio.run(r.run_once())['ANGEL']['status'] == 'LOADED' and r.status()['ANGEL']['consecutive_failures'] == 0
