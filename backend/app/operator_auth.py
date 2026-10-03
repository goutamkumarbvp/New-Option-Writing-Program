import hmac
from .config import settings

def authorize(token):
    if not settings.require_operator_auth and not settings.live_trading:
        return True
    expected=settings.operator_api_token
    return bool(expected) and bool(token) and hmac.compare_digest(str(token), expected)

def require_live_operator(token):
    if not settings.live_trading:
        return True
    return authorize(token)
