/* ================================================================
   AUGUR // RESEARCH LAB — Hypothesis Generator + Track Record
   ----------------------------------------------------------------
   Renders a "synthesis" panel that lets the user generate an
   AI hypothesis for a symbol (POST /api/research/hypothesis/generate),
   preview it, save it, and review the track record of past saves
   with their realized-vs-predicted outcomes.

   Exposes one global entry point:   renderHypothesisLab(containerEl)

   Helpers used (from app.js if present, else fallbacks defined here):
     fmt.pct, fmt.date, fmt.num
   ================================================================ */

(function () {
  'use strict';

  // ── Local fallbacks for the formatters (in case app.js isn't loaded yet)
  const _fmt = (typeof fmt !== 'undefined') ? fmt : {
    pct: (v) => (v == null || isNaN(parseFloat(v))) ? '—' : (parseFloat(v).toFixed(2) + '%'),
    date: (s) => s ? new Date(s).toLocaleDateString() : '—',
    num: (v, d) => (v == null || isNaN(parseFloat(v))) ? '—' : parseFloat(v).toFixed(d || 2),
  };

  const STATUS_COLORS = {
    OPEN:         'col-yellow',
    CONFIRMED:    'col-green',
    REJECTED:     'col-red',
    INVALIDATED:  'col-amber',
  };

  // ── State (per-render) ────────────────────────────────────────────
  let _state = {
    container: null,
    statusFilter: null,
    symbolFilter: '',
    lastPreview: null,
  };

  // ── Fetch helpers ─────────────────────────────────────────────────
  async function apiGenerate(symbol) {
    const r = await fetch('/api/research/hypothesis/generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol }),
    });
    return r.json();
  }

  async function apiSave(symbol, hypothesis) {
    const r = await fetch('/api/research/hypothesis/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ symbol, hypothesis }),
    });
    return r.json();
  }

  async function apiList({ status, symbol, limit } = {}) {
    const qs = new URLSearchParams();
    if (status) qs.set('status', status);
    if (symbol) qs.set('symbol', symbol);
    if (limit)  qs.set('limit',  String(limit));
    const r = await fetch('/api/research/hypothesis?' + qs.toString());
    return r.json();
  }

  async function apiSetStatus(id, status) {
    const r = await fetch('/api/research/hypothesis/' + id + '/status', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status }),
    });
    return r.json();
  }

  // ── Rendering helpers ─────────────────────────────────────────────
  function _escape(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _conditionsBlock(conds) {
    if (!conds || typeof conds !== 'object') return '<em>(none)</em>';
    const rows = [];
    for (const k of Object.keys(conds)) {
      const v = conds[k];
      if (v == null || v === '') continue;
      const val = (typeof v === 'object') ? JSON.stringify(v) : String(v);
      rows.push(
        '<div class="cond-row"><span class="cond-k">' + _escape(k) +
        '</span><span class="cond-v">' + _escape(val) + '</span></div>'
      );
    }
    return rows.length ? rows.join('') : '<em>(none)</em>';
  }

  function _predictionBlock(pred) {
    if (!pred || typeof pred !== 'object') return '<em>(none)</em>';
    const dir = pred.direction || '—';
    const mag = (pred.magnitude_pct != null) ? (pred.magnitude_pct + '%') : '—';
    const horizon = (pred.horizon_days != null) ? (pred.horizon_days + 'd') : '—';
    const conf = (pred.confidence != null && !isNaN(parseFloat(pred.confidence)))
      ? Math.round(parseFloat(pred.confidence) * 100) + '%'
      : '—';
    return (
      '<div class="pred-grid">' +
        '<div><span class="pred-k">Direction</span><span class="pred-v">' + _escape(dir) + '</span></div>' +
        '<div><span class="pred-k">Magnitude</span><span class="pred-v">' + _escape(mag) + '</span></div>' +
        '<div><span class="pred-k">Horizon</span><span class="pred-v">' + _escape(horizon) + '</span></div>' +
        '<div><span class="pred-k">Confidence</span><span class="pred-v">' + _escape(conf) + '</span></div>' +
      '</div>'
    );
  }

  function renderHypothesisPreview(card, hyp) {
    if (!card) return;
    if (!hyp || hyp.error) {
      card.innerHTML = '<div class="hyp-error">Error: ' + _escape((hyp && hyp.error) || 'unknown') + '</div>';
      return;
    }
    const tags = (hyp.tags || []).map(t => '<span class="hyp-tag">' + _escape(t) + '</span>').join('');
    card.innerHTML = (
      '<div class="hyp-title">' + _escape(hyp.title || 'Untitled hypothesis') + '</div>' +
      '<div class="hyp-symbol">' + _escape(hyp.symbol || '') + '</div>' +
      '<div class="hyp-section-label">PREDICTION</div>' +
      _predictionBlock(hyp.prediction) +
      '<div class="hyp-section-label">CONDITIONS</div>' +
      '<div class="hyp-cond">' + _conditionsBlock(hyp.conditions) + '</div>' +
      '<div class="hyp-section-label">RATIONALE</div>' +
      '<div class="hyp-rationale">' + _escape(hyp.rationale || '') + '</div>' +
      (tags ? '<div class="hyp-tags">' + tags + '</div>' : '') +
      '<div class="hyp-actions">' +
        '<button class="btn btn-primary" id="hyp-save-btn">SAVE HYPOTHESIS</button>' +
        '<button class="btn btn-secondary" id="hyp-discard-btn">DISCARD</button>' +
      '</div>'
    );

    const saveBtn = card.querySelector('#hyp-save-btn');
    const discardBtn = card.querySelector('#hyp-discard-btn');
    if (saveBtn) {
      saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = 'SAVING…';
        const res = await apiSave(hyp.symbol, hyp);
        if (res && res.id) {
          card.innerHTML = '<div class="hyp-ok">Saved (id=' + res.id + ')</div>';
          _refreshList();
        } else {
          saveBtn.disabled = false;
          saveBtn.textContent = 'SAVE HYPOTHESIS';
          card.insertAdjacentHTML('beforeend',
            '<div class="hyp-error">Save failed: ' + _escape((res && res.error) || '?') + '</div>');
        }
      });
    }
    if (discardBtn) {
      discardBtn.addEventListener('click', () => {
        card.innerHTML = '';
        _state.lastPreview = null;
      });
    }
  }

  // ── List table ─────────────────────────────────────────────────────
  function _rowHtml(h) {
    const status = h.status || 'OPEN';
    const cls = STATUS_COLORS[status] || '';
    const pred = h.prediction || {};
    const predTxt = (pred.direction || '?') + ' ≥' + (pred.magnitude_pct != null ? pred.magnitude_pct + '%' : '?') +
                    ' / ' + (pred.horizon_days || '?') + 'd';
    const realized = (h.realized_return != null) ? _fmt.pct(h.realized_return) : '—';
    const actions =
      '<div class="hyp-row-actions">' +
        '<button data-act="confirm" data-id="' + h.id + '" class="btn-mini">CONFIRM</button>' +
        '<button data-act="reject"  data-id="' + h.id + '" class="btn-mini">REJECT</button>' +
        '<button data-act="invalid" data-id="' + h.id + '" class="btn-mini">INVALIDATE</button>' +
      '</div>';
    return (
      '<tr>' +
        '<td>' + _escape(h.symbol) + '</td>' +
        '<td class="hyp-cell-title">' + _escape(h.title) + '</td>' +
        '<td>' + _fmt.date(h.created_at) + '</td>' +
        '<td>' + (h.horizon_days || '—') + 'd</td>' +
        '<td class="' + cls + '">' + _escape(status) + '</td>' +
        '<td>' + _escape(predTxt) + ' / realized ' + realized + '</td>' +
        '<td>' + actions + '</td>' +
      '</tr>'
    );
  }

  function _statusChips() {
    const opts = ['ALL', 'OPEN', 'CONFIRMED', 'REJECTED', 'INVALIDATED'];
    return opts.map(o => {
      const active = (o === (_state.statusFilter || 'ALL')) ? ' active' : '';
      const val = (o === 'ALL') ? '' : o;
      return '<button class="chip' + active + '" data-status="' + val + '">' + o + '</button>';
    }).join('');
  }

  async function _refreshList() {
    const tbody = _state.container && _state.container.querySelector('#hyp-list-body');
    const summary = _state.container && _state.container.querySelector('#hyp-list-summary');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="7" class="muted">Loading…</td></tr>';
    const list = await apiList({
      status: _state.statusFilter || undefined,
      symbol: _state.symbolFilter || undefined,
      limit: 100,
    });
    if (!Array.isArray(list)) {
      tbody.innerHTML = '<tr><td colspan="7" class="hyp-error">Error: ' +
        _escape((list && list.error) || 'load failed') + '</td></tr>';
      return;
    }
    if (list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="muted">No hypotheses yet — generate one above.</td></tr>';
    } else {
      tbody.innerHTML = list.map(_rowHtml).join('');
    }
    if (summary) {
      const total = list.length;
      const confirmed = list.filter(h => h.status === 'CONFIRMED').length;
      const rejected = list.filter(h => h.status === 'REJECTED').length;
      const closed = confirmed + rejected;
      const hit = closed ? Math.round((confirmed / closed) * 100) + '%' : '—';
      summary.textContent = 'Showing ' + total + ' / Confirmed ' + confirmed + ' / Rejected ' + rejected + ' / Hit rate ' + hit;
    }

    // Bind row action buttons
    tbody.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', async (ev) => {
        const id = btn.getAttribute('data-id');
        const act = btn.getAttribute('data-act');
        const newStatus =
          act === 'confirm' ? 'CONFIRMED' :
          act === 'reject'  ? 'REJECTED' :
          'INVALIDATED';
        btn.disabled = true;
        await apiSetStatus(id, newStatus);
        _refreshList();
      });
    });
  }

  // ── Top bar (symbol input + generate button) ──────────────────────
  function _topBarHtml() {
    return (
      '<div class="hyp-topbar">' +
        '<label class="hyp-label">GENERATE FOR:</label>' +
        '<input type="text" id="hyp-symbol-input" class="hyp-input" placeholder="AAPL" />' +
        '<button class="btn btn-primary" id="hyp-generate-btn">GENERATE</button>' +
        '<span class="hyp-spinner" id="hyp-spinner" style="display:none">…</span>' +
      '</div>' +
      '<div id="hyp-preview-card" class="hyp-preview-card"></div>'
    );
  }

  function _listHtml() {
    return (
      '<div class="hyp-list-header">' +
        '<h3 class="hyp-list-title">SAVED HYPOTHESES</h3>' +
        '<div class="hyp-list-filters">' +
          '<div class="hyp-chips" id="hyp-status-chips">' + _statusChips() + '</div>' +
          '<input type="text" id="hyp-symbol-filter" class="hyp-input" placeholder="filter symbol" />' +
        '</div>' +
        '<div class="hyp-list-summary" id="hyp-list-summary"></div>' +
      '</div>' +
      '<table class="hyp-table">' +
        '<thead><tr>' +
          '<th>Symbol</th><th>Title</th><th>Created</th><th>Horizon</th>' +
          '<th>Status</th><th>Predicted / Realized</th><th>Actions</th>' +
        '</tr></thead>' +
        '<tbody id="hyp-list-body"><tr><td colspan="7" class="muted">Loading…</td></tr></tbody>' +
      '</table>'
    );
  }

  function renderHypothesisLab(containerEl) {
    if (!containerEl) return;
    _state.container = containerEl;
    containerEl.innerHTML = (
      '<div class="research-lab-panel">' +
        '<h2 class="lab-title">RESEARCH LAB :: HYPOTHESIS SYNTHESIS</h2>' +
        '<div class="lab-blurb">Generate testable, multi-factor hypotheses from every research module. Saved hypotheses are auto-scored at their horizon.</div>' +
        _topBarHtml() +
        _listHtml() +
      '</div>'
    );

    // Bind: generate button
    const genBtn = containerEl.querySelector('#hyp-generate-btn');
    const input = containerEl.querySelector('#hyp-symbol-input');
    const spinner = containerEl.querySelector('#hyp-spinner');
    const previewCard = containerEl.querySelector('#hyp-preview-card');

    async function _doGenerate() {
      const sym = (input.value || '').trim().toUpperCase();
      if (!sym) { previewCard.innerHTML = '<div class="hyp-error">Enter a symbol first.</div>'; return; }
      previewCard.innerHTML = '<div class="muted">Synthesising hypothesis for ' + _escape(sym) + '…</div>';
      if (spinner) spinner.style.display = '';
      genBtn.disabled = true;
      try {
        const hyp = await apiGenerate(sym);
        _state.lastPreview = hyp;
        renderHypothesisPreview(previewCard, hyp);
      } catch (e) {
        previewCard.innerHTML = '<div class="hyp-error">Request failed: ' + _escape(String(e)) + '</div>';
      } finally {
        genBtn.disabled = false;
        if (spinner) spinner.style.display = 'none';
      }
    }
    if (genBtn) genBtn.addEventListener('click', _doGenerate);
    if (input) input.addEventListener('keydown', (e) => { if (e.key === 'Enter') _doGenerate(); });

    // Bind: status chips
    const chips = containerEl.querySelector('#hyp-status-chips');
    if (chips) {
      chips.addEventListener('click', (ev) => {
        const btn = ev.target.closest('.chip');
        if (!btn) return;
        _state.statusFilter = btn.getAttribute('data-status') || null;
        // Toggle active classes
        chips.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        _refreshList();
      });
    }

    // Bind: symbol filter
    const symFilter = containerEl.querySelector('#hyp-symbol-filter');
    if (symFilter) {
      let _t = null;
      symFilter.addEventListener('input', () => {
        clearTimeout(_t);
        _t = setTimeout(() => {
          _state.symbolFilter = (symFilter.value || '').trim().toUpperCase();
          _refreshList();
        }, 250);
      });
    }

    _refreshList();
  }

  // Expose on window for app.js to call.
  window.renderHypothesisLab = renderHypothesisLab;
  window.renderHypothesisPreview = renderHypothesisPreview;
})();
