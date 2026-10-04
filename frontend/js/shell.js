"use strict";
// Shell: IST clock and exchange sessions, feed latency, toasts, command palette, shortcuts.

// ---------------------------------------------------------------- clock and sessions
// Exchange holidays are not modelled here; a holiday simply produces no ticks.
const IST_OFFSET_MS = 5.5 * 3600e3;
function istParts(now = Date.now()) {
  const d = new Date(now + IST_OFFSET_MS);
  return { day: d.getUTCDay(), min: d.getUTCHours() * 60 + d.getUTCMinutes(), text: d.toISOString().slice(11, 19) };
}
function nthSundayUtc(y, month, n) {
  const first = new Date(Date.UTC(y, month, 1));
  return new Date(Date.UTC(y, month, 1 + ((7 - first.getUTCDay()) % 7) + 7 * (n - 1), 7));
}
function usDaylightSaving(now = new Date()) {
  const y = now.getUTCFullYear();
  return now >= nthSundayUtc(y, 2, 2) && now < nthSundayUtc(y, 10, 1);
}
function sessionState(p, open, close, preOpen) {
  if (p.day === 0 || p.day === 6) return ['CLOSED', ''];
  if (preOpen != null && p.min >= preOpen && p.min < open) return ['PRE-OPEN', 'warn'];
  return p.min >= open && p.min < close ? ['OPEN', 'ok'] : ['CLOSED', ''];
}
function tickClock() {
  const p = istParts();
  $('clock').textContent = 'IST ' + p.text;
  const [n, nc] = sessionState(p, 9 * 60 + 15, 15 * 60 + 30, 9 * 60);
  $('nsePill').textContent = 'NSE ' + n; cls($('nsePill'), nc);
  const mcxClose = usDaylightSaving() ? 23 * 60 + 30 : 23 * 60 + 55; // MCX follows US daylight saving
  const [m, mc] = sessionState(p, 9 * 60, mcxClose);
  $('mcxPill').textContent = 'MCX ' + m; cls($('mcxPill'), mc);
}

window.addEventListener('iort:state', ev => {
  const skew = Object.values(ev.detail.market?.stats?.median_skew_ms || {}).filter(v => v != null);
  const el = $('latPill');
  if (!skew.length) { el.textContent = 'FEED —'; cls(el, ''); return; }
  const worst = Math.max(...skew);
  el.textContent = `FEED ${Math.round(worst)} ms`;
  cls(el, worst < 500 ? 'ok' : worst < 1500 ? 'warn' : 'bad');
});

// ---------------------------------------------------------------- toasts
const recentToasts = new Map();
function toast(text, kind = 'ok', ttl = 6000) {
  const key = kind + text, now = Date.now();
  if (now - (recentToasts.get(key) || 0) < 10000) return; // same message within 10 s: shown once
  recentToasts.set(key, now);
  const box = $('toasts'), el = document.createElement('div');
  el.className = 'toast ' + kind;
  el.setAttribute('role', kind === 'bad' ? 'alert' : 'status');
  const msg = document.createElement('span'); msg.textContent = text;
  const close = document.createElement('button'); close.type = 'button'; close.textContent = '×'; close.setAttribute('aria-label', 'Dismiss');
  close.onclick = () => el.remove();
  el.append(msg, close); box.prepend(el);
  while (box.children.length > 5) box.lastChild.remove();
  setTimeout(() => el.remove(), kind === 'bad' ? ttl * 2 : ttl);
}
const p_ = e => e.payload || {};
const TOAST_RULES = {
  KILL_SWITCH: ['bad', e => `Kill switch engaged: ${p_(e).reason || ''} (${p_(e).actor || ''})`],
  KILL_SWITCH_RESET: ['ok', () => 'Kill switch reset'],
  EMERGENCY_STOP: ['bad', e => `Emergency stop on ${p_(e).broker || ''}`],
  RISK_BLOCK: ['warn', e => `Order blocked: ${p_(e).reason || ''} ${p_(e).symbol || ''}`],
  RISK_ALERT: ['warn', e => `Risk alert: ${p_(e).reason || ''}`],
  FILL: ['ok', e => `Fill ${p_(e).side || ''} ${p_(e).qty || p_(e).filled_qty || ''} ${p_(e).symbol || ''} @ ${p_(e).price || p_(e).avg_price || ''}`],
  INSTRUMENT_MASTER_LOADED: ['ok', e => `${p_(e).broker} instrument master loaded: ${fmt(p_(e).count)} contracts`],
  INSTRUMENT_MASTER_LOAD_ERROR: ['warn', e => `${p_(e).broker} instrument master failed; retry in ${p_(e).retry_in_sec}s`],
  STREAM_ERROR: ['warn', e => `${e.broker} stream: ${String(e.error || '').slice(0, 90)}`],
};
window.addEventListener('iort:event', ev => {
  const rule = TOAST_RULES[ev.detail.type];
  if (rule) toast(rule[1](ev.detail), rule[0]);
});

// ---------------------------------------------------------------- overlays
let overlayReturnFocus = null;
function openOverlay(id) {
  closeOverlays();
  overlayReturnFocus = document.activeElement;
  const el = $(id); el.hidden = false;
  (el.querySelector('input') || el.querySelector('button')).focus();
}
function closeOverlays() {
  let closed = false;
  document.querySelectorAll('.overlay').forEach(o => { if (!o.hidden) { o.hidden = true; closed = true; } });
  if (closed && overlayReturnFocus && overlayReturnFocus.focus) overlayReturnFocus.focus();
  return closed;
}
document.querySelectorAll('.overlay').forEach(o => o.addEventListener('mousedown', e => { if (e.target === o) closeOverlays(); }));

