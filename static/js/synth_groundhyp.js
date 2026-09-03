/* ================================================================
   AUGUR // SYNTH — Pattern-Grounded Hypothesis View
   ----------------------------------------------------------------
   Retrieves the current symbol's signal pattern + closest historical
   analogs from /api/synth/grounded-hypothesis/<sym> and renders:

     1. CURRENT PATTERN     — bar chart of normalised features in [-1, +1]
     2. ANCHOR CASES        — top-5 historical analogs with their outcomes
     3. BASE-RATE SUMMARY   — n, hit rate, avg realized return, IQR
     4. LLM HYPOTHESIS      — grounded refinement (or graceful "LLM
                              unavailable" placeholder)

   Exposes:   window.renderGroundedHypothesis(containerEl, symbol)

   No dependencies on charting libraries — uses inline <div> bars so
   it works whether or not LightweightCharts has loaded yet.
   ================================================================ */
(function () {
  'use strict';

  const FEATURE_ORDER = [
    'ml_prob_up',
    'smart_money',
    'gex_regime',
    'narrative_phase',
    'insider_form4_net',
    'factor_alpha',
    'price_momentum',
  ];

  const FEATURE_LABEL = {
    ml_prob_up:        'ML P(up 20d)',
    smart_money:       'Smart Money',
    gex_regime:        'GEX Regime',
    narrative_phase:   'Narrative Phase',
    insider_form4_net: 'Insider Form-4 Net',
    factor_alpha:      'Factor α',
    price_momentum:    'Price Momentum',
  };

  // ── utils ─────────────────────────────────────────────────────────
  function _esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  function _pct(v, d) {
    if (v == null || isNaN(parseFloat(v))) return '—';
    return parseFloat(v).toFixed(d == null ? 2 : d) + '%';
  }

  function _num(v, d) {
    if (v == null || isNaN(parseFloat(v))) return '—';
    return parseFloat(v).toFixed(d == null ? 2 : d);
  }

  function _dt(s) {
    if (!s) return '—';
    try { return new Date(s).toLocaleDateString(); } catch (e) { return String(s); }
  }

  function _statusColor(hit) {
    if (hit === true)  return 'col-green';
    if (hit === false) return 'col-red';
    return 'col-amber';
  }

  // ── API ───────────────────────────────────────────────────────────
  async function apiGroundedHypothesis(symbol) {
    const r = await fetch(
      '/api/synth/grounded-hypothesis/' + encodeURIComponent(symbol),
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }
    );
    if (!r.ok) {
      let body = null;
      try { body = await r.json(); } catch (e) { /* ignore */ }
      return { error: (body && body.error) || ('HTTP ' + r.status) };
    }
    return r.json();
  }

  // ── render: pattern bar chart ─────────────────────────────────────
  function _patternBars(vector) {
    if (!vector || typeof vector !== 'object') {
      return '<div class="gh-empty">No pattern features available.</div>';
    }
    const rows = FEATURE_ORDER.map(k => {
      const raw = vector[k];
      const v = (raw == null) ? null : Math.max(-1, Math.min(1, parseFloat(raw)));
      if (v == null) {
        return (
          '<div class="gh-bar-row gh-bar-missing">' +
            '<span class="gh-bar-label">' + _esc(FEATURE_LABEL[k] || k) + '</span>' +
            '<span class="gh-bar-track"><span class="gh-bar-zero"></span></span>' +
            '<span class="gh-bar-val gh-muted">n/a</span>' +
          '</div>'
        );
      }
      // Map [-1, +1] → 0..100% along the half-width track
      const pct = Math.round(Math.abs(v) * 50);
      const side = v >= 0 ? 'right' : 'left';
      const fillStyle =
        side === 'right'
          ? 'left:50%;width:' + pct + '%;background:#3da673'
          : 'left:' + (50 - pct) + '%;width:' + pct + '%;background:#c75b5b';
      return (
        '<div class="gh-bar-row">' +
          '<span class="gh-bar-label">' + _esc(FEATURE_LABEL[k] || k) + '</span>' +
          '<span class="gh-bar-track">' +
            '<span class="gh-bar-zero"></span>' +
            '<span class="gh-bar-fill" style="' + fillStyle + '"></span>' +
          '</span>' +
          '<span class="gh-bar-val">' + (v >= 0 ? '+' : '') + v.toFixed(2) + '</span>' +
        '</div>'
      );
    }).join('');
    return '<div class="gh-bars">' + rows + '</div>';
  }

  // ── render: anchor cases table ────────────────────────────────────
  function _analogsTable(analogs) {
    if (!Array.isArray(analogs) || analogs.length === 0) {
      return (
        '<div class="gh-empty">' +
          'No historical analogs found yet. The signal-forecast ledger '+
          'needs at least a handful of scored calls before retrieval '+
          'becomes meaningful.' +
        '</div>'
      );
    }
    const rows = analogs.map(a => {
      const o = a.outcome || {};
      const hit = o.hit;
      const ret = (o.realized_return_pct != null) ? _pct(o.realized_return_pct) : '—';
      const hitLbl =
        hit === true ? 'HIT' :
        hit === false ? 'MISS' :
        'n/a';
      return (
        '<tr>' +
          '<td>' + _esc(a.symbol) + '</td>' +
          '<td>' + _esc(a.signal_name || '—') + '</td>' +
          '<td>' + _dt(a.issued_at) + '</td>' +
          '<td>' + _esc(a.horizon_days != null ? a.horizon_days + 'd' : '—') + '</td>' +
          '<td>' + _num(a.pattern_distance, 3) + '</td>' +
          '<td>' + _esc(o.direction || '—') + '</td>' +
          '<td class="' + _statusColor(hit) + '">' + hitLbl + ' / ' + ret + '</td>' +
        '</tr>'
      );
    }).join('');
    return (
      '<table class="gh-analogs">' +
        '<thead><tr>' +
          '<th>Symbol</th><th>Signal</th><th>Issued</th>' +
          '<th>Horizon</th><th>Distance</th><th>Direction</th>' +
          '<th>Outcome</th>' +
        '</tr></thead>' +
        '<tbody>' + rows + '</tbody>' +
      '</table>'
    );
  }

  // ── render: base rate ─────────────────────────────────────────────
  function _baseRateCard(b) {
    if (!b || typeof b !== 'object') return '';
    const n = b.n_analogs != null ? b.n_analogs : 0;
    const hit = (b.hit_rate != null) ? Math.round(b.hit_rate * 100) + '%' : '—';
    const avg = (b.avg_realized_return_pct != null) ? _pct(b.avg_realized_return_pct) : '—';
    const iqr = (Array.isArray(b.iqr_realized) && b.iqr_realized.length === 2)
      ? '[' + _pct(b.iqr_realized[0]) + ' , ' + _pct(b.iqr_realized[1]) + ']'
      : '—';
    return (
      '<div class="gh-baserate">' +
        '<div><span class="gh-k">Analogs</span><span class="gh-v">' + n + '</span></div>' +
        '<div><span class="gh-k">Hit rate</span><span class="gh-v">' + hit + '</span></div>' +
        '<div><span class="gh-k">Avg realized</span><span class="gh-v">' + avg + '</span></div>' +
        '<div><span class="gh-k">IQR realized</span><span class="gh-v">' + iqr + '</span></div>' +
      '</div>'
    );
  }

  // ── render: LLM hypothesis card ───────────────────────────────────
  function _hypothesisCard(hyp, llmStatus) {
    if (!hyp) {
      const msg = llmStatus === 'no_api_key'
        ? 'LLM unavailable — set OPENAI_API_KEY to enable grounded synthesis.'
        : llmStatus === 'daily_cap_exceeded'
          ? 'LLM unavailable — daily AI request cap reached.'
          : 'LLM unavailable: ' + _esc(llmStatus || 'unknown');
      return '<div class="gh-llm-empty">' + msg + '</div>';
    }
    const pred = hyp.prediction || {};
    const conds = hyp.conditions || {};
    const condRows = Object.keys(conds)
      .filter(k => conds[k] != null && conds[k] !== '')
      .map(k => {
        const v = conds[k];
        const txt = (typeof v === 'object') ? JSON.stringify(v) : String(v);
        return '<div class="gh-cond"><span class="gh-k">' + _esc(k) +
               '</span><span class="gh-v">' + _esc(txt) + '</span></div>';
      }).join('');
    const tags = (hyp.tags || []).map(t =>
      '<span class="gh-tag">' + _esc(t) + '</span>'
    ).join('');
    const conf = (pred.confidence != null)
      ? Math.round(parseFloat(pred.confidence) * 100) + '%' : '—';
    return (
      '<div class="gh-hyp-card">' +
        '<div class="gh-hyp-title">' + _esc(hyp.title || 'Untitled hypothesis') + '</div>' +
        '<div class="gh-hyp-meta">' +
          '<span class="gh-hyp-dir">' + _esc(pred.direction || '—') + '</span> · ' +
          '<span>≥ ' + _esc(pred.magnitude_pct != null ? pred.magnitude_pct + '%' : '—') + '</span> · ' +
          '<span>' + _esc(pred.horizon_days != null ? pred.horizon_days + 'd horizon' : '—') + '</span> · ' +
          '<span>conf ' + conf + '</span>' +
        '</div>' +
        (condRows ? '<div class="gh-hyp-conds">' + condRows + '</div>' : '') +
        '<div class="gh-hyp-rationale">' + _esc(hyp.rationale || '') + '</div>' +
        (tags ? '<div class="gh-hyp-tags">' + tags + '</div>' : '') +
        '<div class="gh-hyp-actions">' +
          '<button class="btn btn-primary" data-act="save-grounded">SAVE HYPOTHESIS</button>' +
        '</div>' +
      '</div>'
    );
  }

  // ── render: top-level ─────────────────────────────────────────────
  function _scaffold(symbol) {
    return (
      '<div class="research-lab-panel gh-panel">' +
        '<h2 class="lab-title">SYNTH :: PATTERN-GROUNDED HYPOTHESIS</h2>' +
        '<div class="lab-blurb">' +
          'Looks up the closest historical analogs in the signal-forecast ledger, ' +
          'computes their realized outcomes, then asks the LLM to refine a ' +
          'falsifiable hypothesis grounded in those cases.' +
        '</div>' +
        '<div class="gh-topbar">' +
          '<label class="hyp-label">SYMBOL:</label>' +
          '<input type="text" id="gh-symbol-input" class="hyp-input" value="' + _esc(symbol || '') + '" placeholder="AAPL" />' +
          '<button class="btn btn-primary" id="gh-run-btn">RUN</button>' +
          '<span class="hyp-spinner" id="gh-spinner" style="display:none">…</span>' +
        '</div>' +
        '<div id="gh-result-root"></div>' +
      '</div>'
    );
  }

  function _renderResult(rootEl, data) {
    if (!rootEl) return;
    if (!data || data.error) {
      rootEl.innerHTML = '<div class="hyp-error">Error: ' +
        _esc((data && data.error) || 'unknown') + '</div>';
      return;
    }
    rootEl.innerHTML = (
      '<div class="gh-section">' +
        '<h3 class="gh-section-title">CURRENT PATTERN — ' + _esc(data.symbol || '') + '</h3>' +
        _patternBars(data.current_pattern) +
      '</div>' +
      '<div class="gh-section">' +
        '<h3 class="gh-section-title">ANCHOR CASES (top ' + (data.anchor_cases || []).length + ')</h3>' +
        _analogsTable(data.anchor_cases) +
      '</div>' +
      '<div class="gh-section">' +
        '<h3 class="gh-section-title">BASE-RATE SUMMARY</h3>' +
        _baseRateCard(data.base_rate_summary) +
      '</div>' +
      '<div class="gh-section">' +
        '<h3 class="gh-section-title">GROUNDED HYPOTHESIS</h3>' +
        _hypothesisCard(data.hypothesis, data.llm_status) +
      '</div>' +
      '<div class="gh-asof muted">as of ' + _dt(data.as_of) +
        (data.computed_in_ms != null ? ' · ' + data.computed_in_ms + ' ms' : '') +
      '</div>'
    );

    // Save-grounded button — re-uses the existing /api/research/hypothesis/save
    // endpoint so the grounded payload lives in the same hypotheses table.
    const saveBtn = rootEl.querySelector('button[data-act="save-grounded"]');
    if (saveBtn && data.hypothesis) {
      saveBtn.addEventListener('click', async () => {
        saveBtn.disabled = true;
        saveBtn.textContent = 'SAVING…';
        try {
          const r = await fetch('/api/research/hypothesis/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              symbol: data.symbol,
              hypothesis: data.hypothesis,
            }),
          });
          const js = await r.json();
          if (js && js.id) {
            saveBtn.textContent = 'SAVED (id ' + js.id + ')';
          } else {
            saveBtn.textContent = 'SAVE FAILED';
            saveBtn.disabled = false;
          }
        } catch (e) {
          saveBtn.textContent = 'SAVE FAILED';
          saveBtn.disabled = false;
        }
      });
    }
  }

  async function _run(rootEl, symbol, spinner, btn) {
    if (!symbol) {
      rootEl.innerHTML = '<div class="hyp-error">Enter a symbol first.</div>';
      return;
    }
    rootEl.innerHTML = '<div class="muted">Retrieving analogs and synthesising hypothesis for ' +
      _esc(symbol) + '…</div>';
    if (spinner) spinner.style.display = '';
    if (btn) btn.disabled = true;
    try {
      const data = await apiGroundedHypothesis(symbol);
      _renderResult(rootEl, data);
    } catch (e) {
      rootEl.innerHTML = '<div class="hyp-error">Request failed: ' +
        _esc(String(e)) + '</div>';
    } finally {
      if (spinner) spinner.style.display = 'none';
      if (btn) btn.disabled = false;
    }
  }

  function renderGroundedHypothesis(containerEl, symbol) {
    if (!containerEl) return;
    containerEl.innerHTML = _scaffold(symbol);
    const input = containerEl.querySelector('#gh-symbol-input');
    const btn = containerEl.querySelector('#gh-run-btn');
    const spinner = containerEl.querySelector('#gh-spinner');
    const resultRoot = containerEl.querySelector('#gh-result-root');

    // In-flight guard: the Enter-key handler bypasses the disabled RUN button,
    // so without it two slow LLM-backed requests could race and the stale
    // first response could overwrite the newer symbol's result panel.
    let _inflight = false;
    function _go() {
      if (_inflight) return;
      const sym = ((input && input.value) || '').trim().toUpperCase();
      _inflight = true;
      _run(resultRoot, sym, spinner, btn).finally(() => { _inflight = false; });
    }
    if (btn) btn.addEventListener('click', _go);
    if (input) input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') _go();
    });
    if (symbol) _go();
  }

  // Minimal inline styles so the component looks correct even before the
  // host page ships matching CSS rules. Idempotent (only injects once).
  function _injectStyles() {
    if (document.getElementById('synth-groundhyp-style')) return;
    const css = (
      '.gh-panel{padding:14px}' +
      '.gh-topbar{display:flex;gap:8px;align-items:center;margin:12px 0 18px}' +
      '.gh-section{margin:18px 0;padding-bottom:8px;border-bottom:1px solid #0f2a3a}' +
      '.gh-section-title{margin:0 0 10px;font-size:11px;letter-spacing:0.08em;color:#7aa7b9;text-transform:uppercase}' +
      '.gh-bars{display:flex;flex-direction:column;gap:4px}' +
      '.gh-bar-row{display:grid;grid-template-columns:160px 1fr 60px;gap:8px;align-items:center;font-size:12px}' +
      '.gh-bar-label{color:#9fb9c6}' +
      '.gh-bar-track{position:relative;height:14px;background:#0a1f2a;border:1px solid #143040;border-radius:2px}' +
      '.gh-bar-zero{position:absolute;left:50%;top:0;bottom:0;width:1px;background:#2a4a5a}' +
      '.gh-bar-fill{position:absolute;top:1px;bottom:1px;border-radius:1px}' +
      '.gh-bar-val{font-family:monospace;text-align:right;color:#d7e6ee}' +
      '.gh-bar-missing .gh-bar-val{color:#5a7180}' +
      '.gh-analogs{width:100%;border-collapse:collapse;font-size:12px}' +
      '.gh-analogs th{text-align:left;padding:6px;color:#7aa7b9;border-bottom:1px solid #143040;font-weight:normal}' +
      '.gh-analogs td{padding:6px;border-bottom:1px dotted #102b3a;color:#d7e6ee}' +
      '.gh-baserate{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}' +
      '.gh-baserate>div{padding:8px;background:#0a1f2a;border:1px solid #143040;border-radius:3px}' +
      '.gh-baserate .gh-k{display:block;font-size:10px;letter-spacing:0.08em;color:#7aa7b9;text-transform:uppercase}' +
      '.gh-baserate .gh-v{display:block;font-size:18px;color:#d7e6ee;margin-top:4px}' +
      '.gh-hyp-card{padding:12px;background:#081a26;border:1px solid #143040;border-radius:3px}' +
      '.gh-hyp-title{font-size:14px;color:#d7e6ee;margin-bottom:6px}' +
      '.gh-hyp-meta{font-size:11px;color:#9fb9c6;margin-bottom:10px}' +
      '.gh-hyp-meta .gh-hyp-dir{color:#3da673;font-weight:bold}' +
      '.gh-hyp-conds{margin:8px 0;display:flex;flex-direction:column;gap:2px}' +
      '.gh-cond{display:flex;justify-content:space-between;font-size:11px;color:#7aa7b9}' +
      '.gh-cond .gh-v{color:#d7e6ee}' +
      '.gh-hyp-rationale{font-size:12px;color:#d7e6ee;line-height:1.5;margin:8px 0}' +
      '.gh-hyp-tags{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}' +
      '.gh-tag{font-size:10px;padding:2px 6px;background:#143040;color:#9fb9c6;border-radius:2px}' +
      '.gh-hyp-actions{margin-top:10px;display:flex;gap:8px}' +
      '.gh-llm-empty{padding:14px;background:#0a1f2a;border:1px dashed #143040;color:#9fb9c6;font-size:12px;border-radius:3px}' +
      '.gh-empty{padding:14px;background:#0a1f2a;border:1px dashed #143040;color:#7aa7b9;font-size:12px;border-radius:3px}' +
      '.gh-asof{margin-top:10px;font-size:10px;color:#5a7180;text-align:right}' +
      '.col-green{color:#3da673}.col-red{color:#c75b5b}.col-amber{color:#d39a3a}' +
      '.gh-muted{color:#5a7180}'
    );
    const tag = document.createElement('style');
    tag.id = 'synth-groundhyp-style';
    tag.appendChild(document.createTextNode(css));
    document.head.appendChild(tag);
  }
  _injectStyles();

  // Expose for app.js to call.
  window.renderGroundedHypothesis = renderGroundedHypothesis;
})();
