"""Automatic Kotak instrument-master loading."""
import asyncio
import csv
import io
from datetime import date, datetime, timedelta

import httpx
import pytest
from conftest import TOKEN, FakeBroker, expiry_in, override
from starlette.testclient import TestClient

from app.brokers import BrokerRegistry, Kotak
from app.clock import IST, trading_date
from app.instrument_loader import KotakInstrumentLoader, files_date, select_segment_files
from app.instruments import KOTAK_FO_EPOCH_OFFSET, InstrumentMaster
from app.main import create_app

BASE = 'https://lapi.kotaksecurities.com/wso2-scripmaster/v1/prod'
HEADER = ['pSymbol', 'pGroup', 'pExchSeg', 'pInstType', 'pSymbolName ', 'pTrdSymbol', 'pOptionType', ' pExpiryDate',
          'dStrikePrice;', 'lLotSize']


def file_urls(day, segments=('nse_cm', 'nse_fo', 'bse_fo', 'mcx_fo')):
    urls = []
    for seg in segments:
        urls += [f'{BASE}/{day}/transformed-v1/{seg}-v1.csv', f'{BASE}/{day}/transformed/{seg}.csv']
    return urls


def csv_text(rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    w.writerows(rows)
    return buf.getvalue()


def nse_fo_rows():
    d = date.fromisoformat(expiry_in(7))
    raw = int(datetime(d.year, d.month, d.day, 14, 30, tzinfo=IST).timestamp()) - KOTAK_FO_EPOCH_OFFSET
    wk = f"NIFTY{d:%y}{'123456789OND'[d.month - 1]}{d:%d}"
    return [['48201', '', 'nse_fo', 'OPTIDX', 'NIFTY', wk + '25000CE', 'CE', raw, '2500000', '75'],
            ['48202', '', 'nse_fo', 'OPTIDX', 'NIFTY', wk + '25000PE', 'PE', raw, '2500000', '75']], d.isoformat()


NSE_CM = csv_text([['2885', 'EQ', 'nse_cm', '', 'RELIANCE', 'RELIANCE-EQ', '', '', '-1', '1']])


class Server:
    """httpx MockTransport serving scrip-master CSVs; paths in `fail` return 404."""

    def __init__(self, fail=()):
        rows, self.expiry = nse_fo_rows()
        self.files = {'nse_cm.csv': NSE_CM, 'nse_fo.csv': csv_text(rows)}
        self.fail = set(fail)
        self.hits = []

    def __call__(self, request):
        name = request.url.path.rsplit('/', 1)[-1]
        self.hits.append(name)
        if name in self.fail or name not in self.files:
            return httpx.Response(404, text='missing')
        return httpx.Response(200, text=self.files[name])


class FakeKotak(FakeBroker):
    def __init__(self, days=None):
        super().__init__('KOTAK')
        self.days = days or [trading_date()]
        self.calls = 0
        self.exc = None

    async def scrip_master_files(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return file_urls(self.days[min(self.calls, len(self.days)) - 1])


def make_loader(tmp_path, settings_override, broker=None, server=None, **extra):
    settings_override(**{'kotak_api_key': 'key', 'kotak_instrument_master_auto': True, 'kotak_scrip_segments': 'nse_cm,nse_fo', **extra})
    im = InstrumentMaster(tmp_path / 'data')
    server = server or Server()
    im.transport = httpx.MockTransport(server)
    broker = broker or FakeKotak()
    events, now = [], [1000.0]

    async def emit(e):
        events.append(e['type'])
    loader = KotakInstrumentLoader(im, BrokerRegistry({'KOTAK': broker}), emit=emit, clock=lambda: now[0])
    return loader, im, broker, server, events, now


def run(loader, force=False):
    return asyncio.run(loader.run_once(force=force))


def test_select_segment_files_prefers_exact_name_and_reports_missing():
    files = file_urls('2026-10-05', ('nse_fo', 'nse_cm'))
    chosen, missing = select_segment_files(files, ['nse_fo', 'nse_cm', 'mcx_fo'])
    assert chosen['nse_fo'].endswith('/transformed/nse_fo.csv') and chosen['nse_cm'].endswith('/transformed/nse_cm.csv')
    assert missing == ['mcx_fo']
    loose, _ = select_segment_files([f'{BASE}/2026-10-05/x/nse_fo-v1.csv'], ['nse_fo'])
    assert loose['nse_fo'].endswith('nse_fo-v1.csv')  # SDK rule when no exact name exists


def test_files_date_takes_the_oldest_file():
    assert files_date([f'{BASE}/2026-10-05/a/nse_fo.csv', f'{BASE}/2026-10-04/a/nse_cm.csv']) == '2026-10-04'
    assert files_date(['https://x/nse_fo.csv']) is None


def test_loads_todays_segments_then_stays_quiet(tmp_path, settings_override):
    loader, im, broker, server, events, _ = make_loader(tmp_path, settings_override)
    r = run(loader)
    assert r['status'] == 'LOADED' and r['segments'] == ['nse_cm', 'nse_fo'] and r['count'] == 3 and r['files'] == 2
    assert sorted(server.hits) == ['nse_cm.csv', 'nse_fo.csv']  # exact files, not the -v1 variants
    assert im.get('KOTAK', '48201')['expiry'] == server.expiry and im.get('KOTAK', '2885')['symbol'] == 'RELIANCE-EQ'
    assert im.source_date['KOTAK'] == trading_date() and im.fresh_today('KOTAK')
    assert events == ['INSTRUMENT_MASTER_LOADED']
    assert run(loader) is None and broker.calls == 1  # fresh: no further requests today
    assert InstrumentMaster(tmp_path / 'data').source_date['KOTAK'] == trading_date()  # survives a restart


def test_yesterdays_files_load_once_as_interim_and_never_count_as_fresh(tmp_path, settings_override):
    yesterday = (date.fromisoformat(trading_date()) - timedelta(days=1)).isoformat()
    broker = FakeKotak(days=[yesterday, yesterday, trading_date()])
    loader, im, _, server, _, _ = make_loader(tmp_path, settings_override, broker=broker)
    assert run(loader)['status'] == 'LOADED' and im.loaded('KOTAK') and not im.fresh_today('KOTAK')
    hits = len(server.hits)
    assert run(loader)['status'] == 'WAITING_FOR_NEW_FILES' and len(server.hits) == hits  # no re-download
    assert run(loader)['status'] == 'LOADED' and im.fresh_today('KOTAK')


def test_failures_back_off_and_keep_the_current_index(tmp_path, settings_override):
    loader, im, broker, _, events, now = make_loader(tmp_path, settings_override)
    im.load_rows('KOTAK', [{'pSymbol': '1', 'pTrdSymbol': 'OLD', 'pExchSeg': 'nse_cm'}], source_date='2000-01-01')
    broker.exc = RuntimeError('403 Forbidden')
    assert run(loader) == {'status': 'ERROR', 'error': '403 Forbidden', 'retry_in_sec': 60}
    assert run(loader) is None and broker.calls == 1
    now[0] += 60
    assert run(loader)['retry_in_sec'] == 120 and broker.calls == 2
    assert im.get('KOTAK', '1')['symbol'] == 'OLD' and events == ['INSTRUMENT_MASTER_LOAD_ERROR'] * 2
    broker.exc = None
    now[0] += 120
    assert run(loader)['status'] == 'LOADED' and loader.status()['consecutive_failures'] == 0


def test_one_failed_download_changes_nothing(tmp_path, settings_override):
    loader, im, _, _, _, _ = make_loader(tmp_path, settings_override, server=Server(fail={'nse_cm.csv'}))
    r = run(loader)
    assert r['status'] == 'ERROR' and '404' in r['error'] and not im.loaded('KOTAK')


def test_missing_segment_file_is_an_error(tmp_path, settings_override):
    loader, im, _, _, _, _ = make_loader(tmp_path, settings_override, kotak_scrip_segments='nse_fo,cde_fo')
    r = run(loader)
    assert r['status'] == 'ERROR' and 'KOTAK_SEGMENT_FILES_MISSING:cde_fo' in r['error'] and not im.loaded('KOTAK')


def test_disabled_without_consumer_key_or_when_switched_off(tmp_path, settings_override):
    loader, _, broker, _, _, _ = make_loader(tmp_path, settings_override, kotak_instrument_master_auto=False)
    assert run(loader) is None and run(loader, force=True) is None
    settings_override(kotak_instrument_master_auto=True, kotak_api_key='')
    assert run(loader) is None and broker.calls == 0 and loader.status()['enabled'] is False


def test_kotak_scrip_master_files_needs_no_login(settings_override):
    settings_override(kotak_api_key='key')
    urls = file_urls('2026-10-05', ('nse_fo',))

    class Neo:
        resp = {'filesPaths': urls, 'baseFolder': BASE}

        def __init__(self, consumer_key, environment):
            assert consumer_key == 'key' and environment == 'prod'

        def scrip_master(self):
            return Neo.resp

        def totp_login(self, **kw):
            raise AssertionError('scrip master must not log in')

    k = Kotak(client_factory=Neo)
    assert asyncio.run(k.scrip_master_files()) == urls
    Neo.resp = {'Error': 'Exchange Segment is not available'}
    with pytest.raises(RuntimeError, match='KOTAK_SCRIP_MASTER_UNAVAILABLE'):
        asyncio.run(k.scrip_master_files())
    assert k.login_state()['consecutive_failures'] == 0


def test_refresh_endpoint_and_dashboard(make_terminal, settings_override):
    settings_override(kotak_api_key='key', kotak_instrument_master_auto=True, kotak_scrip_segments='nse_cm,nse_fo')
    with override(operator_api_token=TOKEN):
        t, _ = make_terminal(broker=FakeKotak())
        t.instruments.transport = httpx.MockTransport(Server())
        c = TestClient(create_app(t, run_background=False))
        assert c.post('/instruments/refresh/KOTAK').status_code == 401
        h = {'X-IORT-Operator-Token': TOKEN}
        assert c.post('/instruments/refresh/ANGEL', headers=h).status_code == 400
        r = c.post('/instruments/refresh/KOTAK', headers=h)
        assert r.status_code == 200 and r.json()['status'] == 'LOADED'
        state = c.get('/dashboard/state').json()
        assert state['instrument_loader']['fresh_today'] and state['instruments']['KOTAK']['source_date'] == trading_date()
