"use strict";
// Portfolio tab: broker-authoritative positions with model Greeks, firm aggregates in rupees,
// payoff per underlying and the full-revaluation scenario grid (GET /risk/portfolio).

const PF = { active: false, busy: false, data: null, und: null, chart: null };
const rupees = v => v == null ? '—' : (v < 0 ? '-₹' : '₹') + Charts.compactNum(Math.abs(v));
const signCls = v => v == null || v === 0 ? '' : v > 0 ? 'pos' : 'neg';

function pfTile(label, value, sub = '', valueCls = '') {
  return `<div class="stat"><div class="muted">${esc(label)}</div><div class="v ${valueCls}">${esc(value)}</div>${sub ? `<div class="muted">${sub}</div>` : ''}</div>`;
}

async function pfLoad() {
  if (PF.busy) return;
  PF.busy = true;
  try {
    const r = await fetchT('/risk/portfolio');
    if (!r.ok) throw new Error(r.status);
    PF.data = await r.json();
    pfRender();
  } catch {
    pfBlank('BACKEND UNAVAILABLE', 'No live data: the terminal backend is not responding.');
  } finally { PF.busy = false; }
}

// No live broker data: every number is blank. Nothing from an earlier load stays on screen.
const PF_TILES = [['Total P&L', ''], ['Day P&L', ''], ['Delta notional', 'Rs exposure to spot'], ['Gamma, 1% move', 'second-order P&L'],
  ['Theta per day', 'time decay'], ['Vega per vol point', 'IV +1 point'], ['Worst scenario', 'spot ±7%, vol −5 to +10'], ['Open positions', '']];
function pfBlank(status, why) {
  PF.data = null;
  $('pfStatus').textContent = status; cls($('pfStatus'), 'bad'); $('pfStatus').title = why;
  $('pfUpdated').textContent = '';
  $('pfTiles').innerHTML = PF_TILES.map(([l]) => pfTile(l, '—', 'No live broker data')).join('');
  $('pfRows').innerHTML = `<tr><td colspan="14" class="muted">${esc(why)}</td></tr>`;
  $('pfUnd').innerHTML = `<tr><td colspan="10" class="muted">${esc(why)}</td></tr>`;
  setOptions($('pfUndSel'), [], '');
  pfChart(null, 'No live broker data');
  $('pfHeat').innerHTML = `<p class="muted">${esc(why)}</p>`;
}

