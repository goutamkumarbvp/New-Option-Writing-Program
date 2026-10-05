"use strict";
// ---------------------------------------------------------------- option chain
// Read-only view of /option-chain/.../ladder. Orders from the ticket go to POST /orders,
// the same path as every other order, so all pre-trade controls still decide.
const CH={idx:[],policy:{},data:null,sel:{sym:'',exp:'',depth:'15',greeks:false},busy:false,scroll:true,hold:false,active:false,tk:null,tkBusy:false};
try{Object.assign(CH.sel,JSON.parse(localStorage.getItem('iort.chain')||'{}'))}catch{}
const chSave=()=>{try{localStorage.setItem('iort.chain',JSON.stringify(CH.sel))}catch{}};
const px=v=>v>0?Number(v).toFixed(2):'—',dp=(v,d)=>v==null?'—':Number(v).toFixed(d),ivp=v=>v==null?'—':(v*100).toFixed(1);
const compact=v=>{if(v==null)return'—';const a=Math.abs(v),s=v<0?'-':'';return a>=1e7?s+(a/1e7).toFixed(2)+'Cr':a>=1e5?s+(a/1e5).toFixed(2)+'L':a>=1e4?s+(a/1e3).toFixed(1)+'K':String(v)};
const signed=v=>v==null?'—':(v>0?'+':'')+compact(v);
const expLabel=e=>{const d=new Date(e+'T00:00:00');return isNaN(d)?e:d.toLocaleDateString('en-IN',{weekday:'short',day:'2-digit',month:'short',year:'numeric'})};
const newCid=()=>'chain-'+Array.from(crypto.getRandomValues(new Uint8Array(12)),b=>b.toString(16).padStart(2,'0')).join('');
function setOptions(sel,opts,val){const h=opts.map(([v,l])=>`<option value="${esc(v)}">${esc(l)}</option>`).join('');if(sel.dataset.h!==h){sel.innerHTML=h;sel.dataset.h=h}sel.value=val}
function chSetStatus(text,c){$('chStatus').textContent=text;cls($('chStatus'),c)}
function chCols(){return[['oi','OI'],['oi_change','OI chg'],['volume','Vol'],['iv','IV'],['delta','Δ'],...(CH.sel.greeks?[['gamma','Γ'],['theta','Θ/day'],['vega','Vega']]:[]),['bid','Bid'],['ask','Ask'],['ltp','LTP']]}
function chHead(cols){$('chHead').innerHTML='<tr>'+cols.map(c=>`<th class="ceh">${c[1]}</th>`).join('')+'<th class="k">Strike</th>'+[...cols].reverse().map(c=>`<th class="peh">${c[1]}</th>`).join('')+'</tr>'}
// The backend being unreachable blanks everything: no last-known quote stays on screen as if live.
function chUnavailable(){CH.data=null;CH.policy={};chRenderEmpty('BACKEND UNAVAILABLE','<b>No live data.</b> The terminal backend is not responding; it is retried every second.');if(CH.tk){CH.tk.leg=Object.assign({},CH.tk.leg,{stale:true});tkRender()}}
async function chLoadIndex(){try{const r=await fetchT('/option-chain');if(!r.ok)throw new Error(r.status);const j=await r.json();CH.idx=j.chains||[];CH.policy=j;chFillSelectors()}catch{chUnavailable()}}
function chFillSelectors(){const key=c=>c.exchange+'|'+c.underlying;const syms=CH.idx.map(key);$('chDepth').value=CH.sel.depth;$('chGreeks').checked=!!CH.sel.greeks;
 if(!syms.length){setOptions($('chSym'),[],'');setOptions($('chExp'),[],'');return} // keep the saved choice until data arrives
 if(!syms.includes(CH.sel.sym))CH.sel.sym=syms[0];
 setOptions($('chSym'),CH.idx.map(c=>[key(c),c.underlying+' · '+c.exchange]),CH.sel.sym);
 const exps=(CH.idx.find(c=>key(c)===CH.sel.sym)||{}).expiries||[];if(!exps.includes(CH.sel.exp))CH.sel.exp=exps[0]||'';
 setOptions($('chExp'),exps.map(e=>[e,expLabel(e)]),CH.sel.exp)}
