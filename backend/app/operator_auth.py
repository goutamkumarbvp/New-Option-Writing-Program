import hmac

from .config import settings


def authorize(token):
    """Constant-time check of the shared operator token."""
    if not settings.require_operator_auth and not settings.live_trading:
        return True
    expected = settings.operator_api_token
    return bool(expected) and bool(token) and hmac.compare_digest(str(token), expected)


def require_live_operator(token):
    """Order-path rule: while order routing is locked (LIVE_TRADING=false) every order is refused
    anyway, so only live routing needs the token."""
    if not settings.live_trading:
        return True
    return authorize(token)


def require_control_operator(token):
    """Control actions (kill reset, emergency stop, reconcile, flatten, order resolution,
    audit append) need the token whenever one is configured, and always in live mode."""
    if settings.live_trading or settings.operator_api_token:
        return bool(settings.operator_api_token) and bool(token) and hmac.compare_digest(str(token), settings.operator_api_token)
    return True
