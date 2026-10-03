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
import time
from datetime import datetime, timezone
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


def normalize_row(broker, r):
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
        # Kotak's expiry encoding is not documented in the SDK, so expiry stays None
        # and option-specific gates fail closed for Kotak until it is verified.
        strike = _f(r.get('dStrikePrice;') or r.get('dStrikePrice'))
        return {'token': str(r.get('pSymbol', '')), 'symbol': r.get('pTrdSymbol', ''),
                'exchange': _EXCH.get(str(r.get('pExchSeg', '')).upper(), ''), 'underlying': r.get('pSymbolName') or None,
                'expiry': None, 'strike': strike / 100 if strike and strike > 0 else None,
                'option_type': _opt(r.get('pOptionType')), 'lot_size': int(_f(r.get('lLotSize')) or 1), 'tick_size': None,
                'instrument_type': r.get('pInstType')}
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
        records = [normalize_row(broker, r) for r in raw_rows if isinstance(r, dict)]
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