async function chLoadLadder(){if(CH.busy)return;if(!CH.idx.length||!CH.sel.sym||!CH.sel.exp){CH.data=null;chRenderEmpty();return}
 const i=CH.sel.sym.indexOf('|'),ex=CH.sel.sym.slice(0,i),und=CH.sel.sym.slice(i+1),exp=CH.sel.exp,q=CH.sel.depth&&CH.sel.depth!=='0'?'?depth='+CH.sel.depth:'';
 CH.busy=true;try{const r=await fetchT(`/option-chain/${encodeURIComponent(ex)}/${encodeURIComponent(und)}/${encodeURIComponent(exp)}/ladder${q}`);if(!r.ok)throw new Error(r.status);
  const d=await r.json();if(CH.sel.sym!==ex+'|'+und||CH.sel.exp!==exp)return;CH.data=d;if(!CH.hold)chRender()}catch{chUnavailable()}finally{CH.busy=false}}
function chStats(d){const s=d.summary||{},sp=d.spot||{},src={SPOT_TOKEN:['LIVE SPOT','ok'],PUT_CALL_PARITY:['PARITY ESTIMATE','warn']}[sp.source]||['SPOT UNAVAILABLE','bad'];
 const tile=(l,v,sub='')=>`<div class="stat"><div class="muted">${esc(l)}</div><div class="v">${esc(v)}</div>${sub?`<div class="muted">${sub}</div>`:''}</div>`;
 $('chStats').innerHTML=tile('Spot',sp.value?fmt(sp.value):'—',`<span class="badge ${src[1]}">${src[0]}</span>${sp.symbol?' <span class="muted">'+esc(sp.symbol)+'</span>':''}`)+tile('ATM strike',d.atm_strike!=null?fmt(d.atm_strike):'—')
  +tile('Days to expiry',d.days_to_expiry!=null?d.days_to_expiry.toFixed(1):'—',esc(d.expiry?expLabel(d.expiry):''))
  +tile('PCR (OI)',s.pcr!=null?s.pcr.toFixed(2):'—','Volume PCR '+(s.pcr_volume!=null?s.pcr_volume.toFixed(2):'—'))+tile('Max pain',s.max_pain!=null?fmt(s.max_pain):'—')
  +tile('ATM IV',s.atm_iv!=null?(s.atm_iv*100).toFixed(1)+'%':'—')+tile('ATM straddle',s.atm_straddle!=null?fmt(s.atm_straddle):'—',s.expected_move_pct!=null?'≈ ±'+s.expected_move_pct.toFixed(2)+'% expected move':'')
  +tile('Call OI / Put OI',compact(s.ce_oi)+' / '+compact(s.pe_oi),'chg '+esc(signed(s.ce_oi_change))+' / '+esc(signed(s.pe_oi_change)))}
// Analytics charts under the ladder: OI and OI change by strike (grouped columns), IV smile (lines).
const CHV={};const live=leg=>!!leg&&!leg.stale;
function chChart(id,Cls,o){if(!window.Charts)return;if(CHV[id])CHV[id].update(o);else CHV[id]=new Cls($(id),o)}
function chCharts(d){const rows=(d&&d.strikes)||[],ks=rows.map(r=>r.strike),atm=rows.findIndex(r=>r.strike===d?.atm_strike);
 const S1='var(--series-1)',S2='var(--series-2)',catFmt=k=>Number(k).toLocaleString('en-IN'),base={categories:ks,highlight:atm,catFmt,catName:'Strike',height:220};
 chChart('chOiChart',Charts.BarChart,{...base,title:'Open interest by strike, calls and puts',series:[{name:'Calls',color:S1,values:rows.map(r=>live(r.CE)?r.CE.oi:null)},{name:'Puts',color:S2,values:rows.map(r=>live(r.PE)?r.PE.oi:null)}]});
 chChart('chOiChgChart',Charts.BarChart,{...base,title:'Open interest change by strike',emptyText:'No OI change yet',series:[{name:'Calls',color:S1,values:rows.map(r=>live(r.CE)?r.CE.oi_change:null)},{name:'Puts',color:S2,values:rows.map(r=>live(r.PE)?r.PE.oi_change:null)}]});
 chChart('chIvChart',Charts.LineChart,{x:ks,xFmt:catFmt,xName:'Strike',yFmt:v=>v.toFixed(1)+'%',height:220,title:'Implied volatility by strike',emptyText:'IV needs a spot price',
  markers:d?.spot?.value?[{x:d.spot.value,label:'Spot'}]:[],series:[{name:'Call IV',color:S1,values:rows.map(r=>r.CE?.iv!=null&&!r.CE.stale?r.CE.iv*100:null)},{name:'Put IV',color:S2,values:rows.map(r=>r.PE?.iv!=null&&!r.PE.stale?r.PE.iv*100:null)}]})}