function pfRender() {
  const d = PF.data, t = d.totals, sc = d.scenarios, brokers = Object.entries(d.brokers);
  const bad = brokers.filter(([, b]) => !b.ok);
  if (!brokers.length) { pfBlank('NO LIVE BROKER DATA', 'No live broker data. Positions always come from the broker, never from this terminal\'s own records.'); return; }
  if (bad.length === brokers.length) { pfBlank('NO LIVE BROKER DATA', 'No live broker data: ' + bad.map(([n, b]) => `${n}: ${b.error || (b.issues || []).join(', ') || 'not usable'}`).join(' · ')); return; }
  const [st, sc_] = !brokers.length ? ['NO BROKER SNAPSHOTS', 'warn'] : bad.length ? ['BROKER DATA INCOMPLETE', 'bad']
    : !t.positions ? ['FLAT', 'ok'] : t.complete ? ['ALL POSITIONS PRICED', 'ok'] : ['SOME POSITIONS UNPRICED', 'warn'];
  $('pfStatus').textContent = st; cls($('pfStatus'), sc_);
  $('pfStatus').title = bad.map(([n, b]) => `${n}: ${b.error || (b.issues || []).join(', ')}`).join('\n');
  const age = Math.max(...brokers.filter(([, b]) => b.ok).map(([, b]) => b.age_sec ?? 0));
  $('pfUpdated').textContent = 'Broker data ' + dur(age) + ' old' + (bad.length ? ' · missing: ' + bad.map(([n, b]) => `${n}: ${b.error || 'issues'}`).join(' · ') : '');
  // Firm totals are shown only when every broker reported: a partial sum is not a firm total.
  if (bad.length) {
    $('pfTiles').innerHTML = PF_TILES.map(([l]) => pfTile(l, '—', 'incomplete: ' + bad.map(([n]) => n).join(', ') + ' missing')).join('');
  } else $('pfTiles').innerHTML =
    pfTile('Total P&L', rupees(t.total_pnl), '', signCls(t.total_pnl)) +
    pfTile('Day P&L', rupees(t.day_pnl), '', signCls(t.day_pnl)) +
    pfTile('Delta notional', rupees(t.delta_notional), 'Rs exposure to spot') +
    pfTile('Gamma, 1% move', rupees(t.gamma_pnl_1pct), 'second-order P&L', signCls(t.gamma_pnl_1pct)) +
    pfTile('Theta per day', rupees(t.theta), 'time decay', signCls(t.theta)) +
    pfTile('Vega per vol point', rupees(t.vega), 'IV +1 point', signCls(t.vega)) +
    pfTile('Worst scenario', sc.ok && t.positions ? rupees(sc.worst_pnl) : '—', sc.ok ? (t.positions ? 'spot ±7%, vol −5 to +10' : 'no positions') : esc((sc.errors || [])[0] || ''), sc.ok ? signCls(sc.worst_pnl) : 'neg') +
    pfTile('Open positions', String(t.positions), t.complete ? 'all priced' : 'see Status column');

  $('pfRows').innerHTML = d.positions.map(p => {
    const type = p.kind === 'OPT' ? `${fmt(p.strike)} ${p.option_type}` : p.kind;
    return `<tr><td>${esc(p.broker)}</td><td>${esc(p.symbol)}</td><td>${esc(type)}</td><td>${esc(p.expiry || '—')}</td><td>${p.lots == null ? '—' : esc(p.lots)}</td>
      <td class="${signCls(p.qty)}">${esc(p.qty)}</td><td>${p.price_source === 'FEED' ? px(p.price) : p.price > 0 ? `${px(p.price)} <span class="muted" title="No live feed tick for this contract: this is the broker's own last price from its positions report">broker</span>` : '—'}</td><td class="${signCls(p.pnl)}">${fmt(p.pnl)}</td><td>${ivp(p.iv)}</td>
      <td>${dp(p.delta, 1)}</td><td class="${signCls(p.theta)}">${fmt(p.theta)}</td><td class="${signCls(p.vega)}">${fmt(p.vega)}</td><td>${fmt(p.delta_notional)}</td>
      <td class="${p.issue ? 'bad' : 'ok'}">${esc(p.issue || 'PRICED')}</td></tr>`;
  }).join('') || `<tr><td colspan="14" class="muted">${bad.length ? 'No positions from the brokers that reported; ' + bad.map(([n]) => n).join(', ') + ' did not report.' : 'No open positions (confirmed by the broker).'}</td></tr>`;

  $('pfUnd').innerHTML = d.underlyings.map(u => `<tr><td><b>${esc(u.underlying)}</b></td><td>${u.spot ? fmt(u.spot) : '—'} <span class="muted">${esc(u.spot_source || '')}</span></td>
    <td>${esc(u.positions)}${u.complete ? '' : ' <span class="warn">(incomplete)</span>'}</td><td>${esc(u.short_option_qty)} / ${esc(u.long_option_qty)}</td>
    <td class="${signCls(u.pnl)}">${fmt(u.pnl)}</td><td>${fmt(u.delta_notional)}</td><td class="${signCls(u.gamma_pnl_1pct)}">${fmt(u.gamma_pnl_1pct)}</td>
    <td class="${signCls(u.theta)}">${fmt(u.theta)}</td><td class="${signCls(u.vega)}">${fmt(u.vega)}</td>
    <td>${u.payoff ? esc(u.payoff.breakevens.map(fmt).join(' · ') || 'none in ±10%') : '—'}</td></tr>`).join('')
    || `<tr><td colspan="10" class="muted">${bad.length ? 'Incomplete broker data.' : 'No open positions (confirmed by the broker).'}</td></tr>`;

  const withPayoff = d.underlyings.filter(u => u.payoff);
  if (!withPayoff.some(u => u.underlying === PF.und)) PF.und = withPayoff[0]?.underlying || null;
  setOptions($('pfUndSel'), withPayoff.map(u => [u.underlying, u.underlying]), PF.und || '');
  pfChart(withPayoff.find(u => u.underlying === PF.und));

  if (sc.ok && sc.scenarios.length) {
    const spots = [...new Set(sc.scenarios.map(g => g.spot_shock))].sort((a, b) => a - b);
    const vols = [...new Set(sc.scenarios.map(g => g.vol_shock_pts))].sort((a, b) => a - b);
    const at = new Map(sc.scenarios.map(g => [g.spot_shock + '|' + g.vol_shock_pts, g.pnl]));
    Charts.heatmapTable($('pfHeat'), {
      rows: spots.map(v => (v > 0 ? '+' : '') + (v * 100).toFixed(0) + '%'), cols: vols.map(v => (v > 0 ? '+' : '') + v + ' vol'),
      value: (i, j) => at.get(spots[i] + '|' + vols[j]), fmt: rupees, rowHead: 'Spot', colHead: 'IV', caption: 'P&L change from now, firm-wide',
    });
  } else {
    $('pfHeat').innerHTML = `<p class="${t.positions ? 'bad' : 'muted'}">${t.positions ? 'Scenario grid unavailable: ' + esc((sc.errors || []).join(', ')) : 'No open positions to revalue (confirmed by the broker).'}</p>`;
  }
}

function pfChart(u, emptyText = 'No priced option positions') {
  const S1 = 'var(--series-1)', S2 = 'var(--series-2)';
  const o = !u ? { x: [], series: [], emptyText } : {
    x: u.payoff.spots, xName: u.underlying, xFmt: v => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 }), yFmt: rupees, zero: true, height: 280,
    title: `${u.underlying} payoff at expiry and today`,
    markers: [{ x: u.spot, label: 'Spot' }, ...u.payoff.breakevens.map(b => ({ x: b, label: 'BE' }))],
    series: [{ name: 'At expiry ' + (u.payoff.target_expiry || ''), color: S1, values: u.payoff.expiry }, { name: 'Today (T+0)', color: S2, values: u.payoff.today }],
  };
  if (PF.chart) PF.chart.update(o); else PF.chart = new Charts.LineChart($('pfChart'), o);
}

window.addEventListener('iort:state', e => { if (e.detail === null && PF.active) pfBlank('BACKEND UNAVAILABLE', 'No live data: the terminal backend is not responding.'); });
$('pfUndSel').addEventListener('change', e => { PF.und = e.target.value; if (PF.data) pfRender(); });
window.addEventListener('iort:tab', e => { PF.active = e.detail === 'portfolio'; if (PF.active) pfLoad(); });
setInterval(() => { if (PF.active && !document.hidden) pfLoad(); }, 2000);
