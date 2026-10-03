import csv,io,json,gzip,time
from pathlib import Path
import httpx
class InstrumentMaster:
 def __init__(self,path='./data/instruments.json'):self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self.rows=json.loads(self.path.read_text()) if self.path.exists() else []
 async def load_url(self,url,source):
  async with httpx.AsyncClient(timeout=60,follow_redirects=True) as c:r=await c.get(url);r.raise_for_status();raw=gzip.decompress(r.content) if r.content[:2]==b'\x1f\x8b' else r.content
  try:rows=json.loads(raw.decode('utf-8-sig'))
  except Exception:rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
  if not isinstance(rows,list):raise ValueError('INSTRUMENT_MASTER_NOT_LIST')
  self.rows=rows;self.path.write_text(json.dumps(rows,default=str));return {'source':source,'count':len(rows),'loaded_at':time.time()}
 def find(self,**filters):
  out=[]
  for r in self.rows:
   ok=True
   for k,v in filters.items():
    if v is None:continue
    rv=r.get(k,r.get('tradingsymbol' if k=='symbol' else k,''));
    if k=='exchange' and str(rv).upper()!=str(v).upper():ok=False
    elif k=='symbol' and str(rv).upper()!=str(v).upper():ok=False
    elif k=='token' and str(rv)!=str(v):ok=False
    elif k in ('underlying','expiry','option_type') and str(v).upper() not in str(rv).upper():ok=False
   if ok:out.append(r)
  return out