// ---------------------------------------------------------------- command palette
const PALETTE_PROVIDERS = [];
const PAL = { items: [], shown: [], sel: 0 };
function registerPalette(fn) { PALETTE_PROVIDERS.push(fn); }
function fuzzyScore(q, s) {
  q = q.toLowerCase(); s = s.toLowerCase();
  if (!q) return 1;
  let i = 0, score = 0, last = -2;
  for (let j = 0; j < s.length && i < q.length; j++) {
    if (s[j] === q[i]) { score += j === last + 1 ? 3 : 1; if (j === 0 || s[j - 1] === ' ') score += 2; last = j; i++; }
  }
  if (i < q.length) return 0;
  return score + (s.startsWith(q) ? 6 : 0); // labels that start with the query rank first
}
function paletteItems() {
  const items = TABS.map(([id, label], i) => ({ label: 'Go to ' + label, hint: i < 9 ? 'Alt+' + (i + 1) : '', run: () => showTab(id) }));
  (CH.idx || []).forEach(c => c.expiries.forEach(e => items.push({
    label: `Option chain · ${c.underlying} · ${expLabel(e)}`, hint: c.exchange, run: () => openChain(c.exchange + '|' + c.underlying, e) })));
  items.push(
    { label: 'Toggle Greeks in the option chain', hint: 'G', run: () => { showTab('chain'); $('chGreeks').click(); } },
    { label: 'Refresh data now', hint: 'R', run: () => refresh() },
    { label: 'Reconcile selected broker now…', hint: 'operator', run: () => { showTab('execution'); reconcileNow(); } },
    { label: 'Engage kill switch…', hint: 'confirm', danger: true, run: () => { showTab('execution'); if (confirm('Engage the kill switch now? New orders will be blocked.')) panic(); } },
    { label: 'Open Prometheus metrics', hint: '/metrics', run: () => window.open('/metrics', '_blank', 'noopener') },
    { label: 'Open API reference', hint: '/docs', run: () => window.open('/docs', '_blank', 'noopener') },
    { label: 'Keyboard shortcuts', hint: '?', run: () => openOverlay('help') });
  PALETTE_PROVIDERS.forEach(fn => { try { items.push(...fn()); } catch { /* a provider failing never breaks the palette */ } });
  return items;
}
function paletteRender() {
  const q = $('paletteInput').value.trim();
  PAL.shown = PAL.items.map(it => ({ it, s: fuzzyScore(q, it.label) })).filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 40).map(x => x.it);
  PAL.sel = Math.min(PAL.sel, Math.max(0, PAL.shown.length - 1));
  $('paletteList').innerHTML = PAL.shown.map((it, i) => `<li role="option" id="pal-${i}" aria-selected="${i === PAL.sel}" class="${it.danger ? 'danger' : ''}" data-i="${i}"><span>${esc(it.label)}</span><span class="muted">${esc(it.hint || '')}</span></li>`).join('')
    || '<li class="muted" role="option" aria-disabled="true">No match</li>';
  $('paletteInput').setAttribute('aria-activedescendant', PAL.shown.length ? 'pal-' + PAL.sel : '');
  const cur = $('pal-' + PAL.sel); if (cur) cur.scrollIntoView({ block: 'nearest' });
}
function openPalette() {
  PAL.items = paletteItems(); PAL.sel = 0;
  $('paletteInput').value = '';
  openOverlay('palette'); paletteRender();
}
function paletteRun(i) {
  const it = PAL.shown[i]; if (!it) return;
  closeOverlays(); it.run();
}
$('paletteInput').addEventListener('input', () => { PAL.sel = 0; paletteRender(); });
$('paletteInput').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); PAL.sel = Math.min(PAL.sel + 1, PAL.shown.length - 1); paletteRender(); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); PAL.sel = Math.max(PAL.sel - 1, 0); paletteRender(); }
  else if (e.key === 'Enter') { e.preventDefault(); paletteRun(PAL.sel); }
});
$('paletteList').addEventListener('click', e => { const li = e.target.closest('li[data-i]'); if (li) paletteRun(+li.dataset.i); });
$('paletteList').addEventListener('mousemove', e => { const li = e.target.closest('li[data-i]'); if (li && +li.dataset.i !== PAL.sel) { PAL.sel = +li.dataset.i; paletteRender(); } });
$('paletteBtn').addEventListener('click', openPalette);

// ---------------------------------------------------------------- keyboard shortcuts
// Shortcuts navigate only. Nothing here places an order or engages a control without a confirm().
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); openPalette(); return; }
  if (e.key === 'Escape') {
    if (closeOverlays()) return;
    if (!typing && CH.tk) { CH.tk = null; tkRender(); }
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || document.querySelector('.overlay:not([hidden])')) return;
  if (e.altKey) {
    const m = /^Digit([1-9])$/.exec(e.code);
    if (m && TABS[+m[1] - 1]) { e.preventDefault(); showTab(TABS[+m[1] - 1][0]); }
    return;
  }
  if (e.key === '?') { e.preventDefault(); openOverlay('help'); }
  else if (e.key === 'r' || e.key === 'R') { refresh(); }
  else if ((e.key === 'g' || e.key === 'G') && CH.active) { $('chGreeks').click(); }
});

tickClock();
setInterval(tickClock, 1000);