const CH_EMPTY_HELP='<b>No live data.</b> No live option quote has reached this chain. With Kotak Neo the chain fills by itself once the login succeeds and the day\'s instrument master is loaded (KOTAK_AUTO_CHAIN_JSON keeps strikes around spot subscribed). Subscribe the index (for Kotak, "nse_cm|Nifty 50") for an exact spot.';
function chRenderEmpty(status='NO LIVE DATA',html=CH_EMPTY_HELP){chCharts(null);const cols=chCols();chHead(cols);chStats({});chSetStatus(status,'bad');$('chUpdated').textContent='';
 $('chRows').innerHTML=`<tr><td class="empty" colspan="${cols.length*2+1}">${html}</td></tr>`}
function chCell(leg,key,typ,strike,maxOI){if(!leg)return'<td></td>';
 // A stale leg shows blank in every column: no last-known price is ever presented as live.
 if(leg.stale)return`<td class="stale" title="No live quote: last update ${esc(Math.round(leg.age_ms/1000))} s ago">—</td>`;
 const c=[];if(leg.itm)c.push('itm');let v,st='',title='';
 switch(key){case'oi':{const w=maxOI?Math.round(leg.oi/maxOI*100):0;st=` style="background-image:linear-gradient(${typ==='CE'?'to left':'to right'},${typ==='CE'?'rgba(255,116,116,.28)':'rgba(98,230,169,.25)'} ${w}%,transparent ${w}%)"`;v=compact(leg.oi);break}
  case'oi_change':v=signed(leg.oi_change);if(leg.oi_change)c.push(leg.oi_change>0?'pos':'neg');break;
  case'volume':v=compact(leg.volume);break;case'iv':v=ivp(leg.iv);if(leg.iv_source)title=(title?title+' · ':'')+'IV source: '+leg.iv_source;break;
  case'delta':v=dp(leg.delta,2);break;case'gamma':v=dp(leg.gamma,4);break;case'theta':v=dp(leg.theta,2);break;case'vega':v=dp(leg.vega,2);break;
  case'bid':return`<td class="${c.join(' ')}" title="${esc(title)}">${leg.bid>0?`<button class="q sell" data-k="${esc(strike)}" data-t="${typ}" data-s="SELL" title="Sell (write) at bid">${px(leg.bid)}</button>`:'—'}</td>`;
  case'ask':return`<td class="${c.join(' ')}" title="${esc(title)}">${leg.ask>0?`<button class="q buy" data-k="${esc(strike)}" data-t="${typ}" data-s="BUY" title="Buy at ask">${px(leg.ask)}</button>`:'—'}</td>`;
  case'ltp':v=px(leg.ltp);c.push('ltp');break}
 return`<td class="${c.join(' ')}" title="${esc(title)}"${st}>${esc(v)}</td>`}
