"""Per-broker instrument master.

Each broker publishes its own scrip master with its own token scheme, so one
global list (the original design) cannot work: loading a second broker's file
replaced the first, and Zerodha/Upstox/Kotak rows never matched the ``token``
lookup. Here every broker keeps its own normalised index keyed by its own token.

Normalised record: token, symbol, exchange, underlying, expiry (ISO date or None),
strike (rupees or None), option_type ('CE'/'PE'/None), lot_size, tick_size (rupees
or None when the unit is not verified), instrument_type.
"""
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


def normalize_row(broker, r, today=None):
    b = broker.upper()
    if b == 'ZERODHA':
        return {'token': str(r.get('instrument_token', '')), 'symbol': r.get('tradingsymbol', ''),
                'exchange': str(r.get('exchange', '')).upper(), 'underlying': r.get('name') or None,
                'expiry': _date(r.get('expiry')), 'strike': _f(r.get('strike')) or None,
                'option_type': _opt(r.get('instrument_type')), 'lot_size': int(_f(r.get('lot_size')) or 1),
                'tick_size': _f(r.get('tick_size')), 'instrument_type': r.get('instrument_type')}
    if b == 'ANGEL':
        # Angel publishes strike and tick size in paise.
        sym = str(r.get('symbol', ''))
        strike = _f(r.get('strike'))
        return {'token': str(r.get('token', '')), 'symbol': sym, 'exchange': _EXCH.get(str(r.get('exch_seg', '')).upper(), ''),
                'underlying': r.get('name') or None, 'expiry': _date(r.get('expiry')),
                'strike': strike / 100 if strike and strike > 0 else None,
                'option_type': _opt(sym[-2:]) if str(r.get('instrumenttype', '')).startswith('OPT') else None,
                'lot_size': int(_f(r.get('lotsize')) or 1), 'tick_size': None, 'instrument_type': r.get('instrumenttype')}
    if b == 'UPSTOX':
        return {'token': str(r.get('instrument_key', '')), 'symbol': r.get('trading_symbol', ''),
                'exchange': _EXCH.get(str(r.get('segment', r.get('exchange', ''))).upper(), ''),
                'underlying': r.get('underlying_symbol') or r.get('name') or None, 'expiry': _date(r.get('expiry')),
                'strike': _f(r.get('strike_price')) or None, 'option_type': _opt(r.get('instrument_type')),
                'lot_size': int(_f(r.get('lot_size')) or 1), 'tick_size': None, 'instrument_type': r.get('instrument_type')}
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
                'strike': strike, 'option_type': _opt(r.get('pOptionType')), 'lot_size': int(_f(r.get('lLotSize')) or 1),
                'tick_size': None, 'instrument_type': r.get('pInstType')}
    raise ValueError(f'UNKNOWN_BROKER:{broker}')


class InstrumentMaster:
    def __init__(self, data_dir=None):
        self.dir = Path(data_dir or settings.data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index = {}       # broker -> token -> record
        self.loaded_at = {}   # broker -> epoch seconds
        for p in self.dir.glob('instruments_*.json'):
            try:
                blob = json.loads(p.read_text())
                self._install(blob['broker'], blob['rows'], blob.get('loaded_at', p.stat().st_mtime))
            except Exception:  # noqa: BLE001 - a corrupt cache is ignored, not trusted
                continue

    def _install(self, broker, records, loaded_at):
        b = broker.upper()
        self.index[b] = {r['token']: r for r in records if r.get('token')}
        self.loaded_at[b] = loaded_at

    def load_rows(self, broker, raw_rows):
        today = datetime.now(IST).date()
        records = [normalize_row(broker, r, today) for r in raw_rows if isinstance(r, dict)]
        now = time.time()
        self._install(broker, records, now)
        (self.dir / f'instruments_{broker.upper()}.json').write_text(
            json.dumps({'broker': broker.upper(), 'loaded_at': now, 'rows': records}))
        return {'broker': broker.upper(), 'count': len(self.index[broker.upper()]), 'loaded_at': now}

    async def load_url(self, url, broker):
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
            r = await c.get(url)
            r.raise_for_status()
            raw = gzip.decompress(r.content) if r.content[:2] == b'\x1f\x8b' else r.content
        text = raw.decode('utf-8-sig')
        try:
            rows = json.loads(text)
        except ValueError:
            rows = list(csv.DictReader(io.StringIO(text)))
        if not isinstance(rows, list):
            raise ValueError('INSTRUMENT_MASTER_NOT_LIST')
        return self.load_rows(broker, rows)

    def get(self, broker, token):
        return self.index.get(str(broker).upper(), {}).get(str(token))

    def loaded(self, broker):
        return bool(self.index.get(str(broker).upper()))

    def fresh_today(self, broker):
        ts = self.loaded_at.get(str(broker).upper())
        return bool(ts) and trading_date(datetime.fromtimestamp(ts, tz=timezone.utc)) == trading_date()

    def summary(self):
        return {b: {'count': len(ix), 'loaded_at': self.loaded_at.get(b), 'fresh_today': self.fresh_today(b)}
                for b, ix in self.index.items()}

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
