/**
 * research_optimizer.js — front-end for the Portfolio Optimizer panel.
 *
 * Public entry point:
 *   renderOptimizer(containerEl, opts?)
 *     opts.symbols      — string[] seed tickers (defaults to portfolio holdings)
 *     opts.currentWeights — { TICKER: weight } current allocation (optional)
 *
 * The panel POSTs to /api/research/optimize and renders:
 *   - KPIs (expected return / vol / Sharpe / tracking error)
 *   - Two-column current-vs-recommended weights table, sorted by |delta|
 *   - Efficient-frontier scatter (Chart.js) for Markowitz / target objectives
 *
 * No build step — written in vanilla ES2017 so it loads as a plain <script>.
 */

(function () {
  'use strict';

  const POST_PATH = '/api/research/optimize';

  // ── small DOM helpers ───────────────────────────────────────────────────
  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      for (const k of Object.keys(attrs)) {
        const v = attrs[k];
        if (k === 'style' && v && typeof v === 'object') {
          Object.assign(node.style, v);
        } else if (k === 'class' || k === 'className') {
          node.className = v;
        } else if (k.startsWith('on') && typeof v === 'function') {
          node.addEventListener(k.slice(2).toLowerCase(), v);
        } else if (v != null) {
          node.setAttribute(k, v);
        }
      }
    }
    (children || []).forEach((c) => {
      if (c == null) return;
      if (typeof c === 'string') node.appendChild(document.createTextNode(c));
      else node.appendChild(c);
    });
    return node;
  }

  function fmtPct(x, digits) {
    if (x == null || Number.isNaN(x)) return '–';
    return Number(x).toFixed(digits == null ? 2 : digits) + '%';
  }
  function fmtNum(x, digits) {
    if (x == null || Number.isNaN(x)) return '–';
    return Number(x).toFixed(digits == null ? 3 : digits);
  }

  // ── view state (one-shot per renderOptimizer call) ──────────────────────
  function makeState(seedSymbols, currentWeights) {
    return {
      symbols: (seedSymbols || []).slice(),
      currentWeights: Object.assign({}, currentWeights || {}),
      objective: 'max_sharpe',
      constraints: {
        min_weight: 0,
        max_weight: 1,
        long_only: true,
        target_return: null,
        target_vol: null,
        risk_free_rate: 0.045,
      },
      views: [], // each: {symbolA, symbolB, by_pct, confidence_pct, relative}
      lastResult: null,
      mktCaps: {}, // optional per-symbol cap override for BL
    };
  }

  // ── controls section ────────────────────────────────────────────────────
  function renderControls(state, onSubmit) {
    const wrap = el('div', { class: 'optimizer-controls', style: {
      display: 'grid', gap: '12px',
      gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
      padding: '12px', border: '1px solid var(--border, #444)',
      borderRadius: '6px', background: 'var(--panel, #1a1a1a)',
    } });

    // ── Symbols input ──
    const symBox = el('div');
    symBox.appendChild(el('label', { style: { fontSize: '11px', opacity: 0.7 } }, ['UNIVERSE (comma-separated tickers)']));
    const symIn = el('input', {
      type: 'text', value: state.symbols.join(', '),
      style: { width: '100%', padding: '6px 8px', marginTop: '4px', boxSizing: 'border-box' },
      placeholder: 'AAPL, MSFT, GLD, SPY, TLT',
    });
    symIn.addEventListener('input', () => {
      state.symbols = symIn.value.split(/[\s,]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
    });
    symBox.appendChild(symIn);
    wrap.appendChild(symBox);

    // ── Objective selector ──
    const objBox = el('div');
    objBox.appendChild(el('label', { style: { fontSize: '11px', opacity: 0.7 } }, ['OBJECTIVE']));
    const objSel = el('select', {
      style: { width: '100%', padding: '6px 8px', marginTop: '4px', boxSizing: 'border-box' },
    });
    [
      ['max_sharpe', 'Max Sharpe (Markowitz)'],
      ['min_vol', 'Min Volatility (Markowitz)'],
      ['target_return', 'Target Return (Markowitz)'],
      ['target_vol', 'Target Vol (Markowitz)'],
      ['risk_parity', 'Risk Parity (ERC)'],
      ['black_litterman', 'Black-Litterman'],
    ].forEach(([v, l]) => objSel.appendChild(el('option', { value: v }, [l])));
    objSel.value = state.objective;
    objSel.addEventListener('change', () => {
      state.objective = objSel.value;
      renderConditionalFields();
    });
    objBox.appendChild(objSel);
    wrap.appendChild(objBox);

    // ── Common numeric constraints ──
    function num(label, key, step, val) {
      const b = el('div');
      b.appendChild(el('label', { style: { fontSize: '11px', opacity: 0.7 } }, [label]));
      const i = el('input', {
        type: 'number', step: step, value: val == null ? '' : val,
        style: { width: '100%', padding: '6px 8px', marginTop: '4px', boxSizing: 'border-box' },
      });
      i.addEventListener('input', () => {
        const x = i.value === '' ? null : parseFloat(i.value);
        state.constraints[key] = (x == null || Number.isNaN(x)) ? null : x;
      });
      b.appendChild(i);
      return { box: b, input: i };
    }
    wrap.appendChild(num('MIN WEIGHT (per asset)', 'min_weight', '0.01', state.constraints.min_weight).box);
    wrap.appendChild(num('MAX WEIGHT (per asset)', 'max_weight', '0.01', state.constraints.max_weight).box);
    wrap.appendChild(num('RISK-FREE RATE (decimal)', 'risk_free_rate', '0.005', state.constraints.risk_free_rate).box);

    // ── Conditional fields (target ret/vol + BL views table) ──
    const condHost = el('div', { style: { gridColumn: '1 / -1' } });
    wrap.appendChild(condHost);

    function renderConditionalFields() {
      condHost.innerHTML = '';
      if (state.objective === 'target_return') {
        condHost.appendChild(num('TARGET RETURN (decimal, e.g. 0.12)', 'target_return', '0.01', state.constraints.target_return).box);
      } else if (state.objective === 'target_vol') {
        condHost.appendChild(num('TARGET VOL (decimal, e.g. 0.15)', 'target_vol', '0.01', state.constraints.target_vol).box);
      } else if (state.objective === 'black_litterman') {
        condHost.appendChild(renderViewsEditor(state));
      }
    }
    renderConditionalFields();

    // ── Submit ──
    const submitBox = el('div', { style: { gridColumn: '1 / -1', display: 'flex', gap: '8px', alignItems: 'center' } });
    const btn = el('button', {
      type: 'button',
      style: {
        padding: '8px 16px', background: 'var(--accent, #3a7bd5)',
        color: '#fff', border: 'none', borderRadius: '4px', cursor: 'pointer', fontWeight: 600,
      },
    }, ['OPTIMIZE']);
    btn.addEventListener('click', () => onSubmit());
    submitBox.appendChild(btn);

    const status = el('span', { id: 'opt-status', style: { fontSize: '12px', opacity: 0.7 } }, []);
    submitBox.appendChild(status);
    wrap.appendChild(submitBox);

    return wrap;
  }

  // ── Black-Litterman views editor ────────────────────────────────────────
  function renderViewsEditor(state) {
    const host = el('div', { style: {
      border: '1px dashed var(--border, #444)', borderRadius: '6px', padding: '10px',
    } });
    host.appendChild(el('div', { style: { fontWeight: 600, fontSize: '12px', marginBottom: '6px' } },
      ['BLACK-LITTERMAN VIEWS — e.g. "NVDA outperforms SPY by 8% (70% confident)"']));

    const table = el('table', { style: { width: '100%', fontSize: '12px', borderCollapse: 'collapse' } });
    const thead = el('thead', null, [
      el('tr', null, ['Symbol A', 'Relation', 'Symbol B', 'By (%)', 'Confidence (%)', ''].map(
        (h) => el('th', { style: { textAlign: 'left', padding: '4px', borderBottom: '1px solid var(--border,#444)' } }, [h]))),
    ]);
    table.appendChild(thead);
    const tbody = el('tbody');
    table.appendChild(tbody);
    host.appendChild(table);

    function rerender() {
      tbody.innerHTML = '';
      state.views.forEach((v, idx) => {
        const tr = el('tr');
        function cell(child) {
          const td = el('td', { style: { padding: '3px' } });
          td.appendChild(child);
          return td;
        }
        const symA = el('input', { type: 'text', value: v.symbolA || '', style: { width: '70px' } });
        symA.addEventListener('input', () => { v.symbolA = symA.value.toUpperCase().trim(); });

        const rel = el('select');
        ['outperforms', 'returns (absolute)'].forEach((opt, i) => {
          rel.appendChild(el('option', { value: i === 0 ? 'relative' : 'absolute' }, [opt]));
        });
        rel.value = v.relative ? 'relative' : 'absolute';
        rel.addEventListener('change', () => { v.relative = (rel.value === 'relative'); rerender(); });

        const symB = el('input', { type: 'text', value: v.symbolB || '', style: { width: '70px' } });
        symB.addEventListener('input', () => { v.symbolB = symB.value.toUpperCase().trim(); });
        symB.disabled = !v.relative;

        const byVal = el('input', { type: 'number', step: '0.5', value: v.by_pct == null ? '' : v.by_pct, style: { width: '70px' } });
        byVal.addEventListener('input', () => { v.by_pct = byVal.value === '' ? null : parseFloat(byVal.value); });

        const confVal = el('input', { type: 'number', step: '5', min: '1', max: '99', value: v.confidence_pct == null ? 70 : v.confidence_pct, style: { width: '70px' } });
        confVal.addEventListener('input', () => { v.confidence_pct = confVal.value === '' ? null : parseFloat(confVal.value); });

        const rm = el('button', { type: 'button', style: { padding: '2px 8px', cursor: 'pointer' } }, ['×']);
        rm.addEventListener('click', () => { state.views.splice(idx, 1); rerender(); });

        tr.appendChild(cell(symA));
        tr.appendChild(cell(rel));
        tr.appendChild(cell(symB));
        tr.appendChild(cell(byVal));
        tr.appendChild(cell(confVal));
        tr.appendChild(cell(rm));
        tbody.appendChild(tr);
      });
    }

    const addBtn = el('button', { type: 'button', style: { marginTop: '6px', padding: '4px 10px', cursor: 'pointer' } }, ['+ ADD VIEW']);
    addBtn.addEventListener('click', () => {
      state.views.push({ symbolA: '', symbolB: '', by_pct: 5.0, confidence_pct: 70, relative: true });
      rerender();
    });
    host.appendChild(addBtn);

    // Mkt-cap inputs (optional). Provided as a compact textarea for speed.
    const capBox = el('div', { style: { marginTop: '10px' } });
    capBox.appendChild(el('div', { style: { fontSize: '11px', opacity: 0.7 } },
      ['MARKET CAPS (optional, "TICKER=cap" per line; default = equal weight)']));
    const capTa = el('textarea', { rows: 3, style: { width: '100%', fontSize: '12px', fontFamily: 'monospace', boxSizing: 'border-box' } });
    capTa.addEventListener('input', () => {
      const obj = {};
      capTa.value.split('\n').forEach((ln) => {
        const m = ln.match(/^\s*([A-Z0-9.\-]+)\s*=\s*([0-9.eE+-]+)\s*$/i);
        if (m) obj[m[1].toUpperCase()] = parseFloat(m[2]);
      });
      state.mktCaps = obj;
    });
    capBox.appendChild(capTa);
    host.appendChild(capBox);

    rerender();
    return host;
  }

  // ── Result rendering ────────────────────────────────────────────────────
  function renderKPIs(result, compareResult) {
    const wrap = el('div', { style: {
      display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '8px', margin: '12px 0',
    } });
    const tiles = [
      ['Expected Return', fmtPct(result.expected_return_pct)],
      ['Expected Vol', fmtPct(result.expected_vol_pct)],
      ['Sharpe', fmtNum(result.sharpe, 3)],
      ['Tracking Error', compareResult && compareResult.tracking_error_pct != null
        ? fmtPct(compareResult.tracking_error_pct) : '–'],
      ['Objective', String(result.objective || '–')],
      ['# Symbols', String((result.symbols || []).length)],
    ];
    tiles.forEach(([k, v]) => {
      const t = el('div', { style: {
        padding: '10px', background: 'var(--panel-2, #232323)', border: '1px solid var(--border,#444)', borderRadius: '4px',
      } }, [
        el('div', { style: { fontSize: '10px', opacity: 0.6, textTransform: 'uppercase' } }, [k]),
        el('div', { style: { fontSize: '18px', fontWeight: 600, marginTop: '4px' } }, [v]),
      ]);
      wrap.appendChild(t);
    });
    return wrap;
  }

  function renderWeightsTable(result, currentWeights, compareResult) {
    const rec = result.weights || {};
    const syms = Array.from(new Set([...Object.keys(rec), ...Object.keys(currentWeights || {})]));
    const rows = syms.map((s) => {
      const cur = (currentWeights || {})[s] || 0;
      const opt = rec[s] || 0;
      const d = (compareResult && compareResult.delta && compareResult.delta[s] != null)
        ? compareResult.delta[s] : (opt - cur);
      return { sym: s, cur, opt, delta: d };
    }).sort((a, b) => Math.abs(b.delta) - Math.abs(a.delta));

    const table = el('table', { style: { width: '100%', fontSize: '13px', borderCollapse: 'collapse', marginTop: '8px' } });
    const head = el('thead', null, [
      el('tr', null, ['Symbol', 'Current', 'Recommended', 'Δ'].map(
        (h) => el('th', { style: { textAlign: 'right', padding: '6px', borderBottom: '1px solid var(--border,#444)' } }, [h]))),
    ]);
    head.firstChild.firstChild.style.textAlign = 'left';
    table.appendChild(head);

    const body = el('tbody');
    rows.forEach((r) => {
      const tr = el('tr');
      const dColor = r.delta > 0.001 ? '#3ec46d' : (r.delta < -0.001 ? '#e15a5a' : 'inherit');
      function cell(text, align, color) {
        return el('td', {
          style: { padding: '4px 6px', textAlign: align || 'right',
            borderBottom: '1px solid var(--border-soft, #333)', color: color || 'inherit' },
        }, [text]);
      }
      tr.appendChild(cell(r.sym, 'left'));
      tr.appendChild(cell(fmtPct(r.cur * 100)));
      tr.appendChild(cell(fmtPct(r.opt * 100)));
      tr.appendChild(cell((r.delta >= 0 ? '+' : '') + fmtPct(r.delta * 100), 'right', dColor));
      body.appendChild(tr);
    });
    table.appendChild(body);
    return table;
  }

  function renderFrontier(result) {
    const frontier = result.efficient_frontier || [];
    const cur = { x: result.expected_vol_pct, y: result.expected_return_pct };
    if (!frontier.length || typeof window.Chart !== 'function') {
      return el('div', { style: { fontSize: '12px', opacity: 0.6, padding: '12px' } },
        [frontier.length ? '(Chart.js not loaded — frontier omitted)' : '(efficient frontier unavailable for this objective)']);
    }
    const wrap = el('div', { style: { marginTop: '12px', height: '320px', position: 'relative' } });
    const canvas = el('canvas');
    wrap.appendChild(canvas);
    // Defer chart creation until after the canvas is inserted into the DOM.
    setTimeout(() => {
      try {
        // Destroy any prior chart bound to this canvas before creating a new
        // one. Without this, every re-render (each OPTIMIZE click) leaks a
        // Chart.js instance and the canvas accumulates listeners until the
        // tab is closed.
        if (canvas._chart && typeof canvas._chart.destroy === 'function') {
          try { canvas._chart.destroy(); } catch (e) { /* ignore */ }
        }
        canvas._chart = new window.Chart(canvas.getContext('2d'), {
          type: 'scatter',
          data: {
            datasets: [
              {
                label: 'Efficient Frontier',
                data: frontier.map((p) => ({ x: p.vol, y: p.ret })),
                showLine: true,
                pointRadius: 3,
                borderColor: '#3a7bd5',
                backgroundColor: 'rgba(58,123,213,0.3)',
              },
              {
                label: 'Optimal Portfolio',
                data: [cur],
                pointRadius: 8,
                pointStyle: 'rectRot',
                borderColor: '#f0a020',
                backgroundColor: '#f0a020',
              },
            ],
          },
          options: {
            maintainAspectRatio: false,
            scales: {
              x: { title: { display: true, text: 'Volatility (%)' } },
              y: { title: { display: true, text: 'Expected Return (%)' } },
            },
            plugins: { legend: { position: 'top' } },
          },
        });
      } catch (e) {
        console.warn('frontier chart failed', e);
      }
    }, 0);
    return wrap;
  }

  // ── Network call ─────────────────────────────────────────────────────────
  async function postOptimize(state) {
    const body = {
      symbols: state.symbols,
      objective: state.objective,
      constraints: Object.assign({}, state.constraints),
      current_weights: state.currentWeights,
    };
    if (state.objective === 'black_litterman') {
      body.views = state.views
        .filter((v) => v.symbolA && (v.relative ? v.symbolB : true) && v.by_pct != null)
        .map((v) => ({
          type: v.relative ? 'relative' : 'absolute',
          symbols: v.relative ? [v.symbolA, v.symbolB] : [v.symbolA],
          expected_excess_return_pct: v.by_pct,
        }));
      body.view_confidence = state.views
        .filter((v) => v.symbolA && (v.relative ? v.symbolB : true) && v.by_pct != null)
        .map((v) => (v.confidence_pct == null ? 0.7 : v.confidence_pct / 100));
      body.mkt_caps = state.mktCaps || {};
    }

    const resp = await fetch(POST_PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const txt = await resp.text().catch(() => '');
      throw new Error('HTTP ' + resp.status + ': ' + txt.slice(0, 200));
    }
    return resp.json();
  }

  // ── Entry point ──────────────────────────────────────────────────────────
  function renderOptimizer(containerEl, opts) {
    if (!containerEl) return;
    opts = opts || {};
    const state = makeState(opts.symbols, opts.currentWeights);
    containerEl.innerHTML = '';

    const header = el('div', { style: { padding: '12px 12px 0 12px' } }, [
      el('div', { style: { fontWeight: 700, fontSize: '16px', letterSpacing: '0.05em' } }, ['PORTFOLIO OPTIMIZER']),
      el('div', { style: { fontSize: '12px', opacity: 0.7, marginTop: '2px' } },
        ['Mean-variance, Black-Litterman, and risk parity in one panel.']),
    ]);
    containerEl.appendChild(header);

    const body = el('div', { style: { padding: '12px' } });
    containerEl.appendChild(body);

    const resultsHost = el('div', { id: 'opt-results' });

    async function submit() {
      const status = document.getElementById('opt-status');
      if (status) status.textContent = 'optimizing…';
      resultsHost.innerHTML = '';
      try {
        const data = await postOptimize(state);
        state.lastResult = data;
        if (data.error) {
          resultsHost.appendChild(el('div', { style: { color: '#e15a5a', padding: '12px' } },
            ['Error: ' + data.error]));
        } else {
          const opt = data.optimal || {};
          const cmp = data.compare || {};
          resultsHost.appendChild(renderKPIs(opt, cmp));
          const grid = el('div', { style: {
            display: 'grid', gridTemplateColumns: 'minmax(260px, 1fr) 2fr', gap: '16px', alignItems: 'start',
          } });
          grid.appendChild(renderWeightsTable(opt, state.currentWeights, cmp));
          grid.appendChild(renderFrontier(opt));
          resultsHost.appendChild(grid);

          // Render the BL diagnostic block if present.
          if (opt.blended_returns && opt.prior_returns) {
            resultsHost.appendChild(renderBLDiagnostics(opt));
          }
          if (opt.marginal_contributions) {
            resultsHost.appendChild(renderRiskParityDiagnostics(opt));
          }
        }
        if (status) status.textContent = 'done';
      } catch (e) {
        if (status) status.textContent = 'failed';
        resultsHost.appendChild(el('div', { style: { color: '#e15a5a', padding: '12px' } },
          ['Request failed: ' + (e && e.message ? e.message : String(e))]));
      }
    }

    body.appendChild(renderControls(state, submit));
    body.appendChild(resultsHost);
  }

  function renderBLDiagnostics(opt) {
    const host = el('div', { style: { marginTop: '16px' } });
    host.appendChild(el('div', { style: { fontSize: '12px', fontWeight: 600, opacity: 0.8, marginBottom: '4px' } },
      ['BLACK-LITTERMAN: PRIOR vs BLENDED RETURNS']));
    const t = el('table', { style: { width: '100%', fontSize: '12px', borderCollapse: 'collapse' } });
    const head = el('thead', null, [el('tr', null, ['Symbol', 'Prior %', 'Blended %', 'Δ'].map(
      (h) => el('th', { style: { textAlign: 'right', padding: '4px', borderBottom: '1px solid var(--border,#444)' } }, [h])))]);
    head.firstChild.firstChild.style.textAlign = 'left';
    t.appendChild(head);
    const body = el('tbody');
    Object.keys(opt.prior_returns).forEach((s) => {
      const p = opt.prior_returns[s], b = opt.blended_returns[s];
      const d = b - p;
      const color = d > 0.1 ? '#3ec46d' : (d < -0.1 ? '#e15a5a' : 'inherit');
      function c(txt, al, col) { return el('td', { style: { padding: '3px 6px', textAlign: al || 'right', color: col || 'inherit' } }, [txt]); }
      const tr = el('tr');
      tr.appendChild(c(s, 'left'));
      tr.appendChild(c(fmtPct(p)));
      tr.appendChild(c(fmtPct(b)));
      tr.appendChild(c((d >= 0 ? '+' : '') + fmtPct(d), 'right', color));
      body.appendChild(tr);
    });
    t.appendChild(body);
    host.appendChild(t);
    return host;
  }

  function renderRiskParityDiagnostics(opt) {
    const host = el('div', { style: { marginTop: '16px' } });
    host.appendChild(el('div', { style: { fontSize: '12px', fontWeight: 600, opacity: 0.8, marginBottom: '4px' } },
      ['RISK PARITY: MARGINAL CONTRIBUTIONS']));
    const t = el('table', { style: { width: '100%', fontSize: '12px', borderCollapse: 'collapse' } });
    const head = el('thead', null, [el('tr', null, ['Symbol', 'Risk Contribution %'].map(
      (h) => el('th', { style: { textAlign: 'right', padding: '4px', borderBottom: '1px solid var(--border,#444)' } }, [h])))]);
    head.firstChild.firstChild.style.textAlign = 'left';
    t.appendChild(head);
    const body = el('tbody');
    Object.keys(opt.marginal_contributions).forEach((s) => {
      const tr = el('tr');
      tr.appendChild(el('td', { style: { padding: '3px 6px', textAlign: 'left' } }, [s]));
      tr.appendChild(el('td', { style: { padding: '3px 6px', textAlign: 'right' } }, [fmtPct(opt.marginal_contributions[s])]));
      body.appendChild(tr);
    });
    t.appendChild(body);
    host.appendChild(t);
    return host;
  }

  // Export
  window.renderOptimizer = renderOptimizer;
})();
