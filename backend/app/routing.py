"""Broker selection and submission.

Never retry an ambiguous submission on another broker: the first order may
exist remotely, and a retry elsewhere would double the position.
"""
from .brokers import classify_exception
from .config import settings


class SmartOrderRouter:
    def __init__(self, registry, health=None):
        self.registry = registry
        self.health = health

    def candidates(self):
        return [x.strip().upper() for x in settings.routing_brokers.split(',') if x.strip()]

    async def select(self, requested, feed):
        requested = str(requested).upper()
        if requested != 'AUTO':
            return requested, {'mode': 'EXPLICIT', 'candidates': [requested]}
        states = await (self.health.get() if self.health else self.registry.health())
        live = {str(x.get('broker', '')).upper() for x in states if x.get('status') == 'LIVE'}
        ranked = sorted((0 if feed.live(b) else 1, i, b) for i, b in enumerate(self.candidates()) if b in live)
        if not ranked:
            return None, {'mode': 'AUTO', 'reason': 'NO_HEALTHY_BROKER'}
        return ranked[0][2], {'mode': 'AUTO', 'candidates': [x[2] for x in ranked]}

    async def route(self, order):
        broker = str(order['broker']).upper()
        if broker not in self.registry.items:
            return {'status': 'REJECTED', 'reason': 'BROKER_NOT_CONFIGURED', 'broker': broker}
        if not order.get('client_order_id'):
            return {'status': 'REJECTED', 'reason': 'CLIENT_ORDER_ID_REQUIRED', 'broker': broker}
        try:
            return await self.registry.get(broker).place(dict(order, broker=broker))
        except Exception as exc:  # noqa: BLE001 - classified: delivered or not?
            status, reason = classify_exception(exc)
            return {'status': status, 'reason': reason, 'broker': broker}
