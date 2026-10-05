"use strict";
// Strategy tab: option-writing structures built from the live chain, analysed server-side
// (POST /strategy/analyze) and executed leg by leg through POST /orders, hedges first.
// Every leg still passes every pre-trade control; nothing here bypasses the order path.

const ST = { active: false, sel: { sym: '', exp: '', tpl: 'iron_condor' }, templates: [], legs: [], strikes: [], a: null,
             busy: false, dirty: false, chart: null, execBusy: false, timer: null };
try { Object.assign(ST.sel, JSON.parse(localStorage.getItem('iort.strategy') || '{}')); } catch { /* storage unavailable */ }
const stSave = () => { try { localStorage.setItem('iort.strategy', JSON.stringify(ST.sel)); } catch { /* ignore */ } };
const stParts = () => { const i = ST.sel.sym.indexOf('|'); return [ST.sel.sym.slice(0, i), ST.sel.sym.slice(i + 1)]; };
const tplLabel = n => n.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());

async function stInit() {
  if (!ST.templates.length) {
    try { ST.templates = (await (await fetch('/strategy/templates')).json()).templates; } catch { ST.templates = []; }
    setOptions($('stTpl'), ST.templates.map(t => [t.name, tplLabel(t.name)]), ST.sel.tpl);
  }
  await chLoadIndex();
  stFillSelectors();
}
function stFillSelectors() {
  const key = c => c.exchange + '|' + c.underlying, syms = CH.idx.map(key);
  if (!syms.length) { setOptions($('stSym'), [], ''); setOptions($('stExp'), [], ''); stStatus('No option chains are live yet.', 'warn'); return; }
  if (!syms.includes(ST.sel.sym)) ST.sel.sym = syms[0];
  setOptions($('stSym'), CH.idx.map(c => [key(c), c.underlying + ' · ' + c.exchange]), ST.sel.sym);
  const exps = (CH.idx.find(c => key(c) === ST.sel.sym) || {}).expiries || [];
  if (!exps.includes(ST.sel.exp)) ST.sel.exp = exps[0] || '';
  setOptions($('stExp'), exps.map(e => [e, expLabel(e)]), ST.sel.exp);
}
function stStatus(text, c = '') { $('stStatus').textContent = text; $('stStatus').className = c || 'muted'; }

async function stLoadStrikes() {
  const [ex, und] = stParts();
  try {
    const r = await fetchT(`/option-chain/${encodeURIComponent(ex)}/${encodeURIComponent(und)}/${encodeURIComponent(ST.sel.exp)}/ladder`);
    ST.strikes = (await r.json()).strikes.map(s => s.strike);
  } catch { ST.strikes = []; }
}

async function stBuild() {
  const [ex, und] = stParts();
  if (!ex || !ST.sel.exp) return;
  stStatus('Building…');
  const q = new URLSearchParams({ lots: $('stLots').value || 1, delta: $('stDelta').value || 0.2, wing: $('stWing').value || 4 });
  try {
    const r = await fetchT(`/strategy/template/${encodeURIComponent(ex)}/${encodeURIComponent(und)}/${encodeURIComponent(ST.sel.exp)}/${encodeURIComponent(ST.sel.tpl)}?${q}`, {}, 5000);
    let j; try { j = await r.json(); } catch { j = { reason: 'HTTP ' + r.status }; }
    if (!r.ok) { ST.a = null; stStatus('Cannot build: ' + (j.reason || r.status), 'bad'); stRender(); return; }
    await stLoadStrikes();
    ST.legs = j.legs.map(l => ({ side: l.side, strike: l.strike, option_type: l.option_type, lots: l.lots }));
    ST.a = j; stStatus(`${tplLabel(ST.sel.tpl)} built from the live chain`, 'ok'); stRender();
  } catch { ST.a = null; stStatus('Backend unavailable: no live data. Try again.', 'bad'); stRender(); }
}

