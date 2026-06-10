/* research_iv_density.js — Options-implied risk-neutral density panel.
 *
 * Renders the Breeden-Litzenberger PDF/CDF extracted from a symbol's call
 * chain at a chosen expiry. Pairs with research_iv_density.py:
 *   GET /api/research/rnd/<symbol>             (default expiry, >7d out)
 *   GET /api/research/rnd/<symbol>/<expiry>
 *
 * Usage from app.js:
 *   ResearchIVDensity.render(containerEl, symbol);
 *
 * The module is self-contained — it owns its own Chart.js instance lifecycle
 * (destroying any previous chart before recreating) and exposes a single
 * global `ResearchIVDensity` namespace so app.js can call it without import.
 */
(function (global) {
  'use strict';

  // ── Helpers ────────────────────────────────────────────────────────────
  function escapeHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function fmtNum(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    const d = digits == null ? 2 : digits;
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtPct(v) {
    if (v == null || !isFinite(v)) return '—';
    return (v * 100).toFixed(1) + '%';
  }

  function fmtMoney(v) {
    if (v == null || !isFinite(v)) return '—';
    return '$' + fmtNum(v, 2);
  }

  // Cache of chart instances keyed by canvas id (so re-renders don't leak).
  const _charts = {};

  function destroyChart(id) {
    if (_charts[id]) {
      try { _charts[id].destroy(); } catch (e) { /* noop */ }
      delete _charts[id];
    }
  }

  // ── API ────────────────────────────────────────────────────────────────
  async function fetchRND(symbol, expiry) {
    let url = '/api/research/rnd/' + encodeURIComponent(symbol);
    if (expiry) url += '/' + encodeURIComponent(expiry);
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  async function fetchExpiries(symbol) {
    const r = await fetch('/api/options/' + encodeURIComponent(symbol) + '/dates');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // ── DOM scaffolding ────────────────────────────────────────────────────
  // We intentionally compose the whole panel HTML in one go so the caller
  // (app.js) can just drop ResearchIVDensity.render(parent, symbol) and walk
  // away. The chart canvas, expiry dropdown, and probability calculator are
  // all wired up by id-prefix scoped to the symbol so multiple panels could
  // coexist on the same page if we ever wanted that.
  function scaffold(container, symbol) {
    const sId = symbol.replace(/[^A-Z0-9]/gi, '_');  // safe id segment
    container.innerHTML = `
      <div class="panel" id="rnd-panel-${sId}">
        <div class="panel-header">
          <span class="panel-title">IMPLIED DENSITY — ${escapeHtml(symbol)}</span>
          <div style="display:flex;gap:8px;align-items:center">
            <span style="font-size:10px;color:var(--text-dim)">EXPIRY:</span>
            <select class="form-select" id="rnd-expiry-${sId}"
                    style="font-size:11px;padding:2px 6px;min-width:140px">
              <option value="">Loading…</option>
            </select>
            <span style="font-size:10px;color:var(--text-dim)" id="rnd-method-${sId}">
              Breeden-Litzenberger
            </span>
          </div>
        </div>
        <div class="panel-body" style="padding:8px">
          <div id="rnd-status-${sId}" style="font-size:10px;color:var(--text-dim);margin-bottom:6px">
            Loading risk-neutral density…
          </div>
          <div style="display:grid;grid-template-columns:1fr 280px;gap:12px;align-items:start">
            <div style="min-height:300px;position:relative">
              <canvas id="rnd-chart-${sId}" style="max-height:340px"></canvas>
            </div>
            <div>
              <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">
                IMPLIED MOMENTS
              </div>
              <table class="data-table" id="rnd-moments-${sId}"
                     style="width:100%;font-size:11px;margin-bottom:8px">
                <tbody><tr><td colspan="2" style="color:var(--text-dim)">—</td></tr></tbody>
              </table>

              <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">
                TAIL PROBABILITIES
              </div>
              <table class="data-table" id="rnd-tails-${sId}"
                     style="width:100%;font-size:11px;margin-bottom:8px">
                <tbody><tr><td colspan="2" style="color:var(--text-dim)">—</td></tr></tbody>
              </table>

              <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">
                PROBABILITY CALCULATOR
              </div>
              <div style="display:flex;gap:4px;margin-bottom:4px">
                <select class="form-select" id="rnd-calc-dir-${sId}"
                        style="font-size:11px;padding:2px 4px;flex:0 0 80px">
                  <option value="above">ABOVE</option>
                  <option value="below">BELOW</option>
                </select>
                <input type="number" step="any" id="rnd-calc-strike-${sId}"
                       placeholder="Strike"
                       class="form-input" style="font-size:11px;padding:2px 4px;flex:1;min-width:0">
              </div>
              <div id="rnd-calc-result-${sId}"
                   style="font-size:13px;color:var(--green);font-weight:600;text-align:center;padding:6px;
                          border:1px solid var(--border);border-radius:2px">
                —
              </div>
            </div>
          </div>
        </div>
      </div>
    `;
    return sId;
  }

  // ── Render the chart + side panels ─────────────────────────────────────
  function renderChart(canvasId, rnd) {
    if (typeof Chart === 'undefined') {
      // Chart.js not loaded — show a textual fallback. Shouldn't happen since
      // index.html pulls Chart.js globally, but defensive in case the panel
      // is opened in a stripped-down view.
      const canvas = document.getElementById(canvasId);
      if (canvas) {
        const parent = canvas.parentElement;
        parent.innerHTML = '<div class="empty-state"><span class="text-red">Chart.js unavailable</span></div>';
      }
      return;
    }
    destroyChart(canvasId);
    const ctx = document.getElementById(canvasId);
    if (!ctx) return;

    // Subsample the density to ~100 points for chart smoothness (Chart.js
    // gets sluggish with 200+ datapoints and the visual difference is nil).
    const strikes = rnd && Array.isArray(rnd.strikes) ? rnd.strikes : [];
    const density = rnd && Array.isArray(rnd.density) ? rnd.density : [];
    const N = strikes.length;
    if (!N || N !== density.length) {
      // Empty / mismatched density payload — draw nothing rather than crash
      // with "Cannot read property 'length' of undefined" inside Math.max.
      const parent = ctx.parentElement;
      if (parent) parent.innerHTML +=
        '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;' +
        'color:var(--text-dim);font-size:11px;pointer-events:none">no density data</div>';
      return;
    }
    const target = 120;
    let step = Math.max(1, Math.floor(N / target));
    const xs = [], ys = [];
    for (let i = 0; i < N; i += step) {
      xs.push(strikes[i]);
      ys.push(density[i]);
    }
    if (xs[xs.length - 1] !== strikes[N - 1]) {
      xs.push(strikes[N - 1]);
      ys.push(density[N - 1]);
    }

    // Vertical spot marker via an extra dataset with two points.
    const spot = rnd.spot;
    const yMax = Math.max.apply(null, ys);
    const spotLineData = [
      { x: spot, y: 0 },
      { x: spot, y: yMax * 1.05 },
    ];

    // Compact, terminal-style chart matching the rest of AUGUR's theme.
    _charts[canvasId] = new Chart(ctx, {
      type: 'line',
      data: {
        labels: xs.map(x => x),
        datasets: [
          {
            label: 'Implied PDF',
            data: ys,
            borderColor: '#37e6a2',
            backgroundColor: 'rgba(55,230,162,0.15)',
            borderWidth: 1.5,
            pointRadius: 0,
            tension: 0.35,
            fill: true,
          },
          {
            label: 'Spot',
            data: spotLineData.map(p => p.y),  // y values at indices corresponding to xs nearest spot
            // We'll instead draw the spot as a vertical reference via an
            // x-aligned scatter trick: build a sparse dataset where only the
            // entry whose x is closest to spot has a non-null y.
            xAxisID: 'x',
            borderColor: '#ffae42',
            backgroundColor: '#ffae42',
            borderWidth: 1,
            borderDash: [4, 4],
            pointRadius: 0,
            showLine: false,
            hidden: true,  // hidden — the annotation below replaces it
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'nearest', intersect: false },
        plugins: {
          legend: {
            display: false,
          },
          tooltip: {
            backgroundColor: '#0a2030',
            borderColor: '#1a4060',
            borderWidth: 1,
            titleColor: '#37e6a2',
            bodyColor: '#cfd6e4',
            titleFont: { family: 'JetBrains Mono', size: 11 },
            bodyFont:  { family: 'JetBrains Mono', size: 10 },
            callbacks: {
              title: (items) => 'K = $' + fmtNum(items[0].parsed.x, 2),
              label: (item) => 'density = ' + fmtNum(item.parsed.y, 6),
            },
          },
        },
        scales: {
          x: {
            type: 'linear',
            ticks: {
              color: '#7a8794',
              font: { family: 'JetBrains Mono', size: 10 },
              callback: v => '$' + fmtNum(v, 0),
            },
            grid: { color: 'rgba(255,255,255,0.04)' },
            title: {
              display: true,
              text: 'STRIKE',
              color: '#7a8794',
              font: { family: 'JetBrains Mono', size: 10 },
            },
          },
          y: {
            ticks: {
              color: '#7a8794',
              font: { family: 'JetBrains Mono', size: 9 },
              callback: v => (typeof v === 'number' ? v.toExponential(0) : v),
            },
            grid: { color: 'rgba(255,255,255,0.04)' },
            title: {
              display: true,
              text: 'PDF f(K)',
              color: '#7a8794',
              font: { family: 'JetBrains Mono', size: 10 },
            },
          },
        },
      },
      plugins: [
        // Custom plugin: draw a vertical line at spot price + label.
        // Chart.js plugins receive the chart instance and we draw directly
        // on the canvas context, mapping data units → pixel space via the
        // chart's x-scale.
        {
          id: 'spotMarker',
          afterDatasetsDraw(chart) {
            const xScale = chart.scales.x;
            const yScale = chart.scales.y;
            const x = xScale.getPixelForValue(spot);
            const yTop = yScale.top;
            const yBot = yScale.bottom;
            const c = chart.ctx;
            c.save();
            c.strokeStyle = '#ffae42';
            c.setLineDash([4, 4]);
            c.lineWidth = 1;
            c.beginPath();
            c.moveTo(x, yTop);
            c.lineTo(x, yBot);
            c.stroke();
            c.setLineDash([]);
            c.fillStyle = '#ffae42';
            c.font = '10px JetBrains Mono';
            c.fillText('spot $' + fmtNum(spot, 2), x + 4, yTop + 12);
            c.restore();
          },
        },
      ],
    });
  }

  function renderMoments(tableId, rnd) {
    const t = document.getElementById(tableId);
    if (!t) return;
    const m = rnd.implied_moments || {};
    const rows = [
      ['MEAN',     fmtMoney(m.mean_implied)],
      ['STD',      fmtMoney(m.std_implied)],
      ['SKEW',     fmtNum(m.skew_implied, 3)],
      ['EXCESS K', fmtNum(m.kurtosis_implied, 3)],
      ['SPOT',     fmtMoney(rnd.spot)],
      ['DTE',      String(rnd.days_to_expiry)],
      ['r',        ((rnd.risk_free_rate || 0) * 100).toFixed(2) + '%'],
    ];
    t.innerHTML = '<tbody>' + rows.map(r =>
      `<tr><td style="color:var(--text-dim)">${r[0]}</td><td style="text-align:right">${escapeHtml(r[1])}</td></tr>`
    ).join('') + '</tbody>';
  }

  function renderTails(tableId, rnd) {
    const t = document.getElementById(tableId);
    if (!t) return;
    const p = rnd.probabilities || {};
    const rows = [
      ['P[K > +5%]',  fmtPct(p.p_above_spot_5pct),  'col-positive'],
      ['P[K > +10%]', fmtPct(p.p_above_spot_10pct), 'col-positive'],
      ['P[K > +20%]', fmtPct(p.p_above_spot_20pct), 'col-positive'],
      ['P[K < -5%]',  fmtPct(p.p_below_spot_5pct),  'col-negative'],
      ['P[K < -10%]', fmtPct(p.p_below_spot_10pct), 'col-negative'],
      ['P[K < -20%]', fmtPct(p.p_below_spot_20pct), 'col-negative'],
    ];
    t.innerHTML = '<tbody>' + rows.map(r =>
      `<tr><td style="color:var(--text-dim)">${r[0]}</td><td class="${r[2]}" style="text-align:right">${r[1]}</td></tr>`
    ).join('') + '</tbody>';
  }

  function probAboveFromCDF(strikes, cdf, x) {
    if (!strikes || !cdf || !strikes.length) return null;
    if (x <= strikes[0]) return 1.0;
    if (x >= strikes[strikes.length - 1]) return 0.0;
    // Linear interpolation of CDF at x.
    for (let i = 1; i < strikes.length; i++) {
      if (strikes[i] >= x) {
        const t = (x - strikes[i - 1]) / (strikes[i] - strikes[i - 1]);
        const c = cdf[i - 1] + t * (cdf[i] - cdf[i - 1]);
        return Math.max(0, Math.min(1, 1 - c));
      }
    }
    return 0.0;
  }

  function wireCalculator(sId, rnd) {
    const dirEl = document.getElementById('rnd-calc-dir-' + sId);
    const strikeEl = document.getElementById('rnd-calc-strike-' + sId);
    const resultEl = document.getElementById('rnd-calc-result-' + sId);
    if (!dirEl || !strikeEl || !resultEl) return;

    // Pre-fill with spot to give the user a starting point that yields a
    // meaningful probability (P[K > spot] is the standard "above"
    // baseline).
    if (rnd.spot != null && isFinite(rnd.spot)) strikeEl.value = String(rnd.spot);  // avoid literal "null"
    const update = () => {
      const x = parseFloat(strikeEl.value);
      if (!isFinite(x) || x <= 0) {
        resultEl.textContent = '—';
        resultEl.style.color = 'var(--text-dim)';
        return;
      }
      const pAbove = probAboveFromCDF(rnd.strikes, rnd.cdf, x);
      if (pAbove == null) {
        resultEl.textContent = '—';
        return;
      }
      const dir = dirEl.value;
      const p = dir === 'above' ? pAbove : (1 - pAbove);
      const label = dir === 'above' ? '> $' : '< $';
      resultEl.textContent = 'P[K ' + label + fmtNum(x, 2) + '] = ' + fmtPct(p);
      // Color-cue: green if tail is small (informative), amber otherwise.
      resultEl.style.color = p < 0.1 ? 'var(--red)' : p > 0.5 ? 'var(--green)' : 'var(--amber)';
    };
    dirEl.onchange = update;
    strikeEl.oninput = update;
    update();
  }

  // ── Main render orchestration ──────────────────────────────────────────
  // Per-symbol generation counter so a stale fetch can't overwrite a newer
  // one. The dropdown handler at the bottom of render() increments _gen
  // before awaiting; loadExpiry compares to its captured value after the
  // await returns and bails if the user has already moved on.
  const _gen = {};
  async function loadExpiry(sId, symbol, expiry) {
    const myGen = (_gen[sId] = (_gen[sId] || 0) + 1);
    const statusEl = document.getElementById('rnd-status-' + sId);
    const methodEl = document.getElementById('rnd-method-' + sId);
    if (statusEl) statusEl.textContent = 'Computing risk-neutral density…';
    let rnd;
    try {
      rnd = await fetchRND(symbol, expiry);
    } catch (e) {
      if (myGen !== _gen[sId]) return; // user moved on
      if (statusEl) {
        statusEl.style.color = 'var(--red)';
        statusEl.textContent = 'Fetch failed: ' + e.message;
      }
      return;
    }
    if (myGen !== _gen[sId]) return; // newer change in flight; drop stale result
    if (rnd.error) {
      if (statusEl) {
        statusEl.style.color = 'var(--red)';
        statusEl.textContent = 'Error: ' + rnd.error + (rnd.expiry ? ' (expiry ' + rnd.expiry + ')' : '');
      }
      return;
    }
    if (statusEl) {
      const liq = rnd.diagnostics || {};
      const flag = liq.illiquid_flag ? ' · LOW-LIQ' : '';
      statusEl.style.color = 'var(--text-dim)';
      statusEl.textContent =
        rnd.n_strikes + ' strikes · expiry ' + rnd.expiry + ' (' + rnd.days_to_expiry + 'd)' +
        ' · ∫f dK = ' + fmtNum(liq.density_integral, 3) + flag;
    }
    if (methodEl) methodEl.textContent = (rnd.method || 'breeden_litzenberger').replace(/_/g, ' ');

    renderChart('rnd-chart-' + sId, rnd);
    renderMoments('rnd-moments-' + sId, rnd);
    renderTails('rnd-tails-' + sId, rnd);
    wireCalculator(sId, rnd);
  }

  async function render(container, symbol) {
    if (!container) return;
    symbol = String(symbol || '').toUpperCase();
    if (!symbol) {
      container.innerHTML = '<div class="empty-state"><span class="text-red">No symbol</span></div>';
      return;
    }
    const sId = scaffold(container, symbol);

    // Populate the expiry selector from /api/options/<symbol>/dates so the
    // user can pick any tenor the chain exposes. We default to whatever the
    // backend picks (first expiry >7d out) by initially loading with no
    // expiry arg and then syncing the dropdown's selected option to the
    // backend's choice once the response arrives.
    const selectEl = document.getElementById('rnd-expiry-' + sId);
    let dates = [];
    try {
      dates = await fetchExpiries(symbol);
    } catch (e) {
      if (selectEl) selectEl.innerHTML = '<option value="">No expiries</option>';
      const statusEl = document.getElementById('rnd-status-' + sId);
      if (statusEl) {
        statusEl.style.color = 'var(--red)';
        statusEl.textContent = 'Failed to load expiries: ' + e.message;
      }
      return;
    }
    if (!Array.isArray(dates) || !dates.length) {
      if (selectEl) selectEl.innerHTML = '<option value="">No expiries</option>';
      const statusEl = document.getElementById('rnd-status-' + sId);
      if (statusEl) {
        statusEl.style.color = 'var(--text-dim)';
        statusEl.textContent = 'No options expiries available for ' + symbol + '.';
      }
      return;
    }
    if (selectEl) {
      selectEl.innerHTML = dates.map(d => `<option value="${escapeHtml(d)}">${escapeHtml(d)}</option>`).join('');
    }

    // Initial load — let the backend pick the default expiry. We'll then
    // align the dropdown to whichever expiry the backend chose.
    const initial = await (async () => {
      try {
        const rnd = await fetchRND(symbol);
        if (rnd && rnd.expiry && selectEl) selectEl.value = rnd.expiry;
        return rnd;
      } catch (e) {
        return { error: e.message };
      }
    })();

    if (initial.error) {
      const statusEl = document.getElementById('rnd-status-' + sId);
      if (statusEl) {
        statusEl.style.color = 'var(--red)';
        statusEl.textContent = 'Error: ' + initial.error;
      }
    } else {
      const statusEl = document.getElementById('rnd-status-' + sId);
      const methodEl = document.getElementById('rnd-method-' + sId);
      if (statusEl) {
        const liq = initial.diagnostics || {};
        const flag = liq.illiquid_flag ? ' · LOW-LIQ' : '';
        statusEl.style.color = 'var(--text-dim)';
        statusEl.textContent =
          initial.n_strikes + ' strikes · expiry ' + initial.expiry + ' (' + initial.days_to_expiry + 'd)' +
          ' · ∫f dK = ' + fmtNum(liq.density_integral, 3) + flag;
      }
      if (methodEl) methodEl.textContent = (initial.method || 'breeden_litzenberger').replace(/_/g, ' ');
      renderChart('rnd-chart-' + sId, initial);
      renderMoments('rnd-moments-' + sId, initial);
      renderTails('rnd-tails-' + sId, initial);
      wireCalculator(sId, initial);
    }

    // Wire the expiry-change handler. Dedup is handled inside loadExpiry
    // via the module-level _gen counter keyed by sId — that way the guard
    // also covers the initial-load path above, not just dropdown changes.
    if (selectEl) {
      selectEl.onchange = () => loadExpiry(sId, symbol, selectEl.value);
    }
  }

  // Public surface — minimal so app.js integration is trivial.
  global.ResearchIVDensity = {
    render: render,
    // Exposed for unit-style smoke tests in the browser console:
    probAboveFromCDF: probAboveFromCDF,
  };

  // Convenience alias mirroring the spec ("renderRND(containerEl, symbol)").
  global.renderRND = render;
})(window);
