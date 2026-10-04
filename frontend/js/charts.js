"use strict";
// Dependency-free SVG charts for the terminal (dark surface).
// Colour roles come from CSS custom properties (--series-1/2, --div-*, --grid, --axis);
// series pair and diverging poles were validated for CVD separation and contrast on the
// card surface. Labels are inserted with textContent only (names can be broker data).
// Every chart has a table twin elsewhere on its tab, so tooltips enhance and never gate.

const SVGNS = 'http://www.w3.org/2000/svg';
function svgEl(tag, attrs = {}, style = {}) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  Object.assign(el.style, style);
  return el;
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function niceTicks(lo, hi, count = 5) {
  if (!isFinite(lo) || !isFinite(hi)) return [0];
  if (lo === hi) { lo -= 1; hi += 1; }
  const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  const step = (f < 1.5 ? 1 : f < 3 ? 2 : f < 7 ? 5 : 10) * mag;
  const out = [];
  // Ticks cover the whole data range, so the top and bottom of every mark sit inside a labelled band.
  for (let v = Math.floor(lo / step + 1e-9) * step; v < hi + step * (1 - 1e-9); v += step) out.push(+v.toFixed(10));
  if (out[out.length - 1] < hi) out.push(+(out[out.length - 1] + step).toFixed(10));
  return out;
}
const compactNum = v => {
  if (v == null || !isFinite(v)) return '—';
  const a = Math.abs(v), s = v < 0 ? '-' : '';
  if (a >= 1e7) return s + (a / 1e7).toFixed(a >= 1e8 ? 0 : 1) + 'Cr';
  if (a >= 1e5) return s + (a / 1e5).toFixed(a >= 1e6 ? 0 : 1) + 'L';
  if (a >= 1e3) return s + (a / 1e3).toFixed(a >= 1e4 ? 0 : 1) + 'K';
  return s + (a >= 100 ? Math.round(a) : Math.round(a * 100) / 100);
};

function legendEl(series, kind) {
  const box = document.createElement('div');
  box.className = 'viz-legend';
  series.forEach(s => {
    const item = document.createElement('span');
    const key = document.createElement('i');
    key.className = kind === 'bar' ? 'key-rect' : 'key-line';
    key.style.background = s.color;
    item.append(key, document.createTextNode(s.name));
    box.append(item);
  });
  return box;
}

class BaseChart {
  constructor(container, opts) {
    this.c = container; this.opts = opts; this.hover = null;
    this.c.classList.add('viz');
    this.ro = new ResizeObserver(() => { if (this.c.clientWidth !== this._w) this.render(); });
    this.ro.observe(this.c);
  }
  update(opts) { this.opts = { ...this.opts, ...opts }; this.render(); }
  frame(empty) {
    const o = this.opts, W = Math.max(this.c.clientWidth, 240), H = o.height || 240;
    this._w = this.c.clientWidth;
    this.c.replaceChildren();
    if (o.series && o.series.length > 1) this.c.append(legendEl(o.series, this.kind));
    const wrap = document.createElement('div'); wrap.className = 'viz-plot';
    const svg = svgEl('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: 'img', tabindex: 0, 'aria-label': o.title || 'chart' });
    const tip = document.createElement('div'); tip.className = 'viz-tip'; tip.hidden = true; tip.setAttribute('role', 'status');
    wrap.append(svg, tip); this.c.append(wrap);
    if (empty) {
      const t = svgEl('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle', class: 'viz-empty' }); t.textContent = empty; svg.append(t);
      return null;
    }
    return { svg, tip, W, H, m: { l: 52, r: 14, t: (o.markers || []).length ? 24 : 14, b: 26 } };
  }
  yAxis(svg, f, ticks, y, fmtY) {
    ticks.forEach(v => {
      svg.append(svgEl('line', { x1: f.m.l, x2: f.W - f.m.r, y1: y(v), y2: y(v), class: v === 0 ? 'viz-base' : 'viz-grid' }));
      const t = svgEl('text', { x: f.m.l - 6, y: y(v) + 4, 'text-anchor': 'end', class: 'viz-tick' }); t.textContent = fmtY(v); svg.append(t);
    });
  }
  showTip(f, px, rows, head) {
    const tip = f.tip;
    tip.replaceChildren();
    const h = document.createElement('div'); h.className = 'viz-tip-head'; h.textContent = head; tip.append(h);
    rows.forEach(r => {
      const row = document.createElement('div'); row.className = 'viz-tip-row';
      const key = document.createElement('i'); key.className = 'key-line'; key.style.background = r.color;
      const val = document.createElement('b'); val.textContent = r.value;
      const name = document.createElement('span'); name.textContent = r.name;
      row.append(key, val, name); tip.append(row);
    });
    tip.hidden = false;
    const w = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(px + 12, 0), f.W - w - 4) + 'px';
    tip.style.top = f.m.t + 'px';
  }
  keyNav(f, n, move) {
    f.svg.addEventListener('keydown', e => {
      if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
      e.preventDefault();
      this.hover = Math.min(Math.max((this.hover ?? Math.floor(n / 2)) + (e.key === 'ArrowRight' ? 1 : -1), 0), n - 1);
      move(this.hover);
    });
    f.svg.addEventListener('focus', () => { if (this.hover == null) this.hover = Math.floor(n / 2); move(this.hover); });
    f.svg.addEventListener('blur', () => { this.hover = null; f.tip.hidden = true; this.clear && this.clear(); });
  }
}