async function stAnalyze() {
  if (ST.busy || !ST.legs.length) { if (!ST.legs.length) { ST.a = null; stRender(); } return; }
  const [ex, und] = stParts();
  ST.busy = true;
  try {
    const r = await fetchT('/strategy/analyze', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ exchange: ex, underlying: und, expiry: ST.sel.exp, legs: ST.legs }) });
    let j; try { j = await r.json(); } catch { j = { reason: 'HTTP ' + r.status }; }
    // A failed re-analysis clears the numbers: an old analysis is never left on screen as current.
    if (!r.ok) { ST.a = null; stStatus('No live analysis: ' + (j.reason || r.status), 'bad'); stRender(); return; }
    ST.a = j; stRender();
  } catch { ST.a = null; stStatus('Backend unavailable: no live data', 'bad'); stRender(); } finally { ST.busy = false; }
}
const stAnalyzeSoon = () => { clearTimeout(ST.timer); ST.timer = setTimeout(stAnalyze, 250); };

function stRender() {
  const a = ST.a, s = a?.stats, g = a?.greeks;
  $('stTiles').innerHTML = !a ? '<span class="muted">Pick a template and build it from the live chain, or add legs by hand.</span>' :
    pfTile('Net credit', rupees(a.net_credit), a.net_credit >= 0 ? 'premium received' : 'premium paid', signCls(a.net_credit)) +
    pfTile('Max profit', s ? (s.unbounded_profit ? 'Unlimited' : rupees(s.max_profit)) : '—', 'at expiry', 'pos') +
    pfTile('Max loss', s ? (s.unbounded_loss ? 'Unlimited' : rupees(s.max_loss)) : '—', s?.unbounded_loss ? 'add wings to cap it' : 'at expiry', 'neg') +
    pfTile('Breakevens', s && s.breakevens.length ? s.breakevens.map(v => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })).join(' · ') : '—', a.spot ? 'spot ' + fmt(a.spot) + (a.spot_source === 'PUT_CALL_PARITY' ? ' (parity estimate)' : '') : '') +
    pfTile('Probability of profit', a.probability_of_profit != null ? (a.probability_of_profit * 100).toFixed(1) + '%' : '—', 'model, at ATM IV') +
    pfTile(a.margin_basis === 'MAX_LOSS_DEFINED_RISK' ? 'Margin estimate' : 'Margin (config estimate)', rupees(a.margin_estimate), a.margin_basis === 'MAX_LOSS_DEFINED_RISK' ? 'defined risk = max loss' : 'SHORT_OPTION_MARGIN_PCT; broker is authoritative') +
    pfTile('Return on margin', a.return_on_margin != null ? (a.return_on_margin * 100).toFixed(1) + '%' : '—', a.days_to_expiry != null ? a.days_to_expiry.toFixed(1) + ' days to expiry' : '') +
    pfTile('Theta / Vega', g ? `${rupees(g.theta)} / ${rupees(g.vega)}` : '—', g ? 'Δ notional ' + rupees(g.delta_notional) : 'Greeks need a spot');
  const strikeOpts = k => (ST.strikes.length ? ST.strikes : [k]).map(v => `<option value="${v}"${v === k ? ' selected' : ''}>${fmt(v)}</option>`).join('');
  $('stLegs').innerHTML = ST.legs.map((l, i) => {
    const r = a?.legs?.[i];
    return `<tr data-i="${i}"><td><select class="legsel" data-f="side" aria-label="Side"><option${l.side === 'SELL' ? ' selected' : ''}>SELL</option><option${l.side === 'BUY' ? ' selected' : ''}>BUY</option></select></td>
      <td><select class="legsel" data-f="strike" aria-label="Strike">${strikeOpts(l.strike)}</select></td>
      <td><select class="legsel" data-f="option_type" aria-label="Type"><option${l.option_type === 'CE' ? ' selected' : ''}>CE</option><option${l.option_type === 'PE' ? ' selected' : ''}>PE</option></select></td>
      <td><input class="legsel narrow" data-f="lots" type="number" min="1" max="100" value="${esc(l.lots)}" aria-label="Lots"></td>
      <td>${r ? px(r.price) : '—'}${r?.stale ? ' <span class="warn">stale</span>' : ''}</td><td>${r ? ivp(r.iv) : '—'}</td><td>${r ? dp(r.delta_unit, 2) : '—'}</td>
      <td><button class="linkbtn" type="button" data-remove="${i}" aria-label="Remove leg">✕</button></td></tr>`;
  }).join('') || '<tr><td colspan="8" class="muted">No legs.</td></tr>';
  const warn = (a?.warnings || []).map(w => w.startsWith('NAKED_SHORTS') ? 'Naked shorts will be blocked by the order path (' + w.split(':')[1] + '). Add a long leg of the same type, or execution will stop at that short.' : w);
  $('stWarn').innerHTML = warn.map(w => `<div class="warnbox">${esc(w)}</div>`).join('') +
    (a ? `<div class="warnbox">${!('live_trading' in CH.policy) ? 'Order policy not loaded from the backend: Execute is disabled.' : CH.policy.live_trading ? 'ORDER ROUTING LIVE: Execute sends real orders, one leg at a time.' : 'Order routing is locked (LIVE_TRADING=false): every leg is refused with LIVE_TRADING_DISABLED before any broker.'}</div>` : '');
  const S1 = 'var(--series-1)', S2 = 'var(--series-2)', c = a?.curves;
  const o = !c ? { x: [], series: [], emptyText: a ? 'Payoff needs a spot price' : 'Build a strategy to see its payoff' } : {
    x: c.spots, xName: a.underlying, xFmt: v => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 }), yFmt: rupees, zero: true, height: 300,
    title: 'Strategy payoff at expiry and today', markers: [{ x: a.spot, label: 'Spot' }, ...(s?.breakevens || []).filter(b => b >= c.spots[0] && b <= c.spots[c.spots.length - 1]).map(b => ({ x: b, label: 'BE' }))],
    series: [{ name: 'At expiry', color: S1, values: c.expiry }, { name: 'Today (T+0)', color: S2, values: c.today }],
  };
  if (ST.chart) ST.chart.update(o); else ST.chart = new Charts.LineChart($('stChart'), o);
  $('stExec').disabled = !a || ST.execBusy || !('live_trading' in CH.policy);
}

