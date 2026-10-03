"""Deterministic multi-broker routing with an important safety rule:
never retry an ambiguous submission on another broker because that can duplicate a live order.
"""
from .config import settings

class SmartOrderRouter:
    def __init__(self, registry): self.registry=registry

    def candidates(self):
        return [x.strip().upper() for x in settings.routing_brokers.split(',') if x.strip()]

    async def select(self, requested, feed):
        requested=str(requested).upper()
        if requested!='AUTO': return requested, {'mode':'EXPLICIT','candidates':[requested]}
        states=await self.registry.health()
        live={str(x.get('broker','')).upper() for x in states if x.get('status')=='LIVE'}
        ranked=[]
        for i,b in enumerate(self.candidates()):
            if b not in live: continue
            feed_live=bool(feed.live(b))
            ranked.append((0 if feed_live else 1,i,b))
        ranked.sort()
        if not ranked: return None, {'mode':'AUTO','reason':'NO_HEALTHY_BROKER'}
        return ranked[0][2], {'mode':'AUTO','candidates':[x[2] for x in ranked]}

    async def route(self, order):
        broker=str(order['broker']).upper()
        if broker not in self.registry.items:
            return {'status':'BROKER_NOT_CONFIGURED','broker':broker}
        client_id=order.get('client_order_id')
        if not client_id: return {'status':'CLIENT_ORDER_ID_REQUIRED','broker':broker}
        try:
            result=await self.registry.get(broker).place(dict(order, broker=broker))
            return result
        except TimeoutError:
            # Never fail over after an ambiguous broker timeout. The order may exist remotely.
            return {'status':'UNKNOWN_SUBMISSION_STATE','broker':broker,'reason':'BROKER_TIMEOUT_NO_RETRY'}
        except Exception as e:
            return {'status':'BROKER_EXCEPTION','broker':broker,'reason':str(e)}