// Line chart with crosshair. opts: {x, series:[{name, values, color}], markers:[{x,label}], xFmt, yFmt, zero}
class LineChart extends BaseChart {
  get kind() { return 'line'; }
  render() {
    const o = this.opts, xs = o.x || [];
    const vals = (o.series || []).flatMap(s => s.values.filter(v => v != null && isFinite(v)));
    if (!xs.length || !vals.length) { this.frame(o.emptyText || 'No data'); return; }
    const f = this.frame(), { svg, W, H, m } = f;
    let lo = Math.min(...vals), hi = Math.max(...vals);
    if (o.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
    const yt = niceTicks(lo, hi, 5); lo = Math.min(lo, yt[0]); hi = Math.max(hi, yt[yt.length - 1]);
    const x0 = xs[0], x1 = xs[xs.length - 1];
    const x = v => m.l + (v - x0) / (x1 - x0 || 1) * (W - m.l - m.r), y = v => m.t + (hi - v) / (hi - lo || 1) * (H - m.t - m.b);
    const fmtY = o.yFmt || compactNum, fmtX = o.xFmt || compactNum;
    this.yAxis(svg, f, yt, y, fmtY);
    niceTicks(x0, x1, Math.max(2, Math.floor((W - m.l) / 90))).filter(v => v >= x0 && v <= x1).forEach(v => {
      const t = svgEl('text', { x: x(v), y: H - 8, 'text-anchor': 'middle', class: 'viz-tick' }); t.textContent = fmtX(v); svg.append(t);
    });
    // Marker labels sit above the plot, centred on their line; a label that would overlap the
    // previous one is dropped (the value is in the stat tiles and the tooltip).
    let lastRight = -Infinity;
    (o.markers || []).filter(mk => mk.x >= x0 && mk.x <= x1).sort((a, b) => a.x - b.x).forEach(mk => {
      const px = x(mk.x);
      svg.append(svgEl('line', { x1: px, x2: px, y1: m.t, y2: H - m.b, class: 'viz-marker' }));
      const half = mk.label.length * 3.4 + 2;
      if (px - half < lastRight + 4) return;
      const t = svgEl('text', { x: px, y: m.t - 8, 'text-anchor': 'middle', class: 'viz-marker-label' }); t.textContent = mk.label; svg.append(t);
      lastRight = px + half;
    });
    o.series.forEach(s => {
      let d = '', pen = false;
      s.values.forEach((v, i) => {
        if (v == null || !isFinite(v)) { pen = false; return; }
        d += (pen ? 'L' : 'M') + x(xs[i]).toFixed(1) + ' ' + y(v).toFixed(1); pen = true;
      });
      svg.append(svgEl('path', { d, class: 'viz-line' }, { stroke: s.color }));
    });
    const cross = svgEl('line', { y1: m.t, y2: H - m.b, class: 'viz-cross', visibility: 'hidden' });
    const dots = o.series.map(s => svgEl('circle', { r: 4, class: 'viz-dot', visibility: 'hidden' }, { fill: s.color }));
    svg.append(cross, ...dots);
    const hit = svgEl('rect', { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: 'transparent' });
    svg.append(hit);
    const move = i => {
      const px = x(xs[i]);
      cross.setAttribute('x1', px); cross.setAttribute('x2', px); cross.setAttribute('visibility', 'visible');
      const rows = [];
      o.series.forEach((s, k) => {
        const v = s.values[i];
        if (v == null || !isFinite(v)) { dots[k].setAttribute('visibility', 'hidden'); return; }
        dots[k].setAttribute('cx', px); dots[k].setAttribute('cy', y(v)); dots[k].setAttribute('visibility', 'visible');
        rows.push({ name: s.name, value: fmtY(v), color: s.color });
      });
      this.showTip(f, px, rows, (o.xName ? o.xName + ' ' : '') + fmtX(xs[i]));
    };
    this.clear = () => { cross.setAttribute('visibility', 'hidden'); dots.forEach(d => d.setAttribute('visibility', 'hidden')); };
    const nearest = px => {
      let best = 0, bd = Infinity;
      xs.forEach((v, i) => { const dd = Math.abs(x(v) - px); if (dd < bd) { bd = dd; best = i; } });
      return best;
    };
    hit.addEventListener('pointermove', e => { const r = svg.getBoundingClientRect(); this.hover = nearest(e.clientX - r.left); move(this.hover); });
    hit.addEventListener('pointerleave', () => { this.hover = null; f.tip.hidden = true; this.clear(); });
    this.keyNav(f, xs.length, move);
    if (this.hover != null && this.hover < xs.length) move(this.hover);
  }
}

// Grouped columns. opts: {categories, series:[{name, values, color}], highlight, yFmt, catFmt}
function columnPath(x, w, y0, y1, r) {
  const up = y1 < y0, h = Math.abs(y1 - y0); r = Math.min(r, h, w / 2);
  if (h < 0.5) return '';
  if (up) return `M${x} ${y0}V${y1 + r}Q${x} ${y1} ${x + r} ${y1}H${x + w - r}Q${x + w} ${y1} ${x + w} ${y1 + r}V${y0}Z`;
  return `M${x} ${y0}V${y1 - r}Q${x} ${y1} ${x + r} ${y1}H${x + w - r}Q${x + w} ${y1} ${x + w} ${y1 - r}V${y0}Z`;
}
class BarChart extends BaseChart {
  get kind() { return 'bar'; }
  render() {
    const o = this.opts, cats = o.categories || [], n = cats.length, k = (o.series || []).length;
    const vals = (o.series || []).flatMap(s => s.values.filter(v => v != null && isFinite(v)));
    if (!n || !vals.length) { this.frame(o.emptyText || 'No data'); return; }
    const f = this.frame(), { svg, W, H, m } = f;
    let lo = Math.min(0, ...vals), hi = Math.max(0, ...vals);
    const yt = niceTicks(lo, hi, 4); lo = Math.min(lo, yt[0]); hi = Math.max(hi, yt[yt.length - 1]);
    const y = v => m.t + (hi - v) / (hi - lo || 1) * (H - m.t - m.b);
    const fmtY = o.yFmt || compactNum, fmtC = o.catFmt || (c => String(c));
    this.yAxis(svg, f, yt, y, fmtY);
    const band = (W - m.l - m.r) / n, gap = 2;
    const bw = Math.max(1, Math.min(24, (band * 0.8 - gap * (k - 1)) / k));
    const groupW = bw * k + gap * (k - 1);
    const bx = i => m.l + band * i + (band - groupW) / 2;
    if (o.highlight != null && o.highlight >= 0) {
      svg.append(svgEl('rect', { x: m.l + band * o.highlight, y: m.t, width: band, height: H - m.t - m.b, class: 'viz-band' }));
    }
    const every = Math.max(1, Math.ceil(46 / band));
    const hl = o.highlight != null && o.highlight >= 0 ? o.highlight : null;
    cats.forEach((c, i) => {
      // Regular labels every `every` bands; none may crowd the highlighted (ATM) label.
      if (i !== hl && (i % every || (hl != null && Math.abs(i - hl) < every))) return;
      const t = svgEl('text', { x: m.l + band * (i + 0.5), y: H - 8, 'text-anchor': 'middle', class: 'viz-tick' + (i === o.highlight ? ' strong' : '') });
      t.textContent = fmtC(c); svg.append(t);
    });
    const bars = [];
    o.series.forEach((s, j) => s.values.forEach((v, i) => {
      if (v == null || !isFinite(v) || v === 0) return;
      const p = svgEl('path', { d: columnPath(bx(i) + j * (bw + gap), bw, y(0), y(v), 4), class: 'viz-bar', 'data-i': i }, { fill: s.color });
      bars.push(p); svg.append(p);
    }));
    const hits = cats.map((c, i) => svgEl('rect', { x: m.l + band * i, y: m.t, width: band, height: H - m.t - m.b, fill: 'transparent' }));
    svg.append(...hits);
    const move = i => {
      bars.forEach(b => b.classList.toggle('dim', +b.dataset.i !== i));
      this.showTip(f, m.l + band * (i + 0.5), o.series.map(s => ({ name: s.name, value: fmtY(s.values[i]), color: s.color })), (o.catName ? o.catName + ' ' : '') + fmtC(cats[i]));
    };
    this.clear = () => bars.forEach(b => b.classList.remove('dim'));
    hits.forEach((h, i) => {
      h.addEventListener('pointermove', () => { if (this.hover !== i) { this.hover = i; move(i); } });
      h.addEventListener('pointerleave', () => { this.hover = null; f.tip.hidden = true; this.clear(); });
    });
    this.keyNav(f, n, move);
    if (this.hover != null && this.hover < n) move(this.hover);
  }
}

// Diverging heatmap rendered as an accessible table (it is its own table view).
function hexRgb(h) { h = h.replace('#', ''); return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)); }
function mix(a, b, t) { const A = hexRgb(a), B = hexRgb(b); return 'rgb(' + A.map((v, i) => Math.round(v + (B[i] - v) * t)).join(',') + ')'; }
function luminance(rgb) {
  const [r, g, b] = rgb.match(/\d+/g).map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}
