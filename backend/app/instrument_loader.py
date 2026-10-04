"""Automatic Kotak instrument-master loading.

Kotak publishes a new scrip master every day under a dated path, so a fixed URL in
INSTRUMENT_MASTER_URLS_JSON goes stale overnight. This loader asks Kotak for the day's
file paths (consumer key only: no login, so it never touches the login backoff), loads
the configured segments together, and loads again when the files' date moves on.

- Freshness follows the files' own date. Yesterday's files fetched after midnight are
  loaded only when nothing is loaded yet, and never count as today's, so orders stay
  blocked by INSTRUMENT_MASTER_STALE until today's files are in.
- A failed fetch or download backs off 60 s, 120 s ... up to an hour, and leaves the
  current index in place (all or nothing).
"""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta

from .clock import IST
from .config import settings

MIN_RETRY_SEC, MAX_RETRY_SEC = 60, 3600
_DATE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def kotak_segments():
    return [s.strip().lower() for s in settings.kotak_scrip_segments.split(',') if s.strip()]


def select_segment_files(files, segments):
    """One CSV per segment: a file named exactly '<segment>.csv' first, otherwise the SDK's
    rule (the first path containing the segment name). Returns (chosen, missing)."""
    chosen, missing = {}, []
    for seg in segments:
        exact = [f for f in files if f.lower().split('?')[0].rsplit('/', 1)[-1] == f'{seg}.csv']
        loose = [f for f in files if seg in f.lower()]
        pick = (exact or loose or [None])[0]
        if pick:
            chosen[seg] = pick
        else:
            missing.append(seg)
    return chosen, missing


def files_date(urls):
    """Oldest date found in the file paths (conservative when segments differ), or None."""
    dates = [m.group(1) for m in (_DATE.search(u) for u in urls) if m]
    return min(dates) if dates else None


class KotakInstrumentLoader:
    broker_name = 'KOTAK'

    def __init__(self, instruments, brokers, ledger=None, emit=None, clock=None):
        self.instruments = instruments
        self.brokers = brokers
        self.ledger = ledger
        self.emit = emit
        self.clock = clock or time.monotonic
        self.stop = False
        self.failures = 0
        self.retry_at = 0.0
        self.last = None
        self.last_error = None
        self._busy = asyncio.Lock()

    def broker(self):
        try:
            return self.brokers.get(self.broker_name)
        except KeyError:
            return None

    def enabled(self):
        b = self.broker()
        return bool(settings.kotak_instrument_master_auto and settings.kotak_api_key and b is not None
                    and hasattr(b, 'scrip_master_files'))

    def status(self):
        now = self.clock()
        return {'enabled': self.enabled(), 'segments': kotak_segments(), 'fresh_today': self.instruments.fresh_today(self.broker_name),
                'source_date': self.instruments.source_date.get(self.broker_name), 'consecutive_failures': self.failures,
                'retry_in_sec': max(0, round(self.retry_at - now)) if self.retry_at > now else 0,
                'last': self.last, 'last_error': self.last_error}

    async def _event(self, kind, payload):
        if self.ledger:
            self.ledger.event(kind, payload)
        if self.emit:
            await self.emit({'type': kind, 'payload': payload})

    async def refresh(self, force=False):
        async with self._busy:
            files = await self.broker().scrip_master_files()
            chosen, missing = select_segment_files(files, kotak_segments())
            if missing:
                raise RuntimeError('KOTAK_SEGMENT_FILES_MISSING:' + ','.join(missing))
            fdate = files_date(chosen.values())
            have = self.instruments.source_date.get(self.broker_name)
            if not force and fdate and have and fdate <= have and self.instruments.loaded(self.broker_name):
                return {'status': 'WAITING_FOR_NEW_FILES', 'file_date': fdate, 'loaded_date': have}
            r = await self.instruments.load_urls(self.broker_name, list(chosen.values()), source_date=fdate)
            return {'status': 'LOADED', 'file_date': fdate, 'segments': sorted(chosen), 'count': r['count'], 'files': r['files']}

    async def run_once(self, force=False):
        """One check. Returns the refresh result, or None when nothing was due."""
        if not self.enabled():
            return None
        if not force and (self.instruments.fresh_today(self.broker_name) or self.clock() < self.retry_at):
            return None
        try:
            r = await self.refresh(force=force)
        except Exception as exc:  # noqa: BLE001 - any failure keeps the current index and backs off
            self.failures += 1
            delay = min(MIN_RETRY_SEC * 2 ** (self.failures - 1), MAX_RETRY_SEC)
            self.retry_at = self.clock() + delay
            self.last_error = str(exc)[:300]
            await self._event('INSTRUMENT_MASTER_LOAD_ERROR', {'broker': self.broker_name, 'error': self.last_error,
                                                               'retry_in_sec': delay})
            return {'status': 'ERROR', 'error': self.last_error, 'retry_in_sec': delay}
        self.failures, self.retry_at, self.last_error = 0, 0.0, None
        self.last = {**r, 'at': time.time()}
        if r['status'] == 'LOADED':
            await self._event('INSTRUMENT_MASTER_LOADED', {'broker': self.broker_name, **r})
        return r

    async def run(self):
        while not self.stop:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop itself must survive
                if self.ledger:
                    self.ledger.event('INSTRUMENT_LOADER_LOOP_ERROR', {'error': str(exc)[:200]})
            await asyncio.sleep(settings.instrument_refresh_check_sec)


