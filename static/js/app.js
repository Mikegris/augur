/* ================================================================
   AUGUR // WEALTH INTELLIGENCE SYSTEM
   Main Application — SPA Controller
   ================================================================ */

'use strict';

// ── Formatting helpers ────────────────────────────────────────────────────────
const fmt = {
  currency: (v, digits = 2) => {
    if (v == null) return '—';
    const n = parseFloat(v);
    if (isNaN(n)) return '—';
    return n.toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
  },
  price: (v) => {
    if (v == null) return '—';
    const n = parseFloat(v);
    if (isNaN(n)) return '—';
    if (n >= 1000) return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (n >= 1) return n.toFixed(2);
    return n.toFixed(4);
  },
  pct: (v, sign = true) => {
    if (v == null) return '—';
    const n = parseFloat(v);
    if (isNaN(n)) return '—';
    const s = (sign && n > 0) ? '+' : '';
    return `${s}${n.toFixed(2)}%`;
  },
  compact: (v) => {
    if (v == null) return '—';
    const n = parseFloat(v);
    if (isNaN(n)) return '—';
    if (Math.abs(n) >= 1e12) return (n / 1e12).toFixed(2) + 'T';
    if (Math.abs(n) >= 1e9)  return (n / 1e9).toFixed(2)  + 'B';
    if (Math.abs(n) >= 1e6)  return (n / 1e6).toFixed(2)  + 'M';
    if (Math.abs(n) >= 1e3)  return (n / 1e3).toFixed(1)  + 'K';
    return n.toFixed(0);
  },
  num: (v, digits = 2) => {
    if (v == null) return '—';
    const n = parseFloat(v);
    return isNaN(n) ? '—' : n.toFixed(digits);
  },
  date: (s) => {
    if (!s) return '—';
    try {
      const d = new Date(s);
      // `new Date()` silently returns "Invalid Date" rather than throwing for
      // bad strings like "—" or "None"; the original try/catch was dead code.
      if (isNaN(d.getTime())) return '—';
      return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
    } catch(e) { return '—'; }
  },
  timeAgo: (s) => {
    if (!s) return '';
    try {
      const t = new Date(s).getTime();
      if (isNaN(t)) return '';
      const diff = Date.now() - t;
      const m = Math.floor(diff / 60000);
      if (m < 1) return 'just now';
      if (m < 60) return `${m}m ago`;
      const h = Math.floor(m / 60);
      if (h < 24) return `${h}h ago`;
      return fmt.date(s);
    } catch(e) { return ''; }
  },
};

// ── HTML escape (for any string interpolated into innerHTML) ──────────────────
function _esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// ── JS-string escape (for strings interpolated INSIDE single-quoted JS string
// arguments in onclick="..." attributes — e.g. tickers like "Domino's" would
// otherwise produce a SyntaxError and the button silently no-ops).
function _jesc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, "\\'")
    .replace(/\n/g, '\\n')
    .replace(/\r/g, '\\r');
}

// ── URL scheme validation (block javascript:/data: hrefs from third-party data)
function safeUrl(u) {
  if (u == null) return '#';
  return /^https?:\/\//i.test(String(u)) ? String(u) : '#';
}

// ── Color helpers ─────────────────────────────────────────────────────────────
const col = {
  pnl: (v) => v > 0 ? 'col-positive' : v < 0 ? 'col-negative' : 'col-neutral',
  pct: (v) => v > 0 ? 'col-positive' : v < 0 ? 'col-negative' : 'col-neutral',
  badge: (v) => v > 0 ? 'badge-green' : v < 0 ? 'badge-red' : 'badge-dim',
  kpi: (v) => v > 0 ? 'green' : v < 0 ? 'red' : '',
};

// ── API helper ────────────────────────────────────────────────────────────────
// Surface the server's JSON {"error": ...} message on a non-2xx instead of a
// bare "HTTP 400" — routes return helpful messages (e.g. "Rate limited. Try
// after a while.") that were being discarded, leaving users with an opaque code.
async function _apiError(r) {
  try {
    const d = await r.json();
    if (d && d.error) return new Error(d.error);
  } catch (_) { /* non-JSON body */ }
  return new Error(`HTTP ${r.status}`);
}
// ── Resilient-API banner ──────────────────────────────────────────────────────
// ≥3 consecutive API.get failures inside a 30s window → one dismissible
// "Connection trouble" banner; the next successful call removes it. DOM is
// built with createElement + addEventListener (no inline handlers).
const ApiHealth = {
  _fails: [],          // timestamps of consecutive GET failures
  _banner: null,
  fail() {
    const now = Date.now();
    this._fails = this._fails.filter(t => now - t < 30000);
    this._fails.push(now);
    if (this._fails.length >= 3) this._show();
  },
  ok() {
    this._fails = [];  // "consecutive" — any success resets the streak
    this._hide();
  },
  _show() {
    if (this._banner || !document.body) return;
    const b = document.createElement('div');
    b.id = 'api-trouble-banner';
    b.setAttribute('role', 'alert');
    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:9500;'
      + 'display:flex;align-items:center;justify-content:center;gap:10px;'
      + 'padding:4px 12px;font-size:11px;letter-spacing:.05em;'
      + 'background:var(--red,#ff3366);color:#000';
    const span = document.createElement('span');
    span.textContent = 'Connection trouble — retrying…';
    const x = document.createElement('button');
    x.textContent = '×';
    x.setAttribute('aria-label', 'Dismiss');
    x.style.cssText = 'background:none;border:none;color:#000;cursor:pointer;font-size:14px;line-height:1;padding:0 4px';
    x.addEventListener('click', () => this._hide());
    b.appendChild(span);
    b.appendChild(x);
    document.body.appendChild(b);
    this._banner = b;
  },
  _hide() {
    if (this._banner) { this._banner.remove(); this._banner = null; }
  },
};

const API = {
  async get(path) {
    let r;
    try {
      r = await fetch(path);
    } catch (e) {
      ApiHealth.fail();   // network-level failure (server down, offline)
      throw e;
    }
    if (!r.ok) { ApiHealth.fail(); throw await _apiError(r); }
    ApiHealth.ok();
    return r.json();
  },
  async post(path, body) {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw await _apiError(r);
    ApiHealth.ok();
    return r.json();
  },
  async del(path) {
    const r = await fetch(path, { method: 'DELETE' });
    if (!r.ok) throw await _apiError(r);
    ApiHealth.ok();
    return r.json();
  },
  async put(path, body) {
    const r = await fetch(path, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw await _apiError(r);
    ApiHealth.ok();
    return r.json();
  },
};

// ── Toast notifications ───────────────────────────────────────────────────────
const Toast = {
  // Optional 4th arg: action {label, onClick} renders a small button inside
  // the toast (createElement + addEventListener — never an inline handler).
  show(msg, type = 'green', duration = 3000, action) {
    const container = document.getElementById('toast-container');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span>${_esc(msg)}</span>`;
    if (action && action.label && typeof action.onClick === 'function') {
      const btn = document.createElement('button');
      btn.textContent = action.label;
      btn.style.cssText = 'margin-left:8px;background:none;border:1px solid currentColor;'
        + 'color:inherit;font:inherit;font-size:10px;letter-spacing:.05em;'
        + 'padding:1px 6px;cursor:pointer;border-radius:2px';
      btn.addEventListener('click', () => {
        try { action.onClick(); } finally { el.remove(); }
      });
      el.appendChild(btn);
    }
    container.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transform = 'translateX(20px)'; el.style.transition = '0.2s'; setTimeout(() => el.remove(), 200); }, duration);
  },
  success: (m) => Toast.show(m, 'green'),
  error:   (m) => Toast.show(m, 'red', 5000),
  info:    (m) => Toast.show(m, 'blue'),
  warn:    (m) => Toast.show(m, 'amber'),
};

// ── Progress bar ──────────────────────────────────────────────────────────────
const Progress = {
  el: null,
  init() { this.el = document.getElementById('progress-bar'); },
  show(pct = 30) { if (this.el) { this.el.style.width = pct + '%'; this.el.style.opacity = '1'; } },
  done() { if (this.el) { this.el.style.width = '100%'; setTimeout(() => { this.el.style.opacity = '0'; this.el.style.width = '0'; }, 300); } },
  error() { if (this.el) { this.el.style.background = 'var(--red)'; this.done(); } },
};

// ── Macro context bar (Treasury/VIX strip across all views) ────────────────────
const MacroBar = {
  el: null,
  _timer: null,
  init() {
    if (document.getElementById('macro-bar')) return;
    // Insert as the FIRST child of #main so it doesn't disrupt the
    // #app CSS grid (which has fixed rows: 44/32/1fr/24px).
    const main = document.getElementById('main');
    if (!main) return;
    const bar = document.createElement('div');
    bar.id = 'macro-bar';
    bar.style.cssText = [
      'position:sticky','top:0','z-index:50',
      'display:flex','align-items:center','gap:14px',
      'padding:4px 14px','font-size:10px','letter-spacing:.05em',
      'background:var(--bg-secondary,#0a0e0a)',
      'border-bottom:1px solid var(--border,#1c1c1c)',
      'color:var(--text-secondary,#888)','flex-wrap:wrap',
    ].join(';');
    bar.innerHTML = '<span style="color:var(--text-dim)">MACRO //</span><span id="macro-bar-body" style="display:flex;gap:14px;flex-wrap:wrap">loading…</span>';
    main.insertBefore(bar, main.firstChild);
    this.el = bar;
    this.refresh();
    this._startTimer();
  },
  _startTimer() {
    if (this._timer) clearInterval(this._timer);
    // MacroBar refreshes at 5× the user's refresh_interval (so a 60s
    // refresh becomes 5min — matches the server-side cache window for
    // macro data), with a 300s floor for legacy behaviour.
    const sec = parseInt((State.settings && State.settings.refresh_interval) || 60, 10);
    const ms = Math.max(sec * 5 * 1000, 60000);
    // Skip while the tab is hidden — a backgrounded tab does zero network.
    this._timer = setInterval(() => { if (!document.hidden) this.refresh(); }, ms);
  },
  restart() { this._startTimer(); },
  _latestPerSecurity(rates) {
    const out = {};
    (rates || []).forEach(r => {
      const cur = out[r.security];
      if (!cur || (r.date || '') > (cur.date || '')) out[r.security] = r;
    });
    return out;
  },
  _vixRegime(v) {
    if (v == null) return ['NORMAL', 'var(--text-dim)'];
    if (v >= 25) return ['STRESS', 'var(--red,#ff3366)'];
    if (v <= 15) return ['CALM',   'var(--green,#00ff41)'];
    return ['NORMAL', 'var(--amber,#ffcc00)'];
  },
  async refresh() {
    const body = document.getElementById('macro-bar-body');
    if (!body) return;
    try {
      const [tc, vix] = await Promise.all([
        API.get('/api/macro/treasury-curve'),
        API.get('/api/macro/vix'),
      ]);
      const latest = this._latestPerSecurity(tc?.rates);
      const cell = (label, val, suffix) =>
        `<span><span style="color:var(--text-dim)">${label}:</span> <span style="color:var(--green)">${val ?? '—'}${suffix||''}</span></span>`;
      const fmtRate = r => r != null ? r.toFixed(2) : '—';
      const [regime, regimeColor] = this._vixRegime(vix?.vix_close);
      body.innerHTML = [
        cell('BILLS', fmtRate(latest['Treasury Bills']?.rate), '%'),
        cell('NOTES', fmtRate(latest['Treasury Notes']?.rate), '%'),
        cell('BONDS', fmtRate(latest['Treasury Bonds']?.rate), '%'),
        cell('VIX',   vix?.vix_close != null ? vix.vix_close.toFixed(2) : '—', ''),
        `<span><span style="color:var(--text-dim)">REGIME:</span> <span style="color:${regimeColor};font-weight:600">${regime}</span></span>`,
        `<span style="color:var(--text-muted);margin-left:auto;font-size:9px">${tc?.as_of || ''} · ${vix?.vix_date || ''}</span>`,
      ].join('');
    } catch(e) {
      body.innerHTML = '<span style="color:var(--text-muted)">macro feed offline</span>';
    }
  },
};

// ── Holdings tape (header strip of held symbols + day %) ─────────────────────
// Thin marquee under the header showing every held symbol with its day move.
// rAF-driven translateX (no CSS keyframes needed), paused on hidden tabs,
// static under prefers-reduced-motion. Click a symbol → openResearch().
// Inserted inside #main (after the macro bar) so the fixed #app grid rows
// stay untouched — same trick MacroBar uses.
const HoldingsTape = {
  el: null, inner: null,
  _raf: null, _x: 0, _timer: null,

  _reduced() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  },

  init() {
    if (document.getElementById('holdings-tape')) return;
    const main = document.getElementById('main');
    if (!main) return;
    const bar = document.createElement('div');
    bar.id = 'holdings-tape';
    bar.style.cssText = [
      'overflow:hidden', 'white-space:nowrap', 'position:relative',
      'padding:3px 0', 'font-size:10px', 'letter-spacing:.05em',
      'background:var(--bg-secondary,#0a0e0a)',
      'border-bottom:1px solid var(--border,#1c1c1c)',
      'display:none',  // shown once holdings arrive
    ].join(';');
    const inner = document.createElement('div');
    inner.id = 'holdings-tape-inner';
    inner.style.cssText = 'display:inline-block;white-space:nowrap;will-change:transform';
    bar.appendChild(inner);
    const macro = document.getElementById('macro-bar');
    if (macro && macro.parentNode === main) macro.insertAdjacentElement('afterend', bar);
    else main.insertBefore(bar, main.firstChild);
    this.el = bar;
    this.inner = inner;
    // Delegated click — symbols carry data-sym, no inline handlers.
    bar.addEventListener('click', (e) => {
      const t = e.target.closest('[data-sym]');
      if (t && typeof openResearch === 'function') openResearch(t.dataset.sym);
    });
    this.refresh();
    // Data refresh every 5 minutes, visible tabs only (forced — don't just
    // re-read the possibly-stale State cache).
    this._timer = setInterval(() => { if (!document.hidden) this.refresh(true); }, 5 * 60 * 1000);
    // Marquee pauses while hidden; resumes on return.
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) this._stop();
      else if (this.el && this.el.style.display !== 'none') this._start();
    });
  },

  async refresh(force) {
    let p = (!force && State.portfolio && State.portfolio.holdings) ? State.portfolio : null;
    if (!p) {
      try { p = await API.get('/api/portfolio'); State.portfolio = p; }
      catch (e) {
        p = State.portfolio;                       // fall back to the cache
        if (!p || !p.holdings) return;             // nothing to show yet
      }
    }
    const hs = (p.holdings || []).filter(h => h && h.symbol);
    if (!this.el || !this.inner) return;
    if (!hs.length) { this.el.style.display = 'none'; this._stop(); return; }
    const item = (h) => {
      const chg = h.day_change_pct;
      const cls = chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat';
      const arrow = chg > 0 ? '▲' : chg < 0 ? '▼' : '';
      return `<span class="ticker-item" data-sym="${_esc(h.symbol)}" style="cursor:pointer;margin:0 12px">`
        + `<span class="ticker-label">${_esc(h.symbol)}</span>`
        + `<span class="ticker-chg ${cls}">${arrow}${fmt.pct(chg)}</span>`
        + `</span>`;
    };
    const row = hs.map(item).join('');
    const reduced = this._reduced();
    // Duplicate the row for a seamless wrap; reduced motion gets one static row.
    this.inner.innerHTML = reduced ? row : row + row;
    this.el.style.display = '';
    if (reduced) {
      this._stop();
      this.inner.style.transform = 'none';
      this._x = 0;
    } else {
      this._start();
    }
  },

  _start() {
    if (this._raf || !this.inner || this._reduced()) return;
    const step = () => {
      this._raf = null;
      if (document.hidden || !this.inner || !document.body.contains(this.inner)) return;
      this._x -= 0.5;                          // ~30px/s at 60fps
      const half = this.inner.scrollWidth / 2;
      if (half > 0 && -this._x >= half) this._x += half;
      this.inner.style.transform = 'translateX(' + this._x + 'px)';
      this._raf = requestAnimationFrame(step);
    };
    this._raf = requestAnimationFrame(step);
  },

  _stop() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  },
};

// ── Keyboard Shortcut Help & global hotkeys ───────────────────────────────────
const ShortcutHelp = {
  el: null,
  _navMap: {
    'o': 'overview', 'p': 'portfolio', 'r': 'research', 'm': 'markets',
    'w': 'watchlist', 'a': 'alerts', 'n': 'news',
  },
  init() {
    this.el = document.getElementById('shortcut-help');
    if (!this.el) return;
    // ESC closes overlay if open; close on backdrop click
    this.el.addEventListener('click', (e) => { if (e.target === this.el) this.close(); });
    document.addEventListener('keydown', (e) => this._onKey(e));
  },
  open() {
    if (!this.el) return;
    this.el.classList.add('open');
    const closer = this.el.querySelector('button');
    if (closer) closer.focus();
  },
  close() { if (this.el) this.el.classList.remove('open'); },
  isOpen() { return !!(this.el && this.el.classList.contains('open')); },
  _isTyping(target) {
    if (!target) return false;
    const tag = (target.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || target.isContentEditable;
  },
  _onKey(e) {
    // ESC closes shortcut help, open modals
    if (e.key === 'Escape') {
      if (this.isOpen()) { this.close(); return; }
    }
    // Don't trigger global hotkeys while typing
    if (this._isTyping(e.target)) return;
    // ? opens help (handle both `?` and Shift+/)
    if (e.key === '?' || (e.key === '/' && e.shiftKey)) {
      e.preventDefault();
      this.isOpen() ? this.close() : this.open();
      return;
    }
    // / opens the Jarvis palette (jarvis.js owns the open — just don't
    // also grab the command bar); fall back to the command bar when the
    // Jarvis layer isn't loaded.
    if (e.key === '/' && !e.shiftKey && !e.metaKey && !e.ctrlKey) {
      if (window.Jarvis && typeof Jarvis.openPalette === 'function') {
        e.preventDefault();
        return;
      }
      const cmd = document.getElementById('cmd-input');
      if (cmd) { e.preventDefault(); cmd.focus(); cmd.select(); }
      return;
    }
    // Alt+<letter> jumps to a view
    if (e.altKey && !e.metaKey && !e.ctrlKey && !e.shiftKey) {
      const k = (e.key || '').toLowerCase();
      const view = this._navMap[k];
      if (view && typeof navigate === 'function') {
        e.preventDefault();
        navigate(view);
      }
    }
  },
};

// ── Modal ─────────────────────────────────────────────────────────────────────
const Modal = {
  open(id) {
    document.getElementById(id)?.classList.add('open');
  },
  close(id) {
    document.getElementById(id)?.classList.remove('open');
  },
  closeAll() {
    document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
  },
};

// ── State ─────────────────────────────────────────────────────────────────────
const State = {
  activeView: 'overview',
  portfolio: null,
  watchlist: [],
  indices: [],
  cryptoMarket: [],
  cryptoGlobal: {},
  researchSymbol: null,
  chartSymbol: null,
  chartPeriod: '6mo',
  chartInterval: '1d',
  chartRef: null,
  refreshInterval: null,
  lastRefresh: null,
  tickerData: [],
  settings: {},
  accounts: [],
};

// Expose the core helpers on `window`. They're declared with `const` (script
// scope), so they are NOT automatically properties of `window`. Several feature
// modules (research_montecarlo, research_probforecast, synth_consensus/peerdiv/
// divmap/whatif/sectorflow, research_factors) are IIFEs invoked as `(window)`
// and read `global.API` / `global.State` / `global.fmt` / `global.Toast` — which
// were silently undefined, crashing those panels (e.g. Monte Carlo:
// "undefined is not an object (global.API.get)"). Publishing them here fixes
// every such module at once. (Chart is already global via its UMD bundle.)
window.API = API;
window.State = State;
window.fmt = fmt;
window.Toast = Toast;

// ── Navigation ────────────────────────────────────────────────────────────────
// Five top-level groups, each fans out to its full feature set via a
// sub-tab strip. Every existing data-view ID continues to work — navigate()
// resolves the view to its group and lights up both levels.
const NAV_GROUPS = {
  dashboard: {
    label: 'DASHBOARD',
    items: [
      { view: 'overview',     label: 'Overview' },
      { view: 'jarvis',       label: '◉ Jarvis' },
      { view: 'trading',      label: '◉ Trading' },
      { view: 'portfolio',    label: 'Portfolio' },
      { view: 'watchlist',    label: 'Watchlist' },
      { view: 'transactions', label: 'Transactions' },
      { view: 'dividends',    label: 'Dividends' },
      { view: 'alerts',       label: 'Alerts' },
      { view: 'scanner',      label: 'Scanner' },
    ],
  },
  markets: {
    label: 'MARKETS',
    items: [
      { view: 'markets',     label: 'Markets' },
      { view: 'sectorflow',  label: 'Sector Flow' },
      { view: 'crypto',      label: 'Crypto' },
      { view: 'macro',       label: 'Macro' },
      { view: 'stress',      label: 'Stress Test' },
      { view: 'liquidity',   label: 'Liquidity' },
      { view: 'news',        label: 'News' },
    ],
  },
  research: {
    label: 'RESEARCH',
    items: [
      { view: 'research',     label: 'Research' },
      { view: 'forecast',     label: 'Forecast' },
      { view: 'analytics',    label: 'Analytics' },
      { view: 'backtest',     label: 'Backtest' },
      { view: 'optimizer',    label: 'Optimizer' },
      { view: 'montecarlo',   label: 'Monte Carlo' },
      { view: 'whatif',       label: 'What-If' },
      { view: 'catalysts',    label: 'Catalysts' },
      { view: 'research-lab', label: 'Research Lab' },
      { view: 'intel',        label: 'Intel' },
      { view: 'earnings',     label: 'Earnings' },
      { view: 'screener',     label: 'Screener' },
      { view: 'narrative',    label: 'Narrative' },
      { view: 'alt-data',     label: 'Alt Data' },
    ],
  },
  alpha: {
    label: 'ALPHA',
    items: [
      { view: 'signals',           label: 'Signals' },
      { view: 'cluster',           label: 'Cluster' },
      { view: 'divergences',       label: 'Divergences' },
      { view: 'ideas',             label: 'Ideas' },
      { view: 'options-flow',      label: 'Options Flow' },
      { view: 'gex',               label: 'GEX' },
      { view: 'contagion',         label: 'Contagion' },
      { view: 'reflexivity',       label: 'Reflexivity' },
      { view: 'synthetic-insider', label: 'Synth Insider' },
      { view: 'congress',          label: 'Congress' },
    ],
  },
  console: {
    label: 'CONSOLE',
    items: [
      { view: 'terminal', label: 'Terminal' },
      { view: 'settings', label: 'Settings' },
    ],
  },
};

function findGroupForView(view) {
  for (const [gid, g] of Object.entries(NAV_GROUPS)) {
    if (g.items.some(it => it.view === view)) return gid;
  }
  return null;
}

function renderSubNav(group, activeView) {
  const subNav = document.getElementById('sub-nav');
  if (!subNav) return;
  const items = NAV_GROUPS[group].items;
  subNav.innerHTML = items.map(it => {
    const isActive = it.view === activeView;
    return `<button class="sub-nav-item${isActive ? ' active' : ''}" `
         + `data-view="${it.view}" role="tab" aria-selected="${isActive}">`
         + `${it.label}</button>`;
  }).join('');
  subNav.querySelectorAll('.sub-nav-item').forEach(el => {
    el.addEventListener('click', () => navigate(el.dataset.view));
  });
}

function navigate(view) {
  const group = findGroupForView(view);
  if (!group) {
    console.warn('navigate: unknown view', view);
    return;
  }

  // Top-level sidebar groups
  document.querySelectorAll('.nav-item[data-group]').forEach(el => {
    el.classList.toggle('active', el.dataset.group === group);
  });

  // Sub-tab strip for the active group
  renderSubNav(group, view);

  // Show the matching view div, hide all others
  document.querySelectorAll('.view').forEach(el => {
    el.classList.toggle('active', el.id === `view-${view}`);
  });

  State.activeView = view;
  State.activeGroup = group;

  // Jarvis context strip — one line about wherever you just landed
  if (window.Jarvis && Jarvis.onNavigate) Jarvis.onNavigate(view);

  // Lazy-load views (unchanged)
  switch (view) {
    case 'overview':     loadOverview(); break;
    case 'jarvis':       if (window.Jarvis) Jarvis.loadView(); break;
    case 'trading':      loadTradingView(); break;
    case 'portfolio':    loadPortfolio(); break;
    case 'markets':      loadMarkets(); break;
    case 'crypto':       loadCrypto(); break;
    case 'watchlist':    loadWatchlistView(); break;
    case 'transactions': loadTransactions(); break;
    case 'news':         loadGlobalNews(); break;
    case 'screener':     loadScreener(); break;
    case 'settings':     loadSettings(); break;
    case 'research':     loadResearchDefault(); break;
    case 'forecast':     loadForecastView(); break;
    case 'analytics':    loadAnalyticsView(); break;
    case 'intel':        loadIntelView(); break;
    case 'earnings':     loadEarningsView(); break;
    case 'dividends':     loadDividendsView(); break;
    case 'macro':         loadMacroView(); break;
    case 'stress':        loadStressTestView(); break;
    case 'alerts':        loadAlertsView(); break;
    case 'signals':       loadSignalsView(); break;
    case 'options-flow':  loadOptionsFlowView(); break;
    case 'congress':      loadCongressView(); break;
    case 'terminal':      loadTerminalView(); break;
    case 'scanner':       loadScannerView(); break;
    case 'gex':              loadGexView(); break;
    case 'contagion':        loadContagionView(); break;
    case 'narrative':        loadNarrativeView(); break;
    case 'synthetic-insider': loadSyntheticInsiderView(); break;
    case 'reflexivity':      loadReflexivityView(); break;
    case 'liquidity':        loadLiquidityView(); break;
    case 'alt-data':         loadAltDataView(); break;
    case 'ideas':            loadIdeasView(); break;
    case 'backtest':         loadBacktest(); break;
    case 'optimizer':        loadOptimizer(); break;
    case 'montecarlo':       loadMonteCarloView(); break;
    case 'research-lab':     loadHypothesisLab(); break;
    // ── v0.3.0 synthesis views ──────────────────────────────────
    case 'catalysts':        loadCatalystsView(); break;
    case 'cluster':          loadClusterView(); break;
    case 'divergences':      loadDivergencesView(); break;
    case 'sectorflow':       loadSectorFlowView(); break;
    case 'whatif':           loadWhatIfView(); break;
  }
}

// ── Clock & Market Status ─────────────────────────────────────────────────────
function startClock() {
  // Cache the DOM refs once — this ticks every second, and the market
  // status only flips a handful of times a day, so skip the className /
  // textContent writes (each one invalidates style) unless it changed.
  const clockEl = document.getElementById('clock');
  const dot = document.querySelector('.status-dot');
  const label = document.querySelector('.status-label');
  let lastStatus = null;
  function tick() {
    // Hidden tab: skip the Intl formatting + DOM writes entirely. The
    // next visible tick (≤1s after refocus) repaints the clock.
    if (document.hidden) return;
    const now = new Date();
    if (clockEl) clockEl.textContent =
      now.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });

    // Market status (simplified EST)
    const est = new Date(now.toLocaleString('en-US', { timeZone: 'America/New_York' }));
    const h = est.getHours(), m = est.getMinutes(), day = est.getDay();
    const mins = h * 60 + m;
    if (dot && label) {
      let cls, text;
      if (day === 0 || day === 6) {
        cls = 'status-dot closed'; text = 'CLOSED';
      } else if (mins >= 570 && mins < 960) { // 9:30 - 16:00
        cls = 'status-dot open'; text = 'MARKET OPEN';
      } else if (mins >= 240 && mins < 570) { // 4:00 - 9:30
        cls = 'status-dot pre'; text = 'PRE-MARKET';
      } else if (mins >= 960 && mins < 1200) { // 16:00 - 20:00
        cls = 'status-dot pre'; text = 'AFTER-HRS';
      } else {
        cls = 'status-dot closed'; text = 'CLOSED';
      }
      if (text !== lastStatus) {
        dot.className = cls; label.textContent = text;
        lastStatus = text;
      }
    }
  }
  tick();
  setInterval(tick, 1000);
}

// ── Ticker ────────────────────────────────────────────────────────────────────
async function loadTicker() {
  try {
    const indices = await API.get('/api/market/indices');
    State.indices = indices;
    renderTicker(indices);
  } catch(e) {
    console.warn('Ticker load failed', e);
  }
}

function renderTicker(items) {
  const el = document.getElementById('header-ticker');
  if (!el) return;
  // API can return null/undefined or a {error:...} envelope when the
  // indices endpoint is rate-limited; spreading non-iterable throws.
  if (!Array.isArray(items) || !items.length) return;
  const html = [...items, ...items].map(item => {
    const chg = item.change_pct;
    const cls = chg == null ? 'flat' : chg > 0 ? 'up' : chg < 0 ? 'down' : 'flat';
    const arrow = chg > 0 ? '▲' : chg < 0 ? '▼' : '';
    return `<span class="ticker-item">
      <span class="ticker-label">${_esc(item.label || item.symbol)}</span>
      <span class="ticker-price">${fmt.price(item.price)}</span>
      <span class="ticker-chg ${cls}">${arrow}${fmt.pct(chg)}</span>
    </span>`;
  }).join('');
  el.innerHTML = html;
}

// ── Sidebar Quick Watchlist ───────────────────────────────────────────────────
async function loadSidebarWatchlist() {
  try {
    const items = await API.get('/api/watchlist');
    State.watchlist = items;
    const el = document.getElementById('sidebar-watchlist');
    if (!el) return;
    if (!items.length) {
      el.innerHTML = `<div class="empty-state" style="padding:12px;font-size:10px;"><span class="text-muted">No watchlist items</span></div>`;
      return;
    }
    el.innerHTML = items.map(i => `
      <div class="swl-item" onclick="openResearch('${_jesc(i.symbol)}')">
        <div>
          <div class="swl-sym">${_esc(i.symbol)}</div>
          <div class="swl-price">${fmt.price(i.price)}</div>
        </div>
        <div class="swl-chg ${i.change_pct == null ? 'flat' : i.change_pct > 0 ? 'up' : i.change_pct < 0 ? 'down' : 'flat'}">${fmt.pct(i.change_pct)}</div>
      </div>
    `).join('');
  } catch(e) {}
}

// ── Overview / Dashboard ──────────────────────────────────────────────────────
async function loadOverview() {
  const view = document.getElementById('view-overview');

  // Render skeleton immediately so the page isn't stuck on "INITIALIZING..."
  view.innerHTML = `
    <div id="ov-jarvis"></div>
    <div class="kpi-grid" style="grid-template-columns:repeat(auto-fill,minmax(160px,1fr))">
      <div class="kpi-card" id="ov-kpi-value"><div class="kpi-label">PORTFOLIO VALUE</div><div class="kpi-value text-dim">—</div></div>
      <div class="kpi-card" id="ov-kpi-pnl"><div class="kpi-label">UNREALIZED P&L</div><div class="kpi-value text-dim">—</div></div>
      <div class="kpi-card blue" id="ov-kpi-pos"><div class="kpi-label">POSITIONS</div><div class="kpi-value blue">—</div></div>
      <div class="kpi-card" id="ov-kpi-crypto"><div class="kpi-label">CRYPTO MKT CAP</div><div class="kpi-value text-dim">—</div></div>
      <div class="kpi-card" id="ov-kpi-sp"><div class="kpi-label">S&P 500</div><div class="kpi-value text-dim">—</div></div>
      <div class="kpi-card" id="ov-kpi-ndx"><div class="kpi-label">NASDAQ 100</div><div class="kpi-value text-dim">—</div></div>
      <div class="kpi-card amber" id="ov-kpi-vix"><div class="kpi-label">VIX (FEAR INDEX)</div><div class="kpi-value text-dim">—</div></div>
      <div class="kpi-card" id="ov-kpi-gold"><div class="kpi-label">GOLD</div><div class="kpi-value text-dim">—</div></div>
    </div>
    <div class="panel-grid grid-2" style="margin-top:8px">
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">GLOBAL INDICES</span>
          <button class="btn btn-ghost btn-sm" onclick="loadOverview()">↻ REFRESH</button>
        </div>
        <div class="panel-body" id="ov-indices"><div class="loading"><div class="spinner"></div> Loading indices...</div></div>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="panel-title">SECTOR PERFORMANCE</span></div>
        <div class="panel-body" id="ov-sectors"><div class="loading"><div class="spinner"></div> Loading sectors...</div></div>
      </div>
    </div>
    <div class="panel-grid grid-2" style="margin-top:8px">
      <div class="panel">
        <div class="panel-header"><span class="panel-title">▲ TOP GAINERS</span></div>
        <div class="panel-body" id="ov-gainers" style="padding:0"><div class="loading"><div class="spinner"></div></div></div>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="panel-title">▼ TOP LOSERS</span></div>
        <div class="panel-body" id="ov-losers" style="padding:0"><div class="loading"><div class="spinner"></div></div></div>
      </div>
    </div>
    <div id="ov-calendar-section" style="margin-top:8px"></div>
    <div id="ov-portfolio-section"></div>
    <div id="ov-allocation-section"></div>
  `;
  Progress.show(20);
  // Jarvis briefing — fires in parallel with everything below
  if (window.Jarvis) Jarvis.renderBriefing('ov-jarvis');
  // Upcoming events overlay (earnings/IPOs/dividends/splits)
  DataPanels.appendCalendarOverlay(document.getElementById('ov-calendar-section'));

  // Fire all requests in parallel, render each section as it arrives
  const indicesP = API.get('/api/market/indices').then(indices => {
    State.indices = indices;
    renderTicker(indices);
    const sp = indices.find(i => i.symbol === '^GSPC');
    const ndx = indices.find(i => i.symbol === '^NDX');
    const vix = indices.find(i => i.symbol === '^VIX');
    const gold = indices.find(i => i.symbol === 'GC=F');
    const el = id => document.getElementById(id);
    if (el('ov-kpi-sp'))   el('ov-kpi-sp').innerHTML   = `<div class="kpi-label">S&P 500</div>${renderIndexKpi(sp)}`;
    if (el('ov-kpi-ndx'))  el('ov-kpi-ndx').innerHTML  = `<div class="kpi-label">NASDAQ 100</div>${renderIndexKpi(ndx)}`;
    if (el('ov-kpi-vix'))  el('ov-kpi-vix').innerHTML  = `<div class="kpi-label">VIX (FEAR INDEX)</div>${renderIndexKpi(vix, true)}`;
    if (el('ov-kpi-gold')) el('ov-kpi-gold').innerHTML = `<div class="kpi-label">GOLD</div>${renderIndexKpi(gold)}`;
    ['ov-kpi-sp', 'ov-kpi-ndx', 'ov-kpi-vix', 'ov-kpi-gold'].forEach(kpiCountUp);
    const tbody = indices.map(idx => `<tr>
      <td><span class="col-symbol" onclick="openResearch('${_jesc(idx.symbol)}')">${_esc(idx.label || idx.symbol)}</span></td>
      <td class="col-price">${fmt.price(idx.price)}</td>
      <td class="${col.pnl(idx.change)}">${fmt.price(idx.change)}</td>
      <td class="${col.pct(idx.change_pct)}">${fmt.pct(idx.change_pct)}</td>
    </tr>`).join('');
    if (el('ov-indices')) el('ov-indices').innerHTML = `<table class="data-table w-full" style="padding:0"><thead><tr><th>INDEX</th><th>PRICE</th><th style="text-align:right">CHANGE</th><th style="text-align:right">%</th></tr></thead><tbody>${tbody}</tbody></table>`;
    Progress.show(40);
  }).catch(() => {});

  const cryptoP = API.get('/api/crypto/global').then(cryptoGlobal => {
    State.cryptoGlobal = cryptoGlobal;
    const el = document.getElementById('ov-kpi-crypto');
    if (el) el.innerHTML = `<div class="kpi-label">CRYPTO MKT CAP</div><div class="kpi-value">$${fmt.compact(cryptoGlobal.total_market_cap_usd)}</div><div class="kpi-sub">BTC DOM: ${fmt.num(cryptoGlobal.btc_dominance)}%</div>`;
    kpiCountUp('ov-kpi-crypto');
  }).catch(() => {});

  const sectorsP = API.get('/api/market/sectors').then(sectors => {
    const el = document.getElementById('ov-sectors');
    if (el) el.innerHTML = renderSectors(sectors);
  }).catch(() => {});

  const moversP = API.get('/api/market/movers').then(movers => {
    const el = id => document.getElementById(id);
    const moverRows = (list, cls) => (list || []).map(g => `<tr>
      <td><span class="col-symbol" onclick="openResearch('${_jesc(g.symbol)}')">${_esc(g.symbol)}</span></td>
      <td class="col-price">${fmt.price(g.price)}</td>
      <td class="${cls}">${fmt.pct(g.change_pct)}</td>
      <td class="col-number">${fmt.compact(g.market_cap)}</td>
    </tr>`).join('');
    if (el('ov-gainers')) el('ov-gainers').innerHTML = `<table class="data-table"><thead><tr><th>SYMBOL</th><th>PRICE</th><th style="text-align:right">%</th><th>MKT CAP</th></tr></thead><tbody>${moverRows(movers.gainers, 'col-positive')}</tbody></table>`;
    if (el('ov-losers'))  el('ov-losers').innerHTML  = `<table class="data-table"><thead><tr><th>SYMBOL</th><th>PRICE</th><th style="text-align:right">%</th><th>MKT CAP</th></tr></thead><tbody>${moverRows(movers.losers, 'col-negative')}</tbody></table>`;
    Progress.show(70);
  }).catch(() => {});

  const portfolioP = API.get('/api/portfolio').then(portfolio => {
    State.portfolio = portfolio;
    const summary = portfolio.summary || {};
    const pnlClass = col.kpi(summary.total_pnl);
    const el = id => document.getElementById(id);
    if (el('ov-kpi-value')) el('ov-kpi-value').innerHTML = `<div class="kpi-label">PORTFOLIO VALUE</div><div class="kpi-value">$${fmt.compact(summary.total_value)}</div><div class="kpi-sub">$${fmt.currency(summary.total_value)}</div>`;
    if (el('ov-kpi-value')) el('ov-kpi-value').className = `kpi-card ${pnlClass}`;
    if (el('ov-kpi-pnl'))   el('ov-kpi-pnl').innerHTML  = `<div class="kpi-label">UNREALIZED P&L</div><div class="kpi-value ${col.kpi(summary.total_pnl)}">${summary.total_pnl >= 0 ? '+$' : '-$'}${fmt.compact(Math.abs(summary.total_pnl))}</div><div class="kpi-sub">${fmt.pct(summary.total_pnl_pct)}</div>`;
    if (el('ov-kpi-pnl'))   el('ov-kpi-pnl').className  = `kpi-card ${col.kpi(summary.total_pnl)}`;
    if (el('ov-kpi-pos'))   el('ov-kpi-pos').innerHTML   = `<div class="kpi-label">POSITIONS</div><div class="kpi-value blue">${summary.num_positions || 0}</div><div class="kpi-sub">TOTAL HOLDINGS</div>`;
    ['ov-kpi-value', 'ov-kpi-pnl', 'ov-kpi-pos'].forEach(kpiCountUp);
    const sect = el('ov-portfolio-section');
    if (sect && portfolio.holdings?.length) {
      sect.innerHTML = `<div class="panel" style="margin-top:8px">
        <div class="panel-header"><span class="panel-title">PORTFOLIO SNAPSHOT</span><button class="btn btn-green btn-sm" onclick="navigate('portfolio')">FULL VIEW →</button></div>
        <div class="panel-body" style="padding:0"><table class="data-table"><thead><tr>
          <th>SYMBOL</th><th>NAME</th><th>SHARES</th><th>AVG COST</th>
          <th style="text-align:right">CUR PRICE</th><th style="text-align:right">MKT VALUE</th>
          <th style="text-align:right">P&L</th><th style="text-align:right">P&L %</th>
        </tr></thead><tbody>${portfolio.holdings.map(h => `<tr>
          <td><span class="col-symbol" onclick="openResearch('${_jesc(h.symbol)}')">${_esc(h.symbol)}</span></td>
          <td class="col-name">${_esc(h.name || '—')}</td>
          <td class="col-number">${fmt.num(h.shares, 4)}</td>
          <td class="col-number">$${fmt.price(h.avg_cost)}</td>
          <td class="col-price">${fmt.price(h.current_price)}</td>
          <td class="col-number">$${fmt.currency(h.market_value)}</td>
          <td class="${col.pnl(h.unrealized_pnl)}">$${fmt.currency(h.unrealized_pnl)}</td>
          <td class="${col.pct(h.unrealized_pct)}">${fmt.pct(h.unrealized_pct)}</td>
        </tr>`).join('')}</tbody></table></div></div>`;
    } else if (sect) {
      sect.innerHTML = `<div class="panel" style="margin-top:8px"><div class="panel-body"><div class="empty-state"><span class="empty-icon">◈</span><span>No portfolio positions. <a href="#" class="text-green" onclick="navigate('portfolio')">Add your first holding →</a></span></div></div></div>`;
    }
  }).catch(() => {});

  const allocP = API.get('/api/analytics/portfolio').then(a => {
    const sect = document.getElementById('ov-allocation-section');
    if (!sect) return;
    if (!a || !a.allocation_by_sector) { sect.innerHTML = ''; return; }
    sect.innerHTML = renderAllocationPanel(a);
  }).catch(() => {});

  // Wait for all to finish, then mark done
  try {
    await Promise.all([indicesP, cryptoP, sectorsP, moversP, portfolioP, allocP]);
  } catch(e) {}
  Progress.done();
  State.lastRefresh = new Date();
  updateStatusBar();
}

function renderAllocationPanel(a) {
  const total = a.total_value || 0;
  const sectors = Object.entries(a.allocation_by_sector_values || {}).sort((x, y) => y[1] - x[1]);
  const types = Object.entries(a.allocation_by_type_values || {}).sort((x, y) => y[1] - x[1]);
  if (!sectors.length) return '';
  const palette = ['#0a4d2a', '#0e6b3a', '#125e8a', '#7a4d0a', '#6a1f8a', '#8a1f3a', '#3a3a3a', '#0a4d8a', '#0a8a4d', '#8a4d0a'];
  const sectorRow = (name, val, idx) => {
    const pct = total ? (val / total * 100) : 0;
    return `<div class="sector-row" style="display:flex;align-items:center;gap:8px;font-size:11px;padding:4px 0">
      <span style="width:140px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(name)}</span>
      <div style="flex:1;background:var(--bg-secondary);border-radius:2px;height:14px;position:relative">
        <div style="width:${pct.toFixed(1)}%;height:100%;background:${palette[idx % palette.length]};border-radius:2px"></div>
      </div>
      <span style="width:60px;text-align:right;color:var(--green)">${pct.toFixed(1)}%</span>
      <span style="width:90px;text-align:right;color:var(--text-dim)">$${fmt.compact(val)}</span>
    </div>`;
  };
  return `<div class="panel-grid grid-2" style="margin-top:8px">
    <div class="panel">
      <div class="panel-header"><span class="panel-title">SECTOR ALLOCATION</span></div>
      <div class="panel-body">${sectors.map((s, i) => sectorRow(s[0], s[1], i)).join('')}</div>
    </div>
    <div class="panel">
      <div class="panel-header"><span class="panel-title">ASSET TYPE</span></div>
      <div class="panel-body">${types.map((s, i) => sectorRow(s[0].toUpperCase(), s[1], i)).join('')}</div>
    </div>
  </div>`;
}

// ── KPI count-up ──────────────────────────────────────────────────────────────
// Animate the first numeric token inside a KPI card's .kpi-value from 0 to its
// final value over ~500ms (rAF). The final frame restores the exact original
// string, so the at-rest format is byte-identical to the non-animated render.
// Skipped entirely under prefers-reduced-motion.
function kpiCountUp(cardId) {
  const card = document.getElementById(cardId);
  if (!card) return;
  if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  const el = card.querySelector('.kpi-value');
  if (!el) return;
  const finalText = el.textContent;
  const m = finalText.match(/-?\d[\d,]*\.?\d*/);
  if (!m) return;                                  // "—" placeholders etc.
  const target = parseFloat(m[0].replace(/,/g, ''));
  if (!isFinite(target) || target === 0) return;
  const decimals = (m[0].split('.')[1] || '').length;
  const grouped = m[0].includes(',');
  const renderNum = (v) => grouped
    ? v.toLocaleString('en-US', { minimumFractionDigits: decimals, maximumFractionDigits: decimals })
    : v.toFixed(decimals);
  const t0 = performance.now();
  const DUR = 500;
  const step = (now) => {
    // The card re-renders on refresh — if our value node is gone, stop.
    if (!document.body.contains(el)) return;
    const k = Math.min(1, (now - t0) / DUR);
    if (k >= 1) { el.textContent = finalText; return; }
    const eased = 1 - Math.pow(1 - k, 3);
    el.textContent = finalText.replace(m[0], renderNum(target * eased));
    requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

function renderIndexKpi(idx, inverted = false) {
  if (!idx) return '<div class="kpi-value text-dim">—</div>';
  const c = inverted ? col.kpi(-idx.change_pct) : col.kpi(idx.change_pct);
  return `<div class="kpi-value ${c}">${fmt.price(idx.price)}</div>
    <div class="kpi-sub">${fmt.pct(idx.change_pct)}</div>`;
}

function renderSectors(sectors) {
  if (!sectors?.length) return '<div class="loading">No sector data</div>';
  const max = Math.max(...sectors.map(s => Math.abs(s.change_pct || 0)));
  return sectors.map(s => {
    const pct = s.change_pct || 0;
    const barW = Math.min(50, Math.abs(pct) / (max || 1) * 50);
    const cls = pct >= 0 ? 'col-positive' : 'col-negative';
    return `<div class="sector-row">
      <span class="sector-name">${_esc(s.sector)}</span>
      <div class="sector-bar-wrap">
        <div class="sector-divider"></div>
        <div class="sector-bar ${pct >= 0 ? 'pos' : 'neg'}" style="width:${barW}%"></div>
      </div>
      <span class="sector-pct ${cls}">${fmt.pct(pct)}</span>
    </div>`;
  }).join('');
}

// ── Portfolio View ────────────────────────────────────────────────────────────
var _portfolioAccountFilter = '';
var _portfolioGroupByAccount = false;

function _portfolioTableHtml(items, summary, acctOptsHtml, showAccount) {
  var h = '<table class="data-table" style="min-width:900px"><thead><tr>'
    + '<th>SYMBOL</th><th>NAME</th><th>TYPE</th>';
  if (showAccount) h += '<th>ACCOUNT</th>';
  h += '<th style="text-align:right">SHARES</th>'
    + '<th style="text-align:right">AVG COST</th>'
    + '<th style="text-align:right">CUR PRICE</th>'
    + '<th style="text-align:right">MKT VALUE</th>'
    + '<th style="text-align:right">UNRLZ P&L</th>'
    + '<th style="text-align:right">P&L %</th>'
    + '<th style="text-align:right">DAY CHG%</th>'
    + '<th style="text-align:right">WEIGHT%</th>'
    + '<th></th></tr></thead><tbody>';
  items.forEach(function(p) {
    var weight = summary.total_value ? ((p.market_value / summary.total_value) * 100) : 0;
    var acctColor = p.account_color || '';
    var acctDot = acctColor ? '<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:' + _esc(acctColor) + ';margin-right:4px"></span>' : '';
    var acctLabel = p.account_name || '<span class="text-dim">--</span>';
    h += '<tr>'
      + '<td><span class="col-symbol" onclick="openResearch(\'' + _jesc(p.symbol) + '\')">' + _esc(p.symbol) + '</span></td>'
      + '<td class="col-name truncate" style="max-width:120px" title="' + _esc(p.name || '') + '">' + _esc(p.name || '\u2014') + '</td>'
      + '<td><span class="badge badge-dim">' + _esc(p.asset_type) + '</span></td>';
    if (showAccount) {
      h += '<td style="font-size:10px">'
        + '<select class="form-select" style="font-size:9px;padding:1px 4px;height:20px;width:auto;min-width:80px;background:transparent;border-color:var(--border)" '
        + 'onchange="changePositionAccount(' + p.id + ',this.value)">';
      h += '<option value=""' + (!p.account_id ? ' selected' : '') + '>--</option>';
      (State.accounts || []).forEach(function(a) {
        h += '<option value="' + a.id + '"' + (p.account_id == a.id ? ' selected' : '') + '>' + _esc(a.name) + '</option>';
      });
      h += '</select></td>';
    }
    h += '<td class="col-number">' + fmt.num(p.shares, 4) + '</td>'
      + '<td class="col-number">$' + fmt.price(p.avg_cost) + '</td>'
      + '<td class="col-price">' + (p.current_price ? '$' + fmt.price(p.current_price) : '<span class="text-dim">\u2014</span>') + '</td>'
      + '<td class="col-number">' + (p.market_value ? '$' + fmt.currency(p.market_value) : '\u2014') + '</td>'
      + '<td class="' + col.pnl(p.unrealized_pnl) + '">' + (p.unrealized_pnl != null ? (p.unrealized_pnl >= 0 ? '+$' : '-$') + fmt.currency(Math.abs(p.unrealized_pnl)) : '\u2014') + '</td>'
      + '<td class="' + col.pct(p.unrealized_pct) + '">' + fmt.pct(p.unrealized_pct) + '</td>'
      + '<td class="' + col.pct(p.day_change_pct) + '">' + fmt.pct(p.day_change_pct) + '</td>'
      + '<td class="col-number">' + weight.toFixed(1) + '%</td>'
      + '<td class="col-actions">'
      + '<button class="btn btn-ghost btn-sm" onclick="openResearch(\'' + _jesc(p.symbol) + '\')">CHART</button>'
      + '<button class="btn btn-red btn-sm" onclick="deletePosition(' + p.id + ', \'' + _jesc(p.symbol) + '\')">\u2715</button>'
      + '</td></tr>';
  });
  h += '</tbody></table>';
  return h;
}

async function loadPortfolio() {
  const view = document.getElementById('view-portfolio');
  view.innerHTML = '<div class="loading"><div class="spinner"></div> FETCHING PORTFOLIO...</div>';

  try {
    var url = '/api/portfolio';
    if (_portfolioAccountFilter) url += '?account_id=' + _portfolioAccountFilter;
    const data = await API.get(url);
    State.portfolio = data;
    const { holdings, summary } = data;

    // Load accounts for filter dropdown
    var accounts = State.accounts || [];
    try { accounts = await API.get('/api/accounts'); State.accounts = accounts; } catch(e) {}

    // Allocation by asset type
    const byType = {};
    holdings.forEach(function(h) {
      var t = h.asset_type;
      byType[t] = (byType[t] || 0) + (h.market_value || h.shares * h.avg_cost);
    });

    // Allocation by account
    const byAccount = {};
    holdings.forEach(function(h) {
      var a = h.account_name || 'Unassigned';
      byAccount[a] = (byAccount[a] || 0) + (h.market_value || h.shares * h.avg_cost);
    });

    // Build account filter options
    var acctFilterHtml = '<option value="">ALL ACCOUNTS</option>';
    accounts.forEach(function(a) {
      var sel = (_portfolioAccountFilter == a.id) ? ' selected' : '';
      acctFilterHtml += '<option value="' + a.id + '"' + sel + '>' + _esc(a.name) + '</option>';
    });

    // Build account select options for inline account assignment
    var acctOptsHtml = '<option value="">--</option>';
    accounts.forEach(function(a) {
      acctOptsHtml += '<option value="' + a.id + '">' + _esc(a.name) + '</option>';
    });

    var html = '<div class="flex-between mb-8">'
      + '<div class="flex gap-8">'
      + '<div class="kpi-card" style="padding:8px 14px;min-width:140px">'
      + '<div class="kpi-label">TOTAL VALUE</div>'
      + '<div class="kpi-value" style="font-size:18px">$' + fmt.currency(summary.total_value) + '</div>'
      + '</div>'
      + '<div class="kpi-card ' + col.kpi(summary.total_pnl) + '" style="padding:8px 14px;min-width:140px">'
      + '<div class="kpi-label">TOTAL P&L</div>'
      + '<div class="kpi-value ' + col.kpi(summary.total_pnl) + '" style="font-size:18px">' + (summary.total_pnl >= 0 ? '+$' : '-$') + fmt.currency(Math.abs(summary.total_pnl)) + '</div>'
      + '<div class="kpi-sub">' + fmt.pct(summary.total_pnl_pct) + '</div>'
      + '</div>'
      + '<div class="kpi-card" style="padding:8px 14px;min-width:120px">'
      + '<div class="kpi-label">COST BASIS</div>'
      + '<div class="kpi-value" style="font-size:18px">$' + fmt.compact(summary.total_cost) + '</div>'
      + '</div>'
      + '</div>'
      + '<div class="flex gap-8 align-center">'
      + '<select class="form-select" style="font-size:10px;padding:2px 6px;height:24px;width:auto" onchange="_portfolioAccountFilter=this.value;loadPortfolio()">' + acctFilterHtml + '</select>'
      + '<button class="btn btn-green btn-sm" onclick="Modal.open(\'modal-add-position\');loadAccountsDropdown()">+ ADD POSITION</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="Modal.open(\'modal-manage-accounts\');loadAccountsList()">ACCOUNTS</button>'
      + '<button class="btn btn-blue btn-sm" onclick="exportPortfolioCsv()">EXPORT CSV</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="Modal.open(\'modal-csv-import\')">IMPORT CSV</button>'
      + '<button class="btn btn-ghost btn-sm" onclick="loadPortfolio()">↻ REFRESH</button>'
      + '</div></div>';

    // Charts row — add account allocation chart if accounts exist
    var hasAccounts = Object.keys(byAccount).length > 1 || (Object.keys(byAccount).length === 1 && !byAccount['Unassigned']);
    html += '<div class="panel-grid grid-' + (hasAccounts ? '3' : '2') + '" style="margin-bottom:8px">'
      + '<div class="panel"><div class="panel-header"><span class="panel-title">ALLOCATION BY TYPE</span></div>'
      + '<div class="panel-body" style="height:220px;position:relative"><canvas id="allocation-chart"></canvas></div></div>'
      + '<div class="panel"><div class="panel-header"><span class="panel-title">POSITION P&L</span></div>'
      + '<div class="panel-body" style="height:220px;position:relative"><canvas id="pnl-chart"></canvas></div></div>';
    if (hasAccounts) {
      html += '<div class="panel"><div class="panel-header"><span class="panel-title">ALLOCATION BY ACCOUNT</span></div>'
        + '<div class="panel-body" style="height:220px;position:relative"><canvas id="account-allocation-chart"></canvas></div></div>';
    }
    html += '</div>';

    // AI Analysis Panel
    html += '<div class="panel" id="portfolio-ai-panel" style="margin-bottom:8px;border-color:var(--green-dim)">'
      + '<div class="panel-header" style="border-bottom:1px solid var(--green-dim)">'
      + '<span class="panel-title" style="color:var(--green)">\u25C8 AI PORTFOLIO INTELLIGENCE</span>'
      + '<div class="flex gap-8 align-center">'
      + '<select class="form-select" id="ai-model-select" style="font-size:10px;padding:2px 6px;height:24px;width:auto">'
      + '<option value="gpt-4o">GPT-4o (Advanced)</option><option value="gpt-4o-mini">GPT-4o-mini (Fast)</option></select>'
      + '<button class="btn btn-green btn-sm" id="ai-analyze-btn" onclick="runPortfolioAnalysis()">\u25B2 ANALYZE PORTFOLIO</button>'
      + '</div></div>'
      + '<div id="portfolio-ai-content" class="panel-body">'
      + '<div style="color:var(--text-dim);font-size:11px;padding:8px 0">'
      + 'Click <strong style="color:var(--green)">ANALYZE PORTFOLIO</strong> to run a deep AI analysis of your holdings using GPT-4o. '
      + 'Requires an OpenAI API key configured in <span class="col-symbol" onclick="navigate(\'settings\')" style="cursor:pointer">Settings</span>.'
      + '</div></div></div>';

    // Group by account toggle
    html += '<div class="panel"><div class="panel-header">'
      + '<span class="panel-title">HOLDINGS</span>'
      + '<div class="flex gap-8 align-center">'
      // Quick-filter: client-side row filter on symbol/name substring.
      // Wired with addEventListener after render (never an inline handler).
      + '<input class="form-input" id="pf-quick-filter" type="text" placeholder="FILTER…" '
      + 'autocomplete="off" spellcheck="false" aria-label="Filter holdings by symbol or name" '
      + 'style="font-size:10px;padding:2px 6px;height:24px;width:120px">'
      + '<span class="text-dim" style="font-size:10px">' + summary.num_positions + ' POSITIONS</span>'
      + '<button class="btn btn-ghost btn-sm" style="font-size:9px" onclick="_portfolioGroupByAccount=!_portfolioGroupByAccount;loadPortfolio()">'
      + (_portfolioGroupByAccount ? '\u25BC UNGROUP' : '\u25B6 GROUP BY ACCOUNT') + '</button>'
      + '</div></div>';

    html += '<div class="panel-body" style="padding:0;overflow-x:auto">';

    if (!holdings.length) {
      html += '<div class="empty-state"><span class="empty-icon">\u25C8</span><span>No positions. Add your first holding.</span></div>';
    } else if (_portfolioGroupByAccount) {
      // Group holdings by account
      var groups = {};
      holdings.forEach(function(h) {
        var key = h.account_id || 0;
        if (!groups[key]) groups[key] = { name: h.account_name || 'Unassigned', type: h.acct_type || '', color: h.account_color || '', items: [] };
        groups[key].items.push(h);
      });
      var groupKeys = Object.keys(groups).sort(function(a, b) {
        if (a == 0) return 1; if (b == 0) return -1;
        return groups[a].name.localeCompare(groups[b].name);
      });
      groupKeys.forEach(function(gk) {
        var g = groups[gk];
        var gValue = 0, gCost = 0, gPnl = 0;
        g.items.forEach(function(h) { gValue += (h.market_value || 0); gCost += (h.cost_basis || h.shares * h.avg_cost); });
        gPnl = gValue - gCost;
        var gPct = gCost ? (gPnl / gCost * 100) : 0;
        var colorBar = g.color ? 'border-left:3px solid ' + _esc(g.color) + ';padding-left:8px' : '';
        var typeLabel = g.type ? ' (' + (ACCT_TYPE_LABELS[g.type] || g.type) + ')' : '';
        html += '<div class="pf-group-band" style="padding:8px 12px;background:var(--bg-panel);border-bottom:1px solid var(--border);' + colorBar + '">'
          + '<div class="flex-between">'
          + '<span style="font-size:11px;letter-spacing:.08em;color:var(--green)">' + _esc(g.name.toUpperCase()) + '<span class="text-dim">' + typeLabel + '</span></span>'
          + '<span style="font-size:11px">'
          + '<span class="text-dim">VALUE</span> $' + fmt.currency(gValue)
          + ' &nbsp; <span class="' + col.pnl(gPnl) + '">' + (gPnl >= 0 ? '+' : '') + '$' + fmt.currency(gPnl) + ' (' + fmt.pct(gPct) + ')</span>'
          + '</span></div></div>';
        html += _portfolioTableHtml(g.items, summary, acctOptsHtml, false);
      });
    } else {
      html += _portfolioTableHtml(holdings, summary, acctOptsHtml, true);
    }
    html += '</div></div>';

    view.innerHTML = html;

    // Holdings quick-filter — hides table rows whose SYMBOL or NAME cell
    // doesn't contain the typed substring (works in grouped view too; the
    // group header bands stay visible).
    var pfFilter = document.getElementById('pf-quick-filter');
    if (pfFilter) {
      var pfPanel = pfFilter.closest('.panel');
      pfFilter.addEventListener('input', function() {
        var q = pfFilter.value.trim().toLowerCase();
        if (!pfPanel) return;
        pfPanel.querySelectorAll('tbody tr').forEach(function(tr) {
          if (!q) { tr.style.display = ''; return; }
          var cells = tr.querySelectorAll('td');
          var sym = cells[0] ? cells[0].textContent.toLowerCase() : '';
          var name = cells[1] ? cells[1].textContent.toLowerCase() : '';
          tr.style.display = (sym.indexOf(q) >= 0 || name.indexOf(q) >= 0) ? '' : 'none';
        });
        // Hide orphan group-header bands whose following table has no visible rows.
        pfPanel.querySelectorAll('.pf-group-band').forEach(function(band) {
          if (!q) { band.style.display = ''; return; }
          var table = band.nextElementSibling;
          while (table && table.tagName !== 'TABLE') table = table.nextElementSibling;
          var anyVisible = false;
          if (table) {
            table.querySelectorAll('tbody tr').forEach(function(tr) {
              if (tr.style.display !== 'none') anyVisible = true;
            });
          }
          band.style.display = anyVisible ? '' : 'none';
        });
      });
    }

    // Render charts
    if (holdings.length) {
      var typeLabels = Object.keys(byType);
      var typeValues = typeLabels.map(function(k) { return byType[k]; });
      ChartEngine.createAllocationChart('allocation-chart', typeLabels, typeValues);

      var pnlData = holdings.filter(function(h) { return h.unrealized_pnl != null; });
      ChartEngine.createPnlChart(
        'pnl-chart',
        pnlData.map(function(h) { return h.symbol; }),
        pnlData.map(function(h) { return h.unrealized_pnl; })
      );

      // Account allocation chart
      if (hasAccounts && document.getElementById('account-allocation-chart')) {
        var acctLabels = Object.keys(byAccount);
        var acctValues = acctLabels.map(function(k) { return byAccount[k]; });
        ChartEngine.createAllocationChart('account-allocation-chart', acctLabels, acctValues);
      }
    }

  } catch(e) {
    view.innerHTML = '<div class="empty-state"><span class="empty-icon">\u26A0</span><span class="text-red">' + _esc(e.message) + '</span></div>';
  }
}

async function runPortfolioAnalysis() {
  const btn = document.getElementById('ai-analyze-btn');
  const content = document.getElementById('portfolio-ai-content');
  const model = document.getElementById('ai-model-select')?.value || 'gpt-4o';
  if (!btn || !content) return;

  btn.disabled = true;
  btn.textContent = '◈ ANALYZING...';
  content.innerHTML = `
    <div style="display:flex;align-items:center;gap:10px;padding:16px 0;color:var(--green)">
      <div class="spinner"></div>
      <span style="font-size:11px">Running ${model} analysis across ${State.portfolio?.holdings?.length || 0} positions…</span>
    </div>`;

  try {
    const result = await API.post('/api/portfolio/ai-analysis', { model });
    renderPortfolioAnalysis(result);
  } catch(e) {
    content.innerHTML = `<div class="text-red" style="padding:8px;font-size:11px">Analysis failed: ${_esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ RE-ANALYZE';
  }
}

function renderPortfolioAnalysis(r) {
  const content = document.getElementById('portfolio-ai-content');
  if (!content) return;

  const signalClass = {
    BULLISH: 'signal-bullish', BEARISH: 'signal-bearish',
    NEUTRAL: 'signal-neutral', MIXED: 'signal-material',
  }[r.overall_signal] || 'signal-neutral';

  const scoreColor = r.overall_score >= 7 ? 'var(--green)' : r.overall_score >= 4 ? 'var(--amber)' : 'var(--red)';
  const modelBadge = r.ai_powered
    ? `<span class="ai-badge" style="color:var(--green);font-size:10px">◈ ${r.model_used || 'AI'}</span>`
    : `<span style="font-size:9px;color:var(--text-dim)">RULE-BASED</span>`;

  // Action items
  const actionRows = (r.action_items || []).map(a => {
    const priColor = a.priority === 'HIGH' ? 'var(--red)' : a.priority === 'MEDIUM' ? 'var(--amber)' : 'var(--text-dim)';
    return `<div style="display:flex;gap:8px;padding:6px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:9px;color:${priColor};min-width:48px;padding-top:1px">${_esc(a.priority)}</span>
      <div>
        <div style="font-size:11px;color:var(--text-primary)">${_esc(a.action)}</div>
        <div style="font-size:10px;color:var(--text-dim);margin-top:2px">${_esc(a.rationale)}</div>
      </div>
    </div>`;
  }).join('');

  // Position insights table
  const assessColor = { HOLD: 'var(--text-dim)', ADD: 'var(--green)', TRIM: 'var(--amber)', EXIT: 'var(--red)', REVIEW: 'var(--amber)' };
  const posRows = (r.position_insights || []).map(p =>
    `<tr>
      <td><span class="col-symbol" onclick="openResearch('${_jesc(p.symbol)}')">${_esc(p.symbol)}</span></td>
      <td><span style="color:${assessColor[p.assessment] || 'var(--text-dim)'}; font-size:10px;font-weight:bold">${_esc(p.assessment)}</span></td>
      <td style="color:var(--text-secondary);font-size:10px">${_esc(p.note)}</td>
    </tr>`
  ).join('');

  const listItems = (arr) => (arr || []).map(s =>
    `<div style="display:flex;gap:6px;padding:3px 0;font-size:11px">
      <span style="color:var(--green-dim);flex-shrink:0">▸</span>
      <span style="color:var(--text-secondary)">${_esc(s)}</span>
    </div>`
  ).join('');

  const divScore = r.diversification?.score || 0;
  const divColor = divScore >= 7 ? 'var(--green)' : divScore >= 4 ? 'var(--amber)' : 'var(--red)';
  const concWarnings = (r.diversification?.concentration_warnings || [])
    .map(w => `<div style="font-size:10px;color:var(--amber);margin-top:3px">⚠ ${_esc(w)}</div>`).join('');

  content.innerHTML = `
    <!-- Header row: signal, score, model -->
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px;flex-wrap:wrap">
      <span class="signal-badge ${signalClass}" style="font-size:12px;padding:4px 10px">${_esc(r.overall_signal)}</span>
      <div style="display:flex;align-items:baseline;gap:4px">
        <span style="font-size:22px;font-weight:bold;color:${scoreColor}">${r.overall_score}</span>
        <span style="font-size:11px;color:var(--text-dim)">/10 PORTFOLIO HEALTH</span>
      </div>
      ${modelBadge}
    </div>

    <!-- Executive summary -->
    <div style="background:var(--bg-secondary);border-left:2px solid var(--green);padding:10px 14px;margin-bottom:16px;font-size:12px;color:var(--text-primary);line-height:1.6">
      ${_esc(r.executive_summary)}
    </div>

    <div class="panel-grid grid-2" style="gap:12px;margin-bottom:16px">
      <!-- Strengths -->
      <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
        <div style="font-size:10px;color:var(--green);margin-bottom:8px;letter-spacing:.1em">▲ STRENGTHS</div>
        ${listItems(r.strengths)}
      </div>
      <!-- Risks -->
      <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
        <div style="font-size:10px;color:var(--red);margin-bottom:8px;letter-spacing:.1em">▼ RISKS</div>
        ${listItems(r.risks)}
      </div>
    </div>

    <div class="panel-grid grid-2" style="gap:12px;margin-bottom:16px">
      <!-- Opportunities -->
      <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
        <div style="font-size:10px;color:var(--amber);margin-bottom:8px;letter-spacing:.1em">◎ OPPORTUNITIES</div>
        ${listItems(r.opportunities)}
      </div>
      <!-- Diversification -->
      <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
        <div style="font-size:10px;color:var(--blue);margin-bottom:8px;letter-spacing:.1em">⬡ DIVERSIFICATION</div>
        <div style="display:flex;align-items:baseline;gap:6px;margin-bottom:6px">
          <span style="font-size:18px;font-weight:bold;color:${divColor}">${divScore}</span>
          <span style="font-size:10px;color:var(--text-dim)">/10</span>
        </div>
        <div style="font-size:11px;color:var(--text-secondary)">${_esc(r.diversification?.assessment || '')}</div>
        ${concWarnings}
      </div>
    </div>

    <!-- Action items -->
    ${actionRows ? `
    <div style="margin-bottom:16px">
      <div style="font-size:10px;color:var(--amber);margin-bottom:8px;letter-spacing:.1em">⚡ ACTION ITEMS</div>
      ${actionRows}
    </div>` : ''}

    <!-- Position insights -->
    ${posRows ? `
    <div style="margin-bottom:12px">
      <div style="font-size:10px;color:var(--text-dim);margin-bottom:8px;letter-spacing:.1em">▸ POSITION ASSESSMENTS</div>
      <table class="data-table" style="font-size:11px">
        <thead><tr><th>SYMBOL</th><th>ASSESSMENT</th><th>INSIGHT</th></tr></thead>
        <tbody>${posRows}</tbody>
      </table>
    </div>` : ''}

    <!-- Market context -->
    ${r.market_context ? `
    <div style="font-size:10px;color:var(--text-dim);border-top:1px solid var(--border);padding-top:10px;line-height:1.6">
      <span style="color:var(--text-secondary);letter-spacing:.05em">MACRO CONTEXT:</span> ${_esc(r.market_context)}
    </div>` : ''}
  `;
}

async function deletePosition(id, symbol) {
  if (!confirm(`Remove ${symbol} from portfolio?`)) return;
  try {
    await API.del(`/api/portfolio/${id}`);
    Toast.success(`${symbol} removed from portfolio`);
    loadPortfolio();
  } catch(e) {
    Toast.error('Failed to remove position');
  }
}

// ── Account Management ──────────────────────────────────────────────────────
const ACCT_TYPE_LABELS = {
  brokerage: 'Brokerage', '401k': '401(k)', ira: 'Traditional IRA',
  roth_ira: 'Roth IRA', hsa: 'HSA', '529': '529 Plan',
  trust: 'Trust', crypto: 'Crypto Exchange', other: 'Other'
};

async function loadAccountsDropdown() {
  try {
    const accounts = await API.get('/api/accounts');
    State.accounts = accounts;
    const sel = document.getElementById('pos-account');
    if (!sel) return;
    const curVal = sel.value;
    sel.innerHTML = '<option value="">-- No Account --</option>';
    accounts.forEach(function(a) {
      const opt = document.createElement('option');
      opt.value = a.id;
      opt.textContent = a.name + ' (' + (ACCT_TYPE_LABELS[a.account_type] || a.account_type) + ')';
      sel.appendChild(opt);
    });
    if (curVal) sel.value = curVal;
  } catch(e) {}
}

async function loadAccountsList() {
  try {
    const accounts = await API.get('/api/accounts');
    State.accounts = accounts;
    const container = document.getElementById('accounts-list');
    if (!container) return;
    if (!accounts.length) {
      container.innerHTML = '<div class="text-dim" style="font-size:11px;padding:8px 0">No accounts yet. Create one below.</div>';
      return;
    }
    var html = '<table class="data-table" style="font-size:11px">'
      + '<thead><tr><th>NAME</th><th>TYPE</th><th>INSTITUTION</th><th>POSITIONS</th><th></th></tr></thead><tbody>';
    accounts.forEach(function(a) {
      var colorDot = a.color ? '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + _esc(a.color) + ';margin-right:6px"></span>' : '';
      html += '<tr>'
        + '<td>' + colorDot + _esc(a.name) + '</td>'
        + '<td>' + _esc(ACCT_TYPE_LABELS[a.account_type] || a.account_type) + '</td>'
        + '<td class="text-dim">' + _esc(a.institution || '-') + '</td>'
        + '<td class="text-dim">' + _esc(a.position_count || '-') + '</td>'
        + '<td class="col-actions">'
        + '<button class="btn btn-red btn-sm" onclick="deleteAccount(' + a.id + ', \'' + _esc(_jesc(a.name)) + '\')">DEL</button>'
        + '</td></tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
  } catch(e) {
    var c = document.getElementById('accounts-list');
    if (c) c.innerHTML = '<div class="text-dim">Failed to load accounts</div>';
  }
}

async function submitAddAccount() {
  var nameEl = document.getElementById('acct-name');
  var name = nameEl ? nameEl.value.trim() : '';
  if (!name) { Toast.warn('Account name is required'); return; }
  var acctType = document.getElementById('acct-type').value;
  var institution = document.getElementById('acct-institution').value.trim();
  var color = document.getElementById('acct-color').value;
  var notes = document.getElementById('acct-notes').value.trim();
  try {
    await API.post('/api/accounts', { name: name, account_type: acctType, institution: institution, color: color, notes: notes });
    Toast.success('Account "' + name + '" created');
    if (nameEl) nameEl.value = '';
    document.getElementById('acct-institution').value = '';
    document.getElementById('acct-notes').value = '';
    loadAccountsList();
    loadAccountsDropdown();
  } catch(e) {
    Toast.error('Failed to create account');
  }
}

async function deleteAccount(id, name) {
  if (!confirm('Delete account "' + name + '"? Positions will be unlinked but not deleted.')) return;
  try {
    await API.del('/api/accounts/' + id);
    Toast.success('Account deleted');
    loadAccountsList();
    loadAccountsDropdown();
    if (State.activeView === 'portfolio') loadPortfolio();
  } catch(e) {
    Toast.error('Failed to delete account');
  }
}

async function changePositionAccount(posId, newAccountId) {
  try {
    await API.put('/api/portfolio/' + posId, { account_id: newAccountId });
    Toast.success('Account updated');
  } catch(e) {
    Toast.error('Failed to update account');
  }
}

async function submitAddPosition() {
  const symbol = document.getElementById('pos-symbol').value.trim().toUpperCase();
  const name   = document.getElementById('pos-name').value.trim();
  const shares = parseFloat(document.getElementById('pos-shares').value);
  const cost   = parseFloat(document.getElementById('pos-cost').value);
  const type   = document.getElementById('pos-type').value;
  const notes  = document.getElementById('pos-notes').value.trim();
  const accountSel = document.getElementById('pos-account');
  const accountId = accountSel ? accountSel.value : '';

  if (!symbol || !shares || isNaN(cost) || cost < 0) { Toast.warn('Symbol, shares, and cost are required'); return; }

  try {
    // Auto-fetch name if blank
    let resolvedName = name;
    if (!resolvedName) {
      try {
        const fund = await API.get(`/api/fundamentals/${symbol}`);
        resolvedName = fund.name || symbol;
      } catch(e) {}
    }
    const payload = { symbol, name: resolvedName, shares, avg_cost: cost, asset_type: type, notes };
    if (accountId) payload.account_id = accountId;
    await API.post('/api/portfolio/add', payload);
    Toast.success(`${symbol} added to portfolio`);
    Modal.close('modal-add-position');
    document.getElementById('form-add-position').reset();
    if (State.activeView === 'portfolio') loadPortfolio();
    else navigate('portfolio');
    loadSidebarWatchlist();
  } catch(e) {
    Toast.error('Failed to add position: ' + e.message);
  }
}

// ── Markets View ──────────────────────────────────────────────────────────────
async function loadMarkets() {
  const view = document.getElementById('view-markets');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> LOADING MARKET DATA...</div>`;

  try {
    const [indices, sectors, movers] = await Promise.all([
      API.get('/api/market/indices'),
      API.get('/api/market/sectors'),
      API.get('/api/market/movers'),
    ]);

    view.innerHTML = `
      <div class="panel-grid grid-3" style="margin-bottom:8px">
        ${indices.map(idx => `
          <div class="panel" style="cursor:pointer" onclick="openResearch('${_esc(idx.symbol)}')">
            <div class="panel-body" style="padding:10px">
              <div class="flex-between">
                <span style="color:var(--text-dim);font-size:10px">${_esc(idx.label || idx.symbol)}</span>
                <span class="badge ${idx.change_pct >= 0 ? 'badge-green' : 'badge-red'}">${fmt.pct(idx.change_pct)}</span>
              </div>
              <div style="font-size:18px;font-weight:600;margin-top:4px;color:var(--text-primary)">${fmt.price(idx.price)}</div>
              <div style="font-size:10px;color:var(--text-dim);margin-top:2px">
                H: ${fmt.price(idx.day_high)} &nbsp; L: ${fmt.price(idx.day_low)}
              </div>
            </div>
          </div>`).join('')}
      </div>

      <div class="panel-grid grid-2" style="margin-bottom:8px">
        <!-- Gainers -->
        <div class="panel">
          <div class="panel-header"><span class="panel-title text-green">▲ TOP GAINERS</span></div>
          <div style="overflow-x:auto">
            <table class="data-table">
              <thead><tr><th>#</th><th>SYMBOL</th><th style="text-align:right">PRICE</th><th style="text-align:right">CHG</th><th style="text-align:right">CHG%</th><th style="text-align:right">MKT CAP</th></tr></thead>
              <tbody>
                ${(movers.gainers||[]).map((g,i)=>`<tr>
                  <td class="text-dim" style="font-size:10px">${i+1}</td>
                  <td><span class="col-symbol" onclick="openResearch('${_esc(g.symbol)}')">${_esc(g.symbol)}</span></td>
                  <td class="col-price">$${fmt.price(g.price)}</td>
                  <td class="col-positive">+$${fmt.price(g.change)}</td>
                  <td class="col-positive">${fmt.pct(g.change_pct)}</td>
                  <td class="col-number">${fmt.compact(g.market_cap)}</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>
        </div>
        <!-- Losers -->
        <div class="panel">
          <div class="panel-header"><span class="panel-title" style="color:var(--red)">▼ TOP LOSERS</span></div>
          <div style="overflow-x:auto">
            <table class="data-table">
              <thead><tr><th>#</th><th>SYMBOL</th><th style="text-align:right">PRICE</th><th style="text-align:right">CHG</th><th style="text-align:right">CHG%</th><th style="text-align:right">MKT CAP</th></tr></thead>
              <tbody>
                ${(movers.losers||[]).map((g,i)=>`<tr>
                  <td class="text-dim" style="font-size:10px">${i+1}</td>
                  <td><span class="col-symbol" onclick="openResearch('${_esc(g.symbol)}')">${_esc(g.symbol)}</span></td>
                  <td class="col-price">$${fmt.price(g.price)}</td>
                  <td class="col-negative">-$${fmt.price(Math.abs(g.change))}</td>
                  <td class="col-negative">${fmt.pct(g.change_pct)}</td>
                  <td class="col-number">${fmt.compact(g.market_cap)}</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Sectors Full View -->
      <div class="panel">
        <div class="panel-header"><span class="panel-title">SECTOR HEATMAP</span></div>
        <div class="panel-body">
          <div class="panel-grid grid-4" style="gap:4px">
            ${sectors.map(s => {
              const pct = s.change_pct || 0;
              const intensity = Math.min(Math.abs(pct) * 15, 80);
              const bg = pct >= 0
                ? `rgba(0,255,159,${intensity/300})`
                : `rgba(255,51,85,${intensity/300})`;
              const border = pct >= 0 ? `rgba(0,255,159,${intensity/200})` : `rgba(255,51,85,${intensity/200})`;
              return `<div style="background:${bg};border:1px solid ${border};padding:10px;cursor:pointer;border-radius:2px" onclick="openResearch('${_esc(s.symbol)}')">
                <div style="font-size:10px;color:var(--text-dim)">${_esc(s.sector)}</div>
                <div style="font-size:16px;font-weight:600;${pct>=0?'color:var(--green)':'color:var(--red)'}">${fmt.pct(pct)}</div>
                <div style="font-size:10px;color:var(--text-secondary)">${_esc(s.symbol)} $${fmt.price(s.price)}</div>
              </div>`;
            }).join('')}
          </div>
        </div>
      </div>
    `;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="empty-icon">⚠</span><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

// ── Research View ─────────────────────────────────────────────────────────────
async function openResearch(symbol) {
  navigate('research');
  State.researchSymbol = symbol;
  loadResearchFor(symbol);
}

// Called when the user clicks the "Research" menu item without an explicit
// symbol. Reuse the last-viewed symbol, or pick the first portfolio holding,
// or fall back to AAPL. Without this the user would see the empty splash
// every time they navigated to the tab from elsewhere.
async function loadResearchDefault() {
  if (State.researchSymbol) {
    loadResearchFor(State.researchSymbol);
    return;
  }
  let sym = null;
  try {
    const p = await API.get('/api/portfolio');
    const holdings = (p && p.holdings) || [];
    const first = holdings.find(h => h.asset_type !== 'crypto');
    if (first) sym = first.symbol;
  } catch (e) {}
  if (sym) {
    State.researchSymbol = sym;
    loadResearchFor(sym);
  }
  // else: leave the empty-state splash visible so the user can pick a symbol.
}

let _researchGen = 0;
async function loadResearchFor(symbol) {
  // Whitelist-sanitize at the entry: `symbol` flows into dozens of innerHTML
  // positions, element ids and single-quoted onclick handlers below. Sources
  // include the command bar's raw text and LLM-derived Jarvis action symbols,
  // so strip anything that isn't a real ticker character (^ for indices,
  // = for futures, . - for share classes). One choke point beats escaping
  // every interpolation individually — and keeps ids/lookups consistent.
  symbol = String(symbol || '').replace(/[^A-Za-z0-9.\-^=]/g, '').toUpperCase().slice(0, 12);
  if (!symbol) { Toast.warn('Invalid symbol'); return; }
  const view = document.getElementById('view-research');
  // Generation token: if the user clicks a second symbol before the first
  // request lands, the older (slower) response must NOT overwrite the newer
  // view. Each call gets a monotonically-increasing gen; the await-then-write
  // points below bail when their gen no longer matches the latest.
  const gen = ++_researchGen;
  view.innerHTML = `<div class="loading"><div class="spinner"></div> FETCHING ${_esc(symbol)}...</div>`;

  // Keep the Jarvis strip in sync when the symbol changes within the view
  State.researchSymbol = symbol;
  if (window.Jarvis && Jarvis.onNavigate) Jarvis.onNavigate('research');

  try {
    const [quote, fund] = await Promise.all([
      API.get(`/api/quote/${symbol}`),
      API.get(`/api/fundamentals/${symbol}`),
    ]);
    if (gen !== _researchGen) return;

    const chgCls = col.pnl(quote.change);

    view.innerHTML = `
      <!-- Header -->
      <div class="research-header">
        <div>
          <div class="rh-symbol">${symbol}</div>
          <div class="rh-name">${_esc(fund.name || '')}</div>
          ${fund.sector ? `<div style="margin-top:4px"><span class="badge badge-blue">${_esc(fund.sector)}</span> <span class="badge badge-dim">${_esc(fund.industry || '')}</span></div>` : ''}
        </div>
        <div style="margin-left:auto;text-align:right">
          <div class="rh-price">$${fmt.price(quote.price)}</div>
          <div class="rh-change ${chgCls}">${quote.change >= 0 ? '+' : ''}$${fmt.price(quote.change)} (${fmt.pct(quote.change_pct)})</div>
          <div style="font-size:10px;color:var(--text-dim);margin-top:4px">
            H: $${fmt.price(quote.day_high)} &nbsp; L: $${fmt.price(quote.day_low)} &nbsp;
            52W H: $${fmt.price(quote['52w_high'] || quote.fifty_two_week_high)} &nbsp; 52W L: $${fmt.price(quote['52w_low'] || quote.fifty_two_week_low)}
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-left:16px">
          <button class="btn btn-green btn-sm" onclick="quickAddToPortfolio('${_esc(symbol)}', '${_esc(_jesc(fund.name || ''))}')">+ PORTFOLIO</button>
          <button class="btn btn-blue btn-sm" onclick="quickAddToWatchlist('${_esc(symbol)}', '${_esc(_jesc(fund.name || ''))}')">+ WATCHLIST</button>
        </div>
      </div>

      <!-- Quick-glance fundamentals strip -->
      ${renderFundamentalsStrip(fund)}

      <!-- Tabs: Chart / Fundamentals / News / Options / SEC / Intel / v0.2.0 -->
      <div class="research-tabs" style="display:flex;flex-wrap:wrap;gap:0;border-bottom:1px solid var(--border);margin-bottom:8px">
        <button class="research-tab active" id="rtab-chart" onclick="switchResearchTab('chart','${symbol}')">CHART</button>
        <button class="research-tab" id="rtab-fundamentals" onclick="switchResearchTab('fundamentals','${symbol}')">FUNDAMENTALS</button>
        <button class="research-tab" id="rtab-news" onclick="switchResearchTab('news','${symbol}')">NEWS</button>
        <button class="research-tab" id="rtab-options" onclick="switchResearchTab('options','${symbol}')">OPTIONS</button>
        <button class="research-tab" id="rtab-rnd" onclick="switchResearchTab('rnd','${symbol}')">IV DENSITY</button>
        <button class="research-tab" id="rtab-sec" onclick="switchResearchTab('sec','${symbol}')">SEC</button>
        <button class="research-tab" id="rtab-intel" onclick="switchResearchTab('intel','${symbol}')">⊕ INTEL</button>
        <button class="research-tab" id="rtab-forecast" onclick="switchResearchTab('forecast','${symbol}')">FORECAST</button>
        <button class="research-tab" id="rtab-horizons" onclick="switchResearchTab('horizons','${symbol}')">HORIZONS</button>
        <button class="research-tab" id="rtab-factors" onclick="switchResearchTab('factors','${symbol}')">FACTORS</button>
        <button class="research-tab" id="rtab-eventstudy" onclick="switchResearchTab('eventstudy','${symbol}')">EVENT STUDY</button>
        <button class="research-tab" id="rtab-consensus" onclick="switchResearchTab('consensus','${symbol}')">CONSENSUS</button>
        <button class="research-tab" id="rtab-peerdiv" onclick="switchResearchTab('peerdiv','${symbol}')">PEER DIV</button>
        <button class="research-tab" id="rtab-bayes" onclick="switchResearchTab('bayes','${symbol}')">BAYES SMART</button>
        <button class="research-tab" id="rtab-groundhyp" onclick="switchResearchTab('groundhyp','${symbol}')">GROUNDED HYP</button>
      </div>

      <!-- Chart Tab -->
      <div id="rpanel-chart">
        <div class="panel" style="margin-bottom:8px">
          <div class="panel-header">
            <span class="panel-title">PRICE CHART — ${symbol}</span>
            <div class="chart-controls" id="chart-period-btns">
              ${['1d','5d','1mo','3mo','6mo','1y','2y','5y'].map(p =>
                `<button class="chart-btn ${p === State.chartPeriod ? 'active' : ''}" onclick="setChartPeriod('${p}')">${p.toUpperCase()}</button>`
              ).join('')}
              <span style="margin-left:8px"></span>
              ${['1m','5m','15m','30m','1h','1d','1wk','1mo'].map(iv => {
                const allowed = CHART_INTERVALS_FOR[State.chartPeriod] || ['1d'];
                const disabled = allowed.indexOf(iv) === -1;
                return `<button class="chart-btn chart-interval-btn ${iv === State.chartInterval ? 'active' : ''}" data-interval="${iv}" ${disabled ? 'disabled' : ''} onclick="setChartInterval('${iv}')">${iv}</button>`;
              }).join('')}
            </div>
          </div>
          <div id="price-chart-container" style="height:400px"></div>
          <div id="indicator-bar" class="panel-body" style="border-top:1px solid var(--border);padding:6px 10px;display:flex;flex-wrap:wrap;gap:10px;font-size:10px"></div>
        </div>
      </div>

      <!-- Fundamentals Tab -->
      <div id="rpanel-fundamentals" style="display:none">
        <div class="panel" style="margin-bottom:8px">
          <div class="panel-header"><span class="panel-title">FUNDAMENTALS — ${symbol}</span></div>
          <div class="fund-grid">
            ${renderFundamentals(fund)}
          </div>
        </div>
      </div>

      <!-- News Tab -->
      <div id="rpanel-news" style="display:none">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">NEWS — ${symbol}</span></div>
          <div id="news-feed-${symbol}" style="max-height:calc(100vh - 300px);overflow-y:auto">
            <div class="loading"><div class="spinner"></div></div>
          </div>
        </div>
      </div>

      <!-- Options Tab -->
      <div id="rpanel-options" style="display:none">
        <div class="panel">
          <div class="panel-header">
            <span class="panel-title">OPTIONS CHAIN — ${symbol}</span>
            <div style="display:flex;gap:8px;align-items:center">
              <span style="font-size:10px;color:var(--text-dim)">EXPIRY:</span>
              <select class="form-select" id="options-expiry-select" style="font-size:11px;padding:2px 6px;min-width:120px" onchange="loadOptionsChain('${symbol}', this.value)">
                <option value="">Loading...</option>
              </select>
            </div>
          </div>
          <div id="options-chain-body" style="overflow-x:auto;max-height:calc(100vh - 300px);overflow-y:auto">
            <div class="loading"><div class="spinner"></div> Loading options...</div>
          </div>
        </div>
      </div>

      <!-- INTEL Tab — fused multi-source intelligence -->
      <div id="rpanel-intel" style="display:none">
        <div id="intel-body-${symbol}">
          <div class="empty-state"><span style="color:var(--text-dim)">Click INTEL tab to load fused intelligence</span></div>
        </div>
      </div>

      <!-- SEC Tab -->
      <div id="rpanel-sec" style="display:none">
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          <div class="panel">
            <div class="panel-header">
              <span class="panel-title">RECENT FILINGS — ${symbol}</span>
            </div>
            <div id="sec-filings-${symbol}" style="max-height:calc(100vh - 350px);overflow-y:auto">
              <div class="loading"><div class="spinner"></div></div>
            </div>
          </div>
          <div class="panel">
            <div class="panel-header">
              <span class="panel-title">INSIDER TRANSACTIONS — ${symbol}</span>
            </div>
            <div id="sec-insiders-${symbol}" style="max-height:calc(100vh - 350px);overflow-y:auto">
              <div class="loading"><div class="spinner"></div></div>
            </div>
          </div>
        </div>
      </div>

      <!-- IV DENSITY Tab (v0.2.0) -->
      <div id="rpanel-rnd" style="display:none">
        <div id="rnd-body-${symbol}">
          <div class="loading"><div class="spinner"></div> Loading implied density…</div>
        </div>
      </div>

      <!-- FORECAST Tab (probabilistic forecast — v0.2.0) -->
      <div id="rpanel-forecast" style="display:none">
        <section class="panel mb-8">
          <div class="panel-header">
            <span class="panel-title">PROBABILISTIC FORECAST</span>
            <div class="flex gap-8 align-center">
              <span style="font-size:10px;color:var(--text-dim)">HORIZON:</span>
              <select class="form-select" data-pf-horizon
                      style="font-size:11px;padding:2px 6px">
                <option value="5">5D</option>
                <option value="20" selected>20D</option>
                <option value="60">60D</option>
                <option value="120">120D</option>
              </select>
            </div>
          </div>
          <div id="probforecast-panel" class="panel-body"></div>
        </section>
      </div>

      <!-- HORIZONS Tab (multi-horizon forecast — v0.2.0) -->
      <div id="rpanel-horizons" style="display:none">
        <div class="panel" id="mh-panel-wrapper">
          <div class="panel-header">
            <div class="panel-title">MULTI-HORIZON FORECAST</div>
          </div>
          <div class="panel-body" id="mh-panel"></div>
        </div>
      </div>

      <!-- FACTORS Tab (Fama-French — v0.2.0) -->
      <div id="rpanel-factors" style="display:none">
        <div id="factor-exposure-host" class="card"></div>
      </div>

      <!-- EVENT STUDY Tab (v0.2.0) -->
      <div id="rpanel-eventstudy" style="display:none">
        <div id="es-container-${symbol}"></div>
      </div>

      <!-- CONSENSUS Tab (v0.3.0) -->
      <div id="rpanel-consensus" style="display:none">
        <div id="consensus-host" class="card"></div>
      </div>

      <!-- PEER DIVERGENCE Tab (v0.3.0) -->
      <div id="rpanel-peerdiv" style="display:none">
        <div id="peerdiv-host" class="card"></div>
      </div>

      <!-- BAYES SMART MONEY Tab (v0.3.0) -->
      <div id="rpanel-bayes" style="display:none">
        <div id="bayes-smart-host" class="card"></div>
      </div>

      <!-- GROUNDED HYPOTHESIS Tab (v0.3.0) -->
      <div id="rpanel-groundhyp" style="display:none">
        <div id="grounded-hyp-host" class="card"></div>
      </div>
    `;

    // Load chart
    State.chartSymbol = symbol;
    loadPriceChart(symbol, State.chartPeriod, State.chartInterval);

    // Load news async
    loadNewsFor(symbol, `news-feed-${symbol}`);

    // Wikidata corporate-metadata profile (HQ, CEO, employees, parent, …)
    DataPanels.appendWikidataFacts(view, symbol);

  } catch(e) {
    if (gen !== _researchGen) return;
    view.innerHTML = `<div class="empty-state"><span class="empty-icon">⚠</span><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

function renderFundamentalsStrip(fund) {
  const cells = [
    ['P/E',     fmt.num(fund.pe_ratio)],
    ['P/B',     fmt.num(fund.pb_ratio)],
    ['DIV YLD', fund.dividend_yield != null ? fmt.pct(fund.dividend_yield * 100, false) : '—'],
    ['ROE',     fund.return_on_equity != null ? fmt.pct(fund.return_on_equity * 100, false) : '—'],
    ['BETA',    fmt.num(fund.beta)],
    ['MKT CAP', '$' + fmt.compact(fund.market_cap)],
  ];
  const inner = cells.map(([k, v]) =>
    `<div style="flex:1;padding:6px 10px;border-right:1px solid var(--border);min-width:80px">
       <div style="font-size:9px;color:var(--text-dim);letter-spacing:0.5px">${_esc(k)}</div>
       <div style="font-size:13px;font-weight:600">${v === 'null' || v === 'NaN' || v == null ? '—' : v}</div>
     </div>`
  ).join('');
  return `<div class="panel" style="margin:0 0 8px 0">
    <div class="panel-body" style="display:flex;padding:0">${inner}</div>
  </div>`;
}

function renderFundamentals(fund) {
  const items = [
    ['Market Cap', '$' + fmt.compact(fund.market_cap)],
    ['Enterprise Value', '$' + fmt.compact(fund.enterprise_value)],
    ['P/E Ratio', fmt.num(fund.pe_ratio)],
    ['Forward P/E', fmt.num(fund.forward_pe)],
    ['PEG Ratio', fmt.num(fund.peg_ratio)],
    ['P/S Ratio', fmt.num(fund.ps_ratio)],
    ['P/B Ratio', fmt.num(fund.pb_ratio)],
    ['EV/EBITDA', fmt.num(fund.ev_ebitda)],
    ['Beta', fmt.num(fund.beta)],
    ['Revenue', '$' + fmt.compact(fund.revenue)],
    ['Revenue Growth', fmt.pct(fund.revenue_growth != null ? fund.revenue_growth * 100 : null)],
    ['Earnings Growth', fmt.pct(fund.earnings_growth != null ? fund.earnings_growth * 100 : null)],
    ['Profit Margin', fmt.pct(fund.profit_margin != null ? fund.profit_margin * 100 : null)],
    ['Gross Margin', fmt.pct(fund.gross_margin != null ? fund.gross_margin * 100 : null)],
    ['Operating Margin', fmt.pct(fund.operating_margin != null ? fund.operating_margin * 100 : null)],
    ['ROE', fmt.pct(fund.return_on_equity != null ? fund.return_on_equity * 100 : null)],
    ['ROA', fmt.pct(fund.return_on_assets != null ? fund.return_on_assets * 100 : null)],
    ['Debt/Equity', fmt.num(fund.debt_equity)],
    ['Current Ratio', fmt.num(fund.current_ratio)],
    ['Free Cash Flow', '$' + fmt.compact(fund.free_cashflow)],
    ['Dividend Yield', fmt.pct(fund.dividend_yield != null ? fund.dividend_yield * 100 : null)],
    ['Payout Ratio', fmt.pct(fund.payout_ratio != null ? fund.payout_ratio * 100 : null)],
    ['Short % Float', fmt.pct(fund.short_percent_float != null ? fund.short_percent_float * 100 : null)],
    ['Short Ratio', fmt.num(fund.short_ratio)],
    ['Shares Out', fmt.compact(fund.shares_outstanding)],
    ['Float', fmt.compact(fund.float_shares)],
    ['50D Avg', '$' + fmt.price(fund['50d_avg'])],
    ['200D Avg', '$' + fmt.price(fund['200d_avg'])],
    ['Analyst Target', '$' + fmt.price(fund.analyst_target)],
    ['Analyst Rating', (fund.recommendation || '—').toUpperCase()],
    ['# Analysts', fmt.num(fund.num_analysts, 0)],
    ['Country', fund.country || '—'],
    ['Employees', fmt.compact(fund.employees)],
  ];
  return items.map(([k, v]) => `
    <div class="fund-item">
      <span class="fund-key">${k}</span>
      <span class="fund-val">${v === 'null' || v === 'NaN' ? '—' : _esc(v)}</span>
    </div>`).join('');
}

let _newsFeedGen = 0;
async function loadNewsFor(symbol, containerId) {
  const el = document.getElementById(containerId);
  if (!el) return;
  // Race-guard: if the user clicks a second symbol before the first news
  // fetch lands, the older response must not overwrite the newer panel.
  const gen = ++_newsFeedGen;
  try {
    const news = await API.get(`/api/news/${symbol}`);
    if (gen !== _newsFeedGen) return;
    if (!news.length) {
      el.innerHTML = `<div class="empty-state"><span>No news found</span></div>`;
      return;
    }
    el.innerHTML = news.map(n => `
      <div class="news-item" onclick="window.open('${_esc(_jesc(safeUrl(n.url)))}', '_blank')">
        <div class="news-title">${_esc(n.title || '')}</div>
        <div class="news-summary">${_esc(n.summary ? n.summary.substring(0, 120) + '...' : '')}</div>
        <div class="news-meta">
          <span class="news-source">${_esc(n.source || '')}</span>
          <span class="news-time">${_esc(fmt.timeAgo(n.published))}</span>
        </div>
      </div>`).join('');
  } catch(e) {
    if (gen !== _newsFeedGen) return;
    el.innerHTML = `<div class="empty-state"><span class="text-red">Failed to load news</span></div>`;
  }
}

let _priceChartGen = 0;
async function loadPriceChart(symbol, period = '6mo', interval = '1d') {
  // Rapid period/interval clicks can race — guard with a generation token so
  // the older (slower) /api/chart response can't overwrite the chart that
  // matches the user's current selection.
  const gen = ++_priceChartGen;
  try {
    const data = await API.get(`/api/chart/${symbol}?period=${period}&interval=${interval}`);
    if (gen !== _priceChartGen) return;
    const chartRef = ChartEngine.createPriceChart('price-chart-container', data.data, { height: 400 });
    State.chartRef = chartRef;

    // Add MA overlays
    if (data.data.length >= 20) {
      ChartEngine.addSMAOverlay(chartRef, data.data, 20, 'rgba(0,200,255,0.7)');
    }
    if (data.data.length >= 50) {
      ChartEngine.addSMAOverlay(chartRef, data.data, 50, 'rgba(255,170,0,0.7)');
    }

    // Indicator bar
    const ind = data.indicators || {};
    const indBar = document.getElementById('indicator-bar');
    if (indBar) {
      const items = [
        ['RSI(14)', ind.rsi, v => { const c = v > 70 ? 'text-red' : v < 30 ? 'text-green' : ''; return `<span class="${c}">${fmt.num(v)}</span>`; }],
        ['MACD', ind.macd, v => `<span class="${col.pnl(v)}">${fmt.num(v)}</span>`],
        ['Signal', ind.macd_signal, v => fmt.num(v)],
        ['SMA20', ind.sma20, v => `$${fmt.price(v)}`],
        ['SMA50', ind.sma50, v => `$${fmt.price(v)}`],
        ['SMA200', ind.sma200, v => `$${fmt.price(v)}`],
        ['BB Upper', ind.bb_upper, v => `$${fmt.price(v)}`],
        ['BB Lower', ind.bb_lower, v => `$${fmt.price(v)}`],
        ['ATR', ind.atr, v => fmt.num(v)],
        ['vs SMA20', ind.price_vs_sma20, v => `<span class="${col.pct(v)}">${fmt.pct(v)}</span>`],
        ['vs SMA50', ind.price_vs_sma50, v => `<span class="${col.pct(v)}">${fmt.pct(v)}</span>`],
        ['vs SMA200', ind.price_vs_sma200, v => `<span class="${col.pct(v)}">${fmt.pct(v)}</span>`],
      ];
      indBar.innerHTML = items.filter(([,v]) => v != null).map(([label, val, render]) =>
        `<span><span class="text-dim">${label}:</span> ${render(val)}</span>`
      ).join(' &nbsp;|&nbsp; ');
    }
  } catch(e) {
    console.error('Chart load failed', e);
  }
}

// Map each period to the set of intervals yfinance accepts as sensible
// combinations. Anything outside this list returns empty data, so we
// gate the UI rather than letting the user shoot themselves.
const CHART_INTERVALS_FOR = {
  '1d':  ['1m','5m','15m','30m','1h'],
  '5d':  ['1m','5m','15m','30m','1h'],
  '1mo': ['15m','30m','1h','1d'],
  '3mo': ['15m','30m','1h','1d'],
  '6mo': ['1h','1d','1wk'],
  '1y':  ['1h','1d','1wk'],
  '2y':  ['1d','1wk','1mo'],
  '5y':  ['1d','1wk','1mo'],
  '10y': ['1d','1wk','1mo'],
  'max': ['1d','1wk','1mo'],
};

function _validIntervalFor(period, currentInterval) {
  const allowed = CHART_INTERVALS_FOR[period] || ['1d'];
  if (allowed.indexOf(currentInterval) !== -1) return currentInterval;
  // Snap to the closest sensible default, preferring '1d'.
  return allowed.indexOf('1d') !== -1 ? '1d' : allowed[0];
}

function _refreshIntervalButtons() {
  const period = State.chartPeriod;
  const allowed = CHART_INTERVALS_FOR[period] || ['1d'];
  document.querySelectorAll('#chart-period-btns .chart-interval-btn').forEach(b => {
    const iv = b.dataset.interval;
    const valid = allowed.indexOf(iv) !== -1;
    b.disabled = !valid;
    b.classList.toggle('active', valid && iv === State.chartInterval);
  });
}

function setChartPeriod(p) {
  State.chartPeriod = p;
  // If the current interval isn't valid for the new period, snap it.
  const newInterval = _validIntervalFor(p, State.chartInterval);
  if (newInterval !== State.chartInterval) State.chartInterval = newInterval;
  document.querySelectorAll('#chart-period-btns .chart-btn').forEach(b => {
    if (b.classList.contains('chart-interval-btn')) return;
    b.classList.toggle('active', b.textContent.toLowerCase() === p.toLowerCase());
  });
  _refreshIntervalButtons();
  if (State.chartSymbol) loadPriceChart(State.chartSymbol, p, State.chartInterval);
}

function setChartInterval(iv) {
  const allowed = CHART_INTERVALS_FOR[State.chartPeriod] || ['1d'];
  if (allowed.indexOf(iv) === -1) return; // ignore disabled clicks
  State.chartInterval = iv;
  _refreshIntervalButtons();
  if (State.chartSymbol) loadPriceChart(State.chartSymbol, State.chartPeriod, iv);
}

// ── Crypto View ───────────────────────────────────────────────────────────────
async function loadCrypto() {
  const view = document.getElementById('view-crypto');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> FETCHING CRYPTO MARKETS...</div>`;

  try {
    const [global_, market] = await Promise.all([
      API.get('/api/crypto/global'),
      API.get('/api/crypto/market?limit=100'),
    ]);

    State.cryptoGlobal = global_;
    State.cryptoMarket = market;

    view.innerHTML = `
      <!-- Global Stats -->
      <div class="kpi-grid" style="margin-bottom:8px">
        <div class="kpi-card ${col.kpi(global_.market_cap_change_24h)}">
          <div class="kpi-label">TOTAL MKT CAP</div>
          <div class="kpi-value" style="font-size:16px">$${fmt.compact(global_.total_market_cap_usd)}</div>
          <div class="kpi-sub">${fmt.pct(global_.market_cap_change_24h)} 24H</div>
        </div>
        <div class="kpi-card blue">
          <div class="kpi-label">24H VOLUME</div>
          <div class="kpi-value blue" style="font-size:16px">$${fmt.compact(global_.total_volume_usd)}</div>
        </div>
        <div class="kpi-card amber">
          <div class="kpi-label">BTC DOMINANCE</div>
          <div class="kpi-value amber" style="font-size:16px">${fmt.num(global_.btc_dominance)}%</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">ETH DOMINANCE</div>
          <div class="kpi-value" style="font-size:16px">${fmt.num(global_.eth_dominance)}%</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">ACTIVE COINS</div>
          <div class="kpi-value" style="font-size:16px">${fmt.compact(global_.active_coins)}</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">MARKETS</div>
          <div class="kpi-value" style="font-size:16px">${fmt.compact(global_.markets)}</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">CRYPTO MARKET — TOP 100</span>
          <button class="btn btn-ghost btn-sm" onclick="loadCrypto()">↻ REFRESH</button>
        </div>
        <div style="overflow-x:auto">
          <table class="data-table" style="min-width:900px">
            <thead><tr>
              <th>#</th><th colspan="2">COIN</th>
              <th style="text-align:right">PRICE</th>
              <th style="text-align:right">1H%</th>
              <th style="text-align:right">24H%</th>
              <th style="text-align:right">7D%</th>
              <th style="text-align:right">MKT CAP</th>
              <th style="text-align:right">24H VOLUME</th>
              <th style="text-align:right">CIRC SUPPLY</th>
              <th>7D TREND</th>
            </tr></thead>
            <tbody>
              ${market.map(c => `
                <tr style="cursor:pointer" onclick="openCryptoResearch('${_jesc(c.id)}', '${_jesc(c.symbol)}')">
                  <td class="crypto-rank">${c.market_cap_rank}</td>
                  <td><img src="${_esc(c.image)}" class="crypto-logo" onerror="this.style.display='none'" loading="lazy"></td>
                  <td>
                    <div class="col-symbol">${_esc(c.symbol)}</div>
                    <div class="col-name" style="font-size:10px">${_esc(c.name)}</div>
                  </td>
                  <td class="col-price">$${fmt.price(c.price)}</td>
                  <td class="${col.pct(c.change_1h)}">${fmt.pct(c.change_1h)}</td>
                  <td class="${col.pct(c.change_24h)}">${fmt.pct(c.change_24h)}</td>
                  <td class="${col.pct(c.change_7d)}">${fmt.pct(c.change_7d)}</td>
                  <td class="col-number">$${fmt.compact(c.market_cap)}</td>
                  <td class="col-number">$${fmt.compact(c.volume_24h)}</td>
                  <td class="col-number">${fmt.compact(c.circulating_supply)} ${c.symbol}</td>
                  <td>
                    <canvas class="sparkline-cell" width="80" height="24" id="spark-${c.id}"></canvas>
                  </td>
                </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>
    `;

    // Render sparklines
    requestAnimationFrame(() => {
      market.forEach(c => {
        const canvas = document.getElementById(`spark-${c.id}`);
        if (canvas && c.sparkline?.length) {
          const color = c.change_7d >= 0 ? '#00ff9f' : '#ff3355';
          ChartEngine.drawSparkline(canvas, c.sparkline, color);
        }
      });
    });

    // DefiLlama TVL + BTC mempool + cross-exchange (no-key Tier 1)
    DataPanels.appendDefiPanel(view);
    // Finviz sector heatmap (1w performance, P/E, stock counts)
    DataPanels.appendFinvizSectors(view);

  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="empty-icon">⚠</span><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

async function openCryptoResearch(coinId, symbol) {
  // Same entry sanitization as loadResearchFor — symbol reaches innerHTML.
  symbol = String(symbol || '').replace(/[^A-Za-z0-9.\-]/g, '').toUpperCase().slice(0, 12);
  coinId = String(coinId || '').replace(/[^a-z0-9\-]/g, '');
  const gen = ++_researchGen;
  navigate('research');
  const view = document.getElementById('view-research');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> FETCHING ${_esc(symbol)}...</div>`;

  try {
    const [coin, chartData] = await Promise.all([
      API.get(`/api/crypto/quote/${coinId}`),
      API.get(`/api/crypto/chart/${coinId}?days=90`),
    ]);

    // A newer research request (stock or crypto) superseded this one.
    if (gen !== _researchGen || !view.isConnected) return;

    const chgCls = col.pnl(coin.change_24h);

    view.innerHTML = `
      <div class="research-header">
        <div>
          <div class="rh-symbol">${_esc(coin.symbol)}</div>
          <div class="rh-name">${_esc(coin.name)}</div>
          <div style="margin-top:4px"><span class="badge badge-purple">CRYPTO</span> <span class="badge badge-dim">RANK #${coin.market_cap_rank}</span></div>
        </div>
        <div style="margin-left:auto;text-align:right">
          <div class="rh-price">$${fmt.price(coin.price)}</div>
          <div class="rh-change ${chgCls}">${fmt.pct(coin.change_24h)} (24H)</div>
          <div style="font-size:10px;color:var(--text-dim);margin-top:4px">
            7D: ${fmt.pct(coin.change_7d)} &nbsp;
            30D: ${fmt.pct(coin.change_30d)} &nbsp;
            1Y: ${fmt.pct(coin.change_1y)}
          </div>
        </div>
        <div style="display:flex;flex-direction:column;gap:4px;margin-left:16px">
          <button class="btn btn-green btn-sm" onclick="quickAddToPortfolio('${_esc(coin.symbol)}', '${_esc(_jesc(coin.name))}', 'crypto')">+ PORTFOLIO</button>
          <button class="btn btn-blue btn-sm" onclick="quickAddToWatchlist('${_esc(coin.symbol)}', '${_esc(_jesc(coin.name))}')">+ WATCHLIST</button>
        </div>
      </div>

      <div class="panel" style="margin-bottom:8px">
        <div class="panel-header"><span class="panel-title">PRICE CHART — ${_esc(coin.symbol)} / USD</span>
          <div class="chart-controls" id="crypto-period-btns">
            ${[7,14,30,90,180,365].map(d=>`<button class="chart-btn ${d===90?'active':''}" onclick="loadCryptoChart('${coinId}',${d})">${d}D</button>`).join('')}
          </div>
        </div>
        <div id="crypto-chart-container" style="height:380px"></div>
      </div>

      <div class="panel-grid grid-2">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">MARKET DATA</span></div>
          <div class="fund-grid">
            ${[
              ['Market Cap', '$' + fmt.compact(coin.market_cap)],
              ['24H Volume', '$' + fmt.compact(coin.volume_24h)],
              ['Circ Supply', fmt.compact(coin.circulating_supply) + ' ' + coin.symbol],
              ['Total Supply', fmt.compact(coin.total_supply)],
              ['Max Supply', coin.max_supply != null ? fmt.compact(coin.max_supply) : '∞'],
              ['ATH', '$' + fmt.price(coin.ath)],
              ['ATH Change', fmt.pct(coin.ath_change_pct)],
              ['ATL', '$' + fmt.price(coin.atl)],
              ['CMC Rank', '#' + coin.market_cap_rank],
            ].map(([k,v]) => `<div class="fund-item"><span class="fund-key">${k}</span><span class="fund-val">${_esc(v)}</span></div>`).join('')}
          </div>
        </div>
        <div class="panel">
          <div class="panel-header"><span class="panel-title">ABOUT</span></div>
          <div class="panel-body">
            <p style="font-size:11px;color:var(--text-secondary);line-height:1.6">${_esc(coin.description || 'No description available.')}</p>
          </div>
        </div>
      </div>
    `;

    // Render chart
    if (chartData.length) {
      ChartEngine.createPriceChart('crypto-chart-container', chartData, { type: 'line', height: 380, showVolume: false });
    }
  } catch(e) {
    if (gen !== _researchGen || !view.isConnected) return;
    view.innerHTML = `<div class="empty-state"><span class="empty-icon">⚠</span><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

async function loadCryptoChart(coinId, days) {
  document.querySelectorAll('#crypto-period-btns .chart-btn').forEach(b => {
    b.classList.toggle('active', b.textContent === `${days}D`);
  });
  try {
    const data = await API.get(`/api/crypto/chart/${coinId}?days=${days}`);
    ChartEngine.createPriceChart('crypto-chart-container', data, { type: 'line', height: 380, showVolume: false });
  } catch(e) {}
}

// ── Watchlist View ────────────────────────────────────────────────────────────
async function loadWatchlistView() {
  const view = document.getElementById('view-watchlist');
  view.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
  try {
    const items = await API.get('/api/watchlist');
    State.watchlist = items;

    view.innerHTML = `
      <div class="flex-between mb-8">
        <span class="panel-title">WATCHLIST (${items.length})</span>
        <button class="btn btn-green btn-sm" onclick="Modal.open('modal-add-watchlist')">+ ADD SYMBOL</button>
      </div>
      <div class="panel">
        <div style="overflow-x:auto">
          ${items.length ? `
          <table class="data-table">
            <thead><tr>
              <th>SYMBOL</th><th>NAME</th><th>TYPE</th>
              <th style="text-align:right">PRICE</th>
              <th style="text-align:right">CHANGE</th>
              <th style="text-align:right">%</th>
              <th style="text-align:right">HIGH</th>
              <th style="text-align:right">LOW</th>
              <th style="text-align:right">ALERT HIGH</th>
              <th style="text-align:right">ALERT LOW</th>
              <th></th>
            </tr></thead>
            <tbody>
              ${items.map(i => {
                const alertHigh = i.alert_high && i.price >= i.alert_high;
                const alertLow  = i.alert_low  && i.price <= i.alert_low;
                return `<tr ${alertHigh || alertLow ? 'style="background:rgba(255,170,0,0.05)"' : ''}>
                  <td>
                    <span class="col-symbol" onclick="openResearch('${_jesc(i.symbol)}')">${_esc(i.symbol)}</span>
                    ${alertHigh ? '<span class="badge badge-amber" style="margin-left:4px">ALERT ▲</span>' : ''}
                    ${alertLow  ? '<span class="badge badge-red" style="margin-left:4px">ALERT ▼</span>' : ''}
                  </td>
                  <td class="col-name">${_esc(i.name || '—')}</td>
                  <td><span class="badge badge-dim">${_esc(i.asset_type)}</span></td>
                  <td class="col-price">${fmt.price(i.price)}</td>
                  <td class="${col.pnl(i.change)}">${fmt.price(i.change)}</td>
                  <td class="${col.pct(i.change_pct)}">${fmt.pct(i.change_pct)}</td>
                  <td class="col-number">${fmt.price(i.day_high)}</td>
                  <td class="col-number">${fmt.price(i.day_low)}</td>
                  <td class="col-number ${alertHigh ? 'text-amber' : ''}">${i.alert_high ? '$' + fmt.price(i.alert_high) : '—'}</td>
                  <td class="col-number ${alertLow ? 'text-red' : ''}">${i.alert_low ? '$' + fmt.price(i.alert_low) : '—'}</td>
                  <td class="col-actions">
                    <button class="btn btn-ghost btn-sm" onclick="openResearch('${_jesc(i.symbol)}')">CHART</button>
                    <button class="btn btn-red btn-sm" onclick="removeWatchlist(${i.id}, '${_jesc(i.symbol)}')">✕</button>
                  </td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>` : `<div class="empty-state"><span class="empty-icon">◎</span><span>Watchlist is empty</span></div>`}
        </div>
      </div>`;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

async function removeWatchlist(id, symbol) {
  if (!confirm(`Remove ${symbol} from watchlist?`)) return;
  try {
    await API.del(`/api/watchlist/${id}`);
    Toast.success(`${symbol} removed`);
    loadWatchlistView();
    loadSidebarWatchlist();
  } catch(e) { Toast.error('Failed'); }
}

async function submitAddWatchlist() {
  const symbol = document.getElementById('wl-symbol').value.trim().toUpperCase();
  const alertHigh = parseFloat(document.getElementById('wl-alert-high').value) || null;
  const alertLow  = parseFloat(document.getElementById('wl-alert-low').value) || null;
  const type = document.getElementById('wl-type').value;
  if (!symbol) { Toast.warn('Symbol required'); return; }
  try {
    let name = '';
    try { const f = await API.get(`/api/fundamentals/${symbol}`); name = f.name || ''; } catch(e) {}
    await API.post('/api/watchlist/add', { symbol, name, asset_type: type, alert_high: alertHigh, alert_low: alertLow });
    Toast.success(`${symbol} added to watchlist`);
    Modal.close('modal-add-watchlist');
    document.getElementById('form-add-watchlist').reset();
    loadWatchlistView();
    loadSidebarWatchlist();
  } catch(e) { Toast.error('Failed: ' + e.message); }
}

// ── Transactions View ─────────────────────────────────────────────────────────
async function loadTransactions() {
  const view = document.getElementById('view-transactions');
  view.innerHTML = `<div class="loading"><div class="spinner"></div></div>`;
  try {
    // KPIs come from the server-side aggregate (covers ALL rows); the table
    // body still uses the paginated 200-row slice for display.
    const [txns, summary] = await Promise.all([
      API.get('/api/transactions?limit=200'),
      API.get('/api/transactions/summary').catch(() => null),
    ]);
    const total_buy  = summary ? summary.total_buy  : txns.filter(t=>t.action==='BUY').reduce((s,t)=>s+(t.total||0),0);
    const total_sell = summary ? summary.total_sell : txns.filter(t=>t.action==='SELL').reduce((s,t)=>s+(t.total||0),0);
    const total_count = summary ? summary.count : txns.length;

    view.innerHTML = `
      <div class="flex-between mb-8">
        <div class="flex gap-8">
          <div class="kpi-card" style="padding:8px 14px">
            <div class="kpi-label">TOTAL BUY</div>
            <div class="kpi-value" style="font-size:16px;color:var(--red)">$${fmt.currency(total_buy)}</div>
          </div>
          <div class="kpi-card" style="padding:8px 14px">
            <div class="kpi-label">TOTAL SELL</div>
            <div class="kpi-value" style="font-size:16px;color:var(--green)">$${fmt.currency(total_sell)}</div>
          </div>
          <div class="kpi-card" style="padding:8px 14px">
            <div class="kpi-label">TRANSACTIONS</div>
            <div class="kpi-value" style="font-size:16px">${total_count}</div>
          </div>
        </div>
        <button class="btn btn-green btn-sm" onclick="Modal.open('modal-add-txn')">+ LOG TRADE</button>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="panel-title">TRANSACTION HISTORY</span></div>
        <div style="overflow-x:auto">
          ${txns.length ? `
          <table class="data-table">
            <thead><tr>
              <th>DATE</th><th>SYMBOL</th><th>ACTION</th>
              <th style="text-align:right">SHARES</th>
              <th style="text-align:right">PRICE</th>
              <th style="text-align:right">TOTAL</th>
              <th style="text-align:right">FEES</th>
              <th>NOTES</th>
              <th class="tx-id">TX ID</th>
            </tr></thead>
            <tbody>
              ${txns.map(t => `<tr>
                <td class="text-dim">${_esc(t.date || '—')}</td>
                <td><span class="col-symbol" onclick="openResearch('${_esc(t.symbol)}')">${_esc(t.symbol)}</span></td>
                <td><span class="badge ${t.action==='BUY' ? 'badge-green' : 'badge-red'}">${_esc(t.action)}</span></td>
                <td class="col-number">${fmt.num(t.shares, 4)}</td>
                <td class="col-number">$${fmt.price(t.price)}</td>
                <td class="col-number">$${fmt.currency(t.total)}</td>
                <td class="col-number">${t.fees ? '$' + fmt.num(t.fees) : '—'}</td>
                <td class="text-dim" style="font-size:10px">${_esc(t.notes || '')}</td>
                <td class="tx-id">#${String(t.id).padStart(6,'0')}</td>
              </tr>`).join('')}
            </tbody>
          </table>` : `<div class="empty-state"><span class="empty-icon">◈</span><span>No transactions yet</span></div>`}
        </div>
      </div>`;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

async function submitAddTxn() {
  const symbol = document.getElementById('txn-symbol').value.trim().toUpperCase();
  const action = document.getElementById('txn-action').value;
  const shares = parseFloat(document.getElementById('txn-shares').value);
  const price  = parseFloat(document.getElementById('txn-price').value);
  const fees   = parseFloat(document.getElementById('txn-fees').value) || 0;
  const date   = document.getElementById('txn-date').value;
  const notes  = document.getElementById('txn-notes').value;
  if (!symbol || !shares || !price) { Toast.warn('Symbol, shares, price required'); return; }
  try {
    await API.post('/api/transactions/add', { symbol, action, shares, price, fees, date, notes });
    Toast.success(`${action} ${symbol} logged`);
    Modal.close('modal-add-txn');
    document.getElementById('form-add-txn').reset();
    if (State.activeView === 'transactions') loadTransactions();
  } catch(e) { Toast.error('Failed: ' + e.message); }
}

// ── News View ─────────────────────────────────────────────────────────────────
async function loadGlobalNews() {
  const view = document.getElementById('view-news');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> LOADING MARKET NEWS...</div>`;
  try {
    // Prefer the user's actual equity holdings (cap 8) so the feed is
    // relevant; only fall back to the broad-market defaults if the
    // portfolio is empty.
    const defaultTickers = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA'];
    let tickers = defaultTickers;
    try {
      const holdings = (State.portfolio && State.portfolio.holdings) || [];
      const equitySyms = holdings
        .filter(h => h.asset_type !== 'crypto' && h.symbol)
        .slice(0, 8)
        .map(h => h.symbol);
      if (equitySyms.length) tickers = equitySyms;
    } catch(e) {}
    const newsMap = await Promise.all(tickers.map(t => API.get(`/api/news/${t}?limit=8`)));
    const all = [];
    const seen = new Set();
    newsMap.flat().forEach(n => {
      const key = n.title || n.url;
      if (key && !seen.has(key)) { seen.add(key); all.push(n); }
    });
    all.sort((a, b) => {
      const ta = new Date(a.published).getTime();
      const tb = new Date(b.published).getTime();
      // Undated/unparseable items sort last (treated as oldest).
      return (isNaN(tb) ? -Infinity : tb) - (isNaN(ta) ? -Infinity : ta);
    });

    view.innerHTML = `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">MARKET NEWS FEED</span>
          <button class="btn btn-ghost btn-sm" onclick="loadGlobalNews()">↻ REFRESH</button>
        </div>
        <div style="max-height:calc(100vh - 200px);overflow-y:auto">
          ${all.length ? all.map(n => `
            <div class="news-item" onclick="window.open('${_esc(_jesc(safeUrl(n.url)))}', '_blank')">
              <div class="news-title">${_esc(n.title)}</div>
              <div class="news-summary">${_esc(n.summary ? n.summary.substring(0, 200) + '...' : '')}</div>
              <div class="news-meta">
                <span class="news-source">${_esc(n.source)}</span>
                <span class="news-time">${fmt.timeAgo(n.published)}</span>
              </div>
            </div>`).join('')
          : '<div class="empty-state"><span>No news available</span></div>'}
        </div>
      </div>`;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

// ── Screener View ─────────────────────────────────────────────────────────────
async function loadScreener() {
  const view = document.getElementById('view-screener');
  view.innerHTML = `
    <div class="research-tabs" style="display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:8px">
      <button class="research-tab active" id="sctab-quotes" onclick="switchScreenerMode('quotes')">QUOTE LIST</button>
      <button class="research-tab" id="sctab-universe" onclick="switchScreenerMode('universe')">UNIVERSE (FinanceDatabase)</button>
    </div>

    <div id="scpanel-quotes">
      <div class="panel mb-8">
        <div class="panel-header"><span class="panel-title">STOCK SCREENER — QUOTE LIST</span></div>
        <div class="panel-body">
          <div class="form-row" style="grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:10px">
            <div class="form-group">
              <label class="form-label">SYMBOL / SECTOR</label>
              <input class="form-input" id="sc-symbols" placeholder="AAPL,MSFT,GOOGL" value="AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,BRK-B,JPM,V,MA,UNH,XOM,JNJ,PG">
            </div>
            <div class="form-group">
              <label class="form-label">MIN MARKET CAP</label>
              <select class="form-select" id="sc-mincap">
                <option value="">Any</option>
                <option value="1e9">$1B+</option>
                <option value="10e9">$10B+</option>
                <option value="100e9">$100B+</option>
                <option value="500e9">$500B+</option>
              </select>
            </div>
            <div class="form-group">
              <label class="form-label">SORT BY</label>
              <select class="form-select" id="sc-sort">
                <option value="market_cap">Market Cap</option>
                <option value="change_pct">Day Change%</option>
                <option value="price">Price</option>
              </select>
            </div>
            <div class="form-group">
              <label class="form-label">&nbsp;</label>
              <button class="btn btn-green w-full" onclick="runScreener()">▶ RUN SCREEN</button>
            </div>
          </div>
          <div class="flex gap-4" style="margin-bottom:8px;flex-wrap:wrap">
            ${['Large Cap (>$10B)', 'Mega Cap (>$200B)', 'FAANG', 'Semiconductors', 'Biotech', 'Banks', 'REITs', 'Clean Energy'].map(f =>
              `<button class="filter-chip" onclick="applyScreenerPreset('${f}')">${f}</button>`
            ).join('')}
          </div>
        </div>
      </div>
      <div id="screener-results">
        <div class="empty-state"><span class="empty-icon">◈</span><span>Enter symbols and click RUN SCREEN</span></div>
      </div>
    </div>

    <div id="scpanel-universe" style="display:none">
      <div class="panel mb-8">
        <div class="panel-header"><span class="panel-title">UNIVERSE SCREENER — FinanceDatabase (~160k symbols)</span></div>
        <div class="panel-body">
          <div class="form-row" style="grid-template-columns:repeat(6,1fr);gap:8px;margin-bottom:10px">
            <div class="form-group">
              <label class="form-label">ASSET CLASS</label>
              <select class="form-select" id="su-asset" onchange="loadScreenerFacets()">
                <option value="equities">Equities</option>
                <option value="etfs">ETFs</option>
                <option value="funds">Funds</option>
                <option value="indices">Indices</option>
                <option value="currencies">Currencies</option>
                <option value="cryptos">Cryptos</option>
                <option value="moneymarkets">Money Markets</option>
              </select>
            </div>
            <div class="form-group">
              <label class="form-label">COUNTRY</label>
              <select class="form-select" id="su-country"><option value="">Any</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">SECTOR</label>
              <select class="form-select" id="su-sector"><option value="">Any</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">INDUSTRY</label>
              <select class="form-select" id="su-industry"><option value="">Any</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">EXCHANGE</label>
              <select class="form-select" id="su-exchange"><option value="">Any</option></select>
            </div>
            <div class="form-group">
              <label class="form-label">&nbsp;</label>
              <button class="btn btn-green w-full" onclick="runUniverseScreener()">▶ RUN UNIVERSE</button>
            </div>
          </div>
          <div style="font-size:10px;color:var(--text-dim)">
            Capped at 200 results · Source: <a href="https://github.com/JerBouma/FinanceDatabase" target="_blank" rel="noopener" style="color:var(--blue)">FinanceDatabase</a>
          </div>
        </div>
      </div>
      <div id="universe-results">
        <div class="empty-state"><span class="empty-icon">⊟</span><span>Pick filters and click RUN UNIVERSE</span></div>
      </div>
    </div>
  `;
  // Pre-load facets for the default asset class
  loadScreenerFacets();
}

function switchScreenerMode(mode) {
  ['quotes','universe'].forEach(m => {
    const btn = document.getElementById('sctab-' + m);
    const panel = document.getElementById('scpanel-' + m);
    if (btn) btn.classList.toggle('active', m === mode);
    if (panel) panel.style.display = m === mode ? '' : 'none';
  });
}

async function loadScreenerFacets() {
  const asset = document.getElementById('su-asset')?.value || 'equities';
  const setSel = (id, items) => {
    const el = document.getElementById(id);
    if (!el) return;
    const cur = el.value;
    el.innerHTML = '<option value="">Any</option>' +
      items.map(v => `<option value="${_esc(v)}">${_esc(v)}</option>`).join('');
    if (cur && items.includes(cur)) el.value = cur;
  };
  // Clear dependent selects while loading
  setSel('su-country', []); setSel('su-sector', []); setSel('su-industry', []); setSel('su-exchange', []);
  try {
    const f = await API.get(`/api/screener/facets?asset=${encodeURIComponent(asset)}`);
    setSel('su-country',  f.countries  || []);
    setSel('su-sector',   f.sectors    || []);
    setSel('su-industry', f.industries || []);
    setSel('su-exchange', f.exchanges  || []);
  } catch(e) {
    Toast.warn(`Facets failed: ${e.message}`);
  }
}

async function runUniverseScreener() {
  const asset    = document.getElementById('su-asset').value;
  const country  = document.getElementById('su-country').value;
  const sector   = document.getElementById('su-sector').value;
  const industry = document.getElementById('su-industry').value;
  const exchange = document.getElementById('su-exchange').value;
  const params = new URLSearchParams({ asset, limit: '200' });
  if (country)  params.set('country',  country);
  if (sector)   params.set('sector',   sector);
  if (industry) params.set('industry', industry);
  if (exchange) params.set('exchange', exchange);

  const out = document.getElementById('universe-results');
  out.innerHTML = `<div class="loading"><div class="spinner"></div> QUERYING UNIVERSE…</div>`;
  try {
    const data = await API.get(`/api/screener/universe?${params.toString()}`);
    const rows = (data.results || []).map(r => `<tr>
      <td><span class="col-symbol" onclick="openResearch('${_esc(r.symbol)}')">${_esc(r.symbol)}</span></td>
      <td class="col-name">${_esc((r.name||'').slice(0,42))}</td>
      <td style="font-size:10px">${_esc(r.sector || '—')}</td>
      <td style="font-size:10px;color:var(--text-secondary)">${_esc(r.industry || '—')}</td>
      <td style="font-size:10px">${_esc(r.exchange || '—')}</td>
      <td style="font-size:10px;color:var(--text-dim)">${_esc(r.country || '—')}</td>
      <td style="font-size:10px">${_esc(r.market_cap || '—')}</td>
      <td class="col-actions">
        <button class="btn btn-green btn-sm" onclick="quickAddToPortfolio('${_esc(r.symbol)}', '')">+PORT</button>
        <button class="btn btn-blue btn-sm"  onclick="quickAddToWatchlist('${_esc(r.symbol)}', '')">+WATCH</button>
      </td>
    </tr>`).join('');
    out.innerHTML = `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">UNIVERSE RESULTS — ${data.count || 0} matches</span><span class="panel-meta">${asset}</span></div>
        <div style="overflow-x:auto;max-height:calc(100vh - 280px);overflow-y:auto">
          <table class="data-table">
            <thead><tr>
              <th>SYMBOL</th><th>NAME</th><th>SECTOR</th><th>INDUSTRY</th>
              <th>EXCHANGE</th><th>COUNTRY</th><th>CAP</th><th></th>
            </tr></thead>
            <tbody>${rows || '<tr><td colspan="8" class="empty-state">No matches — relax filters</td></tr>'}</tbody>
          </table>
        </div>
      </div>`;
  } catch(e) {
    out.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

const SCREENER_PRESETS = {
  'Large Cap (>$10B)':  'AAPL,MSFT,GOOGL,AMZN,NVDA,META,TSLA,BRK-B,JPM,V,MA,UNH,XOM,JNJ,PG,HD,LLY,AVGO,MRK,ABBV',
  'Mega Cap (>$200B)':  'AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,BRK-B,JPM,V',
  'FAANG':              'META,AAPL,AMZN,NFLX,GOOGL',
  'Semiconductors':     'NVDA,AMD,INTC,AVGO,QCOM,MU,TSM,AMAT,LRCX,KLAC',
  'Biotech':            'MRNA,BNTX,GILD,REGN,VRTX,BIIB,ALNY,BMRN,SRPT,RARE',
  'Banks':              'JPM,BAC,WFC,C,GS,MS,USB,PNC,TFC,SCHW',
  'REITs':              'AMT,PLD,CCI,EQIX,PSA,SPG,O,WELL,DLR,AVB',
  'Clean Energy':       'ENPH,FSLR,SEDG,RUN,NEE,CEG,VST,BE,PLUG,SPWR',
};

function applyScreenerPreset(name) {
  const syms = SCREENER_PRESETS[name] || '';
  document.getElementById('sc-symbols').value = syms;
  runScreener();
}

async function runScreener() {
  const symbolsRaw = document.getElementById('sc-symbols').value;
  const minCap = parseFloat(document.getElementById('sc-mincap').value) || 0;
  const sortBy = document.getElementById('sc-sort').value;
  const symbols = symbolsRaw.split(',').map(s => s.trim()).filter(Boolean);
  if (!symbols.length) { Toast.warn('Enter at least one symbol'); return; }

  const results = document.getElementById('screener-results');
  results.innerHTML = `<div class="loading"><div class="spinner"></div> SCREENING ${symbols.length} SYMBOLS...</div>`;

  try {
    const data = await API.get(`/api/quotes?symbols=${symbols.join(',')}`);
    let rows = Object.values(data).filter(q => !q.error);
    if (minCap) rows = rows.filter(q => (q.market_cap || 0) >= minCap);
    rows.sort((a, b) => {
      if (sortBy === 'market_cap') return (b.market_cap || 0) - (a.market_cap || 0);
      if (sortBy === 'change_pct') return (b.change_pct ?? -999) - (a.change_pct ?? -999);
      if (sortBy === 'price') return (b.price || 0) - (a.price || 0);
      return 0;
    });

    results.innerHTML = `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">RESULTS — ${rows.length} MATCHES</span></div>
        <div style="overflow-x:auto">
          <table class="data-table">
            <thead><tr>
              <th>SYMBOL</th>
              <th style="text-align:right">PRICE</th>
              <th style="text-align:right">CHANGE</th>
              <th style="text-align:right">CHANGE%</th>
              <th style="text-align:right">DAY HIGH</th>
              <th style="text-align:right">DAY LOW</th>
              <th style="text-align:right">MKT CAP</th>
              <th></th>
            </tr></thead>
            <tbody>
              ${rows.map(q => `<tr>
                <td><span class="col-symbol" onclick="openResearch('${_esc(q.symbol)}')">${_esc(q.symbol)}</span></td>
                <td class="col-price">$${fmt.price(q.price)}</td>
                <td class="${col.pnl(q.change)}">${q.change >= 0 ? '+' : ''}$${fmt.price(q.change)}</td>
                <td class="${col.pct(q.change_pct)}">${fmt.pct(q.change_pct)}</td>
                <td class="col-number">$${fmt.price(q.day_high)}</td>
                <td class="col-number">$${fmt.price(q.day_low)}</td>
                <td class="col-number">${fmt.compact(q.market_cap)}</td>
                <td class="col-actions">
                  <button class="btn btn-green btn-sm" onclick="quickAddToPortfolio('${_esc(q.symbol)}', '')">+PORT</button>
                  <button class="btn btn-blue btn-sm"  onclick="quickAddToWatchlist('${_esc(q.symbol)}', '')">+WATCH</button>
                </td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>`;
  } catch(e) {
    results.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

// ── Price Alerts ──────────────────────────────────────────────────────────────

async function loadAlertsView() {
  const view = document.getElementById('view-alerts');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> LOADING ALERTS...</div>`;
  try {
    const alerts = await API.get('/api/alerts?include_triggered=true');
    const active    = alerts.filter(a => !a.triggered);
    const triggered = alerts.filter(a =>  a.triggered);

    view.innerHTML = `
      <div class="flex-between mb-8">
        <div>
          <h2 style="font-size:14px;letter-spacing:.12em;color:var(--green);margin:0">◉ PRICE ALERTS</h2>
          <div style="font-size:10px;color:var(--text-dim);margin-top:3px">${active.length} ACTIVE · ${triggered.length} TRIGGERED</div>
        </div>
        <div class="flex gap-8">
          <button class="btn btn-green btn-sm" onclick="openAddAlertModal()">+ ADD ALERT</button>
          ${triggered.length ? `<button class="btn btn-ghost btn-sm" onclick="clearTriggeredAlerts()">CLEAR TRIGGERED</button>` : ''}
        </div>
      </div>

      ${active.length ? `
      <div class="panel" style="margin-bottom:8px">
        <div class="panel-header"><span class="panel-title">ACTIVE ALERTS</span></div>
        <div class="panel-body" style="padding:0">
          <table class="data-table">
            <thead><tr><th>SYMBOL</th><th>TYPE</th><th style="text-align:right">TARGET</th><th style="text-align:right">CURRENT</th><th style="text-align:right">DISTANCE</th><th>CREATED</th><th></th></tr></thead>
            <tbody>
              ${active.map(a => {
                const atTarget = a.distance_pct != null && Math.abs(a.distance_pct) < 1;
                const distColor = a.distance_pct == null ? 'var(--text-dim)' :
                  atTarget ? 'var(--amber)' :
                  (a.alert_type === 'above' && a.distance_pct < 0) ? 'var(--green)' :
                  (a.alert_type === 'below' && a.distance_pct > 0) ? 'var(--green)' : 'var(--text-dim)';
                return `<tr>
                  <td><span class="col-symbol" onclick="openResearch('${_esc(a.symbol)}')">${_esc(a.symbol)}</span></td>
                  <td><span class="badge ${a.alert_type === 'above' ? 'badge-green' : 'badge-red'}">${_esc(a.alert_type.toUpperCase())}</span></td>
                  <td class="col-number">$${fmt.price(a.price)}</td>
                  <td class="col-number">${a.current_price ? '$' + fmt.price(a.current_price) : '—'}</td>
                  <td class="col-number" style="color:${distColor}">${a.distance_pct != null ? (a.distance_pct >= 0 ? '+' : '') + a.distance_pct.toFixed(2) + '%' : '—'}</td>
                  <td style="font-size:10px;color:var(--text-dim)">${fmt.date(a.created_at?.slice(0,10))}</td>
                  <td><button class="btn btn-red btn-sm" onclick="deleteAlert(${a.id})">✕</button></td>
                </tr>`;
              }).join('')}
            </tbody>
          </table>
        </div>
      </div>` : `<div class="empty-state" style="margin-bottom:8px"><span class="empty-icon">◉</span><span>No active alerts. Click + ADD ALERT to set price targets.</span></div>`}

      ${triggered.length ? `
      <div class="panel">
        <div class="panel-header"><span class="panel-title" style="color:var(--amber)">⚡ TRIGGERED ALERTS</span></div>
        <div class="panel-body" style="padding:0">
          <table class="data-table">
            <thead><tr><th>SYMBOL</th><th>TYPE</th><th style="text-align:right">TARGET</th><th style="text-align:right">TRIGGERED AT</th></tr></thead>
            <tbody>
              ${triggered.map(a => `<tr style="opacity:.7">
                <td><span class="col-symbol">${_esc(a.symbol)}</span></td>
                <td><span class="badge badge-dim">${_esc(a.alert_type.toUpperCase())}</span></td>
                <td class="col-number">$${fmt.price(a.price)}</td>
                <td style="font-size:10px;color:var(--amber)">${fmt.date(a.triggered_at?.slice(0,10))}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>
      </div>` : ''}

      <!-- Add Alert Modal (inline) -->
      <div id="add-alert-inline" style="display:none;margin-top:8px">
        <div class="panel" style="border-color:var(--green-dim)">
          <div class="panel-header"><span class="panel-title">NEW PRICE ALERT</span></div>
          <div class="panel-body">
            <div class="form-row">
              <div class="form-group">
                <label class="form-label">SYMBOL</label>
                <input class="form-input" id="alert-symbol" placeholder="AAPL" style="text-transform:uppercase">
              </div>
              <div class="form-group">
                <label class="form-label">TYPE</label>
                <select class="form-select" id="alert-type">
                  <option value="above">PRICE ABOVE (breakout)</option>
                  <option value="below">PRICE BELOW (stop-loss)</option>
                </select>
              </div>
              <div class="form-group">
                <label class="form-label">TARGET PRICE ($)</label>
                <input class="form-input" id="alert-price" type="number" step="0.01" placeholder="0.00">
              </div>
            </div>
            <div class="flex gap-8" style="margin-top:8px">
              <button class="btn btn-green" onclick="submitAddAlert()">CREATE ALERT</button>
              <button class="btn btn-ghost" onclick="document.getElementById('add-alert-inline').style.display='none'">CANCEL</button>
            </div>
          </div>
        </div>
      </div>
    `;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

function openAddAlertModal() {
  const el = document.getElementById('add-alert-inline');
  if (el) { el.style.display = el.style.display === 'none' ? 'block' : 'none'; }
}

async function submitAddAlert() {
  const symbol = document.getElementById('alert-symbol').value.trim().toUpperCase();
  const type   = document.getElementById('alert-type').value;
  const price  = parseFloat(document.getElementById('alert-price').value);
  if (!symbol || !price) { Toast.warn('Symbol and price required'); return; }
  try {
    await API.post('/api/alerts', { symbol, alert_type: type, price });
    Toast.success(`Alert set: ${symbol} ${type} $${price}`);
    loadAlertsView();
  } catch(e) { Toast.error('Failed to create alert'); }
}

async function deleteAlert(id) {
  try {
    await API.del(`/api/alerts/${id}`);
    Toast.success('Alert deleted');
    loadAlertsView();
  } catch(e) { Toast.error('Failed to delete'); }
}

async function clearTriggeredAlerts() {
  try {
    await API.post('/api/alerts/clear-triggered', {});
    Toast.success('Triggered alerts cleared');
    loadAlertsView();
  } catch(e) { Toast.error('Failed to clear'); }
}

// Poll triggered alerts: update sidebar badge AND toast newly-fired alerts.
// We persist the IDs we've already shown in localStorage so a page refresh
// doesn't re-toast the same alerts.
const ALERT_SEEN_KEY = 'augur.alerts.seenTriggered';

function _loadSeenAlertIds() {
  try {
    const raw = localStorage.getItem(ALERT_SEEN_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch (e) { return new Set(); }
}
function _saveSeenAlertIds(set) {
  try { localStorage.setItem(ALERT_SEEN_KEY, JSON.stringify([...set])); } catch (e) {}
}

async function _pollTriggeredAlerts() {
  try {
    const triggered = await API.get('/api/alerts/triggered');
    const active = triggered.filter(a => a.triggered);

    const badge = document.getElementById('alert-nav-count');
    if (badge) {
      if (active.length) {
        badge.textContent = active.length;
        badge.style.display = 'inline-block';
      } else {
        badge.style.display = 'none';
      }
    }

    // Toast any alert ID we haven't seen before (delta tracking).
    const seen = _loadSeenAlertIds();
    let dirty = false;
    for (const a of active) {
      if (a.id == null || seen.has(a.id)) continue;
      const dir = a.alert_type === 'above' ? '↑' : '↓';
      const cur = a.current_price != null ? '$' + fmt.price(a.current_price) : '—';
      Toast.warn(`${dir} ALERT ${a.symbol} @ ${cur} (target $${fmt.price(a.price)})`);
      seen.add(a.id);
      dirty = true;
    }
    // Forget IDs that no longer exist (alert was deleted/cleared) so the
    // localStorage doesn't grow forever.
    if (dirty || seen.size > active.length * 2) {
      const live = new Set(active.map(a => a.id));
      const pruned = new Set([...seen].filter(id => live.has(id)));
      _saveSeenAlertIds(pruned);
    }
  } catch(e) {}
}

let _alertPollTimer = null;
function _startAlertPollTimer() {
  if (_alertPollTimer) clearInterval(_alertPollTimer);
  const sec = parseInt((State.settings && State.settings.refresh_interval) || 60, 10);
  const ms = Math.max(sec * 1000, 15000); // never poll faster than 15s
  // Skip while the tab is hidden; the visibilitychange handler below
  // catches up the moment the user comes back.
  _alertPollTimer = setInterval(() => { if (!document.hidden) _pollTriggeredAlerts(); }, ms);
}
_startAlertPollTimer();
// Run once on load so the user sees pending alerts immediately
setTimeout(_pollTriggeredAlerts, 2000);

// Page Visibility catch-up: the hidden-tab guards above skip refreshes,
// so when the tab becomes visible again refresh ticker + alert badge
// immediately instead of leaving them stale until the next interval.
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  try { loadTicker(); } catch(e) {}
  try { _pollTriggeredAlerts(); } catch(e) {}
});

// Restart all user-configurable timers so a settings change takes
// effect immediately (no page reload needed).
function restartTimers() {
  _startAlertPollTimer();
  if (typeof MacroBar !== 'undefined' && MacroBar.restart) MacroBar.restart();
  if (typeof startAutoRefresh === 'function') startAutoRefresh();
}


// ── Dividend Income Tracker ────────────────────────────────────────────────────

async function loadDividendsView() {
  const view = document.getElementById('view-dividends');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> FETCHING DIVIDEND DATA...</div>`;
  try {
    const data = await API.get('/api/dividends/portfolio');
    const { positions = [], summary = {} } = data || {};  // tolerate an error/empty shape

    if (!positions.length) {
      view.innerHTML = `<div class="empty-state"><span class="empty-icon">⊛</span><span>No stock positions found. Add stocks to track dividend income.</span></div>`;
      return;
    }

    const divPositions = positions.filter(p => p.div_rate);
    const noDivPositions = positions.filter(p => !p.div_rate);

    view.innerHTML = `
      <div class="flex-between mb-8">
        <h2 style="font-size:14px;letter-spacing:.12em;color:var(--green);margin:0">⊛ DIVIDEND INCOME TRACKER</h2>
        <button class="btn btn-ghost btn-sm" onclick="loadDividendsView()">↻ REFRESH</button>
      </div>

      <!-- KPI row -->
      <div class="flex gap-8 mb-8" style="flex-wrap:wrap">
        <div class="kpi-card green" style="padding:8px 16px;min-width:140px">
          <div class="kpi-label">ANNUAL INCOME</div>
          <div class="kpi-value" style="font-size:20px">$${fmt.currency(summary.total_annual_income)}</div>
        </div>
        <div class="kpi-card" style="padding:8px 16px;min-width:140px">
          <div class="kpi-label">MONTHLY AVG</div>
          <div class="kpi-value" style="font-size:20px">$${fmt.currency(summary.monthly_income)}</div>
        </div>
        <div class="kpi-card" style="padding:8px 16px;min-width:130px">
          <div class="kpi-label">PORTFOLIO YIELD</div>
          <div class="kpi-value" style="font-size:20px">${fmt.num(summary.portfolio_yield)}%</div>
        </div>
        <div class="kpi-card" style="padding:8px 16px;min-width:130px">
          <div class="kpi-label">DIVIDEND PAYERS</div>
          <div class="kpi-value" style="font-size:20px">${summary.dividend_positions} / ${summary.total_positions}</div>
        </div>
      </div>

      <div class="panel-grid grid-2" style="margin-bottom:8px">
        <!-- Dividend positions table -->
        <div class="panel" style="grid-column:1/-1">
          <div class="panel-header"><span class="panel-title">DIVIDEND POSITIONS</span></div>
          <div class="panel-body" style="padding:0;overflow-x:auto">
            <table class="data-table" style="min-width:800px">
              <thead><tr>
                <th>SYMBOL</th><th>NAME</th>
                <th style="text-align:right">YIELD</th>
                <th style="text-align:right">YIELD ON COST</th>
                <th style="text-align:right">ANNUAL RATE</th>
                <th style="text-align:right">ANNUAL INCOME</th>
                <th style="text-align:right">INCOME WEIGHT</th>
                <th>FREQUENCY</th>
                <th>EX-DATE</th>
                <th>5Y DIV GROWTH</th>
              </tr></thead>
              <tbody>
                ${divPositions.map(p => {
                  const yieldColor = (p.div_yield || 0) > 4 ? 'var(--green)' : (p.div_yield || 0) > 1.5 ? 'var(--amber)' : 'var(--text-secondary)';
                  const yocColor   = (p.yield_on_cost || 0) > (p.div_yield || 0) ? 'var(--green)' : 'var(--text-secondary)';
                  return `<tr>
                    <td><span class="col-symbol" onclick="openResearch('${_esc(p.symbol)}')">${_esc(p.symbol)}</span></td>
                    <td class="col-name truncate" style="max-width:120px">${_esc(p.name || '—')}</td>
                    <td class="col-number" style="color:${yieldColor}">${p.div_yield != null ? p.div_yield.toFixed(2) + '%' : '—'}</td>
                    <td class="col-number" style="color:${yocColor}">${p.yield_on_cost != null ? p.yield_on_cost.toFixed(2) + '%' : '—'}</td>
                    <td class="col-number">$${p.div_rate ? p.div_rate.toFixed(4) : '—'}</td>
                    <td class="col-number col-green">$${fmt.currency(p.annual_income)}</td>
                    <td class="col-number">${p.income_weight != null ? p.income_weight.toFixed(1) + '%' : '—'}</td>
                    <td style="font-size:10px;color:var(--text-dim)">${_esc(p.frequency || '—')}</td>
                    <td style="font-size:10px;color:${p.ex_date ? 'var(--amber)' : 'var(--text-dim)'}">${_esc(p.ex_date || '—')}</td>
                    <td class="${(p.div_growth_5y || 0) > 0 ? 'col-green' : 'col-red'}">${p.div_growth_5y != null ? (p.div_growth_5y >= 0 ? '+' : '') + p.div_growth_5y.toFixed(1) + '%/yr' : '—'}</td>
                  </tr>`;
                }).join('')}
                ${noDivPositions.map(p => `<tr style="opacity:.4">
                  <td><span class="col-symbol">${_esc(p.symbol)}</span></td>
                  <td class="col-name truncate" style="max-width:120px">${_esc(p.name || '—')}</td>
                  <td colspan="8" style="font-size:10px;color:var(--text-dim)">No dividend</td>
                </tr>`).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- Upcoming ex-dates -->
      ${divPositions.filter(p => p.ex_date).length ? `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">UPCOMING EX-DIVIDEND DATES</span></div>
        <div class="panel-body">
          ${divPositions.filter(p => p.ex_date).sort((a,b) => a.ex_date > b.ex_date ? 1 : -1).map(p => {
            const daysUntil = Math.round((new Date(p.ex_date + 'T00:00:00') - new Date()) / 86400000);
            const urgColor = daysUntil <= 7 ? 'var(--red)' : daysUntil <= 30 ? 'var(--amber)' : 'var(--text-dim)';
            return `<div style="display:flex;align-items:center;gap:16px;padding:6px 0;border-bottom:1px solid var(--border)">
              <span class="col-symbol" style="min-width:60px" onclick="openResearch('${_jesc(p.symbol)}')">${_esc(p.symbol)}</span>
              <span style="font-size:11px;color:var(--text-secondary);min-width:100px">${fmt.date(p.ex_date)}</span>
              <span style="font-size:10px;color:${urgColor}">${daysUntil <= 0 ? 'PASSED' : daysUntil + 'd away'}</span>
              ${(() => {
                if (!p.div_rate) return `<span style="font-size:10px;color:var(--green)">$— / share</span>`;
                // Normalize the frequency label (case/whitespace/synonym tolerant) before lookup.
                const freqMap = { monthly: 12, quarterly: 4, 'semi-annual': 2, semiannual: 2, 'semi annual': 2, biannual: 2, annual: 1, yearly: 1 };
                const freqKey = String(p.frequency || '').trim().toLowerCase();
                const known = freqMap[freqKey] != null;
                const divisor = known ? freqMap[freqKey] : 4; // unknown/null/Irregular → quarterly
                const perShare = (p.div_rate / divisor).toFixed(4);
                const note = known ? '' : ' <small style="color:var(--text-dim)">(approx, freq unknown)</small>';
                return `<span style="font-size:10px;color:var(--green)">$${perShare} / share${note}</span>`;
              })()}
              ${p.pay_date ? `<span style="font-size:10px;color:var(--text-dim)">pays ${fmt.date(p.pay_date)}</span>` : ''}
            </div>`;
          }).join('')}
        </div>
      </div>` : ''}
    `;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}


// ── Macro Conditions Dashboard ─────────────────────────────────────────────────

async function loadMacroView() {
  const view = document.getElementById('view-macro');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> FETCHING MACRO DATA...</div>`;
  try {
    const m = await API.get('/api/macro');

    const macroCard = (label, value, unit, chgPct, colorOverride, note) => {
      const isPos = chgPct > 0;
      const chgColor = colorOverride || (isPos ? 'var(--green)' : 'var(--red)');
      const valStr = value != null ? value.toFixed(unit === '%' ? 2 : unit === 'pts' ? 0 : 2) + (unit !== '$' ? unit : '') : '—';
      return `
        <div class="macro-card">
          <div class="macro-card-label">${label}</div>
          <div class="macro-card-value">${unit === '$' ? '$' : ''}${valStr}</div>
          ${chgPct != null ? `<div class="macro-card-chg" style="color:${chgColor}">${chgPct >= 0 ? '+' : ''}${chgPct.toFixed(2)}%</div>` : '<div class="macro-card-chg" style="color:var(--text-dim)">—</div>'}
          ${note ? `<div class="macro-card-note">${note}</div>` : ''}
        </div>`;
    };

    // VIX interpretation
    const vix = m.vix?.value;
    const vixNote = vix == null ? '' : vix > 30 ? 'FEAR' : vix > 20 ? 'ELEVATED' : 'CALM';
    const vixColor = vix == null ? null : vix > 30 ? 'var(--red)' : vix > 20 ? 'var(--amber)' : 'var(--green)';

    // Yield curve
    const spread = m.yield_curve_spread;
    const spreadColor = spread == null ? 'var(--text-dim)' : spread < 0 ? 'var(--red)' : spread < 0.5 ? 'var(--amber)' : 'var(--green)';
    const spreadNote = spread == null ? '' : spread < 0 ? '⚠ INVERTED' : spread < 0.5 ? 'FLAT' : 'NORMAL';

    // Dollar strength
    const dxy = m.dxy?.value;
    const dxyNote = dxy == null ? '' : dxy > 105 ? 'STRONG $' : dxy > 95 ? 'NEUTRAL' : 'WEAK $';

    view.innerHTML = `
      <div class="flex-between mb-8">
        <div>
          <h2 style="font-size:14px;letter-spacing:.12em;color:var(--green);margin:0">⊕ MACRO CONDITIONS DASHBOARD</h2>
          <div style="font-size:10px;color:var(--text-dim);margin-top:3px">GLOBAL MACRO INDICATORS</div>
        </div>
        <button class="btn btn-ghost btn-sm" onclick="loadMacroView()">↻ REFRESH</button>
      </div>

      <!-- Fear & Greed row -->
      <div class="macro-section-label">VOLATILITY & SENTIMENT</div>
      <div class="macro-grid">
        ${macroCard('VIX FEAR INDEX', vix, 'pts', m.vix?.change_pct, vixColor, vixNote)}
        ${macroCard('S&P 500', m.sp500?.value, '', m.sp500?.change_pct, null, '')}
        ${macroCard('NASDAQ', m.nasdaq?.value, '', m.nasdaq?.change_pct, null, '')}
      </div>

      <!-- Rates row -->
      <div class="macro-section-label">INTEREST RATES</div>
      <div class="macro-grid">
        ${macroCard('2Y TREASURY', m.yield_2y?.value, '%', m.yield_2y?.change_pct, m.yield_2y?.change_pct > 0 ? 'var(--red)' : 'var(--green)', '')}
        ${macroCard('10Y TREASURY', m.yield_10y?.value, '%', m.yield_10y?.change_pct, m.yield_10y?.change_pct > 0 ? 'var(--red)' : 'var(--green)', '')}
        ${macroCard('30Y TREASURY', m.yield_30y?.value, '%', m.yield_30y?.change_pct, m.yield_30y?.change_pct > 0 ? 'var(--red)' : 'var(--green)', '')}
        <div class="macro-card" style="border-color:${spreadColor}">
          <div class="macro-card-label">10Y–2Y SPREAD</div>
          <div class="macro-card-value" style="color:${spreadColor}">${spread != null ? (spread >= 0 ? '+' : '') + spread.toFixed(3) + '%' : '—'}</div>
          <div class="macro-card-chg" style="color:${spreadColor}">${spreadNote}</div>
          <div class="macro-card-note">Yield curve shape</div>
        </div>
      </div>

      <!-- Commodities & FX -->
      <div class="macro-section-label">COMMODITIES & FX</div>
      <div class="macro-grid">
        ${macroCard('US DOLLAR (DXY)', m.dxy?.value, '', m.dxy?.change_pct, m.dxy?.change_pct > 0 ? 'var(--green)' : 'var(--red)', dxyNote)}
        ${macroCard('CRUDE OIL (WTI)', m.crude_oil?.value, '$', m.crude_oil?.change_pct, null, '')}
        ${macroCard('GOLD', m.gold?.value, '$', m.gold?.change_pct, null, '')}
        ${macroCard('BITCOIN', m.btc?.value, '$', m.btc?.change_pct, null, '')}
      </div>

      <!-- Macro interpretation -->
      <div class="panel" style="margin-top:8px">
        <div class="panel-header"><span class="panel-title">MARKET REGIME ASSESSMENT</span></div>
        <div class="panel-body">
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px">
            ${[
              {
                label: 'RISK ENVIRONMENT',
                value: vix == null ? '—' : vix > 30 ? 'RISK-OFF' : vix > 20 ? 'CAUTIOUS' : 'RISK-ON',
                color: vix == null ? 'var(--text-dim)' : vix > 30 ? 'var(--red)' : vix > 20 ? 'var(--amber)' : 'var(--green)',
                note: vix == null ? '' : `VIX at ${vix?.toFixed(1)} — ${vixNote}`,
              },
              {
                label: 'YIELD CURVE SIGNAL',
                value: spread == null ? '—' : spread < 0 ? 'INVERTED ⚠' : spread < 0.5 ? 'FLAT' : 'NORMAL',
                color: spreadColor,
                note: spread != null ? `${(spread >= 0 ? '+' : '')}${spread?.toFixed(3)}% 10Y–2Y spread` : '',
              },
              {
                label: 'DOLLAR REGIME',
                value: dxyNote || '—',
                color: dxy == null ? 'var(--text-dim)' : dxy > 105 ? 'var(--red)' : dxy > 95 ? 'var(--amber)' : 'var(--green)',
                note: dxy != null ? `DXY at ${dxy?.toFixed(2)}` : '',
              },
              {
                label: 'COMMODITY TREND',
                value: m.crude_oil?.change_pct == null ? '—' : m.crude_oil.change_pct > 1 ? 'RISING OIL' : m.crude_oil.change_pct < -1 ? 'FALLING OIL' : 'STABLE',
                color: m.crude_oil?.change_pct == null ? 'var(--text-dim)' : m.crude_oil.change_pct > 0 ? 'var(--amber)' : 'var(--green)',
                note: m.crude_oil?.value != null ? `WTI at $${m.crude_oil.value?.toFixed(2)}` : '',
              },
            ].map(item => `
              <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
                <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:6px">${_esc(item.label)}</div>
                <div style="font-size:15px;font-weight:bold;color:${item.color}">${item.value}</div>
                <div style="font-size:10px;color:var(--text-dim);margin-top:4px">${_esc(item.note)}</div>
              </div>`).join('')}
          </div>
        </div>
      </div>
    `;

    // Treasury yield curve + CBOE VIX (no-key Tier 1)
    DataPanels.appendTreasuryPanel(view);
    // FRED economic indicators — CPI, unemployment, fed funds, M2, GDP, etc.
    DataPanels.appendFredSnapshot(view);
    // CFTC Commitments of Traders — weekly futures positioning
    DataPanels.appendCftcSnapshot(view);
    // Cross-Asset Macro Translator (v0.3.0) — release impact → portfolio
    if (typeof window.renderMacroTranslate === 'function') {
      const mtSection = document.createElement('section');
      mtSection.id = 'macro-translate';
      mtSection.className = 'card';
      mtSection.style.marginTop = '8px';
      const mtHost = document.createElement('div');
      mtHost.id = 'macro-translate-host';
      mtSection.appendChild(mtHost);
      view.appendChild(mtSection);
      try { window.renderMacroTranslate(mtHost, 'CPIAUCSL', window._lastPortfolioHoldings || []); }
      catch(e) { mtHost.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}


// ── Portfolio Stress Test ──────────────────────────────────────────────────────

async function loadStressTestView() {
  const view = document.getElementById('view-stress');
  view.innerHTML = `
    <div class="flex-between mb-8">
      <div>
        <h2 style="font-size:14px;letter-spacing:.12em;color:var(--green);margin:0">⚠ PORTFOLIO STRESS TEST</h2>
        <div style="font-size:10px;color:var(--text-dim);margin-top:3px">HISTORICAL SCENARIO ANALYSIS — SECTOR-ADJUSTED DRAWDOWNS</div>
      </div>
      <div class="flex gap-8 align-center">
        <div class="flex gap-4 align-center">
          <span style="font-size:10px;color:var(--text-dim)">CUSTOM DROP:</span>
          <input type="number" id="stress-custom-drop" class="form-input" style="width:80px;height:28px;font-size:11px" placeholder="-20" value="-20">
          <span style="font-size:10px;color:var(--text-dim)">%</span>
        </div>
        <button class="btn btn-amber btn-sm" id="stress-run-btn" onclick="runStressTest()">▶ RUN STRESS TEST</button>
      </div>
    </div>
    <div id="stress-results">
      <div class="empty-state">
        <span class="empty-icon">⚠</span>
        <span>Click <strong style="color:var(--amber)">RUN STRESS TEST</strong> to analyze your portfolio against historical crash scenarios.</span>
        <div style="margin-top:8px;font-size:11px;color:var(--text-dim)">Uses beta-adjusted sector drawdowns from 2008 GFC, 2020 COVID, 2022 Rate Hike Bear, and 2000 Dot-com Bust.</div>
      </div>
    </div>`;
}

async function runStressTest() {
  const btn = document.getElementById('stress-run-btn');
  const resultsDiv = document.getElementById('stress-results');
  // Note: `parseFloat(...) || -20` would treat 0 as "missing" and substitute
  // -20, hiding a perfectly valid stress test. Use Number.isFinite to
  // distinguish a real 0 from blank/NaN input.
  const _rawDrop = parseFloat(document.getElementById('stress-custom-drop').value);
  const customDrop = Number.isFinite(_rawDrop) ? _rawDrop : -20;
  if (!btn || !resultsDiv) return;

  btn.disabled = true;
  btn.textContent = '◈ ANALYZING...';
  resultsDiv.innerHTML = `<div style="display:flex;align-items:center;gap:10px;padding:20px;color:var(--amber)"><div class="spinner"></div><span style="font-size:11px">Fetching beta and sector data for all positions…</span></div>`;

  try {
    const data = await API.post('/api/stress-test', { custom_drop_pct: customDrop });
    renderStressResults(data);
  } catch(e) {
    resultsDiv.innerHTML = `<div class="text-red" style="padding:12px;font-size:11px">Stress test failed: ${_esc(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '↻ RE-RUN';
  }
}

// Module-scoped cache so the per-card click handler can look the scenario up by
// name instead of round-tripping the whole object through an HTML attribute.
let _lastStressScenarios = {};
function renderStressResults(data) {
  const div = document.getElementById('stress-results');
  if (!div) return;
  const current = data.current_total_value;
  const scenarios = data.scenarios;
  _lastStressScenarios = scenarios || {};

  const scenarioOrder = ['2008 Financial Crisis', '2020 COVID Crash', '2022 Rate Hike Bear', '2000 Dot-com Bust', 'Custom Scenario'];

  div.innerHTML = `
    <!-- Scenario summary cards -->
    <div class="stress-scenario-grid" style="margin-bottom:16px">
      ${scenarioOrder.filter(n => scenarios[n]).map(name => {
        const s = scenarios[name];
        const lossColor = s.total_loss_pct < -30 ? 'var(--red)' : s.total_loss_pct < -15 ? 'var(--amber)' : 'var(--text-secondary)';
        const isCustom = name === 'Custom Scenario';
        return `
          <div class="stress-card ${isCustom ? 'stress-card-custom' : ''}" onclick="showScenarioDetail('${_esc(_jesc(name))}')">
            <div style="font-size:10px;color:${isCustom ? 'var(--amber)' : 'var(--blue)'};letter-spacing:.08em;margin-bottom:6px">${name.toUpperCase()}</div>
            <div style="font-size:11px;color:var(--text-dim);margin-bottom:10px;line-height:1.5">${_esc(s.description)}</div>
            <div style="font-size:22px;font-weight:bold;color:${lossColor}">${s.total_loss_pct.toFixed(1)}%</div>
            <div style="font-size:11px;color:var(--text-dim)">portfolio drawdown</div>
            <div style="margin-top:8px;display:flex;justify-content:space-between;font-size:11px">
              <span style="color:var(--text-dim)">$${fmt.currency(current)} → <span style="color:${lossColor}">$${fmt.currency(s.new_portfolio_value)}</span></span>
            </div>
            <div style="margin-top:6px;font-size:9px;color:var(--text-dim)">▸ CLICK FOR POSITION BREAKDOWN</div>
          </div>`;
      }).join('')}
    </div>

    <!-- Summary comparison bar chart (text-based) -->
    <div class="panel" style="margin-bottom:8px">
      <div class="panel-header"><span class="panel-title">SCENARIO COMPARISON</span></div>
      <div class="panel-body">
        ${scenarioOrder.filter(n => scenarios[n]).map(name => {
          const s = scenarios[name];
          const pct = Math.abs(s.total_loss_pct);
          const barWidth = Math.min(100, pct * 1.5);
          const barColor = pct > 30 ? 'var(--red)' : pct > 15 ? 'var(--amber)' : 'var(--text-dim)';
          return `
            <div style="display:flex;align-items:center;gap:10px;padding:5px 0;border-bottom:1px solid var(--border)">
              <div style="min-width:180px;font-size:11px;color:var(--text-secondary)">${name}</div>
              <div style="flex:1;background:var(--bg-elevated);height:10px;border-radius:1px;overflow:hidden">
                <div style="width:${barWidth}%;height:100%;background:${barColor};transition:width .3s"></div>
              </div>
              <div style="min-width:60px;text-align:right;font-size:11px;font-weight:bold;color:${barColor}">${s.total_loss_pct.toFixed(1)}%</div>
              <div style="min-width:100px;text-align:right;font-size:10px;color:var(--text-dim)">$${fmt.currency(s.total_loss)}</div>
            </div>`;
        }).join('')}
      </div>
    </div>

    <div id="stress-detail-panel"></div>
  `;
}

function showScenarioDetail(name, scenario) {
  const panel = document.getElementById('stress-detail-panel');
  if (!panel) return;
  // Prefer the cached scenario for `name`; fall back to an explicitly-passed
  // object (legacy callers) so behavior is unchanged.
  scenario = scenario || _lastStressScenarios[name] || {};
  const positions = scenario.positions || [];
  const lossColor = scenario.total_loss_pct < -30 ? 'var(--red)' : scenario.total_loss_pct < -15 ? 'var(--amber)' : 'var(--text-secondary)';

  panel.innerHTML = `
    <div class="panel" style="border-color:var(--amber-dim)">
      <div class="panel-header" style="border-bottom:1px solid var(--border)">
        <span class="panel-title" style="color:var(--amber)">⚠ ${name} — Position Breakdown</span>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('stress-detail-panel').innerHTML=''">✕</button>
      </div>
      <div class="panel-body" style="padding:0;overflow-x:auto">
        <table class="data-table" style="min-width:700px">
          <thead><tr>
            <th>SYMBOL</th><th>SECTOR</th><th style="text-align:right">BETA</th>
            <th style="text-align:right">CURRENT VALUE</th><th style="text-align:right">SCENARIO DROP</th>
            <th style="text-align:right">EST. LOSS</th><th style="text-align:right">NEW VALUE</th>
          </tr></thead>
          <tbody>
            ${positions.map(p => {
              const dropColor = p.drop_pct < -30 ? 'var(--red)' : p.drop_pct < -10 ? 'var(--amber)' : p.drop_pct > 0 ? 'var(--green)' : 'var(--text-secondary)';
              return `<tr>
                <td><span class="col-symbol" onclick="openResearch('${_esc(p.symbol)}')">${_esc(p.symbol)}</span></td>
                <td style="font-size:10px;color:var(--text-dim)">${_esc(p.sector || '—')}</td>
                <td class="col-number">${p.beta?.toFixed(2) || '—'}</td>
                <td class="col-number">$${fmt.currency(p.current_value)}</td>
                <td class="col-number" style="color:${dropColor}">${p.drop_pct >= 0 ? '+' : ''}${p.drop_pct?.toFixed(1)}%</td>
                <td class="${p.loss < 0 ? 'col-red' : 'col-green'}">${p.loss >= 0 ? '+' : ''}$${fmt.currency(Math.abs(p.loss))}</td>
                <td class="col-number">$${fmt.currency(p.new_value)}</td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
}


// ── Earnings Catalyst Intelligence ────────────────────────────────────────────

async function loadEarningsView() {
  const view = document.getElementById('view-earnings');
  view.innerHTML = `<div class="loading"><div class="spinner"></div> SCANNING EARNINGS CALENDAR...</div>`;

  try {
    const calendar = await API.get('/api/earnings/calendar');

    if (!calendar.length) {
      view.innerHTML = `
        <div class="empty-state">
          <span class="empty-icon">⚡</span>
          <span>No upcoming earnings found for your portfolio or watchlist symbols.</span>
          <div style="margin-top:8px;font-size:11px;color:var(--text-dim)">Add stocks with upcoming earnings to your portfolio or watchlist.</div>
        </div>`;
      return;
    }

    // Group by urgency
    const urgent = calendar.filter(e => e.days_until <= 7);
    const soon   = calendar.filter(e => e.days_until > 7 && e.days_until <= 30);
    const later  = calendar.filter(e => e.days_until > 30);

    view.innerHTML = `
      <div class="flex-between mb-8">
        <div>
          <h2 style="font-size:14px;letter-spacing:.12em;color:var(--green);margin:0">⚡ EARNINGS CATALYST CALENDAR</h2>
          <div style="font-size:10px;color:var(--text-dim);margin-top:3px">${calendar.length} UPCOMING EVENTS · PORTFOLIO + WATCHLIST</div>
        </div>
        <div class="flex gap-8 align-center">
          <select class="form-select" id="earnings-ai-model" style="font-size:10px;padding:2px 6px;height:24px;width:auto">
            <option value="gpt-4o">GPT-4o</option>
            <option value="gpt-4o-mini">GPT-4o-mini</option>
          </select>
          <button class="btn btn-ghost btn-sm" onclick="loadEarningsView()">↻ REFRESH</button>
        </div>
      </div>

      ${urgent.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:10px;color:var(--red);letter-spacing:.1em;margin-bottom:6px">⚡ THIS WEEK (${urgent.length})</div>
        ${renderEarningsGrid(urgent)}
      </div>` : ''}

      ${soon.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:10px;color:var(--amber);letter-spacing:.1em;margin-bottom:6px">◎ NEXT 30 DAYS (${soon.length})</div>
        ${renderEarningsGrid(soon)}
      </div>` : ''}

      ${later.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:10px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:6px">▸ LATER (${later.length})</div>
        ${renderEarningsGrid(later)}
      </div>` : ''}

      <!-- Dossier panel — populated when user clicks a card -->
      <div id="earnings-dossier-panel"></div>
    `;
  } catch(e) {
    view.innerHTML = `<div class="empty-state"><span class="text-red">${_esc(e.message)}</span></div>`;
  }
}

function renderEarningsGrid(events) {
  return `<div class="earnings-grid">
    ${events.map(e => {
      const beatColor = e.beat_rate >= 75 ? 'var(--green)' : e.beat_rate >= 50 ? 'var(--amber)' : 'var(--red)';
      const urgencyBorder = e.days_until <= 3 ? 'var(--red)' : e.days_until <= 7 ? 'var(--amber)' : 'var(--border)';
      return `
      <div class="earnings-card" style="border-color:${urgencyBorder}" onclick="loadEarningsDossier('${_esc(e.symbol)}')">
        <div class="earnings-card-header">
          <span class="col-symbol" style="font-size:14px">${_esc(e.symbol)}</span>
          <span class="earnings-days-badge" style="color:${e.days_until <= 7 ? 'var(--red)' : 'var(--text-dim)'}">
            ${e.days_until === 0 ? 'TODAY' : e.days_until === 1 ? 'TOMORROW' : `${e.days_until}d`}
          </span>
        </div>
        <div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">${fmt.date(e.earnings_date)}</div>
        <div class="earnings-card-stats">
          <div class="earnings-stat">
            <div class="earnings-stat-label">EPS EST</div>
            <div class="earnings-stat-val">${e.eps_estimate != null ? '$' + e.eps_estimate.toFixed(2) : '—'}</div>
          </div>
          <div class="earnings-stat">
            <div class="earnings-stat-label">BEAT RATE</div>
            <div class="earnings-stat-val" style="color:${e.beat_rate != null ? beatColor : 'var(--text-dim)'}">
              ${e.beat_rate != null ? e.beat_rate + '%' : '—'}
            </div>
          </div>
          <div class="earnings-stat">
            <div class="earnings-stat-label">AVG SURP</div>
            <div class="earnings-stat-val" style="color:var(--green)">
              ${e.avg_surprise_pct != null ? '+' + e.avg_surprise_pct.toFixed(1) + '%' : '—'}
            </div>
          </div>
        </div>
        <div style="margin-top:8px;font-size:10px;color:var(--green);letter-spacing:.05em">
          ▸ CLICK FOR FULL DOSSIER + AI BRIEF
        </div>
      </div>`;
    }).join('')}
  </div>`;
}

async function loadEarningsDossier(symbol) {
  const panel = document.getElementById('earnings-dossier-panel');
  if (!panel) return;

  const model = document.getElementById('earnings-ai-model')?.value || 'gpt-4o';

  panel.innerHTML = `
    <div class="panel" style="margin-top:8px;border-color:var(--green-dim)">
      <div class="panel-header" style="border-bottom:1px solid var(--green-dim)">
        <span class="panel-title" style="color:var(--green)">⚡ ${symbol} — EARNINGS DOSSIER</span>
        <button class="btn btn-ghost btn-sm" onclick="document.getElementById('earnings-dossier-panel').innerHTML=''">✕ CLOSE</button>
      </div>
      <div class="panel-body">
        <div style="display:flex;align-items:center;gap:10px;color:var(--green);padding:12px 0">
          <div class="spinner"></div>
          <span style="font-size:11px">Building ${symbol} dossier with ${model}… fetching earnings history, options IV, insider activity…</span>
        </div>
      </div>
    </div>`;

  // Scroll into view
  panel.scrollIntoView({ behavior: 'smooth', block: 'start' });

  try {
    const d = await API.get(`/api/earnings/dossier/${symbol}?model=${model}`);
    renderEarningsDossier(panel, d);
  } catch(e) {
    const body = panel.querySelector('.panel-body');
    if (body) body.innerHTML =
      `<div class="text-red" style="font-size:11px;padding:8px">Failed: ${_esc(e.message || String(e))}</div>`;
  }
}

function renderEarningsDossier(panel, d) {
  const b = d.brief || {};
  const setupColor = { BULLISH: 'var(--green)', BEARISH: 'var(--red)', NEUTRAL: 'var(--text-dim)', AVOID: 'var(--red)' }[b.setup_signal] || 'var(--text-dim)';
  const setupClass = { BULLISH: 'signal-bullish', BEARISH: 'signal-bearish', NEUTRAL: 'signal-neutral', AVOID: 'signal-bearish' }[b.setup_signal] || 'signal-neutral';
  const convictionColor = { HIGH: 'var(--green)', MEDIUM: 'var(--amber)', LOW: 'var(--text-dim)' }[b.conviction] || 'var(--text-dim)';

  // History table rows
  const histRows = (d.history || []).map(h => `
    <tr class="${h.beat ? 'insider-buy-row' : 'insider-sell-row'}">
      <td>${_esc(h.date || '—')}</td>
      <td class="col-number">${h.estimate != null ? '$' + h.estimate.toFixed(2) : '—'}</td>
      <td class="col-number">${h.actual != null ? '$' + h.actual.toFixed(2) : '—'}</td>
      <td class="${h.beat ? 'col-green' : 'col-red'}">${h.surprise_pct != null ? (h.surprise_pct >= 0 ? '+' : '') + h.surprise_pct.toFixed(1) + '%' : '—'}</td>
      <td><span style="color:${h.beat ? 'var(--green)' : 'var(--red)'};font-size:10px">${h.beat ? '✓ BEAT' : '✗ MISS'}</span></td>
    </tr>`).join('');

  // Post-earnings moves
  const moveRows = (d.post_earnings_moves || []).map(m => `
    <tr>
      <td>${_esc(m.date || '—')}</td>
      <td class="${(m.move_pct || 0) >= 0 ? 'col-green' : 'col-red'}">${m.move_pct != null ? (m.move_pct >= 0 ? '+' : '') + m.move_pct.toFixed(2) + '%' : '—'}</td>
      <td><span style="color:${m.move_pct >= 0 ? 'var(--green)' : 'var(--red)'}">${_esc(m.direction)}</span></td>
    </tr>`).join('');

  // Insider summary
  const ins = d.insider_activity || {};
  const insColor = { BULLISH: 'var(--green)', BEARISH: 'var(--red)', NEUTRAL: 'var(--text-dim)' }[ins.signal] || 'var(--text-dim)';

  // IV comparison
  const ivVsHist = d.iv_vs_historical;
  const ivColor = ivVsHist == null ? 'var(--text-dim)' : Math.abs(ivVsHist) < 1 ? 'var(--text-dim)' : ivVsHist > 0 ? 'var(--amber)' : 'var(--green)';
  const ivLabel = ivVsHist == null ? '—'
    : ivVsHist > 2  ? `OVERPRICED +${ivVsHist.toFixed(1)}pp`
    : ivVsHist < -2 ? `UNDERPRICED ${ivVsHist.toFixed(1)}pp`
    : `FAIRLY PRICED ${ivVsHist >= 0 ? '+' : ''}${ivVsHist.toFixed(1)}pp`;

  const modelBadge = b.ai_powered
    ? `<span class="ai-badge" style="font-size:10px;color:var(--green)">◈ ${b.model_used || 'AI'}</span>`
    : `<span style="font-size:9px;color:var(--text-dim)">RULE-BASED</span>`;

  panel.innerHTML = `
    <div class="panel" style="margin-top:8px;border-color:var(--green-dim)">
      <div class="panel-header" style="border-bottom:1px solid var(--green-dim)">
        <div class="flex gap-8 align-center">
          <span class="panel-title" style="color:var(--green)">⚡ ${_esc(d.symbol)} — ${_esc(d.name || d.symbol)}</span>
          ${modelBadge}
        </div>
        <div class="flex gap-8 align-center">
          <button class="btn btn-ghost btn-sm" onclick="loadEarningsDossier('${_esc(d.symbol)}')">↻ REFRESH</button>
          <button class="btn btn-ghost btn-sm" onclick="document.getElementById('earnings-dossier-panel').innerHTML=''">✕</button>
        </div>
      </div>
      <div class="panel-body">

        <!-- AI Brief banner -->
        <div style="display:flex;gap:16px;align-items:flex-start;margin-bottom:20px;flex-wrap:wrap">
          <div style="display:flex;flex-direction:column;gap:6px;min-width:160px">
            <span class="signal-badge ${setupClass}" style="font-size:13px;padding:6px 14px;align-self:flex-start">${_esc(b.setup_signal || 'NEUTRAL')}</span>
            <div style="display:flex;align-items:center;gap:6px">
              <span style="font-size:10px;color:var(--text-dim)">CONVICTION:</span>
              <span style="font-size:11px;font-weight:bold;color:${convictionColor}">${_esc(b.conviction || '—')}</span>
            </div>
            <div style="font-size:10px;color:var(--text-dim)">
              REPORTS: <span style="color:var(--text-primary)">${fmt.date(d.earnings_date)}</span>
            </div>
            <div style="font-size:10px;color:${d.days_until <= 7 ? 'var(--red)' : 'var(--amber)'}">
              ${d.days_until === 0 ? '⚡ TODAY' : d.days_until === 1 ? '⚡ TOMORROW' : `⚡ ${d.days_until} DAYS AWAY`}
            </div>
          </div>
          <div style="flex:1;min-width:280px">
            ${b.headline ? `<div style="font-size:13px;color:var(--text-primary);font-weight:bold;margin-bottom:8px">${_esc(b.headline)}</div>` : ''}
            <div style="font-size:12px;color:var(--text-secondary);line-height:1.7;background:var(--bg-secondary);padding:12px;border-left:2px solid ${setupColor}">
              ${_esc(b.brief) || 'Configure OpenAI API key for analyst brief.'}
            </div>
          </div>
        </div>

        <div class="panel-grid grid-2" style="gap:12px;margin-bottom:16px">

          <!-- EPS Consensus -->
          <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
            <div style="font-size:10px;color:var(--blue);letter-spacing:.1em;margin-bottom:10px">EPS CONSENSUS</div>
            <div style="display:flex;gap:20px;flex-wrap:wrap">
              <div>
                <div style="font-size:10px;color:var(--text-dim)">ESTIMATE</div>
                <div style="font-size:20px;font-weight:bold;color:var(--text-primary)">${d.eps_estimate != null ? '$' + d.eps_estimate.toFixed(2) : '—'}</div>
              </div>
              <div>
                <div style="font-size:10px;color:var(--text-dim)">RANGE</div>
                <div style="font-size:12px;color:var(--text-secondary)">
                  ${d.eps_low != null ? '$' + d.eps_low.toFixed(2) : '—'} – ${d.eps_high != null ? '$' + d.eps_high.toFixed(2) : '—'}
                </div>
              </div>
              ${d.beat_rate != null ? `
              <div>
                <div style="font-size:10px;color:var(--text-dim)">BEAT RATE</div>
                <div style="font-size:20px;font-weight:bold;color:${d.beat_rate >= 75 ? 'var(--green)' : d.beat_rate >= 50 ? 'var(--amber)' : 'var(--red)'}">${d.beat_rate}%</div>
              </div>` : ''}
              ${d.avg_surprise_pct != null ? `
              <div>
                <div style="font-size:10px;color:var(--text-dim)">AVG SURPRISE</div>
                <div style="font-size:16px;font-weight:bold;color:var(--green)">+${d.avg_surprise_pct.toFixed(1)}%</div>
              </div>` : ''}
            </div>
          </div>

          <!-- Options IV vs Historical -->
          <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
            <div style="font-size:10px;color:var(--amber);letter-spacing:.1em;margin-bottom:10px">OPTIONS IMPLIED MOVE</div>
            <div style="display:flex;gap:20px;flex-wrap:wrap;align-items:flex-end">
              <div>
                <div style="font-size:10px;color:var(--text-dim)">STRADDLE IMPLIES</div>
                <div style="font-size:20px;font-weight:bold;color:var(--amber)">${d.implied_move_pct != null ? '±' + d.implied_move_pct.toFixed(1) + '%' : '—'}</div>
                ${d.options_expiry_used ? `<div style="font-size:9px;color:var(--text-dim)">expiry ${d.options_expiry_used}</div>` : ''}
              </div>
              <div>
                <div style="font-size:10px;color:var(--text-dim)">HIST AVG MOVE</div>
                <div style="font-size:20px;font-weight:bold;color:var(--text-secondary)">${d.avg_abs_move_pct != null ? '±' + d.avg_abs_move_pct.toFixed(1) + '%' : '—'}</div>
              </div>
              <div>
                <div style="font-size:10px;color:var(--text-dim)">IV STATUS</div>
                <div style="font-size:12px;font-weight:bold;color:${ivColor}">${ivLabel}</div>
              </div>
            </div>
            ${b.options_take ? `<div style="font-size:11px;color:var(--text-dim);margin-top:10px;border-top:1px solid var(--border);padding-top:8px">${_esc(b.options_take)}</div>` : ''}
          </div>
        </div>

        <div class="panel-grid grid-2" style="gap:12px;margin-bottom:16px">

          <!-- Bull/Bear cases + positioning -->
          <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
            <div style="font-size:10px;color:var(--green);letter-spacing:.1em;margin-bottom:8px">POSITIONING ADVICE</div>
            ${b.positioning_advice ? `<div style="font-size:12px;color:var(--text-primary);margin-bottom:10px;line-height:1.6">${_esc(b.positioning_advice)}</div>` : ''}
            <div style="display:flex;flex-direction:column;gap:6px">
              ${b.bull_case ? `<div style="font-size:11px"><span style="color:var(--green)">▲ BULL:</span> <span style="color:var(--text-secondary)">${_esc(b.bull_case)}</span></div>` : ''}
              ${b.bear_case ? `<div style="font-size:11px"><span style="color:var(--red)">▼ BEAR:</span> <span style="color:var(--text-secondary)">${_esc(b.bear_case)}</span></div>` : ''}
            </div>
            ${b.key_metrics_to_watch ? `
            <div style="margin-top:10px;border-top:1px solid var(--border);padding-top:8px">
              <div style="font-size:10px;color:var(--text-dim);margin-bottom:6px">KEY METRICS TO WATCH</div>
              ${b.key_metrics_to_watch.map(m => `<div style="font-size:11px;color:var(--text-secondary);padding:2px 0">▸ ${_esc(m)}</div>`).join('')}
            </div>` : ''}
          </div>

          <!-- Insider activity -->
          <div style="background:var(--bg-secondary);padding:12px;border:1px solid var(--border)">
            <div style="font-size:10px;color:var(--blue);letter-spacing:.1em;margin-bottom:10px">INSIDER ACTIVITY (60 DAYS PRE-EARNINGS)</div>
            <div style="display:flex;gap:20px;flex-wrap:wrap;margin-bottom:10px">
              <div>
                <div style="font-size:10px;color:var(--text-dim)">SIGNAL</div>
                <div style="font-size:14px;font-weight:bold;color:${insColor}">${_esc(ins.signal || 'NEUTRAL')}</div>
              </div>
              <div>
                <div style="font-size:10px;color:var(--text-dim)">BUYS</div>
                <div style="font-size:14px;color:var(--green)">${ins.buys || 0} <span style="font-size:10px">(${ins.buy_value ? '$' + (ins.buy_value/1e3).toFixed(0) + 'K' : '$0'})</span></div>
              </div>
              <div>
                <div style="font-size:10px;color:var(--text-dim)">SELLS</div>
                <div style="font-size:14px;color:var(--red)">${ins.sells || 0} <span style="font-size:10px">(${ins.sell_value ? '$' + (ins.sell_value/1e3).toFixed(0) + 'K' : '$0'})</span></div>
              </div>
            </div>
            <div style="font-size:10px;color:var(--text-dim)">
              Net insider flow is <span style="color:${(ins.net_buy_value || 0) >= 0 ? 'var(--green)' : 'var(--red)'};font-weight:bold">
              ${(ins.net_buy_value || 0) >= 0 ? '+' : ''}$${((ins.net_buy_value || 0)/1e3).toFixed(0)}K
              </span> in the 60 days before this earnings event.
            </div>
          </div>
        </div>

        <!-- EPS History table -->
        ${histRows ? `
        <div style="margin-bottom:16px">
          <div style="font-size:10px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:6px">EPS HISTORY — LAST 8 QUARTERS</div>
          <table class="data-table" style="font-size:11px">
            <thead><tr><th>QUARTER</th><th style="text-align:right">ESTIMATE</th><th style="text-align:right">ACTUAL</th><th style="text-align:right">SURPRISE</th><th>RESULT</th></tr></thead>
            <tbody>${histRows}</tbody>
          </table>
        </div>` : ''}

        <!-- Post-earnings moves table -->
        ${moveRows ? `
        <div>
          <div style="font-size:10px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:6px">POST-EARNINGS PRICE REACTION — LAST 6 QUARTERS</div>
          <table class="data-table" style="font-size:11px">
            <thead><tr><th>EARNINGS DATE</th><th style="text-align:right">NEXT DAY MOVE</th><th>DIRECTION</th></tr></thead>
            <tbody>${moveRows}</tbody>
          </table>
        </div>` : ''}

      </div>
    </div>`;
}

// ── Settings View ─────────────────────────────────────────────────────────────
async function loadSettings() {
  const view = document.getElementById('view-settings');
  // Fetch settings, but NEVER let a fetch error blank the page — the OpenAI key
  // input lives here, so the form must always render (with defaults on failure)
  // or the user has no way to configure their key. (Previously a silent
  // catch(e){} left the whole Settings view empty on any error.)
  let settings = {};
  try {
    settings = (await API.get('/api/settings')) || {};
    State.settings = settings;
  } catch (e) {
    settings = State.settings || {};
  }
  try {
    view.innerHTML = `
      <div class="panel-grid grid-2">
        <div class="panel">
          <div class="panel-header"><span class="panel-title">PREFERENCES</span></div>
          <div class="panel-body">
            <div class="form-group">
              <label class="form-label">AUTO-REFRESH INTERVAL (SECONDS)</label>
              <input class="form-input" id="set-refresh" type="number" value="${settings.refresh_interval || 60}" min="10" max="3600">
            </div>
            <div class="form-group">
              <label class="form-label">BENCHMARK SYMBOL</label>
              <input class="form-input" id="set-benchmark" value="${_esc(settings.benchmark || 'SPY')}">
            </div>
            <div class="form-group">
              <label class="form-label">CURRENCY</label>
              <select class="form-select" id="set-currency">
                <option ${settings.currency==='USD'?'selected':''}>USD</option>
                <option ${settings.currency==='EUR'?'selected':''}>EUR</option>
                <option ${settings.currency==='GBP'?'selected':''}>GBP</option>
              </select>
            </div>
            <div class="form-group" style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
              <label class="form-label" style="color:var(--amber)">▲ AI BUDGET</label>
              <label class="form-label" style="margin-top:4px">DAILY AI CALL CAP</label>
              <input class="form-input" id="set-ai-cap" type="number" min="1" max="100000" value="${parseInt(settings.ai_daily_cap, 10) || 200}">
              <div style="font-size:10px;color:var(--text-dim);margin-top:4px">
                Jarvis answers, voice lines and summaries per day. The
                AUGUR_AI_DAILY_CAP environment variable overrides this.
              </div>
              <label class="form-label" style="margin-top:8px">JARVIS VOICE (PREMIUM TTS)</label>
              <div style="display:flex;gap:6px;align-items:center">
                <select class="form-select" id="set-tts-voice" style="flex:1">
                  ${['onyx','alloy','echo','fable','nova','shimmer'].map(v =>
                    `<option value="${v}" ${(settings.jarvis_tts_voice || 'onyx') === v ? 'selected' : ''}>${v.toUpperCase()}</option>`).join('')}
                </select>
                <button class="btn btn-ghost btn-sm" id="tts-preview-btn" type="button"
                        title="Preview this voice" aria-label="Preview the selected Jarvis voice">▶</button>
              </div>
            </div>
            <div class="form-group" style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
              <label class="form-label" style="color:var(--accent,#4cd8ff)">◉ CLAUDE CODE (JARVIS)</label>
              <div style="font-size:10px;color:var(--text-dim);margin:4px 0 8px">
                Lets Jarvis answer open-ended questions by driving the local Claude Code CLI
                (read the codebase &amp; engines, search the web). Trigger with “claude, …”,
                “deep dive”, or “investigate”.
              </div>
              <label class="form-label">BACKEND</label>
              <select class="form-select" id="set-claude-enabled">
                <option value="1" ${String(settings.jarvis_claude_enabled || '1') !== '0' ? 'selected' : ''}>Enabled</option>
                <option value="0" ${String(settings.jarvis_claude_enabled || '1') === '0' ? 'selected' : ''}>Disabled</option>
              </select>
              <label class="form-label" style="margin-top:8px">PERMISSION MODE</label>
              <select class="form-select" id="set-claude-permission">
                ${['default', 'acceptEdits', 'bypassPermissions'].map(m =>
                  `<option value="${m}" ${(settings.jarvis_claude_permission || 'default') === m ? 'selected' : ''}>${m}${m === 'default' ? ' — read-only (safest)' : m === 'bypassPermissions' ? ' — full power (caution)' : ''}</option>`).join('')}
              </select>
              <div style="font-size:10px;color:var(--text-dim);margin-top:4px">
                Default is read-only (the allowlist below). bypassPermissions grants full local
                shell/file power — only on a machine you trust.
              </div>
              <label class="form-label" style="margin-top:8px">ALLOWED TOOLS</label>
              <input class="form-input" id="set-claude-tools" value="${_esc(settings.jarvis_claude_allowed_tools || 'Read Grep Glob WebSearch WebFetch')}">
              <label class="form-label" style="margin-top:8px">MODEL (OPTIONAL)</label>
              <input class="form-input" id="set-claude-model" placeholder="inherit Claude Code default" value="${_esc(settings.jarvis_claude_model || '')}">
            </div>
            <div class="form-group" style="margin-top:16px;padding-top:16px;border-top:1px solid var(--border)">
              <label class="form-label" style="color:var(--amber)">▲ DAILY DIGEST</label>
              <div style="font-size:10px;color:var(--text-dim);margin:4px 0 8px">
                Deliver the morning briefing outside the app: macOS notification, a dated markdown
                file in ./digests/, and/or email (when SMTP is configured).
              </div>
              <label class="form-label">SCHEDULED DELIVERY</label>
              <select class="form-select" id="set-digest-enabled">
                <option value="0" ${String(settings.digest_enabled || '0') !== '1' ? 'selected' : ''}>Off</option>
                <option value="1" ${String(settings.digest_enabled || '0') === '1' ? 'selected' : ''}>On</option>
              </select>
              <label class="form-label" style="margin-top:8px">DELIVERY HOUR (LOCAL, 0–23)</label>
              <input class="form-input" id="set-digest-hour" type="number" min="0" max="23" value="${Number.isInteger(parseInt(settings.digest_hour, 10)) ? parseInt(settings.digest_hour, 10) : 8}">
              <label class="form-label" style="margin-top:8px">CHANNELS</label>
              <input class="form-input" id="set-digest-channels" placeholder="file,notify,email" value="${_esc(settings.digest_channels || 'file')}">
            </div>
            <button class="btn btn-green btn-lg" onclick="saveSettings()" style="margin-top:8px">SAVE SETTINGS</button>
          </div>
        </div>
        <div class="panel">
          <div class="panel-header"><span class="panel-title">SYSTEM INFO</span></div>
          <div class="panel-body">
            <div class="fund-grid">
              ${[
                ['SYSTEM', ((document.getElementById('sys-version') || {}).textContent || 'AUGUR').trim()],
                ['DATA SOURCE', 'YAHOO FINANCE + COINGECKO'],
                ['DATABASE', 'SQLITE3 (LOCAL)'],
                ['BACKEND', 'PYTHON / FLASK'],
                ['LAST REFRESH', State.lastRefresh ? State.lastRefresh.toLocaleTimeString('en-US') : '—'],
              ].map(([k,v])=>`<div class="fund-item"><span class="fund-key">${k}</span><span class="fund-val">${_esc(v)}</span></div>`).join('')}
            </div>
            <div style="margin-top:12px">
              <div class="chain-node">AUGUR NODE // CONNECTED</div>
            </div>
          </div>
        </div>
      </div>
      <div class="panel" style="margin-top:8px">
        <div class="panel-header"><span class="panel-title">API KEYS</span>
          <span style="font-size:9px;color:var(--text-dim);letter-spacing:.06em">BRING YOUR OWN — STORED LOCALLY, NEVER SENT ANYWHERE BUT THE PROVIDER</span>
        </div>
        <div class="panel-body" id="keys-panel"><div class="loading"><div class="spinner"></div></div></div>
      </div>
    `;
    const ttsPreview = document.getElementById('tts-preview-btn');
    if (ttsPreview) ttsPreview.addEventListener('click', previewTtsVoice);
    loadKeysPanel();
  } catch(e) {
    // Render failed — show the error plus a minimal key form rather than a
    // blank page, so the OpenAI key input is always reachable.
    view.innerHTML =
      '<div class="panel"><div class="panel-header"><span class="panel-title">SETTINGS</span></div>'
      + '<div class="panel-body">'
      + '<div class="col-negative" style="font-size:11px;margin-bottom:12px">Settings failed to render: ' + _esc(e.message) + '</div>'
      + '<div class="form-group"><label class="form-label">OPENAI API KEY</label>'
      + '<input class="form-input" id="set-openai-key" type="password" placeholder="sk-..." autocomplete="off"></div>'
      + '<button class="btn btn-green" onclick="saveSettings()" style="margin-top:8px">SAVE SETTINGS</button>'
      + '</div></div>';
  }
}

// ── Jarvis voice preview (Settings) ──────────────────────────────────────────
// POSTs a short fixed phrase to /api/jarvis/tts with the currently selected
// voice (the route accepts an explicit `voice`) and plays the returned clip.
// 404 = keyless/capped → friendly info toast, mirroring jarvis.js Voice.
let _ttsPreviewAudio = null;
async function previewTtsVoice() {
  const btn = document.getElementById('tts-preview-btn');
  const sel = document.getElementById('set-tts-voice');
  if (!sel) return;
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  // Stop a previous preview so two clips never overlap.
  if (_ttsPreviewAudio) {
    try { _ttsPreviewAudio.pause(); _ttsPreviewAudio.src = ''; } catch (e) {}
    _ttsPreviewAudio = null;
  }
  try {
    const r = await fetch('/api/jarvis/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'At your service. This is how I sound.', voice: sel.value }),
    });
    if (!r.ok) {
      if (r.status === 404) Toast.info('Premium voice needs an OpenAI key — Jarvis uses browser speech meanwhile.');
      else Toast.warn('Voice preview failed (HTTP ' + r.status + ')');
      return;
    }
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = new Audio(url);
    _ttsPreviewAudio = a;
    let revoked = false;
    const fin = () => {
      if (!revoked) { revoked = true; URL.revokeObjectURL(url); }
      if (_ttsPreviewAudio === a) _ttsPreviewAudio = null;
    };
    a.onended = fin;
    a.onerror = fin;
    a.play().catch(fin);
  } catch (e) {
    Toast.warn('Voice preview failed: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '▶'; }
  }
}

// ── API key console (Settings → API KEYS panel) ──────────────────────────────
async function loadKeysPanel() {
  const el = document.getElementById('keys-panel');
  if (!el) return;
  try {
    const { providers } = await API.get('/api/keys');
    el.innerHTML = providers.map(p => `
      <div class="key-row" id="key-row-${p.provider}">
        <div class="key-row-head">
          <span class="key-label">${_esc(p.label)}</span>
          ${p.configured
            ? `<span style="color:var(--green);font-size:10px">● ${_esc(p.masked)}${p.source === 'env' ? ' (from environment)' : ''}</span>`
            : '<span style="color:var(--text-dim);font-size:10px">○ NOT SET</span>'}
        </div>
        <div style="font-size:10px;color:var(--text-secondary);margin:3px 0 6px">${_esc(p.unlocks)}</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          <input class="form-input" id="key-input-${p.provider}" type="password" style="flex:1;min-width:200px"
                 placeholder="${p.configured ? 'Paste a new key to replace ' + _esc(p.masked) : _esc(p.hint)}" autocomplete="off">
          <button class="btn btn-green btn-sm" onclick="saveProviderKey('${p.provider}')">SAVE</button>
          <button class="btn btn-ghost btn-sm" onclick="testProviderKey('${p.provider}')" ${p.configured ? '' : 'disabled'}>TEST</button>
          <button class="btn btn-ghost btn-sm" onclick="removeProviderKey('${p.provider}')" ${p.configured && p.source !== 'env' ? '' : 'disabled'}>REMOVE</button>
        </div>
        <div id="key-status-${p.provider}" style="font-size:10px;margin-top:4px"></div>
      </div>`).join('');
  } catch (e) {
    el.innerHTML = `<div class="col-negative" style="font-size:11px">Key console unavailable: ${_esc(e.message)}</div>`;
  }
}

async function saveProviderKey(provider) {
  const input = document.getElementById(`key-input-${provider}`);
  const status = document.getElementById(`key-status-${provider}`);
  const key = input ? input.value.trim() : '';
  if (!key) { Toast.warn('Paste a key first'); return; }
  try {
    const r = await API.post(`/api/keys/${provider}`, { key });
    Toast.success(`${provider} key saved (${r.masked})`);
    loadKeysPanel();
  } catch (e) {
    if (status) status.innerHTML = `<span class="col-negative">${_esc(e.message)}</span>`;
  }
}

async function testProviderKey(provider) {
  const status = document.getElementById(`key-status-${provider}`);
  if (status) status.innerHTML = '<span style="color:var(--text-dim)">Testing…</span>';
  try {
    const r = await API.post(`/api/keys/${provider}/test`, {});
    if (status) status.innerHTML = r.ok
      ? `<span class="col-positive">✓ ${_esc(r.detail)}</span>`
      : `<span class="col-negative">✗ ${_esc(r.detail)}</span>`;
  } catch (e) {
    if (status) status.innerHTML = `<span class="col-negative">✗ ${_esc(e.message)}</span>`;
  }
}

async function removeProviderKey(provider) {
  if (!confirm(`Remove the ${provider} key? AI features fall back to rule-based behavior.`)) return;
  try {
    const r = await API.del(`/api/keys/${provider}`);
    Toast.info(`${provider} key removed${r.note ? ' — ' + r.note : ''}`);
    loadKeysPanel();
  } catch (e) {
    Toast.error(e.message);
  }
}

async function saveSettings() {
  try {
    // Null-guard every field: the Settings fallback-render path emits ONLY
    // the key input, so reading set-refresh/.value blindly threw and made
    // SAVE a no-op exactly when the user needed to set their key.
    const payload = {};
    const refresh = document.getElementById('set-refresh');
    const benchmark = document.getElementById('set-benchmark');
    const currency = document.getElementById('set-currency');
    if (refresh) payload.refresh_interval = refresh.value;
    if (benchmark) payload.benchmark = benchmark.value;
    if (currency) payload.currency = currency.value;
    const capField = document.getElementById('set-ai-cap');
    if (capField && capField.value) payload.ai_daily_cap = capField.value;
    const voiceField = document.getElementById('set-tts-voice');
    if (voiceField && voiceField.value) payload.jarvis_tts_voice = voiceField.value;
    const keyField = document.getElementById('set-openai-key');  // fallback-render path only
    if (keyField && keyField.value.trim()) {
      payload.openai_api_key = keyField.value.trim();
    }
    // Claude Code backend + daily digest (v2.6).
    const cE = document.getElementById('set-claude-enabled');     if (cE) payload.jarvis_claude_enabled = cE.value;
    const cP = document.getElementById('set-claude-permission');  if (cP) payload.jarvis_claude_permission = cP.value;
    const cT = document.getElementById('set-claude-tools');       if (cT) payload.jarvis_claude_allowed_tools = cT.value.trim();
    const cM = document.getElementById('set-claude-model');       if (cM) payload.jarvis_claude_model = cM.value.trim();
    const dE = document.getElementById('set-digest-enabled');     if (dE) payload.digest_enabled = dE.value;
    const dH = document.getElementById('set-digest-hour');        if (dH && dH.value !== '') payload.digest_hour = dH.value;
    const dC = document.getElementById('set-digest-channels');    if (dC) payload.digest_channels = dC.value.trim();
    await API.post('/api/settings', payload);
    // Pull the freshly-saved settings back so timers see the new interval.
    try { State.settings = await API.get('/api/settings'); } catch(e) {}
    if (typeof restartTimers === 'function') restartTimers();
    Toast.success('Settings saved');
    // Reload to refresh key status indicator
    loadSettings();
  } catch(e) { Toast.error('Failed to save'); }
}

// ── Quick helpers ─────────────────────────────────────────────────────────────
function quickAddToPortfolio(symbol, name, type) {
  if (type === undefined) type = 'stock';
  document.getElementById('pos-symbol').value = symbol;
  document.getElementById('pos-name').value = name || '';
  document.getElementById('pos-type').value = type;
  loadAccountsDropdown();
  Modal.open('modal-add-position');
}

function quickAddToWatchlist(symbol, name) {
  document.getElementById('wl-symbol').value = symbol;
  Modal.open('modal-add-watchlist');
}

// ── Status bar ────────────────────────────────────────────────────────────────
function updateStatusBar() {
  const el = document.getElementById('status-last-refresh');
  if (el) el.textContent = State.lastRefresh ? State.lastRefresh.toLocaleTimeString('en-US') : '—';

  const portEl = document.getElementById('status-portfolio-value');
  if (portEl && State.portfolio?.summary?.total_value) {
    portEl.textContent = '$' + fmt.currency(State.portfolio.summary.total_value);
  }
}

// ── Command bar search ────────────────────────────────────────────────────────
let searchTimer = null;

function handleCmdInput(e) {
  const q = e.target.value.trim();

  if (e.key === 'Enter' && q) {
    const sym = q.toUpperCase();
    clearTimeout(searchTimer);
    document.getElementById('search-results').classList.remove('visible');
    e.target.value = '';
    openResearch(sym);
    return;
  }

  if (e.key === 'Escape') {
    clearTimeout(searchTimer);
    document.getElementById('search-results').classList.remove('visible');
    e.target.value = '';
    return;
  }

  clearTimeout(searchTimer);
  if (q.length < 1) {
    document.getElementById('search-results').classList.remove('visible');
    return;
  }

  searchTimer = setTimeout(async () => {
    try {
      const results = await API.get(`/api/search?q=${encodeURIComponent(q)}`);
      const el = document.getElementById('search-results');
      if (!el) return;
      if (!Array.isArray(results) || !results.length) {
        el.classList.remove('visible');
        return;
      }
      el.innerHTML = results.map(r => `
        <div class="search-result-item" onclick="selectSearchResult('${_jesc(r.symbol)}')">
          <span class="sr-symbol">${_esc(r.symbol)}</span>
          <span class="sr-name">${_esc(r.name)}</span>
          <span class="sr-type">${_esc(r.type)}</span>
          <span class="sr-exch">${_esc(r.exchange)}</span>
        </div>`).join('');
      el.classList.add('visible');
    } catch(e) {}
  }, 300);
}

function selectSearchResult(symbol) {
  document.getElementById('cmd-input').value = '';
  document.getElementById('search-results').classList.remove('visible');
  openResearch(symbol);
}

// ── Auto-refresh ──────────────────────────────────────────────────────────────
// Map every refreshable view to its loader function. Views not listed
// here are considered "static enough" that ticking them on a 60s timer
// adds noise without value (e.g. terminal, settings, research detail
// which the user is actively interacting with).
// ── Trading Agent (AJTA) view ───────────────────────────────────────────────
function _ajToast(msg, ok) {
  try {
    if (typeof Toast !== 'undefined' && Toast) {
      (ok === false ? (Toast.error || Toast.show) : (Toast.success || Toast.show)).call(Toast, msg);
      return;
    }
  } catch (e) {}
}
// window.confirm() is unreliable in pywebview/WKWebView (returns falsy without
// showing a dialog), which would silently dead the KILL/RUN buttons. Use an
// in-DOM modal that works in every webview.
function _ajConfirm(message) {
  return new Promise((resolve) => {
    const ov = document.createElement('div');
    ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:99999;display:flex;align-items:center;justify-content:center';
    ov.innerHTML = '<div style="background:var(--panel,#0d0d0d);border:1px solid var(--border,#333);border-radius:8px;padding:20px;max-width:440px;font-size:13px">'
      + '<div style="margin-bottom:16px">' + _esc(message) + '</div>'
      + '<div style="display:flex;gap:10px;justify-content:flex-end">'
      + '<button class="btn btn-sm" data-x="0">Cancel</button>'
      + '<button class="btn btn-sm" data-x="1" style="background:var(--red,#c0392b);color:#fff;border-color:var(--red,#c0392b)">Confirm</button>'
      + '</div></div>';
    const done = (v) => { if (ov.parentNode) document.body.removeChild(ov); resolve(v); };
    ov.addEventListener('click', (e) => {
      const t = e.target;
      if (t === ov) return done(false);
      const x = t && t.getAttribute && t.getAttribute('data-x');
      if (x === '0') return done(false);
      if (x === '1') return done(true);
    });
    document.body.appendChild(ov);
  });
}
function _ajMoney(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v);
  return (n < 0 ? '-$' : '$') + Math.abs(n).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
// Render an option ticker like "OPT:AAPL:20260717:C:150.0" as a human label
// "AAPL $150 CALL 07/17/26". Non-option symbols pass through unchanged.
function _ajFmtSymbol(sym) {
  if (typeof sym !== 'string' || sym.indexOf('OPT:') !== 0) return sym;
  const parts = sym.split(':');
  if (parts.length < 5) return sym;
  const und = parts[1];
  const ymd = parts[2];
  const cp = (parts[3] || '').toUpperCase();
  const strikeRaw = parts[4];
  if (!/^\d{8}$/.test(ymd)) return sym;
  const mm = ymd.slice(4, 6), dd = ymd.slice(6, 8), yy = ymd.slice(2, 4);
  const right = cp === 'C' ? 'CALL' : (cp === 'P' ? 'PUT' : cp);
  const strikeN = Number(strikeRaw);
  const strike = isFinite(strikeN) ? String(strikeN) : strikeRaw;
  return `${und} $${strike} ${right} ${mm}/${dd}/${yy}`;
}
function _ajPill(label, on, tone) {
  const cls = on ? (tone || 'green') : 'dim';
  return `<span class="status-val ${cls}" style="padding:2px 8px;border:1px solid var(--border);border-radius:10px;font-size:11px;margin-right:6px">${_esc(label)}</span>`;
}
function _ajPct(v) {
  if (v === null || v === undefined || isNaN(v)) return '—';
  const n = Number(v);
  // accept either 0–1 or 0–100; treat ≤1 as a fraction.
  return Math.round((Math.abs(n) <= 1 ? n * 100 : n)) + '%';
}
// Render the full council response (decision + per-analyst reports + debate).
function _ajRenderCouncil(d) {
  const dec = d.decision || {};
  const reports = d.reports || [];
  const debate = d.debate || [];
  const actTone = (dec.action || '').toLowerCase() === 'buy' ? 'green'
    : (dec.action || '').toLowerCase() === 'sell' ? 'red' : 'amber';
  // ── per-analyst cards ──
  const cards = reports.length ? `<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px;margin:10px 0">${reports.map(r =>
    `<div class="panel"><div class="panel-body">
      <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
        <b>${_esc(r.analyst || '—')}</b>
        ${_ajPill(String(r.band || '—'), true, 'blue')}
      </div>
      <div class="muted" style="font-size:11px;margin-bottom:6px">Score <b style="color:var(--text-primary)">${r.score == null ? '—' : (Math.round(Number(r.score) * 10) / 10) + '/10'}</b> · Confidence <b style="color:var(--text-primary)">${_ajPct(r.confidence)}</b></div>
      <div style="font-size:12px">${_esc(r.narrative || '')}</div>
    </div></div>`).join('')}</div>` : '<div class="muted">No analyst reports.</div>';
  // ── debate transcript, grouped by debate then role ──
  let transcript = '';
  if (debate.length) {
    const byDebate = {};
    debate.forEach(t => { const k = t.debate || 'debate'; (byDebate[k] = byDebate[k] || []).push(t); });
    transcript = Object.keys(byDebate).map(dk => {
      const turns = byDebate[dk].slice().sort((a, b) => (a.round || 0) - (b.round || 0));
      return `<div style="margin-bottom:10px"><div class="muted" style="font-size:10px;letter-spacing:.05em;text-transform:uppercase;margin-bottom:4px">${_esc(dk)} debate</div>${turns.map(t =>
        `<div class="jv-obs info" style="margin-bottom:4px"><span class="muted">[R${_esc(String(t.round == null ? '?' : t.round))} · ${_esc(t.role || '—')}]</span> ${_esc(t.content || '')}</div>`).join('')}</div>`;
    }).join('');
  } else {
    transcript = '<div class="muted">No debate transcript.</div>';
  }
  // ── final decision ──
  const decBox = `<div class="panel" style="margin:10px 0;border-left:3px solid var(--${actTone})"><div class="panel-body">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">
      <b style="font-size:15px">${_esc(dec.symbol || '')}</b>
      ${_ajPill((dec.action || '—').toUpperCase(), true, actTone)}
      ${dec.rating != null ? _ajPill('rating ' + _esc(String(dec.rating)), true, 'blue') : ''}
      ${_ajPill('conviction ' + _ajPct(dec.conviction), true, 'blue')}
      ${dec.status ? _ajPill(_esc(String(dec.status)), true, 'dim') : ''}
    </div>
    ${dec.thesis ? `<div style="font-size:12px;margin-bottom:6px"><span class="muted">Thesis:</span> ${_esc(dec.thesis)}</div>` : ''}
    ${dec.dissent ? `<div style="font-size:12px;margin-bottom:6px"><span class="muted">Dissent:</span> ${_esc(dec.dissent)}</div>` : ''}
    <div class="muted" style="font-size:11px;display:flex;gap:16px;flex-wrap:wrap">
      ${dec.price_target != null ? '<span>Target <b style="color:var(--text-primary)">' + _ajMoney(dec.price_target) + '</b></span>' : ''}
      ${dec.time_horizon ? '<span>Horizon <b style="color:var(--text-primary)">' + _esc(String(dec.time_horizon)) + '</b></span>' : ''}
      <span>Cost <b style="color:var(--text-primary)">${dec.cost_usd == null ? '—' : _ajMoney(dec.cost_usd)}</b></span>
      <span>LLM calls <b style="color:var(--text-primary)">${dec.n_calls == null ? '—' : _esc(String(dec.n_calls))}</b></span>
    </div>
  </div>`;
  return decBox
    + '<div class="panel-title" style="font-size:11px;margin-top:6px">ANALYST REPORTS</div>' + cards
    + '<div class="panel-title" style="font-size:11px;margin-top:6px">DEBATE TRANSCRIPT</div><div style="margin-top:6px">' + transcript + '</div>';
}

async function loadTradingView() {
  const el = document.getElementById('view-trading');
  if (!el) return;
  el.innerHTML = '<div class="loading"><div class="spinner"></div> Loading trading agent…</div>';
  let s, cfg;
  try {
    s = await API.get('/api/aj/status');
    cfg = await API.get('/api/aj/config');   // FULL config — status returns only a subset
  }
  catch (e) { el.innerHTML = `<div class="empty-state"><span>Trading agent unavailable: ${_esc(e.message)}</span></div>`; return; }

  const pnl = s.day_pnl || {};
  const cum = s.cumulative_pnl || {};
  const orders = s.orders || {};
  const halted = !!s.halted;
  const enabled = !!s.trading_enabled;
  const live = !!s.live_trading_enabled;

  const alerts = (s.alerts || []).map(a =>
    `<div class="jv-obs ${a.level === 'critical' ? 'neg' : (a.level === 'warning' ? 'warn' : 'info')}">• [${_esc(a.level)}] ${_esc(a.message)}</div>`).join('') || '<div class="muted">No alerts.</div>';

  const allowlist = (cfg.symbol_allowlist || []).join(', ');

  const cumPos = (s.cumulative_pnl || {}).total || 0;
  el.innerHTML = `
    <div class="aj-hero">
      <div class="aj-hero-left">
        <div class="aj-hero-title">◉ TRADING AGENT <span class="aj-hero-ver">AJTA v${_esc(s.version || '3.0.0')}</span></div>
        <div class="aj-hero-pills">
          ${_ajPill(enabled ? 'TRADING ON' : 'TRADING OFF', enabled, enabled ? 'green' : 'amber')}
          ${_ajPill(live ? 'LIVE ENABLED' : 'PAPER ONLY', !live, live ? 'red' : 'green')}
          ${_ajPill('session: ' + _esc(s.session || '—'), true, 'blue')}
          ${halted ? _ajPill('HALTED', true, 'red') : ''}
          ${_ajPill('chain ' + ((s.audit_chain || {}).ok ? 'OK' : 'BROKEN'), (s.audit_chain || {}).ok, (s.audit_chain || {}).ok ? 'green' : 'red')}
        </div>
      </div>
      <div class="aj-hero-right">
        <div class="aj-hero-pnl">
          <div class="muted" style="font-size:10px;letter-spacing:.05em">CUMULATIVE P&L</div>
          <div class="aj-hero-pnl-val" style="color:${cumPos>=0?'var(--green)':'var(--red)'}">${_ajMoney(cumPos)}</div>
        </div>
        <div class="aj-hero-actions">
          <button class="btn btn-sm" id="aj-run">▶ RUN CYCLE</button>
          <button class="btn btn-sm" id="aj-kill" style="background:var(--red);color:#fff;border-color:var(--red)">■ KILL</button>
          ${halted ? '<button class="btn btn-sm" id="aj-rearm">↻ RE-ARM</button>' : ''}
          <button class="btn btn-sm btn-ghost" id="aj-refresh" title="Refresh">↻</button>
        </div>
      </div>
    </div>

    <div class="aj-tabs">
      <button class="aj-tab active" data-pane="dashboard">Dashboard</button>
      <button class="aj-tab" data-pane="positions">Positions</button>
      <button class="aj-tab" data-pane="council">Council</button>
      <button class="aj-tab" data-pane="config">Config</button>
      <button class="aj-tab" data-pane="analytics">Analytics</button>
      <button class="aj-tab" data-pane="insights">Insights</button>
      <button class="aj-tab" data-pane="activity">Activity</button>
    </div>

    <div class="aj-pane active" data-pane="dashboard">
      ${enabled ? '' : '<div class="panel" style="border-left:3px solid var(--amber)"><div class="panel-body muted" style="font-size:12px">Fail-closed: set an allowlist + caps in <b>Config</b> to trade (paper).</div></div>'}
      <div class="kpi-grid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px">
        <div class="panel"><div class="panel-body"><div class="muted">Day P&L (paper)</div><div style="font-size:22px;color:${(pnl.day_pnl||0)>=0?'var(--green)':'var(--red)'}">${_ajMoney(pnl.day_pnl)}</div></div></div>
        <div class="panel"><div class="panel-body"><div class="muted">Cumulative P&L</div><div style="font-size:22px;color:${(cum.total||0)>=0?'var(--green)':'var(--red)'}">${_ajMoney(cum.total)}</div></div></div>
        <div class="panel"><div class="panel-body"><div class="muted">Positions</div><div style="font-size:22px" id="aj-kpi-positions">—</div></div></div>
        <div class="panel"><div class="panel-body"><div class="muted">Win rate</div><div style="font-size:22px" id="aj-kpi-winrate">—</div></div></div>
        <div class="panel"><div class="panel-body"><div class="muted">Open orders</div><div style="font-size:22px">${_esc(String(orders.open || 0))}</div></div></div>
        <div class="panel"><div class="panel-body"><div class="muted">Fill rate</div><div style="font-size:22px">${orders.fill_rate == null ? '—' : Math.round(orders.fill_rate * 100) + '%'}</div></div></div>
      </div>
      <div class="panel"><div class="panel-header"><span class="panel-title">EQUITY CURVE</span></div><div class="panel-body" style="height:240px"><canvas id="aj-equity-chart"></canvas></div></div>
      <div class="panel"><div class="panel-header"><span class="panel-title">ALERTS</span></div><div class="panel-body">${alerts}</div></div>
    </div>

    <div class="aj-pane" data-pane="positions">
      <div class="panel"><div class="panel-header"><span class="panel-title">◉ AGENT PAPER POSITIONS</span> <span class="muted" id="aj-pos-summary" style="font-size:11px"></span></div><div class="panel-body" id="aj-positions-panel"><div class="muted">loading…</div></div></div>
    </div>

    <div class="aj-pane" data-pane="council">
      <div class="panel" style="border-left:3px solid var(--amber)"><div class="panel-body muted" style="font-size:12px">The <b>Analyst Council</b> is <b>advisory only</b> — it never trades and never overrides the risk gate. It's double-gated (council_enabled + VERIFY-COUNCIL) and runs only when you ask. Running a council may incur LLM cost.</div></div>
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◉ ANALYST COUNCIL</span></div>
        <div class="panel-body">
          <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
            <input id="aj-council-symbol" placeholder="Symbol (e.g. NVDA)" style="max-width:160px;text-transform:uppercase">
            <button class="btn btn-sm" id="aj-council-run">▶ Run Council</button>
            <span class="muted" id="aj-council-status" style="font-size:11px"></span>
          </div>
          <div id="aj-council-result"></div>
        </div>
      </div>
      <div class="panel"><div class="panel-header"><span class="panel-title">RECENT DECISIONS</span></div><div class="panel-body" id="aj-council-recent"><div class="muted">loading…</div></div></div>
    </div>

    <div class="aj-pane" data-pane="analytics">
      <div class="panel"><div class="panel-header"><span class="panel-title">ANALYTICS</span></div><div class="panel-body" id="aj-analytics"><div class="muted">loading…</div></div></div>
    </div>

    <div class="aj-pane" data-pane="insights">
      <div id="aj-insights-root"><div class="muted">loading insights…</div></div>
    </div>

    <div class="aj-pane" data-pane="activity">
      <div class="panel"><div class="panel-header"><span class="panel-title">RECENT PROPOSALS</span></div><div class="panel-body" id="aj-proposals"><div class="muted">loading…</div></div></div>
      <div class="panel"><div class="panel-header"><span class="panel-title">RECENT ORDERS</span></div><div class="panel-body" id="aj-orders"><div class="muted">loading…</div></div></div>
    </div>

    <div class="aj-pane" data-pane="config">
    ${(() => {
      const yn = (id, v, yes, no) => `<select id="${id}"><option value="true"${v?' selected':''}>${yes||'Yes'}</option><option value="false"${!v?' selected':''}>${no||'No'}</option></select>`;
      const num = (id, v, step, unit) => `<div class="aj-input-wrap${unit?' aj-has-unit':''}"><input id="${id}" type="number"${step?` step="${step}"`:''} value="${_esc(String(v == null ? '' : v))}">${unit?`<span class="aj-input-unit">${unit}</span>`:''}</div>`;
      const txt = (id, v, ph) => `<input id="${id}" value="${_esc(String(v == null ? '' : v))}"${ph?` placeholder="${ph}"`:''}>`;
      const sess = (cfg.session_whitelist || ['regular']).join(', ');
      // One labelled field: title, control, and a plain-language hint.
      const f = (label, control, hint) => `<label class="aj-field"><span class="aj-field-label">${label}</span>${control}${hint?`<span class="aj-field-hint">${hint}</span>`:''}</label>`;
      // A collapsible group. `open` groups show by default; advanced ones start
      // collapsed so the page isn't a wall of inputs.
      const group = (title, desc, open, fields) =>
        `<details class="aj-cfg-group"${open?' open':''}><summary><span class="aj-cfg-grp-title">${title}</span><span class="aj-cfg-grp-desc">${desc}</span><span class="aj-cfg-grp-chev">▾</span></summary><div class="aj-cfg-fields">${fields}</div></details>`;
      return `
      <div class="aj-cfg">
        <div class="aj-cfg-intro muted">All settings affect the agent's <b>paper</b> book only. Pick a preset to set sensible defaults, then fine-tune anything below. Changes apply when you press <b>Save</b>.</div>

        <div class="aj-cfg-presets">
          <div class="aj-cfg-presets-label">Quick setup — apply a risk profile:</div>
          <div class="aj-cfg-preset-row">
            <button class="btn aj-preset" data-p="conservative">🛡 Conservative<small>small size · tight stops · few trades</small></button>
            <button class="btn aj-preset" data-p="moderate">⚖ Moderate<small>balanced size · standard stops</small></button>
            <button class="btn aj-preset" data-p="aggressive">🚀 Aggressive<small>larger size · wider stops · more trades</small></button>
          </div>
        </div>

        ${group('Master switch & universe', 'What the agent is allowed to trade', true,
          f('Trading', yn('aj-cfg-trading_enabled', enabled, 'On (paper)', 'Off'), 'Master switch. When off, the agent never trades — no matter the other settings.') +
          f('Symbol allowlist', txt('aj-cfg-symbol_allowlist', allowlist, 'NVDA, AAPL, …'), 'Comma-separated tickers it may trade. The only universe unless “open universe” is on.') +
          f('Open universe', yn('aj-cfg-allow_any_symbol', cfg.allow_any_symbol, 'Yes — any quotable', 'No — allowlist only'), 'Let it trade any quotable symbol, not just the allowlist above.') +
          f('Scan limit (open mode)', num('aj-cfg-scan_universe_max', cfg.scan_universe_max, null, 'symbols'), 'In open-universe mode, how many candidates to scan each cycle.')
        )}

        ${group('🌐 Universe / screener', 'How wide a net the agent casts for candidates', false,
          f('Universe', `<select id="aj-cfg-universe_mode"><option value="allowlist"${cfg.universe_mode!=='open'&&cfg.universe_mode!=='market_screen'?' selected':''}>Allowlist (only your tickers)</option><option value="open"${cfg.universe_mode==='open'?' selected':''}>Open (any quotable)</option><option value="market_screen"${cfg.universe_mode==='market_screen'?' selected':''}>Market screen (auto-shortlist)</option></select>`, 'Allowlist trades only your tickers; open lets it trade anything quotable; market-screen builds a shortlist by liquidity & size each cycle.') +
          f('Include crypto', yn('aj-cfg-include_crypto', cfg.include_crypto), 'Add liquid crypto names to the candidate universe.') +
          f('Top-N crypto', num('aj-cfg-crypto_universe_top', cfg.crypto_universe_top, null, 'names'), 'How many of the largest crypto names to include.') +
          f('Screen shortlist size', num('aj-cfg-screen_max', cfg.screen_max, null, 'names'), 'Market-screen: how many names to keep on the shortlist.') +
          f('Per-cycle scan batch', num('aj-cfg-screen_scan_batch', cfg.screen_scan_batch, null, 'names'), 'Market-screen: how many names to scan each cycle.') +
          f('Min price $', num('aj-cfg-screen_min_price', cfg.screen_min_price, '0.5', '$'), 'Screen out names trading below this price.') +
          f('Min $ volume', num('aj-cfg-screen_min_dollar_volume', cfg.screen_min_dollar_volume, null, '$'), 'Require at least this much daily dollar-volume (liquidity).') +
          f('Min market cap $', num('aj-cfg-screen_min_market_cap', cfg.screen_min_market_cap, null, '$'), 'Screen out names smaller than this market cap.')
        )}

        ${group('📑 Options (paper)', 'Let the agent trade long calls/puts on its signals — paper only', false,
          f('Trade options (long calls/puts, paper)', yn('aj-cfg-trade_options', cfg.trade_options), 'When on, the agent may express a signal as a long call or put instead of stock. Paper only.') +
          f('Option target DTE', num('aj-cfg-option_target_dte', cfg.option_target_dte, null, 'days'), 'Aim for a contract roughly this many days to expiration.') +
          f('Option moneyness (0=ATM)', num('aj-cfg-option_moneyness', cfg.option_moneyness, '0.01'), 'Strike offset from spot: 0=at-the-money, positive=out-of-the-money.') +
          f('Contract multiplier', num('aj-cfg-option_contract_multiplier', cfg.option_contract_multiplier, null, '×'), 'Shares per contract (standard US equity options = 100).')
        )}

        ${group('🧠 Analyst Council (advisory)', 'A panel of analysts that debate a name — advisory only, never trades, double-gated', false,
          f('Council enabled', yn('aj-cfg-council_enabled', cfg.council_enabled), 'Master switch for the council. Off by default; also needs the VERIFY-COUNCIL gate to actually run.') +
          f('Policy', `<select id="aj-cfg-council_policy"><option value="advisory"${cfg.council_policy!=='confirm'&&cfg.council_policy!=='coequal'?' selected':''}>Advisory (read-only)</option><option value="confirm"${cfg.council_policy==='confirm'?' selected':''}>Confirm (must agree)</option><option value="coequal"${cfg.council_policy==='coequal'?' selected':''}>Co-equal</option></select>`, 'How much weight the council carries. The risk gate is always the final authority — the council can never push an order past a cap or unblock a gate.') +
          f('Analysts polled (top-K)', num('aj-cfg-council_topk', cfg.council_topk, null, 'analysts'), 'How many analyst viewpoints to include in the debate.') +
          f('Max research rounds', num('aj-cfg-max_research_rounds', cfg.max_research_rounds, null, 'rounds'), 'Debate rounds among the research analysts.') +
          f('Max risk rounds', num('aj-cfg-max_risk_rounds', cfg.max_risk_rounds, null, 'rounds'), 'Debate rounds for the risk review.') +
          f('Fundamental analyst', yn('aj-cfg-council_analyst_fundamental', cfg.council_analyst_fundamental), 'Include the fundamentals viewpoint.') +
          f('Technical analyst', yn('aj-cfg-council_analyst_technical', cfg.council_analyst_technical), 'Include the price/technical viewpoint.') +
          f('Sentiment analyst', yn('aj-cfg-council_analyst_sentiment', cfg.council_analyst_sentiment), 'Include the news/sentiment viewpoint.') +
          f('Macro analyst', yn('aj-cfg-council_analyst_macro', cfg.council_analyst_macro), 'Include the macro/regime viewpoint.') +
          f('Analyst personas', yn('aj-cfg-personas_enabled', cfg.personas_enabled), 'Give each analyst a distinct persona/voice in the debate.')
        )}

        ${group('Risk limits', 'Hard guardrails — the agent fails closed if any are unset', true,
          f('Max order size', num('aj-cfg-max_order_notional_usd', cfg.max_order_notional_usd, null, '$'), 'Largest single order, in dollars.') +
          f('Max trades / day', num('aj-cfg-max_trades_per_day', cfg.max_trades_per_day, null, 'trades'), 'Hard daily cap across all symbols.') +
          f('Daily loss → HALT', num('aj-cfg-max_daily_loss_usd', cfg.max_daily_loss_usd, null, '$'), 'If the day’s loss hits this, the agent halts itself.') +
          f('Loss counts', `<select id="aj-cfg-daily_loss_basis"><option value="realized_plus_unrealized"${cfg.daily_loss_basis!=='realized'?' selected':''}>Realized + unrealized</option><option value="realized"${cfg.daily_loss_basis==='realized'?' selected':''}>Realized only</option></select>`, 'Whether open-position paper losses count toward the halt trigger.') +
          f('Re-arm after halt', `<select id="aj-cfg-halt_rearm"><option value="manual"${cfg.halt_rearm!=='session_open'?' selected':''}>Manually</option><option value="session_open"${cfg.halt_rearm==='session_open'?' selected':''}>Next session open</option></select>`, 'How a halt clears — you re-arm it, or it auto-clears next session.') +
          f('Trading sessions', txt('aj-cfg-session_whitelist', sess, 'regular, premarket'), 'Which sessions it may trade in (regular, premarket, afterhours).') +
          f('Max open positions', num('aj-cfg-max_open_positions', cfg.max_open_positions, null, 'positions'), 'Most names it can hold at once.') +
          f('Max weight / name', num('aj-cfg-max_symbol_weight_pct', cfg.max_symbol_weight_pct, '0.5', '%'), 'Cap on any single position’s share of the book.') +
          f('Max trades / name / day', num('aj-cfg-max_trades_per_symbol_per_day', cfg.max_trades_per_symbol_per_day, null, 'trades'), 'Limits churn in one symbol per day.')
        )}

        ${group('How it decides (entry signals)', 'When the agent chooses to buy or sell', false,
          f('Forecast horizon', num('aj-cfg-forecast_horizon_days', cfg.forecast_horizon_days, null, 'days'), 'Trading-day horizon the forecast targets.') +
          f('Buy when P(up) ≥', num('aj-cfg-buy_prob_threshold', cfg.buy_prob_threshold, '0.01'), 'Buy only when the forecast probability of going up clears this (0–1).') +
          f('Sell when P(up) ≤', num('aj-cfg-sell_prob_threshold', cfg.sell_prob_threshold, '0.01'), 'Exit when the up-probability falls to this or below (0–1).') +
          f('Minimum edge', num('aj-cfg-min_edge_pct_pts', cfg.min_edge_pct_pts, '0.5', 'pts'), 'Required expected edge before acting, in percentage points.') +
          f('Skip buys above VIX', num('aj-cfg-risk_off_vix', cfg.risk_off_vix, '0.5', 'VIX'), 'Pause new buys when market fear (VIX) is above this level.') +
          f('LLM thesis', yn('aj-cfg-use_llm_synthesis', cfg.use_llm_synthesis), 'Use the language model to write each trade’s rationale.')
        )}

        ${group('Position sizing', 'How big each trade is', false,
          f('Target order size', num('aj-cfg-order_notional_target_usd', cfg.order_notional_target_usd, null, '$'), 'Dollars per order. 0 = use half the max-order size above.') +
          f('Conviction sizing', yn('aj-cfg-conviction_sizing', cfg.conviction_sizing), 'Scale order size up when the forecast is more confident.') +
          f('Minimum order size', num('aj-cfg-min_order_notional_usd', cfg.min_order_notional_usd, null, '$'), 'Never place an order smaller than this.')
        )}

        ${group('Exits (protect profits, cap losses)', 'When to automatically close a position', false,
          f('Take-profit', num('aj-cfg-take_profit_pct', cfg.take_profit_pct, '0.5', '%'), 'Auto-sell a winner once it’s up this much. 0 = off.') +
          f('Stop-loss', num('aj-cfg-stop_loss_pct', cfg.stop_loss_pct, '0.5', '%'), 'Auto-sell a loser once it’s down this much. 0 = off.') +
          f('Trailing stop', num('aj-cfg-trailing_stop_pct', cfg.trailing_stop_pct, '0.5', '%'), 'Exit if price falls this far from its peak. 0 = off.') +
          f('Re-entry cooldown', num('aj-cfg-exit_cooldown_min', cfg.exit_cooldown_min, null, 'min'), 'Wait this long before buying a name again after exiting it.')
        )}

        ${group('Order placement & execution', 'How orders are sent and filled', false,
          f('Broker', txt('aj-cfg-default_broker', cfg.default_broker || 'paper'), 'Execution venue. “paper” = simulated fills, no real money.') +
          f('Auto-approve (paper)', yn('aj-cfg-auto_approve_paper', cfg.auto_approve_paper), 'Place paper proposals automatically, without a manual confirm step.') +
          f('Entry order type', `<select id="aj-cfg-entry_order_type"><option value="market"${cfg.entry_order_type!=='limit'?' selected':''}>Market (fill now)</option><option value="limit"${cfg.entry_order_type==='limit'?' selected':''}>Limit (price-protected)</option></select>`, 'Market fills immediately; limit waits for your price.') +
          f('Limit offset', num('aj-cfg-entry_limit_offset_bps', cfg.entry_limit_offset_bps, '1', 'bps'), 'How far from the quote to place a limit order, in basis points.') +
          f('Order time-to-live', num('aj-cfg-order_ttl_cycles', cfg.order_ttl_cycles, null, 'cycles'), 'Cancel an unfilled order after this many cycles.') +
          f('Max slippage', num('aj-cfg-max_slippage_bps', cfg.max_slippage_bps, '0.5', 'bps'), 'Reject a fill if slippage exceeds this (basis points).') +
          f('Paper slippage', num('aj-cfg-paper_slippage_bps', cfg.paper_slippage_bps, '0.5', 'bps'), 'Simulated slippage added to paper fills, for realism.') +
          f('Paper spread crossed', num('aj-cfg-paper_spread_fraction', cfg.paper_spread_fraction, '0.1'), 'Fraction of the bid/ask spread paid on a paper fill (0–1).') +
          f('Skip after open', num('aj-cfg-trade_skip_open_min', cfg.trade_skip_open_min, null, 'min'), 'Avoid the volatile first N minutes after the open.') +
          f('Skip before close', num('aj-cfg-trade_skip_close_min', cfg.trade_skip_close_min, null, 'min'), 'Avoid the last N minutes before the close.') +
          f('Dry-run', yn('aj-cfg-dry_run', cfg.dry_run), 'Preview proposals only — place no orders at all.') +
          f('Notify on fills', yn('aj-cfg-notify_fills', cfg.notify_fills), 'Show a macOS notification each time an order fills.')
        )}

        ${group('⚡ 100x — smart sizing', 'Size bigger when the edge is real, smaller when it isn’t', false,
          f('Kelly sizing', yn('aj-cfg-kelly_sizing', cfg.kelly_sizing), 'Size by the math of optimal growth — your realized win-rate & payoff.') +
          f('Kelly fraction', num('aj-cfg-kelly_fraction', cfg.kelly_fraction, '0.05'), 'Fraction of full Kelly to bet (0.5 = half-Kelly, safer).') +
          f('Volatility target', num('aj-cfg-volatility_target_pct', cfg.volatility_target_pct, '0.1', '%/day'), 'Aim each position at this daily-volatility budget; 0=off.') +
          f('Compound sizing', yn('aj-cfg-compound_sizing', cfg.compound_sizing), 'Grow order size as the agent’s equity grows.') +
          f('Compounding base', num('aj-cfg-compound_base_equity_usd', cfg.compound_base_equity_usd, null, '$'), 'Equity baseline the compounding ratio is measured against.') +
          f('Per-symbol weighting', yn('aj-cfg-symbol_performance_weighting', cfg.symbol_performance_weighting), 'Lean into names you’ve won on; fade chronic losers.') +
          f('Drawdown throttle', num('aj-cfg-drawdown_throttle_pct', cfg.drawdown_throttle_pct, '0.5', '%'), 'Shrink size as drawdown deepens; fully off at this %. 0=off.')
        )}

        ${group('⚡ 100x — entry alpha', 'Only take the highest-quality setups', false,
          f('Momentum filter', num('aj-cfg-momentum_filter_days', cfg.momentum_filter_days, null, 'd SMA'), 'Only buy when price is above its N-day average. 0=off.') +
          f('RSI cap', num('aj-cfg-mean_reversion_rsi_max', cfg.mean_reversion_rsi_max, '0.5'), 'Don’t chase — only buy when RSI(14) is at/below this. 0=off.') +
          f('Relative strength', yn('aj-cfg-relative_strength_filter', cfg.relative_strength_filter), 'Only buy names outperforming SPY over the lookback.') +
          f('RS lookback', num('aj-cfg-relative_strength_lookback_days', cfg.relative_strength_lookback_days, null, 'days'), 'Window for the relative-strength comparison.') +
          f('Max correlation', num('aj-cfg-max_book_correlation', cfg.max_book_correlation, '0.05'), 'Block a buy too correlated with the book (0–1). 0=off.') +
          f('Earnings blackout', num('aj-cfg-earnings_blackout_days', cfg.earnings_blackout_days, null, 'days'), 'Skip buys within N days of earnings. 0=off.') +
          f('Max sector weight', num('aj-cfg-max_sector_weight_pct', cfg.max_sector_weight_pct, '0.5', '%'), 'Cap any one GICS sector’s share of the book. 0=off.')
        )}

        ${group('⚡ 100x — smart exits', 'Lock in winners, cut losers automatically', false,
          f('Max holding days', num('aj-cfg-max_holding_days', cfg.max_holding_days, null, 'days'), 'Force-exit a position that hasn’t worked after N days. 0=off.') +
          f('Profit ratchet', num('aj-cfg-profit_ratchet_pct', cfg.profit_ratchet_pct, '0.5', '%'), 'Once up this much, protect a floor of gains. 0=off.') +
          f('Ratchet lock', num('aj-cfg-profit_ratchet_lock_pct', cfg.profit_ratchet_lock_pct, '0.5', '%'), 'The floor gain the ratchet exits at if price falls back.') +
          f('TP ladder', yn('aj-cfg-tp_ladder', cfg.tp_ladder), 'Scale out in thirds at take-profit, 1.5×, and 2× instead of all at once.') +
          f('ATR stop', num('aj-cfg-atr_stop_mult', cfg.atr_stop_mult, '0.1', '×ATR'), 'Volatility-aware stop: exit at mult × ATR below entry. 0=off.') +
          f('ATR period', num('aj-cfg-atr_period', cfg.atr_period, null, 'bars'), 'Lookback for the Average True Range.')
        )}

        ${group('⚡ 100x — adaptive brain', 'The agent tunes itself to results & regime', false,
          f('Adaptive thresholds', yn('aj-cfg-adaptive_thresholds', cfg.adaptive_thresholds), 'Tighten/loosen entry thresholds from the recent realized hit-rate.') +
          f('Regime adaptive', yn('aj-cfg-regime_adaptive', cfg.regime_adaptive), 'Shift profile on bull / bear / chop (detected from SPY).') +
          f('Pyramiding', yn('aj-cfg-pyramiding', cfg.pyramiding), 'Allow disciplined adds to a winning position.') +
          f('Pyramid max adds', num('aj-cfg-pyramid_max_adds', cfg.pyramid_max_adds, null, 'adds'), 'How many times you can add to one position.') +
          f('Pyramid min gain', num('aj-cfg-pyramid_min_gain_pct', cfg.pyramid_min_gain_pct, '0.5', '%'), 'A position must be up this much before adding.') +
          f('Signal scorecard', yn('aj-cfg-signal_scorecard', cfg.signal_scorecard), 'Track realized win-rate by entry conviction.') +
          f('Opportunity radar', yn('aj-cfg-opportunity_radar', cfg.opportunity_radar), 'Rank the universe each cycle and trade only the best setups.') +
          f('Radar top-K', num('aj-cfg-opportunity_radar_top_k', cfg.opportunity_radar_top_k, null, 'names'), 'How many top-ranked names the radar keeps.')
        )}

        ${group('⚡ 100x — autonomy', 'Let the agent run itself (paper)', false,
          f('Auto-run', yn('aj-cfg-auto_run_enabled', cfg.auto_run_enabled), 'Run cycles automatically during market hours.') +
          f('Auto-run interval', num('aj-cfg-auto_run_interval_min', cfg.auto_run_interval_min, null, 'min'), 'Minutes between automatic cycles.') +
          f('Align to clock', yn('aj-cfg-auto_run_align_to_clock', cfg.auto_run_align_to_clock), 'Fire on wall-clock marks (:00, :10, :20…) so runs land on the expected minute. Off = interval measured from the previous run.') +
          f('Health auto-halt', yn('aj-cfg-health_autohalt', cfg.health_autohalt), 'Self-halt on fill-rate collapse, divergence, or a broken audit chain.') +
          f('Preset escalation', yn('aj-cfg-auto_preset_escalation', cfg.auto_preset_escalation), 'Earn your way up conservative→moderate→aggressive; step down on drawdown.') +
          f('Daily reflection', yn('aj-cfg-daily_reflection', cfg.daily_reflection), 'Write an end-of-day self-review of what worked.') +
          f('Pre-market briefing', yn('aj-cfg-premarket_briefing', cfg.premarket_briefing), 'Build a ranked “what to watch” list before the open.')
        )}

        ${group('🎯 Effectiveness — better buys & sells', 'Richer signals, pick the best names, trade smarter (all opt-in)', false,
          f('Multi-factor signals', yn('aj-cfg-multi_factor_signals', cfg.multi_factor_signals), 'Fuse smart-money / insider / congressional / social signals into the forecast — orthogonal to price.') +
          f('Validate before trusting', yn('aj-cfg-signal_ic_gate', cfg.signal_ic_gate), 'A new signal only earns weight after it proves realized skill (IC/Brier). Strongly recommended ON.') +
          f('Regime-aware weighting', yn('aj-cfg-regime_conditional_weights', cfg.regime_conditional_weights), 'Trust momentum in trends, mean-reversion in chop.') +
          f('Cross-sectional selection', yn('aj-cfg-cross_sectional_selection', cfg.cross_sectional_selection), 'Trade the best N relative opportunities, not anything above a bar.') +
          f('Top-N', num('aj-cfg-cross_sectional_top_n', cfg.cross_sectional_top_n, null, 'names'), 'How many of the best ranked names to trade per cycle.') +
          f('Cost gate', yn('aj-cfg-cost_gate', cfg.cost_gate), 'Skip trades whose edge won’t survive spread + fees.') +
          f('Limit entries', yn('aj-cfg-limit_entry', cfg.limit_entry), 'Post a limit through the mid instead of paying the full spread.') +
          f('Time-stop (days)', num('aj-cfg-time_stop_days', cfg.time_stop_days, null, 'days'), 'Exit a stagnant thesis after N days (0 = off).') +
          f('Earnings blackout (days)', num('aj-cfg-event_blackout_days', cfg.event_blackout_days, null, 'days'), 'Don’t open new positions within N days of earnings (0 = off).') +
          f('Profit ladder', yn('aj-cfg-profit_ladder', cfg.profit_ladder), 'Scale out at gain rungs.') +
          f('Portfolio construction', yn('aj-cfg-portfolio_construction', cfg.portfolio_construction), 'Size by risk-aware allocation across the book (caps only shrink).') +
          f('Allocation method', `<select id="aj-cfg-alloc_method"><option value="risk_parity"${cfg.alloc_method!=='max_sharpe'&&cfg.alloc_method!=='equal'?' selected':''}>Risk parity</option><option value="max_sharpe"${cfg.alloc_method==='max_sharpe'?' selected':''}>Max Sharpe</option><option value="equal"${cfg.alloc_method==='equal'?' selected':''}>Equal weight</option></select>`, 'How target weights are computed when portfolio construction is on.')
        )}

        <div class="aj-cfg-savebar">
          <button class="btn" id="aj-save-cfg">Save config</button>
          <span class="muted aj-cfg-savenote">Live trading isn’t editable here — it’s gated behind the CLI + VERIFY checks.</span>
        </div>
      </div>`;
    })()}
    </div>
  `;

  // tab switching
  let councilRecentLoaded = false;
  el.querySelectorAll('.aj-tab').forEach(t => t.addEventListener('click', () => {
    const pane = t.getAttribute('data-pane');
    el.querySelectorAll('.aj-tab').forEach(x => x.classList.toggle('active', x === t));
    el.querySelectorAll('.aj-pane').forEach(p => p.classList.toggle('active', p.getAttribute('data-pane') === pane));
    // Lazy-load recent council decisions only the first time the Council tab is opened.
    if (pane === 'council' && !councilRecentLoaded) { councilRecentLoaded = true; councilLoadRecent(); }
    // Insights tab — render the 10 visualization panels (re-render each open so
    // the data is fresh; AJInsights.render is idempotent + destroys old charts).
    if (pane === 'insights' && window.AJInsights) {
      try { window.AJInsights.render(document.getElementById('aj-insights-root')); }
      catch (e) { /* never break the tab */ }
    }
  }));
  const refreshBtn = document.getElementById('aj-refresh');
  if (refreshBtn) refreshBtn.addEventListener('click', () => loadTradingView());

  // wire actions
  const killBtn = document.getElementById('aj-kill');
  if (killBtn) killBtn.addEventListener('click', async () => {
    if (!(await _ajConfirm('KILL SWITCH: disable trading and cancel open orders now?'))) return;
    try {
      const r = await API.post('/api/aj/kill', { reason: 'kill (UI)' });
      _ajToast('◉ Kill switch engaged — trading disabled' + (r && r.still_open ? ' (' + r.still_open + ' still open!)' : ''), r && r.still_open ? false : true);
      loadTradingView();
    } catch (e) { _ajToast('Kill failed: ' + e.message, false); }
  });
  const runBtn = document.getElementById('aj-run');
  if (runBtn) runBtn.addEventListener('click', async () => {
    if (!(await _ajConfirm('Run one PAPER operator cycle now? It may place paper trades if configured.'))) return;
    runBtn.disabled = true; runBtn.textContent = '… running';
    try {
      const r = await API.post('/api/aj/run', { mode: 'paper' });
      const props = r.proposals || [];
      const executed = props.filter(p => p && p.result === 'executed').length;
      _ajToast('◉ Cycle complete: ' + executed + ' executed / ' + props.length + ' scanned');
      loadTradingView();
    } catch (e) { _ajToast('Run failed: ' + (e.message || e), false); runBtn.disabled = false; runBtn.textContent = '▶ RUN CYCLE'; }
  });
  const rearmBtn = document.getElementById('aj-rearm');
  if (rearmBtn) rearmBtn.addEventListener('click', async () => {
    try { await API.post('/api/aj/rearm', {}); _ajToast('◉ Re-armed'); loadTradingView(); }
    catch (e) { _ajToast('Re-arm failed: ' + e.message, false); }
  });
  const saveBtn = document.getElementById('aj-save-cfg');
  if (saveBtn) saveBtn.addEventListener('click', async () => {
    // Defensive readers — a missing field is skipped, never throws the save.
    const el = (id) => document.getElementById(id);
    const vBool = (id) => { const e = el(id); return e ? (e.value === 'true') : undefined; };
    // Blank field => undefined (omitted from the save, leaves the stored value
    // unchanged) rather than 0 — so clearing a threshold field can't silently
    // set buy/sell/min-edge to 0 ("trade on everything").
    const vNum = (id) => { const e = el(id); if (!e) return undefined; const t = e.value.trim(); if (t === '') return undefined; const n = Number(t); return isFinite(n) ? n : undefined; };
    const vStr = (id) => { const e = el(id); return e ? e.value.trim() : undefined; };
    const vList = (id) => { const e = el(id); return e ? e.value.split(',').map(x => x.trim()).filter(Boolean) : undefined; };
    const raw = {
      trading_enabled: vBool('aj-cfg-trading_enabled'),
      symbol_allowlist: vList('aj-cfg-symbol_allowlist'),
      allow_any_symbol: vBool('aj-cfg-allow_any_symbol'),
      scan_universe_max: vNum('aj-cfg-scan_universe_max'),
      // universe / screener
      universe_mode: vStr('aj-cfg-universe_mode'),
      include_crypto: vBool('aj-cfg-include_crypto'),
      crypto_universe_top: vNum('aj-cfg-crypto_universe_top'),
      screen_max: vNum('aj-cfg-screen_max'),
      screen_scan_batch: vNum('aj-cfg-screen_scan_batch'),
      screen_min_price: vNum('aj-cfg-screen_min_price'),
      screen_min_dollar_volume: vNum('aj-cfg-screen_min_dollar_volume'),
      screen_min_market_cap: vNum('aj-cfg-screen_min_market_cap'),
      // options (paper)
      trade_options: vBool('aj-cfg-trade_options'),
      option_target_dte: vNum('aj-cfg-option_target_dte'),
      option_moneyness: vNum('aj-cfg-option_moneyness'),
      option_contract_multiplier: vNum('aj-cfg-option_contract_multiplier'),
      max_order_notional_usd: vNum('aj-cfg-max_order_notional_usd'),
      max_trades_per_day: vNum('aj-cfg-max_trades_per_day'),
      max_daily_loss_usd: vNum('aj-cfg-max_daily_loss_usd'),
      daily_loss_basis: vStr('aj-cfg-daily_loss_basis'),
      halt_rearm: vStr('aj-cfg-halt_rearm'),
      session_whitelist: vList('aj-cfg-session_whitelist'),
      forecast_horizon_days: vNum('aj-cfg-forecast_horizon_days'),
      buy_prob_threshold: vNum('aj-cfg-buy_prob_threshold'),
      sell_prob_threshold: vNum('aj-cfg-sell_prob_threshold'),
      min_edge_pct_pts: vNum('aj-cfg-min_edge_pct_pts'),
      order_notional_target_usd: vNum('aj-cfg-order_notional_target_usd'),
      use_llm_synthesis: vBool('aj-cfg-use_llm_synthesis'),
      default_broker: vStr('aj-cfg-default_broker') || 'paper',
      auto_approve_paper: vBool('aj-cfg-auto_approve_paper'),
      paper_slippage_bps: vNum('aj-cfg-paper_slippage_bps'),
      paper_spread_fraction: vNum('aj-cfg-paper_spread_fraction'),
      // enhancements
      conviction_sizing: vBool('aj-cfg-conviction_sizing'),
      min_order_notional_usd: vNum('aj-cfg-min_order_notional_usd'),
      entry_order_type: vStr('aj-cfg-entry_order_type'),
      entry_limit_offset_bps: vNum('aj-cfg-entry_limit_offset_bps'),
      order_ttl_cycles: vNum('aj-cfg-order_ttl_cycles'),
      take_profit_pct: vNum('aj-cfg-take_profit_pct'),
      stop_loss_pct: vNum('aj-cfg-stop_loss_pct'),
      trailing_stop_pct: vNum('aj-cfg-trailing_stop_pct'),
      exit_cooldown_min: vNum('aj-cfg-exit_cooldown_min'),
      max_open_positions: vNum('aj-cfg-max_open_positions'),
      max_symbol_weight_pct: vNum('aj-cfg-max_symbol_weight_pct'),
      max_trades_per_symbol_per_day: vNum('aj-cfg-max_trades_per_symbol_per_day'),
      trade_skip_open_min: vNum('aj-cfg-trade_skip_open_min'),
      trade_skip_close_min: vNum('aj-cfg-trade_skip_close_min'),
      max_slippage_bps: vNum('aj-cfg-max_slippage_bps'),
      risk_off_vix: vNum('aj-cfg-risk_off_vix'),
      dry_run: vBool('aj-cfg-dry_run'),
      notify_fills: vBool('aj-cfg-notify_fills'),
      // 100x layer — smart sizing
      kelly_sizing: vBool('aj-cfg-kelly_sizing'),
      kelly_fraction: vNum('aj-cfg-kelly_fraction'),
      volatility_target_pct: vNum('aj-cfg-volatility_target_pct'),
      compound_sizing: vBool('aj-cfg-compound_sizing'),
      compound_base_equity_usd: vNum('aj-cfg-compound_base_equity_usd'),
      symbol_performance_weighting: vBool('aj-cfg-symbol_performance_weighting'),
      drawdown_throttle_pct: vNum('aj-cfg-drawdown_throttle_pct'),
      // entry alpha
      momentum_filter_days: vNum('aj-cfg-momentum_filter_days'),
      mean_reversion_rsi_max: vNum('aj-cfg-mean_reversion_rsi_max'),
      relative_strength_filter: vBool('aj-cfg-relative_strength_filter'),
      relative_strength_lookback_days: vNum('aj-cfg-relative_strength_lookback_days'),
      max_book_correlation: vNum('aj-cfg-max_book_correlation'),
      earnings_blackout_days: vNum('aj-cfg-earnings_blackout_days'),
      max_sector_weight_pct: vNum('aj-cfg-max_sector_weight_pct'),
      // smart exits
      max_holding_days: vNum('aj-cfg-max_holding_days'),
      profit_ratchet_pct: vNum('aj-cfg-profit_ratchet_pct'),
      profit_ratchet_lock_pct: vNum('aj-cfg-profit_ratchet_lock_pct'),
      tp_ladder: vBool('aj-cfg-tp_ladder'),
      atr_stop_mult: vNum('aj-cfg-atr_stop_mult'),
      atr_period: vNum('aj-cfg-atr_period'),
      // adaptive brain
      adaptive_thresholds: vBool('aj-cfg-adaptive_thresholds'),
      regime_adaptive: vBool('aj-cfg-regime_adaptive'),
      pyramiding: vBool('aj-cfg-pyramiding'),
      pyramid_max_adds: vNum('aj-cfg-pyramid_max_adds'),
      pyramid_min_gain_pct: vNum('aj-cfg-pyramid_min_gain_pct'),
      signal_scorecard: vBool('aj-cfg-signal_scorecard'),
      opportunity_radar: vBool('aj-cfg-opportunity_radar'),
      opportunity_radar_top_k: vNum('aj-cfg-opportunity_radar_top_k'),
      // analyst council (advisory)
      council_enabled: vBool('aj-cfg-council_enabled'),
      council_policy: vStr('aj-cfg-council_policy'),
      council_topk: vNum('aj-cfg-council_topk'),
      max_research_rounds: vNum('aj-cfg-max_research_rounds'),
      max_risk_rounds: vNum('aj-cfg-max_risk_rounds'),
      council_analyst_fundamental: vBool('aj-cfg-council_analyst_fundamental'),
      council_analyst_technical: vBool('aj-cfg-council_analyst_technical'),
      council_analyst_sentiment: vBool('aj-cfg-council_analyst_sentiment'),
      council_analyst_macro: vBool('aj-cfg-council_analyst_macro'),
      personas_enabled: vBool('aj-cfg-personas_enabled'),
      // autonomy
      auto_run_enabled: vBool('aj-cfg-auto_run_enabled'),
      auto_run_interval_min: vNum('aj-cfg-auto_run_interval_min'),
      auto_run_align_to_clock: vBool('aj-cfg-auto_run_align_to_clock'),
      health_autohalt: vBool('aj-cfg-health_autohalt'),
      auto_preset_escalation: vBool('aj-cfg-auto_preset_escalation'),
      daily_reflection: vBool('aj-cfg-daily_reflection'),
      premarket_briefing: vBool('aj-cfg-premarket_briefing'),
      // effectiveness layer
      multi_factor_signals: vBool('aj-cfg-multi_factor_signals'),
      signal_ic_gate: vBool('aj-cfg-signal_ic_gate'),
      regime_conditional_weights: vBool('aj-cfg-regime_conditional_weights'),
      cross_sectional_selection: vBool('aj-cfg-cross_sectional_selection'),
      cross_sectional_top_n: vNum('aj-cfg-cross_sectional_top_n'),
      cost_gate: vBool('aj-cfg-cost_gate'),
      limit_entry: vBool('aj-cfg-limit_entry'),
      time_stop_days: vNum('aj-cfg-time_stop_days'),
      event_blackout_days: vNum('aj-cfg-event_blackout_days'),
      profit_ladder: vBool('aj-cfg-profit_ladder'),
      portfolio_construction: vBool('aj-cfg-portfolio_construction'),
      alloc_method: vStr('aj-cfg-alloc_method'),
    };
    const body = {};
    Object.keys(raw).forEach(k => { if (raw[k] !== undefined) body[k] = raw[k]; });
    try { await API.post('/api/aj/config', body); _ajToast('◉ Config saved'); loadTradingView(); }
    catch (e) { _ajToast('Save failed: ' + e.message, false); }
  });

  // tables (best-effort)
  try {
    const [pr, od] = await Promise.all([
      API.get('/api/aj/proposals?limit=10').catch(() => ({ proposals: [] })),
      API.get('/api/aj/orders?limit=10').catch(() => ({ orders: [] })),
    ]);
    const pEl = document.getElementById('aj-proposals');
    if (pEl) {
      const rows = (pr.proposals || []);
      pEl.innerHTML = rows.length ? `<table class="data-table" style="width:100%"><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Status</th><th>Thesis</th></tr></thead><tbody>${rows.map(p =>
        `<tr><td>${_esc(_ajFmtSymbol(p.symbol))}</td><td>${_esc(p.side)}</td><td>${_esc(String(p.qty || ''))}</td><td>${_esc(p.status)}</td><td class="muted" style="font-size:11px">${_esc((p.thesis || p.risk_reason || '').slice(0, 80))}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">No proposals yet — run a cycle.</div>';
    }
    const oEl = document.getElementById('aj-orders');
    if (oEl) {
      const rows = (od.orders || []);
      oEl.innerHTML = rows.length ? `<table class="data-table" style="width:100%"><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>State</th><th>Avg fill</th><th>Mode</th></tr></thead><tbody>${rows.map(o =>
        `<tr><td>${_esc(_ajFmtSymbol(o.symbol))}</td><td>${_esc(o.side)}</td><td>${_esc(String(o.qty || ''))}</td><td>${_esc(o.state)}</td><td>${o.avg_fill_price ? _ajMoney(o.avg_fill_price) : '—'}</td><td>${_esc(o.mode)}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">No orders yet.</div>';
    }
  } catch (e) {}

  // preset buttons
  document.querySelectorAll('.aj-preset').forEach(btn => btn.addEventListener('click', async () => {
    const name = btn.getAttribute('data-p');
    if (!(await _ajConfirm('Apply the "' + name + '" preset? (overwrites risk + strategy config; not your switches/allowlist)'))) return;
    try { await API.post('/api/aj/preset', { name }); _ajToast('◉ Applied ' + name + ' preset'); loadTradingView(); }
    catch (e) { _ajToast('Preset failed: ' + (e.message || e), false); }
  }));

  // analytics panel (best-effort)
  try {
    const a = await API.get('/api/aj/analytics');
    // ── equity chart + dashboard KPIs ──
    const eqRows = a.equity_curve || [];
    if (eqRows.length >= 2 && typeof ChartEngine !== 'undefined' && ChartEngine.createEquityChart) {
      ChartEngine.createEquityChart('aj-equity-chart', eqRows.map(r => r.date), eqRows.map(r => Number(r.equity_usd)));
    } else {
      const cc = document.getElementById('aj-equity-chart');
      if (cc && cc.parentNode) cc.parentNode.innerHTML = '<div class="muted" style="padding:20px;text-align:center">No equity history yet — each cycle adds a point.</div>';
    }
    const ts2 = a.trade_stats || {};
    const kw = document.getElementById('aj-kpi-winrate'); if (kw) kw.textContent = ts2.win_rate == null ? '—' : Math.round(ts2.win_rate * 100) + '%';
    const kp = document.getElementById('aj-kpi-positions'); if (kp) kp.textContent = (a.positions || {}).count != null ? String((a.positions).count) : '—';
    // ── agent paper positions table ──
    const pd = a.positions || {};
    const posRows = pd.positions || [];
    const ppEl = document.getElementById('aj-positions-panel');
    const sumEl = document.getElementById('aj-pos-summary');
    if (sumEl) sumEl.textContent = posRows.length ? (posRows.length + ' positions · MV ' + _ajMoney(pd.total_market_value) + ' · unreal ' + _ajMoney(pd.total_unrealized)) : '';
    if (ppEl) {
      ppEl.innerHTML = posRows.length ? `<table class="data-table" style="width:100%"><thead><tr><th>Symbol</th><th>Qty</th><th>Avg cost</th><th>Price</th><th>Mkt value</th><th>Unreal P&L</th><th>%</th><th>Weight</th><th>Age</th></tr></thead><tbody>${posRows.map(p =>
        `<tr><td><b>${_esc(_ajFmtSymbol(p.symbol))}</b></td><td>${_esc(String(p.qty))}</td><td>${_ajMoney(p.avg_cost)}</td><td>${_ajMoney(p.mark)}</td><td>${_ajMoney(p.market_value)}</td><td style="color:${(p.unrealized||0)>=0?'var(--green)':'var(--red)'}">${_ajMoney(p.unrealized)}</td><td style="color:${(p.unrealized_pct||0)>=0?'var(--green)':'var(--red)'}">${p.unrealized_pct==null?'—':(p.unrealized_pct>=0?'+':'')+_esc(String(p.unrealized_pct))+'%'}</td><td>${p.weight_pct==null?'—':_esc(String(p.weight_pct))+'%'}</td><td>${p.age_days==null?'—':_esc(String(p.age_days))+'d'}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">No paper positions yet — the agent opens positions when it buys (run a cycle during market hours).</div>';
    }
    const aEl = document.getElementById('aj-analytics');
    if (aEl) {
      const ts = a.trade_stats || {}; const sh = a.sharpe || {};
      const attr = (a.attribution || []).slice(0, 8);
      const eq = a.equity_curve || [];
      const lastEq = eq.length ? eq[eq.length - 1].equity_usd : null;
      const statline = `<div style="display:flex;gap:18px;flex-wrap:wrap;margin-bottom:10px">
        <span class="muted">Trades <b style="color:var(--text-primary)">${ts.trades || 0}</b></span>
        <span class="muted">Win rate <b style="color:var(--text-primary)">${ts.win_rate == null ? '—' : Math.round(ts.win_rate * 100) + '%'}</b></span>
        <span class="muted">Profit factor <b style="color:var(--text-primary)">${ts.profit_factor == null ? '—' : fmt.num(ts.profit_factor)}</b></span>
        <span class="muted">Net <b style="color:${(ts.net || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${_ajMoney(ts.net)}</b></span>
        <span class="muted">Sharpe <b style="color:var(--text-primary)">${sh.sharpe == null ? '—' : fmt.num(sh.sharpe)}</b></span>
        <span class="muted">Equity <b style="color:var(--text-primary)">${lastEq == null ? '—' : _ajMoney(lastEq)}</b></span>
      </div>`;
      const attrTable = attr.length ? `<table class="data-table" style="width:100%"><thead><tr><th>Symbol</th><th>Realized</th><th>Unrealized</th><th>Total</th></tr></thead><tbody>${attr.map(d =>
        `<tr><td>${_esc(_ajFmtSymbol(d.symbol))}</td><td>${_ajMoney(d.realized)}</td><td>${_ajMoney(d.unrealized)}</td><td style="color:${(d.total || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${_ajMoney(d.total)}</td></tr>`).join('')}</tbody></table>` : '<div class="muted">No closed trades yet.</div>';
      aEl.innerHTML = statline + attrTable + '<div style="margin-top:8px"><a href="/api/aj/journal.csv" class="btn btn-sm">⬇ Export trade journal (CSV)</a></div>';
    }
  } catch (e) {
    const aEl = document.getElementById('aj-analytics'); if (aEl) aEl.innerHTML = '<div class="muted">Analytics unavailable.</div>';
  }

  // ── Analyst Council (advisory) ──
  let councilBusy = false;
  const councilRun = async () => {
    if (councilBusy) return;
    const symEl = document.getElementById('aj-council-symbol');
    const stEl = document.getElementById('aj-council-status');
    const resEl = document.getElementById('aj-council-result');
    const runBtn = document.getElementById('aj-council-run');
    if (!symEl || !resEl) return;
    const sym = (symEl.value || '').trim().toUpperCase();
    if (!sym) { if (stEl) stEl.textContent = 'Enter a symbol first.'; return; }
    councilBusy = true;
    if (stEl) stEl.textContent = '';
    resEl.innerHTML = '<div class="loading"><div class="spinner"></div> Convening council for ' + _esc(sym) + '…</div>';
    if (runBtn) { runBtn.disabled = true; runBtn.textContent = '… running'; }
    try {
      // Raw fetch so we can read the 403 hint + status (API.get drops the body).
      const r = await fetch('/api/aj/council/' + encodeURIComponent(sym));
      let body = {};
      try { body = await r.json(); } catch (_) {}
      if (r.status === 403) {
        resEl.innerHTML = '<div class="panel" style="border-left:3px solid var(--amber)"><div class="panel-body">'
          + '<div style="font-size:12px;margin-bottom:6px">' + _esc(body.error || 'VERIFY-COUNCIL gate not passed') + '</div>'
          + '<div class="muted" style="font-size:11px">To enable: ' + _esc(body.hint || 'pass VERIFY-COUNCIL') + '</div></div></div>';
        return;
      }
      if (!r.ok) { resEl.innerHTML = '<div class="muted">Council failed: ' + _esc(body.error || ('HTTP ' + r.status)) + '</div>'; return; }
      resEl.innerHTML = _ajRenderCouncil(body);
      councilLoadRecent();
    } catch (e) {
      resEl.innerHTML = '<div class="muted">Council failed: ' + _esc(e.message || String(e)) + '</div>';
    } finally {
      councilBusy = false;
      if (runBtn) { runBtn.disabled = false; runBtn.textContent = '▶ Run Council'; }
    }
  };
  const _ajActionColor = (a) => {
    const s = String(a || '').toLowerCase();
    if (/buy|accumulate|long|add/.test(s)) return '#2ecc71';
    if (/sell|reduce|trim|avoid|short|exit/.test(s)) return '#e74c3c';
    return 'var(--muted, #888)';
  };
  const councilLoadRecent = async () => {
    const rEl = document.getElementById('aj-council-recent');
    if (!rEl) return;
    try {
      const d = await API.get('/api/aj/council/recent');
      const runs = d.runs || [];
      if (!runs.length) { rEl.innerHTML = '<div class="muted">No council decisions yet — run one above.</div>'; return; }
      rEl.innerHTML = `<table class="data-table" style="width:100%"><thead><tr><th>When</th><th>Symbol</th><th>Final decision</th><th>Conviction</th><th>Status</th><th>Thesis</th><th>Cost</th></tr></thead><tbody>${runs.map((r, i) => {
        const act = String(r.action || '—').toUpperCase();
        const rating = (r.rating == null ? '' : ` · rating ${_esc(String(r.rating))}/10`);
        const decided = String(r.status || '').toLowerCase() === 'ok';
        // The "final decision" is the action only when the run actually decided;
        // a degraded/skipped/error run produced no actionable call.
        const decisionCell = decided
          ? `<b style="color:${_ajActionColor(r.action)}">${_esc(act)}</b><span class="muted" style="font-size:11px">${rating}</span>`
          : `<span class="muted">no decision</span>`;
        const thesis = String(r.thesis || '');
        const dissent = String(r.dissent || '');
        const short = thesis.length > 80 ? thesis.slice(0, 80) + '…' : (thesis || '—');
        const hasMore = thesis.length > 80 || !!dissent;
        const toggle = hasMore ? ` <span class="aj-cr-toggle" style="color:var(--accent,#4ea1ff);cursor:pointer;white-space:nowrap">details ▸</span>` : '';
        const detail = `<tr class="aj-cr-detail" id="aj-cr-d-${i}" style="display:none"><td colspan="7" style="background:rgba(255,255,255,0.03)">`
          + `<div style="white-space:pre-wrap;line-height:1.5"><b>Full thesis</b><br>${_esc(thesis || '—')}</div>`
          + (dissent ? `<div style="white-space:pre-wrap;line-height:1.5;margin-top:6px"><b>Dissent</b><br>${_esc(dissent)}</div>` : '')
          + `<div class="muted" style="font-size:11px;margin-top:6px">cycle ${_esc(String(r.cycle_id || '—'))} · ${r.n_calls == null ? '?' : _esc(String(r.n_calls))} LLM call(s)</div>`
          + `</td></tr>`;
        return `<tr class="aj-cr-row" data-idx="${i}" style="cursor:${hasMore ? 'pointer' : 'default'}">`
          + `<td class="muted" style="font-size:11px">${_esc(String(r.ts || '').slice(0, 19))}</td>`
          + `<td><b>${_esc(r.symbol || '')}</b></td>`
          + `<td>${decisionCell}</td>`
          + `<td>${_ajPct(r.conviction)}</td>`
          + `<td>${_esc(String(r.status || '—'))}</td>`
          + `<td class="muted" style="font-size:11px">${_esc(short)}${toggle}</td>`
          + `<td>${r.cost_usd == null ? '—' : _ajMoney(r.cost_usd)}</td></tr>${detail}`;
      }).join('')}</tbody></table>`;
      // delegated expand/collapse
      rEl.querySelectorAll('.aj-cr-row').forEach(row => row.addEventListener('click', () => {
        const det = document.getElementById('aj-cr-d-' + row.getAttribute('data-idx'));
        if (!det) return;
        const open = det.style.display !== 'none';
        det.style.display = open ? 'none' : 'table-row';
        const t = row.querySelector('.aj-cr-toggle');
        if (t) t.textContent = open ? 'details ▸' : 'details ▾';
      }));
    } catch (e) {
      rEl.innerHTML = '<div class="muted">Recent decisions unavailable.</div>';
    }
  };
  const cRunBtn = document.getElementById('aj-council-run');
  if (cRunBtn) cRunBtn.addEventListener('click', councilRun);
  const cSymEl = document.getElementById('aj-council-symbol');
  if (cSymEl) cSymEl.addEventListener('keyup', (e) => { if (e.key === 'Enter') councilRun(); });
  // Recent decisions are lazy-loaded when the Council tab is first opened (see tab handler).
}

const VIEW_LOADERS = {
  overview:     () => loadOverview(),
  // NOTE: 'trading' is intentionally NOT auto-refreshed. The trading view hosts
  // the AJ Config tab with unsaved edits; rebuilding it on the refresh tick
  // would reset to Dashboard and clobber in-progress Config changes.
  portfolio:    () => loadPortfolio(),
  crypto:       () => loadCrypto(),
  watchlist:    () => loadWatchlistView(),
  markets:      () => loadMarkets(),
  macro:        () => loadMacroView(),
  analytics:    () => loadAnalyticsView(),
  intel:        () => loadIntelView(),
  earnings:     () => loadEarningsView(),
  signals:      () => loadSignalsView(),
  'options-flow': () => loadOptionsFlowView(),
  congress:     () => loadCongressView(),
  dividends:    () => loadDividendsView(),
  scanner:      () => loadScannerView(),
  news:         () => loadGlobalNews(),
  alerts:       () => loadAlertsView(),
  gex:          () => loadGexView(),
  contagion:    () => loadContagionView(),
  narrative:    () => loadNarrativeView(),
  reflexivity:  () => loadReflexivityView(),
  liquidity:    () => loadLiquidityView(),
  // v0.2.0 research views — opt them into auto-refresh. Backtest results
  // are cached server-side for 6h so this is cheap; optimizer/montecarlo
  // re-render their own controls and only fire heavy work on user action.
  backtest:     () => loadBacktest(),
  optimizer:    () => loadOptimizer(),
  montecarlo:   () => loadMonteCarloView(),
  'research-lab': () => loadHypothesisLab(),
  // v0.3.0 synthesis views — heavy fan-out scans server-cached for
  // 10min–1h, so the auto-refresh just re-renders against cache.
  catalysts:    () => loadCatalystsView(),
  cluster:      () => loadClusterView(),
  divergences:  () => loadDivergencesView(),
  sectorflow:   () => loadSectorFlowView(),
  // whatif and research are intentionally excluded — re-loading
  // would clobber user-entered candidate parameters / charts.
};

function startAutoRefresh() {
  if (State.refreshInterval) clearInterval(State.refreshInterval);
  const intervalSec = parseInt(State.settings.refresh_interval || 60);
  const interval = intervalSec * 1000;
  // Keep the status-bar indicator honest about what "AUTO" means.
  const nodeEl = document.getElementById('status-node-live');
  if (nodeEl) nodeEl.title = 'Auto-refresh every ' + intervalSec + 's';
  State.refreshInterval = setInterval(() => {
    // Page Visibility: a backgrounded tab does zero network. The
    // visibilitychange handler refreshes the ticker on return, and the
    // next visible tick picks the active view back up.
    if (document.hidden) return;
    try { loadTicker(); } catch(e) {}
    try { loadSidebarWatchlist(); } catch(e) {}
    const loader = VIEW_LOADERS[State.activeView];
    if (loader) {
      try { loader(); } catch(e) { /* one broken view shouldn't kill the tick */ }
    }
  }, interval);
}

// ── Initialisation ────────────────────────────────────────────────────────────
async function init() {
  Progress.init();
  Progress.show(10);
  startClock();

  // Load settings first
  try {
    State.settings = await API.get('/api/settings');
  } catch(e) {}

  // Wire top-level sidebar groups — each navigates to its first sub-item,
  // which lights up the sub-nav strip for that group.
  document.querySelectorAll('.nav-item[data-group]').forEach(el => {
    el.addEventListener('click', () => {
      const g = el.dataset.group;
      const first = NAV_GROUPS[g] && NAV_GROUPS[g].items[0];
      if (first) navigate(first.view);
    });
  });

  // Wire command bar
  const cmdInput = document.getElementById('cmd-input');
  if (cmdInput) {
    cmdInput.addEventListener('keyup', handleCmdInput);
    document.addEventListener('click', (e) => {
      if (!e.target.closest('#cmdbar') && !e.target.closest('#search-results')) {
        document.getElementById('search-results').classList.remove('visible');
      }
    });
  }

  // Wire modal close buttons
  document.querySelectorAll('[data-modal-close]').forEach(btn => {
    btn.addEventListener('click', () => Modal.close(btn.dataset.modalClose));
  });

  // Wire modal overlay click
  document.querySelectorAll('.modal-overlay').forEach(el => {
    el.addEventListener('click', (e) => { if (e.target === el) Modal.closeAll(); });
  });

  // Sidebar toggle (mobile)
  const sbToggle = document.getElementById('sidebar-toggle');
  const sb = document.getElementById('sidebar');
  if (sbToggle && sb) {
    sbToggle.addEventListener('click', () => {
      const open = sb.classList.toggle('open');
      document.body.classList.toggle('sidebar-open', open);
      sbToggle.setAttribute('aria-expanded', String(open));
    });
    // Close sidebar after navigating on narrow screens
    document.querySelectorAll('.nav-item[data-group]').forEach(el => {
      el.addEventListener('click', () => {
        if (window.matchMedia('(max-width: 900px)').matches) {
          sb.classList.remove('open');
          document.body.classList.remove('sidebar-open');
          sbToggle.setAttribute('aria-expanded', 'false');
        }
      });
    });
    // Click backdrop to close
    document.body.addEventListener('click', (e) => {
      if (!sb.classList.contains('open')) return;
      if (e.target === sb || sb.contains(e.target)) return;
      if (e.target === sbToggle || sbToggle.contains(e.target)) return;
      if (window.matchMedia('(max-width: 900px)').matches) {
        sb.classList.remove('open');
        document.body.classList.remove('sidebar-open');
        sbToggle.setAttribute('aria-expanded', 'false');
      }
    });
  }

  // Global keyboard shortcuts
  ShortcutHelp.init();

  // Macro context bar across all views (Treasury/VIX strip)
  MacroBar.init();

  // Holdings tape — held symbols + day % marquee under the header
  HoldingsTape.init();

  // Navigate to overview immediately — don't block on data fetches
  navigate('overview');

  // Load sidebar data in background (non-blocking)
  loadTicker().catch(() => {});
  loadSidebarWatchlist().catch(() => {});

  startAutoRefresh();
  // Re-pin the alert-poll cadence now that State.settings is populated.
  // The module-init _startAlertPollTimer() at line ~2572 fires before
  // settings have loaded, so without this restart the user's saved
  // refresh_interval was ignored until they opened Settings.
  if (typeof _startAlertPollTimer === 'function') _startAlertPollTimer();
  updateStatusBar();
}

// ── Research Tabs ─────────────────────────────────────────────────────────────
function switchResearchTab(tab, symbol) {
  const tabs = [
    'chart', 'fundamentals', 'news', 'options', 'sec', 'intel',
    // v0.2.0 research tabs
    'rnd', 'forecast', 'horizons', 'factors', 'eventstudy',
    // v0.3.0 synthesis tabs
    'consensus', 'peerdiv', 'bayes', 'groundhyp',
  ];
  tabs.forEach(t => {
    const btn = document.getElementById('rtab-' + t);
    const panel = document.getElementById('rpanel-' + t);
    if (btn) btn.classList.toggle('active', t === tab);
    if (panel) panel.style.display = t === tab ? '' : 'none';
  });
  if (tab === 'options') {
    loadOptionsForSymbol(symbol);
  } else if (tab === 'news') {
    // News may already be loading, no-op if already populated
    const feedEl = document.getElementById('news-feed-' + symbol);
    if (feedEl && feedEl.querySelector('.spinner')) {
      loadNewsFor(symbol, 'news-feed-' + symbol);
    }
  } else if (tab === 'sec') {
    loadResearchSecTab(symbol);
  } else if (tab === 'intel') {
    loadResearchIntelTab(symbol);
  } else if (tab === 'rnd') {
    const body = document.getElementById('rnd-body-' + symbol);
    if (body && body.dataset.loaded !== '1' && window.ResearchIVDensity) {
      body.dataset.loaded = '1';
      try { window.ResearchIVDensity.render(body, symbol); }
      catch(e) { body.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'forecast') {
    const pfHost = document.getElementById('probforecast-panel');
    if (pfHost && window.renderProbForecast && pfHost.dataset.loaded !== '1') {
      pfHost.dataset.loaded = '1';
      const sel = document.querySelector('[data-pf-horizon]');
      const horizon = sel ? (parseInt(sel.value, 10) || 20) : 20;
      try { window.renderProbForecast(pfHost, symbol, horizon); }
      catch(e) { pfHost.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'horizons') {
    const mhEl = document.getElementById('mh-panel');
    if (mhEl && window.renderMultiHorizon && mhEl.dataset.loaded !== '1') {
      mhEl.dataset.loaded = '1';
      try { window.renderMultiHorizon(mhEl, symbol); }
      catch(e) { mhEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'factors') {
    const fEl = document.getElementById('factor-exposure-host');
    if (fEl && window.renderFactorExposure && fEl.dataset.loaded !== '1') {
      fEl.dataset.loaded = '1';
      try { window.renderFactorExposure(fEl, symbol, { years: 3 }); }
      catch(e) { fEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'eventstudy') {
    const esEl = document.getElementById('es-container-' + symbol);
    if (esEl && esEl.childElementCount === 0 && typeof window.renderEventStudy === 'function') {
      try { window.renderEventStudy(esEl, symbol, 'earnings'); }
      catch(e) { esEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'consensus') {
    const cEl = document.getElementById('consensus-host');
    if (cEl && window.renderConsensus && cEl.dataset.loaded !== '1') {
      cEl.dataset.loaded = '1';
      try { window.renderConsensus(cEl, symbol); }
      catch(e) { cEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'peerdiv') {
    const pdEl = document.getElementById('peerdiv-host');
    if (pdEl && window.renderPeerDiv && pdEl.dataset.loaded !== '1') {
      pdEl.dataset.loaded = '1';
      try { window.renderPeerDiv(pdEl, symbol, { n: 5 }); }
      catch(e) { pdEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'bayes') {
    const bEl = document.getElementById('bayes-smart-host');
    if (bEl && window.renderBayesSmart && bEl.dataset.loaded !== '1') {
      bEl.dataset.loaded = '1';
      try { window.renderBayesSmart(bEl, symbol); }
      catch(e) { bEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  } else if (tab === 'groundhyp') {
    const gEl = document.getElementById('grounded-hyp-host');
    if (gEl && window.renderGroundedHypothesis && gEl.dataset.loaded !== '1') {
      gEl.dataset.loaded = '1';
      try { window.renderGroundedHypothesis(gEl, symbol); }
      catch(e) { gEl.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
    }
  }
}

// Re-render probabilistic forecast on horizon select change.
document.addEventListener('change', (e) => {
  if (!e.target || !e.target.matches('[data-pf-horizon]')) return;
  const pfHost = document.getElementById('probforecast-panel');
  if (!pfHost || !window.renderProbForecast) return;
  const sym = (State && State.researchSymbol) || 'AAPL';
  const horizon = parseInt(e.target.value, 10) || 20;
  try { window.renderProbForecast(pfHost, sym, horizon); } catch(_) {}
});

// ── Research → INTEL tab (XBRL + GDELT + StockTwits + Form 4 + Senate) ───────
async function loadResearchIntelTab(symbol) {
  const body = document.getElementById('intel-body-' + symbol);
  if (!body) return;
  if (body.dataset.loaded === '1') return;  // lazy load once
  body.dataset.loaded = '1';
  body.innerHTML = `<div class="loading"><div class="spinner"></div> FUSING INTEL FOR ${_esc(symbol)}…</div>`;

  // Helper: settle each request individually so one failure doesn't kill the panel
  const safe = (p) => p.then(v => ({ok: true, v})).catch(e => ({ok: false, e}));
  const [xbrlR, gdeltR, toneR, stwitsR, form4R, senateR] = await Promise.all([
    safe(API.get(`/api/research/xbrl/${encodeURIComponent(symbol)}`)),
    safe(API.get(`/api/news/gdelt/${encodeURIComponent(symbol)}`)),
    safe(API.get(`/api/news/gdelt-tone/${encodeURIComponent(symbol)}`)),
    safe(API.get(`/api/alt-data/stocktwits/${encodeURIComponent(symbol)}`)),
    safe(API.get(`/api/intel/form4-v2/${encodeURIComponent(symbol)}`)),
    safe(API.get(`/api/congress/senate?symbol=${encodeURIComponent(symbol)}&limit=20`)),
  ]);

  const xbrl   = xbrlR.ok   ? xbrlR.v   : null;
  const gdelt  = gdeltR.ok  ? gdeltR.v  : null;
  const tone   = toneR.ok   ? toneR.v   : null;
  const stwits = stwitsR.ok ? stwitsR.v : null;
  const form4  = form4R.ok  ? form4R.v  : null;
  const senate = senateR.ok ? senateR.v : null;

  // ── XBRL block ─────────────────────────────────────────────
  let xbrlHtml = '<div class="empty-state"><span style="color:var(--text-dim)">XBRL unavailable</span></div>';
  if (xbrl?.metrics) {
    const m = xbrl.metrics;
    const cell = (label, obj, money = true) => {
      if (!obj || obj.value == null) return `<div class="kpi-card"><div class="kpi-label">${label}</div><div class="kpi-value text-dim">—</div></div>`;
      const v = money ? '$' + fmt.compact(obj.value) : fmt.num(obj.value);
      return `<div class="kpi-card"><div class="kpi-label">${label}</div><div class="kpi-value">${v}</div><div class="kpi-sub" style="font-size:9px">${_esc(obj.form || '')} · ${_esc(obj.end || '')}</div></div>`;
    };
    xbrlHtml = `
      <div class="kpi-grid" style="grid-template-columns:repeat(auto-fill,minmax(150px,1fr))">
        ${cell('REVENUE',         m.revenue)}
        ${cell('NET INCOME',      m.net_income)}
        ${cell('OPERATING INC',   m.operating_income)}
        ${cell('ASSETS',          m.assets)}
        ${cell('LIABILITIES',     m.liabilities)}
        ${cell("STOCKHOLDERS' EQ", m.stockholders_equity)}
        ${cell('CASH',            m.cash)}
        ${cell('R&D EXPENSE',     m.rd_expense)}
        ${cell('EPS DILUTED',     m.eps_diluted, false)}
        ${cell('SHARES OUT',      m.shares_out, false)}
      </div>
      <div style="margin-top:6px;font-size:10px;color:var(--text-dim)">SEC XBRL · ${_esc(m.entity || '')} · CIK ${_esc(xbrl.cik || '')}</div>`;
  }

  // ── GDELT tone sparkline-ish ────────────────────────────────
  let toneHtml = '<div class="empty-state"><span style="color:var(--text-dim)">No tone data</span></div>';
  if (tone?.tone?.length) {
    const series = tone.tone.slice(-21);
    const vals = series.map(p => p.value || 0);
    const avg = vals.reduce((a,b) => a+b, 0) / Math.max(vals.length, 1);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const norm = v => (hi === lo) ? 50 : 100 - ((v - lo) / (hi - lo) * 100);
    const denom = series.length > 1 ? series.length - 1 : 1; // avoid 0/0 → NaN for single-point series
    const pts = series.map((p, i) => `${(i / denom * 100).toFixed(2)},${norm(p.value).toFixed(2)}`).join(' ');
    const cls = avg >= 0.5 ? 'col-positive' : avg <= -0.5 ? 'col-negative' : '';
    toneHtml = `
      <div style="display:flex;align-items:center;gap:14px">
        <div>
          <div class="kpi-label">21-DAY AVG TONE</div>
          <div class="kpi-value ${cls}" style="font-size:22px">${avg.toFixed(2)}</div>
          <div style="font-size:10px;color:var(--text-dim)">range ${lo.toFixed(2)} → ${hi.toFixed(2)}</div>
        </div>
        <svg viewBox="0 0 100 100" preserveAspectRatio="none" style="flex:1;height:60px">
          <polyline points="${pts}" fill="none" stroke="var(--green)" stroke-width="1.2" vector-effect="non-scaling-stroke"/>
        </svg>
      </div>`;
  }

  // ── GDELT articles list ────────────────────────────────────
  const arts = (gdelt?.articles || []).slice(0, 10);
  const articlesHtml = arts.length
    ? arts.map(a => `<tr>
        <td style="font-size:10px;color:var(--text-dim);white-space:nowrap">${_esc((a.seendate||'').slice(0,8))}</td>
        <td><a href="${_esc(safeUrl(a.url))}" target="_blank" rel="noopener" style="color:var(--blue);font-size:11px">${_esc((a.title||'').slice(0,90))}</a></td>
        <td style="font-size:10px;color:var(--text-secondary)">${_esc(a.domain || '')}</td>
      </tr>`).join('')
    : '<tr><td colspan="3" class="empty-state">No recent GDELT articles</td></tr>';

  // ── StockTwits sentiment ───────────────────────────────────
  let stwitsHtml = '<div class="empty-state"><span style="color:var(--text-dim)">StockTwits unavailable</span></div>';
  if (stwits && (stwits.bullish != null || stwits.bearish != null)) {
    const bull = stwits.bullish || 0, bear = stwits.bearish || 0, total = bull + bear;
    const ratio = total ? (bull / total * 100) : 0;
    const cls = ratio >= 60 ? 'col-positive' : ratio <= 40 ? 'col-negative' : '';
    stwitsHtml = `
      <div style="display:flex;align-items:center;gap:18px">
        <div>
          <div class="kpi-label">BULL RATIO</div>
          <div class="kpi-value ${cls}" style="font-size:22px">${ratio.toFixed(0)}%</div>
          <div style="font-size:10px;color:var(--text-dim)">${bull} bull · ${bear} bear · ${stwits.messages_total || 0} msgs</div>
        </div>
        <div style="flex:1;height:18px;background:var(--bg-secondary);border-radius:3px;overflow:hidden;display:flex">
          <div style="width:${ratio.toFixed(2)}%;background:var(--green)"></div>
          <div style="flex:1;background:var(--red,#ff3366)"></div>
        </div>
      </div>`;
  }

  // ── Form 4 insider feed ────────────────────────────────────
  const f4 = (form4?.transactions || []).slice(0, 10);
  const form4Html = f4.length
    ? f4.map(t => `<tr>
        <td style="font-size:10px;color:var(--text-dim);white-space:nowrap">${_esc(t.filed || '')}</td>
        <td style="font-size:11px"><a href="${_esc(safeUrl(t.url))}" target="_blank" rel="noopener" style="color:var(--blue)">${_esc(t.owner || '—')}</a></td>
        <td style="font-size:10px;color:var(--text-secondary)">${_esc((t.role||'').slice(0,38))}</td>
      </tr>`).join('')
    : '<tr><td colspan="3" class="empty-state">No recent Form 4 filings</td></tr>';

  // ── Senate trades for this symbol ───────────────────────────
  const stx = (senate?.trades || []).filter(t => (t.ticker || '').toUpperCase() === symbol.toUpperCase()).slice(0, 10);
  const senateHtml = stx.length
    ? stx.map(t => {
        const cls = (t.type||'').toLowerCase().includes('purchase') ? 'col-positive' :
                    (t.type||'').toLowerCase().includes('sale') ? 'col-negative' : '';
        return `<tr>
          <td style="font-size:10px;color:var(--text-dim)">${_esc(t.date || '')}</td>
          <td><span class="signal-badge ${cls}" style="font-size:9px">${_esc(t.type || '')}</span></td>
          <td style="font-size:10px">${_esc(t.amount || '')}</td>
          <td style="font-size:10px;color:var(--text-secondary)">${_esc(t.name || '')}</td>
        </tr>`;
      }).join('')
    : '<tr><td colspan="4" class="empty-state">No matching Senate trades</td></tr>';

  body.innerHTML = `
    <div class="panel" style="margin-bottom:8px">
      <div class="panel-header"><span class="panel-title">⊕ STANDARDIZED FUNDAMENTALS — SEC XBRL</span></div>
      <div class="panel-body">${xbrlHtml}</div>
    </div>

    <div class="panel-grid grid-2" style="gap:8px;margin-bottom:8px">
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◉ NARRATIVE TONE — GDELT</span></div>
        <div class="panel-body">${toneHtml}</div>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◇ RETAIL SENTIMENT — STOCKTWITS</span></div>
        <div class="panel-body">${stwitsHtml}</div>
      </div>
    </div>

    <div class="panel" style="margin-bottom:8px">
      <div class="panel-header"><span class="panel-title">⊡ RECENT NEWS — GDELT</span></div>
      <table class="data-table"><tbody>${articlesHtml}</tbody></table>
    </div>

    <div class="panel-grid grid-2" style="gap:8px">
      <div class="panel">
        <div class="panel-header"><span class="panel-title">⊛ INSIDER FORM 4 (edgartools)</span></div>
        <table class="data-table"><tbody>${form4Html}</tbody></table>
      </div>
      <div class="panel">
        <div class="panel-header"><span class="panel-title">⊠ SENATE STOCK ACT — ${_esc(symbol)}</span></div>
        <table class="data-table"><tbody>${senateHtml}</tbody></table>
      </div>
    </div>
  `;
}

async function loadOptionsForSymbol(symbol) {
  const selectEl = document.getElementById('options-expiry-select');
  const bodyEl = document.getElementById('options-chain-body');
  if (!selectEl || !bodyEl) return;
  try {
    const dates = await API.get('/api/options/' + symbol + '/dates');
    if (!dates || !dates.length) {
      bodyEl.innerHTML = '<div class="empty-state"><span>No options available for ' + symbol + '</span></div>';
      return;
    }
    selectEl.innerHTML = dates.map(d => '<option value="' + _esc(d) + '">' + _esc(d) + '</option>').join('');
    loadOptionsChain(symbol, dates[0]);
  } catch(e) {
    bodyEl.innerHTML = '<div class="empty-state"><span class="text-red">Failed to load options: ' + _esc(e.message) + '</span></div>';
  }
}

let _optionsChainGen = 0;
async function loadOptionsChain(symbol, date) {
  const bodyEl = document.getElementById('options-chain-body');
  if (!bodyEl) return;
  // Guard against expiry-dropdown thrash overwriting the latest chain with a
  // slow earlier response.
  const gen = ++_optionsChainGen;
  bodyEl.innerHTML = '<div class="loading"><div class="spinner"></div> Loading chain...</div>';
  try {
    const url = '/api/options/' + symbol + '/chain' + (date ? '?date=' + encodeURIComponent(date) : '');
    const chain = await API.get(url);
    if (gen !== _optionsChainGen) return;
    const calls = chain.calls || [];
    const puts = chain.puts || [];
    if (chain.error && !calls.length) {
      bodyEl.innerHTML = '<div class="empty-state"><span class="text-red">' + _esc(chain.error) + '</span></div>';
      return;
    }
    // Build strike-keyed map
    const allStrikes = Array.from(new Set([...calls.map(c => c.strike), ...puts.map(p => p.strike)])).sort((a,b)=>a-b);
    const callMap = {};
    calls.forEach(c => { callMap[c.strike] = c; });
    const putMap = {};
    puts.forEach(p => { putMap[p.strike] = p; });

    function optCell(opt, side) {
      if (!opt) return '<td colspan="7" style="text-align:center;color:var(--text-muted)">—</td>';
      const itmCls = opt.inTheMoney ? (side === 'call' ? 'options-itm-call' : 'options-itm-put') : '';
      return '<td class="col-number ' + itmCls + '">' + fmt.price(opt.bid) + '</td>' +
             '<td class="col-number ' + itmCls + '">' + fmt.price(opt.ask) + '</td>' +
             '<td class="col-number ' + itmCls + '">' + fmt.price(opt.lastPrice) + '</td>' +
             '<td class="col-number ' + itmCls + '">' + fmt.compact(opt.volume || 0) + '</td>' +
             '<td class="col-number ' + itmCls + '">' + fmt.compact(opt.openInterest || 0) + '</td>' +
             '<td class="col-number ' + itmCls + '">' + (opt.impliedVolatility != null ? opt.impliedVolatility.toFixed(1) + '%' : '—') + '</td>' +
             '<td class="col-number ' + itmCls + '">' + (opt.delta != null ? fmt.num(opt.delta, 3) : '—') + (opt.inTheMoney ? ' <span class="badge badge-green" style="font-size:9px">ITM</span>' : '') + '</td>';
    }

    bodyEl.innerHTML = '<table class="data-table options-table" style="min-width:800px">' +
      '<thead><tr>' +
        '<th colspan="7" style="text-align:center;color:var(--green);border-right:2px solid var(--border-bright)">CALLS</th>' +
        '<th style="text-align:center;color:var(--amber)">STRIKE</th>' +
        '<th colspan="7" style="text-align:center;color:var(--red)">PUTS</th>' +
      '</tr><tr>' +
        '<th style="color:var(--text-dim)">BID</th><th>ASK</th><th>LAST</th><th>VOL</th><th>OI</th><th>IV%</th><th style="border-right:2px solid var(--border-bright)">DELTA/ITM</th>' +
        '<th style="color:var(--amber);text-align:center">—</th>' +
        '<th>BID</th><th>ASK</th><th>LAST</th><th>VOL</th><th>OI</th><th>IV%</th><th>DELTA/ITM</th>' +
      '</tr></thead><tbody>' +
      allStrikes.map(strike => {
        const call = callMap[strike];
        const put = putMap[strike];
        return '<tr>' + optCell(call, 'call') + '<td class="col-price" style="text-align:center;color:var(--amber);font-weight:600;border-left:1px solid var(--border);border-right:1px solid var(--border)">' + fmt.price(strike) + '</td>' + optCell(put, 'put') + '</tr>';
      }).join('') +
      '</tbody></table>';
  } catch(e) {
    if (gen !== _optionsChainGen) return;
    bodyEl.innerHTML = '<div class="empty-state"><span class="text-red">Failed to load chain: ' + _esc(e.message) + '</span></div>';
  }
}

// ── Analytics View ────────────────────────────────────────────────────────────
async function loadAnalyticsView() {
  const view = document.getElementById('view-analytics');
  view.innerHTML = '<div class="loading"><div class="spinner"></div> LOADING ANALYTICS...</div>';

  try {
    const [history, riskData, corrData] = await Promise.all([
      API.get('/api/portfolio/history'),
      API.get('/api/analytics/risk?period=1y'),
      API.get('/api/analytics/correlation?period=3mo'),
    ]);

    // Build equity curve data
    const hist = Array.isArray(history) ? history : [];
    const equityData = hist.map(s => ({
      time: Math.floor(new Date(s.date + 'T00:00:00Z').getTime() / 1000),
      value: s.total_value,
    }));

    // Compute stats
    let totalReturn = null, maxDrawdown = null;
    if (hist.length >= 2) {
      const first = hist[0].total_value;
      const last  = hist[hist.length - 1].total_value;
      totalReturn = ((last - first) / first) * 100;
      let peak = first;
      let dd = 0;
      hist.forEach(s => {
        if (s.total_value > peak) peak = s.total_value;
        const d = (s.total_value - peak) / peak * 100;
        if (d < dd) dd = d;
      });
      maxDrawdown = dd;
    }

    view.innerHTML = '<div class="panel mb-8" id="analytics-equity-panel">' +
      '<div class="panel-header">' +
        '<span class="panel-title">PORTFOLIO EQUITY CURVE</span>' +
        '<div class="flex gap-8 align-center">' +
          '<span style="font-size:10px;color:var(--text-dim)">BENCHMARK:</span>' +
          '<select class="form-select" id="benchmark-select" style="font-size:11px;padding:2px 6px" onchange="reloadBenchmark()">' +
            '<option value="SPY">SPY</option><option value="QQQ">QQQ</option><option value="DIA">DIA</option><option value="IWM">IWM</option>' +
          '</select>' +
          '<button class="btn btn-ghost btn-sm" onclick="loadAnalyticsView()">↻ REFRESH</button>' +
        '</div>' +
      '</div>' +
      '<div id="equity-chart-container" style="height:320px"></div>' +
      '<div class="panel-body" style="border-top:1px solid var(--border);padding:8px 12px;display:flex;gap:24px;font-size:11px">' +
        '<span><span class="text-dim">TOTAL RETURN: </span><span class="' + col.pct(totalReturn) + '">' + fmt.pct(totalReturn) + '</span></span>' +
        '<span><span class="text-dim">MAX DRAWDOWN: </span><span class="col-negative">' + (maxDrawdown != null ? maxDrawdown.toFixed(2) + '%' : '—') + '</span></span>' +
        '<span><span class="text-dim">SNAPSHOTS: </span><span>' + hist.length + '</span></span>' +
        (hist.length === 0 ? '<span class="text-amber">No history yet — snapshots taken every 5 min, once per day</span>' : '') +
      '</div>' +
    '</div>' +

    '<div class="panel mb-8">' +
      '<div class="panel-header"><span class="panel-title">RISK METRICS</span>' +
        '<div class="flex gap-4" id="risk-period-btns">' +
          '<button class="btn btn-ghost btn-sm" data-period="3mo" onclick="loadRiskTable(\'3mo\')">3MO</button>' +
          '<button class="btn btn-ghost btn-sm" data-period="6mo" onclick="loadRiskTable(\'6mo\')">6MO</button>' +
          '<button class="btn btn-ghost btn-sm active" data-period="1y" onclick="loadRiskTable(\'1y\')">1Y</button>' +
        '</div>' +
      '</div>' +
      '<div id="risk-table-body" style="overflow-x:auto">' + renderRiskTable(riskData) + '</div>' +
    '</div>' +

    '<div class="panel">' +
      '<div class="panel-header"><span class="panel-title">CORRELATION MATRIX</span>' +
        '<div class="flex gap-4" id="corr-period-btns">' +
          '<button class="btn btn-ghost btn-sm" data-period="1mo" onclick="loadCorrMatrix(\'1mo\')">1MO</button>' +
          '<button class="btn btn-ghost btn-sm active" data-period="3mo" onclick="loadCorrMatrix(\'3mo\')">3MO</button>' +
          '<button class="btn btn-ghost btn-sm" data-period="6mo" onclick="loadCorrMatrix(\'6mo\')">6MO</button>' +
          '<button class="btn btn-ghost btn-sm" data-period="1y" onclick="loadCorrMatrix(\'1y\')">1Y</button>' +
        '</div>' +
      '</div>' +
      '<div id="corr-matrix-body" class="panel-body">' + renderCorrMatrix(corrData) + '</div>' +
    '</div>';

    // Render equity chart
    if (equityData.length >= 2) {
      renderEquityChart(equityData, hist);
    }

    // v0.2.0 — append portfolio Fama-French factor exposure panel.
    try {
      if (window.renderPortfolioFactors) {
        const factorHost = document.createElement('section');
        factorHost.className = 'panel mb-8';
        factorHost.id = 'portfolio-factor-exposure';
        view.appendChild(factorHost);
        const holdings = (State.portfolio && State.portfolio.holdings) || [];
        window.renderPortfolioFactors(factorHost, holdings, { years: 3 });
      }
    } catch (e) {
      console.warn('renderPortfolioFactors failed:', e);
    }

  } catch(e) {
    view.innerHTML = '<div class="empty-state"><span class="empty-icon">⚠</span><span class="text-red">' + _esc(e.message) + '</span></div>';
  }
}

// ── v0.2.0 RESEARCH VIEW LOADERS ──────────────────────────────────────────────

async function loadBacktest() {
  const view = document.getElementById('view-backtest');
  if (!view) return;
  const defaultSym = (State && State.researchSymbol) ? State.researchSymbol : 'AAPL';

  view.innerHTML = `
    <div class="panel mb-8">
      <div class="panel-header">
        <span class="panel-title">WALK-FORWARD BACKTEST</span>
        <div class="flex gap-8 align-center" style="font-size:11px">
          <label>Ticker:
            <input type="text" id="bt-view-symbol" value="${defaultSym}"
              style="background:transparent;color:var(--green);border:1px solid var(--border);padding:2px 6px;font-family:monospace;width:80px;text-transform:uppercase">
          </label>
          <button class="btn btn-ghost btn-sm" onclick="reloadBacktestPanel()">↻ LOAD</button>
        </div>
      </div>
      <div class="panel-body" style="font-size:11px;color:var(--text-dim);padding:6px 12px;border-bottom:1px solid var(--border)">
        Replays a signal-producing function over historical bars and reports hit-rate, Sharpe, drawdown, and the equity curve.
        Momentum and mean-reversion adapters are leak-free; ml_forecast is best-effort.
      </div>
    </div>
    <div id="backtest-host"></div>
  `;
  reloadBacktestPanel();
}

function reloadBacktestPanel() {
  const symInput = document.getElementById('bt-view-symbol');
  const symbol = (symInput && symInput.value ? symInput.value : 'AAPL').toUpperCase().trim();
  const host = document.getElementById('backtest-host');
  if (!host) return;
  if (window.renderBacktest) {
    const today = new Date();
    const start = new Date(today.getFullYear() - 2, today.getMonth(), today.getDate())
                    .toISOString().slice(0, 10);
    const end = today.toISOString().slice(0, 10);
    try { window.renderBacktest(host, symbol, 'momentum', { start, end }); }
    catch(e) { host.innerHTML = '<div style="color:var(--red);padding:12px">' + (e.message||e) + '</div>'; }
  } else {
    host.innerHTML = '<div style="color:var(--red);padding:12px">research_backtest.js not loaded</div>';
  }
}

async function loadOptimizer() {
  const view = document.getElementById('view-optimizer');
  if (!view) return;
  if (typeof window.renderOptimizer !== 'function') {
    view.innerHTML = '<div style="padding:24px;color:#e15a5a">research_optimizer.js not loaded</div>';
    return;
  }
  // Seed universe + current weights from the live portfolio.
  let seedSymbols = [];
  let currentWeights = {};
  try {
    const port = State.portfolio || await API.get('/api/portfolio');
    if (port && Array.isArray(port.holdings)) {
      const totalVal = port.holdings.reduce((acc, h) => acc + (Number(h.market_value) || 0), 0);
      port.holdings.forEach((h) => {
        if (!h || !h.symbol) return;
        seedSymbols.push(String(h.symbol).toUpperCase());
        if (totalVal > 0) {
          currentWeights[String(h.symbol).toUpperCase()] = (Number(h.market_value) || 0) / totalVal;
        }
      });
    }
  } catch (e) {
    console.warn('loadOptimizer: portfolio seed failed', e);
  }
  if (!seedSymbols.length) seedSymbols = ['AAPL', 'MSFT', 'GLD', 'SPY', 'TLT'];
  try { window.renderOptimizer(view, { symbols: seedSymbols, currentWeights }); }
  catch(e) { view.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
}

async function loadMonteCarloView() {
  const view = document.getElementById('view-montecarlo');
  if (!view) return;
  // research_montecarlo.js exports loadMonteCarlo() as a window global.
  if (typeof window.loadMonteCarlo === 'function') {
    try { window.loadMonteCarlo(); return; } catch(e) { /* fall through */ }
  }
  // Fallback: hand the container to renderMonteCarlo if available.
  if (typeof window.renderMonteCarlo === 'function') {
    try { window.renderMonteCarlo(view); return; } catch(e) {}
  }
  view.innerHTML = '<div style="padding:24px;color:#e15a5a">research_montecarlo.js not loaded</div>';
}

function loadHypothesisLab() {
  const root = document.getElementById('research-lab-root');
  if (!root) return;
  if (typeof window.renderHypothesisLab === 'function') {
    try { window.renderHypothesisLab(root); }
    catch(e) { root.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
  } else {
    root.innerHTML = '<div style="padding:24px;color:#e15a5a">research_hypothesis.js not loaded</div>';
  }
}

// ── v0.3.0 synthesis view loaders ────────────────────────────────────────────
// Each loader is idempotent — the underlying renderers either skip re-render
// if their container is already populated (catalysts) or rebuild their own
// controls in place (cluster, divergences, sector-flow, what-if).

function loadCatalystsView() {
  const host = document.getElementById('catalysts-host');
  if (!host) return;
  if (typeof window.renderCatalystTimeline !== 'function') {
    host.innerHTML = '<div style="padding:24px;color:#e15a5a">synth_catalyst.js not loaded</div>';
    return;
  }
  // Don't re-render if user is already interacting with the populated panel —
  // the panel's own RUN button refreshes data in place.
  if (host.childElementCount === 0) {
    try { window.renderCatalystTimeline(host, { symbols: null, days_ahead: 60 }); }
    catch(e) { host.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
  }
}

function loadClusterView() {
  const el = document.getElementById('cluster-panel');
  if (!el) return;
  if (typeof window.renderClusterScan !== 'function') {
    el.innerHTML = '<div style="padding:24px;color:#e15a5a">synth_cluster.js not loaded</div>';
    return;
  }
  try { window.renderClusterScan(el, { direction: 'bullish', min_sources: 4 }); }
  catch(e) { el.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
}

function loadDivergencesView() {
  const el = document.getElementById('divergence-map-host');
  if (!el) return;
  if (typeof window.renderDivergenceMap !== 'function') {
    el.innerHTML = '<div style="padding:24px;color:#e15a5a">synth_divmap.js not loaded</div>';
    return;
  }
  try { window.renderDivergenceMap(el, { universe: 'sp500_top100', top_n: 20 }); }
  catch(e) { el.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
}

function loadSectorFlowView() {
  // synth_sectorflow.js registers window.loadSectorFlow, which targets
  // #view-sectorflow directly. Fall back to renderSectorFlow if needed.
  const view = document.getElementById('view-sectorflow');
  if (typeof window.loadSectorFlow === 'function') {
    try { window.loadSectorFlow(); return; }
    catch(e) {
      if (view) view.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>';
      return;
    }
  }
  if (view && typeof window.renderSectorFlow === 'function') {
    try { window.renderSectorFlow(view); }
    catch(e) { view.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
  } else if (view) {
    view.innerHTML = '<div style="padding:24px;color:#e15a5a">synth_sectorflow.js not loaded</div>';
  }
}

function loadWhatIfView() {
  const el = document.getElementById('whatif-panel');
  if (!el) return;
  if (typeof window.renderWhatIf !== 'function') {
    el.innerHTML = '<div style="padding:24px;color:#e15a5a">synth_whatif.js not loaded</div>';
    return;
  }
  try { window.renderWhatIf(el, { holdings: window._lastPortfolioHoldings || null }); }
  catch(e) { el.innerHTML = '<div style="color:var(--red);padding:24px">' + (e.message||e) + '</div>'; }
}

async function renderEquityChart(equityData, history) {
  const container = document.getElementById('equity-chart-container');
  if (!container || !window.LightweightCharts) return;

  ChartEngine.destroyChart('equity-chart-container');

  const chart = LightweightCharts.createChart(container, {
    layout: { background: { type: 'solid', color: '#061520' }, textColor: '#3a5a70', fontFamily: "'JetBrains Mono', monospace", fontSize: 10 },
    grid: { vertLines: { color: '#0f2a3a', style: 1 }, horzLines: { color: '#0f2a3a', style: 1 } },
    crosshair: { mode: 1 },
    rightPriceScale: { borderColor: '#0f2a3a', textColor: '#3a5a70' },
    timeScale: { borderColor: '#0f2a3a', textColor: '#3a5a70', timeVisible: true },
    width: container.clientWidth,
    height: 320,
  });

  // Register with ChartEngine immediately (before any await) so the
  // destroyChart() above actually finds the previous instance next time.
  // Previously this chart was stored in a dead local object, so every
  // analytics reload leaked a live chart + its ResizeObserver.
  const ro = new ResizeObserver(() => chart.applyOptions({ width: container.clientWidth }));
  ro.observe(container);
  chart._resizeObserver = ro; // destroyChart() disconnects this
  ChartEngine.registerChart('equity-chart-container', chart);

  const portfolioSeries = chart.addAreaSeries({
    lineColor: '#00ff9f',
    topColor: '#00ff9f33',
    bottomColor: '#00ff9f00',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
  });
  portfolioSeries.setData(equityData);

  // Load benchmark overlay. Pick the period from the portfolio's actual
  // history span so multi-year accounts aren't visually clipped to 1y.
  const benchmarkSym = document.getElementById('benchmark-select') ? document.getElementById('benchmark-select').value : 'SPY';
  try {
    const baseVal = history.length ? history[0].total_value : null;
    let benchPeriod = '1y';
    if (equityData.length) {
      const firstT = equityData[0].time;
      const lastT = equityData[equityData.length - 1].time;
      // equityData times are seconds-since-epoch (lightweight-charts UTCTimestamp).
      const spanDays = (Number(lastT) - Number(firstT)) / 86400;
      if (spanDays < 90)      benchPeriod = '3mo';
      else if (spanDays < 365) benchPeriod = '1y';
      else if (spanDays < 730) benchPeriod = '2y';
      else if (spanDays < 1825) benchPeriod = '5y';
      else                     benchPeriod = 'max';
    }
    const benchResp = await API.get('/api/portfolio/benchmark?symbol=' + benchmarkSym + '&period=' + benchPeriod);
    const benchData = (benchResp.data || []).filter(d => d.time >= equityData[0].time);
    if (benchData.length && baseVal) {
      // Normalize benchmark to first portfolio value
      const benchFirst = benchData[0].value;
      const normalizedBench = benchData.map(d => ({ time: d.time, value: round4(d.value / benchFirst * baseVal) }));
      const benchSeries = chart.addLineSeries({
        color: '#00c8ff66',
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        crosshairMarkerVisible: false,
      });
      benchSeries.setData(normalizedBench);
    }
  } catch(e) {}
}

function round4(v) { return Math.round(v * 10000) / 10000; }

async function reloadBenchmark() {
  // Re-render the analytics view to pick up new benchmark
  loadAnalyticsView();
}

function _setActivePeriodBtn(containerId, period) {
  const ct = document.getElementById(containerId);
  if (!ct) return;
  ct.querySelectorAll('button[data-period]').forEach(b => {
    b.classList.toggle('active', b.dataset.period === period);
  });
}

async function loadRiskTable(period) {
  const el = document.getElementById('risk-table-body');
  if (!el) return;
  _setActivePeriodBtn('risk-period-btns', period);
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const data = await API.get('/api/analytics/risk?period=' + period);
    el.innerHTML = renderRiskTable(data);
  } catch(e) {
    el.innerHTML = '<div class="empty-state"><span class="text-red">' + _esc(e.message) + '</span></div>';
  }
}

function renderRiskTable(data) {
  if (!data || typeof data !== 'object' || Object.keys(data).length === 0) {
    return '<div class="empty-state"><span>No risk data. Add stocks to portfolio first.</span></div>';
  }
  if (data.error) {
    return '<div class="empty-state"><span class="text-red">' + data.error + '</span></div>';
  }
  const symbols = Object.keys(data).filter(k => k !== 'error');
  if (!symbols.length) return '<div class="empty-state"><span>No data available</span></div>';

  const cols = [
    ['Symbol', s => '<span class="col-symbol" onclick="openResearch(\'' + s + '\')">' + s + '</span>'],
    ['Total Ret%', s => { const v = data[s].total_return; return '<span class="' + col.pct(v) + '">' + fmt.pct(v) + '</span>'; }],
    ['Ann Ret%', s => { const v = data[s].annualized_return; return '<span class="' + col.pct(v) + '">' + fmt.pct(v) + '</span>'; }],
    ['Ann Vol%', s => { const v = data[s].annualized_vol; return '<span style="color:var(--amber)">' + fmt.num(v) + '%</span>'; }],
    ['Sharpe', s => { const v = data[s].sharpe_ratio; return '<span class="' + col.pct(v) + '">' + fmt.num(v, 3) + '</span>'; }],
    ['Sortino', s => { const v = data[s].sortino_ratio; return '<span class="' + col.pct(v) + '">' + fmt.num(v, 3) + '</span>'; }],
    ['Max DD%', s => { const v = data[s].max_drawdown; return '<span class="col-negative">' + fmt.num(v) + '%</span>'; }],
    ['Calmar', s => { const v = data[s].calmar_ratio; return '<span class="' + col.pct(v) + '">' + fmt.num(v, 3) + '</span>'; }],
    ['VaR 95%', s => { const v = data[s].var_95; return '<span class="col-negative">' + fmt.num(v) + '%</span>'; }],
  ];

  return '<table class="data-table" style="min-width:700px">' +
    '<thead><tr>' + cols.map(([h]) => '<th>' + h + '</th>').join('') + '</tr></thead>' +
    '<tbody>' +
    symbols.map(s => '<tr>' + cols.map(([,fn]) => '<td>' + fn(s) + '</td>').join('') + '</tr>').join('') +
    '</tbody></table>';
}

async function loadCorrMatrix(period) {
  const el = document.getElementById('corr-matrix-body');
  if (!el) return;
  _setActivePeriodBtn('corr-period-btns', period);
  el.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
  try {
    const data = await API.get('/api/analytics/correlation?period=' + period);
    el.innerHTML = renderCorrMatrix(data);
  } catch(e) {
    el.innerHTML = '<div class="empty-state"><span class="text-red">' + _esc(e.message) + '</span></div>';
  }
}

function renderCorrMatrix(data) {
  if (!data || !data.symbols || !data.symbols.length) {
    return '<div class="empty-state"><span>No correlation data. Add multiple stocks to portfolio.</span></div>';
  }
  const syms = data.symbols;
  const matrix = data.matrix || {};

  function cellColor(val, isDiag) {
    if (isDiag) return 'background:#004d2e;color:#00ff9f';
    if (val == null) return 'background:var(--bg-panel)';
    if (val >= 0.7) return 'background:#003a20;color:#00ff9f';
    if (val >= 0.4) return 'background:#002a18;color:#00cc7a';
    if (val >= 0.1) return 'background:#011a10;color:#009955';
    if (val >= -0.1) return 'background:var(--bg-panel);color:var(--text-secondary)';
    if (val >= -0.3) return 'background:#1a0808;color:#cc4444';
    return 'background:#2a0a0a;color:#ff3355';
  }

  let html = '<table class="data-table" style="min-width:' + (syms.length * 70 + 60) + 'px">';
  html += '<thead><tr><th></th>' + syms.map(s => '<th style="text-align:center">' + s + '</th>').join('') + '</tr></thead>';
  html += '<tbody>';
  syms.forEach(row => {
    html += '<tr><td><span class="col-symbol" onclick="openResearch(\'' + row + '\')">' + row + '</span></td>';
    syms.forEach(col2 => {
      const val = (matrix[row] && matrix[row][col2] != null) ? matrix[row][col2] : null;
      const isDiag = row === col2;
      html += '<td style="text-align:center;padding:4px;' + cellColor(val, isDiag) + '">' + (val != null ? val.toFixed(2) : '—') + '</td>';
    });
    html += '</tr>';
  });
  html += '</tbody></table>';
  return html;
}

// ── CSV Export / Import ───────────────────────────────────────────────────────
function exportPortfolioCsv() {
  window.location.href = '/api/portfolio/export';
}

function exportTransactionsCsv() {
  window.location.href = '/api/transactions/export';
}

let _csvFile = null;

function handleCsvDrop(event) {
  event.preventDefault();
  document.getElementById('csv-drop-zone').style.borderColor = 'var(--border-bright)';
  const file = event.dataTransfer.files[0];
  if (file) {
    _csvFile = file;
    document.getElementById('csv-import-status').innerHTML = '<span class="text-green">File ready: ' + _esc(file.name) + ' (' + Math.round(file.size/1024) + ' KB)</span>';
  }
}

function handleCsvFileSelect(event) {
  const file = event.target.files[0];
  if (file) {
    _csvFile = file;
    document.getElementById('csv-import-status').innerHTML = '<span class="text-green">File ready: ' + _esc(file.name) + ' (' + Math.round(file.size/1024) + ' KB)</span>';
  }
}

async function submitCsvImport() {
  if (!_csvFile) { Toast.warn('Please select a CSV file first'); return; }
  const statusEl = document.getElementById('csv-import-status');
  statusEl.innerHTML = '<span class="text-amber">Importing...</span>';
  try {
    const formData = new FormData();
    formData.append('file', _csvFile);
    const resp = await fetch('/api/portfolio/import', { method: 'POST', body: formData });
    const result = await resp.json();
    if (result.error) {
      statusEl.innerHTML = '<span class="text-red">Error: ' + _esc(result.error) + '</span>';
      return;
    }
    statusEl.innerHTML = '<span class="text-green">Imported: ' + result.imported + ' positions &nbsp;|&nbsp; Skipped: ' + result.skipped + '</span>';
    if (result.errors && result.errors.length) {
      statusEl.innerHTML += '<br><span class="text-amber" style="font-size:10px">' + result.errors.slice(0,3).map(_esc).join('<br>') + '</span>';
    }
    Toast.success('Imported ' + result.imported + ' positions');
    _csvFile = null;
    document.getElementById('csv-file-input').value = '';
    setTimeout(() => { Modal.close('modal-csv-import'); loadPortfolio(); }, 1500);
  } catch(e) {
    statusEl.innerHTML = '<span class="text-red">Failed: ' + _esc(e.message) + '</span>';
  }
}

// ── Intel / SEC Intelligence Hub ──────────────────────────────────────────────

const IntelState = {
  activeTab: 'feed',
  intelFeed: null,
  institutionalData: null,
  insiderSymbol: null,
};

async function loadIntelView() {
  const view = document.getElementById('view-intel');
  view.innerHTML = `
    <div class="view-header" style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;border-bottom:1px solid var(--border)">
      <div>
        <span style="font-size:16px;font-weight:700;color:var(--green)">◑ SEC INTELLIGENCE HUB</span>
        <span style="margin-left:12px;font-size:10px;color:var(--text-dim)">EDGAR // FORM 4 // 13F // AI ANALYSIS</span>
      </div>
    </div>
    <div class="intel-tabs" style="padding:0 16px">
      <button class="intel-tab active" id="itab-feed" onclick="switchIntelTab('feed')">FILING FEED</button>
      <button class="intel-tab" id="itab-insiders" onclick="switchIntelTab('insiders')">INSIDER TRACKER</button>
      <button class="intel-tab" id="itab-institutional" onclick="switchIntelTab('institutional')">INSTITUTIONAL</button>
    </div>
    <div style="padding:0 16px 16px;overflow-y:auto;height:calc(100vh - 160px)">
      <div id="intel-panel-feed" class="intel-panel active"></div>
      <div id="intel-panel-insiders" class="intel-panel"></div>
      <div id="intel-panel-institutional" class="intel-panel"></div>
    </div>
  `;
  switchIntelTab('feed');
}

function switchIntelTab(tab) {
  IntelState.activeTab = tab;
  ['feed', 'insiders', 'institutional'].forEach(t => {
    const btn = document.getElementById('itab-' + t);
    const panel = document.getElementById('intel-panel-' + t);
    if (btn) btn.classList.toggle('active', t === tab);
    if (panel) panel.classList.toggle('active', t === tab);
  });
  if (tab === 'feed') loadIntelFeed();
  else if (tab === 'insiders') loadIntelInsiders();
  else if (tab === 'institutional') loadIntelInstitutional();
}

// ── Filing Feed ───────────────────────────────────────────────────────────────

async function loadIntelFeed(refresh) {
  const panel = document.getElementById('intel-panel-feed');
  if (!panel) return;
  panel.innerHTML = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <span style="font-size:11px;color:var(--text-dim)">Real-time 8-K, 10-K, 10-Q, S-1 alerts for portfolio + watchlist symbols</span>
      <button class="btn btn-ghost btn-sm" onclick="loadIntelFeed(true)">↻ REFRESH</button>
    </div>
    <div class="loading"><div class="spinner"></div> Fetching filings from SEC EDGAR...</div>
  `;
  try {
    const url = '/api/intel/feed' + (refresh ? '?refresh=true' : '');
    const data = await API.get(url);
    IntelState.intelFeed = data;
    renderFilingFeed(data, panel);
  } catch(e) {
    panel.innerHTML = `<div class="empty-state"><span class="text-red">Failed to load feed: ${_esc(e.message)}</span></div>`;
  }
}

function renderFilingFeed(filings, panel) {
  const header = `
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
      <span style="font-size:11px;color:var(--text-dim)">${filings.length} filings loaded</span>
      <button class="btn btn-ghost btn-sm" onclick="loadIntelFeed(true)">↻ REFRESH</button>
    </div>
  `;
  if (!filings.length) {
    panel.innerHTML = header + '<div class="empty-state"><span>No filings found. Add stocks to portfolio or watchlist.</span></div>';
    return;
  }

  const rows = filings.map((f, idx) => {
    const sigClass = 'signal-' + (f.signal || 'neutral').toLowerCase();
    const aiBadge = f.ai_powered
      ? '<span class="ai-badge">AI</span>'
      : '<span class="rule-badge">AUTO</span>';
    return `
      <tr onclick="toggleFilingExpand('fexpand-${idx}')" style="cursor:pointer">
        <td style="white-space:nowrap;font-size:11px;color:var(--text-dim)">${_esc(f.filing_date || '—')}</td>
        <td><span class="col-symbol" style="cursor:pointer" onclick="event.stopPropagation();openResearch('${_esc(_jesc(f.ticker))}')">${_esc(f.ticker)}</span></td>
        <td><span class="badge badge-blue">${_esc(f.form_type)}</span></td>
        <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${_esc(f.event_type || f.description || '—')}</td>
        <td><span class="signal-badge ${sigClass}">${_esc(f.signal || 'NEUTRAL')}</span></td>
        <td style="max-width:260px;font-size:11px;color:var(--text-secondary);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(f.summary || '—')}</td>
        <td>
          ${aiBadge}
          <button class="btn btn-ghost btn-sm" style="margin-left:4px" onclick="event.stopPropagation();toggleFilingExpand('fexpand-${idx}')">ANALYZE</button>
          <a href="${_esc(safeUrl(f.filing_url))}" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="margin-left:4px;font-size:10px;color:var(--blue)">VIEW ↗</a>
        </td>
      </tr>
      <tr class="filing-row-expand" id="fexpand-${idx}">
        <td colspan="7">
          <div style="display:flex;gap:16px;align-items:flex-start">
            <div style="flex:1">
              <div style="font-size:10px;color:var(--text-dim);margin-bottom:6px">KEY POINTS</div>
              <ul class="key-points-list">
                ${(f.key_points || []).map(p => `<li>${_esc(p)}</li>`).join('')}
              </ul>
            </div>
            <div style="flex:2">
              <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">SUMMARY</div>
              <div style="font-size:11px;color:var(--text-secondary);line-height:1.6">${_esc(f.summary || '—')}</div>
            </div>
            <div>
              <a href="${_esc(safeUrl(f.filing_url))}" target="_blank" rel="noopener" class="btn btn-ghost btn-sm">VIEW FILING ↗</a>
            </div>
          </div>
        </td>
      </tr>
    `;
  }).join('');

  panel.innerHTML = header + `
    <div class="filing-table-wrap">
      <table class="data-table" style="min-width:900px">
        <thead>
          <tr>
            <th>DATE</th><th>SYMBOL</th><th>TYPE</th><th>EVENT</th><th>SIGNAL</th><th>SUMMARY</th><th>ACTIONS</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function toggleFilingExpand(rowId) {
  const row = document.getElementById(rowId);
  if (row) row.classList.toggle('open');
}

// ── Insider Tracker ───────────────────────────────────────────────────────────

// Pick a sensible default symbol for ticker-driven panels: the first
// non-crypto holding in the user's portfolio, falling back to AAPL
// only when the portfolio is empty or unavailable.
function _firstPortfolioSymbol() {
  try {
    const holdings = (State.portfolio && State.portfolio.holdings) || [];
    const first = holdings.find(h => h.asset_type !== 'crypto');
    if (first && first.symbol) return first.symbol;
  } catch(e) {}
  return 'AAPL';
}

async function loadIntelInsiders() {
  const panel = document.getElementById('intel-panel-insiders');
  if (!panel) return;

  const defaultSym = _firstPortfolioSymbol();

  panel.innerHTML = `
    <div class="sec-symbol-search">
      <input class="form-input sec-symbol-input" id="insider-symbol-input" placeholder="Enter symbol e.g. AAPL, MSFT..." value="${defaultSym}">
      <button class="btn btn-green btn-sm" onclick="searchInsiders()">SEARCH</button>
    </div>
    <div id="insider-results">
      <div class="loading"><div class="spinner"></div></div>
    </div>
    <div id="finviz-insider-feed"></div>
  `;
  document.getElementById('insider-symbol-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') searchInsiders();
  });

  // Auto-load first symbol
  await loadInsidersFor(defaultSym);

  // Cross-ticker Finviz insider feed (latest Form 4 across all US equities)
  DataPanels.appendFinvizInsiders(document.getElementById('finviz-insider-feed'));
}

async function searchInsiders() {
  const input = document.getElementById('insider-symbol-input');
  const sym = (input ? input.value.trim().toUpperCase() : '') || _firstPortfolioSymbol();
  await loadInsidersFor(sym);
}

async function loadInsidersFor(symbol) {
  const panel = document.getElementById('insider-results');
  if (!panel) return;
  panel.innerHTML = `<div class="loading"><div class="spinner"></div> Fetching Form 4 filings for ${symbol}...</div>`;
  try {
    const data = await API.get(`/api/intel/insiders/${symbol}`);
    renderInsiders(data, panel, symbol);
  } catch(e) {
    panel.innerHTML = `<div class="empty-state"><span class="text-red">Failed: ${_esc(e.message)}</span></div>`;
  }
}

function renderInsiders(data, panel, symbol) {
  const txns = data.transactions || [];
  const pattern = data.pattern || {};

  const sigClass = 'signal-' + (pattern.signal || 'neutral').toLowerCase();
  const patternBar = `
    <div class="insider-pattern-bar">
      <span class="signal-badge ${sigClass}">${_esc(pattern.signal || 'NEUTRAL')}</span>
      <span style="flex:1;color:var(--text-secondary)">${_esc(pattern.summary || '—')}</span>
      ${pattern.buy_value ? `<span style="color:var(--green);font-size:10px">BUY: $${fmt.compact(pattern.buy_value)}</span>` : ''}
      ${pattern.sell_value ? `<span style="color:var(--red);font-size:10px">SELL: $${fmt.compact(pattern.sell_value)}</span>` : ''}
      ${pattern.ai_powered ? '<span class="ai-badge">AI ANALYSIS</span>' : '<span class="rule-badge">AUTO</span>'}
    </div>
  `;

  if (!txns.length) {
    panel.innerHTML = patternBar + `<div class="empty-state"><span>No recent insider transactions found for ${symbol}</span></div>`;
    return;
  }

  const rows = txns.map(t => {
    const isBuy = t.transaction_type === 'BUY';
    const rowCls = isBuy ? 'insider-buy-row' : 'insider-sell-row';
    const actionCls = isBuy ? 'col-positive' : 'col-negative';
    return `
      <tr class="${rowCls}">
        <td style="font-size:11px;color:var(--text-dim)">${_esc(t.date || '—')}</td>
        <td style="font-size:11px">${_esc(t.insider_name || '—')}</td>
        <td style="font-size:10px;color:var(--text-dim)">${_esc(t.title || (t.is_director ? 'Director' : t.is_officer ? 'Officer' : '—'))}</td>
        <td><span class="${actionCls}" style="font-weight:700;font-size:11px">${_esc(t.transaction_type)}</span></td>
        <td style="font-size:11px">${_esc(t.security || 'Common Stock')}</td>
        <td style="font-size:11px;text-align:right">${t.shares ? fmt.compact(t.shares) : '—'}</td>
        <td style="font-size:11px;text-align:right">${t.price ? '$' + fmt.price(t.price) : '—'}</td>
        <td style="font-size:11px;text-align:right;color:${isBuy ? 'var(--green)' : 'var(--red)'}">${t.value ? '$' + fmt.compact(t.value) : '—'}</td>
        <td style="font-size:11px;text-align:right;color:var(--text-dim)">${t.shares_after ? fmt.compact(t.shares_after) : '—'}</td>
        <td><a href="${_esc(safeUrl(t.form_url))}" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue)">FORM4 ↗</a></td>
      </tr>
    `;
  }).join('');

  panel.innerHTML = patternBar + `
    <div class="insider-table-wrap">
      <table class="data-table" style="min-width:900px">
        <thead>
          <tr>
            <th>DATE</th><th>INSIDER</th><th>TITLE</th><th>ACTION</th><th>SECURITY</th>
            <th style="text-align:right">SHARES</th><th style="text-align:right">PRICE</th>
            <th style="text-align:right">VALUE</th><th style="text-align:right">HELD AFTER</th><th>FILING</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

// ── Institutional Holdings ────────────────────────────────────────────────────

async function loadIntelInstitutional() {
  const panel = document.getElementById('intel-panel-institutional');
  if (!panel) return;
  panel.innerHTML = `<div class="loading"><div class="spinner"></div> Loading 13-F filings for tracked funds...</div>`;
  try {
    const data = await API.get('/api/intel/institutional');
    IntelState.institutionalData = data;
    renderInstitutional(data, panel);
  } catch(e) {
    panel.innerHTML = `<div class="empty-state"><span class="text-red">Failed: ${_esc(e.message)}</span></div>`;
  }
}

function renderInstitutional(data, panel) {
  const funds = Object.keys(data);
  if (!funds.length) {
    panel.innerHTML = '<div class="empty-state"><span>No institutional data available</span></div>';
    return;
  }

  const cards = funds.map((name, i) => {
    const fd = data[name];
    const hasError = fd.error && !fd.holdings?.length;
    const val = fd.total_value ? '$' + fmt.compact(fd.total_value) : '—';
    const numH = fd.num_holdings || fd.holdings?.length || 0;
    const overlap = fd.overlap_with_portfolio?.length || 0;
    // Use index as the ID suffix; whitespace-collapsing produced collisions
    // between e.g. "Berkshire Hathaway" and "Berkshire  Hathaway" (double
    // space), and broke entirely on names with non-alphanumeric chars.
    return `
      <div class="fund-card" id="fcard-${i}" onclick="showFundHoldings('${_esc(_jesc(name))}', ${i})">
        <div style="font-size:11px;font-weight:700;color:var(--green);margin-bottom:6px">${_esc(name)}</div>
        ${hasError
          ? `<div style="font-size:10px;color:var(--red)">Data unavailable</div>`
          : `
            <div style="font-size:10px;color:var(--text-dim)">AUM: <span style="color:var(--text-primary)">${val}</span></div>
            <div style="font-size:10px;color:var(--text-dim)">Holdings: <span style="color:var(--text-primary)">${numH}</span></div>
            <div style="font-size:10px;color:var(--text-dim)">Filed: <span style="color:var(--text-primary)">${_esc(fd.filing_date || '—')}</span></div>
            ${overlap ? `<div style="font-size:10px;color:var(--amber);margin-top:4px">◈ ${overlap} PORTFOLIO OVERLAP</div>` : ''}
          `
        }
      </div>
    `;
  }).join('');

  panel.innerHTML = `
    <div class="fund-cards-grid">${cards}</div>
    <div id="fund-holdings-panel"></div>
  `;
}

function showFundHoldings(fundName, idx) {
  // Update active card. Cards use index-based IDs (fcard-${i}); look up by the
  // same index — the old fundName-based lookup never matched, so the active
  // highlight silently never applied.
  document.querySelectorAll('.fund-card').forEach(c => c.classList.remove('active'));
  const card = idx != null ? document.getElementById('fcard-' + idx) : null;
  if (card) card.classList.add('active');

  const panel = document.getElementById('fund-holdings-panel');
  if (!panel) return;

  const data = IntelState.institutionalData;
  if (!data || !data[fundName]) {
    panel.innerHTML = '<div class="empty-state"><span>No data for this fund</span></div>';
    return;
  }

  const fd = data[fundName];
  const holdings = (fd.holdings || []).slice(0, 25);
  const overlap = new Set((fd.overlap_with_portfolio || []).map(h => h.cusip || h.name));

  if (!holdings.length) {
    panel.innerHTML = `<div class="empty-state"><span>${fd.error || 'No holdings data available'}</span></div>`;
    return;
  }

  // Overlap section
  const overlapItems = fd.overlap_with_portfolio || [];
  const overlapSection = overlapItems.length ? `
    <div class="panel" style="margin-bottom:8px;border-color:var(--amber)">
      <div class="panel-header" style="border-color:var(--amber)">
        <span class="panel-title" style="color:var(--amber)">◈ OVERLAP WITH MY PORTFOLIO — ${overlapItems.length} SHARED POSITIONS</span>
      </div>
      <div class="panel-body" style="display:flex;flex-wrap:wrap;gap:8px">
        ${overlapItems.map(h => `
          <div style="background:var(--bg-elevated);border:1px solid var(--amber-dim);padding:6px 10px;border-radius:2px">
            <div style="font-size:11px;font-weight:700;color:var(--amber)">${_esc(h.portfolio_symbol)}</div>
            <div style="font-size:10px;color:var(--text-dim)">${_esc(h.name)}</div>
            <div style="font-size:10px;color:var(--text-primary)">$${fmt.compact(h.value_usd)}</div>
            <div style="font-size:10px;color:var(--text-dim)">${fmt.num(h.pct_of_portfolio)}% of fund</div>
          </div>
        `).join('')}
      </div>
    </div>
  ` : '';

  const rows = holdings.map((h, i) => {
    const isOverlap = overlap.has(h.cusip) || overlap.has(h.name);
    return `
      <tr class="${isOverlap ? 'overlap-highlight' : ''}">
        <td style="font-size:11px;color:var(--text-dim)">${i + 1}</td>
        <td style="font-size:11px;font-weight:600">${_esc(h.name || '—')}</td>
        <td style="font-size:11px;text-align:right">$${fmt.compact(h.value_usd)}</td>
        <td style="font-size:11px;text-align:right">${h.shares ? fmt.compact(h.shares) : '—'}</td>
        <td style="font-size:11px;text-align:right;color:var(--text-dim)">${fmt.num(h.pct_of_portfolio)}%</td>
        ${isOverlap ? '<td><span style="color:var(--amber);font-size:10px">◈ IN PORTFOLIO</span></td>' : '<td></td>'}
      </tr>
    `;
  }).join('');

  panel.innerHTML = overlapSection + `
    <div class="panel">
      <div class="panel-header">
        <span class="panel-title">TOP HOLDINGS — ${_esc(fundName)}</span>
        <span style="font-size:10px;color:var(--text-dim)">Period: ${_esc(fd.period_of_report || '—')} | AUM: $${fmt.compact(fd.total_value)}</span>
      </div>
      <div style="overflow-x:auto">
        <table class="data-table">
          <thead>
            <tr><th>#</th><th>NAME</th><th style="text-align:right">VALUE</th><th style="text-align:right">SHARES</th><th style="text-align:right">% PORTFOLIO</th><th></th></tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}

// ── Research SEC Tab ──────────────────────────────────────────────────────────

async function loadResearchSecTab(symbol) {
  const filingsEl = document.getElementById('sec-filings-' + symbol);
  const insidersEl = document.getElementById('sec-insiders-' + symbol);

  // Load both in parallel
  const [filingsData, insidersData] = await Promise.allSettled([
    API.get('/api/intel/feed').catch(() => []),
    API.get('/api/intel/insiders/' + symbol).catch(() => ({ transactions: [] })),
  ]);

  // Filings for this symbol
  if (filingsEl && filingsEl.isConnected) {
    const allFilings = filingsData.status === 'fulfilled' ? filingsData.value : [];
    const symFilings = allFilings.filter(f => f.ticker === symbol.toUpperCase());
    if (!symFilings.length) {
      filingsEl.innerHTML = '<div class="empty-state" style="font-size:11px"><span>No recent filings found</span></div>';
    } else {
      filingsEl.innerHTML = symFilings.slice(0, 10).map(f => {
        const sigClass = 'signal-' + (f.signal || 'neutral').toLowerCase();
        return `
          <div style="padding:8px;border-bottom:1px solid var(--border)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
              <span class="badge badge-blue">${_esc(f.form_type)}</span>
              <span class="signal-badge ${sigClass}">${_esc(f.signal)}</span>
              <span style="font-size:10px;color:var(--text-dim)">${_esc(f.filing_date)}</span>
              ${f.ai_powered ? '<span class="ai-badge">AI</span>' : ''}
              <a href="${_esc(safeUrl(f.filing_url))}" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue);margin-left:auto">VIEW ↗</a>
            </div>
            <div style="font-size:11px;color:var(--text-secondary)">${_esc(f.summary || f.description || '—')}</div>
          </div>
        `;
      }).join('');
    }
  }

  // Insiders
  if (insidersEl && insidersEl.isConnected) {
    const iData = insidersData.status === 'fulfilled' ? insidersData.value : { transactions: [] };
    const txns = iData.transactions || [];
    if (!txns.length) {
      insidersEl.innerHTML = '<div class="empty-state" style="font-size:11px"><span>No insider transactions found</span></div>';
    } else {
      insidersEl.innerHTML = txns.slice(0, 15).map(t => {
        const isBuy = t.transaction_type === 'BUY';
        return `
          <div style="padding:6px 8px;border-bottom:1px solid var(--border);border-left:3px solid ${isBuy ? 'var(--green)' : 'var(--red)'}">
            <div style="display:flex;justify-content:space-between;font-size:11px">
              <span style="font-weight:600">${_esc(t.insider_name)}</span>
              <span class="${isBuy ? 'col-positive' : 'col-negative'}" style="font-weight:700">${_esc(t.transaction_type)}</span>
            </div>
            <div style="font-size:10px;color:var(--text-dim)">${_esc(t.title || '')} &bull; ${_esc(t.date)}</div>
            <div style="font-size:11px">${t.shares ? fmt.compact(t.shares) + ' shares' : ''} ${t.price ? '@ $' + fmt.price(t.price) : ''} ${t.value ? '= $' + fmt.compact(t.value) : ''}</div>
          </div>
        `;
      }).join('');
    }
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ANIMATED LOADING INDICATOR (shared across all 3 new views)
// ══════════════════════════════════════════════════════════════════════════════

function _loadingStages(containerId, stages, intervalMs = 2500) {
  let idx = 0;
  const el = document.getElementById(containerId);
  if (!el) return { stop() {} };
  const render = () => {
    if (!el.parentNode || !el.querySelector('.scan-loading')) return;
    const stagesHTML = stages.map((s, i) => {
      const state = i < idx ? 'done' : i === idx ? 'active' : 'pending';
      const icon = state === 'done' ? '<span class="col-positive">✓</span>' : state === 'active' ? '<span class="spinner-sm"></span>' : '<span style="color:var(--text-dim)">○</span>';
      const cls = state === 'done' ? 'color:var(--text-secondary)' : state === 'active' ? 'color:var(--green);font-weight:600' : 'color:var(--text-dim)';
      return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;font-size:11px;${cls}">${icon} ${s}</div>`;
    }).join('');
    const scanEl = el.querySelector('.scan-loading');
    if (scanEl) scanEl.innerHTML = `<div style="padding:24px;max-width:360px;margin:0 auto">${stagesHTML}</div>`;
  };
  render();
  const timer = setInterval(() => {
    // Self-clear if the loader's node was detached by navigation — avoids
    // firing against a node that is no longer in the document.
    if (!el.isConnected || !el.querySelector('.scan-loading')) { clearInterval(timer); return; }
    if (idx < stages.length - 1) { idx++; render(); }
    else clearInterval(timer);
  }, intervalMs);
  return { stop() { clearInterval(timer); } };
}


// ══════════════════════════════════════════════════════════════════════════════
// SMART MONEY CONVERGENCE SCORE (SIGNALS VIEW)
// ══════════════════════════════════════════════════════════════════════════════

let _signalsCache = [];

async function loadSignalsView() {
  const el = document.getElementById('view-signals');
  const explainerHTML = [
    ['Insider Activity', '0–20', 'Form 4 buy/sell ratio from EDGAR — cluster buying by 3+ insiders adds bonus'],
    ['Institutional Flow', '0–15', 'Institutional ownership %, short float, and days-to-cover'],
    ['Earnings Quality', '0–15', 'Forward EPS vs trailing, profit margin, revenue growth trajectory'],
    ['Options Signal', '0–10', 'IV premium vs historical vol — cheap options = bullish entry signal'],
    ['Price Momentum', '0–10', 'Multi-timeframe composite return + MA alignment (50/200-day)'],
    ['Filing Sentiment', '0–10', 'AI-analyzed SEC 8-K/10-Q signal from recent filings'],
    ['ML Forecast', '0–20', 'Random Forest classifier + trend regression + regime detection + mean reversion'],
  ].map(([name, range, desc]) => `
    <div class="stat-card" style="padding:12px">
      <div style="font-size:11px;color:var(--green);font-weight:600;margin-bottom:4px">${name} <span style="color:var(--text-dim)">${range}</span></div>
      <div style="font-size:10px;color:var(--text-secondary)">${desc}</div>
    </div>
  `).join('');

  el.innerHTML = `
    <div class="view-header">
      <div>
        <div class="view-title">◈ SMART MONEY CONVERGENCE SCORE</div>
        <div class="view-subtitle">Multi-factor institutional conviction scoring — 6 real-time data dimensions</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <button class="btn btn-green" id="signals-scan-btn" onclick="runSignalsScan()">⟳ SCAN PORTFOLIO</button>
        <input id="signals-symbol-input" type="text" class="filter-input" placeholder="Add symbol..." style="width:120px" onkeydown="if(event.key==='Enter')addSignalSymbol()" />
        <button class="btn btn-ghost btn-sm" onclick="addSignalSymbol()">+ ADD</button>
      </div>
    </div>
    <div id="signals-meta" style="padding:6px 24px;font-size:11px;color:var(--text-dim)"></div>
    <div id="signals-grid" style="padding:0 24px 24px"></div>
    <div id="signals-explainer" style="padding:0 24px 32px">
      <div class="section-header">SCORE METHODOLOGY</div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">${explainerHTML}</div>
    </div>
  `;

  // If we have cached data, render immediately, otherwise auto-scan
  if (_signalsCache.length) {
    renderSignalsGrid(_signalsCache);
    document.getElementById('signals-meta').textContent = `${_signalsCache.length} symbols scored — click SCAN to refresh`;
  } else {
    runSignalsScan();
  }
}

async function runSignalsScan(extraSymbols) {
  const grid = document.getElementById('signals-grid');
  const meta = document.getElementById('signals-meta');
  const btn = document.getElementById('signals-scan-btn');
  if (!grid) return;

  if (btn) { btn.disabled = true; btn.textContent = 'SCANNING...'; }

  grid.innerHTML = `<div class="scan-loading"></div>`;
  const loader = _loadingStages('signals-grid', [
    'Fetching portfolio positions...',
    'Querying EDGAR Form 4 insider filings...',
    'Analyzing institutional ownership & short interest...',
    'Computing earnings quality metrics...',
    'Scanning options chains for IV signals...',
    'Calculating multi-timeframe momentum...',
    'Aggregating SEC filing sentiment...',
    'Training Random Forest classifier on historical data...',
    'Running trend regression & regime detection...',
    'Computing composite ML forecast scores...',
  ], 3000);

  if (meta) meta.textContent = 'Deep scan in progress — analyzing 6 signal dimensions per symbol...';

  try {
    const payload = extraSymbols ? { symbols: extraSymbols } : {};
    const data = await API.post('/api/smart-money/scores', payload);
    loader.stop();
    if (data.error) throw new Error(data.error);
    _signalsCache = data.scores || [];
    renderSignalsGrid(_signalsCache);
    if (meta) meta.textContent = `${_signalsCache.length} symbols scored — ranked by conviction`;
  } catch (e) {
    loader.stop();
    grid.innerHTML = `<div class="empty-state"><span class="col-red">Error: ${_esc(e.message)}</span></div>`;
    if (meta) meta.textContent = '';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟳ SCAN PORTFOLIO'; }
  }
}

async function addSignalSymbol() {
  const input = document.getElementById('signals-symbol-input');
  if (!input || !input.value.trim()) return;
  const sym = input.value.trim().toUpperCase();
  input.value = '';
  const meta = document.getElementById('signals-meta');
  const grid = document.getElementById('signals-grid');

  // Show inline loading for this symbol
  if (meta) meta.innerHTML = `<span class="spinner-sm" style="margin-right:6px"></span> Scoring <strong>${sym}</strong> — fetching insider, options, momentum data...`;

  try {
    const data = await API.get(`/api/smart-money/score/${sym}`);
    if (data.error) throw new Error(data.error);
    // Add to cache and re-render
    _signalsCache = [data, ..._signalsCache.filter(s => s.symbol !== sym)];
    _signalsCache.sort((a, b) => (b.score || 0) - (a.score || 0));
    renderSignalsGrid(_signalsCache);
    if (meta) meta.textContent = `${sym} added — ${_signalsCache.length} symbols scored`;
    Toast.show(`${sym} score: ${data.score}/100 — ${data.signal}`, data.score >= 60 ? 'green' : 'amber');
  } catch(e) {
    Toast.show(`${sym}: ${_esc(e.message)}`, 'red');
    if (meta) meta.textContent = _signalsCache.length ? `${_signalsCache.length} symbols scored` : '';
  }
}

function renderSignalsGrid(scores) {
  const grid = document.getElementById('signals-grid');
  if (!grid) return;
  if (!scores.length) {
    grid.innerHTML = '<div class="empty-state"><span>No portfolio stocks found. Add positions or search a symbol above.</span></div>';
    return;
  }
  grid.innerHTML = `<div class="signals-grid-inner">${scores.map(s => buildSignalCard(s)).join('')}</div>`;
  // v0.2.0 — mount Smart Money track-record badges into the placeholders.
  if (window.renderTrackBadge) {
    grid.querySelectorAll('.sm-track-badge').forEach(function(el) {
      try { window.renderTrackBadge(el, 'smart_money'); } catch(_) {}
    });
  }
}

function buildSignalCard(s) {
  const sym = s && s.symbol ? String(s.symbol) : '';
  if (!sym) return '';
  const components = s.components || {};
  const cardId = 'ml-panel-' + sym.replace(/[^A-Z0-9]/g, '');

  const bars = Object.entries(components).map(([key, c]) => {
    const pct = c.max ? Math.round((c.score / c.max) * 100) : 0;  // avoid NaN/Infinity width
    const barColor = pct >= 70 ? 'var(--green)' : pct >= 40 ? 'var(--amber)' : 'var(--red)';
    const isML = key === 'ml_forecast';
    return `
      <div style="margin-bottom:5px">
        <div style="display:flex;justify-content:space-between;font-size:9px;color:var(--text-dim);margin-bottom:2px">
          <span>${isML ? '⚡ ' : ''}${_esc(c.label)}</span><span>${c.score}/${c.max}</span>
        </div>
        <div style="height:5px;background:var(--border);border-radius:3px;overflow:hidden">
          <div class="score-bar-fill" style="width:${pct}%;height:100%;background:${isML ? 'var(--cyan, #0ff)' : barColor};border-radius:3px"></div>
        </div>
      </div>
    `;
  }).join('');

  const sc = s.score || 0;
  const scoreColor = sc >= 75 ? 'var(--green)' : sc >= 60 ? '#7fff7f' : sc >= 45 ? 'var(--amber)' : sc >= 30 ? '#ff9800' : 'var(--red)';

  // Ring gauge SVG
  const ringPct = Math.min(100, Math.max(0, sc));
  const circumf = 2 * Math.PI * 32;
  const dashOffset = circumf * (1 - ringPct / 100);
  const ringHTML = `
    <svg width="76" height="76" viewBox="0 0 76 76" style="display:block">
      <circle cx="38" cy="38" r="32" fill="none" stroke="var(--border)" stroke-width="5"/>
      <circle cx="38" cy="38" r="32" fill="none" stroke="${scoreColor}" stroke-width="5"
        stroke-dasharray="${circumf}" stroke-dashoffset="${dashOffset}"
        stroke-linecap="round" transform="rotate(-90 38 38)" style="transition:stroke-dashoffset .8s ease"/>
      <text x="38" y="35" text-anchor="middle" fill="${scoreColor}" font-size="20" font-weight="700" font-family="monospace">${sc}</text>
      <text x="38" y="48" text-anchor="middle" fill="var(--text-dim)" font-size="8" font-family="monospace">/100</text>
    </svg>
  `;

  const signalColors = {'STRONG BUY': 'col-positive', 'BUY': 'col-green', 'NEUTRAL': 'col-yellow', 'CAUTION': 'col-amber', 'AVOID': 'col-negative'};

  return `
    <div class="smart-money-card">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:10px">
        <div>
          <div style="font-size:15px;font-weight:700;color:var(--text-primary)">${_esc(sym)}</div>
          <div style="font-size:10px;color:var(--text-dim);max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(s.name || '')}</div>
          <div style="margin-top:6px">
            <span class="signal-badge ${signalColors[s.signal] || ''}" style="font-size:11px">${_esc(s.signal)}</span>
            <span class="sm-track-badge" data-signal="smart_money" style="margin-left:6px"></span>
          </div>
          ${s.price ? `<div style="font-size:11px;color:var(--text-secondary);margin-top:4px">$${fmt.price(s.price)}</div>` : ''}
        </div>
        <div>${ringHTML}</div>
      </div>
      <div style="margin-top:4px">${bars}</div>
      <div style="margin-top:8px;border-top:1px solid var(--border);padding-top:8px">
        <button class="btn btn-ghost btn-sm" style="width:100%;font-size:10px" onclick="toggleMLPanel('${cardId}', '${_jesc(sym)}')">
          ⚡ ML FORECAST DETAIL ▾
        </button>
        <div id="${cardId}" class="ml-detail-panel" style="display:none;margin-top:8px"></div>
      </div>
      ${s.computed_in_ms ? `<div style="font-size:9px;color:var(--text-dim);margin-top:6px;text-align:right">computed in ${(s.computed_in_ms/1000).toFixed(1)}s</div>` : ''}
    </div>
  `;
}

async function toggleMLPanel(panelId, symbol) {
  const panel = document.getElementById(panelId);
  if (!panel) return;

  if (panel.style.display !== 'none') {
    panel.style.display = 'none';
    return;
  }

  panel.style.display = 'block';

  // If already loaded, just show
  if (panel.dataset.loaded) return;

  panel.innerHTML = `<div style="text-align:center;padding:12px"><span class="spinner-sm" style="margin-right:6px"></span> <span style="font-size:10px;color:var(--text-dim)">Loading ML forecast for ${symbol}...</span></div>`;

  try {
    const data = await API.get(`/api/smart-money/ml-forecast/${symbol}`);
    if (data.error) throw new Error(data.error);
    panel.dataset.loaded = '1';
    panel.innerHTML = renderMLForecastPanel(data)
      + '<div style="margin-top:6px;text-align:right" class="ml-track-slot"></div>';
    // v0.2.0 — mount ML forecast tracker badge.
    if (window.renderTrackBadge) {
      const slot = panel.querySelector('.ml-track-slot');
      if (slot) {
        try { window.renderTrackBadge(slot, 'ml_forecast'); } catch(_) {}
      }
    }
  } catch(e) {
    panel.innerHTML = `<div style="font-size:10px;color:var(--red);padding:8px">ML Error: ${_esc(e.message)}</div>`;
  }
}

function renderMLForecastPanel(data) {
  const rf = data.rf_classifier || {};
  const trend = data.trend_forecast || {};
  const regime = data.regime || {};
  const mr = data.mean_reversion || {};

  const probUp = Math.round((rf.prob_up_20d || 0) * 100);
  const rfColor = probUp >= 60 ? 'var(--green)' : probUp <= 40 ? 'var(--red)' : 'var(--amber)';
  const rfAccuracy = Math.round((rf.accuracy_recent || 0) * 100);

  // Regime badge colors
  const regimeColors = {
    'COMPRESSION': '#0af',
    'BREAKOUT': '#f80',
    'HIGH VOL CHOP': '#f44',
    'LOW VOL TREND': '#4f4',
  };
  const regimeLabel = regime.current_regime || 'N/A';
  const regimeColor = regimeColors[regimeLabel] || 'var(--text-dim)';

  // MR signal
  const mrSignalColors = {'OVERSOLD': 'var(--green)', 'OVERBOUGHT': 'var(--red)', 'FAIR VALUE': 'var(--amber)', 'FLAT': 'var(--text-dim)'};
  const mrSig = mr.signal || 'N/A';
  const mrColor = mrSignalColors[mrSig] || 'var(--text-dim)';

  // Feature importances
  const feats = (rf.top_features || []).slice(0, 6);
  const maxImp = feats.length ? Math.max(...feats.map(f => f.importance)) : 1;
  const featBars = feats.map(f => {
    const pct = Math.round((f.importance / maxImp) * 100);
    return `<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
      <span style="font-size:9px;color:var(--text-dim);width:80px;text-align:right;flex-shrink:0">${_esc(f.name)}</span>
      <div style="flex:1;height:4px;background:var(--border);border-radius:2px;overflow:hidden">
        <div style="width:${pct}%;height:100%;background:var(--cyan, #0ff);border-radius:2px"></div>
      </div>
      <span style="font-size:8px;color:var(--text-dim);width:30px">${(f.importance * 100).toFixed(1)}%</span>
    </div>`;
  }).join('');

  // Trend forecast mini chart (SVG sparkline)
  const path = trend.path || [];
  let chartSVG = '';
  if (path.length > 2) {
    const w = 260, h = 80, pad = 4;
    const prices = path.map(p => p.price);
    const uppers = path.map(p => p.upper);
    const lowers = path.map(p => p.lower);
    const allVals = [...prices, ...uppers, ...lowers];
    const minY = Math.min(...allVals);
    const maxY = Math.max(...allVals);
    const rangeY = maxY - minY || 1;
    const sx = (i) => pad + (i / (path.length - 1)) * (w - 2 * pad);
    const sy = (v) => pad + (1 - (v - minY) / rangeY) * (h - 2 * pad);

    const ciPath = path.map((p, i) => `${sx(i)},${sy(p.upper)}`).join(' ') + ' ' +
                   [...path].reverse().map((p, i) => `${sx(path.length - 1 - i)},${sy(p.lower)}`).join(' ');
    const priceLine = path.map((p, i) => `${i === 0 ? 'M' : 'L'}${sx(i)},${sy(p.price)}`).join(' ');

    const fcPct = trend.forecast_pct || 0;
    const fcColor = fcPct >= 0 ? 'var(--green)' : 'var(--red)';

    chartSVG = `
      <div style="margin-top:8px">
        <div style="font-size:9px;color:var(--text-dim);margin-bottom:4px">30-DAY TREND FORECAST</div>
        <svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" style="display:block;background:rgba(0,0,0,0.2);border-radius:4px">
          <polygon points="${ciPath}" fill="rgba(0,200,255,0.08)" stroke="none"/>
          <path d="${priceLine}" fill="none" stroke="${fcColor}" stroke-width="1.5"/>
          <circle cx="${sx(0)}" cy="${sy(prices[0])}" r="2.5" fill="${fcColor}"/>
          <circle cx="${sx(path.length-1)}" cy="${sy(prices[prices.length-1])}" r="2.5" fill="${fcColor}"/>
        </svg>
        <div style="display:flex;justify-content:space-between;font-size:8px;color:var(--text-dim);margin-top:2px">
          <span>Today</span>
          <span style="color:${fcColor}">${fcPct >= 0 ? '+' : ''}${fcPct.toFixed(1)}% → $${(trend.forecast_price || 0).toFixed(2)}</span>
          <span>+30d</span>
        </div>
      </div>`;
  }

  // Composite signal
  const compSig = data.composite_signal || 'HOLD';
  const compScore = Math.round((data.composite_ml_score || 0.5) * 100);
  const compColor = compSig === 'BUY' || compSig === 'STRONG BUY' ? 'var(--green)' :
                    compSig === 'SELL' || compSig === 'STRONG SELL' ? 'var(--red)' : 'var(--amber)';

  return `
    <div class="ml-forecast-content">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid var(--border)">
        <div>
          <span style="font-size:12px;font-weight:700;color:${compColor}">${compSig}</span>
          <span style="font-size:9px;color:var(--text-dim);margin-left:6px">ML Score: ${compScore}/100</span>
        </div>
        <span style="font-size:9px;color:var(--text-dim)">${data.computed_in_ms ? (data.computed_in_ms/1000).toFixed(1)+'s' : ''}</span>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
        <div class="ml-stat-box">
          <div class="ml-stat-label">RF P(UP 20d)</div>
          <div class="ml-stat-value" style="color:${rfColor}">${probUp}%</div>
          <div class="ml-stat-sub">Accuracy: ${rfAccuracy}%</div>
          <div style="height:4px;background:var(--border);border-radius:2px;margin-top:4px;overflow:hidden">
            <div style="width:${probUp}%;height:100%;background:${rfColor};border-radius:2px"></div>
          </div>
        </div>
        <div class="ml-stat-box">
          <div class="ml-stat-label">REGIME</div>
          <div class="ml-stat-value" style="color:${regimeColor};font-size:11px">${regimeLabel}</div>
          <div class="ml-stat-sub">${regime.days_in_regime || '?'} days in regime</div>
        </div>
        <div class="ml-stat-box">
          <div class="ml-stat-label">MEAN REVERSION</div>
          <div class="ml-stat-value" style="color:${mrColor}">${mrSig}</div>
          <div class="ml-stat-sub">z-score: ${(mr.zscore || 0).toFixed(2)} | t½: ${(mr.half_life_days || 0).toFixed(0)}d</div>
        </div>
        <div class="ml-stat-box">
          <div class="ml-stat-label">TREND</div>
          <div class="ml-stat-value" style="color:${(trend.trend_short || '') === 'UP' ? 'var(--green)' : 'var(--red)'}">${_esc(trend.trend_short || 'N/A')}</div>
          <div class="ml-stat-sub">${trend.trends_aligned ? 'Trends aligned' : 'Mixed signals'}</div>
        </div>
      </div>

      ${chartSVG}

      ${feats.length ? `
        <div style="margin-top:10px">
          <div style="font-size:9px;color:var(--text-dim);margin-bottom:6px">TOP FEATURES (RF Importance)</div>
          ${featBars}
        </div>
      ` : ''}
    </div>
  `;
}


// ══════════════════════════════════════════════════════════════════════════════
// WEB TERMINAL
// ══════════════════════════════════════════════════════════════════════════════

const _termHistory = [];
let _termHistIdx = -1;

function loadTerminalView() {
  const el = document.getElementById('view-terminal');
  el.innerHTML = `
    <div class="term-container">
      <div class="term-header">
        <span class="term-title">▸ AUGUR SHELL</span>
        <span class="term-subtitle">Type a command below — try <code>help</code> or <code>quote AAPL</code></span>
        <button class="btn btn-ghost btn-sm" onclick="termClear()" style="margin-left:auto">CLEAR</button>
      </div>
      <div id="term-output" class="term-output"></div>
      <div class="term-input-row">
        <span class="term-prompt">augur ▸</span>
        <input id="term-input" class="term-input" type="text" autofocus spellcheck="false" autocomplete="off"
          placeholder="Enter command..."
          onkeydown="termKeyDown(event)" />
      </div>
      <div class="term-help-bar">
        ${['quote AAPL','portfolio list','smart-money score NVDA','ml-forecast TSLA','market indices','chart MSFT','congress','options flow SPY','analytics risk','earnings calendar','intel insiders AAPL','crypto market','dividends','macro'].map(c =>
          `<button class="term-quick-btn" onclick="termRun('${c}')">${c}</button>`
        ).join('')}
      </div>
    </div>
  `;
  // Print welcome
  const out = document.getElementById('term-output');
  out.innerHTML = _ansiToHtml(
    '\\033[36m══════════════════════════════════════════════════\\033[0m\\n' +
    '\\033[97m\\033[1m  AUGUR // WEALTH INTELLIGENCE SYSTEM\\033[0m\\n' +
    '\\033[36m══════════════════════════════════════════════════\\033[0m\\n\\n' +
    '\\033[2m  Web Terminal — all CLI commands available here.\\033[0m\\n' +
    '\\033[2m  Type \\033[36mhelp\\033[2m for the full command list.\\033[0m\\n\\n'
  );
  document.getElementById('term-input').focus();
}

function termKeyDown(e) {
  if (e.key === 'Enter') {
    e.preventDefault();
    const input = document.getElementById('term-input');
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = '';
    _termHistory.unshift(cmd);
    _termHistIdx = -1;
    termRun(cmd);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (_termHistIdx < _termHistory.length - 1) {
      _termHistIdx++;
      document.getElementById('term-input').value = _termHistory[_termHistIdx];
    }
  } else if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (_termHistIdx > 0) {
      _termHistIdx--;
      document.getElementById('term-input').value = _termHistory[_termHistIdx];
    } else {
      _termHistIdx = -1;
      document.getElementById('term-input').value = '';
    }
  }
}

async function termRun(cmd) {
  const out = document.getElementById('term-output');
  const input = document.getElementById('term-input');
  if (!out) return;

  // Echo command
  out.innerHTML += `<div class="term-cmd-line"><span class="term-prompt-echo">augur ▸</span> <span class="term-cmd-echo">${_escHtml(cmd)}</span></div>`;

  // Handle local commands
  if (cmd === 'clear') { termClear(); return; }
  if (cmd === 'help') {
    out.innerHTML += _ansiToHtml(_termHelpText());
    _termScroll();
    return;
  }

  // Show spinner
  const spinnerId = 'term-spin-' + Date.now();
  out.innerHTML += `<div id="${spinnerId}" class="term-spinner"><span class="spinner-sm"></span> <span class="term-spinner-text">Running...</span></div>`;
  _termScroll();

  if (input) { input.disabled = true; }

  try {
    const data = await API.post('/api/terminal', { command: cmd });
    const spinEl = document.getElementById(spinnerId);
    if (spinEl) spinEl.remove();
    const output = data.output || data.error || 'No output';
    out.innerHTML += `<div class="term-result">${_ansiToHtml(output)}</div>`;
  } catch (e) {
    const spinEl = document.getElementById(spinnerId);
    if (spinEl) spinEl.remove();
    out.innerHTML += `<div class="term-result"><span style="color:var(--red)">Error: ${_escHtml(e.message)}</span></div>`;
  }
  if (input) { input.disabled = false; input.focus(); }
  _termScroll();
}

function termClear() {
  const out = document.getElementById('term-output');
  if (out) out.innerHTML = '';
  const input = document.getElementById('term-input');
  if (input) input.focus();
}

function _termScroll() {
  const out = document.getElementById('term-output');
  if (out) out.scrollTop = out.scrollHeight;
}

function _escHtml(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _ansiToHtml(text) {
  // Convert ANSI escape codes to HTML spans
  const colorMap = {
    '0':  '</span>',
    '1':  '<span style="font-weight:700">',
    '2':  '<span style="opacity:0.6">',
    '31': '<span style="color:var(--red)">',
    '32': '<span style="color:var(--green)">',
    '33': '<span style="color:var(--amber)">',
    '35': '<span style="color:#c792ea">',
    '36': '<span style="color:var(--cyan)">',
    '97': '<span style="color:var(--text-primary)">',
  };

  let html = _escHtml(text);
  // Handle escaped literal \033 from template strings
  html = html.replace(/\\033/g, '\x1b');
  // Replace ANSI codes. Track how many spans are currently open so a reset (0)
  // closes ALL of them — a combined sequence like 97;1 opens two spans, and a
  // single </span> on reset would otherwise leave one dangling and bleed styling.
  let openSpans = 0;
  html = html.replace(/\x1b\[([0-9;]+)m/g, (match, codes) => {
    const parts = codes.split(';');
    let result = '';
    for (const code of parts) {
      if (code === '0') {
        while (openSpans > 0) { result += '</span>'; openSpans--; }
      } else if (colorMap[code]) {
        result += colorMap[code];
        openSpans++;
      } else {
        // unknown code — skip
      }
    }
    return result;
  });
  // Close any spans left open at end of string.
  while (openSpans > 0) { html += '</span>'; openSpans--; }
  // Convert newlines to <br>
  html = html.replace(/\n/g, '<br>');
  return html;
}

function _termHelpText() {
  var ESC = '\x1b';
  return ESC + '[36m  AVAILABLE COMMANDS:' + ESC + '[0m\n\n' +
    ESC + '[97m  Market Data' + ESC + '[0m\n' +
    '    quote <SYM> [SYM...]       Live stock quotes\n' +
    '    fundamentals <SYM>         Company fundamentals & ratios\n' +
    '    chart <SYM> [-p 1y]        ASCII price chart + indicators\n' +
    '    news <SYM>                 Latest headlines\n' +
    '    search <QUERY>             Symbol lookup\n' +
    '    market indices             Major indices\n' +
    '    market sectors             Sector ETF performance\n' +
    '    market movers              Top gainers/losers\n\n' +
    ESC + '[97m  Portfolio & Tracking' + ESC + '[0m\n' +
    '    portfolio list             Show all holdings with P&L\n' +
    '    portfolio add <SYM> <SHARES> <COST>\n' +
    '    portfolio remove <ID>\n' +
    '    portfolio export\n' +
    '    watchlist list             Show watchlist\n' +
    '    watchlist add <SYM>\n' +
    '    transactions list          Transaction log\n' +
    '    transactions add <SYM> buy/sell <SHARES> <PRICE>\n' +
    '    alerts list                Price alerts\n' +
    '    alerts add <SYM> above/below <PRICE>\n\n' +
    ESC + '[97m  Analytics' + ESC + '[0m\n' +
    '    analytics risk [SYMS...]   Sharpe, Sortino, VaR, drawdown\n' +
    '    analytics correlation      Correlation matrix\n' +
    '    analytics stress [-d 30]   Stress test at custom drop %\n' +
    '    analytics benchmark        Portfolio vs SPY\n\n' +
    ESC + '[97m  Intelligence' + ESC + '[0m\n' +
    '    smart-money score <SYM>    7-factor convergence score\n' +
    '    smart-money scan           Score all portfolio symbols\n' +
    '    ml-forecast <SYM>          ML predictions (RF, trend, regime)\n' +
    '    intel filings <SYM>        Recent SEC filings\n' +
    '    intel insiders <SYM>       Form 4 insider trades\n' +
    '    intel institutional        Tracked fund holdings\n' +
    '    earnings calendar          Upcoming earnings\n' +
    '    earnings dossier <SYM>     Pre-earnings deep dive\n' +
    '    congress                   Congressional trading summary\n' +
    '    congress <SYM>             Congress trades for ticker\n\n' +
    ESC + '[97m  Options' + ESC + '[0m\n' +
    '    options dates <SYM>        Expiration dates\n' +
    '    options chain <SYM> <EXP>  Full option chain\n' +
    '    options flow [SYM]         Unusual activity scanner\n\n' +
    ESC + '[97m  Other' + ESC + '[0m\n' +
    '    crypto market              Top coins by market cap\n' +
    '    crypto quote <COIN_ID>     Coin details (e.g. bitcoin)\n' +
    '    crypto global              Global crypto stats\n' +
    '    dividends                  Portfolio dividend analysis\n' +
    '    macro                      Macro indicators\n' +
    '    settings list              App settings\n' +
    '    settings set <KEY> <VAL>   Update setting\n' +
    '    ai portfolio               AI portfolio analysis\n' +
    '    ai filing <SYM>            AI filing analysis\n' +
    '    clear                      Clear terminal\n' +
    '    help                       Show this help\n';
}


// ══════════════════════════════════════════════════════════════════════════════
// UNUSUAL OPTIONS FLOW
// ══════════════════════════════════════════════════════════════════════════════

async function loadOptionsFlowView() {
  const el = document.getElementById('view-options-flow');
  el.innerHTML = `
    <div class="view-header">
      <div>
        <div class="view-title">⊛ UNUSUAL OPTIONS ACTIVITY</div>
        <div class="view-subtitle">Contracts with volume > 2x open interest — institutional positioning signals</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <input id="options-flow-input" type="text" class="filter-input" placeholder="Enter symbol..." style="width:120px" onkeydown="if(event.key==='Enter')scanOptionsFlow()" />
        <button class="btn btn-green" id="options-scan-btn" onclick="scanOptionsFlow()">SCAN</button>
        <button class="btn btn-ghost" id="options-portfolio-btn" onclick="scanPortfolioOptions()">PORTFOLIO SCAN</button>
      </div>
    </div>
    <div id="options-flow-body" style="padding:0 24px 32px">
      <div class="empty-state">
        <span class="empty-icon" style="font-size:36px">⊛</span>
        <span style="color:var(--green)">UNUSUAL OPTIONS SCANNER</span>
        <span style="color:var(--text-secondary);font-size:12px">Enter a symbol or scan your portfolio for unusual activity</span>
        <div style="margin-top:16px;display:flex;gap:8px;justify-content:center">
          ${['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'].map(s => `<button class="btn btn-ghost btn-sm" onclick="document.getElementById('options-flow-input').value='${s}';scanOptionsFlow()">${s}</button>`).join('')}
        </div>
      </div>
    </div>
  `;
}

async function scanOptionsFlow() {
  const input = document.getElementById('options-flow-input');
  const sym = (input ? input.value.trim().toUpperCase() : '') || 'SPY';
  const body = document.getElementById('options-flow-body');
  const btn = document.getElementById('options-scan-btn');
  if (btn) { btn.disabled = true; btn.textContent = '...'; }

  body.innerHTML = `<div class="scan-loading"></div>`;
  const loader = _loadingStages('options-flow-body', [
    `Fetching ${sym} option chains...`,
    'Scanning expirations for unusual volume...',
    'Computing volume/OI ratios...',
    'Classifying sentiment signals...',
  ], 1500);

  try {
    const data = await API.get(`/api/options-flow/${sym}`);
    loader.stop();
    if (data.error) throw new Error(data.error);
    body.innerHTML = renderOptionsResult(data);
  } catch(e) {
    loader.stop();
    body.innerHTML = `<div class="empty-state"><span class="col-red">${_esc(e.message)}</span></div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'SCAN'; }
  }
}

async function scanPortfolioOptions() {
  const body = document.getElementById('options-flow-body');
  const btn = document.getElementById('options-portfolio-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'SCANNING...'; }

  body.innerHTML = `<div class="scan-loading"></div>`;
  const loader = _loadingStages('options-flow-body', [
    'Fetching portfolio positions...',
    'Scanning option chains across holdings...',
    'Identifying unusual volume patterns...',
    'Classifying bullish/bearish positioning...',
  ], 3000);

  try {
    const data = await API.post('/api/options-flow/scan', {});
    loader.stop();
    if (data.error) throw new Error(data.error);
    const results = data.results || [];
    if (!results.length) {
      body.innerHTML = '<div class="empty-state"><span>No unusual options activity found across portfolio</span></div>';
      return;
    }
    let html = `<div style="margin-bottom:16px;font-size:11px;color:var(--text-dim)">${results.length} symbols with unusual activity / ${data.scanned} scanned</div>`;
    for (const r of results) html += renderOptionsResult(r);
    body.innerHTML = html;
  } catch(e) {
    loader.stop();
    body.innerHTML = `<div class="empty-state"><span class="col-red">${_esc(e.message)}</span></div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'PORTFOLIO SCAN'; }
  }
}

function renderOptionsResult(data) {
  const sym = data.symbol || '';
  const unusual = data.unusual || [];
  if (!unusual.length) {
    return `<div class="section-header">${sym}</div><div style="padding:12px;color:var(--text-dim);font-size:11px">No unusual activity detected (${data.expirations_scanned || 0} expirations scanned)</div>`;
  }

  const sentimentColors = { 'BULLISH': 'col-positive', 'BEARISH': 'col-negative', 'SPECULATIVE CALL': 'col-green', 'SPECULATIVE PUT': 'col-amber' };
  const rows = unusual.slice(0, 25).map(c => {
    const sColor = sentimentColors[c.sentiment] || '';
    const sideColor = c.side === 'CALL' ? 'col-positive' : 'col-negative';
    const volOI = c.vol_oi_ratio ? `${c.vol_oi_ratio}x` : 'NEW';
    return `
      <tr>
        <td><span class="${sideColor}" style="font-weight:700">${_esc(c.side)}</span></td>
        <td>$${c.strike}</td>
        <td>${_esc(c.expiry)} <span style="color:var(--text-dim)">(${c.dte}d)</span></td>
        <td style="font-weight:600">${fmt.compact(c.volume)}</td>
        <td>${fmt.compact(c.open_interest)}</td>
        <td style="color:var(--amber);font-weight:700">${volOI}</td>
        <td>${c.iv_pct}%</td>
        <td>${c.otm_pct > 0 ? '+' : ''}${c.otm_pct}%</td>
        <td>$${fmt.compact(c.notional)}</td>
        <td><span class="signal-badge ${sColor}" style="font-size:9px">${_esc(c.sentiment)}</span></td>
      </tr>
    `;
  }).join('');

  const bulls = unusual.filter(c => (c.sentiment || '').includes('BULL') || (c.side === 'CALL' && !(c.sentiment || '').includes('SPEC'))).length;
  const bears = unusual.filter(c => (c.sentiment || '').includes('BEAR') || (c.side === 'PUT' && !(c.sentiment || '').includes('SPEC'))).length;
  const overall = bulls > bears * 1.5 ? 'BULLISH FLOW' : bears > bulls * 1.5 ? 'BEARISH FLOW' : 'MIXED FLOW';
  const overallColor = overall.includes('BULL') ? 'col-positive' : overall.includes('BEAR') ? 'col-negative' : 'col-yellow';

  return `
    <div style="margin-bottom:24px">
      <div class="section-header" style="display:flex;justify-content:space-between;align-items:center">
        <span>${sym} — ${unusual.length} UNUSUAL CONTRACTS</span>
        <span class="signal-badge ${overallColor}">${overall}</span>
      </div>
      <div style="display:flex;gap:20px;padding:8px 0;font-size:11px;color:var(--text-secondary);border-bottom:1px solid var(--border);margin-bottom:8px">
        <span>Price: <strong style="color:var(--text-primary)">$${fmt.price(data.current_price)}</strong></span>
        <span>Calls: <strong class="col-positive">${bulls}</strong></span>
        <span>Puts: <strong class="col-negative">${bears}</strong></span>
        <span>Expirations: <strong>${data.expirations_scanned}</strong></span>
      </div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr>
            <th>SIDE</th><th>STRIKE</th><th>EXPIRY</th><th>VOLUME</th><th>OI</th>
            <th>VOL/OI</th><th>IV</th><th>OTM%</th><th>NOTIONAL</th><th>SIGNAL</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}


// ══════════════════════════════════════════════════════════════════════════════
// CONGRESSIONAL TRADING INTELLIGENCE
// ══════════════════════════════════════════════════════════════════════════════

let _congressData = null;

async function loadCongressView() {
  const el = document.getElementById('view-congress');
  el.innerHTML = `
    <div class="view-header">
      <div>
        <div class="view-title">⊠ CONGRESSIONAL TRADING INTELLIGENCE</div>
        <div class="view-subtitle">STOCK Act filings parsed in real-time from official House Clerk records</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="congress-days" class="filter-input" style="width:100px">
          <option value="30">30 days</option>
          <option value="60" selected>60 days</option>
          <option value="90">90 days</option>
        </select>
        <button class="btn btn-green" id="congress-refresh-btn" onclick="loadCongressData()">⟳ REFRESH</button>
      </div>
    </div>
    <div id="congress-body" style="padding:0 24px 32px"></div>
  `;
  await loadCongressData();
}

async function loadCongressData() {
  const body = document.getElementById('congress-body');
  const btn = document.getElementById('congress-refresh-btn');
  if (!body) return;
  const days = document.getElementById('congress-days')?.value || 60;
  if (btn) { btn.disabled = true; btn.textContent = 'LOADING...'; }

  body.innerHTML = `<div class="scan-loading"></div>`;
  const loader = _loadingStages('congress-body', [
    'Downloading House Financial Disclosure index...',
    'Identifying recent PTR (Periodic Transaction Report) filings...',
    'Downloading and parsing PTR PDFs from House Clerk...',
    'Extracting stock transactions from filings...',
    'Computing ticker sentiment & aggregations...',
  ], 4000);

  try {
    const data = await API.get(`/api/congress/trades?days=${days}&max_pdfs=60`);
    loader.stop();
    if (data.error) throw new Error(data.error);
    _congressData = data;
    renderCongressView(data);
    // Senate STOCK Act trades (senate-stock-watcher-data, no-key Tier 1)
    DataPanels.appendSenatePanel(body);
  } catch(e) {
    loader.stop();
    body.innerHTML = `<div class="empty-state"><span class="col-red">Error: ${_esc(e.message)}</span></div>`;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '⟳ REFRESH'; }
  }
}

function renderCongressView(data) {
  const body = document.getElementById('congress-body');
  if (!body) return;

  const trades = data.trades || [];
  const tickerSummary = data.ticker_summary || [];
  const filingsParsed = data.filings_parsed || 0;
  const totalTrades = data.total_trades_found || 0;

  if (!trades.length) {
    body.innerHTML = `<div class="empty-state"><span>No stock trades found in ${filingsParsed} PTR filings parsed</span></div>`;
    return;
  }

  const topTickers = tickerSummary.slice(0, 20);
  const heatmapHTML = topTickers.map(t => {
    const color = t.sentiment === 'BULLISH' ? 'var(--green)' : t.sentiment === 'BEARISH' ? 'var(--red)' : 'var(--amber)';
    const opacity = Math.min(1.0, Math.max(0.4, t.total_trades / (topTickers[0]?.total_trades || 1)) + 0.1);
    return `
      <div class="congress-heatmap-item" data-congress-ticker="${_esc(t.ticker)}" style="cursor:pointer;border-color:${color};opacity:${opacity}">
        <div style="font-size:12px;font-weight:700;color:${color}">${_esc(t.ticker)}</div>
        <div style="font-size:9px;color:var(--text-dim)">${t.total_trades} trades</div>
        <div style="font-size:9px;color:${color}">${t.buy_pct}% BUY</div>
      </div>
    `;
  }).join('');

  const txnTypeColor = (t) => {
    t = t || '';  // a null/undefined txn_type would crash .includes
    if (t.includes('Buy') || t === 'P') return 'col-positive';
    if (t.includes('Sell') || t === 'S') return 'col-negative';
    return 'col-amber';
  };

  const tradeRows = trades.slice(0, 100).map(t => `
    <tr>
      <td style="font-size:10px;color:var(--text-dim)">${t.txn_date || '—'}</td>
      <td style="font-weight:700;color:var(--green);cursor:pointer" data-congress-ticker="${_esc(t.ticker)}">${_esc(t.ticker)}</td>
      <td><span class="signal-badge ${txnTypeColor(t.txn_type)}" style="font-size:9px">${_esc(t.txn_type)}</span></td>
      <td style="font-size:10px">${t.amount_str || '—'}</td>
      <td style="font-size:10px;color:var(--text-secondary)">${_esc(t.member_name)}</td>
      <td style="font-size:10px;color:var(--text-dim)">${t.state || ''}</td>
      <td style="font-size:10px;color:var(--text-dim)">${t.owner || ''}</td>
      <td><a href="${_esc(safeUrl(t.pdf_url))}" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue)">PDF ↗</a></td>
    </tr>
  `).join('');

  body.innerHTML = `
    <div style="display:flex;gap:16px;margin-bottom:16px">
      <div class="stat-card" style="flex:1"><div class="stat-label">FILINGS PARSED</div><div class="stat-value">${filingsParsed}</div></div>
      <div class="stat-card" style="flex:1"><div class="stat-label">STOCK TRADES FOUND</div><div class="stat-value">${totalTrades}</div></div>
      <div class="stat-card" style="flex:1"><div class="stat-label">UNIQUE TICKERS</div><div class="stat-value">${tickerSummary.length}</div></div>
      <div class="stat-card" style="flex:1"><div class="stat-label">DATA SOURCE</div><div class="stat-value" style="font-size:10px;color:var(--text-secondary)">House Clerk (Official)</div></div>
    </div>

    <div class="section-header">MOST TRADED BY CONGRESS — CLICK TO FILTER</div>
    <div id="congress-heatmap" style="display:flex;flex-wrap:wrap;gap:8px;padding:8px 0 20px">${heatmapHTML}</div>

    <div class="section-header" style="display:flex;justify-content:space-between;align-items:center">
      <span id="congress-table-title">ALL RECENT TRADES (${trades.length})</span>
      <button class="btn btn-ghost btn-sm" onclick="clearCongressFilter()" id="congress-clear-filter" style="display:none">✕ CLEAR FILTER</button>
    </div>
    <div class="table-wrap">
      <table class="data-table" id="congress-trades-table">
        <thead><tr><th>DATE</th><th>TICKER</th><th>ACTION</th><th>AMOUNT</th><th>MEMBER</th><th>STATE</th><th>OWNER</th><th>DOC</th></tr></thead>
        <tbody id="congress-trades-body">${tradeRows}</tbody>
      </table>
    </div>

    <div style="margin-top:24px">
      <div class="section-header">TICKER SENTIMENT SUMMARY</div>
      <div class="table-wrap">
        <table class="data-table">
          <thead><tr><th>TICKER</th><th>TRADES</th><th>BUYS</th><th>SELLS</th><th>BUY%</th><th>MEMBERS</th><th>LAST TRADE</th><th>SENTIMENT</th></tr></thead>
          <tbody>
            ${tickerSummary.map(t => {
              const sentColor = t.sentiment === 'BULLISH' ? 'col-positive' : t.sentiment === 'BEARISH' ? 'col-negative' : 'col-yellow';
              return `<tr>
                <td style="font-weight:700;color:var(--green);cursor:pointer" data-congress-ticker="${_esc(t.ticker)}">${_esc(t.ticker)}</td>
                <td>${t.total_trades}</td><td class="col-positive">${t.buys}</td><td class="col-negative">${t.sells}</td>
                <td>${t.buy_pct}%</td><td>${t.member_count}</td><td style="color:var(--text-dim)">${_esc(t.latest_date)}</td>
                <td><span class="signal-badge ${sentColor}">${_esc(t.sentiment)}</span></td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>
    </div>

    <div style="margin-top:16px;padding:12px;background:var(--bg-panel);border:1px solid var(--border);font-size:10px;color:var(--text-dim)">
      Data sourced from official House Financial Disclosure PTR PDFs at disclosures-clerk.house.gov.
      Under the STOCK Act, members must report trades within 30-45 days. Parsed in real-time from source PDFs.
    </div>
  `;

  // Delegated listener: ticker values come from third-party House Clerk PDFs (hostile).
  // Using data-attributes + delegation avoids onclick-attribute injection.
  // Attach once — body is a persistent element re-rendered on filter/clear.
  if (!body.dataset.congressWired) {
    body.dataset.congressWired = '1';
    body.addEventListener('click', (e) => {
      const el = e.target.closest('[data-congress-ticker]');
      if (el && body.contains(el)) filterCongressByTicker(el.getAttribute('data-congress-ticker'));
    });
  }
}

function filterCongressByTicker(ticker) {
  if (!_congressData) return;
  const trades = (_congressData.trades || []).filter(t => t.ticker === ticker);
  const tbody = document.getElementById('congress-trades-body');
  const title = document.getElementById('congress-table-title');
  const clearBtn = document.getElementById('congress-clear-filter');
  const txnTypeColor = (t) => {
    t = t || '';  // a null/undefined txn_type would crash .includes
    if (t.includes('Buy') || t === 'P') return 'col-positive';
    if (t.includes('Sell') || t === 'S') return 'col-negative';
    return 'col-amber';
  };
  if (tbody) {
    tbody.innerHTML = trades.map(t => `
      <tr>
        <td style="font-size:10px;color:var(--text-dim)">${t.txn_date || '—'}</td>
        <td style="font-weight:700;color:var(--green)">${_esc(t.ticker)}</td>
        <td><span class="signal-badge ${txnTypeColor(t.txn_type)}" style="font-size:9px">${_esc(t.txn_type)}</span></td>
        <td style="font-size:10px">${t.amount_str || '—'}</td>
        <td style="font-size:10px;color:var(--text-secondary)">${_esc(t.member_name)}</td>
        <td style="font-size:10px;color:var(--text-dim)">${t.state || ''}</td>
        <td style="font-size:10px;color:var(--text-dim)">${t.owner || ''}</td>
        <td><a href="${_esc(safeUrl(t.pdf_url))}" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue)">PDF ↗</a></td>
      </tr>
    `).join('');
  }
  if (title) title.textContent = `${ticker} TRADES (${trades.length})`;
  if (clearBtn) clearBtn.style.display = '';
}

function clearCongressFilter() {
  if (!_congressData) return;
  renderCongressView(_congressData);
}

// ══════════════════════════════════════════════════════════════════════════════
// OPPORTUNITY SCANNER
// ══════════════════════════════════════════════════════════════════════════════

var _scannerProfile = null;
var _scannerResults = null;

async function loadScannerView() {
  var view = document.getElementById('view-scanner');
  // Load saved profile
  try {
    _scannerProfile = await API.get('/api/scanner/profile');
  } catch (e) {
    _scannerProfile = {};
  }
  var p = _scannerProfile;
  var hasProfile = p.risk_tolerance && p.horizon && p.strategy;

  if (hasProfile) {
    renderScannerDashboard(view, p);
  } else {
    renderScannerQuestionnaire(view, p);
  }
}

function renderScannerQuestionnaire(view, p) {
  var riskVal = p.risk_tolerance || 'moderate';
  var horizonVal = p.horizon || 'medium';
  var strategyVal = p.strategy || 'growth';
  var budgetMin = p.budget_min || 0;
  var budgetMax = p.budget_max || 10000;
  var assetTypes = p.asset_types || ['stocks', 'etfs'];

  view.innerHTML = '<div class="panel mb-8">'
    + '<div class="panel-header"><span class="panel-title">OPPORTUNITY SCANNER // PROFILE SETUP</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:16px">Configure your investment profile to find opportunities tailored to your goals. The scanner analyzes smart money flow, ML forecasts, momentum, fundamentals, insider activity, congressional trades, and options flow to surface high-conviction ideas early.</p>'

    // Risk Tolerance
    + '<div class="form-group" style="margin-bottom:14px">'
    + '<label class="form-label">RISK TOLERANCE</label>'
    + '<div class="flex gap-4" style="flex-wrap:wrap">'
    + _scannerChip('risk', 'conservative', 'CONSERVATIVE', riskVal)
    + _scannerChip('risk', 'moderate', 'MODERATE', riskVal)
    + _scannerChip('risk', 'aggressive', 'AGGRESSIVE', riskVal)
    + '</div></div>'

    // Investment Horizon
    + '<div class="form-group" style="margin-bottom:14px">'
    + '<label class="form-label">INVESTMENT HORIZON</label>'
    + '<div class="flex gap-4" style="flex-wrap:wrap">'
    + _scannerChip('horizon', 'short', 'SHORT (< 3 months)', horizonVal)
    + _scannerChip('horizon', 'medium', 'MEDIUM (3-12 months)', horizonVal)
    + _scannerChip('horizon', 'long', 'LONG (1+ years)', horizonVal)
    + '</div></div>'

    // Strategy
    + '<div class="form-group" style="margin-bottom:14px">'
    + '<label class="form-label">STRATEGY</label>'
    + '<div class="flex gap-4" style="flex-wrap:wrap">'
    + _scannerChip('strategy', 'growth', 'GROWTH', strategyVal)
    + _scannerChip('strategy', 'value', 'VALUE', strategyVal)
    + _scannerChip('strategy', 'income', 'INCOME / DIVIDEND', strategyVal)
    + _scannerChip('strategy', 'momentum', 'MOMENTUM / SWING', strategyVal)
    + '</div></div>'

    // Asset Types
    + '<div class="form-group" style="margin-bottom:14px">'
    + '<label class="form-label">ASSET TYPES TO SCAN</label>'
    + '<div class="flex gap-4" style="flex-wrap:wrap">'
    + _scannerToggle('asset', 'stocks', 'STOCKS', assetTypes)
    + _scannerToggle('asset', 'etfs', 'ETFs', assetTypes)
    + _scannerToggle('asset', 'crypto', 'CRYPTO', assetTypes)
    + _scannerToggle('asset', 'options', 'OPTIONS', assetTypes)
    + '</div></div>'

    // Budget Range
    + '<div class="form-row" style="grid-template-columns:1fr 1fr;gap:8px;margin-bottom:14px">'
    + '<div class="form-group">'
    + '<label class="form-label">MIN BUDGET PER POSITION</label>'
    + '<input class="form-input" id="scan-budget-min" type="number" min="0" step="100" value="' + budgetMin + '">'
    + '</div>'
    + '<div class="form-group">'
    + '<label class="form-label">MAX BUDGET PER POSITION</label>'
    + '<input class="form-input" id="scan-budget-max" type="number" min="0" step="100" value="' + budgetMax + '">'
    + '</div></div>'

    // Sectors
    + '<div class="form-group" style="margin-bottom:16px">'
    + '<label class="form-label">SECTOR FOCUS (optional — leave empty for all)</label>'
    + '<input class="form-input" id="scan-sectors" placeholder="Technology, Healthcare, Energy..." value="' + (p.sectors || []).join(', ') + '">'
    + '</div>'

    + '<button class="btn btn-green btn-lg" onclick="saveScannerProfile()" style="width:100%">SAVE PROFILE & START SCANNING</button>'
    + '</div></div>';
}

function _scannerChip(group, value, label, current) {
  var active = (value === current) ? ' scanner-chip-active' : '';
  return '<button class="filter-chip' + active + '" data-scan-group="' + group + '" data-scan-value="' + value + '" onclick="selectScannerChip(this, \'' + group + '\')">' + label + '</button>';
}

function _scannerToggle(group, value, label, currentArr) {
  var active = (currentArr.indexOf(value) !== -1) ? ' scanner-chip-active' : '';
  return '<button class="filter-chip' + active + '" data-scan-group="' + group + '" data-scan-value="' + value + '" onclick="toggleScannerChip(this)">' + label + '</button>';
}

function selectScannerChip(el, group) {
  document.querySelectorAll('[data-scan-group="' + group + '"]').forEach(function(c) {
    c.classList.remove('scanner-chip-active');
  });
  el.classList.add('scanner-chip-active');
}

function toggleScannerChip(el) {
  el.classList.toggle('scanner-chip-active');
}

async function saveScannerProfile() {
  var risk = document.querySelector('[data-scan-group="risk"].scanner-chip-active');
  var horizon = document.querySelector('[data-scan-group="horizon"].scanner-chip-active');
  var strategy = document.querySelector('[data-scan-group="strategy"].scanner-chip-active');
  var assetEls = document.querySelectorAll('[data-scan-group="asset"].scanner-chip-active');
  var assets = [];
  assetEls.forEach(function(el) { assets.push(el.dataset.scanValue); });

  if (!risk || !horizon || !strategy) {
    Toast.warn('Please select risk tolerance, horizon, and strategy');
    return;
  }
  if (assets.length === 0) {
    Toast.warn('Please select at least one asset type');
    return;
  }

  var sectorsRaw = (document.getElementById('scan-sectors').value || '').trim();
  var sectors = sectorsRaw ? sectorsRaw.split(',').map(function(s) { return s.trim(); }).filter(Boolean) : [];

  var profile = {
    risk_tolerance: risk.dataset.scanValue,
    horizon: horizon.dataset.scanValue,
    strategy: strategy.dataset.scanValue,
    asset_types: assets,
    budget_min: parseFloat(document.getElementById('scan-budget-min').value) || 0,
    budget_max: parseFloat(document.getElementById('scan-budget-max').value) || 10000,
    sectors: sectors
  };

  try {
    await API.post('/api/scanner/profile', profile);
    _scannerProfile = profile;
    Toast.show('Profile saved — starting scan...');
    var view = document.getElementById('view-scanner');
    renderScannerDashboard(view, profile);
    runScan(false);
  } catch (e) {
    Toast.error('Failed to save profile');
  }
}

function renderScannerDashboard(view, profile) {
  var stratLabel = (profile.strategy || 'growth').toUpperCase();
  var riskLabel = (profile.risk_tolerance || 'moderate').toUpperCase();
  var horizonLabel = (profile.horizon || 'medium').toUpperCase();

  view.innerHTML = '<div class="panel mb-8">'
    + '<div class="panel-header">'
    + '<span class="panel-title">OPPORTUNITY SCANNER // ' + stratLabel + ' STRATEGY</span>'
    + '<div class="flex gap-4">'
    + '<button class="btn btn-ghost btn-sm" onclick="editScannerProfile()">EDIT PROFILE</button>'
    + '<button class="btn btn-green btn-sm" onclick="runScan(true)">RESCAN NOW</button>'
    + '</div>'
    + '</div>'
    + '<div class="panel-body">'
    + '<div class="kpi-grid" style="grid-template-columns:repeat(4,1fr)">'
    + '<div class="kpi-card"><div class="kpi-label">STRATEGY</div><div class="kpi-value" style="color:var(--green)">' + stratLabel + '</div></div>'
    + '<div class="kpi-card"><div class="kpi-label">RISK</div><div class="kpi-value" style="color:var(--amber)">' + riskLabel + '</div></div>'
    + '<div class="kpi-card"><div class="kpi-label">HORIZON</div><div class="kpi-value">' + horizonLabel + '</div></div>'
    + '<div class="kpi-card"><div class="kpi-label">ASSETS</div><div class="kpi-value">' + (profile.asset_types || []).join(', ').toUpperCase() + '</div></div>'
    + '</div></div></div>'

    + '<div id="scan-results-area">'
    + '<div class="loading"><div class="spinner"></div> CHECKING FOR CACHED RESULTS...</div>'
    + '</div>';

  // Try to load cached results first
  API.get('/api/scanner/results').then(function(data) {
    if (data.opportunities && data.opportunities.length > 0) {
      _scannerResults = data;
      renderScanResults(data);
    } else {
      runScan(false);
    }
  }).catch(function() {
    runScan(false);
  });
}

function editScannerProfile() {
  var view = document.getElementById('view-scanner');
  renderScannerQuestionnaire(view, _scannerProfile || {});
}

async function runScan(force) {
  var area = document.getElementById('scan-results-area');
  if (!area) return;
  area.innerHTML = '<div class="loading"><div class="spinner"></div> SCANNING UNIVERSE — ANALYZING SMART MONEY, ML FORECASTS, MOMENTUM, INSIDER ACTIVITY...</div>'
    + '<p style="text-align:center;color:var(--text-dim);font-size:10px;margin-top:8px">This may take 30-60 seconds on first run. Results are cached for 30 minutes.</p>';

  try {
    var data = await API.post('/api/scanner/scan', { force: !!force });
    _scannerResults = data;
    renderScanResults(data);
    Toast.show('Scan complete — ' + (data.opportunities || []).length + ' opportunities found');
  } catch (e) {
    area.innerHTML = '<div class="empty-state"><span class="empty-icon" style="color:var(--red)">!</span>'
      + '<span style="color:var(--red)">Scan failed: ' + (e.message || 'Unknown error') + '</span>'
      + '<button class="btn btn-ghost btn-sm" onclick="runScan(true)" style="margin-top:8px">RETRY</button></div>';
  }
}

function renderScanResults(data) {
  var area = document.getElementById('scan-results-area');
  if (!area) return;
  var opps = data.opportunities || [];
  var meta = data.meta || {};

  if (opps.length === 0) {
    area.innerHTML = '<div class="empty-state"><span class="empty-icon">⊙</span>'
      + '<span style="color:var(--text-secondary)">No opportunities found matching your criteria.</span>'
      + '<button class="btn btn-ghost btn-sm" onclick="editScannerProfile()" style="margin-top:8px">ADJUST PROFILE</button></div>';
    return;
  }

  var topPicks = opps.filter(function(o) { return o.tags && o.tags.indexOf('TOP PICK') !== -1; });
  var earlySigs = opps.filter(function(o) { return o.tags && o.tags.indexOf('EARLY SIGNAL') !== -1; });
  var html = '';

  // Meta info
  if (meta.scanned_at) {
    html += '<div style="text-align:right;font-size:10px;color:var(--text-dim);margin-bottom:8px">SCANNED: ' + meta.scanned_at + ' | UNIVERSE: ' + (meta.universe_size || '?') + ' SYMBOLS | STRATEGY: ' + (meta.strategy || '?').toUpperCase() + '</div>';
  }

  // Top Picks highlight
  if (topPicks.length > 0) {
    html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title" style="color:var(--green)">TOP PICKS</span></div>'
      + '<div class="panel-body"><div class="kpi-grid" style="grid-template-columns:repeat(' + Math.min(topPicks.length, 3) + ',1fr)">';
    topPicks.forEach(function(o) {
      html += _scannerTopCard(o);
    });
    html += '</div></div></div>';
  }

  // Early Signals
  if (earlySigs.length > 0) {
    html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title" style="color:var(--amber)">EARLY SIGNALS</span></div>'
      + '<div class="panel-body"><div class="kpi-grid" style="grid-template-columns:repeat(' + Math.min(earlySigs.length, 4) + ',1fr)">';
    earlySigs.forEach(function(o) {
      html += _scannerSignalCard(o);
    });
    html += '</div></div></div>';
  }

  // Full results table
  html += '<div class="panel"><div class="panel-header"><span class="panel-title">ALL OPPORTUNITIES (' + opps.length + ')</span></div>'
    + '<div style="overflow-x:auto"><table class="data-table"><thead><tr>'
    + '<th>RANK</th><th>SYMBOL</th><th>TYPE</th><th style="text-align:right">PRICE</th>'
    + '<th style="text-align:right">COMPOSITE</th><th style="text-align:right">SMART $</th>'
    + '<th style="text-align:right">ML FCST</th><th style="text-align:right">MOMENTUM</th>'
    + '<th style="text-align:right">FUNDMTL</th><th>TAGS</th><th>CATALYSTS</th><th></th>'
    + '</tr></thead><tbody>';

  opps.forEach(function(o, i) {
    var compColor = o.composite >= 70 ? 'var(--green)' : (o.composite >= 50 ? 'var(--amber)' : 'var(--text-dim)');
    var tagBadges = (o.tags || []).map(function(t) {
      var cls = t === 'TOP PICK' ? 'col-positive' : (t === 'EARLY SIGNAL' ? 'col-amber' : '');
      return '<span class="signal-badge ' + cls + '" style="font-size:8px">' + _esc(t) + '</span>';
    }).join(' ');
    var catalysts = (o.catalysts || []).slice(0, 2).join(', ');
    var typeLabel = (o.asset_type || 'stock').toUpperCase();

    html += '<tr>'
      + '<td style="color:var(--text-dim)">' + (i + 1) + '</td>'
      + '<td style="font-weight:700;color:var(--green);cursor:pointer" onclick="openResearch(\'' + _jesc(o.symbol) + '\')">' + _esc(o.symbol) + '</td>'
      + '<td style="font-size:10px">' + _esc(typeLabel) + '</td>'
      + '<td style="text-align:right">' + _fmtScanPrice(o.price) + '</td>'
      + '<td style="text-align:right;font-weight:700;color:' + compColor + '">' + (o.composite != null ? o.composite.toFixed(1) : '—') + '</td>'
      + '<td style="text-align:right">' + _fmtScanScore(o.scores, 'smart_money') + '</td>'
      + '<td style="text-align:right">' + _fmtScanScore(o.scores, 'ml_forecast') + '</td>'
      + '<td style="text-align:right">' + _fmtScanScore(o.scores, 'momentum') + '</td>'
      + '<td style="text-align:right">' + _fmtScanScore(o.scores, 'fundamentals') + '</td>'
      + '<td>' + tagBadges + '</td>'
      + '<td style="font-size:10px;color:var(--text-secondary);max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + _esc((o.catalysts || []).join(', ')) + '">' + _esc(catalysts) + '</td>'
      + '<td><button class="btn btn-ghost btn-sm" onclick="openResearch(\'' + _jesc(o.symbol) + '\')">RESEARCH</button></td>'
      + '</tr>';
  });

  html += '</tbody></table></div></div>';
  area.innerHTML = html;
}

function _scannerTopCard(o) {
  var compColor = o.composite >= 70 ? 'var(--green)' : 'var(--amber)';
  var catalysts = (o.catalysts || []).slice(0, 3).map(function(c) {
    return '<div style="font-size:9px;color:var(--text-secondary)">+ ' + _esc(c) + '</div>';
  }).join('');
  return '<div class="kpi-card" style="cursor:pointer;border:1px solid var(--green-dim)" onclick="openResearch(\'' + _jesc(o.symbol) + '\')">'
    + '<div class="kpi-label" style="color:var(--green)">' + _esc(o.symbol) + ' <span style="font-size:9px;color:var(--text-dim)">' + _esc((o.asset_type || 'stock').toUpperCase()) + '</span></div>'
    + '<div class="kpi-value" style="color:' + compColor + ';font-size:28px">' + (o.composite != null ? o.composite.toFixed(1) : '—') + '<span style="font-size:12px;color:var(--text-dim)">/100</span></div>'
    + '<div style="font-size:10px;color:var(--text-secondary);margin-top:4px">' + _fmtScanPrice(o.price) + '</div>'
    + catalysts
    + '</div>';
}

function _scannerSignalCard(o) {
  return '<div class="kpi-card" style="cursor:pointer;border:1px solid var(--amber-dim, var(--amber))" onclick="openResearch(\'' + _jesc(o.symbol) + '\')">'
    + '<div class="kpi-label" style="color:var(--amber)">' + _esc(o.symbol) + '</div>'
    + '<div class="kpi-value" style="font-size:22px">' + (o.composite != null ? o.composite.toFixed(1) : '—') + '</div>'
    + '<div style="font-size:9px;color:var(--text-dim);margin-top:2px">' + _esc((o.catalysts || []).slice(0, 2).join(' | ')) + '</div>'
    + '</div>';
}

function _fmtScanPrice(price) {
  if (price == null) return '—';
  return '$' + Number(price).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}

function _fmtScanScore(scores, key) {
  if (!scores || scores[key] == null) return '<span style="color:var(--text-dim)">—</span>';
  var v = scores[key];
  var color = v >= 70 ? 'var(--green)' : (v >= 50 ? 'var(--amber)' : 'var(--text-dim)');
  return '<span style="color:' + color + '">' + v.toFixed(0) + '</span>';
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — SHARED HELPERS
// ══════════════════════════════════════════════════════════════════════════════

function _alphaScoreColor(score) {
  if (score >= 70) return 'var(--green)';
  if (score >= 50) return 'var(--amber)';
  return 'var(--text-dim)';
}

function _alphaRegimeBadge(regime) {
  var cls = '';
  if (regime === 'NORMAL' || regime === 'LONG GAMMA') cls = 'col-positive';
  else if (regime === 'ELEVATED' || regime === 'NEUTRAL') cls = 'col-amber';
  else if (regime === 'WARNING' || regime === 'SHORT GAMMA') cls = 'col-negative';
  else if (regime === 'CRITICAL') cls = 'col-negative';
  return '<span class="signal-badge ' + cls + '" style="font-size:9px">' + (regime || '—') + '</span>';
}

function _alphaQuickPicks(inputId, fn, symbols) {
  var html = '';
  for (var i = 0; i < symbols.length; i++) {
    html += '<button class="btn btn-ghost btn-sm" onclick="document.getElementById(\'' + inputId + '\').value=\'' + symbols[i] + '\';' + fn + '()">' + symbols[i] + '</button>';
  }
  return html;
}

function _alphaFmtNum(val, decimals) {
  if (val == null || isNaN(val)) return '—';
  var d = decimals != null ? decimals : 2;
  return Number(val).toLocaleString('en-US', {minimumFractionDigits: d, maximumFractionDigits: d});
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — GEX (GAMMA EXPOSURE FLOW)
// ══════════════════════════════════════════════════════════════════════════════

async function loadGexView() {
  var view = document.getElementById('view-gex');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">GAMMA EXPOSURE FLOW</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Delta-hedging flow analysis from options market maker positions</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'
    + '<input id="gex-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')analyzeGex()" />'
    + '<button class="btn btn-green" onclick="analyzeGex()">ANALYZE</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('gex-symbol', 'analyzeGex', ['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'])
    + '</div>'
    + '<div id="gex-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to analyze gamma exposure</span></div>'
    + '</div>'
    + '</div></div>';
}

async function analyzeGex(sym) {
  var input = document.getElementById('gex-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var results = document.getElementById('gex-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Analyzing gamma exposure for ' + symbol + '...</div>';

  try {
    var data = await API.get('/api/gex/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);
    var g = data;

    var netGex = g.net_gex != null ? _alphaFmtNum(g.net_gex, 0) : '—';
    var regime = g.gamma_regime || 'UNKNOWN';
    var flipPrice = g.gamma_flip_price != null ? '$' + _alphaFmtNum(g.gamma_flip_price, 2) : '—';
    var pcRatio = g.put_call_oi_ratio != null ? _alphaFmtNum(g.put_call_oi_ratio, 2) : '—';

    var html = '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Net GEX</div><div style="font-size:18px;font-weight:700">' + netGex + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Gamma Regime</div><div>' + _alphaRegimeBadge(regime) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Gamma Flip Price</div><div style="font-size:18px;font-weight:700">' + flipPrice + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Put/Call OI Ratio</div><div style="font-size:18px;font-weight:700">' + pcRatio + '</div></div>'
      + '</div>';

    // Top Gamma Walls table
    var walls = g.gamma_walls || g.top_gamma_walls || [];
    if (walls.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">TOP GAMMA WALLS</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr><th>Strike</th><th>Net GEX</th><th>Type</th></tr></thead><tbody>';
      for (var i = 0; i < walls.length; i++) {
        var w = walls[i];
        var typeCls = (w.type || '').toLowerCase() === 'support' ? 'col-positive' : 'col-negative';
        html += '<tr><td>$' + _alphaFmtNum(w.strike, 2) + '</td>'
          + '<td>' + _alphaFmtNum(w.net_gex || w.gex, 0) + '</td>'
          + '<td><span class="signal-badge ' + typeCls + '" style="font-size:9px">' + _esc(w.type || '—') + '</span></td></tr>';
      }
      html += '</tbody></table></div></div>';
    }

    // Dealer Hedge Estimates table
    var hedges = Array.isArray(g.dealer_hedge_estimates) ? g.dealer_hedge_estimates
      : Object.entries(g.dealer_hedge_estimates || g.dealer_hedges || g.hedge_estimates || {}).map(function (kv) {
          return Object.assign({move: kv[0]}, kv[1]);
        });
    if (hedges.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">DEALER HEDGE ESTIMATES</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr><th>Move</th><th>Price</th><th>Delta Shares</th></tr></thead><tbody>';
      for (var j = 0; j < hedges.length; j++) {
        var h = hedges[j];
        html += '<tr><td>' + (h.move || '—') + '</td>'
          + '<td>$' + _alphaFmtNum(h.price, 2) + '</td>'
          + '<td>' + _alphaFmtNum(h.delta_shares || h.shares, 0) + '</td></tr>';
      }
      html += '</tbody></table></div></div>';
    }

    html += '<div style="margin-top:12px;padding:12px;background:var(--bg-secondary);border-radius:4px;font-size:10px;color:var(--text-dim)">'
      + '<strong style="color:var(--green)">What is GEX?</strong> Gamma Exposure (GEX) measures the aggregate gamma of market maker hedging positions. '
      + 'In <strong>long gamma</strong> regimes, dealer hedging dampens volatility (sell highs, buy lows). '
      + 'In <strong>short gamma</strong> regimes, hedging amplifies moves. The gamma flip price marks the transition point.'
      + '</div>';

    results.innerHTML = '<span class="gex-track-slot" style="float:right"></span>' + html;
    // v0.2.0 — GEX tracker badge.
    if (window.renderTrackBadge) {
      const slot = results.querySelector('.gex-track-slot');
      if (slot) { try { window.renderTrackBadge(slot, 'gex'); } catch(_) {} }
    }
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — CORPORATE CONTAGION GRAPH
// ══════════════════════════════════════════════════════════════════════════════

async function loadContagionView() {
  var view = document.getElementById('view-contagion');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">CORPORATE CONTAGION GRAPH</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Supply chain &amp; revenue dependency mapping</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'
    + '<input id="contagion-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')mapContagion()" />'
    + '<button class="btn btn-green" onclick="mapContagion()">MAP GRAPH</button>'
    + '<button class="btn btn-ghost" onclick="assessContagionImpact()">ASSESS IMPACT</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('contagion-symbol', 'mapContagion', ['AAPL','TSMC','NVDA','MSFT','AMZN','JPM'])
    + '</div>'
    + '<div id="contagion-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to map corporate dependencies</span></div>'
    + '</div>'
    + '</div></div>';
}

async function mapContagion(sym) {
  var input = document.getElementById('contagion-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var results = document.getElementById('contagion-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Mapping contagion graph for ' + symbol + '...</div>';

  try {
    var data = await API.get('/api/contagion/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);

    var connections = data.connections || data.connected_companies || [];
    if (!connections.length && Array.isArray(data.nodes)) {
      // Build connection rows from nodes joined with edges (by target ticker)
      var edgeByTarget = {};
      (data.edges || []).forEach(function (e) { edgeByTarget[e.target] = e; });
      connections = data.nodes.map(function (n) {
        var e = edgeByTarget[n.ticker] || {};
        return {
          ticker: n.ticker,
          name: n.name,
          relationship: n.relationship || e.type,
          weight: e.weight,
          revenue_pct: e.revenue_pct,
          context: e.context,
        };
      });
    }
    var html = '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Total Connections</div><div style="font-size:18px;font-weight:700">' + connections.length + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Filing Date</div><div style="font-size:14px">' + _esc(data.filing_date || '—') + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Company</div><div style="font-size:14px">' + _esc(data.company_name || data.company || symbol) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Sector</div><div style="font-size:14px">' + _esc(data.sector || '—') + '</div></div>'
      + '</div>';

    if (connections.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">CONNECTED COMPANIES</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Ticker</th><th>Name</th><th>Relationship</th><th>Weight</th><th>Revenue %</th><th>Context</th>'
        + '</tr></thead><tbody>';
      for (var i = 0; i < connections.length; i++) {
        var c = connections[i];
        var relCls = (c.relationship || '').toLowerCase().indexOf('supplier') >= 0 ? 'col-positive' : 'col-amber';
        html += '<tr>'
          + '<td><span class="col-symbol" style="cursor:pointer" onclick="openResearch(\'' + _jesc(c.ticker || '') + '\')">' + _esc(c.ticker || '—') + '</span></td>'
          + '<td>' + _esc(c.name || '—') + '</td>'
          + '<td><span class="signal-badge ' + relCls + '" style="font-size:9px">' + _esc(c.relationship || '—') + '</span></td>'
          + '<td>' + _alphaFmtNum(c.weight, 2) + '</td>'
          + '<td>' + (c.revenue_pct != null ? _alphaFmtNum(c.revenue_pct, 1) + '%' : '—') + '</td>'
          + '<td style="font-size:10px;color:var(--text-dim);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(c.context || '—') + '</td>'
          + '</tr>';
      }
      html += '</tbody></table></div></div>';
    }

    // Store for impact assessment
    window._contagionData = data;
    window._contagionSymbol = symbol;
    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

async function assessContagionImpact() {
  var symbol = window._contagionSymbol;
  if (!symbol) {
    var input = document.getElementById('contagion-symbol');
    symbol = input ? input.value.trim().toUpperCase() : '';
  }
  if (!symbol) return;
  var results = document.getElementById('contagion-results');
  if (!results) return;

  var existing = results.innerHTML;
  results.innerHTML = existing + '<div id="contagion-impact-loading" class="loading" style="margin-top:12px"><div class="spinner"></div> Assessing impact propagation...</div>';

  try {
    var data = await API.get('/api/contagion/impact/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);
    var loadEl = document.getElementById('contagion-impact-loading');
    if (loadEl) loadEl.remove();

    var impacts = data.impacted_companies || data.impacts || data.impact || [];
    if (impacts.length) {
      var impactHtml = '<div class="panel mb-8" style="margin-top:12px"><div class="panel-header"><span class="panel-title">IMPACT ASSESSMENT</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Ticker</th><th>Score</th><th>Correlation</th><th>Lag Days</th><th>Risk Level</th>'
        + '</tr></thead><tbody>';
      for (var i = 0; i < impacts.length; i++) {
        var imp = impacts[i];
        var riskCls = '';
        var riskVal = imp.contagion_risk || imp.risk_level || '';
        var riskLvl = riskVal.toUpperCase();
        if (riskLvl === 'HIGH' || riskLvl === 'CRITICAL') riskCls = 'col-negative';
        else if (riskLvl === 'MEDIUM' || riskLvl === 'MODERATE') riskCls = 'col-amber';
        else riskCls = 'col-positive';
        var impScore = (imp.impact_score != null) ? imp.impact_score : imp.score;
        impactHtml += '<tr>'
          + '<td><span class="col-symbol" style="cursor:pointer" onclick="openResearch(\'' + _jesc(imp.ticker || '') + '\')">' + _esc(imp.ticker || '—') + '</span></td>'
          + '<td style="color:' + _alphaScoreColor(impScore || 0) + '">' + _alphaFmtNum(impScore, 1) + '</td>'
          + '<td>' + _alphaFmtNum(imp.correlation, 2) + '</td>'
          + '<td>' + (imp.lag_days != null ? imp.lag_days : '—') + '</td>'
          + '<td><span class="signal-badge ' + riskCls + '" style="font-size:9px">' + (riskVal || '—') + '</span></td>'
          + '</tr>';
      }
      impactHtml += '</tbody></table></div></div>';
      results.innerHTML = results.innerHTML + impactHtml;
    }
  } catch (e) {
    var loadEl2 = document.getElementById('contagion-impact-loading');
    if (loadEl2) loadEl2.innerHTML = '<span class="col-negative">Impact error: ' + _esc(e.message) + '</span>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// FORECAST ENSEMBLE — calibrated meta-forecast fusing every signal
// ══════════════════════════════════════════════════════════════════════════════

async function loadForecastView() {
  var view = document.getElementById('view-forecast');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">FORECAST ENSEMBLE</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Fuses every forecasting engine (RandomForest, trend, mean-reversion, bootstrap distribution, narrative phase) into one <b>calibrated</b> directional probability. Conviction is shrunk toward neutral when the signals disagree.</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">'
    + '<input id="forecast-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')analyzeForecast()" />'
    + '<select id="forecast-horizon" class="form-input" style="width:130px">'
    + '<option value="5">5d horizon</option>'
    + '<option value="10">10d horizon</option>'
    + '<option value="20" selected>20d horizon</option>'
    + '<option value="40">40d horizon</option>'
    + '<option value="60">60d horizon</option>'
    + '</select>'
    + '<button class="btn btn-green" onclick="analyzeForecast()">FORECAST</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('forecast-symbol', 'analyzeForecast', ['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'])
    + '</div>'
    + '<div id="forecast-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to generate a unified forecast</span></div>'
    + '</div>'
    + '</div></div>'
    + '<div id="forecast-accountability"></div>';
  loadForecastAccountability();
}

async function loadForecastAccountability() {
  var el = document.getElementById('forecast-accountability');
  if (!el) return;
  try {
    var data = await API.get('/api/forecast/accountability');
    if (data.error) throw new Error(data.error);
    var tr = (data.ensemble && data.ensemble.track_record) || {};
    var cal = (data.ensemble && data.ensemble.calibration) || {};
    var board = data.leaderboard || [];
    var weights = data.weights || [];

    var html = '<div class="panel mt-8"><div class="panel-header">'
      + '<span class="panel-title">FORECAST ACCOUNTABILITY — LIVE CALIBRATION</span>'
      + (data.weights_adapted ? '<span class="signal-badge col-positive" style="font-size:9px;margin-left:8px">ADAPTIVE WEIGHTS ON</span>' : '<span class="signal-badge" style="font-size:9px;margin-left:8px;color:var(--text-dim)">STATIC WEIGHTS</span>')
      + '</div><div class="panel-body">';
    html += '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Every forecast is logged and re-priced once its horizon elapses. Brier score grades probability calibration (lower is better); component skill feeds back into the fusion weights.</p>';

    if (!tr.n) {
      html += '<div class="empty-state"><span style="color:var(--text-dim)">No scored forecasts yet — calibration appears once forecasts mature past their horizon (scored every 6h).</span></div>';
      html += '</div></div>';
      el.innerHTML = html;
      // Still show the (cold-start) leaderboard/weights below.
    } else {
      var hitPct = tr.hit_rate != null ? Math.round(tr.hit_rate * 100) + '%' : '—';
      var verdict = cal.verdict || '—';
      var vColor = verdict === 'WELL CALIBRATED' ? 'var(--green)' : verdict === 'SLIGHT EDGE' ? 'var(--amber,#f0a020)' : verdict === 'MISCALIBRATED' ? 'var(--red,#ff4444)' : 'var(--text-dim)';
      html += '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:16px">'
        + '<div class="kpi-card"><div class="form-label">Hit Rate</div><div style="font-size:20px;font-weight:700;color:var(--green)">' + hitPct + '</div><div style="font-size:9px;color:var(--text-dim)">n=' + _esc(tr.n_directional || 0) + ' directional</div></div>'
        + '<div class="kpi-card"><div class="form-label">Brier Score</div><div style="font-size:20px;font-weight:700">' + (cal.brier != null ? _esc(cal.brier) : '—') + '</div><div style="font-size:9px;color:var(--text-dim)">0=perfect</div></div>'
        + '<div class="kpi-card"><div class="form-label">Brier Skill</div><div style="font-size:20px;font-weight:700;color:' + (cal.brier_skill > 0 ? 'var(--green)' : 'var(--red,#ff4444)') + '">' + (cal.brier_skill != null ? _esc(cal.brier_skill) : '—') + '</div><div style="font-size:9px;color:var(--text-dim)">vs base rate</div></div>'
        + '<div class="kpi-card"><div class="form-label">Calibration</div><div style="font-size:13px;font-weight:700;color:' + vColor + ';margin-top:4px">' + _esc(verdict) + '</div></div>'
        + '<div class="kpi-card"><div class="form-label">Total Logged</div><div style="font-size:20px;font-weight:700">' + _esc(tr.n) + '</div><div style="font-size:9px;color:var(--text-dim)">avg ' + (tr.avg_return != null ? (tr.avg_return >= 0 ? '+' : '') + (tr.avg_return * 100).toFixed(1) + '%' : '—') + '</div></div>'
        + '</div>';

      // Reliability curve — predicted prob vs realized frequency per bucket.
      var rel = cal.reliability || [];
      if (rel.length) {
        html += '<div style="font-size:11px;color:var(--green);font-weight:600;margin:4px 0 8px">RELIABILITY CURVE <span style="color:var(--text-dim);font-weight:400">(predicted P(up) vs realized frequency — closer to diagonal = better)</span></div>';
        html += '<table class="data-table" style="margin-bottom:16px"><thead><tr><th>Predicted bucket</th><th>Mean predicted</th><th style="width:200px">Realized freq</th><th>n</th></tr></thead><tbody>';
        for (var i = 0; i < rel.length; i++) {
          var b = rel[i];
          var pm = Math.round(b.predicted_mean * 100), rf = Math.round(b.realized_freq * 100);
          var diff = Math.abs(pm - rf);
          var barColor = diff <= 10 ? 'var(--green)' : diff <= 20 ? 'var(--amber,#f0a020)' : 'var(--red,#ff4444)';
          html += '<tr>'
            + '<td style="font-size:11px">' + Math.round(b.bucket_low*100) + '–' + Math.round(b.bucket_high*100) + '%</td>'
            + '<td style="font-size:11px;color:var(--text-secondary)">' + pm + '%</td>'
            + '<td><div style="display:flex;align-items:center;gap:6px"><div style="flex:1;height:8px;background:var(--bg-secondary);border-radius:4px;overflow:hidden;position:relative">'
            + '<div style="position:absolute;left:' + pm + '%;top:0;bottom:0;width:1px;background:var(--text-dim);z-index:2"></div>'
            + '<div style="height:100%;width:' + rf + '%;background:' + barColor + '"></div></div><span style="font-size:10px;color:' + barColor + '">' + rf + '%</span></div></td>'
            + '<td style="font-size:11px;color:var(--text-dim)">' + _esc(b.count) + '</td>'
            + '</tr>';
        }
        html += '</tbody></table>';
      }
      html += '</div></div>';
      el.innerHTML = html;
    }

    // ── Component leaderboard (appended to whatever we built above) ────
    if (board.length) {
      var wmap = {};
      for (var w = 0; w < weights.length; w++) wmap[weights[w].key] = weights[w];
      var lb = '<div class="panel mt-8"><div class="panel-header"><span class="panel-title">COMPONENT LEADERBOARD — WHICH ENGINES EARN THEIR WEIGHT</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>#</th><th>Engine</th><th>Hit Rate</th><th>Brier</th><th>Skill</th><th>n</th><th>Base → Effective Weight</th>'
        + '</tr></thead><tbody>';
      for (var j = 0; j < board.length; j++) {
        var r = board[j];
        var hr = r.hit_rate != null ? Math.round(r.hit_rate * 100) + '%' : '—';
        var sk = r.brier_skill;
        var skColor = sk == null ? 'var(--text-dim)' : sk > 0 ? 'var(--green)' : 'var(--red,#ff4444)';
        var winfo = wmap[r.key];
        var wHtml = '—';
        if (winfo) {
          var up = winfo.effective_weight > winfo.base_weight + 1e-6;
          var down = winfo.effective_weight < winfo.base_weight - 1e-6;
          var wColor = up ? 'var(--green)' : down ? 'var(--red,#ff4444)' : 'var(--text-secondary)';
          wHtml = '<span style="color:var(--text-dim)">' + Math.round(winfo.base_weight*100) + '%</span> → <span style="color:' + wColor + ';font-weight:600">' + Math.round(winfo.effective_weight*100) + '%</span>' + (up ? ' ▲' : down ? ' ▼' : '');
        }
        lb += '<tr>'
          + '<td style="color:var(--text-dim)">' + (j+1) + '</td>'
          + '<td style="font-size:11px;font-weight:600">' + _esc(r.key) + '</td>'
          + '<td style="font-size:11px">' + hr + '</td>'
          + '<td style="font-size:11px;color:var(--text-secondary)">' + (r.brier != null ? _esc(r.brier) : '—') + '</td>'
          + '<td style="font-size:11px;color:' + skColor + '">' + (sk != null ? _esc(sk) : '—') + '</td>'
          + '<td style="font-size:11px;color:var(--text-dim)">' + _esc(r.n) + '</td>'
          + '<td style="font-size:11px">' + wHtml + '</td>'
          + '</tr>';
      }
      lb += '</tbody></table>';
      lb += '<div style="font-size:10px;color:var(--text-dim);margin-top:8px">Weights tilt toward proven components once an engine has ≥20 scored directional calls. Until then the ensemble uses static base weights.</div>';
      lb += '</div></div>';
      el.innerHTML += lb;
    }
  } catch (e) {
    el.innerHTML = '<div class="panel mt-8"><div class="panel-body"><span class="col-negative" style="font-size:11px">Accountability unavailable: ' + _esc(e.message) + '</span></div></div>';
  }
}

function _forecastVerdictColor(verdict) {
  var v = (verdict || '').toUpperCase();
  if (v.indexOf('STRONG BUY') >= 0) return 'var(--green)';
  if (v.indexOf('BUY') >= 0 || v.indexOf('LONG') >= 0) return 'var(--green)';
  if (v.indexOf('STRONG SELL') >= 0 || v.indexOf('SELL') >= 0 || v.indexOf('SHORT') >= 0) return 'var(--red, #ff4444)';
  return 'var(--amber, #f0a020)';
}

async function analyzeForecast(sym) {
  var input = document.getElementById('forecast-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var hsel = document.getElementById('forecast-horizon');
  var horizon = parseInt(hsel && hsel.value, 10) || 20;
  var results = document.getElementById('forecast-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Fusing forecast signals for ' + _esc(symbol) + '...</div>';

  try {
    var data = await API.get('/api/forecast/ensemble/' + symbol + '?horizon=' + horizon);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);
    var ens = data.ensemble;
    if (!ens) throw new Error('No ensemble produced');

    var probPct = ens.prob_up != null ? Math.round(ens.prob_up * 100) : null;
    var probRawPct = ens.prob_up_raw != null ? Math.round(ens.prob_up_raw * 100) : null;
    var vColor = _forecastVerdictColor(ens.verdict);
    var dirArrow = ens.direction === 'UP' ? '▲' : ens.direction === 'DOWN' ? '▼' : '◆';
    var consensusPct = ens.consensus != null ? Math.round(ens.consensus * 100) : null;

    // ── Hero verdict + probability gauge ──────────────────────────────
    var html = '<div class="panel mb-8"><div class="panel-body" style="display:grid;grid-template-columns:1.2fr 1fr 1fr 1fr;gap:16px;align-items:center">';
    html += '<div>'
      + '<div class="form-label">VERDICT (' + _esc(data.horizon_days) + 'd)</div>'
      + '<div style="font-size:26px;font-weight:800;color:' + vColor + ';line-height:1.1">' + dirArrow + ' ' + _esc(ens.verdict) + '</div>'
      + '<div style="font-size:11px;color:var(--text-dim);margin-top:4px">Conviction: <b style="color:var(--text-secondary)">' + _esc(ens.conviction) + '</b> · ' + _esc(data.n_signals) + ' signals</div>'
      + '</div>';
    // P(up) gauge bar
    html += '<div>'
      + '<div class="form-label">P(UP) — CALIBRATED</div>'
      + '<div style="font-size:24px;font-weight:700;color:' + vColor + '">' + (probPct != null ? probPct + '%' : '—') + '</div>'
      + '<div style="height:8px;background:var(--bg-secondary);border-radius:4px;overflow:hidden;margin-top:4px;position:relative">'
      + '<div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--text-dim);z-index:2"></div>'
      + '<div style="height:100%;width:' + (probPct || 0) + '%;background:' + vColor + '"></div>'
      + '</div>'
      + '<div style="font-size:9px;color:var(--text-dim);margin-top:3px">raw ' + (probRawPct != null ? probRawPct + '%' : '—') + ' · edge ' + (ens.edge_pct_pts >= 0 ? '+' : '') + _esc(ens.edge_pct_pts) + 'pp</div>'
      + '</div>';
    // Consensus
    var conColor = (consensusPct || 0) >= 70 ? 'var(--green)' : (consensusPct || 0) >= 40 ? 'var(--amber,#f0a020)' : 'var(--red,#ff4444)';
    html += '<div>'
      + '<div class="form-label">SIGNAL CONSENSUS</div>'
      + '<div style="font-size:24px;font-weight:700;color:' + conColor + '">' + (consensusPct != null ? consensusPct + '%' : '—') + '</div>'
      + '<div style="font-size:9px;color:var(--text-dim);margin-top:6px">' + _esc(ens.agree_count) + ' agree · ' + _esc(ens.disagree_count) + ' disagree</div>'
      + '</div>';
    // Expected return cone center
    var cone = ens.return_cone;
    var er = cone && cone.expected_return_pct != null ? cone.expected_return_pct : null;
    var erColor = er == null ? 'var(--text-dim)' : er >= 0 ? 'var(--green)' : 'var(--red,#ff4444)';
    html += '<div>'
      + '<div class="form-label">EXPECTED RETURN</div>'
      + '<div style="font-size:24px;font-weight:700;color:' + erColor + '">' + (er != null ? (er >= 0 ? '+' : '') + er + '%' : '—') + '</div>'
      + '<div style="font-size:9px;color:var(--text-dim);margin-top:6px">' + _esc(cone ? cone.source : 'n/a') + '</div>'
      + '</div>';
    html += '</div></div>';

    // ── Return cone visualization (p05..p95) ──────────────────────────
    if (cone && cone.p05 != null && cone.p95 != null) {
      var lo = cone.p05, hi = cone.p95, span = (hi - lo) || 1;
      function pos(v) { return Math.max(0, Math.min(100, ((v - lo) / span) * 100)); }
      var p25 = pos(cone.p25), p75 = pos(cone.p75), med = pos(cone.median), zero = pos(0);
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">RETURN CONE — ' + _esc(data.horizon_days) + 'd</span></div>'
        + '<div class="panel-body">'
        + '<div style="position:relative;height:40px;margin:8px 4px 24px">'
        + '<div style="position:absolute;top:16px;left:0;right:0;height:8px;background:var(--bg-secondary);border-radius:4px"></div>'
        + '<div style="position:absolute;top:16px;left:' + p25 + '%;width:' + Math.max(0,(p75-p25)) + '%;height:8px;background:rgba(80,200,120,0.35);border-radius:4px"></div>'
        + '<div style="position:absolute;top:10px;left:' + med + '%;width:2px;height:20px;background:var(--green)"></div>'
        + '<div style="position:absolute;top:10px;left:' + zero + '%;width:1px;height:20px;background:var(--text-dim)"></div>'
        + '<div style="position:absolute;top:30px;left:0;font-size:9px;color:var(--text-dim)">' + _esc(cone.p05) + '% (p05)</div>'
        + '<div style="position:absolute;top:30px;right:0;font-size:9px;color:var(--text-dim)">' + _esc(cone.p95) + '% (p95)</div>'
        + '<div style="position:absolute;top:-4px;left:' + med + '%;transform:translateX(-50%);font-size:9px;color:var(--green);white-space:nowrap">median ' + _esc(cone.median) + '%</div>'
        + '</div>'
        + '<div style="font-size:10px;color:var(--text-dim)">Shaded band = interquartile (p25–p75). Grey tick = breakeven (0%).'
        + (cone.tilt_applied_pct ? ' Ensemble lean tilted the central estimate by ' + (cone.tilt_applied_pct >= 0 ? '+' : '') + _esc(cone.tilt_applied_pct) + '%.' : '')
        + '</div>'
        + '</div></div>';
    }

    // ── Per-signal contribution table ─────────────────────────────────
    var sigs = data.signals || [];
    if (sigs.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">SIGNAL CONTRIBUTIONS</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Signal</th><th>Direction</th><th>P(up)</th><th style="width:160px">Lean</th><th>Weight</th><th>Agrees</th>'
        + '</tr></thead><tbody>';
      for (var i = 0; i < sigs.length; i++) {
        var s = sigs[i];
        var sp = Math.round(s.prob_up * 100);
        var sColor = s.direction === 'UP' ? 'var(--green)' : s.direction === 'DOWN' ? 'var(--red,#ff4444)' : 'var(--text-dim)';
        var agreeHtml = s.agrees === true ? '<span style="color:var(--green)">✓</span>'
          : s.agrees === false ? '<span style="color:var(--red,#ff4444)">✗</span>'
          : '<span style="color:var(--text-dim)">—</span>';
        html += '<tr>'
          + '<td style="font-size:11px"><b>' + _esc(s.name) + '</b><div style="font-size:9px;color:var(--text-dim)">' + _esc(s.detail) + '</div></td>'
          + '<td><span class="signal-badge" style="font-size:9px;color:' + sColor + '">' + _esc(s.direction) + '</span></td>'
          + '<td style="color:' + sColor + ';font-weight:600">' + sp + '%</td>'
          + '<td><div style="height:6px;background:var(--bg-secondary);border-radius:3px;overflow:hidden;position:relative">'
          + '<div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:var(--text-dim)"></div>'
          + '<div style="height:100%;width:' + sp + '%;background:' + sColor + '"></div></div></td>'
          + '<td style="font-size:11px;color:var(--text-secondary)">' + Math.round(s.weight * 100) + '%</td>'
          + '<td style="text-align:center">' + agreeHtml + '</td>'
          + '</tr>';
      }
      html += '</tbody></table></div></div>';
    }

    // ── Notes ─────────────────────────────────────────────────────────
    var notes = data.notes || [];
    if (notes.length) {
      html += '<div class="panel"><div class="panel-header"><span class="panel-title">CALIBRATION NOTES</span></div>'
        + '<div class="panel-body"><ul style="margin:0;padding-left:18px;font-size:11px;color:var(--text-secondary)">';
      for (var n = 0; n < notes.length; n++) {
        html += '<li style="margin-bottom:4px">' + _esc(notes[n]) + '</li>';
      }
      html += '</ul></div></div>';
    }

    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

async function loadNarrativeView() {
  var view = document.getElementById('view-narrative');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">NARRATIVE VELOCITY ENGINE</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Media narrative rate-of-change analysis</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'
    + '<input id="narrative-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')analyzeNarrative()" />'
    + '<button class="btn btn-green" onclick="analyzeNarrative()">ANALYZE</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('narrative-symbol', 'analyzeNarrative', ['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'])
    + '</div>'
    + '<div id="narrative-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to analyze media narrative velocity</span></div>'
    + '</div>'
    + '</div></div>';
}

async function analyzeNarrative(sym) {
  var input = document.getElementById('narrative-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var results = document.getElementById('narrative-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Analyzing narrative velocity for ' + symbol + '...</div>';

  try {
    var data = await API.get('/api/narrative/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);

    var dominant = data.dominant_narrative || '—';
    var phase = (data.narrative_phase || data.phase || 'UNKNOWN').toUpperCase();
    var phaseCls = '';
    if (phase === 'EMERGENCE' || phase === 'ACCELERATION') phaseCls = 'col-positive';
    else if (phase === 'CONSENSUS') phaseCls = 'col-amber';
    else if (phase === 'EXHAUSTION' || phase === 'REVERSAL') phaseCls = 'col-negative';

    var velocity = data.velocity_score != null ? data.velocity_score : null;
    var velocityColor = velocity != null ? (velocity >= 0 ? 'var(--green)' : 'var(--red, #ff4444)') : 'var(--text-dim)';
    var contrarian = data.contrarian_signal;
    var contrarianHtml = contrarian ? '<span style="color:var(--red, #ff4444);font-weight:700">YES</span>' : '<span style="color:var(--text-dim)">NO</span>';

    var html = '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Dominant Narrative</div><div><span class="signal-badge" style="font-size:9px">' + _esc(dominant) + '</span></div></div>'
      + '<div class="kpi-card"><div class="form-label">Phase</div><div><span class="signal-badge ' + _esc(phaseCls) + '" style="font-size:9px">' + _esc(phase) + '</span></div></div>'
      + '<div class="kpi-card"><div class="form-label">Velocity Score</div><div style="font-size:18px;font-weight:700;color:' + velocityColor + '">' + (velocity != null ? _esc(velocity) : '—') + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Contrarian Signal</div><div style="font-size:16px">' + contrarianHtml + '</div></div>'
      + '</div>';

    // Window Breakdown
    var windows = data.windows || data.window_breakdown || {};
    var windowKeys = ['7d', '14d', '30d'];
    html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">WINDOW BREAKDOWN</span></div>'
      + '<div class="panel-body" style="display:flex;gap:16px">';
    for (var w = 0; w < windowKeys.length; w++) {
      var wk = windowKeys[w];
      var wData = windows[wk] || {};
      var buckets = wData.buckets || wData.distribution || null;
      html += '<div style="flex:1;padding:12px;background:var(--bg-secondary);border-radius:4px">'
        + '<div style="font-size:11px;color:var(--green);font-weight:600;margin-bottom:8px">' + wk.toUpperCase() + '</div>';
      if (typeof buckets === 'object' && buckets !== null) {
        var bKeys = Object.keys(buckets);
        for (var b = 0; b < bKeys.length; b++) {
          html += '<div style="font-size:10px;color:var(--text-secondary);margin-bottom:2px">'
            + '<span style="color:var(--text-dim)">' + _esc(bKeys[b]) + ':</span> ' + _esc(buckets[bKeys[b]])
            + '</div>';
        }
      }
      html += '</div>';
    }
    html += '</div></div>';

    // Article History
    var articles = data.articles || data.article_history || [];
    if (articles.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">ARTICLE HISTORY</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Date</th><th>Bucket</th><th>Title</th><th>Source</th>'
        + '</tr></thead><tbody>';
      for (var a = 0; a < articles.length; a++) {
        var art = articles[a];
        html += '<tr>'
          + '<td style="white-space:nowrap;font-size:10px;color:var(--text-dim)">' + _esc(art.date || '—') + '</td>'
          + '<td><span class="signal-badge" style="font-size:9px">' + _esc(art.bucket || art.sentiment || '—') + '</span></td>'
          + '<td style="font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(art.title || '—') + '</td>'
          + '<td style="font-size:10px;color:var(--text-dim)">' + _esc(art.source || '—') + '</td>'
          + '</tr>';
      }
      html += '</tbody></table></div></div>';
    }

    results.innerHTML = '<span class="narrative-track-slot" style="float:right"></span>' + html;
    // v0.2.0 — narrative tracker badge.
    if (window.renderTrackBadge) {
      const slot = results.querySelector('.narrative-track-slot');
      if (slot) { try { window.renderTrackBadge(slot, 'narrative'); } catch(_) {} }
    }
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — SYNTHETIC INSIDER COMPOSITE
// ══════════════════════════════════════════════════════════════════════════════

async function loadSyntheticInsiderView() {
  var view = document.getElementById('view-synthetic-insider');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">SYNTHETIC INSIDER COMPOSITE</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Multi-signal convergence approximating insider knowledge</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'
    + '<input id="si-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')analyzeSyntheticInsider()" />'
    + '<button class="btn btn-green" onclick="analyzeSyntheticInsider()">ANALYZE</button>'
    + '<button class="btn btn-ghost" onclick="scanSyntheticInsiderPortfolio()">PORTFOLIO SCAN</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('si-symbol', 'analyzeSyntheticInsider', ['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'])
    + '</div>'
    + '<div id="si-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to compute synthetic insider composite</span></div>'
    + '</div>'
    + '</div></div>';
}

async function analyzeSyntheticInsider(sym) {
  var input = document.getElementById('si-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var results = document.getElementById('si-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Computing synthetic insider composite for ' + symbol + '...</div>';

  try {
    var data = await API.get('/api/synthetic-insider/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);

    var score = data.composite_score != null ? data.composite_score : 0;
    var alertLevel = (data.alert_level || 'DORMANT').toUpperCase();
    var alertCls = '';
    if (alertLevel === 'CRITICAL') alertCls = 'col-positive';
    else if (alertLevel === 'CONVERGING') alertCls = 'col-amber';
    else if (alertLevel === 'AWAKENING') alertCls = 'col-positive';
    else alertCls = '';
    var alertStyle = '';
    if (alertLevel === 'AWAKENING') alertStyle = 'color:var(--blue, #4488ff)';
    else if (alertLevel === 'DORMANT') alertStyle = 'color:var(--text-dim)';

    var convergence = data.convergence_count != null ? data.convergence_count : 0;
    var totalChannels = data.total_channels || Object.keys(data.channels || {}).length || 6;
    var signal = data.signal || '—';

    // Large composite score display
    var html = '<div style="text-align:center;padding:20px;margin-bottom:16px;background:var(--bg-secondary);border-radius:6px">'
      + '<div style="font-size:48px;font-weight:700;color:' + _alphaScoreColor(score) + '">' + _alphaFmtNum(score, 1) + '</div>'
      + '<div style="font-size:12px;color:var(--text-dim);margin-top:4px">COMPOSITE SCORE</div>'
      + '<div style="margin-top:8px"><span class="signal-badge ' + alertCls + '" style="font-size:10px;' + alertStyle + '">' + alertLevel + '</span></div>'
      + '</div>';

    html += '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Composite Score</div><div style="font-size:18px;font-weight:700;color:' + _alphaScoreColor(score) + '">' + _alphaFmtNum(score, 1) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Alert Level</div><div><span class="signal-badge ' + alertCls + '" style="font-size:9px;' + alertStyle + '">' + alertLevel + '</span></div></div>'
      + '<div class="kpi-card"><div class="form-label">Convergence Count</div><div style="font-size:18px;font-weight:700">' + convergence + '/' + totalChannels + ' <span style="font-size:10px;color:var(--text-dim)">firing</span></div></div>'
      + '<div class="kpi-card"><div class="form-label">Signal</div><div style="font-size:14px;font-weight:600">' + _esc(signal) + '</div></div>'
      + '</div>';

    // Channel Breakdown
    var channels = Array.isArray(data.channels) ? data.channels
      : Object.entries(data.channels || data.channel_breakdown || {}).map(function (kv) {
          return Object.assign({channel: kv[0]}, kv[1]);
        });
    if (channels.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">CHANNEL BREAKDOWN</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Channel</th><th>Score</th><th>Firing</th><th>Weight</th><th>Detail</th>'
        + '</tr></thead><tbody>';
      for (var i = 0; i < channels.length; i++) {
        var ch = channels[i];
        var chScore = ch.score != null ? ch.score : 0;
        var firing = ch.firing ? '<span style="color:var(--green)">&#10003;</span>' : '<span style="color:var(--text-dim)">—</span>';
        html += '<tr>'
          + '<td style="font-weight:600">' + _esc(ch.channel || ch.name || '—') + '</td>'
          + '<td style="color:' + _alphaScoreColor(chScore) + '">' + _alphaFmtNum(chScore, 1) + '</td>'
          + '<td>' + firing + '</td>'
          + '<td>' + _alphaFmtNum(ch.weight, 2) + '</td>'
          + '<td style="font-size:10px;color:var(--text-dim);max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(ch.detail || '—') + '</td>'
          + '</tr>';
      }
      html += '</tbody></table></div></div>';
    }

    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

async function scanSyntheticInsiderPortfolio() {
  var results = document.getElementById('si-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Scanning portfolio for synthetic insider signals...</div>';

  try {
    var data = await API.post('/api/synthetic-insider/scan', {});
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);
    var items = data.results || data.scores || [];
    if (!items.length) {
      results.innerHTML = '<div class="empty-state"><span style="color:var(--text-dim)">No portfolio symbols found</span></div>';
      return;
    }
    items.sort(function(a, b) { return (b.composite_score || b.score || 0) - (a.composite_score || a.score || 0); });

    var html = '<div style="margin-bottom:12px;font-size:11px;color:var(--text-dim)">' + items.length + ' symbols scanned</div>'
      + '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">PORTFOLIO SYNTHETIC INSIDER SCAN</span></div>'
      + '<div class="panel-body"><table class="data-table"><thead><tr>'
      + '<th>Symbol</th><th>Score</th><th>Alert Level</th><th>Convergence</th>'
      + '</tr></thead><tbody>';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var sc = it.composite_score || it.score || 0;
      html += '<tr>'
        + '<td><span class="col-symbol" style="cursor:pointer" onclick="document.getElementById(\'si-symbol\').value=\'' + _jesc(it.symbol || it.ticker || '') + '\';analyzeSyntheticInsider()">' + _esc(it.symbol || it.ticker || '—') + '</span></td>'
        + '<td style="color:' + _alphaScoreColor(sc) + ';font-weight:700">' + _alphaFmtNum(sc, 1) + '</td>'
        + '<td>' + _alphaRegimeBadge(it.alert_level || '—') + '</td>'
        + '<td>' + (it.convergence_count != null ? it.convergence_count : '—') + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div></div>';
    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — REFLEXIVITY DETECTOR
// ══════════════════════════════════════════════════════════════════════════════

async function loadReflexivityView() {
  var view = document.getElementById('view-reflexivity');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">REFLEXIVITY DETECTOR</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Self-reinforcing feedback loop detection (Soros)</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">'
    + '<input id="reflex-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')detectReflexivity()" />'
    + '<button class="btn btn-green" onclick="detectReflexivity()">DETECT</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('reflex-symbol', 'detectReflexivity', ['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'])
    + '</div>'
    + '<div id="reflex-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to detect reflexivity loops</span></div>'
    + '</div>'
    + '</div></div>';
}

async function detectReflexivity(sym) {
  var input = document.getElementById('reflex-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var results = document.getElementById('reflex-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Detecting reflexivity loops for ' + symbol + '...</div>';

  try {
    var data = await API.get('/api/reflexivity/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);

    // Backend returns active_loops as an ARRAY of loop objects (+ a loop_count).
    // Use the count for the KPI; render the array as text would show
    // "[object Object],[object Object]".
    var loopList = Array.isArray(data.active_loops) ? data.active_loops : [];
    var activeLoops = data.loop_count != null ? data.loop_count
                      : loopList.filter(function (l) { return l && l.detected; }).length;
    var maxStrength = data.max_strength != null ? data.max_strength : 0;
    var dominant = data.dominant_loop || '—';
    var overallRisk = (data.overall_risk || 'LOW').toUpperCase();
    var riskCls = '';
    if (overallRisk === 'HIGH' || overallRisk === 'CRITICAL') riskCls = 'col-negative';
    else if (overallRisk === 'MEDIUM' || overallRisk === 'MODERATE') riskCls = 'col-amber';
    else riskCls = 'col-positive';

    var html = '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Active Loops</div><div style="font-size:18px;font-weight:700">' + activeLoops + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Max Strength</div><div style="font-size:18px;font-weight:700;color:' + _alphaScoreColor(maxStrength) + '">' + _alphaFmtNum(maxStrength, 1) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Dominant Loop</div><div style="font-size:12px;font-weight:600">' + _esc(dominant) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Overall Risk</div><div><span class="signal-badge ' + riskCls + '" style="font-size:9px">' + overallRisk + '</span></div></div>'
      + '</div>';

    // Loop type cards
    var loopTypes = [
      {key: 'EQUITY_FEEDBACK', label: 'EQUITY FEEDBACK'},
      {key: 'DILUTION_SPIRAL', label: 'DILUTION SPIRAL'},
      {key: 'INDEX_INCLUSION', label: 'INDEX INCLUSION'},
      {key: 'SHORT_SQUEEZE', label: 'SHORT SQUEEZE'}
    ];
    // Index the active_loops array by its `type` field so the per-type cards
    // populate (the old code read data.loops[key], but the backend sends an
    // array under active_loops, so every card showed "Detected: NO").
    var loops = data.loops || data.feedback_loops || null;
    if (!loops) {
      loops = {};
      loopList.forEach(function (l) { if (l && l.type) loops[l.type] = l; });
    }

    html += '<div style="display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-bottom:16px">';
    for (var i = 0; i < loopTypes.length; i++) {
      var lt = loopTypes[i];
      var loop = loops[lt.key] || {};
      var detected = loop.detected || false;
      var strength = loop.strength != null ? loop.strength : 0;
      var direction = (loop.direction || '').toUpperCase();
      var dirCls = direction === 'VIRTUOUS' ? 'col-positive' : (direction === 'VICIOUS' ? 'col-negative' : '');
      var proximityVal = loop.proximity_to_trigger_pct != null ? loop.proximity_to_trigger_pct : loop.proximity;
      var proximity = proximityVal != null ? _alphaFmtNum(proximityVal, 1) + '%' : '—';
      var timeline = loop.acceleration_timeline || loop.timeline || '—';
      var detail = loop.detail || loop.description || '—';

      html += '<div class="panel" style="margin-bottom:0">'
        + '<div class="panel-header"><span class="panel-title">' + lt.label + '</span></div>'
        + '<div class="panel-body">'
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
        + '<span style="font-size:11px;font-weight:600">Detected:</span>'
        + (detected
          ? '<span style="color:var(--green);font-weight:700">YES</span>'
          : '<span style="color:var(--text-dim)">NO</span>')
        + '</div>'
        + '<div style="margin-bottom:8px">'
        + '<div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">Strength</div>'
        + '<div style="background:var(--bg-primary);border-radius:3px;height:8px;overflow:hidden">'
        + '<div style="height:100%;width:' + Math.min(strength, 100) + '%;background:' + _alphaScoreColor(strength) + ';border-radius:3px"></div>'
        + '</div>'
        + '<div style="font-size:10px;color:' + _alphaScoreColor(strength) + ';margin-top:2px">' + _alphaFmtNum(strength, 1) + '/100</div>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:10px">'
        + '<div><span style="color:var(--text-dim)">Direction:</span> <span class="' + dirCls + '">' + _esc(direction || '—') + '</span></div>'
        + '<div><span style="color:var(--text-dim)">Proximity:</span> ' + proximity + '</div>'
        + '<div><span style="color:var(--text-dim)">Timeline:</span> ' + _esc(timeline) + '</div>'
        + '</div>'
        + '<div style="font-size:10px;color:var(--text-dim);margin-top:6px;border-top:1px solid var(--border);padding-top:6px">' + _esc(detail) + '</div>'
        + '</div></div>';
    }
    html += '</div>';

    // Fundamentals snapshot
    var fundamentals = data.fundamentals_snapshot || data.fundamentals || {};
    if (Object.keys(fundamentals).length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">FUNDAMENTALS SNAPSHOT</span></div>'
        + '<div class="panel-body" style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:11px">';
      var fKeys = Object.keys(fundamentals);
      for (var f = 0; f < fKeys.length; f++) {
        html += '<div><span style="color:var(--text-dim)">' + _esc(fKeys[f].replace(/_/g, ' ').toUpperCase()) + ':</span> '
          + '<span style="font-weight:600">' + _esc(fundamentals[fKeys[f]]) + '</span></div>';
      }
      html += '</div></div>';
    }

    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — LIQUIDITY REGIME MONITOR
// ══════════════════════════════════════════════════════════════════════════════

async function loadLiquidityView() {
  var view = document.getElementById('view-liquidity');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">LIQUIDITY REGIME MONITOR</span>'
    + '<button class="btn btn-ghost btn-sm" onclick="loadLiquidityData()" style="margin-left:auto">REFRESH</button></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Market-wide stress early warning system</p>'
    + '<div id="liquidity-results">'
    + '<div class="loading"><div class="spinner"></div> Loading liquidity regime data...</div>'
    + '</div>'
    + '</div></div>';

  loadLiquidityData();
}

async function loadLiquidityData() {
  var results = document.getElementById('liquidity-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Fetching liquidity regime data...</div>';

  try {
    var data = await API.get('/api/liquidity');
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);

    var score = data.composite_score != null ? data.composite_score : 0;
    var regime = (data.regime || 'UNKNOWN').toUpperCase();
    var regimeCls = '';
    var regimeStyle = '';
    if (regime === 'NORMAL') regimeCls = 'col-positive';
    else if (regime === 'ELEVATED') regimeCls = 'col-amber';
    else if (regime === 'WARNING') regimeCls = 'col-negative';
    else if (regime === 'CRITICAL') { regimeCls = 'col-negative'; regimeStyle = 'color:var(--magenta, #ff44ff)'; }

    var positionSizingVal = data.position_sizing_multiplier != null ? data.position_sizing_multiplier : data.position_sizing;
    var positionSizing = positionSizingVal != null ? _alphaFmtNum(positionSizingVal, 2) + 'x' : '—';
    var percentile = data.percentile_rank != null ? _alphaFmtNum(data.percentile_rank, 1) + '%' : '—';
    var recommendation = data.recommendation || '';

    // Large composite score display
    var html = '<div style="text-align:center;padding:20px;margin-bottom:16px;background:var(--bg-secondary);border-radius:6px">'
      + '<div style="font-size:48px;font-weight:700;color:' + _alphaScoreColor(score) + '">' + _alphaFmtNum(score, 1) + '</div>'
      + '<div style="font-size:12px;color:var(--text-dim);margin-top:4px">COMPOSITE STRESS SCORE</div>'
      + '<div style="margin-top:8px"><span class="signal-badge ' + regimeCls + '" style="font-size:11px;' + regimeStyle + '">' + regime + '</span></div>'
      + '</div>';

    html += '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Composite Score</div><div style="font-size:18px;font-weight:700;color:' + _alphaScoreColor(score) + '">' + _alphaFmtNum(score, 1) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Regime</div><div><span class="signal-badge ' + regimeCls + '" style="font-size:9px;' + regimeStyle + '">' + regime + '</span></div></div>'
      + '<div class="kpi-card"><div class="form-label">Position Sizing</div><div style="font-size:18px;font-weight:700">' + positionSizing + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Percentile Rank</div><div style="font-size:18px;font-weight:700">' + percentile + '</div></div>'
      + '</div>';

    if (recommendation) {
      html += '<div style="padding:12px;background:var(--bg-secondary);border-radius:4px;font-size:11px;color:var(--text-secondary);margin-bottom:16px;border-left:3px solid var(--green)">'
        + '<strong style="color:var(--green)">RECOMMENDATION:</strong> ' + _esc(recommendation) + '</div>';
    }

    // Indicators table
    var indicators = Array.isArray(data.indicators) ? data.indicators
      : Object.entries(data.indicators || {}).map(function (kv) {
          return Object.assign({indicator: kv[0]}, kv[1]);
        });
    if (indicators.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">STRESS INDICATORS</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Indicator</th><th>Score</th><th>Value</th><th>Weight</th><th>Detail</th>'
        + '</tr></thead><tbody>';
      for (var i = 0; i < indicators.length; i++) {
        var ind = indicators[i];
        var indScore = ind.score != null ? ind.score : 0;
        var barWidth = Math.min(Math.abs(indScore), 100);
        html += '<tr>'
          + '<td style="font-weight:600;font-size:11px">' + _esc(ind.indicator || ind.name || '—') + '</td>'
          + '<td>'
          + '<div style="display:flex;align-items:center;gap:6px">'
          + '<div style="width:60px;height:6px;background:var(--bg-primary);border-radius:3px;overflow:hidden">'
          + '<div style="height:100%;width:' + barWidth + '%;background:' + _alphaScoreColor(indScore) + ';border-radius:3px"></div>'
          + '</div>'
          + '<span style="font-size:11px;color:' + _alphaScoreColor(indScore) + '">' + _alphaFmtNum(indScore, 1) + '</span>'
          + '</div>'
          + '</td>'
          + '<td style="font-size:11px">' + _esc(ind.value != null ? ind.value : '—') + '</td>'
          + '<td style="font-size:11px">' + _alphaFmtNum(ind.weight, 2) + '</td>'
          + '<td style="font-size:10px;color:var(--text-dim);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(ind.detail || '—') + '</td>'
          + '</tr>';
      }
      html += '</tbody></table></div></div>';
    }

    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// ══════════════════════════════════════════════════════════════════════════════
// ALPHA ENGINE — REVENUE NOWCASTING (ALT DATA)
// ══════════════════════════════════════════════════════════════════════════════

async function loadAltDataView() {
  var view = document.getElementById('view-alt-data');
  view.innerHTML = '<div class="panel">'
    + '<div class="panel-header"><span class="panel-title">REVENUE NOWCAST + SOCIAL PULSE</span></div>'
    + '<div class="panel-body">'
    + '<p style="color:var(--text-secondary);font-size:11px;margin-bottom:12px">Alt-data earnings-surprise predictor, plus a live per-symbol <b>Social Pulse</b> — StockTwits sentiment, Hacker News chatter, and Wikipedia attention.</p>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap">'
    + '<input id="altdata-symbol" type="text" class="form-input" placeholder="Symbol..." style="width:120px" onkeydown="if(event.key===\'Enter\')nowcastAltData()" />'
    + '<button class="btn btn-green" onclick="nowcastAltData()">NOWCAST</button>'
    + '<button class="btn btn-ghost" onclick="loadWikiAttention()">WIKI ATTENTION</button>'
    + '<button class="btn btn-ghost" onclick="loadHackerNews()">HN SENTIMENT</button>'
    + '<button class="btn btn-ghost" onclick="scanAltDataPortfolio()">PORTFOLIO SCAN</button>'
    + '</div>'
    + '<div style="display:flex;gap:6px;margin-bottom:16px">'
    + _alphaQuickPicks('altdata-symbol', 'nowcastAltData', ['AAPL','NVDA','TSLA','SPY','QQQ','AMZN'])
    + '</div>'
    + '<div id="altdata-results">'
    + '<div class="empty-state"><span style="color:var(--text-dim)">Enter a symbol to generate revenue nowcast</span></div>'
    + '</div>'
    + '</div></div>';

  // Reddit ticker mentions + StockTwits trending (no-key Tier 3.4)
  DataPanels.appendAltDataPanels(view);
}

async function nowcastAltData(sym) {
  var input = document.getElementById('altdata-symbol');
  var symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  var results = document.getElementById('altdata-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Generating revenue nowcast for ' + symbol + '...</div>';

  try {
    var data = await API.get('/api/alt-data/' + symbol);
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);

    var score = data.nowcast_score != null ? data.nowcast_score : (data.score || 0);
    var beatProb = data.beat_probability != null ? data.beat_probability : null;
    var surprise = data.estimated_surprise_pct != null ? data.estimated_surprise_pct : (data.estimated_surprise != null ? data.estimated_surprise : null);
    var confidence = data.confidence_level || data.confidence || '—';

    var beatProbUpper = (typeof beatProb === 'string' ? beatProb : '').toUpperCase();
    var beatBadgeCls;
    if (typeof beatProb === 'number') {
      beatBadgeCls = beatProb >= 60 ? 'col-positive' : (beatProb >= 40 ? 'col-amber' : 'col-negative');
    } else {
      beatBadgeCls = beatProbUpper.indexOf('BEAT') >= 0 ? 'col-positive' : (beatProbUpper.indexOf('MISS') >= 0 ? 'col-negative' : 'col-amber');
    }
    var beatProbText = beatProb == null ? '—' : (typeof beatProb === 'number' ? _alphaFmtNum(beatProb, 1) + '%' : beatProb);

    var html = '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Nowcast Score</div><div style="font-size:18px;font-weight:700;color:' + _alphaScoreColor(score) + '">' + _alphaFmtNum(score, 1) + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Beat Probability</div><div><span class="signal-badge ' + beatBadgeCls + '" style="font-size:10px">' + beatProbText + '</span></div></div>'
      + '<div class="kpi-card"><div class="form-label">Est. Surprise %</div><div style="font-size:18px;font-weight:700;color:' + (surprise != null && surprise >= 0 ? 'var(--green)' : 'var(--red, #ff4444)') + '">' + (surprise != null ? (surprise >= 0 ? '+' : '') + _alphaFmtNum(surprise, 2) + '%' : '—') + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Confidence</div><div style="font-size:14px;font-weight:600">' + _esc(confidence) + '</div></div>'
      + '</div>';

    // Signal Breakdown
    var signals = Array.isArray(data.signals) ? data.signals
      : Object.entries(data.signals || data.signal_breakdown || {}).map(function (kv) {
          return Object.assign({signal: kv[0]}, kv[1]);
        });
    if (signals.length) {
      html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">SIGNAL BREAKDOWN</span></div>'
        + '<div class="panel-body"><table class="data-table"><thead><tr>'
        + '<th>Signal</th><th>Score</th><th>Weight</th><th>Detail</th>'
        + '</tr></thead><tbody>';
      for (var i = 0; i < signals.length; i++) {
        var sig = signals[i];
        var sigScore = sig.score != null ? sig.score : 0;
        html += '<tr>'
          + '<td style="font-weight:600;font-size:11px">' + _esc(sig.signal || sig.name || '—') + '</td>'
          + '<td style="color:' + _alphaScoreColor(sigScore) + ';font-weight:700">' + _alphaFmtNum(sigScore, 1) + '</td>'
          + '<td>' + _alphaFmtNum(sig.weight, 2) + '</td>'
          + '<td style="font-size:10px;color:var(--text-dim);max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(sig.detail || '—') + '</td>'
          + '</tr>';
      }
      html += '</tbody></table></div></div>';
    }

    // Per-symbol SOCIAL PULSE (StockTwits + Hacker News + Wikipedia) — this is
    // the live social data for the looked-up symbol, rendered below the nowcast.
    results.innerHTML = html + '<div id="altdata-social"></div>';
    renderSocialPulse(symbol);
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// Status badge for a social source.
function _socialBadge(status) {
  var map = {
    live:        ['● LIVE', 'var(--green)'],
    ratelimited: ['◐ RATE-LIMITED', 'var(--amber, #f0a020)'],
    quiet:       ['○ QUIET', 'var(--text-dim)'],
    unavailable: ['✕ UNAVAILABLE', 'var(--text-dim)'],
    error:       ['✕ ERROR', 'var(--red, #ff4444)'],
  };
  var m = map[status] || ['—', 'var(--text-dim)'];
  return '<span style="font-size:9px;font-weight:700;color:' + m[1] + '">' + m[0] + '</span>';
}

// Tiny inline bar sparkline from [{date,views}] (Wikipedia pageviews).
function _miniSparkline(points, color) {
  if (!points || !points.length) return '';
  var vals = points.map(function (p) { return p.views || 0; });
  var max = Math.max.apply(null, vals) || 1, min = Math.min.apply(null, vals);
  var span = (max - min) || 1;
  var bars = vals.map(function (v) {
    var h = 3 + Math.round((v - min) / span * 21);  // 3..24px
    return '<div title="' + _esc(String(v)) + '" style="flex:1;height:' + h + 'px;background:' + color + ';opacity:.55;border-radius:1px"></div>';
  }).join('');
  return '<div style="display:flex;align-items:flex-end;gap:1px;height:24px;margin-top:4px">' + bars + '</div>';
}

async function renderSocialPulse(symbol) {
  var el = document.getElementById('altdata-social');
  if (!el) return;
  el.innerHTML = '<div class="panel mt-8"><div class="panel-body"><div class="loading"><div class="spinner"></div> Pulling social signals for ' + _esc(symbol) + '...</div></div></div>';
  try {
    var d = await API.get('/api/alt-data/social/' + symbol);
    if (d.error) throw new Error(d.error);
    var comp = d.composite || {};
    var src = d.sources || {};
    var st = src.stocktwits || {}, hn = src.hackernews || {}, wk = src.wikipedia || {};

    // ── Composite KPI row ──────────────────────────────────────────────
    var buzz = comp.buzz_score;
    var buzzColor = buzz == null ? 'var(--text-dim)' : buzz >= 70 ? 'var(--green)' : buzz >= 40 ? 'var(--amber,#f0a020)' : 'var(--text-secondary)';
    var sentLabel = comp.sentiment_label || '—';
    var sentColor = sentLabel === 'BULLISH' ? 'var(--green)' : sentLabel === 'BEARISH' ? 'var(--red,#ff4444)' : 'var(--text-dim)';
    var spike = comp.spike_pct;
    var spikeColor = spike == null ? 'var(--text-dim)' : spike > 15 ? 'var(--green)' : spike < -15 ? 'var(--red,#ff4444)' : 'var(--text-secondary)';

    var html = '<div class="panel mt-8"><div class="panel-header"><span class="panel-title">SOCIAL PULSE — ' + _esc(symbol) + '</span>'
      + '<span style="font-size:9px;color:var(--text-dim);margin-left:8px">' + _esc(comp.sources_live || 0) + '/3 sources live · StockTwits · Hacker News · Wikipedia</span></div>'
      + '<div class="panel-body">'
      + '<div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
      + '<div class="kpi-card"><div class="form-label">Social Buzz</div><div style="font-size:22px;font-weight:800;color:' + buzzColor + '">' + (buzz != null ? buzz : '—') + '</div>'
      + '<div style="height:5px;background:var(--bg-secondary);border-radius:3px;margin-top:4px;overflow:hidden"><div style="height:100%;width:' + (buzz || 0) + '%;background:' + buzzColor + '"></div></div></div>'
      + '<div class="kpi-card"><div class="form-label">Social Sentiment</div><div style="font-size:16px;font-weight:700;color:' + sentColor + ';margin-top:4px">' + _esc(sentLabel) + '</div><div style="font-size:9px;color:var(--text-dim)">' + (comp.sentiment != null ? (comp.sentiment >= 0 ? '+' : '') + comp.sentiment : '—') + '</div></div>'
      + '<div class="kpi-card"><div class="form-label">Wiki Attention</div><div style="font-size:18px;font-weight:700;color:' + spikeColor + '">' + (spike != null ? (spike >= 0 ? '+' : '') + spike + '%' : '—') + '</div><div style="font-size:9px;color:var(--text-dim)">vs 7d baseline</div></div>'
      + '<div class="kpi-card"><div class="form-label">HN Mentions</div><div style="font-size:18px;font-weight:700">' + _esc(hn.mention_count != null ? hn.mention_count : '—') + '</div><div style="font-size:9px;color:var(--text-dim)">7-day window</div></div>'
      + '</div>';

    // ── StockTwits sentiment + sample messages ─────────────────────────
    html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">STOCKTWITS</span> ' + _socialBadge(st.status) + '</div><div class="panel-body">';
    if (st.status === 'live') {
      var bull = st.bullish || 0, bear = st.bearish || 0, tot = bull + bear;
      var bullPct = tot ? Math.round(bull / tot * 100) : 50;
      html += '<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:4px"><span style="color:var(--green)">▲ ' + bull + ' bullish</span>'
        + '<span style="color:var(--text-dim)">' + _esc(st.messages_total || 0) + ' msgs</span>'
        + '<span style="color:var(--red,#ff4444)">' + bear + ' bearish ▼</span></div>'
        + '<div style="height:10px;background:var(--red,#ff4444);border-radius:5px;overflow:hidden;margin-bottom:12px"><div style="height:100%;width:' + bullPct + '%;background:var(--green)"></div></div>';
      var samples = st.sample || [];
      for (var i = 0; i < samples.length; i++) {
        var m = samples[i];
        var sc = m.sentiment === 'Bullish' ? 'var(--green)' : m.sentiment === 'Bearish' ? 'var(--red,#ff4444)' : 'var(--text-dim)';
        html += '<div style="border-left:2px solid ' + sc + ';padding:4px 8px;margin-bottom:6px;background:var(--bg-secondary);border-radius:0 3px 3px 0">'
          + '<div style="font-size:11px;color:var(--text-secondary)">' + _esc(m.body || '') + '</div>'
          + '<div style="font-size:9px;color:var(--text-dim);margin-top:2px">@' + _esc(m.user || '?') + (m.sentiment ? ' · <span style="color:' + sc + '">' + _esc(m.sentiment) + '</span>' : '') + '</div></div>';
      }
    } else if (st.status === 'ratelimited') {
      html += '<div style="font-size:11px;color:var(--amber,#f0a020)">StockTwits is rate-limiting (~200 req/hr unauthenticated). Try again shortly.</div>';
    } else {
      html += '<div style="font-size:11px;color:var(--text-dim)">No recent StockTwits sentiment for ' + _esc(symbol) + '.</div>';
    }
    html += '</div></div>';

    // ── Wikipedia attention with sparkline ─────────────────────────────
    html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">WIKIPEDIA ATTENTION</span> ' + _socialBadge(wk.status) + '</div><div class="panel-body">';
    if (wk.status === 'live') {
      var ws = wk.stats || {};
      html += '<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-secondary)">'
        + '<span>Latest: <b>' + _esc((Number.isFinite(+ws.latest) ? (+ws.latest).toLocaleString('en-US') : '—')) + '</b> views/day</span>'
        + '<span>7d baseline: ' + _esc((Number.isFinite(+ws.baseline_7d) ? Math.round(+ws.baseline_7d).toLocaleString('en-US') : '—')) + '</span></div>'
        + _miniSparkline(wk.points, 'var(--blue)')
        + (wk.article_url ? '<div style="margin-top:6px"><a href="' + _esc(safeUrl(wk.article_url)) + '" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue)">' + _esc(wk.article || 'article') + ' ↗</a></div>' : '');
    } else {
      html += '<div style="font-size:11px;color:var(--text-dim)">Wikipedia attention unavailable for ' + _esc(symbol) + '.</div>';
    }
    html += '</div></div>';

    // ── Hacker News mentions ───────────────────────────────────────────
    html += '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">HACKER NEWS</span> ' + _socialBadge(hn.status) + '</div><div class="panel-body">';
    if (hn.status === 'live' && (hn.mention_count || 0) > 0) {
      var hs = hn.stats || {};
      var np = hs.net_polarity;
      var npColor = np == null ? 'var(--text-dim)' : np > 0 ? 'var(--green)' : np < 0 ? 'var(--red,#ff4444)' : 'var(--text-dim)';
      html += '<div style="font-size:11px;color:var(--text-secondary);margin-bottom:6px">' + _esc(hn.mention_count) + ' mentions · net polarity <span style="color:' + npColor + ';font-weight:700">' + (np != null ? (np >= 0 ? '+' : '') + np : '—') + '</span> (' + _esc(hs.positive_count || 0) + '▲ / ' + _esc(hs.negative_count || 0) + '▼)</div>';
      var hm = hn.mentions || [];
      for (var j = 0; j < Math.min(hm.length, 4); j++) {
        var it = hm[j];
        html += '<div style="font-size:10px;margin-bottom:3px">' + (it.url ? '<a href="' + _esc(safeUrl(it.url)) + '" target="_blank" rel="noopener" style="color:var(--blue)">' : '<span style="color:var(--text-secondary)">') + _esc((it.title || it.text || '').slice(0, 110)) + (it.url ? ' ↗</a>' : '</span>') + '</div>';
      }
    } else {
      html += '<div style="font-size:11px;color:var(--text-dim)">No Hacker News chatter on ' + _esc(symbol) + ' in the last 7 days.</div>';
    }
    html += '</div></div>';

    // ── Reddit honest status ───────────────────────────────────────────
    var rd = d.reddit || {};
    html += '<div style="font-size:10px;color:var(--text-dim);padding:6px 2px">REDDIT ' + _socialBadge(rd.status) + ' — ' + _esc(rd.note || '') + '</div>';

    html += '</div></div>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div class="panel mt-8"><div class="panel-body"><span class="col-negative" style="font-size:11px">Social pulse unavailable: ' + _esc(e.message) + '</span></div></div>';
  }
}

async function loadWikiAttention(sym) {
  const input = document.getElementById('altdata-symbol');
  const symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  const results = document.getElementById('altdata-results');
  if (!results) return;
  await DataPanels.renderWikiAttention(results, symbol);
}

async function loadHackerNews(sym) {
  const input = document.getElementById('altdata-symbol');
  const symbol = sym || (input ? input.value.trim().toUpperCase() : '');
  if (!symbol) return;
  if (input) input.value = symbol;
  const results = document.getElementById('altdata-results');
  if (!results) return;
  await DataPanels.renderHackerNews(results, symbol);
}

async function scanAltDataPortfolio() {
  var results = document.getElementById('altdata-results');
  if (!results) return;
  results.innerHTML = '<div class="loading"><div class="spinner"></div> Scanning portfolio for revenue nowcast signals...</div>';

  try {
    var data = await API.post('/api/alt-data/scan', {});
    if (!results.isConnected) return; // navigated away during fetch
    if (data.error) throw new Error(data.error);
    var items = data.results || data.scores || [];
    if (!items.length) {
      results.innerHTML = '<div class="empty-state"><span style="color:var(--text-dim)">No portfolio symbols found</span></div>';
      return;
    }
    items.sort(function(a, b) { return (b.nowcast_score || b.score || 0) - (a.nowcast_score || a.score || 0); });

    var html = '<div style="margin-bottom:12px;font-size:11px;color:var(--text-dim)">' + items.length + ' symbols scanned</div>'
      + '<div class="panel mb-8"><div class="panel-header"><span class="panel-title">PORTFOLIO NOWCAST SCAN</span></div>'
      + '<div class="panel-body"><table class="data-table"><thead><tr>'
      + '<th>Symbol</th><th>Score</th><th>Beat Probability</th><th>Signal</th>'
      + '</tr></thead><tbody>';
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      var sc = it.nowcast_score || it.score || 0;
      var bp = it.beat_probability;
      html += '<tr>'
        + '<td><span class="col-symbol" style="cursor:pointer" onclick="document.getElementById(\'altdata-symbol\').value=\'' + _jesc(it.symbol || it.ticker || '') + '\';nowcastAltData()">' + _esc(it.symbol || it.ticker || '—') + '</span></td>'
        + '<td style="color:' + _alphaScoreColor(sc) + ';font-weight:700">' + _alphaFmtNum(sc, 1) + '</td>'
        + '<td>' + (bp != null ? _alphaFmtNum(bp, 1) + '%' : '—') + '</td>'
        + '<td style="font-size:10px;color:var(--text-dim)">' + _esc(it.signal || '—') + '</td>'
        + '</tr>';
    }
    html += '</tbody></table></div></div>';
    results.innerHTML = html;
  } catch (e) {
    results.innerHTML = '<div class="empty-state"><span class="col-negative">Error: ' + _esc(e.message) + '</span></div>';
  }
}

// ════════════════════════════════════════════════════════════════════
//   NEW DATA-SOURCE PANELS — render helpers for Tier 1 / Tier 3 feeds
// ════════════════════════════════════════════════════════════════════
const DataPanels = {
  _esc: _esc,
  _fmtCompact(n) { try { return fmt.compact(n); } catch { return n; } },

  // ── Crypto: DefiLlama TVL + BTC mempool + cross-exchange ──────
  async appendDefiPanel(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    wrap.innerHTML = `<div class="loading"><div class="spinner"></div> LOADING DEFI / BTC NETWORK / EXCHANGES…</div>`;
    parent.appendChild(wrap);
    try {
      const [defi, btc, xex] = await Promise.all([
        API.get('/api/crypto/defi'),
        API.get('/api/crypto/btc/mempool'),
        API.get('/api/crypto/cross-exchange?pair=BTC%2FUSDT'),
      ]);
      const chains = (defi?.chains || []).map(c =>
        `<tr><td class="col-symbol">${this._esc(c.name)}</td>
             <td class="col-number">$${this._fmtCompact(c.tvl)}</td></tr>`).join('');
      const protos = (defi?.protocols || []).slice(0, 12).map(p => {
        const cls1d = (p.change_1d ?? 0) >= 0 ? 'col-positive' : 'col-negative';
        return `<tr>
          <td class="col-symbol">${this._esc(p.name)}</td>
          <td style="font-size:10px;color:var(--text-dim)">${this._esc(p.category||'')}</td>
          <td class="col-number">$${this._fmtCompact(p.tvl)}</td>
          <td class="${cls1d}">${(p.change_1d ?? 0).toFixed(2)}%</td>
        </tr>`;
      }).join('');
      const fees = btc?.fees || {};
      const ex = (xex?.exchanges || {});
      const exRows = Object.entries(ex).map(([name, d]) =>
        `<tr><td class="col-symbol">${this._esc(name).toUpperCase()}</td>
             <td class="col-price">$${(d.last||0).toFixed(2)}</td>
             <td class="col-number">${this._fmtCompact(d.volume||0)}</td></tr>`).join('');
      wrap.innerHTML = `
        <div class="panel-grid grid-3" style="gap:8px">
          <div class="panel">
            <div class="panel-header"><span class="panel-title">DeFi TVL — Top Protocols</span>
              <span class="panel-meta">total $${this._fmtCompact(defi?.total_tvl)}</span></div>
            <table class="data-table"><thead><tr><th>Protocol</th><th>Cat</th><th>TVL</th><th>Δ1D</th></tr></thead>
              <tbody>${protos}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">DeFi Chains</span></div>
            <table class="data-table"><thead><tr><th>Chain</th><th>TVL</th></tr></thead>
              <tbody>${chains}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">BTC Network — mempool.space</span></div>
            <div class="kpi-grid" style="grid-template-columns:1fr 1fr;gap:6px">
              <div class="kpi-card"><div class="kpi-label">Tip Height</div><div class="kpi-value" style="font-size:14px">${btc?.tip_height ?? '—'}</div></div>
              <div class="kpi-card"><div class="kpi-label">Mempool TX</div><div class="kpi-value" style="font-size:14px">${this._fmtCompact(btc?.mempool_count)}</div></div>
              <div class="kpi-card amber"><div class="kpi-label">Fast Fee</div><div class="kpi-value amber" style="font-size:14px">${fees.fastestFee ?? '—'} sat/vB</div></div>
              <div class="kpi-card blue"><div class="kpi-label">Half-Hr Fee</div><div class="kpi-value blue" style="font-size:14px">${fees.halfHourFee ?? '—'} sat/vB</div></div>
            </div>
            <div style="margin-top:8px;font-size:10px;color:var(--text-dim)">
              Difficulty Δ: ${(btc?.difficulty_change_pct ?? 0).toFixed?.(2) ?? '—'}% — Retarget in ${btc?.remaining_blocks ?? '—'} blks
            </div>
            <div class="panel-header" style="margin-top:10px"><span class="panel-title">BTC Cross-Exchange</span>
              <span class="panel-meta">spread ${(xex?.max_spread_bps ?? 0).toFixed?.(1) ?? '—'} bps</span></div>
            <table class="data-table"><thead><tr><th>Exch</th><th>Last</th><th>Vol</th></tr></thead>
              <tbody>${exRows}</tbody></table>
          </div>
        </div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">DeFi/BTC panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Macro: Treasury curve panel ────────────────────────────────
  async appendTreasuryPanel(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    parent.appendChild(wrap);
    try {
      const [tc, vix] = await Promise.all([
        API.get('/api/macro/treasury-curve'),
        API.get('/api/macro/vix'),
      ]);
      const rows = (tc?.rates || []).map(r =>
        `<tr><td class="col-symbol">${this._esc(r.security)}</td>
             <td class="col-number">${(r.rate ?? 0).toFixed(3)}%</td>
             <td style="font-size:10px;color:var(--text-dim)">${this._esc(r.date)}</td></tr>`).join('');
      wrap.innerHTML = `
        <div class="panel-grid grid-2" style="gap:8px">
          <div class="panel">
            <div class="panel-header"><span class="panel-title">U.S. TREASURY YIELD CURVE</span>
              <span class="panel-meta">as of ${this._esc(tc?.as_of || '—')} · fiscaldata.treasury.gov</span></div>
            <table class="data-table"><thead><tr><th>Security</th><th>Rate</th><th>As-of</th></tr></thead>
              <tbody>${rows}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">VIX HISTORY (CBOE)</span></div>
            <div class="kpi-grid" style="grid-template-columns:1fr 1fr 1fr;gap:6px">
              <div class="kpi-card"><div class="kpi-label">Close</div><div class="kpi-value" style="font-size:18px">${(vix?.vix_close ?? 0).toFixed?.(2) ?? '—'}</div></div>
              <div class="kpi-card amber"><div class="kpi-label">High</div><div class="kpi-value amber" style="font-size:18px">${(vix?.vix_high ?? 0).toFixed?.(2) ?? '—'}</div></div>
              <div class="kpi-card blue"><div class="kpi-label">Low</div><div class="kpi-value blue" style="font-size:18px">${(vix?.vix_low ?? 0).toFixed?.(2) ?? '—'}</div></div>
            </div>
            <div style="margin-top:8px;font-size:10px;color:var(--text-dim)">${this._esc(vix?.vix_date || '')}</div>
          </div>
        </div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">Treasury panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Congress: Senate panel ────────────────────────────────────
  async appendSenatePanel(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '16px';
    parent.appendChild(wrap);
    try {
      const data = await API.get('/api/congress/senate?limit=80');
      const rows = (data?.trades || []).map(t => {
        const cls = (t.type || '').toLowerCase().includes('purchase') ? 'col-positive' :
                    (t.type || '').toLowerCase().includes('sale') ? 'col-negative' : '';
        return `<tr>
          <td style="font-size:10px;color:var(--text-dim)">${this._esc(t.date || '—')}</td>
          <td class="col-symbol">${this._esc(t.ticker || '—')}</td>
          <td><span class="signal-badge ${cls}" style="font-size:9px">${this._esc(t.type || '')}</span></td>
          <td style="font-size:10px">${this._esc(t.amount || '')}</td>
          <td style="font-size:10px;color:var(--text-secondary)">${this._esc(t.name)}</td>
          <td style="font-size:10px;color:var(--text-dim)">${this._esc(t.filed || '')}</td>
        </tr>`;
      }).join('');
      wrap.innerHTML = `
        <div class="section-header">SENATE STOCK ACT TRADES (senate-stock-watcher-data)</div>
        <div class="table-wrap">
          <table class="data-table">
            <thead><tr><th>DATE</th><th>TICKER</th><th>TYPE</th><th>AMOUNT</th><th>SENATOR</th><th>FILED</th></tr></thead>
            <tbody>${rows || '<tr><td colspan="6" class="empty-state">No Senate trades returned</td></tr>'}</tbody>
          </table>
        </div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">Senate panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Dashboard: upcoming events (earnings/IPOs/dividends/splits) ─
  async appendCalendarOverlay(parent) {
    if (!parent) return;
    parent.innerHTML = '<div class="loading"><div class="spinner"></div> CALENDAR…</div>';
    try {
      const [earn, ipos, divs, splits] = await Promise.all([
        API.get('/api/calendar/earnings').catch(() => ({earnings: []})),
        API.get('/api/calendar/ipos').catch(() => ({priced: [], upcoming: []})),
        API.get('/api/calendar/dividends').catch(() => ({dividends: []})),
        API.get('/api/calendar/splits').catch(() => ({splits: []})),
      ]);
      const earnRows = (earn?.earnings || []).slice(0, 8).map(e => `<tr>
        <td><span class="col-symbol" onclick="openResearch('${this._esc(e.symbol)}')">${this._esc(e.symbol)}</span></td>
        <td style="font-size:10px;color:var(--text-secondary)">${this._esc((e.name||'').slice(0,28))}</td>
        <td style="font-size:10px">${this._esc(e.epsforecast || '—')}</td>
        <td style="font-size:9px;color:var(--text-dim)">${this._esc((e.time||'').replace('time-',''))}</td>
      </tr>`).join('');
      const ipoList = (ipos?.upcoming || []).concat(ipos?.priced || []).slice(0, 8);
      const ipoRows = ipoList.map(i => `<tr>
        <td><span class="col-symbol">${this._esc(i.symbol||'—')}</span></td>
        <td style="font-size:10px;color:var(--text-secondary)">${this._esc((i.name||'').slice(0,30))}</td>
        <td style="font-size:10px">${this._esc(i.proposedsharepriceeffectiverange || i.priceshare || i.expectedpricedate || '')}</td>
      </tr>`).join('');
      const divRows = (divs?.dividends || []).slice(0, 8).map(d => `<tr>
        <td><span class="col-symbol" onclick="openResearch('${this._esc(d.symbol)}')">${this._esc(d.symbol)}</span></td>
        <td style="font-size:10px;color:var(--text-secondary)">${this._esc((d.name||'').slice(0,28))}</td>
        <td style="font-size:10px;color:var(--green)">${this._esc(d.dividend_rate || d.dividend || '—')}</td>
      </tr>`).join('');
      const splitRows = (splits?.splits || []).slice(0, 8).map(s => `<tr>
        <td><span class="col-symbol" onclick="openResearch('${this._esc(s.symbol)}')">${this._esc(s.symbol)}</span></td>
        <td style="font-size:10px;color:var(--text-secondary)">${this._esc((s.name||'').slice(0,28))}</td>
        <td style="font-size:10px">${this._esc(s.ratio || s.split_ratio || '—')}</td>
      </tr>`).join('');
      const empty = '<tr><td colspan="3" style="font-size:10px;color:var(--text-muted);padding:8px">none today</td></tr>';
      parent.innerHTML = `
        <div class="panel-grid grid-4" style="gap:8px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">
          <div class="panel">
            <div class="panel-header"><span class="panel-title">⚡ EARNINGS TODAY</span><span class="panel-meta">${(earn?.earnings||[]).length}</span></div>
            <table class="data-table"><tbody>${earnRows || empty}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">◇ IPOS</span><span class="panel-meta">${ipoList.length}</span></div>
            <table class="data-table"><tbody>${ipoRows || empty}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">⊛ DIVIDENDS TODAY</span><span class="panel-meta">${(divs?.dividends||[]).length}</span></div>
            <table class="data-table"><tbody>${divRows || empty}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">⊞ SPLITS TODAY</span><span class="panel-meta">${(splits?.splits||[]).length}</span></div>
            <table class="data-table"><tbody>${splitRows || empty}</tbody></table>
          </div>
        </div>`;
    } catch (e) {
      parent.innerHTML = `<div class="empty-state"><span class="col-negative">Calendar overlay error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Alt-data: Reddit mentions + StockTwits trending + sentiment ─
  async appendAltDataPanels(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    parent.appendChild(wrap);
    try {
      const [reddit, stwits] = await Promise.all([
        API.get('/api/alt-data/reddit/mentions'),
        API.get('/api/alt-data/stocktwits-trending'),
      ]);
      const redditRows = (reddit?.mentions || []).slice(0, 20).map(m => `
        <tr>
          <td class="col-symbol">${this._esc(m.ticker)}</td>
          <td class="col-number">${m.mentions}</td>
          <td class="col-number">${this._fmtCompact(m.total_score)}</td>
          <td><a href="${this._esc(safeUrl(m.sample_url))}" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue)">post ↗</a></td>
        </tr>`).join('');
      const stwitsRows = (stwits?.trending || []).map(s => `
        <tr><td class="col-symbol">${this._esc(s.symbol)}</td>
            <td style="font-size:10px;color:var(--text-secondary)">${this._esc(s.title || '')}</td></tr>`).join('');
      wrap.innerHTML = `
        <div class="panel-grid grid-2" style="gap:8px;margin-top:12px">
          <div class="panel">
            <div class="panel-header"><span class="panel-title">REDDIT TICKER MENTIONS — WSB / stocks / investing</span></div>
            <table class="data-table"><thead><tr><th>Ticker</th><th>Mentions</th><th>Σ Karma</th><th>Sample</th></tr></thead>
              <tbody>${redditRows || '<tr><td colspan="4" class="empty-state" style="font-size:10px;line-height:1.5">Reddit\'s public JSON API now returns HTTP&nbsp;403 to apps (OAuth data host is IP-blocked too), so ticker mentions are unavailable.<br>Live social data flows via the per-symbol <b>Social Pulse</b> (StockTwits · Hacker News · Wikipedia) — enter a symbol above.</td></tr>'}</tbody></table>
          </div>
          <div class="panel">
            <div class="panel-header"><span class="panel-title">STOCKTWITS TRENDING</span></div>
            <table class="data-table"><thead><tr><th>Symbol</th><th>Name</th></tr></thead>
              <tbody>${stwitsRows || '<tr><td colspan="2" class="empty-state">Stocktwits unavailable</td></tr>'}</tbody></table>
          </div>
        </div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">Alt-data panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Macro: FRED economic-indicators snapshot ───────────────────
  async appendFredSnapshot(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    parent.appendChild(wrap);
    wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">ECONOMIC INDICATORS — FRED</span></div><div class="panel-body"><div class="loading"><div class="spinner"></div> Loading FRED series ...</div></div></div>`;
    try {
      const data = await API.get('/api/macro/fred/snapshot');
      const series = (data && data.snapshot) || [];
      if (!series.length) {
        wrap.querySelector('.panel-body').innerHTML = '<div class="empty-state">FRED snapshot unavailable</div>';
        return;
      }
      // Group by category for visual structure
      const byCat = {};
      series.forEach(s => {
        const c = s.category || 'other';
        (byCat[c] = byCat[c] || []).push(s);
      });
      const catLabels = {
        rates: 'INTEREST RATES', inflation: 'INFLATION', labor: 'LABOR MARKET',
        monetary: 'MONETARY', growth: 'GROWTH', consumer: 'CONSUMER',
        commodity: 'COMMODITIES',
      };
      const order = ['rates', 'inflation', 'labor', 'consumer', 'growth', 'monetary', 'commodity'];
      const cardHtml = (s) => {
        const v = s.latest_value;
        const d = s.delta_pct;
        const dColor = d == null ? 'var(--text-dim)' : d > 0 ? 'var(--green)' : 'var(--red)';
        const dStr = d == null ? '—' : `${d >= 0 ? '+' : ''}${d.toFixed(2)}%`;
        const valStr = v == null ? '—' : (Math.abs(v) >= 1000 ? v.toLocaleString('en-US', {maximumFractionDigits: 0}) : v.toFixed(2));
        return `
          <div class="macro-card">
            <div class="macro-card-label">${this._esc(s.label || s.id)}</div>
            <div class="macro-card-value">${valStr}</div>
            <div class="macro-card-chg" style="color:${dColor}">${dStr}</div>
            <div class="macro-card-note">${this._esc(s.latest_date || '')} · ${this._esc(s.frequency || '')}</div>
          </div>`;
      };
      let html = '';
      for (const cat of order) {
        if (!byCat[cat]) continue;
        html += `<div class="macro-section-label" style="margin-top:8px">${catLabels[cat] || cat.toUpperCase()}</div>`;
        html += `<div class="macro-grid">${byCat[cat].map(s => cardHtml(s)).join('')}</div>`;
      }
      html += `<div style="font-size:9px;color:var(--text-dim);margin-top:8px">Source: <a href="https://fred.stlouisfed.org" target="_blank" rel="noopener" style="color:var(--blue)">FRED · Federal Reserve Bank of St. Louis</a></div>`;
      wrap.innerHTML = html;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">FRED panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Alt-data: Wikipedia pageviews (retail-attention proxy) ─────
  async renderWikiAttention(parent, symbol, days) {
    if (!parent || !symbol) return;
    const selectedDays = [7, 30, 90].indexOf(parseInt(days, 10)) !== -1 ? parseInt(days, 10) : 30;
    const meanLabel = selectedDays + '-day mean';
    parent.innerHTML = `<div class="loading"><div class="spinner"></div> Fetching Wikipedia pageviews for ${this._esc(symbol)}...</div>`;
    try {
      const data = await API.get(`/api/alt-data/wiki/${encodeURIComponent(symbol)}?days=${selectedDays}`);
      if (data.error && (!data.points || !data.points.length)) {
        parent.innerHTML = `<div class="empty-state"><span class="col-negative">${this._esc(data.error)}</span></div>`;
        return;
      }
      const stats = data.stats || {};
      const spike = stats.spike_pct_vs_baseline;
      const spikeColor = spike == null ? 'var(--text-dim)' : spike > 30 ? 'var(--green)' : spike < -30 ? 'var(--red)' : 'var(--amber)';
      const spikeLabel = spike == null ? '—' : spike > 50 ? 'STRONG SPIKE' : spike > 10 ? 'ABOVE BASELINE' : spike > -10 ? 'NORMAL' : 'BELOW BASELINE';

      // Tiny inline sparkline (SVG)
      const pts = (data.points || []).map(p => p.views);
      const maxV = Math.max(...pts, 1);
      const w = 280, h = 60, n = pts.length;
      const path = pts.map((v, i) => {
        const x = (i / Math.max(n - 1, 1)) * w;
        const y = h - (v / maxV) * h;
        return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
      }).join(' ');
      const sparkline = `<svg viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" style="display:block"><path d="${path}" fill="none" stroke="var(--green)" stroke-width="1.5" /></svg>`;

      // Ensure parent has a stable id BEFORE we bake it into the inline
      // onclick handlers below.
      if (!parent.id) parent.id = 'wiki-root-' + Math.random().toString(36).slice(2, 8);
      parent.dataset.wikiRoot = '1';
      const parentId = parent.id;
      const symAttr = this._esc(symbol);
      const periodBtns = [7, 30, 90].map(d =>
        `<button class="btn btn-ghost btn-sm ${d === selectedDays ? 'active' : ''}" onclick="DataPanels.renderWikiAttention(document.getElementById('${parentId}'), '${symAttr}', ${d})">${d}D</button>`
      ).join('');

      parent.innerHTML = `
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
          <span style="font-size:10px;color:var(--text-dim)">WINDOW</span>
          <div class="flex gap-4">${periodBtns}</div>
        </div>
        <div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
          <div class="kpi-card"><div class="form-label">Latest pageviews</div><div style="font-size:18px;font-weight:700">${(stats.latest || 0).toLocaleString('en-US')}</div></div>
          <div class="kpi-card"><div class="form-label">${meanLabel}</div><div style="font-size:18px;font-weight:700">${(stats.mean || 0).toLocaleString('en-US')}</div></div>
          <div class="kpi-card"><div class="form-label">vs 7-day baseline</div><div style="font-size:18px;font-weight:700;color:${spikeColor}">${spike == null ? '—' : (spike >= 0 ? '+' : '') + spike.toFixed(1) + '%'}</div></div>
          <div class="kpi-card"><div class="form-label">Signal</div><div style="font-size:14px;font-weight:600;color:${spikeColor}">${spikeLabel}</div></div>
        </div>
        <div class="panel">
          <div class="panel-header"><span class="panel-title">DAILY PAGEVIEWS — ${this._esc(data.article || symbol)}</span></div>
          <div class="panel-body">
            ${sparkline}
            <div style="font-size:10px;color:var(--text-dim);margin-top:6px">${n} daily observations · <a href="${this._esc(safeUrl(data.article_url))}" target="_blank" rel="noopener" style="color:var(--blue)">Wikipedia article ↗</a></div>
          </div>
        </div>`;
    } catch (e) {
      parent.innerHTML = `<div class="empty-state"><span class="col-negative">Wikipedia fetch failed: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Alt-data: Hacker News mentions ────────────────────────────
  async renderHackerNews(parent, symbol) {
    if (!parent || !symbol) return;
    parent.innerHTML = `<div class="loading"><div class="spinner"></div> Fetching Hacker News mentions for ${this._esc(symbol)}...</div>`;
    try {
      const data = await API.get(`/api/alt-data/hackernews/${encodeURIComponent(symbol)}?hours=168`);
      const stats = data.stats || {};
      const net = stats.net_polarity || 0;
      const netColor = net > 3 ? 'var(--green)' : net < -3 ? 'var(--red)' : 'var(--amber)';
      const sentLabel = net > 5 ? 'BULLISH' : net > 0 ? 'SLIGHTLY POSITIVE' : net < -5 ? 'BEARISH' : net < 0 ? 'SLIGHTLY NEGATIVE' : 'NEUTRAL';
      const rows = (data.mentions || []).slice(0, 15).map(m => {
        const polColor = m.polarity > 0 ? 'var(--green)' : m.polarity < 0 ? 'var(--red)' : 'var(--text-dim)';
        const polStr = m.polarity > 0 ? `+${m.polarity}` : String(m.polarity);
        const dateStr = m.created_at ? m.created_at.slice(0, 10) : '—';
        return `
          <tr>
            <td style="color:${polColor};text-align:center;font-weight:700">${polStr}</td>
            <td>${this._esc(m.title || '')}</td>
            <td style="color:var(--text-dim);font-size:10px">${m.kind || ''}</td>
            <td style="color:var(--text-dim);font-size:10px">${dateStr}</td>
            <td><a href="${this._esc(safeUrl(m.url))}" target="_blank" rel="noopener" style="color:var(--blue);font-size:10px">open ↗</a></td>
          </tr>`;
      }).join('');
      parent.innerHTML = `
        <div class="kpi-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
          <div class="kpi-card"><div class="form-label">Mentions (7d)</div><div style="font-size:18px;font-weight:700">${data.mention_count || 0}</div></div>
          <div class="kpi-card"><div class="form-label">Net polarity</div><div style="font-size:18px;font-weight:700;color:${netColor}">${net >= 0 ? '+' : ''}${net}</div></div>
          <div class="kpi-card"><div class="form-label">Positive / Negative</div><div style="font-size:14px;font-weight:600"><span style="color:var(--green)">${stats.positive_count || 0}</span> / <span style="color:var(--red)">${stats.negative_count || 0}</span></div></div>
          <div class="kpi-card"><div class="form-label">Tone</div><div style="font-size:14px;font-weight:600;color:${netColor}">${sentLabel}</div></div>
        </div>
        <div class="panel">
          <div class="panel-header"><span class="panel-title">RECENT HN MENTIONS — ${this._esc(symbol)}${data.company_searched ? ' + "' + this._esc(data.company_searched) + '"' : ''}</span></div>
          <div class="panel-body"><table class="data-table"><thead><tr>
            <th style="width:60px">Polarity</th><th>Title</th><th style="width:80px">Type</th><th style="width:90px">Date</th><th style="width:60px"></th>
          </tr></thead><tbody>${rows || '<tr><td colspan="5" class="empty-state">No mentions in the last 7 days</td></tr>'}</tbody></table></div>
        </div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:8px">Polarity = positive-keyword count − negative-keyword count. Coarse vibe-check, not a true sentiment model.</div>`;
    } catch (e) {
      parent.innerHTML = `<div class="empty-state"><span class="col-negative">HN fetch failed: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Macro: CFTC Commitments of Traders — weekly positioning ─────
  async appendCftcSnapshot(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    parent.appendChild(wrap);
    wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">CFTC COMMITMENTS OF TRADERS — WEEKLY POSITIONING</span></div><div class="panel-body"><div class="loading"><div class="spinner"></div> Loading CFTC futures positioning ...</div></div></div>`;
    try {
      const data = await API.get('/api/macro/cftc/snapshot');
      const rows = (data && data.snapshot) || [];
      if (!rows.length) {
        wrap.querySelector('.panel-body').innerHTML = '<div class="empty-state">CFTC snapshot unavailable</div>';
        return;
      }
      // Group by category
      const byCat = {};
      rows.forEach(r => { (byCat[r.category] = byCat[r.category] || []).push(r); });
      const catLabels = { equity:'EQUITY INDICES', rates:'INTEREST RATES', fx:'CURRENCIES', energy:'ENERGY', metals:'METALS', ag:'AGRICULTURE' };
      const order = ['equity','rates','fx','energy','metals','ag'];
      const fmtNum = (n) => n == null ? '—' : (Math.abs(n) >= 1000 ? n.toLocaleString('en-US',{maximumFractionDigits:0}) : String(n));
      const rowHtml = (r) => {
        const d = r.net_delta_wow;
        const dColor = d == null ? 'var(--text-dim)' : d > 0 ? 'var(--green)' : d < 0 ? 'var(--red)' : 'var(--text-dim)';
        const netColor = r.net == null ? 'var(--text-dim)' : r.net > 0 ? 'var(--green)' : 'var(--red)';
        const dStr = d == null ? '—' : `${d >= 0 ? '+' : ''}${fmtNum(d)}`;
        const pctOi = r.net_pct_of_oi;
        const pctStr = pctOi == null ? '—' : `${pctOi >= 0 ? '+' : ''}${pctOi.toFixed(1)}%`;
        return `<tr>
          <td style="font-size:11px">${this._esc(r.label)}</td>
          <td style="font-size:10px;color:var(--text-dim)">${this._esc(r.trader_cohort||'').replace('_',' ')}</td>
          <td style="font-size:11px;text-align:right;color:var(--text-secondary)">${fmtNum(r.long)}</td>
          <td style="font-size:11px;text-align:right;color:var(--text-secondary)">${fmtNum(r.short)}</td>
          <td style="font-size:11px;text-align:right;font-weight:600;color:${netColor}">${fmtNum(r.net)}</td>
          <td style="font-size:11px;text-align:right;color:${dColor}">${dStr}</td>
          <td style="font-size:10px;text-align:right;color:var(--text-dim)">${pctStr}</td>
          <td style="font-size:10px;text-align:right;color:var(--text-dim)">${this._esc(r.report_date||'')}</td>
        </tr>`;
      };
      let html = '<div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">Net = long − short. Δ WoW = week-over-week change. TFF (financials) shows leveraged-fund positioning; Legacy (commodities) shows non-commercial.</div>';
      for (const cat of order) {
        if (!byCat[cat]) continue;
        html += `<div class="macro-section-label" style="margin-top:8px">${catLabels[cat] || cat.toUpperCase()}</div>`;
        html += `<div style="overflow-x:auto"><table class="data-table"><thead><tr>
          <th style="text-align:left">CONTRACT</th><th>COHORT</th>
          <th style="text-align:right">LONG</th><th style="text-align:right">SHORT</th>
          <th style="text-align:right">NET</th><th style="text-align:right">Δ WoW</th>
          <th style="text-align:right">% OI</th><th style="text-align:right">REPORT</th>
        </tr></thead><tbody>${byCat[cat].map(r => rowHtml(r)).join('')}</tbody></table></div>`;
      }
      html += `<div style="font-size:9px;color:var(--text-dim);margin-top:8px">Source: <a href="https://publicreporting.cftc.gov" target="_blank" rel="noopener" style="color:var(--blue)">publicreporting.cftc.gov</a> · fetched ${this._esc((data.fetched_at||'').slice(0,19))}Z</div>`;
      wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">CFTC COMMITMENTS OF TRADERS — WEEKLY POSITIONING</span></div><div class="panel-body">${html}</div></div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">CFTC panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Research: Wikidata corporate facts ─────────────────────────
  async appendWikidataFacts(parent, symbol) {
    if (!parent || !symbol) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    parent.appendChild(wrap);
    wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">CORPORATE PROFILE — WIKIDATA</span></div><div class="panel-body"><div class="loading"><div class="spinner"></div> Resolving Wikidata entity for ${this._esc(symbol)}...</div></div></div>`;
    try {
      const d = await API.get(`/api/research/wikidata/${encodeURIComponent(symbol)}`);
      if (!d || !d.found) {
        wrap.querySelector('.panel-body').innerHTML = `<div class="empty-state">${this._esc(d && d.note || 'No Wikidata entity for this ticker')}</div>`;
        return;
      }
      const dash = '—';
      const fmtEmp = d.employees == null ? dash : d.employees.toLocaleString('en-US');
      const fmtSite = d.website ? `<a href="${this._esc(safeUrl(d.website))}" target="_blank" rel="noopener" style="color:var(--blue)">${this._esc(d.website.replace(/^https?:\/\//,'').replace(/\/$/,''))} ↗</a>` : dash;
      const wikiLink = d.wikidata_url ? `<a href="${this._esc(safeUrl(d.wikidata_url))}" target="_blank" rel="noopener" style="color:var(--blue)">${this._esc(d.qid)} ↗</a>` : dash;
      const cell = (label, val) => `
        <div style="background:var(--bg-secondary);padding:10px;border:1px solid var(--border)">
          <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:4px">${label}</div>
          <div style="font-size:12px;color:var(--text-primary)">${val}</div>
        </div>`;
      wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">CORPORATE PROFILE — WIKIDATA · ${this._esc(d.name || symbol)}</span></div><div class="panel-body">
        <div class="panel-grid grid-4" style="gap:6px">
          ${cell('INDUSTRY', this._esc(d.industry || dash))}
          ${cell('HEADQUARTERS', this._esc(d.headquarters || dash))}
          ${cell('COUNTRY', this._esc(d.country || dash))}
          ${cell('INCEPTION', this._esc(d.inception || dash))}
          ${cell('EMPLOYEES', fmtEmp)}
          ${cell('CEO', this._esc(d.ceo || dash))}
          ${cell('PARENT', this._esc(d.parent || dash))}
          ${cell('WEBSITE', fmtSite)}
        </div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:8px">Source: ${wikiLink} · community-curated, coverage varies</div>
      </div></div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">Wikidata panel error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Markets: Finviz sector heatmap ─────────────────────────────
  async appendFinvizSectors(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '8px';
    parent.appendChild(wrap);
    wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">FINVIZ SECTOR HEATMAP — 1W PERFORMANCE</span></div><div class="panel-body"><div class="loading"><div class="spinner"></div> Loading Finviz sector data ...</div></div></div>`;
    try {
      const data = await API.get('/api/market/finviz/sectors');
      const rows = (data && data.sectors) || [];
      if (!rows.length) {
        wrap.querySelector('.panel-body').innerHTML = '<div class="empty-state">Finviz sectors unavailable</div>';
        return;
      }
      // Backend returns `change_1w` as a decimal fraction (0.0144 = 1.44%)
      const cells = rows.map(s => {
        const frac = (typeof s.change_1w === 'number') ? s.change_1w : null;
        const pct = frac == null ? null : frac * 100;
        const intensity = pct == null ? 0 : Math.min(Math.abs(pct) * 25, 80);
        const bg = pct == null ? 'var(--bg-secondary)' :
          pct >= 0 ? `rgba(0,255,159,${intensity/300})` : `rgba(255,51,85,${intensity/300})`;
        const border = pct == null ? 'var(--border)' :
          pct >= 0 ? `rgba(0,255,159,${intensity/200})` : `rgba(255,51,85,${intensity/200})`;
        const pctStr = pct == null ? '—' : `${pct >= 0 ? '+' : ''}${pct.toFixed(2)}%`;
        const pctColor = pct == null ? 'var(--text-dim)' : pct >= 0 ? 'var(--green)' : 'var(--red)';
        const peStr = s.pe == null ? '—' : s.pe.toFixed(1);
        return `<div style="background:${bg};border:1px solid ${border};padding:10px;border-radius:2px">
          <div style="font-size:11px;font-weight:600;color:var(--text-primary)">${this._esc(s.name || '—')}</div>
          <div style="font-size:16px;font-weight:700;margin-top:4px;color:${pctColor}">${pctStr}</div>
          <div style="font-size:9px;color:var(--text-dim);margin-top:4px">${s.stocks ?? '—'} stocks · P/E ${peStr}</div>
        </div>`;
      }).join('');
      wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">FINVIZ SECTOR HEATMAP — 1W PERFORMANCE</span></div><div class="panel-body">
        <div class="panel-grid grid-4" style="gap:4px">${cells}</div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:8px">Source: <a href="https://finviz.com" target="_blank" rel="noopener" style="color:var(--blue)">finviz.com</a> via finvizfinance · fetched ${this._esc((data.fetched_at||'').slice(0,19))}Z</div>
      </div></div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">Finviz sectors error: ${this._esc(e.message)}</span></div>`;
    }
  },

  // ── Intel: Finviz cross-ticker insider trades ──────────────────
  async appendFinvizInsiders(parent) {
    if (!parent) return;
    const wrap = document.createElement('div');
    wrap.style.marginTop = '12px';
    parent.appendChild(wrap);
    wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">FINVIZ CROSS-TICKER INSIDER FEED — LATEST FORM 4 ACROSS ALL US EQUITIES</span></div><div class="panel-body"><div class="loading"><div class="spinner"></div> Loading Finviz insider feed ...</div></div></div>`;
    try {
      const data = await API.get('/api/intel/finviz/insiders');
      const trades = (data && data.trades) || [];
      if (!trades.length) {
        wrap.querySelector('.panel-body').innerHTML = '<div class="empty-state">Finviz insider feed unavailable</div>';
        return;
      }
      const fmtCompact = (n) => {
        if (n == null || isNaN(n)) return '—';
        const abs = Math.abs(n);
        if (abs >= 1e9) return (n/1e9).toFixed(2) + 'B';
        if (abs >= 1e6) return (n/1e6).toFixed(2) + 'M';
        if (abs >= 1e3) return (n/1e3).toFixed(1) + 'K';
        return n.toLocaleString('en-US', {maximumFractionDigits: 0});
      };
      const rows = trades.slice(0, 40).map(t => {
        const action = String(t.transaction || '').toUpperCase();
        const isBuy = action.includes('BUY');
        const isSell = action.includes('SALE') || action.includes('SELL') || action.includes('DISPOSITION');
        const actColor = isBuy ? 'var(--green)' : isSell ? 'var(--red)' : 'var(--text-dim)';
        const sym = t.ticker || '';
        return `<tr>
          <td style="font-size:10px;color:var(--text-dim);white-space:nowrap">${this._esc(t.date || '')}</td>
          <td><span class="col-symbol" style="cursor:pointer" onclick="openResearch('${this._esc(sym)}')">${this._esc(sym)}</span></td>
          <td style="font-size:11px">${this._esc(t.owner || '—')}</td>
          <td style="font-size:10px;color:var(--text-dim)">${this._esc(t.relationship || '—')}</td>
          <td style="font-size:11px;font-weight:600;color:${actColor}">${this._esc(t.transaction || '—')}</td>
          <td style="font-size:11px;text-align:right">${t.cost == null ? '—' : '$' + t.cost.toFixed(2)}</td>
          <td style="font-size:11px;text-align:right">${fmtCompact(t.shares)}</td>
          <td style="font-size:11px;text-align:right;color:${actColor}">${t.value == null ? '—' : '$' + fmtCompact(t.value)}</td>
          <td><a href="https://finviz.com/quote.ashx?t=${this._esc(sym)}" target="_blank" rel="noopener" style="font-size:10px;color:var(--blue)">FINVIZ ↗</a></td>
        </tr>`;
      }).join('');
      wrap.innerHTML = `<div class="panel"><div class="panel-header"><span class="panel-title">FINVIZ CROSS-TICKER INSIDER FEED — LATEST FORM 4 ACROSS ALL US EQUITIES</span></div><div class="panel-body">
        <div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">Most recent insider transactions across the entire US equity universe (not filtered to portfolio). ${trades.length} loaded · showing first 40.</div>
        <div style="overflow-x:auto"><table class="data-table"><thead><tr>
          <th>DATE</th><th>TICKER</th><th>INSIDER</th><th>RELATIONSHIP</th><th>ACTION</th>
          <th style="text-align:right">PRICE</th><th style="text-align:right">SHARES</th><th style="text-align:right">VALUE</th><th></th>
        </tr></thead><tbody>${rows}</tbody></table></div>
        <div style="font-size:9px;color:var(--text-dim);margin-top:8px">Source: <a href="https://finviz.com/insidertrading.ashx" target="_blank" rel="noopener" style="color:var(--blue)">finviz.com/insidertrading</a> via finvizfinance</div>
      </div></div>`;
    } catch (e) {
      wrap.innerHTML = `<div class="empty-state"><span class="col-negative">Finviz insiders error: ${this._esc(e.message)}</span></div>`;
    }
  },
};

// ════════════════════════════════════════════════════════════════════════════
// IDEAS — Random Investment Idea Generator
// ════════════════════════════════════════════════════════════════════════════

const Ideas = {
  filters: { asset_class: 'any', strategy: '', min_score: 40, discovery_mode: 'curated' },
  current: null,
  history: [],   // Last 20 dossiers (newest first)
  rolling: false,
  universe: null,
  loadingUniverse: false,

  shellRendered: false,

  signalClass(signal) {
    const s = (signal || '').toUpperCase();
    if (s.includes('BUY') || s === 'BULLISH') return 'signal-bullish';
    if (s === 'AVOID' || s === 'BEARISH' || s === 'CAUTION') return 'signal-bearish';
    return 'signal-neutral';
  },

  scoreColor(score) {
    if (score == null) return 'var(--text-dim)';
    if (score >= 70) return 'var(--green)';
    if (score >= 50) return 'var(--amber)';
    if (score >= 35) return 'var(--text-secondary)';
    return 'var(--red)';
  },

  trendArrow(trend) {
    if (!trend) return '—';
    const t = String(trend).toUpperCase();
    if (t === 'UP') return '<span class="text-green">▲ UP</span>';
    if (t === 'DOWN') return '<span class="text-red">▼ DOWN</span>';
    return '<span class="text-dim">◆ FLAT</span>';
  },

  pctSpan(v, digits = 2) {
    if (v == null || isNaN(parseFloat(v))) return '—';
    const n = parseFloat(v);
    const cls = n > 0 ? 'text-green' : n < 0 ? 'text-red' : 'text-dim';
    const s = n > 0 ? '+' : '';
    return `<span class="${cls}">${s}${n.toFixed(digits)}%</span>`;
  },

  num(v, digits = 2) {
    if (v == null || isNaN(parseFloat(v))) return '—';
    return parseFloat(v).toFixed(digits);
  },

  async loadUniverse(mode) {
    const m = mode || this.filters.discovery_mode || 'curated';
    try { this.universe = await API.get('/api/ideas/universe?discovery_mode=' + m); }
    catch (e) { this.universe = { equity_count: 0, crypto_count: 0, watchlist_count: 0, discovery_mode: m }; }
    return this.universe;
  },

  updateUniverseBadge() {
    const el = document.getElementById('ideas-universe-badge');
    if (!el) return;
    const u = this.universe || {};
    const mode = (u.discovery_mode || this.filters.discovery_mode || 'curated').toUpperCase();
    const loadingNote = this.loadingUniverse ? ' · counting…' : '';
    el.innerHTML = `POOL: ${mode} · ${u.equity_count || 0} EQUITY · ${u.crypto_count || 0} CRYPTO · ${u.watchlist_count || 0} WATCHLIST${loadingNote}`;
  },

  renderShell() {
    const view = document.getElementById('view-ideas');
    const u = this.universe || {};
    view.innerHTML = `
      <div class="flex-between mb-8">
        <div>
          <h2 style="font-size:14px;letter-spacing:.12em;color:var(--green);margin:0">⚄ IDEA ROULETTE</h2>
          <div id="ideas-universe-badge" style="font-size:10px;color:var(--text-dim);margin-top:3px">
            POOL: ${(u.discovery_mode || 'CURATED').toUpperCase()} · ${u.equity_count || 0} EQUITY · ${u.crypto_count || 0} CRYPTO · ${u.watchlist_count || 0} WATCHLIST
          </div>
        </div>
        <div style="display:flex;gap:6px;align-items:center">
          <button class="btn btn-ghost btn-sm" id="ideas-clear-history" onclick="Ideas.clearHistory()">CLEAR HISTORY</button>
        </div>
      </div>

      <div class="panel mb-8">
        <div class="panel-header"><span class="panel-title">FILTERS</span></div>
        <div class="panel-body" style="display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end">
          <div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:4px">POOL</div>
            <select id="ideas-discovery-mode" class="input-sm" onchange="Ideas.onDiscoveryModeChange()">
              <option value="curated">CURATED (~180)</option>
              <option value="full">FULL UNIVERSE (~10k+)</option>
            </select>
          </div>
          <div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:4px">ASSET CLASS</div>
            <select id="ideas-asset-class" class="input-sm" onchange="Ideas.onFilterChange()">
              <option value="any">ANY</option>
              <option value="stock">STOCK / ETF</option>
              <option value="crypto">CRYPTO</option>
            </select>
          </div>
          <div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:4px">STRATEGY LENS</div>
            <select id="ideas-strategy" class="input-sm" onchange="Ideas.onFilterChange()">
              <option value="">RANDOM</option>
              <option value="growth">GROWTH</option>
              <option value="value">VALUE</option>
              <option value="momentum">MOMENTUM</option>
              <option value="income">INCOME</option>
            </select>
          </div>
          <div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em;margin-bottom:4px">MIN SCORE</div>
            <select id="ideas-min-score" class="input-sm" onchange="Ideas.onFilterChange()">
              <option value="0">ANY</option>
              <option value="40" selected>≥ 40 (interesting)</option>
              <option value="55">≥ 55 (strong)</option>
              <option value="70">≥ 70 (high conviction)</option>
            </select>
          </div>
          <div style="flex:1"></div>
          <button class="btn btn-green" id="ideas-roll-btn" onclick="Ideas.roll()" style="font-size:13px;padding:10px 24px;letter-spacing:.1em">
            ⚄ GENERATE IDEA
          </button>
        </div>
      </div>

      <div style="display:grid;grid-template-columns:minmax(0,1fr) 240px;gap:10px;align-items:start">
        <div id="ideas-current"></div>
        <div id="ideas-history" class="panel">
          <div class="panel-header"><span class="panel-title">RECENT ROLLS</span></div>
          <div class="panel-body" id="ideas-history-body" style="padding:6px;max-height:600px;overflow:auto">
            <div class="empty-state" style="padding:20px;font-size:10px;color:var(--text-dim)">No rolls yet — hit GENERATE.</div>
          </div>
        </div>
      </div>
    `;
    this.shellRendered = true;
    this.renderCurrent();
    this.renderHistory();
  },

  renderCurrent() {
    const wrap = document.getElementById('ideas-current');
    if (!wrap) return;
    const d = this.current;

    if (this.rolling && !d) {
      wrap.innerHTML = `
        <div class="panel">
          <div class="panel-body" style="padding:60px;text-align:center">
            <div class="spinner" style="margin:0 auto 16px"></div>
            <div style="color:var(--green);font-size:12px;letter-spacing:.15em">CONSULTING THE ORACLE…</div>
            <div style="color:var(--text-dim);font-size:10px;margin-top:6px">picking · scoring · forecasting · narrating · synthesizing</div>
          </div>
        </div>`;
      return;
    }

    if (!d) {
      wrap.innerHTML = `
        <div class="panel">
          <div class="panel-body" style="padding:60px;text-align:center">
            <div style="font-size:48px;margin-bottom:14px">⚄</div>
            <div style="color:var(--green);font-size:14px;letter-spacing:.12em">ROLL THE DICE</div>
            <div style="color:var(--text-dim);font-size:10px;margin-top:8px;max-width:360px;margin-left:auto;margin-right:auto">
              Surface a random idea from the universe. Each roll runs the scanner, ML forecaster, narrative engine, and synthesizes a thesis.
            </div>
          </div>
        </div>`;
      return;
    }

    if (d.error) {
      wrap.innerHTML = `<div class="panel"><div class="panel-body"><span class="col-negative">Error: ${_esc(d.error)}</span></div></div>`;
      return;
    }

    wrap.innerHTML = this.renderDossier(d);

    // Kick off the embedded chart render — only for stocks (crypto chart panel
    // is not in the layout). Returns immediately; chart fills in async.
    if (d.asset_class !== 'crypto' && d.symbol) {
      this._renderChart(d.symbol);
    }
  },

  renderDossier(d) {
    const snap = d.snapshot || {};
    const opp  = d.opportunity || {};
    const risk = d.risk || {};
    const isCrypto = d.asset_class === 'crypto';

    // Enrichment may be null (still loading) or an object (loaded)
    const str  = d.strength;
    const fc   = d.forecast;
    const thesis = d.thesis;
    const peers = d.peers;
    const analog = d.historical_analog;
    const cryptoExtras = d.crypto_extras;

    const sigClass = this.signalClass(str ? str.signal : null);
    const scoreCol = this.scoreColor(str ? str.composite_score : null);

    const sourceBadge = d.source === 'watchlist'
      ? '<span class="signal-badge" style="background:var(--blue-dim);color:#fff">FROM WATCHLIST</span>'
      : d.source === 'warmed-pool' || d.from_warmed_pool
        ? '<span class="signal-badge" style="background:var(--green-dark);color:var(--green)">⚡ PRE-WARMED</span>'
        : `<span class="signal-badge signal-neutral">${(d.source || 'UNIVERSE').toUpperCase()}</span>`;

    const cacheNote = d.from_cache
      ? '<span style="font-size:9px;color:var(--text-dim);margin-left:6px">⌛ cached</span>' : '';

    const enrichingBanner = d.enrichment_pending
      ? `<div class="panel" style="background:rgba(0,200,255,0.05);border:1px dashed var(--blue);padding:8px 12px;margin-bottom:8px;display:flex;align-items:center;gap:10px">
           <div class="spinner-sm"></div>
           <span style="font-size:11px;color:var(--blue);letter-spacing:.1em">ENRICHING — scanner · ML forecast · insider · options · peers · historical analog · thesis…</span>
         </div>` : '';

    const belowNote = d.below_threshold
      ? `<div style="background:#3a2a00;border:1px solid var(--amber);padding:6px 10px;margin-bottom:8px;font-size:10px;color:var(--amber)">
          ⚠ NO PICK MET YOUR MIN-SCORE BAR AFTER multiple ROLLS — showing the closest match. Lower the bar or change filters.
         </div>` : '';

    const totalMs = d.computed_in_ms || ((d.fast_computed_in_ms || 0) + (d.enrichment_computed_in_ms || 0));

    return `
      ${belowNote}
      ${enrichingBanner}
      <div class="panel mb-8">
        <div class="panel-body" style="padding:14px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap">
            <div style="min-width:0;flex:1">
              <div style="display:flex;align-items:baseline;gap:10px;flex-wrap:wrap">
                <span style="font-size:28px;font-weight:bold;color:var(--green);letter-spacing:.05em">${_esc(d.symbol)}</span>
                <span style="font-size:11px;color:var(--text-secondary)">${_esc(snap.name || '')}</span>
                ${sourceBadge}
                <span class="signal-badge signal-neutral">${_esc((d.strategy || '').toUpperCase())} LENS</span>
                ${cacheNote}
              </div>
              <div style="margin-top:6px;font-size:11px;color:var(--text-dim)">
                ${_esc(snap.sector || (isCrypto ? 'Crypto' : ''))}${snap.industry ? ' · ' + _esc(snap.industry) : ''}
                ${snap.market_cap != null ? ' · MCAP $' + fmt.compact(snap.market_cap) : ''}
              </div>
            </div>
            <div style="text-align:right">
              <div style="font-size:24px;color:var(--text-primary);font-weight:bold">$${fmt.price(snap.price)}</div>
              <div style="font-size:11px">
                ${isCrypto
                  ? `24h ${this.pctSpan(snap.change_24h)} · 7d ${this.pctSpan(snap.change_7d)}`
                  : `${this.pctSpan(snap.change_pct)} today`}
              </div>
            </div>
          </div>

          <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
            <button class="btn btn-green" onclick="Ideas.roll()">⚄ ROLL AGAIN</button>
            <button class="btn btn-blue btn-sm" onclick="quickAddToWatchlist('${_esc(d.symbol)}', '${_esc(_jesc(snap.name || ''))}')">+ WATCHLIST</button>
            <button class="btn btn-ghost btn-sm" onclick="openResearch('${_esc(d.symbol)}')">OPEN RESEARCH ↗</button>
            <div style="flex:1"></div>
            <span style="font-size:9px;color:var(--text-dim);align-self:center">
              ${d.fast_computed_in_ms ? `fast ${d.fast_computed_in_ms}ms` : ''}${d.enrichment_computed_in_ms ? ` · enrich ${d.enrichment_computed_in_ms}ms` : ''}${!d.fast_computed_in_ms && totalMs ? totalMs + 'ms' : ''} · ${fmt.timeAgo(d.generated_at)}
            </span>
          </div>
        </div>
      </div>

      <!-- AI THESIS — skeleton if not yet enriched -->
      ${thesis ? this.renderThesisPanel(thesis, str) : this.renderSkeletonPanel('⚡ THESIS', 'Synthesizing thesis from all factors…')}

      <!-- PRICE CHART — rendered async after innerHTML lands -->
      ${!isCrypto ? `
        <div class="panel mb-8">
          <div class="panel-header">
            <span class="panel-title">📈 6-MONTH CHART</span>
            <span style="font-size:9px;color:var(--text-dim);float:right">
              SMA 50 (orange) · SMA 200 (blue) · 52w range overlay
            </span>
          </div>
          <div class="panel-body" style="padding:8px">
            <div id="ideas-price-chart" style="height:280px;width:100%"></div>
          </div>
        </div>` : ''}

      <!-- 4-QUADRANT DOSSIER -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
        ${str ? this.renderStrengthPanel(str, scoreCol, sigClass) : this.renderSkeletonPanel('◆ STRENGTH', 'Computing composite score across 8 sub-signals…')}
        ${fc !== undefined && fc !== null ? this.renderForecastPanel(fc) : this.renderSkeletonPanel('◆ FORECAST (30D)', 'Running ML forecaster + regime detection…')}
        ${this.renderOpportunityPanel(opp, d.headlines || [])}
        ${this.renderRiskSnapshotPanel(snap, risk, isCrypto)}
      </div>

      <!-- EVENTS STRIP -->
      ${this.renderEventsStrip(d.events || {})}

      <!-- FLOWS & SENTIMENT — 4 mini-cards across -->
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px">
        ${this.renderSocialPanel(d.social || {})}
        ${d.insider !== undefined && d.insider !== null ? this.renderInsiderPanel(d.insider, isCrypto) : this.renderSkeletonPanel('◎ INSIDER (60D)', 'Parsing Form 4 filings…', true)}
        ${d.congress !== undefined && d.congress !== null ? this.renderCongressPanel(d.congress, isCrypto) : this.renderSkeletonPanel('◎ CONGRESS (180D)', 'Querying disclosures…', true)}
        ${d.options_flow !== undefined && d.options_flow !== null ? this.renderOptionsFlowPanel(d.options_flow, isCrypto) : this.renderSkeletonPanel('◎ OPTIONS FLOW', 'Scanning chain for unusual activity…', true)}
      </div>

      <!-- PEERS + HISTORICAL ANALOG row -->
      <div style="display:grid;grid-template-columns:1.6fr 1fr;gap:8px;margin-top:8px">
        ${!isCrypto
          ? (peers !== undefined && peers !== null ? this.renderPeersPanel(peers, d.symbol) : this.renderSkeletonPanel('◇ PEER COMPARISON', 'Pulling sector peers…'))
          : (cryptoExtras !== undefined && cryptoExtras !== null ? this.renderCryptoExtrasPanel(cryptoExtras) : this.renderSkeletonPanel('◇ ON-CHAIN / COMMUNITY', 'Pulling community + dev metrics…'))}
        ${analog !== undefined && analog !== null ? this.renderHistoricalAnalogPanel(analog) : this.renderSkeletonPanel('◇ HISTORICAL ANALOG', 'Finding historical pattern matches…')}
      </div>

      <!-- DECISION SUPPORT row: sizing / correlation / trade plan -->
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-top:8px">
        ${d.sizing !== undefined && d.sizing !== null ? this.renderSizingPanel(d.sizing) : this.renderSkeletonPanel('▣ POSITION SIZING', 'Computing risk-based tiers…')}
        ${d.correlation !== undefined && d.correlation !== null ? this.renderCorrelationPanel(d.correlation, d.symbol) : this.renderSkeletonPanel('▣ PORTFOLIO FIT', 'Computing correlation to your holdings…')}
        ${d.trade_plan !== undefined && d.trade_plan !== null ? this.renderTradePlanPanel(d.trade_plan, snap) : this.renderSkeletonPanel('▣ TRADE PLAN', 'Deriving entry / stop / targets…')}
      </div>
    `;
  },

  renderSizingPanel(s) {
    if (!s || !s.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">▣ POSITION SIZING</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">${_esc((s && s.reason) || 'Add holdings to enable sizing')}</span></div>
        </div>`;
    }
    const tiers = (s.tiers || []).map(t => {
      const tierColor = t.label === 'CONSERVATIVE' ? 'var(--blue)'
                      : t.label === 'AGGRESSIVE' ? 'var(--amber)'
                      : 'var(--green)';
      return `
        <div style="border:1px solid var(--border);padding:8px 10px;margin-bottom:6px;background:var(--bg-elevated)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <span style="font-size:11px;color:${tierColor};font-weight:bold;letter-spacing:.05em">${_esc(t.label)}</span>
            <span style="font-size:10px;color:var(--text-dim)">risk ${t.risk_pct}%</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:10px;line-height:1.5">
            <div><span class="text-dim">SHARES:</span> <span class="text-primary">${t.shares}</span></div>
            <div><span class="text-dim">VALUE:</span> <span class="text-primary">$${fmt.compact(t.position_value)}</span></div>
            <div><span class="text-dim">% BOOK:</span> <span style="color:${t.capped_at_10pct ? 'var(--amber)' : 'var(--text-primary)'}">${t.pct_of_book.toFixed(2)}%${t.capped_at_10pct ? ' ⚠' : ''}</span></div>
            <div><span class="text-dim">MAX LOSS:</span> <span class="text-red">$${fmt.compact(t.max_loss_dollars)}</span></div>
          </div>
        </div>`;
    }).join('');

    return `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">▣ POSITION SIZING</span>
          <span style="font-size:9px;color:var(--text-dim);float:right">stop ${s.stop_pct}% · book $${fmt.compact(s.portfolio_value)}</span>
        </div>
        <div class="panel-body" style="padding:10px">
          ${tiers}
          <div style="font-size:9px;color:var(--text-dim);line-height:1.5;margin-top:4px">${_esc(s.notes || '')}</div>
        </div>
      </div>`;
  },

  renderCorrelationPanel(c, pickSymbol) {
    if (!c || !c.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">▣ PORTFOLIO FIT</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">${_esc((c && c.reason) || 'No portfolio')}</span></div>
        </div>`;
    }
    const verdictCol = c.verdict === 'DIVERSIFIES' ? 'var(--green)'
                     : c.verdict === 'CONCENTRATES' ? 'var(--red)'
                     : 'var(--amber)';

    const corrColor = (v) => {
      if (v >= 0.7) return 'var(--red)';
      if (v >= 0.4) return 'var(--amber)';
      if (v >= 0) return 'var(--text-primary)';
      return 'var(--green)';
    };

    const rows = (c.rows || []).map(r => `
      <tr>
        <td style="padding:3px 6px;color:var(--text-primary);font-weight:bold">${_esc(r.symbol)}</td>
        <td style="padding:3px 6px;text-align:right;color:${corrColor(r.corr)};font-weight:bold">${r.corr >= 0 ? '+' : ''}${r.corr.toFixed(2)}</td>
        <td style="padding:3px 6px;text-align:right;color:var(--text-dim);font-size:9px">${_esc(r.label)}</td>
      </tr>
    `).join('');

    return `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">▣ PORTFOLIO FIT</span>
          <span style="font-size:9px;color:var(--text-dim);float:right">${c.holdings_count} holdings · ${_esc(c.period || '3mo')}</span>
        </div>
        <div class="panel-body" style="padding:10px">
          <div style="margin-bottom:8px">
            <span style="font-size:13px;color:${verdictCol};font-weight:bold;letter-spacing:.05em">${_esc(c.verdict || '—')}</span>
            <div style="font-size:10px;color:var(--text-dim);margin-top:2px;line-height:1.4">${_esc(c.verdict_detail || '')}</div>
          </div>
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">
            <span class="text-dim">AVG CORR:</span> <span style="color:${corrColor(c.average_correlation)}">${c.average_correlation >= 0 ? '+' : ''}${c.average_correlation.toFixed(2)}</span>
            ${c.high_correlation_count ? ` · <span class="text-red">${c.high_correlation_count} high-corr</span>` : ''}
          </div>
          <table style="width:100%;font-size:10px;border-collapse:collapse">
            ${rows}
          </table>
        </div>
      </div>`;
  },

  renderTradePlanPanel(p, snap) {
    if (!p || !p.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">▣ TRADE PLAN</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">${_esc((p && p.reason) || 'Plan unavailable')}</span></div>
        </div>`;
    }
    const qualityColor = p.rr_quality === 'EXCELLENT' ? 'var(--green)'
                       : p.rr_quality === 'GOOD' ? 'var(--green-dim)'
                       : p.rr_quality === 'POOR' ? 'var(--red)'
                       : 'var(--amber)';

    return `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">▣ TRADE PLAN</span>
          <span style="font-size:9px;float:right;color:${qualityColor};font-weight:bold">${_esc(p.rr_quality || '—')}</span>
        </div>
        <div class="panel-body" style="padding:10px">
          <div style="border-left:3px solid var(--blue);padding:6px 10px;margin-bottom:6px;background:rgba(0,200,255,0.04)">
            <div style="font-size:9px;color:var(--blue);letter-spacing:.1em;margin-bottom:2px">▸ ENTRY ZONE</div>
            <div style="font-size:11px;color:var(--text-primary)">$${fmt.price(p.entry.low)} – $${fmt.price(p.entry.high)} <span class="text-dim">· spot $${fmt.price(p.entry.current)}</span></div>
          </div>
          <div style="border-left:3px solid var(--red);padding:6px 10px;margin-bottom:6px;background:rgba(255,51,85,0.04)">
            <div style="font-size:9px;color:var(--red);letter-spacing:.1em;margin-bottom:2px">▼ STOP LOSS</div>
            <div style="font-size:11px;color:var(--text-primary)">$${fmt.price(p.stop.price)} <span class="text-red">(-${p.stop.distance_pct}%)</span></div>
            <div style="font-size:9px;color:var(--text-dim)">${_esc(p.stop.label)} · risk $${fmt.price(p.stop.risk_per_share)}/share</div>
          </div>
          <div style="border-left:3px solid var(--green);padding:6px 10px;margin-bottom:6px;background:rgba(0,255,159,0.04)">
            <div style="font-size:9px;color:var(--green);letter-spacing:.1em;margin-bottom:2px">▲ TARGET 1 · R:R ${p.target_1.rr || '—'}</div>
            <div style="font-size:11px;color:var(--text-primary)">$${fmt.price(p.target_1.price)} <span class="text-green">(+${p.target_1.reward_pct}%)</span></div>
            <div style="font-size:9px;color:var(--text-dim)">${_esc(p.target_1.label)}</div>
          </div>
          <div style="border-left:3px solid var(--green);padding:6px 10px;margin-bottom:6px;background:rgba(0,255,159,0.04)">
            <div style="font-size:9px;color:var(--green);letter-spacing:.1em;margin-bottom:2px">▲ TARGET 2 · R:R ${p.target_2.rr || '—'}</div>
            <div style="font-size:11px;color:var(--text-primary)">$${fmt.price(p.target_2.price)} <span class="text-green">(+${p.target_2.reward_pct}%)</span></div>
            <div style="font-size:9px;color:var(--text-dim)">${_esc(p.target_2.label)}</div>
          </div>
        </div>
      </div>`;
  },

  renderThesisPanel(thesis, str) {
    return `
      <div class="panel mb-8">
        <div class="panel-header">
          <span class="panel-title">⚡ THESIS</span>
          <span style="font-size:9px;color:var(--text-dim);float:right">
            ${thesis.ai_powered ? '🤖 ' + _esc(thesis.model_used || 'AI') : 'RULE-BASED · add OpenAI key in Settings for AI thesis'}
          </span>
        </div>
        <div class="panel-body" style="padding:14px">
          <div style="font-size:14px;color:var(--green);font-weight:bold;margin-bottom:8px;letter-spacing:.04em">
            ${_esc(thesis.headline || (str && str.signal) || '')}
          </div>
          <div style="font-size:12px;color:var(--text-primary);line-height:1.6;margin-bottom:12px">
            ${_esc(thesis.thesis || '')}
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
            <div style="background:rgba(0,255,159,0.06);border-left:2px solid var(--green);padding:8px 10px">
              <div style="font-size:9px;color:var(--green);letter-spacing:.1em;margin-bottom:3px">▲ BULL CASE</div>
              <div style="font-size:11px;color:var(--text-primary);line-height:1.4">${_esc(thesis.bull_case || '—')}</div>
            </div>
            <div style="background:rgba(255,51,85,0.06);border-left:2px solid var(--red);padding:8px 10px">
              <div style="font-size:9px;color:var(--red);letter-spacing:.1em;margin-bottom:3px">▼ BEAR CASE</div>
              <div style="font-size:11px;color:var(--text-primary);line-height:1.4">${_esc(thesis.bear_case || '—')}</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;font-size:10px">
            <div><span style="color:var(--text-dim)">CONVICTION</span><br><span style="color:var(--green);font-weight:bold">${_esc(thesis.conviction || '—')}</span></div>
            <div><span style="color:var(--text-dim)">HORIZON</span><br><span style="color:var(--text-primary)">${_esc(thesis.time_horizon || '—')}</span></div>
            <div style="grid-column:span 2"><span style="color:var(--text-dim)">CATALYST</span><br><span style="color:var(--text-primary)">${_esc(thesis.key_catalyst || '—')}</span></div>
          </div>
          ${thesis.suggested_action ? `
            <div style="margin-top:10px;padding:8px 10px;background:var(--bg-elevated);border:1px dashed var(--border);font-size:11px">
              <span style="color:var(--amber);letter-spacing:.1em;font-size:9px">▶ SUGGESTED ACTION</span>
              <span style="color:var(--text-primary);margin-left:8px">${_esc(thesis.suggested_action)}</span>
            </div>` : ''}
        </div>
      </div>`;
  },

  renderSkeletonPanel(title, subtitle, mini) {
    const padding = mini ? '12px' : '20px';
    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">${_esc(title)}</span></div>
        <div class="panel-body" style="padding:${padding};display:flex;align-items:center;gap:10px;min-height:${mini ? '120' : '160'}px">
          <div class="spinner-sm" style="flex-shrink:0"></div>
          <span style="font-size:10px;color:var(--text-dim);letter-spacing:.05em">${_esc(subtitle || 'Loading…')}</span>
        </div>
      </div>`;
  },

  renderPeersPanel(p, pickSymbol) {
    if (!p || !p.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◇ PEER COMPARISON</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">${_esc((p && p.reason) || 'No peers available')}</span></div>
        </div>`;
    }
    const verdictColor = p.verdict === 'BEST-IN-CLASS' ? 'var(--green)'
                       : p.verdict === 'ABOVE PEERS' ? 'var(--green-dim)'
                       : p.verdict === 'LAGGARD' ? 'var(--red)'
                       : 'var(--amber)';

    const sortedRows = [...(p.rows || [])].sort((a, b) => {
      if (a.is_pick) return -1;
      if (b.is_pick) return 1;
      return (b.market_cap || 0) - (a.market_cap || 0);
    });

    const rowsHtml = sortedRows.map(r => {
      const cellStyle = r.is_pick ? 'background:rgba(0,255,159,0.06);font-weight:bold' : '';
      return `
        <tr style="${cellStyle}">
          <td style="padding:4px 6px;color:${r.is_pick ? 'var(--green)' : 'var(--text-primary)'}">${r.is_pick ? '▶ ' : ''}${_esc(r.symbol)}</td>
          <td style="padding:4px 6px;text-align:right">$${fmt.price(r.price)}</td>
          <td style="padding:4px 6px;text-align:right">${this.pctSpan(r.change_pct, 1)}</td>
          <td style="padding:4px 6px;text-align:right">${fmt.compact(r.market_cap)}</td>
          <td style="padding:4px 6px;text-align:right">${this.num(r.pe_ratio, 1)}</td>
          <td style="padding:4px 6px;text-align:right">${this.num(r.forward_pe, 1)}</td>
          <td style="padding:4px 6px;text-align:right">${r.dividend_yield != null ? (r.dividend_yield * 100).toFixed(2) + '%' : '—'}</td>
          <td style="padding:4px 6px;text-align:right">${r.return_on_equity != null ? (r.return_on_equity * 100).toFixed(0) + '%' : '—'}</td>
        </tr>`;
    }).join('');

    const ranksList = Object.entries(p.ranks || {}).map(([k, v]) => {
      const inTop = v.rank <= Math.max(1, v.of / 2);
      const col = inTop ? 'var(--green)' : 'var(--amber)';
      return `<div style="display:inline-block;margin:2px 8px 2px 0;font-size:10px"><span class="text-dim">${_esc(v.label)}:</span> <span style="color:${col}">#${v.rank}/${v.of}</span></div>`;
    }).join('');

    return `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">◇ PEER COMPARISON</span>
          <span style="font-size:9px;color:var(--text-dim);float:right">${_esc(p.sector || '')} · ${p.peer_count} peers</span>
        </div>
        <div class="panel-body" style="padding:10px">
          <div style="margin-bottom:8px">
            <span style="font-size:13px;color:${verdictColor};font-weight:bold;letter-spacing:.05em">${_esc(p.verdict || '—')}</span>
            ${p.top_half_pct != null ? `<span style="font-size:10px;color:var(--text-dim);margin-left:8px">top half on ${p.top_half_pct.toFixed(0)}% of metrics</span>` : ''}
          </div>
          <div style="margin-bottom:8px;line-height:1.5">${ranksList}</div>
          <table style="width:100%;font-size:10px;border-collapse:collapse">
            <thead>
              <tr style="border-bottom:1px solid var(--border);color:var(--text-dim)">
                <th style="padding:4px 6px;text-align:left">SYM</th>
                <th style="padding:4px 6px;text-align:right">PRICE</th>
                <th style="padding:4px 6px;text-align:right">DAY</th>
                <th style="padding:4px 6px;text-align:right">MCAP</th>
                <th style="padding:4px 6px;text-align:right">P/E</th>
                <th style="padding:4px 6px;text-align:right">FWD P/E</th>
                <th style="padding:4px 6px;text-align:right">YIELD</th>
                <th style="padding:4px 6px;text-align:right">ROE</th>
              </tr>
            </thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>
      </div>`;
  },

  renderHistoricalAnalogPanel(a) {
    if (!a || !a.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◇ HISTORICAL ANALOG</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">${_esc((a && a.error) || 'Insufficient history')}</span></div>
        </div>`;
    }
    const fr = a.forward_returns || {};
    const cc = a.current_conditions || {};
    const dirCol = (fr.mean_pct || 0) > 0 ? 'var(--green)' : (fr.mean_pct || 0) < 0 ? 'var(--red)' : 'var(--text-dim)';
    const hitCol = (fr.hit_rate_pct || 0) >= 60 ? 'var(--green)' : (fr.hit_rate_pct || 0) <= 40 ? 'var(--red)' : 'var(--amber)';
    const samples = (a.sample_dates || []).slice(0, 4).map(s =>
      `<tr><td style="padding:2px 6px;color:var(--text-dim)">${_esc(s.date)}</td><td style="padding:2px 6px;text-align:right">${this.pctSpan(s.forward_pct, 1)}</td></tr>`
    ).join('');

    return `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">◇ HISTORICAL ANALOG</span>
          <span style="font-size:9px;color:var(--text-dim);float:right">${a.match_count} matches · ${a.lookback_years}y lookback</span>
        </div>
        <div class="panel-body" style="padding:12px">
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:8px;line-height:1.5">
            ${_esc(a.interpretation || '')}
          </div>
          <div style="display:flex;gap:14px;margin-bottom:10px">
            <div>
              <div style="font-size:24px;font-weight:bold;color:${dirCol};line-height:1">${this.pctSpan(fr.mean_pct, 1)}</div>
              <div style="font-size:8px;color:var(--text-dim);letter-spacing:.15em">AVG ${a.forward_window_days}D RETURN</div>
            </div>
            <div>
              <div style="font-size:24px;font-weight:bold;color:${hitCol};line-height:1">${fr.hit_rate_pct != null ? fr.hit_rate_pct.toFixed(0) : '—'}%</div>
              <div style="font-size:8px;color:var(--text-dim);letter-spacing:.15em">HIT RATE</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:10px;border-top:1px solid var(--border);padding-top:8px;margin-bottom:8px">
            <div><span class="text-dim">CONDITIONS:</span></div>
            <div></div>
            <div><span class="text-dim">RSI:</span> <span class="text-primary">${this.num(cc.rsi_14, 1)} (${_esc(cc.rsi_band || '—')})</span></div>
            <div><span class="text-dim">VOL BAND:</span> <span class="text-primary">${_esc(cc.vol_band || '—')}</span></div>
            <div style="grid-column:span 2"><span class="text-dim">TREND:</span> <span class="text-primary">${_esc(cc.trend_position || '—')}</span></div>
          </div>
          <div style="border-top:1px solid var(--border);padding-top:8px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:4px">RETURN DISTRIBUTION</div>
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;font-size:10px">
              <div><span class="text-dim">P25:</span> ${this.pctSpan(fr.p25_pct, 1)}</div>
              <div><span class="text-dim">MED:</span> ${this.pctSpan(fr.median_pct, 1)}</div>
              <div><span class="text-dim">P75:</span> ${this.pctSpan(fr.p75_pct, 1)}</div>
              <div><span class="text-dim">BEST:</span> ${this.pctSpan(fr.best_pct, 1)}</div>
            </div>
          </div>
          ${samples ? `
            <div style="border-top:1px solid var(--border);margin-top:8px;padding-top:8px">
              <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:4px">SAMPLE MATCHES</div>
              <table style="width:100%;font-size:10px">${samples}</table>
            </div>` : ''}
        </div>
      </div>`;
  },

  renderCryptoExtrasPanel(x) {
    if (!x || x.error) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◇ ON-CHAIN / COMMUNITY</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">No CoinGecko extras</span></div>
        </div>`;
    }
    const scoreRow = (label, val, max = 100) => {
      if (val == null) return '';
      const pct = Math.max(0, Math.min(100, (val / max) * 100));
      const col = this.scoreColor(val);
      return `
        <div style="display:grid;grid-template-columns:120px 1fr 36px;gap:8px;align-items:center;font-size:10px;margin-bottom:4px">
          <span style="color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em">${_esc(label)}</span>
          <div style="background:var(--bg-elevated);height:6px;border-radius:1px;overflow:hidden">
            <div style="width:${pct}%;height:100%;background:${col}"></div>
          </div>
          <span style="color:${col};text-align:right;font-weight:bold">${val.toFixed(0)}</span>
        </div>`;
    };

    return `
      <div class="panel">
        <div class="panel-header">
          <span class="panel-title">◇ ON-CHAIN / COMMUNITY</span>
          <span style="font-size:9px;color:var(--text-dim);float:right">CoinGecko</span>
        </div>
        <div class="panel-body" style="padding:12px">
          ${scoreRow('Developer', x.developer_score)}
          ${scoreRow('Community', x.community_score)}
          ${scoreRow('Liquidity', x.liquidity_score)}
          ${scoreRow('Public Interest', x.public_interest_score)}
          <div style="border-top:1px solid var(--border);margin-top:8px;padding-top:8px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:4px">SOCIAL FOLLOWING</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:10px;line-height:1.5">
              ${x.twitter_followers ? `<div><span class="text-dim">TWITTER:</span> ${fmt.compact(x.twitter_followers)}</div>` : ''}
              ${x.reddit_subscribers ? `<div><span class="text-dim">REDDIT:</span> ${fmt.compact(x.reddit_subscribers)}</div>` : ''}
              ${x.telegram_users ? `<div><span class="text-dim">TELEGRAM:</span> ${fmt.compact(x.telegram_users)}</div>` : ''}
              ${x.reddit_active_48h ? `<div><span class="text-dim">REDDIT 48H:</span> ${fmt.compact(x.reddit_active_48h)}</div>` : ''}
            </div>
          </div>
          ${x.github_stars != null ? `
            <div style="border-top:1px solid var(--border);margin-top:8px;padding-top:8px">
              <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:4px">DEVELOPMENT (GITHUB)</div>
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:10px;line-height:1.5">
                <div><span class="text-dim">⭐ STARS:</span> ${fmt.compact(x.github_stars || 0)}</div>
                <div><span class="text-dim">⑂ FORKS:</span> ${fmt.compact(x.github_forks || 0)}</div>
                <div><span class="text-dim">COMMITS (4W):</span> <span style="color:${(x.github_commit_count_4w || 0) > 0 ? 'var(--green)' : 'var(--text-dim)'}">${x.github_commit_count_4w || 0}</span></div>
                <div><span class="text-dim">PRs MERGED:</span> ${fmt.compact(x.github_pull_requests_merged || 0)}</div>
              </div>
            </div>` : ''}
        </div>
      </div>`;
  },

  signalColor(label) {
    const s = (label || '').toUpperCase();
    if (s.includes('BULL')) return 'var(--green)';
    if (s.includes('BEAR')) return 'var(--red)';
    if (s.includes('MIXED')) return 'var(--amber)';
    return 'var(--text-dim)';
  },

  renderEventsStrip(ev) {
    if (!ev || !ev.available) return '';
    const items = [];
    if (ev.next_earnings_date) {
      const d = ev.days_to_earnings;
      const urgency = (d != null && d <= 14) ? 'var(--amber)' : 'var(--green)';
      items.push(`
        <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-right:1px solid var(--border)">
          <span style="font-size:14px">⚡</span>
          <div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em">NEXT EARNINGS</div>
            <div style="font-size:11px;color:${urgency};font-weight:bold">
              ${_esc(ev.next_earnings_date)}${d != null ? ` <span style="color:var(--text-dim);font-weight:normal">(${d}d)</span>` : ''}
            </div>
          </div>
        </div>`);
    }
    if (ev.next_ex_dividend_date) {
      items.push(`
        <div style="display:flex;align-items:center;gap:8px;padding:6px 12px;border-right:1px solid var(--border)">
          <span style="font-size:14px">⊛</span>
          <div>
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.1em">EX-DIVIDEND</div>
            <div style="font-size:11px;color:var(--blue);font-weight:bold">
              ${_esc(ev.next_ex_dividend_date)} <span style="color:var(--text-dim);font-weight:normal">· ${this.num(ev.dividend_yield_pct, 2)}% yield</span>
            </div>
          </div>
        </div>`);
    }
    if (!items.length) return '';
    return `
      <div class="panel" style="margin-top:8px">
        <div style="display:flex;flex-wrap:wrap;align-items:stretch">
          ${items.join('')}
        </div>
      </div>`;
  },

  renderSocialPanel(s) {
    const sentColor = this.signalColor(s.sentiment_label);
    const scoreCol = s.social_score != null
      ? this.scoreColor(s.social_score)
      : 'var(--text-dim)';

    if (!s.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ SOCIAL</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">No social data</span></div>
        </div>`;
    }

    const samples = (s.sample_messages || []).slice(0, 2).map(m => {
      const sentBadge = m.sentiment === 'Bullish' ? '<span class="text-green">▲</span>'
                       : m.sentiment === 'Bearish' ? '<span class="text-red">▼</span>'
                       : '<span class="text-dim">·</span>';
      return `
        <div style="font-size:9px;color:var(--text-secondary);margin-top:4px;line-height:1.3;border-left:1px solid var(--border);padding-left:6px">
          ${sentBadge} ${_esc((m.body || '').slice(0, 100))}${(m.body || '').length > 100 ? '…' : ''}
        </div>`;
    }).join('');

    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◎ SOCIAL</span></div>
        <div class="panel-body" style="padding:12px">
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:8px">
            <span style="font-size:20px;font-weight:bold;color:${scoreCol}">${s.social_score != null ? s.social_score.toFixed(0) : '—'}</span>
            <span style="font-size:10px;color:${sentColor};letter-spacing:.05em">${_esc(s.sentiment_label || '—')}</span>
          </div>
          <div style="font-size:10px;line-height:1.6">
            <div><span class="text-dim">STOCKTWITS:</span> <span class="text-green">${s.stocktwits_bull || 0}▲</span> / <span class="text-red">${s.stocktwits_bear || 0}▼</span> (${s.stocktwits_total || 0} msgs)</div>
            ${s.bull_ratio != null ? `<div><span class="text-dim">BULL RATIO:</span> ${(s.bull_ratio * 100).toFixed(0)}%</div>` : ''}
            <div><span class="text-dim">REDDIT MENTIONS:</span> ${s.reddit_mentions || 0}${s.reddit_score ? ' · Σ' + fmt.compact(s.reddit_score) + ' karma' : ''}</div>
          </div>
          ${samples}
        </div>
      </div>`;
  },

  renderInsiderPanel(ins, isCrypto) {
    if (isCrypto) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ INSIDER (60D)</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">N/A for crypto</span></div>
        </div>`;
    }
    const sigCol = this.signalColor(ins.signal);
    if (!ins.available) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ INSIDER (60D)</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">No Form 4 activity</span></div>
        </div>`;
    }
    const sample = (ins.sample || []).slice(0, 2).map(t => {
      const ttype = (t.type || '').toUpperCase();
      const tcol = ttype.includes('BUY') || ttype === 'P' ? 'var(--green)' : ttype.includes('SELL') || ttype === 'S' ? 'var(--red)' : 'var(--text-dim)';
      return `
        <div style="font-size:9px;color:var(--text-secondary);line-height:1.3;border-left:2px solid ${tcol};padding-left:6px;margin-top:4px">
          <div><span style="color:${tcol};font-weight:bold">${_esc(ttype || '—')}</span> · ${_esc(t.date || '')}</div>
          <div class="text-dim">${_esc(t.insider || '—')}${t.value ? ' · $' + fmt.compact(t.value) : ''}</div>
        </div>`;
    }).join('');
    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◎ INSIDER (60D)</span></div>
        <div class="panel-body" style="padding:12px">
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:6px">
            <span style="font-size:13px;color:${sigCol};font-weight:bold;letter-spacing:.05em">${_esc(ins.signal || '—')}</span>
          </div>
          <div style="font-size:10px;line-height:1.6">
            <div><span class="text-dim">TXNS:</span> ${ins.total_transactions || 0} (<span class="text-green">${ins.buys || 0}B</span> / <span class="text-red">${ins.sells || 0}S</span>)</div>
            <div><span class="text-dim">BUY $:</span> $${fmt.compact(ins.buy_value || 0)}</div>
            <div><span class="text-dim">SELL $:</span> $${fmt.compact(ins.sell_value || 0)}</div>
            <div><span class="text-dim">NET:</span> <span style="color:${(ins.net_value || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">$${fmt.compact(Math.abs(ins.net_value || 0))}</span></div>
          </div>
          ${sample}
        </div>
      </div>`;
  },

  renderCongressPanel(c, isCrypto) {
    if (isCrypto) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ CONGRESS (180D)</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">N/A for crypto</span></div>
        </div>`;
    }
    const sigCol = this.signalColor(c.signal);
    if (!c.available || !c.total_trades) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ CONGRESS (180D)</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">No trades</span></div>
        </div>`;
    }
    const sample = (c.sample || []).slice(0, 2).map(t => {
      const ttype = (t.transaction_type || '').toLowerCase();
      const tcol = ttype.includes('purchase') || ttype.includes('buy') ? 'var(--green)' : ttype.includes('sale') || ttype.includes('sell') ? 'var(--red)' : 'var(--text-dim)';
      return `
        <div style="font-size:9px;color:var(--text-secondary);line-height:1.3;border-left:2px solid ${tcol};padding-left:6px;margin-top:4px">
          <div><span style="color:${tcol};font-weight:bold">${_esc((t.transaction_type || '').toUpperCase())}</span> · ${_esc(t.date || '')}</div>
          <div class="text-dim">${_esc(t.member || '—')}${t.party ? ' (' + _esc(t.party) + ')' : ''}${t.amount ? ' · ' + _esc(t.amount) : ''}</div>
        </div>`;
    }).join('');
    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◎ CONGRESS (180D)</span></div>
        <div class="panel-body" style="padding:12px">
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:6px">
            <span style="font-size:13px;color:${sigCol};font-weight:bold;letter-spacing:.05em">${_esc(c.signal || '—')}</span>
          </div>
          <div style="font-size:10px;line-height:1.6">
            <div><span class="text-dim">TRADES:</span> ${c.total_trades || 0} (<span class="text-green">${c.buys || 0}B</span> / <span class="text-red">${c.sells || 0}S</span>)</div>
            <div><span class="text-dim">MEMBERS:</span> ${c.members_count || 0}</div>
          </div>
          ${sample}
        </div>
      </div>`;
  },

  renderOptionsFlowPanel(o, isCrypto) {
    if (isCrypto) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ OPTIONS FLOW</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">N/A for crypto</span></div>
        </div>`;
    }
    const sigCol = this.signalColor(o.bias);
    if (!o.available || !o.unusual_count) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◎ OPTIONS FLOW</span></div>
          <div class="panel-body" style="padding:12px"><span class="text-dim" style="font-size:11px">No unusual flow</span></div>
        </div>`;
    }
    const top = (o.top_contracts || []).slice(0, 2).map(c => {
      const sideCol = (c.side || '').toUpperCase() === 'CALL' ? 'var(--green)' : 'var(--red)';
      return `
        <div style="font-size:9px;color:var(--text-secondary);line-height:1.3;border-left:2px solid ${sideCol};padding-left:6px;margin-top:4px">
          <span style="color:${sideCol};font-weight:bold">${_esc((c.side || '').toUpperCase())}</span>
          <span class="text-primary"> $${this.num(c.strike, 0)} ${c.dte != null ? c.dte + 'd' : ''}</span>
          <div class="text-dim">vol ${fmt.compact(c.volume || 0)} / OI ${fmt.compact(c.open_interest || 0)} = ${this.num(c.vol_oi_ratio, 1)}x</div>
        </div>`;
    }).join('');
    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◎ OPTIONS FLOW</span></div>
        <div class="panel-body" style="padding:12px">
          <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:6px">
            <span style="font-size:13px;color:${sigCol};font-weight:bold;letter-spacing:.05em">${_esc(o.bias || '—')}</span>
          </div>
          <div style="font-size:10px;line-height:1.6">
            <div><span class="text-dim">UNUSUAL:</span> ${o.unusual_count || 0} contracts</div>
            <div><span class="text-dim">CALL VOL:</span> ${fmt.compact(o.call_volume || 0)} (${this.num(o.call_pct, 0)}%)</div>
            <div><span class="text-dim">PUT VOL:</span> ${fmt.compact(o.put_volume || 0)}</div>
          </div>
          ${top}
        </div>
      </div>`;
  },

  renderStrengthPanel(str, scoreCol, sigClass) {
    const subs = str.sub_scores || {};
    const subRows = Object.keys(subs).length === 0
      ? '<div class="empty-state" style="padding:14px;font-size:10px">No sub-scores</div>'
      : Object.entries(subs).sort((a, b) => b[1] - a[1]).map(([k, v]) => {
          const pct = Math.max(0, Math.min(100, v));
          const barColor = this.scoreColor(v);
          return `
            <div style="display:grid;grid-template-columns:110px 1fr 36px;gap:8px;align-items:center;font-size:10px;margin-bottom:4px">
              <span style="color:var(--text-secondary);text-transform:uppercase;letter-spacing:.05em">${_esc(k.replace(/_/g, ' '))}</span>
              <div style="background:var(--bg-elevated);height:6px;border-radius:1px;overflow:hidden">
                <div style="width:${pct}%;height:100%;background:${barColor}"></div>
              </div>
              <span style="color:${barColor};text-align:right;font-weight:bold">${v.toFixed(0)}</span>
            </div>`;
        }).join('');

    const earlySignals = (str.early_signals || []).map(s =>
      `<div style="font-size:10px;color:var(--amber);margin-bottom:2px">▸ ${_esc(s)}</div>`
    ).join('') || '<div style="font-size:10px;color:var(--text-dim)">None detected</div>';

    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◆ STRENGTH</span></div>
        <div class="panel-body" style="padding:12px">
          <div style="display:flex;align-items:center;gap:14px;margin-bottom:14px">
            <div style="text-align:center">
              <div style="font-size:36px;color:${scoreCol};font-weight:bold;line-height:1;text-shadow:0 0 12px ${scoreCol}">
                ${str.composite_score != null ? str.composite_score.toFixed(0) : '—'}
              </div>
              <div style="font-size:8px;color:var(--text-dim);letter-spacing:.15em">COMPOSITE</div>
            </div>
            <div style="flex:1">
              <span class="signal-badge ${sigClass}" style="font-size:11px;padding:4px 10px">${_esc(str.signal || '—')}</span>
              <div style="font-size:9px;color:var(--text-dim);margin-top:6px;letter-spacing:.1em">${_esc(str.strategy_lens || '')} LENS</div>
            </div>
          </div>
          <div style="border-top:1px solid var(--border);padding-top:10px;margin-bottom:10px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:6px">SUB-SCORES</div>
            ${subRows}
          </div>
          <div style="border-top:1px solid var(--border);padding-top:10px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:4px">EARLY SIGNALS</div>
            ${earlySignals}
          </div>
        </div>
      </div>`;
  },

  renderForecastPanel(fc) {
    if (fc.error) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◆ FORECAST (30D)</span></div>
          <div class="panel-body" style="padding:14px"><span class="text-dim" style="font-size:11px">${_esc(fc.error)}</span></div>
        </div>`;
    }
    if (!fc || !Object.keys(fc).length) {
      return `
        <div class="panel">
          <div class="panel-header"><span class="panel-title">◆ FORECAST (30D)</span></div>
          <div class="panel-body" style="padding:14px"><span class="text-dim" style="font-size:11px">Forecast unavailable</span></div>
        </div>`;
    }
    const fcCol = (fc.forecast_pct || 0) > 0 ? 'var(--green)' : (fc.forecast_pct || 0) < 0 ? 'var(--red)' : 'var(--text-dim)';
    const probUp = fc.prob_up_20d != null ? (fc.prob_up_20d * 100) : null;
    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◆ FORECAST (${fc.horizon_days || 30}D)</span></div>
        <div class="panel-body" style="padding:12px">
          <div style="display:flex;gap:14px;align-items:center;margin-bottom:12px">
            <div style="text-align:center">
              <div style="font-size:24px;color:${fcCol};font-weight:bold;line-height:1">
                ${this.pctSpan(fc.forecast_pct, 1)}
              </div>
              <div style="font-size:8px;color:var(--text-dim);letter-spacing:.15em;margin-top:2px">PRICE TARGET</div>
            </div>
            <div style="flex:1;font-size:10px;line-height:1.6">
              <div><span class="text-dim">CURRENT:</span> $${fmt.price(fc.current_price)}</div>
              <div><span class="text-dim">TARGET:</span> $${fmt.price(fc.forecast_price)}</div>
              <div><span class="text-dim">RANGE:</span> $${fmt.price(fc.lower_ci)} – $${fmt.price(fc.upper_ci)}</div>
            </div>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:10px;border-top:1px solid var(--border);padding-top:10px">
            <div><span class="text-dim">ML SIGNAL:</span><br><span style="color:var(--green);font-weight:bold">${_esc(fc.ml_signal || '—')}</span></div>
            <div><span class="text-dim">CONFIDENCE:</span><br>${this.num(fc.ml_confidence, 0)}%</div>
            <div><span class="text-dim">PROB UP (20D):</span><br>${probUp != null ? probUp.toFixed(0) + '%' : '—'}</div>
            <div><span class="text-dim">REGIME:</span><br><span style="color:var(--blue)">${_esc(fc.regime || '—')}</span></div>
            <div><span class="text-dim">TREND (60D):</span><br>${this.trendArrow(fc.trend_short)}</div>
            <div><span class="text-dim">TREND (120D):</span><br>${this.trendArrow(fc.trend_mid)}</div>
            <div style="grid-column:span 2"><span class="text-dim">MEAN REVERSION:</span>
              <span style="color:var(--text-primary);margin-left:6px">${_esc(fc.mean_reversion_signal || '—')} (z=${this.num(fc.zscore, 2)})</span>
            </div>
          </div>
        </div>
      </div>`;
  },

  renderOpportunityPanel(opp, headlines) {
    const phaseColors = {
      'EMERGENCE': 'var(--green)',
      'ACCELERATION': 'var(--green)',
      'CONSENSUS': 'var(--amber)',
      'EXHAUSTION': 'var(--red)',
      'REVERSAL': 'var(--red)',
      'DEVELOPING': 'var(--text-dim)',
    };
    const phaseCol = phaseColors[opp.narrative_phase] || 'var(--text-secondary)';

    const news = headlines.length === 0
      ? '<div style="font-size:10px;color:var(--text-dim)">No recent headlines</div>'
      : headlines.map(h => `
          <div style="font-size:10px;color:var(--text-primary);line-height:1.4;margin-bottom:6px;border-left:2px solid var(--border);padding-left:8px">
            ${h.url ? `<a href="${_esc(safeUrl(h.url))}" target="_blank" rel="noopener" style="color:var(--text-primary);text-decoration:none">${_esc(h.title || '')} ↗</a>` : _esc(h.title || '')}
            <div style="font-size:9px;color:var(--text-dim);margin-top:1px">${_esc(h.source || '')} · ${fmt.timeAgo(h.published)}</div>
          </div>`).join('');

    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◆ OPPORTUNITY / NARRATIVE</span></div>
        <div class="panel-body" style="padding:12px">
          ${opp.narrative_phase ? `
            <div style="margin-bottom:12px">
              <div style="display:flex;align-items:baseline;gap:10px;margin-bottom:4px">
                <span style="font-size:14px;color:${phaseCol};font-weight:bold;letter-spacing:.05em">${_esc(opp.narrative_phase)}</span>
                <span style="font-size:10px;color:var(--text-secondary)">${_esc(opp.velocity_label || '')}</span>
              </div>
              <div style="font-size:10px;color:var(--text-dim)">${_esc(opp.phase_detail || '')}</div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;font-size:10px;border-top:1px solid var(--border);padding-top:8px;margin-bottom:10px">
              <div><span class="text-dim">DOMINANT NARRATIVE:</span><br><span style="color:var(--blue)">${_esc(opp.dominant_narrative || '—')}</span></div>
              <div><span class="text-dim">VELOCITY:</span><br><span style="color:${(opp.velocity_score || 0) >= 0 ? 'var(--green)' : 'var(--red)'}">${this.num(opp.velocity_score, 0)}</span></div>
              <div style="grid-column:span 2"><span class="text-dim">ARTICLES (30D):</span> <span class="text-primary">${opp.article_count || 0}</span></div>
            </div>
            ${opp.contrarian_signal ? `
              <div style="background:#3a2a00;border:1px solid var(--amber);padding:6px 8px;margin-bottom:10px;font-size:10px;color:var(--amber);line-height:1.4">
                ⚠ CONTRARIAN: ${_esc(opp.contrarian_detail || '')}
              </div>` : ''}
          ` : '<div style="font-size:11px;color:var(--text-dim);margin-bottom:10px">Narrative analysis unavailable</div>'}

          <div style="border-top:1px solid var(--border);padding-top:8px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:6px">RECENT HEADLINES</div>
            ${news}
          </div>
        </div>
      </div>`;
  },

  renderRiskSnapshotPanel(snap, risk, isCrypto) {
    const ddPct = risk.max_drawdown != null ? (risk.max_drawdown * 100) : null;

    // Short interest summary (stocks only). Color amber/red as the % of float climbs.
    const shortPct = snap.short_percent_float;
    let shortTile = '';
    if (!isCrypto && shortPct != null) {
      const shortPctNum = parseFloat(shortPct) * 100;
      const sCol = shortPctNum >= 20 ? 'var(--red)' : shortPctNum >= 10 ? 'var(--amber)' : 'var(--text-primary)';
      const squeezeNote = shortPctNum >= 20 ? ' · ⚠ squeeze risk' : shortPctNum >= 10 ? ' · elevated' : '';
      shortTile = `<tr><td class="text-dim">SHORT % FLOAT</td><td><span style="color:${sCol};font-weight:bold">${shortPctNum.toFixed(2)}%</span><span class="text-dim" style="font-size:9px">${squeezeNote}</span></td></tr>
                   <tr><td class="text-dim">SHORT RATIO</td><td>${this.num(snap.short_ratio, 2)} days to cover</td></tr>`;
    }

    const fields = isCrypto ? `
      <tr><td class="text-dim">RANK</td><td>#${snap.rank || '—'}</td></tr>
      <tr><td class="text-dim">24H VOL</td><td>$${fmt.compact(snap.volume_24h)}</td></tr>
      <tr><td class="text-dim">30D CHANGE</td><td>${this.pctSpan(snap.change_30d)}</td></tr>
      <tr><td class="text-dim">FROM ATH</td><td>${this.pctSpan(snap.ath_change_pct)}</td></tr>
    ` : `
      <tr><td class="text-dim">P/E</td><td>${this.num(snap.pe_ratio, 1)}</td></tr>
      <tr><td class="text-dim">FWD P/E</td><td>${this.num(snap.forward_pe, 1)}</td></tr>
      <tr><td class="text-dim">DIV YIELD</td><td>${snap.dividend_yield != null ? (snap.dividend_yield * 100).toFixed(2) + '%' : '—'}</td></tr>
      <tr><td class="text-dim">BETA</td><td>${this.num(snap.beta, 2)}</td></tr>
      <tr><td class="text-dim">52W RANGE</td><td>$${fmt.price(snap.fifty_two_week_low)} – $${fmt.price(snap.fifty_two_week_high)}</td></tr>
      <tr><td class="text-dim">ANALYST TGT</td><td>${snap.analyst_target ? '$' + fmt.price(snap.analyst_target) : '—'} <span style="color:var(--text-dim);font-size:9px">${_esc(snap.recommendation || '')}</span></td></tr>
      ${shortTile}
    `;

    return `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">◆ SNAPSHOT / RISK</span></div>
        <div class="panel-body" style="padding:12px">
          <table style="width:100%;font-size:10px;border-collapse:collapse">
            <tbody>
              ${fields}
            </tbody>
          </table>
          <div style="border-top:1px solid var(--border);margin-top:10px;padding-top:10px">
            <div style="font-size:9px;color:var(--text-dim);letter-spacing:.12em;margin-bottom:6px">RISK (TRAILING 1Y)</div>
            <table style="width:100%;font-size:10px;border-collapse:collapse">
              <tbody>
                <tr><td class="text-dim">ANNUAL RETURN</td><td>${this.pctSpan(risk.annual_return_pct, 1)}</td></tr>
                <tr><td class="text-dim">VOLATILITY</td><td>${this.num(risk.annual_volatility_pct, 1)}%</td></tr>
                <tr><td class="text-dim">SHARPE</td><td>${this.num(risk.sharpe, 2)}</td></tr>
                <tr><td class="text-dim">SORTINO</td><td>${this.num(risk.sortino, 2)}</td></tr>
                <tr><td class="text-dim">MAX DRAWDOWN</td><td>${ddPct != null ? '<span class="text-red">' + ddPct.toFixed(1) + '%</span>' : '—'}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>`;
  },

  renderHistory() {
    const body = document.getElementById('ideas-history-body');
    if (!body) return;
    if (!this.history.length) {
      body.innerHTML = `<div class="empty-state" style="padding:20px;font-size:10px;color:var(--text-dim)">No rolls yet — hit GENERATE.</div>`;
      return;
    }
    body.innerHTML = this.history.map((h, i) => {
      const score = h.strength?.composite_score;
      const col = this.scoreColor(score);
      const isActive = this.current && this.current.symbol === h.symbol && this.current.generated_at === h.generated_at;
      return `
        <div onclick="Ideas.showFromHistory(${i})"
             style="padding:8px 10px;margin-bottom:4px;cursor:pointer;border:1px solid ${isActive ? 'var(--green)' : 'var(--border)'};background:${isActive ? 'rgba(0,255,159,0.05)' : 'transparent'}">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="color:var(--green);font-weight:bold;font-size:12px">${_esc(h.symbol)}</span>
            <span style="color:${col};font-weight:bold;font-size:11px">${score != null ? score.toFixed(0) : '—'}</span>
          </div>
          <div style="font-size:9px;color:var(--text-dim);margin-top:2px">
            ${_esc(h.strength?.signal || '')} · ${_esc((h.strategy || '').toUpperCase())}
          </div>
          <div style="font-size:9px;color:var(--text-dim)">${fmt.timeAgo(h.generated_at)}</div>
        </div>`;
    }).join('');
  },

  showFromHistory(i) {
    if (this.history[i]) {
      this.current = this.history[i];
      this.renderCurrent();
      this.renderHistory();
    }
  },

  onFilterChange() {
    this.filters.asset_class = document.getElementById('ideas-asset-class').value;
    this.filters.strategy = document.getElementById('ideas-strategy').value;
    this.filters.min_score = parseInt(document.getElementById('ideas-min-score').value, 10) || 0;
  },

  async onDiscoveryModeChange() {
    const newMode = document.getElementById('ideas-discovery-mode').value;
    if (newMode === this.filters.discovery_mode) return;
    this.filters.discovery_mode = newMode;
    this.loadingUniverse = true;
    this.updateUniverseBadge();
    await this.loadUniverse(newMode);
    this.loadingUniverse = false;
    this.updateUniverseBadge();
  },

  async roll() {
    if (this.rolling) return;
    this.onFilterChange();
    this.rolling = true;
    this.current = null;
    this.renderCurrent();

    const params = new URLSearchParams();
    if (this.filters.asset_class && this.filters.asset_class !== 'any') params.set('asset_class', this.filters.asset_class);
    if (this.filters.strategy) params.set('strategy', this.filters.strategy);
    if (this.filters.min_score) params.set('min_score', String(this.filters.min_score));
    if (this.filters.discovery_mode) params.set('discovery_mode', this.filters.discovery_mode);
    const exclude = this.history.map(h => h.symbol).slice(0, 10);
    if (exclude.length) params.set('exclude', exclude.join(','));

    try {
      // ── Phase 1: fast roll — symbol + snapshot/social/narrative/events ──
      const idea = await API.get('/api/ideas/random?' + params.toString());
      this.current = idea;
      if (idea && !idea.error) {
        this.history.unshift(idea);
        if (this.history.length > 20) this.history.length = 20;
      }
      this.rolling = false;
      this.renderCurrent();
      this.renderHistory();

      // ── Phase 2: enrichment fetch (only if not pre-warmed/cached) ──
      if (idea && !idea.error && idea.enrichment_pending) {
        await this.enrichCurrent(idea);
      }
    } catch (e) {
      this.current = { error: e.message || 'Failed to generate idea' };
      this.rolling = false;
      this.renderCurrent();
      this.renderHistory();
    }
  },

  async enrichCurrent(idea) {
    const sym = idea.symbol;
    const ac = idea.asset_class || 'stock';
    const strat = idea.strategy || 'growth';
    const url = `/api/ideas/enrich/${encodeURIComponent(sym)}?asset_class=${encodeURIComponent(ac)}&strategy=${encodeURIComponent(strat)}`;
    try {
      const enrichment = await API.get(url);
      // Only merge if the user hasn't rolled to a different idea in the meantime
      if (this.current && this.current.symbol === sym && this.current.generated_at === idea.generated_at) {
        Object.assign(this.current, enrichment);
        this.current.enrichment_pending = false;
        // Update the history entry too
        const hi = this.history.findIndex(h => h.symbol === sym && h.generated_at === idea.generated_at);
        if (hi >= 0) Object.assign(this.history[hi], enrichment, { enrichment_pending: false });
        this.renderCurrent();
        this.renderHistory();
      }
    } catch (e) {
      console.warn('enrichment failed', e);
    }
  },

  // Render the embedded 6-month price chart. Called from renderCurrent() after
  // the innerHTML has been written so the container exists in the DOM.
  _renderChart(symbol) {
    const container = document.getElementById('ideas-price-chart');
    if (!container || typeof ChartEngine === 'undefined') return;
    container.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100%"><div class="spinner-sm"></div></div>';
    API.get(`/api/chart/${encodeURIComponent(symbol)}?period=6mo&interval=1d`).then(data => {
      if (!container.isConnected) return;  // user navigated away or rolled
      container.innerHTML = '';
      try {
        const chartRef = ChartEngine.createPriceChart('ideas-price-chart', data.data, { height: 280 });
        if (data.data.length >= 50) {
          ChartEngine.addSMAOverlay(chartRef, data.data, 50, 'rgba(255,170,0,0.7)');
        }
        if (data.data.length >= 200) {
          ChartEngine.addSMAOverlay(chartRef, data.data, 200, 'rgba(0,200,255,0.7)');
        }
      } catch (e) {
        container.innerHTML = `<div class="text-dim" style="padding:20px;font-size:11px">Chart unavailable: ${_esc(e.message)}</div>`;
      }
    }).catch(e => {
      if (container.isConnected) {
        container.innerHTML = `<div class="text-dim" style="padding:20px;font-size:11px">Chart data unavailable</div>`;
      }
    });
  },

  clearHistory() {
    this.history = [];
    this.current = null;
    this.renderCurrent();
    this.renderHistory();
  },
};

async function loadIdeasView() {
  const view = document.getElementById('view-ideas');
  if (!Ideas.shellRendered) {
    view.innerHTML = `<div class="loading"><div class="spinner"></div> LOADING IDEA ENGINE…</div>`;
    await Ideas.loadUniverse();
    Ideas.renderShell();
  }
}

document.addEventListener('DOMContentLoaded', init);
