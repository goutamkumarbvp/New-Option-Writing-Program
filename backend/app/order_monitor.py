import asyncio
class OrderMonitor:
    def __init__(self,registry,ledger,interval=2):self.registry=registry;self.ledger=ledger;self.interval=interval;self.stop=False
    def _rows(self,x):return x.get('data',[]) if isinstance(x,dict) else (x or [])
    def _bo(self,o):return str(o.get('order_id') or o.get('orderId') or o.get('nOrdNo') or o.get('order_no') or '')
    def _status(self,o):
        s=str(o.get('status') or o.get('orderStatus') or o.get('order_status') or '').upper();return {'COMPLETE':'FILLED','TRADED':'FILLED','REJECTED':'REJECTED','CANCELLED':'CANCELLED','OPEN':'OPEN','PARTIAL':'PARTIAL','PARTIALLY FILLED':'PARTIAL'}.get(s,s or 'UNKNOWN')
    async def run(self):
        while not self.stop:
            for broker in self.registry.items.values():
                try:
                    for o in self._rows(await broker.orders()):
                        bo=self._bo(o)
                        if not bo:continue
                        st=self._status(o);fq=o.get('filled_quantity',o.get('filledQty',o.get('fill_quantity')));avg=o.get('average_price',o.get('avgPrice',o.get('avg_price')))
                        self.ledger.update_order_by_broker_id(bo,st,fq,avg,o);self.ledger.event('ORDER_STATUS',{'broker':broker.name,'broker_order_id':bo,'status':st,'raw':o})
                    for t in self._rows(await broker.trades()):
                        bo=self._bo(t);qty=int(t.get('quantity',t.get('fill_quantity',t.get('filled_quantity',0))) or 0);price=float(t.get('price',t.get('fill_price',t.get('average_price',0))) or 0)
                        if bo and qty>0:
                            rec=self.ledger.order_by_broker_id(bo)
                            if rec:
                                self.ledger.record_fill({'client_order_id':rec['client_order_id'],'broker_order_id':bo,'broker_trade_id':str(t.get('trade_id') or t.get('tradeId') or t.get('fill_id') or t.get('execution_id') or t.get('exec_id') or ''),'qty':qty,'price':price,'raw':t});self.ledger.event('FILL_OBSERVED',{'broker':broker.name,'broker_order_id':bo,'qty':qty,'price':price})
                except Exception as e:self.ledger.event('ORDER_MONITOR_ERROR',{'broker':broker.name,'error':str(e)})
            await asyncio.sleep(self.interval)
