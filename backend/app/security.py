"""Browser-facing protections.

The terminal binds to localhost, but any web page the operator opens can still
send requests to http://127.0.0.1:8000. Browsers always attach an Origin header
to cross-site POSTs and WebSocket handshakes, so rejecting foreign origins stops
cross-site request forgery and cross-site WebSocket hijacking. Non-browser
clients (curl, scripts) send no Origin and are governed by the operator token.
"""
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import settings


def origin_allowed(origin, host):
    if not origin:
        return True
    allowed = {o.strip().rstrip('/') for o in settings.allowed_origins.split(',') if o.strip()}
    if origin.rstrip('/') in allowed:
        return True
    return urlparse(origin).netloc == (host or '')


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and not origin_allowed(request.headers.get('origin'), request.headers.get('host')):
            return JSONResponse({'status': 'BLOCKED', 'reason': 'CROSS_ORIGIN_REQUEST_REJECTED'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Content-Security-Policy'] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                                       "style-src 'self' 'unsafe-inline'; connect-src 'self' ws: wss:; frame-ancestors 'none'")
        return response
