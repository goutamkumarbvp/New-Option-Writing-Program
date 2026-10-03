from collections import defaultdict
class OptionChain:
    def __init__(self): self.data=defaultdict(dict)
    def update(self,t):
        if t.underlying and t.expiry and t.strike is not None and t.option_type:
            self.data[(t.exchange,t.underlying,t.expiry)][(t.strike,t.option_type)]=t.model_dump()
    def snapshot(self,exchange,underlying,expiry): return sorted(self.data.get((exchange,underlying,expiry),{}).values(),key=lambda x:(x['strike'],x['option_type']))
    def summary(self,exchange,underlying,expiry):
        r=self.snapshot(exchange,underlying,expiry); ce=sum(x['oi'] for x in r if x['option_type']=='CE'); pe=sum(x['oi'] for x in r if x['option_type']=='PE')
        strikes=sorted({x['strike'] for x in r}); pain={}
        for k in strikes:
            pain[k]=sum(abs(k-x['strike'])*x['oi'] for x in r if x['strike'] is not None)
        max_pain=min(pain,key=pain.get) if pain else None
        iv=[x.get('iv') for x in r if x.get('iv') is not None and x.get('iv')>0]
        return {'rows':len(r),'ce_oi':ce,'pe_oi':pe,'pcr':pe/ce if ce else None,'max_pain':max_pain,'avg_iv':sum(iv)/len(iv) if iv else None}
