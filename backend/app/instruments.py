"""Per-broker instrument master.

Each broker publishes its own scrip master with its own token scheme, so one
global list (the original design) cannot work: loading a second broker's file
replaced the first, and Zerodha/Upstox/Kotak rows never matched the ``token``
lookup. Here every broker keeps its own normalised index keyed by its own token.

Normalised record: token, symbol, exchange, underlying, expiry (ISO date or None),
strike (rupees or None), option_type ('CE'/'PE'/None), lot_size, tick_size (rupees
or None when the unit is not verified), instrument_type.
"""
import asyncio
import csv
import gzip
import io
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from .clock import IST, trading_date
from .config import settings

_EXCH = {'NSE_FO': 'NFO', 'BSE_FO': 'BFO', 'NSE_EQ': 'NSE', 'BSE_EQ': 'BSE', 'MCX_FO': 'MCX', 'NSE_INDEX': 'NSE',
         'BSE_INDEX': 'BSE', 'NSE_CM': 'NSE', 'BSE_CM': 'BSE', 'NFO': 'NFO', 'BFO': 'BFO', 'NSE': 'NSE', 'BSE': 'BSE',
         'MCX': 'MCX', 'MCX_COMM': 'MCX'}


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _date(v):
    if v in (None, ''):
        return None
    s = str(v).strip()
    for fmt in ('%Y-%m-%d', '%d%b%Y', '%d-%b-%Y', '%d%b%y'):
        try:
            return datetime.strptime(s.title() if 'b' in fmt else s, fmt).date().isoformat()
        except ValueError:
            continue
    n = _f(s)
    if n and n > 10**11:  # epoch milliseconds (Upstox)
        return datetime.fromtimestamp(n / 1000, tz=timezone.utc).astimezone(IST).date().isoformat()
    return None


def _opt(v):
    v = str(v or '').upper()
    return v if v in ('CE', 'PE') else None


# Kotak pExpiryDate is seconds. The official SDK (neo_api_client/services/scrip_search.py)
# adds 315511200 s for every *_fo segment except bse_fo and mcx_fo, which are plain Unix
# seconds, and formats the result as a UTC date. Decoding follows that rule exactly.
KOTAK_FO_EPOCH_OFFSET = 315511200
_MONTHS = ('JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')
_WEEKLY_MONTH = '123456789OND'  # NSE weekly symbols: 1-9 for Jan-Sep, O/N/D for Oct-Dec


def symbol_expiry_hint(symbol, underlying, strike=None):
    """Expiry evidence carried by an exchange trading symbol, used to cross-check a decoded date.

    Monthly  NIFTY26OCT25000CE / NIFTY26OCTFUT -> ('MONTH', (2026, 10))
    Weekly   NIFTY26O0625000CE                -> ('DATE', date(2026, 10, 6))
    Returns None when the symbol does not follow either pattern (nothing to check against).
    """
    s, u = str(symbol or '').upper().replace(' ', ''), str(underlying or '').upper().replace(' ', '')
    if not u or not s.startswith(u):
        return None
    rest = s[len(u):]
    m = re.fullmatch(r'(\d{2})([A-Z]{3})(?:FUT|(\d+(?:\.\d+)?)(?:CE|PE))', rest)
    if m and m.group(2) in _MONTHS:
        if m.group(3) and strike and abs(float(m.group(3)) - strike) > 1e-6:
            return None
        return ('MONTH', (2000 + int(m.group(1)), _MONTHS.index(m.group(2)) + 1))
    m = re.fullmatch(r'(\d{2})([1-9OND])(\d{2})(\d+(?:\.\d+)?)(?:CE|PE)', rest)
    if m and strike and abs(float(m.group(4)) - strike) < 1e-6:
        try:
            return ('DATE', date(2000 + int(m.group(1)), _WEEKLY_MONTH.index(m.group(2)) + 1, int(m.group(3))))
        except ValueError:
            return None
    return None