function chRender(){const d=CH.data;if(!d||!(d.strikes||[]).length){chRenderEmpty();return}
 const legs=d.strikes.flatMap(r=>[r.CE,r.PE]).filter(Boolean);
 if(!d.live){const age=legs.length?Math.round(Math.min(...legs.map(l=>l.age_ms))/1000):null;
  chRenderEmpty('NO LIVE DATA',`<b>No live data.</b> No quote in this chain is live${age!=null?` (last tick ${esc(dur(age))} ago)`:''}. Values reappear as soon as the broker feed delivers again.`);
  if(CH.tk){CH.tk.leg=Object.assign({},CH.tk.leg,{stale:true});tkRender()}return}
 const cols=chCols(),pc=[...cols].reverse(),S=d.spot&&d.spot.value,maxOI=Math.max(1,...d.strikes.flatMap(r=>[live(r.CE)?r.CE.oi:0,live(r.PE)?r.PE.oi:0]));chHead(cols);
 const spotRow=()=>`<tr class="spotrow"><td colspan="${cols.length*2+1}">SPOT ${esc(fmt(S))} · ${d.spot.source==='SPOT_TOKEN'?esc((d.spot.symbol||'spot token')+(d.spot.broker?' · '+d.spot.broker:'')):'put-call parity estimate'}</td></tr>`;
 let html='',spotDone=!S;for(const r of d.strikes){if(!spotDone&&r.strike>S){html+=spotRow();spotDone=true}
  html+=`<tr class="${r.strike===d.atm_strike?'atm':''}">`+cols.map(([k])=>chCell(r.CE,k,'CE',r.strike,maxOI)).join('')
   +`<td class="k">${esc(fmt(r.strike))}<span class="pcr">${r.pcr!=null?'PCR '+r.pcr.toFixed(2):''}</span></td>`+pc.map(([k])=>chCell(r.PE,k,'PE',r.strike,maxOI)).join('')+'</tr>'}
 if(!spotDone)html+=spotRow();$('chRows').innerHTML=html;chStats(d);chCharts(d);
 chSetStatus(d.stale_legs?`LIVE · ${d.stale_legs} NO QUOTE`:'LIVE',d.stale_legs?'warn':'ok');
 const fresh=legs.filter(l=>!l.stale);$('chUpdated').textContent=fresh.length?'Last tick '+dur(Math.min(...fresh.map(l=>l.age_ms))/1000)+' ago':'';
 // First render of a selection: centre the ATM row vertically and the strike column horizontally (narrow screens).
 if(CH.scroll&&d.atm_strike!=null){const row=$('chRows').querySelector('tr.atm');if(row){const w=$('chWrap'),k=row.querySelector('td.k');
  w.scrollTop=Math.max(0,row.offsetTop-w.clientHeight/2);w.scrollLeft=Math.max(0,k.offsetLeft+k.offsetWidth/2-w.clientWidth/2);CH.scroll=false}}
 if(CH.tk&&d.exchange===CH.tk.exchange&&d.underlying===CH.tk.underlying&&d.expiry===CH.tk.expiry){const row=d.strikes.find(x=>x.strike===CH.tk.strike),leg=row&&row[CH.tk.typ];
  if(leg&&leg.instrument_token===CH.tk.leg.instrument_token){CH.tk.leg=leg;tkRender()}}}
// Limit price only from a live same-side quote: a last trade or a stale quote is never offered as executable.
const tkQuotePx=(leg,side)=>{const q=side==='SELL'?leg.bid:leg.ask;return !leg.stale&&q>0?q.toFixed(2):''};
function chOpenTicket(strike,typ,side){const d=CH.data,row=d&&d.strikes.find(r=>r.strike===strike),leg=row&&row[typ];if(!leg||leg.stale)return;
 CH.tk={exchange:d.exchange,underlying:d.underlying,expiry:d.expiry,strike,typ,leg,side,cid:newCid()};
 $('tkLots').value=1;$('tkPrice').value=tkQuotePx(leg,side);$('tkResult').textContent='';$('tkResult').className='';
 tkRender();if(window.innerWidth<900)$('tkBody').scrollIntoView({behavior:'smooth',block:'start'})}