def refresh_due_time():
    """INSTRUMENT_REFRESH_AFTER_IST as (hour, minute); malformed values fall back to 08:30."""
    try:
        h, m = (int(x) for x in str(settings.instrument_refresh_after_ist).split(':', 1))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    return 8, 30


def latest_publication(now):
    """Most recent instant a new daily file is expected: today's due time, or yesterday's
    when today's has not come yet."""
    h, m = refresh_due_time()
    due = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return due if now >= due else due - timedelta(days=1)


class StaticInstrumentRefresher:
    """Reloads brokers whose INSTRUMENT_MASTER_URLS_JSON URL is fixed (Zerodha, Angel, Upstox)
    once per day, after the broker publishes the new file. Startup still loads them first;
    this keeps a terminal that runs overnight from trading on yesterday's master. Kotak is
    left to KotakInstrumentLoader whenever automatic loading is on."""

    def __init__(self, instruments, ledger=None, emit=None, clock=None, now=None):
        self.instruments = instruments
        self.ledger = ledger
        self.emit = emit
        self.clock = clock or time.monotonic
        self.now = now or (lambda: datetime.now(IST))
        self.stop = False
        self.failures, self.retry_at, self.last, self.errors = {}, {}, {}, {}

    def urls(self):
        try:
            m = json.loads(settings.instrument_master_urls_json or '{}')
        except ValueError:
            return {}
        out = {str(b).upper(): u for b, u in (m.items() if isinstance(m, dict) else []) if u}
        if settings.kotak_instrument_master_auto and settings.kotak_api_key:
            out.pop('KOTAK', None)
        return out

    def due(self, broker, now):
        if not self.instruments.loaded(broker):
            return True
        ts = self.instruments.loaded_at.get(broker)
        return ts is None or datetime.fromtimestamp(ts, tz=IST) < latest_publication(now)

    def status(self):
        now = self.clock()
        return {b: {'loaded': self.instruments.loaded(b), 'consecutive_failures': self.failures.get(b, 0),
                    'retry_in_sec': max(0, round(self.retry_at.get(b, 0) - now)), 'last': self.last.get(b),
                    'last_error': self.errors.get(b)} for b in self.urls()}

    async def _event(self, kind, payload):
        if self.ledger:
            self.ledger.event(kind, payload)
        if self.emit:
            await self.emit({'type': kind, 'payload': payload})

    async def run_once(self):
        now, results = self.now(), {}
        for b, url in self.urls().items():
            if not self.due(b, now) or self.clock() < self.retry_at.get(b, 0):
                continue
            try:
                r = await self.instruments.load_url(url, b)
            except Exception as exc:  # noqa: BLE001 - keep the current index and back off
                self.failures[b] = self.failures.get(b, 0) + 1
                delay = min(MIN_RETRY_SEC * 2 ** (self.failures[b] - 1), MAX_RETRY_SEC)
                self.retry_at[b], self.errors[b] = self.clock() + delay, str(exc)[:300]
                await self._event('INSTRUMENT_MASTER_LOAD_ERROR', {'broker': b, 'error': self.errors[b], 'retry_in_sec': delay})
                results[b] = {'status': 'ERROR', 'error': self.errors[b], 'retry_in_sec': delay}
                continue
            self.failures[b], self.retry_at[b], self.errors[b] = 0, 0.0, None
            self.last[b] = {'count': r['count'], 'at': time.time()}
            await self._event('INSTRUMENT_MASTER_LOADED', {'broker': b, 'count': r['count'], 'source': 'STATIC_URL'})
            results[b] = {'status': 'LOADED', 'count': r['count']}
        return results

    async def run(self):
        while not self.stop:
            try:
                await self.run_once()
            except Exception as exc:  # noqa: BLE001 - the loop itself must survive
                if self.ledger:
                    self.ledger.event('INSTRUMENT_REFRESH_LOOP_ERROR', {'error': str(exc)[:200]})
            await asyncio.sleep(settings.instrument_refresh_check_sec)