async function stExecute() {
  const a = ST.a; if (!a || ST.execBusy) return;
  const token = $('stOp').value.trim(), live = !!CH.policy.live_trading;
  if (!('live_trading' in CH.policy)) { alert('Order policy not loaded from the backend: nothing was sent.'); return; }
  if (live && !token) { alert('Operator token required while order routing is live'); return; }
  const order = a.execution_order.map(i => a.legs[i]);
  const lines = order.map((l, n) => `${n + 1}. ${l.side} ${Math.abs(l.qty)} × ${l.symbol} LIMIT @ ${l.price.toFixed(2)}`).join('\n');
  if (!confirm(`Execute ${order.length} legs, hedges first:\n\n${lines}\n\n${live ? 'ORDER ROUTING LIVE: these are real orders.' : 'ORDER ROUTING LOCKED: every leg will be refused before any broker.'}\nExecution stops at the first leg that is not accepted.`)) return;
  ST.execBusy = true; $('stExec').disabled = true;
  const basket = 'strat-' + Array.from(crypto.getRandomValues(new Uint8Array(6)), b => b.toString(16).padStart(2, '0')).join('');
  const out = [];
  try {
    for (const [n, l] of order.entries()) {
      const body = { broker: l.broker, exchange: a.exchange, symbol: l.symbol, side: l.side, qty: Math.abs(l.qty), order_type: 'LIMIT', price: l.price,
                     product: 'NRML', instrument_token: l.instrument_token, client_order_id: `${basket}-${n}` };
      let j, ok = false;
      try {
        const r = await fetch('/orders', { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/json' }, token ? { 'X-IORT-Operator-Token': token } : {}), body: JSON.stringify(body) });
        try { j = await r.json(); } catch { j = { http: r.status }; }
        ok = r.ok && !['BLOCKED', 'REJECTED', 'UNKNOWN'].includes(j.status);
      } catch { j = { status: 'NO_RESPONSE', reason: 'retry is safe: the same client order id is reused' }; }
      out.push(`${n + 1}. ${l.side} ${l.symbol}: ${j.status || 'HTTP ' + j.http}${j.reason ? ' · ' + j.reason : ''}`);
      logEvent('executionEvents', { type: 'STRATEGY_LEG', payload: { request: body, response: j } });
      if (!ok) { out.push(`Stopped: later legs were not sent.`); break; }
    }
  } finally {
    ST.execBusy = false; stRender();
    $('stResult').textContent = out.join('\n');
    toast(`Strategy execution: ${out[out.length - 1]}`, out.some(x => x.startsWith('Stopped')) ? 'warn' : 'ok');
    refresh();
  }
}

$('stSym').addEventListener('change', e => { ST.sel.sym = e.target.value; ST.sel.exp = ''; stSave(); stFillSelectors(); ST.legs = []; ST.a = null; stRender(); });
$('stExp').addEventListener('change', e => { ST.sel.exp = e.target.value; stSave(); ST.legs = []; ST.a = null; stRender(); });
$('stTpl').addEventListener('change', e => { ST.sel.tpl = e.target.value; stSave(); });
$('stBuild').addEventListener('click', stBuild);
$('stExec').addEventListener('click', stExecute);
$('stAddLeg').addEventListener('click', async () => {
  if (!ST.strikes.length) await stLoadStrikes();
  const mid = ST.a?.spot ? ST.strikes.reduce((b, k) => Math.abs(k - ST.a.spot) < Math.abs(b - ST.a.spot) ? k : b, ST.strikes[0]) : ST.strikes[Math.floor(ST.strikes.length / 2)];
  if (mid == null) { stStatus('No strikes for this expiry yet.', 'warn'); return; }
  ST.legs.push({ side: 'SELL', strike: mid, option_type: 'CE', lots: 1 }); stRender(); stAnalyzeSoon();
});
$('stLegs').addEventListener('change', e => {
  const tr = e.target.closest('tr[data-i]'), f = e.target.dataset.f; if (!tr || !f) return;
  const leg = ST.legs[+tr.dataset.i];
  leg[f] = f === 'strike' ? Number(e.target.value) : f === 'lots' ? Math.max(1, parseInt(e.target.value, 10) || 1) : e.target.value;
  stAnalyzeSoon();
});
$('stLegs').addEventListener('click', e => {
  const b = e.target.closest('[data-remove]'); if (!b) return;
  ST.legs.splice(+b.dataset.remove, 1); stRender(); stAnalyzeSoon();
});
window.addEventListener('iort:state', e => { if (e.detail === null && ST.active && ST.a) { ST.a = null; stStatus('Backend unavailable: no live data', 'bad'); stRender(); } });
window.addEventListener('iort:tab', e => { ST.active = e.detail === 'strategy'; if (ST.active) stInit().then(stRender); });
setInterval(() => { if (ST.active && !document.hidden && ST.legs.length && !ST.execBusy && !document.activeElement?.closest?.('#stLegs')) stAnalyze(); }, 3000);
registerPalette(() => ST.templates.map(t => ({ label: 'Strategy · ' + tplLabel(t.name), hint: t.description,
  run: async () => { showTab('strategy'); await stInit(); ST.sel.tpl = t.name; $('stTpl').value = t.name; stSave(); stBuild(); } })));
fetch('/strategy/templates').then(r => r.json()).then(j => {
  ST.templates = j.templates || [];
  setOptions($('stTpl'), ST.templates.map(t => [t.name, tplLabel(t.name)]), ST.sel.tpl);
}).catch(() => { /* loaded again when the tab opens */ });