function tkQty(){const L=CH.tk.leg;return Math.max(1,parseInt($('tkLots').value,10)||1)*(L.lot_size||1)}
function tkRender(){const tk=CH.tk;$('tkBody').hidden=!tk;$('tkEmpty').hidden=!!tk;if(!tk)return;
 const L=tk.leg,known='live_trading' in CH.policy,live=!!CH.policy.live_trading,sell=tk.side==='SELL',qty=tkQty();
 const types=['LIMIT',...(CH.policy.market_orders_allowed?['MARKET']:[])];setOptions($('tkType'),types.map(x=>[x,x]),types.includes($('tkType').value)?$('tkType').value:'LIMIT');
 const mkt=$('tkType').value==='MARKET';$('tkPrice').disabled=mkt;
 $('tkSym').textContent=L.symbol;$('tkQty').value=qty;$('tkCid').value=tk.cid;
 $('tkMeta').innerHTML=`${esc(tk.underlying)} ${esc(fmt(tk.strike))} ${esc(tk.typ)} · ${esc(expLabel(tk.expiry))}<br>${esc(L.broker)} · token ${esc(L.instrument_token)} · lot ${esc(L.lot_size??'unknown')}<br><span class="badge ${!known?'bad':live?'warn':''}">${!known?'ORDER POLICY UNKNOWN':live?'ORDER ROUTING LIVE':'ORDER ROUTING LOCKED'}</span>`;
 $('tkQuote').innerHTML=L.stale?'<span class="bad">No live quote.</span> Bid, ask and LTP are blank until the feed delivers again.':`Bid <b class="ceh">${px(L.bid)}</b> · Ask <b class="peh">${px(L.ask)}</b> · LTP <b>${px(L.ltp)}</b> · IV ${ivp(L.iv)} · Δ ${dp(L.delta,2)}`;
 $('tkSellBtn').classList.toggle('on',sell);$('tkBuyBtn').classList.toggle('on',!sell);
 const b=$('tkSubmit');b.textContent=sell?'Place SELL order':'Place BUY order';b.classList.toggle('red',sell);b.classList.toggle('green',!sell);
 const p=L.stale?NaN:mkt?(sell?L.bid:L.ask):parseFloat($('tkPrice').value);$('tkPremLbl').textContent=(sell?'Premium credit':'Premium debit')+(mkt?' (est.)':'');$('tkPrem').textContent=p>0?fmt(p*qty):'—';
 const w=[];if(!L.lot_size)w.push('Lot size unknown: the instrument master for this broker is not loaded. The order path will block this order.');
 if(sell&&CH.policy.naked_short_options_blocked)w.push('Naked short options are blocked. A new short needs an equal long option of the same underlying, expiry and type already in the book.');
 if(L.stale)w.push('No live quote for this contract. The order path requires a live two-sided quote.');
 else if(!(sell?L.bid>0:L.ask>0))w.push(`No live ${sell?'bid':'ask'}: there is no price to ${sell?'sell':'buy'} at.`);
 if(!known)w.push('Order policy not loaded from the backend: orders are disabled until it is.');
 else if(!live)w.push('Order routing is locked (LIVE_TRADING=false): the order is refused with LIVE_TRADING_DISABLED before any broker.');
 $('tkWarn').innerHTML=w.map(x=>`<div class="warnbox">${esc(x)}</div>`).join('');
 $('tkSubmit').disabled=CH.tkBusy||!known||!!L.stale}
