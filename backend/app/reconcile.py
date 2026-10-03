class Reconciler:
    def _items(self,x):return x.get('data',[]) if isinstance(x,dict) else (x or [])
    def _bo(self,o):return str(o.get('order_id') or o.get('orderId') or o.get('nOrdNo') or o.get('order_no') or '')
    async def reconcile(self,broker,ledger):
        positions=self._items(await broker.positions());orders=self._items(await broker.orders());m=[]
        local=ledger.orders(broker.name); local_by={str(x['broker_order_id']):x for x in local if x.get('broker_order_id')}
        remote_by={self._bo(o):o for o in orders if isinstance(o,dict) and self._bo(o)}
        # Import broker-side/manual orders into the ledger as EXTERNAL orders before
        # comparing. This prevents a pre-existing manual order from being mistaken
        # for data loss while still making it visible to risk/reconciliation.
        for bo,ro in remote_by.items():
            if bo not in local_by:
                ledger.upsert_order({'client_order_id':f'EXTERNAL-{broker.name}-{bo}','broker_order_id':bo,'broker':broker.name,
                                     'exchange':ro.get('exchange') or ro.get('exchange_segment') or '',
                                     'symbol':ro.get('tradingsymbol') or ro.get('trading_symbol') or ro.get('symbol') or '',
                                     'side':ro.get('transaction_type') or ro.get('transactionType') or ro.get('trnsTp') or '',
                                     'qty':int(ro.get('quantity',ro.get('qty',0)) or 0),'filled_qty':int(ro.get('filled_quantity',ro.get('filledQty',0)) or 0),
                                     'status':str(ro.get('status') or ro.get('orderStatus') or ro.get('order_status') or 'UNKNOWN').upper(),
                                     'avg_price':float(ro.get('average_price',ro.get('avgPrice',ro.get('avg_price',0))) or 0),'raw':ro})
        local=ledger.orders(broker.name); local_by={str(x['broker_order_id']):x for x in local if x.get('broker_order_id')}
        for bo,lo in local_by.items():
            if bo not in remote_by:m.append({'type':'MISSING_REMOTE_ORDER','broker_order_id':bo})
        for p in positions:
            if not isinstance(p,dict):continue
            symbol=p.get('symbol') or p.get('tradingsymbol') or p.get('trading_symbol');qty=p.get('quantity',p.get('net_quantity',p.get('netQty')))
            if symbol is not None and qty is not None:ledger.upsert_position({'broker':broker.name,'exchange':p.get('exchange',p.get('exchange_segment','')),'symbol':symbol,'qty':int(qty or 0),'avg_price':float(p.get('average_price',p.get('avgPrice',0)) or 0),'pnl':float(p.get('pnl',0) or 0)})
        result={'ok':not m,'positions_count':len(positions),'orders_count':len(orders),'mismatches':m,'broker':broker.name};ledger.record_reconciliation(broker.name,result['ok'],m);ledger.event('BROKER_RECONCILIATION',result);return result