def kotak_expiry(raw, segment, symbol=None, underlying=None, strike=None, today=None):
    """ISO expiry date from a Kotak scrip-master row, or None (fail closed).

    The SDK rule is tried first. A decoded date must fall between 31 days before and six
    years after `today` (a wrong epoch convention lands about ten years off), and must agree
    with the trading symbol's month or weekly date when the symbol carries one. The other
    epoch convention is accepted only when the symbol positively confirms it.
    """
    n = _f(raw)
    if not n or n <= 0:
        return None
    seg = str(segment or '').strip().lower()
    sdk_offset = seg.endswith('_fo') and seg not in ('bse_fo', 'mcx_fo')
    candidates = (n + KOTAK_FO_EPOCH_OFFSET, n) if sdk_offset else (n, n + KOTAK_FO_EPOCH_OFFSET)
    today = today or datetime.now(IST).date()
    lo, hi = today - timedelta(days=31), today + timedelta(days=6 * 366)
    hint = symbol_expiry_hint(symbol, underlying, strike)
    for i, secs in enumerate(candidates):
        try:
            d = datetime.fromtimestamp(secs, tz=timezone.utc).date()
        except (OverflowError, OSError, ValueError):
            continue
        if not lo <= d <= hi:
            continue
        if hint is None:
            if i == 0:
                return d.isoformat()
            continue
        kind, value = hint
        if (kind == 'DATE' and d == value) or (kind == 'MONTH' and (d.year, d.month) == value):
            return d.isoformat()
    return None



def _lot(v):
    """Lot size from the broker file, or None when the file does not give a positive one.
    A missing lot is never assumed to be 1: the order path blocks with LOT_SIZE_UNKNOWN instead."""
    x = _f(v)
    return int(x) if x and x > 0 else None

def normalize_row(broker, r, today=None):
    b = broker.upper()
    if b == 'ZERODHA':
        return {'token': str(r.get('instrument_token', '')), 'symbol': r.get('tradingsymbol', ''),
                'exchange': str(r.get('exchange', '')).upper(), 'underlying': r.get('name') or None,
                'expiry': _date(r.get('expiry')), 'strike': _f(r.get('strike')) or None,
                'option_type': _opt(r.get('instrument_type')), 'lot_size': _lot(r.get('lot_size')),
                'tick_size': _f(r.get('tick_size')), 'instrument_type': r.get('instrument_type')}
    if b == 'ANGEL':
        # Angel publishes strike and tick size in paise.
        sym = str(r.get('symbol', ''))
        strike = _f(r.get('strike'))
        return {'token': str(r.get('token', '')), 'symbol': sym, 'exchange': _EXCH.get(str(r.get('exch_seg', '')).upper(), ''),
                'underlying': r.get('name') or None, 'expiry': _date(r.get('expiry')),
                'strike': strike / 100 if strike and strike > 0 else None,
                'option_type': _opt(sym[-2:]) if str(r.get('instrumenttype', '')).startswith('OPT') else None,
                'lot_size': _lot(r.get('lotsize')), 'tick_size': None, 'instrument_type': r.get('instrumenttype')}
    if b == 'UPSTOX':
        return {'token': str(r.get('instrument_key', '')), 'symbol': r.get('trading_symbol', ''),
                'exchange': _EXCH.get(str(r.get('segment', r.get('exchange', ''))).upper(), ''),
                'underlying': r.get('underlying_symbol') or r.get('name') or None, 'expiry': _date(r.get('expiry')),
                'strike': _f(r.get('strike_price')) or None, 'option_type': _opt(r.get('instrument_type')),
                'lot_size': _lot(r.get('lot_size')), 'tick_size': None, 'instrument_type': r.get('instrument_type')}
    if b == 'KOTAK':
        # Kotak CSV headers carry stray spaces; the SDK strips them before reading columns.
        # Strike is in paise. Expiry is decoded by kotak_expiry() and stays None when the
        # decoded date fails its checks, so option-specific gates still fail closed.
        r = {str(k).strip(): v for k, v in r.items()}
        strike = _f(r.get('dStrikePrice;') or r.get('dStrikePrice'))
        strike = strike / 100 if strike and strike > 0 else None
        symbol, underlying = str(r.get('pTrdSymbol') or '').strip(), (str(r.get('pSymbolName') or '').strip() or None)
        seg = str(r.get('pExchSeg', '')).strip()
        return {'token': str(r.get('pSymbol', '')).strip(), 'symbol': symbol, 'exchange': _EXCH.get(seg.upper(), ''),
                'underlying': underlying, 'expiry': kotak_expiry(r.get('pExpiryDate'), seg, symbol, underlying, strike, today),
                'strike': strike, 'option_type': _opt(r.get('pOptionType')), 'lot_size': _lot(r.get('lLotSize')),
                'tick_size': None, 'instrument_type': r.get('pInstType')}
    raise ValueError(f'UNKNOWN_BROKER:{broker}')


def _rows_from_text(text):
    """JSON list or CSV rows. CSV rows are yielded one at a time so a large scrip master
    is normalised row by row instead of holding every raw 60-column row in memory."""
    stripped = text.lstrip()
    if stripped[:1] in ('[', '{'):
        rows = json.loads(text)
        if not isinstance(rows, list):
            raise ValueError('INSTRUMENT_MASTER_NOT_LIST')
        return iter(rows)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [str(f).strip() for f in reader.fieldnames]
    return reader


