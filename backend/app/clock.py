"""Exchange-calendar helpers. Indian venues run on IST (UTC+05:30, no DST)."""
from datetime import date, datetime, time as dtime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
EQUITY_SESSION = (dtime(9, 15), dtime(15, 30))
COMMODITY_SESSION = (dtime(9, 0), dtime(23, 30))


def now_ist():
    return datetime.now(IST)


def trading_date(ts=None):
    """IST calendar date used to scope broker order books and daily P&L."""
    ts = ts or now_ist()
    return ts.astimezone(IST).date().isoformat()


def in_session(commodity=False, ts=None):
    """True during regular session hours on a weekday.

    Exchange holidays are not modelled here; a holiday simply produces no ticks.
    """
    ts = (ts or now_ist()).astimezone(IST)
    if ts.weekday() >= 5:
        return False
    start, end = COMMODITY_SESSION if commodity else EQUITY_SESSION
    return start <= ts.time() <= end


def expiry_close(expiry):
    """Expiry instant (15:30 IST) for an ISO date string or date."""
    if isinstance(expiry, str):
        expiry = date.fromisoformat(expiry[:10])
    return datetime.combine(expiry, EQUITY_SESSION[1], tzinfo=IST)


def years_to_expiry(expiry, ts=None):
    ts = ts or now_ist()
    seconds = (expiry_close(expiry) - ts).total_seconds()
    return max(seconds, 60.0) / (365.0 * 24 * 3600)


def expired(expiry, ts=None):
    if not expiry:
        return False
    return (ts or now_ist()) > expiry_close(expiry)