function heatmapTable(container, { rows, cols, value, fmt, rowHead, colHead, caption }) {
  const pos = cssVar('--div-pos') || '#3987e5', neg = cssVar('--div-neg') || '#e66767', mid = cssVar('--div-mid') || '#383835';
  const all = rows.flatMap((r, i) => cols.map((c, j) => value(i, j))).filter(v => v != null && isFinite(v));
  const max = Math.max(1e-9, ...all.map(Math.abs));
  const t = document.createElement('table'); t.className = 'heat';
  const cap = document.createElement('caption'); cap.textContent = caption || ''; t.append(cap);
  const thead = document.createElement('thead'), hr = document.createElement('tr');
  const corner = document.createElement('th'); corner.scope = 'col'; corner.textContent = rowHead + ' / ' + colHead; hr.append(corner);
  cols.forEach(c => { const th = document.createElement('th'); th.scope = 'col'; th.textContent = c; hr.append(th); });
  thead.append(hr); t.append(thead);
  const tb = document.createElement('tbody');
  rows.forEach((r, i) => {
    const tr = document.createElement('tr'), th = document.createElement('th'); th.scope = 'row'; th.textContent = r; tr.append(th);
    cols.forEach((c, j) => {
      const v = value(i, j), td = document.createElement('td');
      if (v == null || !isFinite(v)) { td.textContent = '—'; }
      else {
        const bg = mix(mid, v >= 0 ? pos : neg, Math.min(1, Math.abs(v) / max));
        td.style.background = bg; td.style.color = luminance(bg) > 0.3 ? '#07101d' : '#ffffff';
        td.textContent = fmt(v); td.title = `${rowHead} ${r}, ${colHead} ${c}: ${fmt(v)}`;
      }
      tr.append(td);
    });
    tb.append(tr);
  });
  t.append(tb);
  const scale = document.createElement('div'); scale.className = 'heat-scale';
  const l = document.createElement('span'), bar = document.createElement('i'), rr = document.createElement('span');
  l.textContent = 'Loss ' + fmt(-max); rr.textContent = fmt(max) + ' Profit';
  bar.style.background = `linear-gradient(90deg, ${neg}, ${mid}, ${pos})`;
  scale.append(l, bar, rr);
  container.replaceChildren(t, scale);
}

window.Charts = { LineChart, BarChart, heatmapTable, compactNum };
