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
    const r = await fetch('/risk/portfolio', { cache: 'no-store' });
    if (!r.ok) throw new Error(r.status);
    PF.data = await r.json();
    pfRender();
  } catch {
    $('pfStatus').textContent = 'BACKEND UNAVAILABLE'; cls($('pfStatus'), 'bad');
  } finally { PF.busy = false; }
}

function pfRender() {
  const d = PF.data, t = d.totals, sc = d.scenarios, brokers = Object.entries(d.brokers);
  const bad = brokers.filter(([, b]) => !b.ok);
  const [st, sc_] = !brokers.length ? ['NO BROKER SNAPSHOTS', 'warn'] : bad.length ? ['BROKER DATA INCOMPLETE', 'bad']
    : !t.positions ? ['FLAT', 'ok'] : t.complete ? ['ALL POSITIONS PRICED', 'ok'] : ['SOME POSITIONS UNPRICED', 'warn'];
  $('pfStatus').textContent = st; cls($('pfStatus'), sc_);
  $('pfStatus').title = bad.map(([n, b]) => `${n}: ${b.error || (b.issues || []).join(', ')}`).join('\n');
  $('pfUpdated').textContent = 'Updated ' + new Date(d.as_of_ms).toLocaleTimeString() + (bad.length ? ' · ' + bad.map(([n, b]) => `${n}: ${b.error || 'issues'}`).join(' · ') : '');

  $('pfTiles').innerHTML =
    pfTile('Total P&L', rupees(t.total_pnl), '', signCls(t.total_pnl)) +
    pfTile('Day P&L', rupees(t.day_pnl), '', signCls(t.day_pnl)) +
    pfTile('Delta notional', rupees(t.delta_notional), 'Rs exposure to spot') +
    pfTile('Gamma, 1% move', rupees(t.gamma_pnl_1pct), 'second-order P&L', signCls(t.gamma_pnl_1pct)) +
    pfTile('Theta per day', rupees(t.theta), 'time decay', signCls(t.theta)) +
    pfTile('Vega per vol point', rupees(t.vega), 'IV +1 point', signCls(t.vega)) +
    pfTile('Worst scenario', sc.ok ? rupees(sc.worst_pnl) : 'UNAVAILABLE', sc.ok ? 'spot ±7%, vol −5 to +10' : esc((sc.errors || [])[0] || ''), sc.ok ? signCls(sc.worst_pnl) : 'neg') +
    pfTile('Open positions', String(t.positions), t.complete ? 'all priced' : 'see Status column');

  $('pfRows').innerHTML = d.positions.map(p => {
    const type = p.kind === 'OPT' ? `${fmt(p.strike)} ${p.option_type}` : p.kind;
    return `<tr><td>${esc(p.broker)}</td><td>${esc(p.symbol)}</td><td>${esc(type)}</td><td>${esc(p.expiry || '—')}</td><td>${p.lots == null ? '—' : esc(p.lots)}</td>
      <td class="${signCls(p.qty)}">${esc(p.qty)}</td><td>${px(p.price)}</td><td class="${signCls(p.pnl)}">${fmt(p.pnl)}</td><td>${ivp(p.iv)}</td>
      <td>${dp(p.delta, 1)}</td><td class="${signCls(p.theta)}">${fmt(p.theta)}</td><td class="${signCls(p.vega)}">${fmt(p.vega)}</td><td>${fmt(p.delta_notional)}</td>
      <td class="${p.issue ? 'bad' : 'ok'}">${esc(p.issue || 'PRICED')}</td></tr>`;
  }).join('') || `<tr><td colspan="14" class="muted">${brokers.length ? 'No open positions.' : 'No broker snapshots yet. Positions always come from the broker, never from this terminal\'s own records.'}</td></tr>`;

  $('pfUnd').innerHTML = d.underlyings.map(u => `<tr><td><b>${esc(u.underlying)}</b></td><td>${u.spot ? fmt(u.spot) : '—'} <span class="muted">${esc(u.spot_source || '')}</span></td>
    <td>${esc(u.positions)}${u.complete ? '' : ' <span class="warn">(incomplete)</span>'}</td><td>${esc(u.short_option_qty)} / ${esc(u.long_option_qty)}</td>
    <td class="${signCls(u.pnl)}">${fmt(u.pnl)}</td><td>${fmt(u.delta_notional)}</td><td class="${signCls(u.gamma_pnl_1pct)}">${fmt(u.gamma_pnl_1pct)}</td>
    <td class="${signCls(u.theta)}">${fmt(u.theta)}</td><td class="${signCls(u.vega)}">${fmt(u.vega)}</td>
    <td>${u.payoff ? esc(u.payoff.breakevens.map(fmt).join(' · ') || 'none in ±10%') : '—'}</td></tr>`).join('')
    || '<tr><td colspan="10" class="muted">No positions.</td></tr>';

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
    $('pfHeat').innerHTML = `<p class="${t.positions ? 'bad' : 'muted'}">${t.positions ? 'Scenario grid unavailable: ' + esc((sc.errors || []).join(', ')) : 'No positions to revalue.'}</p>`;
  }
}

function pfChart(u) {
  const S1 = 'var(--series-1)', S2 = 'var(--series-2)';
  const o = !u ? { x: [], series: [], emptyText: 'No priced option positions' } : {
    x: u.payoff.spots, xName: u.underlying, xFmt: v => Number(v).toLocaleString('en-IN', { maximumFractionDigits: 0 }), yFmt: rupees, zero: true, height: 280,
    title: `${u.underlying} payoff at expiry and today`,
    markers: [{ x: u.spot, label: 'Spot' }, ...u.payoff.breakevens.map(b => ({ x: b, label: 'BE' }))],
    series: [{ name: 'At expiry ' + (u.payoff.target_expiry || ''), color: S1, values: u.payoff.expiry }, { name: 'Today (T+0)', color: S2, values: u.payoff.today }],
  };
  if (PF.chart) PF.chart.update(o); else PF.chart = new Charts.LineChart($('pfChart'), o);
}

$('pfUndSel').addEventListener('change', e => { PF.und = e.target.value; if (PF.data) pfRender(); });
window.addEventListener('iort:tab', e => { PF.active = e.detail === 'portfolio'; if (PF.active) pfLoad(); });
setInterval(() => { if (PF.active && !document.hidden) pfLoad(); }, 2000);