async function tkSubmit(){const tk=CH.tk;if(!tk||CH.tkBusy)return;const L=tk.leg,qty=tkQty(),type=$('tkType').value,price=parseFloat($('tkPrice').value),token=$('tkOp').value.trim(),live=!!CH.policy.live_trading;
 if(!('live_trading' in CH.policy)||L.stale){alert('No live quote or order policy: the order was not sent.');return}
 if(type==='LIMIT'&&!(price>0)){alert('Enter a limit price');return}if(live&&!token){alert('Operator token required while order routing is live');return}
 if(!confirm(`${tk.side} ${qty} × ${L.symbol}\n${type==='LIMIT'?'LIMIT @ '+price.toFixed(2):'MARKET'} · ${$('tkProd').value} · ${L.broker}\n\n${live?'ORDER ROUTING LIVE: this can place a real order.':'ORDER ROUTING LOCKED: the order will be refused before any broker.'}`))return;
 const body={broker:L.broker,exchange:tk.exchange,symbol:L.symbol,side:tk.side,qty,order_type:type,product:$('tkProd').value,instrument_token:L.instrument_token,client_order_id:tk.cid};
 if(type==='LIMIT')body.price=Math.round(price*100)/100;
 CH.tkBusy=true;$('tkSubmit').disabled=true;$('tkResult').className='';$('tkResult').textContent='Submitting…';
 try{const r=await fetch('/orders',{method:'POST',headers:Object.assign({'Content-Type':'application/json'},token?{'X-IORT-Operator-Token':token}:{}),body:JSON.stringify(body)});
  let j;try{j=await r.json()}catch{j={http:r.status}}
  tk.cid=newCid();$('tkCid').value=tk.cid; // a response arrived, so the next submit is a new order
  const bad=!r.ok||['BLOCKED','REJECTED','UNKNOWN'].includes(j.status);
  const why=j.reason||(Array.isArray(j.detail)?j.detail.map(x=>x.msg).join('; '):j.detail)||'';
  $('tkResult').className=bad?'bad':'ok';$('tkResult').textContent=`${j.status||('HTTP '+r.status)}${why?'\n'+why:''}\n\n${JSON.stringify(j,null,2)}`;
  logEvent('executionEvents',{type:'CHAIN_ORDER',payload:{request:body,response:j}});refresh()}
 catch{$('tkResult').className='warn';$('tkResult').textContent='No response from the backend. Retrying is safe: the same client order ID is reused, so the order cannot be placed twice.'}
 finally{CH.tkBusy=false;tkRender()}}
function openChain(sym,exp){CH.sel.sym=sym;CH.sel.exp=exp;CH.scroll=true;chSave();showTab('chain')}
// Backend unreachable (core watchdog): blank the chain too.
window.addEventListener('iort:state',e=>{if(e.detail===null&&CH.active)chUnavailable()});
function chInit(){
 $('chSym').onchange=e=>{CH.sel.sym=e.target.value;CH.sel.exp='';CH.scroll=true;chSave();chFillSelectors();chLoadLadder()};
 $('chExp').onchange=e=>{CH.sel.exp=e.target.value;CH.scroll=true;chSave();chLoadLadder()};
 $('chDepth').onchange=e=>{CH.sel.depth=e.target.value;CH.scroll=true;chSave();chLoadLadder()};
 $('chGreeks').onchange=e=>{CH.sel.greeks=e.target.checked;chSave();chRender()};
 const wrap=$('chWrap');wrap.addEventListener('pointerdown',()=>{CH.hold=true});
 const release=()=>{if(CH.hold){CH.hold=false;setTimeout(()=>{if(!CH.hold)chRender()},0)}};window.addEventListener('pointerup',release);window.addEventListener('pointercancel',release);
 $('chRows').addEventListener('click',e=>{const q=e.target.closest('button.q');if(q)chOpenTicket(Number(q.dataset.k),q.dataset.t,q.dataset.s)});
 $('tkSellBtn').onclick=()=>{if(CH.tk){CH.tk.side='SELL';$('tkPrice').value=tkQuotePx(CH.tk.leg,'SELL');tkRender()}};
 $('tkBuyBtn').onclick=()=>{if(CH.tk){CH.tk.side='BUY';$('tkPrice').value=tkQuotePx(CH.tk.leg,'BUY');tkRender()}};
 for(const id of['tkLots','tkPrice','tkType','tkProd'])$(id).addEventListener('input',()=>CH.tk&&tkRender());
 $('tkSubmit').onclick=tkSubmit;$('tkClear').onclick=()=>{CH.tk=null;tkRender()};
 $('op').addEventListener('input',()=>{$('tkOp').value=$('op').value});$('tkOp').addEventListener('input',()=>{$('op').value=$('tkOp').value});
 chFillSelectors();chRenderEmpty();
 setInterval(()=>{if(CH.active&&!document.hidden)chLoadLadder()},1000);setInterval(()=>{if(CH.active&&!document.hidden)chLoadIndex()},5000)}
