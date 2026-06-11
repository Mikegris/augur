// ── JARVIS — unified assistant layer (frontend) ───────────────────────────────
// Two surfaces:
//   1. Briefing panel — rendered at the top of the Overview by loadOverview()
//      via Jarvis.renderBriefing('ov-jarvis'). Prioritized insight cards with
//      deep-link actions into the app's views.
//   2. Command palette — ⌘K / Ctrl+K anywhere. Fuzzy view navigation, instant
//      symbol open, and natural-language questions answered inline by
//      POST /api/jarvis/ask.
// Classic script: reads the app's global lexical declarations (NAV_GROUPS,
// navigate, openResearch, _esc) by name, guarded with typeof checks.
(function (global) {
  'use strict';

  const esc = (s) => (typeof _esc === 'function' ? _esc(s) : String(s == null ? '' : s));

  // Poll for a DOM hook after navigate() — views render async. ~10 tries ×
  // 100ms, then give up silently (the plain navigation already happened).
  function pollFor(getEl, run, tries) {
    let left = tries == null ? 10 : tries;
    const tick = () => {
      const el = getEl();
      if (el) { run(el); return; }
      if (--left > 0) setTimeout(tick, 100);
    };
    setTimeout(tick, 100);
  }

  // Navigate to a view, then — if the view exposes the right hook — run an
  // action inside it. Degrades to plain navigate() when hooks never appear.
  function navigateAndRun(view, getHook, run) {
    if (typeof navigate !== 'function') return;
    navigate(view);
    pollFor(getHook, run);
  }

  function runAction(action) {
    if (!action || !action.view) return;
    const sym = action.symbol ? String(action.symbol).toUpperCase() : null;
    if (sym && action.view === 'research' && typeof openResearch === 'function') {
      openResearch(sym); // openResearch is already navigate-and-run
      return;
    }
    if (sym && action.view === 'forecast' && typeof analyzeForecast === 'function') {
      navigateAndRun('forecast',
        () => document.getElementById('forecast-symbol'),
        (input) => { input.value = sym; analyzeForecast(sym); });
      return;
    }
    if (typeof navigate === 'function') navigate(action.view);
  }

  // "stress test" palette command — open the view and press its run button.
  function runStressCommand() {
    navigateAndRun('stress',
      () => document.getElementById('stress-run-btn'),
      (btn) => btn.click());
  }

  const TONE_COLOR = { pos: 'var(--green)', neg: 'var(--red)', warn: 'var(--amber)', info: 'var(--blue)' };

  // ── Briefing panel ──────────────────────────────────────────────────────────
  const Briefing = {
    _last: null,

    async render(containerId, force) {
      const el = document.getElementById(containerId);
      if (!el) return;
      el.innerHTML = `
        <div class="panel jarvis-panel">
          <div class="panel-header">
            <span class="panel-title jarvis-title">◉ JARVIS</span>
            <span class="jarvis-hint">⌘K to ask anything</span>
            <button class="btn btn-ghost btn-sm" id="jarvis-refresh">↻</button>
          </div>
          <div class="panel-body" id="jarvis-body">
            <div class="loading"><div class="spinner"></div> Synthesizing your briefing...</div>
          </div>
        </div>`;
      const refreshBtn = document.getElementById('jarvis-refresh');
      if (refreshBtn) refreshBtn.addEventListener('click', () => this.render(containerId, true));
      try {
        const b = await API.get('/api/jarvis/briefing' + (force ? '?refresh=1' : ''));
        this._last = b;
        this._renderBody(b);
      } catch (e) {
        const body = document.getElementById('jarvis-body');
        if (body) body.innerHTML = `<div class="empty-state"><span>Briefing unavailable: ${esc(e.message)}</span></div>`;
      }
    },

    _renderBody(b) {
      const body = document.getElementById('jarvis-body');
      if (!body) return;
      const cards = (b.insights || []).map((c, i) => {
        const color = TONE_COLOR[c.tone] || 'var(--blue)';
        const pri = c.priority === 1
          ? '<span class="jarvis-pri p1">P1</span>'
          : (c.priority === 2 ? '<span class="jarvis-pri p2">P2</span>' : '');
        const go = c.action ? `<button class="jarvis-go" data-idx="${i}">OPEN →</button>` : '';
        return `<div class="jarvis-card" style="border-left-color:${color}">
          <div class="jarvis-card-head">${pri}<span class="jarvis-card-title">${esc(c.title)}</span>${go}</div>
          <div class="jarvis-card-detail">${esc(c.detail)}</div>
        </div>`;
      }).join('');
      body.innerHTML = `
        <div class="jarvis-greeting">${esc(b.greeting || '')}</div>
        <div class="jarvis-headline">${esc(b.headline || '')}</div>
        ${cards ? `<div class="jarvis-cards">${cards}</div>`
                : '<div class="jarvis-allclear">All clear — nothing needs your attention right now.</div>'}`;
      body.querySelectorAll('.jarvis-go').forEach(btn => {
        btn.addEventListener('click', () => {
          const c = (this._last.insights || [])[Number(btn.dataset.idx)];
          if (c) runAction(c.action);
        });
      });
    },
  };

  // ── Command palette ─────────────────────────────────────────────────────────
  // Client-side palette commands, parsed before falling through to /ask.
  // e.g. "alert NVDA above 190", "alert tsla < 200", "alert AAPL 250"
  const ALERT_CMD = /^alert\s+([A-Za-z.\-]{1,10})\s+(above|below|at|>|<)?\s*\$?(\d+(?:\.\d+)?)$/i;
  const STRESS_CMD = /^stress\s*test$/i;

  const Palette = {
    el: null, input: null, list: null, answer: null,
    _items: [], _sel: 0, _asking: false, _recent: [], _prevFocus: null,

    SUGGESTIONS: [
      "how's my portfolio",
      'biggest loser today',
      'how are markets',
      'my exposure',
      'any earnings coming up',
      'any ideas',
    ],

    init() {
      if (this.el) return;
      const wrap = document.createElement('div');
      wrap.id = 'jarvis-palette-overlay';
      wrap.innerHTML = `
        <div id="jarvis-palette" role="dialog" aria-modal="true" aria-label="Jarvis command palette">
          <div class="jp-input-row">
            <span class="jp-glyph">◉</span>
            <input id="jp-input" type="text" placeholder="Ask Jarvis or jump anywhere... (e.g. 'forecast NVDA', 'biggest loser today')"
                   autocomplete="off" spellcheck="false">
          </div>
          <div id="jp-answer"></div>
          <div id="jp-list" role="listbox"></div>
          <div class="jp-footer">↑↓ NAVIGATE &nbsp;·&nbsp; ENTER RUN &nbsp;·&nbsp; ESC CLOSE</div>
        </div>`;
      document.body.appendChild(wrap);
      this.el = wrap;
      this.input = document.getElementById('jp-input');
      this.list = document.getElementById('jp-list');
      this.answer = document.getElementById('jp-answer');

      wrap.addEventListener('click', (e) => { if (e.target === wrap) this.close(); });
      wrap.addEventListener('keydown', (e) => this._trapTab(e));
      this.input.addEventListener('input', () => this._refresh());
      this.input.addEventListener('keydown', (e) => this._onKey(e));
      document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
          e.preventDefault();
          this.isOpen() ? this.close() : this.open();
        } else if (e.key === 'Escape' && this.isOpen()) {
          this.close();
        }
      });
    },

    isOpen() { return !!(this.el && this.el.classList.contains('open')); },
    open() {
      this._prevFocus = document.activeElement;
      this.el.classList.add('open');
      this.input.value = '';
      this.answer.innerHTML = '';
      this._refresh();
      this.input.focus();
    },
    close() {
      this.el.classList.remove('open');
      const prev = this._prevFocus;
      this._prevFocus = null;
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) prev.focus();
    },

    // Focus trap: while the palette is open, Tab / Shift+Tab cycle within it.
    _trapTab(e) {
      if (e.key !== 'Tab' || !this.isOpen()) return;
      const focusables = Array.from(
        this.el.querySelectorAll('input, button, [tabindex]:not([tabindex="-1"])')
      ).filter(el => !el.disabled && el.offsetParent !== null);
      if (!focusables.length) { e.preventDefault(); return; }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !this.el.contains(active))) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && (active === last || !this.el.contains(active))) {
        e.preventDefault(); first.focus();
      }
    },

    _views() {
      const out = [];
      if (typeof NAV_GROUPS !== 'object') return out;
      for (const g of Object.values(NAV_GROUPS)) {
        for (const it of g.items) out.push({ group: g.label, view: it.view, label: it.label });
      }
      return out;
    },

    _refresh() {
      const q = this.input.value.trim();
      const ql = q.toLowerCase();
      const items = [];

      if (!q) {
        items.push(...this._recent.map(s => ({ kind: 'ask', label: '↺ ' + s, query: s })));
        items.push(...this.SUGGESTIONS
          .filter(s => !this._recent.includes(s))
          .map(s => ({ kind: 'ask', label: s, query: s })));
      } else {
        // Client-side commands — parsed before everything else
        const am = q.match(ALERT_CMD);
        if (am) {
          const sym = am[1].toUpperCase();
          const dir = (am[2] || '').toLowerCase();
          const type = (dir === 'below' || dir === '<') ? 'below' : 'above';
          const price = parseFloat(am[3]);
          items.push({
            kind: 'alert', symbol: sym, alertType: type, price,
            label: `Set alert: ${sym} ${type} ${am[3]}`,
          });
        }
        if (STRESS_CMD.test(q)) {
          items.push({ kind: 'stress', label: 'Run stress test' });
        }
        // View navigation matches
        for (const v of this._views()) {
          if (v.label.toLowerCase().includes(ql) || v.view.includes(ql)) {
            items.push({ kind: 'nav', label: `${v.group} / ${v.label}`, view: v.view });
          }
        }
        // Ticker-shaped input → instant research open
        if (/^\$?[A-Za-z][A-Za-z.\-]{0,5}$/.test(q)) {
          const sym = q.replace(/^\$/, '').toUpperCase();
          items.push({ kind: 'symbol', label: `Open research: ${sym}`, symbol: sym });
        }
        // Always offer the question route
        items.push({ kind: 'ask', label: `Ask Jarvis: “${q}”`, query: q });
      }

      this._items = items.slice(0, 9);
      this._sel = 0;
      this._renderList();
    },

    _renderList() {
      const ICON = { nav: '→', symbol: '◇', ask: '◉', alert: '⚡', stress: '⚡' };
      const CMD = { alert: 1, stress: 1 };
      this.list.innerHTML = this._items.map((it, i) =>
        `<div class="jp-item${CMD[it.kind] ? ' jp-cmd' : ''}${i === this._sel ? ' sel' : ''}" data-i="${i}" role="option" aria-selected="${i === this._sel}">
           <span class="jp-icon">${ICON[it.kind]}</span>${esc(it.label)}
         </div>`).join('');
      this.list.querySelectorAll('.jp-item').forEach(el => {
        el.addEventListener('click', () => { this._sel = Number(el.dataset.i); this._run(); });
        el.addEventListener('mousemove', () => {
          if (this._sel !== Number(el.dataset.i)) { this._sel = Number(el.dataset.i); this._renderList(); }
        });
      });
    },

    _onKey(e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); this._sel = Math.min(this._sel + 1, this._items.length - 1); this._renderList(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); this._sel = Math.max(this._sel - 1, 0); this._renderList(); }
      else if (e.key === 'Enter') { e.preventDefault(); this._run(); }
    },

    _run() {
      const it = this._items[this._sel];
      if (!it) return;
      if (it.kind === 'nav') { this.close(); navigate(it.view); }
      else if (it.kind === 'symbol') { this.close(); if (typeof openResearch === 'function') openResearch(it.symbol); }
      else if (it.kind === 'alert') { this._setAlert(it); }
      else if (it.kind === 'stress') { this.close(); runStressCommand(); }
      else if (it.kind === 'ask') { this._ask(it.query); }
    },

    async _setAlert(it) {
      try {
        // Body shape matches app.js submitAddAlert(): POST /api/alerts
        await API.post('/api/alerts', { symbol: it.symbol, alert_type: it.alertType, price: it.price });
        Toast.success(`Alert set: ${it.symbol} ${it.alertType} $${it.price}`);
        this.close();
      } catch (e) {
        this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
      }
    },

    async _ask(query) {
      if (this._asking) return;
      this._asking = true;
      this._recent = [query].concat(this._recent.filter(s => s !== query)).slice(0, 4);
      this.answer.innerHTML = '<div class="jp-answer thinking"><div class="spinner"></div> Working on it...</div>';
      try {
        const r = await API.post('/api/jarvis/ask', { query });
        const action = r.action
          ? `<button class="jarvis-go" id="jp-answer-go">OPEN →</button>` : '';
        this.answer.innerHTML = `
          <div class="jp-answer">
            <div class="jp-answer-text">${esc(r.answer)}</div>
            ${r.detail ? `<div class="jp-answer-detail">${esc(r.detail)}</div>` : ''}
            ${action}
          </div>`;
        const go = document.getElementById('jp-answer-go');
        if (go) go.addEventListener('click', () => { this.close(); runAction(r.action); });
      } catch (e) {
        this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
      } finally {
        this._asking = false;
      }
    },
  };

  // ── Context strip — Jarvis speaks on every view ─────────────────────────────
  // A persistent line under the sub-nav, refreshed on each navigate() with a
  // short typewriter reveal. Research view includes the active symbol.
  const Strip = {
    el: null, _seq: 0, _timer: null,

    ensure() {
      if (this.el && document.body.contains(this.el)) return;
      const subNav = document.getElementById('sub-nav');
      if (!subNav || !subNav.parentNode) return;
      const bar = document.createElement('div');
      bar.id = 'jarvis-strip';
      // The typed span rewrites textContent every frame — keep it silent for
      // screen readers; the sr-only sibling receives the full line exactly
      // once, when typing completes.
      bar.innerHTML = '<span class="js-glyph" aria-hidden="true">◉</span>'
        + '<span id="jarvis-strip-text" aria-live="off" aria-hidden="true"></span>'
        + '<span class="js-caret" aria-hidden="true"></span>'
        + '<span id="jarvis-strip-sr" class="sr-only" role="status" aria-live="polite"></span>';
      subNav.parentNode.insertBefore(bar, subNav.nextSibling);
      this.el = bar;
    },

    async update(view) {
      this.ensure();
      if (!this.el) return;
      const seq = ++this._seq;
      let url = '/api/jarvis/context/' + encodeURIComponent(view);
      if (view === 'research' && State.researchSymbol) {
        url += '?symbol=' + encodeURIComponent(State.researchSymbol);
      }
      try {
        const r = await API.get(url);
        if (seq !== this._seq || !r || !r.line) return;
        this._type(r.line, r.tone || 'info');
      } catch (e) { /* server busy — keep the previous line */ }
    },

    _type(text, tone) {
      const out = document.getElementById('jarvis-strip-text');
      if (!out) return;
      if (this._timer) clearInterval(this._timer);
      const sr = document.getElementById('jarvis-strip-sr');
      if (sr) sr.textContent = '';
      this.el.dataset.tone = tone;
      this.el.classList.add('typing');
      let i = 0;
      out.textContent = '';
      this._timer = setInterval(() => {
        i += 3; // 3 chars/frame ≈ fast but visibly "spoken"
        out.textContent = text.slice(0, i);
        if (i >= text.length) {
          clearInterval(this._timer);
          this._timer = null;
          this.el.classList.remove('typing');
          if (sr) sr.textContent = text; // announce the full line once
        }
      }, 16);
    },
  };

  // ── Activity engine — every API call is Jarvis working; show it ────────────
  // Wraps the global API helper so each request becomes a visible operation
  // in the neural-activity panel, with in-flight / done / failed states and
  // timings. The orb (bottom-right) breathes when idle, spins up when busy.
  const OP_PATTERNS = [
    [/^\/api\/quote\/([^/?]+)/,            (m) => 'Pulling live quote · ' + m[1].toUpperCase()],
    [/^\/api\/quotes/,                     () => 'Refreshing quote board'],
    [/^\/api\/chart\/([^/?]+)/,            (m) => 'Rendering price history · ' + m[1].toUpperCase()],
    [/^\/api\/fundamentals\/([^/?]+)/,     (m) => 'Studying fundamentals · ' + m[1].toUpperCase()],
    [/^\/api\/news/,                       () => 'Scanning the wires'],
    [/^\/api\/market\/indices/,            () => 'Sampling global indices'],
    [/^\/api\/market\/sectors/,            () => 'Mapping sector performance'],
    [/^\/api\/market\/movers/,             () => 'Hunting market movers'],
    [/^\/api\/portfolio\/ai-analysis/,     () => 'Deep-reading your portfolio'],
    [/^\/api\/portfolio/,                  () => 'Auditing your book'],
    [/^\/api\/analytics/,                  () => 'Crunching portfolio math'],
    [/^\/api\/forecast\/ensemble\/([^/?]+)/, (m) => 'Fusing forecast engines · ' + m[1].toUpperCase()],
    [/^\/api\/forecast\/accountability/,   () => 'Auditing my own track record'],
    [/^\/api\/jarvis\/briefing/,           () => 'Synthesizing your briefing'],
    [/^\/api\/jarvis\/ask/,                () => 'Reasoning on your question'],
    [/^\/api\/jarvis\/context/,            () => 'Reading the room'],
    [/^\/api\/jarvis\/activity/,           null],  // don't log the logger
    [/^\/api\/intel/,                      () => 'Decoding SEC filings'],
    [/^\/api\/earnings/,                   () => 'Checking the earnings calendar'],
    [/^\/api\/crypto/,                     () => 'Polling crypto markets'],
    [/^\/api\/synth\//,                    () => 'Running synthesis engines'],
    [/^\/api\/research\//,                 () => 'Working the research lab'],
    [/^\/api\/(options-flow|gex)/,         () => 'Parsing options flow'],
    [/^\/api\/alerts/,                     () => 'Checking tripwires'],
    [/^\/api\/watchlist/,                  () => 'Sweeping the watchlist'],
    [/^\/api\/macro/,                      () => 'Reading macro indicators'],
    [/^\/api\/stress-test/,                () => 'Stress-testing your book'],
    [/^\/api\/smart-money/,                () => 'Tracing smart money'],
    [/^\/api\/congress/,                   () => 'Reading congressional filings'],
    [/^\/api\/search/,                     () => 'Searching the universe'],
  ];

  const FAIL_WINDOW_MS = 60 * 1000;       // rolling failure window
  const FAIL_THRESHOLD = 4;               // failures of one feed within the window
  const FAIL_ANNOUNCE_COOLDOWN_MS = 5 * 60 * 1000;

  const Activity = {
    ops: [], _id: 0, _inflight: 0, _raf: false,
    _failures: {},                        // feed key -> [timestamps]
    _lastFailAnnounce: 0,

    describe(path) {
      for (const [re, fn] of OP_PATTERNS) {
        const m = path.match(re);
        if (m) return fn ? fn(m) : null;
      }
      return 'Processing ' + path.replace(/^\/api\//, '').split('?')[0];
    },

    begin(path) {
      const desc = this.describe(path);
      if (desc === null) return null;
      const op = { id: ++this._id, desc, t0: performance.now(), state: 'run', ms: null };
      this.ops.unshift(op);
      if (this.ops.length > 40) this.ops.pop();
      this._inflight++;
      this._schedule();
      return op;
    },

    end(op, ok) {
      if (!op) return;
      op.state = ok ? 'ok' : 'err';
      op.ms = Math.round(performance.now() - op.t0);
      this._inflight = Math.max(0, this._inflight - 1);
      this._pruneFailures();              // keep the window memory bounded
      if (!ok) this._noteFailure(op.desc);
      this._schedule();
    },

    // ── Error awareness — Jarvis speaks up when a feed keeps failing ─────────
    // Failures are keyed by the feed name: the op description's first
    // word-group (the part before the " · SYMBOL" suffix), so e.g. all
    // "Pulling live quote · NVDA/AAPL/..." failures pool together.
    _failKey(desc) {
      return String(desc || '').split('·')[0].trim() || 'API feed';
    },

    _noteFailure(desc) {
      const key = this._failKey(desc);
      const now = Date.now();
      const arr = this._failures[key] || (this._failures[key] = []);
      arr.push(now);
      if (arr.length >= FAIL_THRESHOLD
          && now - this._lastFailAnnounce > FAIL_ANNOUNCE_COOLDOWN_MS) {
        this._lastFailAnnounce = now;
        if (typeof Toast === 'object' && Toast.warn) {
          Toast.warn('◉ JARVIS: ' + key + ' is struggling — data may be stale or rate-limited.');
        }
      }
    },

    _pruneFailures() {
      const cutoff = Date.now() - FAIL_WINDOW_MS;
      for (const key of Object.keys(this._failures)) {
        const kept = this._failures[key].filter(t => t >= cutoff);
        if (kept.length) this._failures[key] = kept;
        else delete this._failures[key];
      }
    },

    instrument() {
      if (API.__jarvis) return;
      API.__jarvis = true;
      for (const method of ['get', 'post', 'put', 'del']) {
        const orig = API[method].bind(API);
        API[method] = async (path, body) => {
          const op = this.begin(String(path));
          try {
            const r = await orig(path, body);
            this.end(op, true);
            return r;
          } catch (e) {
            this.end(op, false);
            throw e;
          }
        };
      }
    },

    _schedule() {
      if (this._raf) return;
      this._raf = true;
      requestAnimationFrame(() => { this._raf = false; Orb.render(); });
    },
  };

  // ── Orb dock + neural activity panel ────────────────────────────────────────
  const Orb = {
    el: null, panel: null, _open: false, _bgTimer: null,
    _es: null, _sseFailed: false, _flashTimer: null,

    init() {
      if (this.el) return;
      const orb = document.createElement('button');
      orb.id = 'jarvis-orb';
      orb.title = 'Jarvis neural activity';
      orb.setAttribute('aria-label', 'Toggle Jarvis activity panel');
      orb.setAttribute('aria-expanded', 'false');
      orb.setAttribute('aria-controls', 'jarvis-activity-panel');
      orb.innerHTML = '<span class="orb-ring"></span><span class="orb-core"></span><span class="orb-count" id="orb-count"></span>';
      orb.addEventListener('click', () => this.toggle());
      document.body.appendChild(orb);
      this.el = orb;

      const panel = document.createElement('div');
      panel.id = 'jarvis-activity-panel';
      panel.setAttribute('role', 'region');
      panel.setAttribute('aria-label', 'Jarvis neural activity');
      panel.innerHTML = `
        <div class="jap-header"><span class="jarvis-core">◉</span> JARVIS // NEURAL ACTIVITY
          <button class="jap-close" aria-label="Close">×</button></div>
        <div class="jap-bg" id="jap-bg">Background systems nominal</div>
        <div class="jap-list" id="jap-list"><div class="jap-empty">Standing by. Operations appear here as I work.</div></div>`;
      panel.querySelector('.jap-close').addEventListener('click', () => this.toggle(false));
      document.body.appendChild(panel);
      this.panel = panel;
    },

    toggle(force) {
      this._open = force !== undefined ? force : !this._open;
      this.panel.classList.toggle('open', this._open);
      if (this.el) this.el.setAttribute('aria-expanded', String(this._open));
      if (this._open) {
        this.render();
        this._sseFailed = false; // reopening the panel re-earns an SSE attempt
        this._startBackground();
      } else {
        this._stopBackground();
      }
    },

    // ── Background section: SSE first, 60s polling fallback ──────────────────
    // The server emits the first snapshot immediately on connect, then only
    // on change; the connection hard-stops server-side every 10 min and the
    // browser's EventSource transparently reconnects.
    _startBackground() {
      if (typeof EventSource === 'undefined' || this._sseFailed) {
        this._startPolling();
        return;
      }
      let es;
      try {
        es = new EventSource('/api/jarvis/activity/stream');
      } catch (e) {
        this._sseFailed = true;
        this._startPolling();
        return;
      }
      this._es = es;
      es.onmessage = (ev) => {
        try {
          const a = JSON.parse(ev.data);
          this._renderBackground(a);
          this._flash();
        } catch (err) { /* malformed frame — keep previous */ }
      };
      es.onerror = () => {
        // CONNECTING = the browser is auto-reconnecting (e.g. the server's
        // 10-min hard stop) — let it. CLOSED = SSE is genuinely unavailable
        // (404/500/wrong mimetype): fall back to polling and don't retry
        // SSE until the panel is reopened.
        if (this._es && this._es.readyState !== EventSource.CLOSED) return;
        this._stopSse();
        this._sseFailed = true;
        if (this._open) this._startPolling();
      };
    },

    _startPolling() {
      if (this._bgTimer) return;
      this._refreshBackground();
      this._bgTimer = setInterval(() => this._refreshBackground(), 60000);
    },

    _stopSse() {
      if (this._es) {
        try { this._es.close(); } catch (e) { /* already dead */ }
        this._es = null;
      }
    },

    _stopBackground() {
      this._stopSse();
      if (this._bgTimer) { clearInterval(this._bgTimer); this._bgTimer = null; }
      if (this._flashTimer) { clearTimeout(this._flashTimer); this._flashTimer = null; }
      if (this.el) this.el.classList.remove('sse-flash');
    },

    // Live-data touch: flash the orb for ~600ms when an SSE frame lands.
    _flash() {
      if (!this.el) return;
      if (global.matchMedia
          && global.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      if (this._flashTimer) clearTimeout(this._flashTimer);
      this.el.classList.add('sse-flash');
      this._flashTimer = setTimeout(() => {
        this._flashTimer = null;
        if (this.el) this.el.classList.remove('sse-flash');
      }, 600);
    },

    // Shared renderer for the background section (SSE frames + poll results).
    _renderBackground(a) {
      const bg = document.getElementById('jap-bg');
      if (!bg || !a) return;
      const lines = (a.background || []).slice(0, 4)
        .map(b => `<div class="jap-bg-row">▹ ${esc(b.label)} <span>${esc(b.when || '')}</span></div>`).join('');
      bg.innerHTML = `<div class="jap-bg-sum">${esc(a.summary || '')}</div>${lines}`;
    },

    async _refreshBackground() {
      try {
        const a = await API.get('/api/jarvis/activity');
        this._renderBackground(a);
      } catch (e) { /* leave previous */ }
    },

    render() {
      if (!this.el) return;
      this.el.classList.toggle('busy', Activity._inflight > 0);
      const count = document.getElementById('orb-count');
      if (count) {
        count.textContent = Activity._inflight > 0 ? String(Activity._inflight) : '';
      }
      if (!this._open) return;
      const list = document.getElementById('jap-list');
      if (!list) return;
      const ICON = { run: '<span class="jap-run"></span>', ok: '✓', err: '✗' };
      list.innerHTML = Activity.ops.length
        ? Activity.ops.map(o =>
            `<div class="jap-row ${o.state}">
               <span class="jap-ic">${ICON[o.state]}</span>
               <span class="jap-desc">${esc(o.desc)}</span>
               <span class="jap-ms">${o.ms != null ? o.ms + 'ms' : ''}</span>
             </div>`).join('')
        : '<div class="jap-empty">Standing by. Operations appear here as I work.</div>';
    },
  };

  // ── Proactive watch — Jarvis speaks up when a NEW urgent insight appears ────
  // Polls the (server-cached) briefing every 5 min and toasts P1 items it
  // hasn't announced yet. First poll seeds the seen-set silently so a page
  // load doesn't re-announce what the overview panel already shows.
  const Watch = {
    _seen: null,
    _timer: null,
    start() {
      if (this._timer) return;
      const tick = async () => {
        try {
          const b = await API.get('/api/jarvis/briefing');
          const p1 = (b.insights || []).filter(c => c.priority === 1);
          if (this._seen === null) {
            this._seen = new Set(p1.map(c => c.title));
            return;
          }
          for (const c of p1) {
            if (!this._seen.has(c.title)) {
              this._seen.add(c.title);
              Toast.show('◉ JARVIS: ' + c.title, 'amber', 8000);
            }
          }
        } catch (e) { /* server asleep or offline — stay quiet */ }
      };
      tick();
      this._timer = setInterval(tick, 5 * 60 * 1000);
    },
  };

  const Jarvis = {
    renderBriefing: (id, force) => Briefing.render(id, force),
    initPalette: () => Palette.init(),
    openPalette: () => { Palette.init(); Palette.open(); },
    // Called by navigate() / loadResearchFor(). Deferred a tick so callers
    // that set State.researchSymbol right after navigating are picked up.
    onNavigate: (view) => { setTimeout(() => Strip.update(view), 40); },
  };

  global.Jarvis = Jarvis;
  const boot = () => { Activity.instrument(); Orb.init(); Palette.init(); Watch.start(); };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
