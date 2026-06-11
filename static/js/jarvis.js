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

  function runAction(action) {
    if (!action || !action.view) return;
    if (action.symbol && action.view === 'research' && typeof openResearch === 'function') {
      openResearch(action.symbol);
    } else if (typeof navigate === 'function') {
      navigate(action.view);
    }
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
  const Palette = {
    el: null, input: null, list: null, answer: null,
    _items: [], _sel: 0, _asking: false,

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
        <div id="jarvis-palette" role="dialog" aria-label="Jarvis command palette">
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
      this.el.classList.add('open');
      this.input.value = '';
      this.answer.innerHTML = '';
      this._refresh();
      this.input.focus();
    },
    close() { this.el.classList.remove('open'); },

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
        items.push(...this.SUGGESTIONS.map(s => ({ kind: 'ask', label: s, query: s })));
      } else {
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
      const ICON = { nav: '→', symbol: '◇', ask: '◉' };
      this.list.innerHTML = this._items.map((it, i) =>
        `<div class="jp-item${i === this._sel ? ' sel' : ''}" data-i="${i}" role="option" aria-selected="${i === this._sel}">
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
      else if (it.kind === 'ask') { this._ask(it.query); }
    },

    async _ask(query) {
      if (this._asking) return;
      this._asking = true;
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
  };

  global.Jarvis = Jarvis;
  const boot = () => { Palette.init(); Watch.start(); };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