class InstrumentMaster:
    def __init__(self, data_dir=None):
        self.dir = Path(data_dir or settings.data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = {}        # broker -> token -> record
        self.loaded_at = {}    # broker -> epoch seconds
        self.source_date = {}  # broker -> ISO date the broker published the files for, when known
        self.transport = None  # httpx transport override (tests)
        for p in self.dir.glob('instruments_*.json'):
            try:
                blob = json.loads(p.read_text())
                self._install(blob['broker'], blob['rows'], blob.get('loaded_at', p.stat().st_mtime), blob.get('source_date'))
            except Exception:  # noqa: BLE001 - a corrupt cache is ignored, not trusted
                continue

    def _install(self, broker, records, loaded_at, source_date=None):
        b = broker.upper()
        self.index[b] = {r['token']: r for r in records if r.get('token')}
        self.loaded_at[b] = loaded_at
        if source_date:
            self.source_date[b] = source_date
        else:
            self.source_date.pop(b, None)

    def load_rows(self, broker, raw_rows, source_date=None):
        today = datetime.now(IST).date()
        records = [normalize_row(broker, r, today) for r in raw_rows if isinstance(r, dict)]
        now = time.time()
        self._install(broker, records, now, source_date)
        self._write_cache(broker.upper(), now, source_date, records)
        return {'broker': broker.upper(), 'count': len(self.index[broker.upper()]), 'loaded_at': now, 'source_date': source_date}

    def _write_cache(self, b, loaded_at, source_date, records, batch=2000):
        """Encode in batches: one json.dumps of a 100k-row master holds the GIL for ~0.25 s,
        which stalls the event loop even from a worker thread. Written to a temp file and
        renamed, so a crash never leaves a half-written cache."""
        path = self.dir / f'instruments_{b}.json'
        tmp = path.with_name(path.name + '.tmp')
        with tmp.open('w') as f:
            f.write(json.dumps({'broker': b, 'loaded_at': loaded_at, 'source_date': source_date})[:-1] + ', "rows": [')
            for i in range(0, len(records), batch):
                f.write((',' if i else '') + json.dumps(records[i:i + batch])[1:-1])
            f.write(']}')
        tmp.replace(path)

    async def _fetch_text(self, client, url):
        r = await client.get(url)
        r.raise_for_status()
        raw = gzip.decompress(r.content) if r.content[:2] == b'\x1f\x8b' else r.content
        return raw.decode('utf-8-sig')

    async def load_urls(self, broker, urls, source_date=None):
        """Download every file, then replace the broker's index with all of them together.
        Any download failure raises before the index is touched (all or nothing). Parsing
        and normalising run in a worker thread so the event loop keeps serving ticks."""
        async with httpx.AsyncClient(timeout=120, follow_redirects=True, transport=self.transport) as c:
            texts = [await self._fetch_text(c, u) for u in urls]

        def build():
            return self.load_rows(broker, (row for text in texts for row in _rows_from_text(text)), source_date)
        result = await asyncio.to_thread(build)
        return {**result, 'files': len(texts)}

    async def load_url(self, url, broker):
        r = await self.load_urls(broker, [url])
        r.pop('files', None)
        return r

    def get(self, broker, token):
        return self.index.get(str(broker).upper(), {}).get(str(token))

    def loaded(self, broker):
        return bool(self.index.get(str(broker).upper()))

    def fresh_today(self, broker):
        """True when the index is today's. When the broker's files carry their own date
        (Kotak), that date decides: yesterday's files loaded after midnight are not fresh."""
        b = str(broker).upper()
        if b in self.source_date:
            return self.source_date[b] == trading_date()
        ts = self.loaded_at.get(b)
        return bool(ts) and trading_date(datetime.fromtimestamp(ts, tz=timezone.utc)) == trading_date()

    def summary(self):
        return {b: {'count': len(ix), 'loaded_at': self.loaded_at.get(b), 'source_date': self.source_date.get(b),
                    'fresh_today': self.fresh_today(b)} for b, ix in self.index.items()}

    # Backward-compatible search used by older tooling.
    @property
    def rows(self):
        return [r for ix in self.index.values() for r in ix.values()]

    def find(self, broker=None, **filters):
        out = []
        for b, ix in self.index.items():
            if broker and b != broker.upper():
                continue
            for r in ix.values():
                if all(v is None or str(r.get(k, '')).upper() == str(v).upper() for k, v in filters.items()):
                    out.append(r)
        return out
