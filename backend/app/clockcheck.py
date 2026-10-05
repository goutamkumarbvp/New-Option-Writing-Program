"""Local clock offset against an HTTPS server's Date header.

A TOTP code is derived from the local clock, so a PC clock that has drifted by more than a few
seconds makes every Kotak login look like a wrong code, and each rejection counts toward the
account lockout. The Date header has one-second resolution, so the result is good to about
+-1 s: enough to tell "in sync" from "off".
"""
import time
from email.utils import parsedate_to_datetime

import httpx

DEFAULT_URL = 'https://mis.kotaksecurities.com'
WARN_SEC = 5.0
BAD_SEC = 15.0


async def clock_offset(url=DEFAULT_URL, transport=None, wall=time.time, timeout=4.0):
    """Seconds the SERVER is ahead of this machine (negative: this machine is ahead), or None
    when the server cannot be reached or sends no usable Date header."""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
            t0 = wall()
            r = await c.head(url)
            t1 = wall()
        raw = r.headers.get('date')
        if not raw:
            return None
        server = parsedate_to_datetime(raw).timestamp()
    except Exception:  # noqa: BLE001 - best effort; the caller treats None as unknown
        return None
    return round(server + 0.5 - (t0 + t1) / 2, 1)  # Date is truncated to whole seconds: +0.5 centres it


def clock_status(offset):
    if offset is None:
        return 'UNKNOWN'
    size = abs(offset)
    return 'OK' if size <= WARN_SEC else 'WARN' if size <= BAD_SEC else 'BAD'


def clock_hint(offset):
    """Plain-language advice for an operator, or None when the clock is fine or unknown."""
    if clock_status(offset) in ('OK', 'UNKNOWN'):
        return None
    side = 'behind' if offset > 0 else 'ahead of'
    return (f'This machine\'s clock is {abs(offset):.0f} s {side} Kotak (TOTP codes are now computed on Kotak\'s time). '
            'Fix the clock: Windows Settings > Time > Sync now (or w32tm /resync as administrator); with Docker Desktop also '
            'run "wsl --shutdown" and restart Docker Desktop, because its Linux VM keeps its own clock.')
