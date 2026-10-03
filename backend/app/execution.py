from dataclasses import dataclass
import uuid
@dataclass
class RoutedOrder: broker:str; request:dict
class SmartOrderRouter:
    def __init__(self,registry): self.registry=registry
    async def route(self,order):
        broker=order['broker'].upper()
        if broker not in self.registry.items:return {'status':'BROKER_NOT_CONFIGURED'}
        client_id=order.get('client_order_id') or str(uuid.uuid4()); order=dict(order);order['client_order_id']=client_id
        return await self.registry.get(broker).place(order)
