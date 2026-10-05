"""Network facts the terminal needs on the operator's machine: this PC's public IP (Kotak accepts
API orders only from the static IP registered with it) and whether each Kotak host is reachable."""
import asyncio
import ipaddress
import time

import httpx

from .config import settings

# Hosts the Kotak Neo SDK talks to (neo_api_client/utils/urls.py): login and REST, the alternate
# data centre, configuration and scrip master, and the market-data websocket.
KOTAK_HOSTS = ('mis.kotaksecurities.com', 'cis.kotaksecurities.com', 'lapi.kotaksecurities.com', 'sfeed.kotaksecurities.com')


class PublicIP:
    """This machine's public IP as seen from the internet, cached for `ttl` seconds."""

    def __init__(self, ttl=300.0, transport=None, clock=time.monotonic, fail_ttl=15.0):
        self.ttl, self.transport, self.clock, self.fail_ttl = ttl, transport, clock, fail_ttl
        self.ip, self.at, self.error = None, 0.0, None
        self._lock = asyncio.Lock()

    async def get(self, force=False):
        async with self._lock:
            # A good answer is kept for `ttl`; a failed lookup only for `fail_ttl`, so one blip blocks
            # live orders (fail-closed) for seconds, not minutes.
            if not force and self.at and self.clock() - self.at < (self.ttl if self.ip else self.fail_ttl):
                return self.ip
            self.at = self.clock()
            try:
                async with httpx.AsyncClient(timeout=4.0, transport=self.transport) as c:
                    r = await c.get(settings.public_ip_url)
                text = r.text.strip()
                self.ip = str(ipaddress.ip_address(text)) if r.status_code == 200 else None
                self.error = None if self.ip else f'HTTP {r.status_code}'
            except Exception as exc:  # noqa: BLE001 - unknown, and the caller fails closed
                self.ip, self.error = None, f'{type(exc).__name__}: {str(exc)[:120]}'
            return self.ip


def static_ip_reasons(public_ip):
    """Readiness reasons for live order routing; empty when the check is off or passes."""
    if not settings.require_static_ip_match:
        return []
    want = settings.registered_static_ip.strip()
    if not want:
        return ['STATIC_IP_NOT_CONFIGURED']
    if public_ip is None:
        return ['PUBLIC_IP_UNKNOWN']
    return [] if public_ip == want else ['PUBLIC_IP_NOT_REGISTERED']


async def host_reachable(host, transport=None, timeout=5.0):
    """(ok, detail). Any HTTP answer, even 403 or 404, proves DNS, routing and TLS work; a proxy's own
    403 on CONNECT, a DNS failure or a timeout means this network cannot reach Kotak."""
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=transport) as c:
            r = await c.head(f'https://{host}/')
        return True, f'HTTP {r.status_code} in {round((time.perf_counter() - t0) * 1000)} ms'
    except httpx.ProxyError as exc:
        return False, f'blocked by a proxy: {str(exc)[:100]}'
    except httpx.ConnectError as exc:
        return False, f'cannot connect (DNS, firewall or no internet): {str(exc)[:100]}'
    except httpx.TimeoutException:
        return False, f'timed out after {timeout:g} s'
    except Exception as exc:  # noqa: BLE001
        return False, f'{type(exc).__name__}: {str(exc)[:100]}'
