/**
 * Monte Carlo Portfolio Simulation — front-end renderer.
 *
 * Exposes a single top-level entry point, `renderMonteCarlo(containerEl,
 * options)`, that paints a controls bar + a Chart.js percentile-cone
 * chart + a right-side stats panel into the supplied container.
 *
 * Backend contract (see `research_montecarlo.py` + INTEGRATE_MONTECARLO.md):
 *   GET /api/research/montecarlo/portfolio?horizon_days=...&method=...
 *       — derives holdings from State.portfolio server-side.
 *   POST /api/research/montecarlo
 *       — body: { holdings, n_paths, horizon_days, method }
 *
 * Designed to drop into a `<div id="view-montecarlo">` slot but works
 * inside any host element. Re-rendering is safe (we tear down any prior
 * Chart.js instance via the `_chart` slot on the host element).
 *
 * Depends on globals already loaded by `index.html`:
 *   - `Chart` (chart.js@4 UMD)
 *   - `API`   (fetch wrappers in app.js)
 *   - `fmt`   (number formatters in app.js)
 *   - `State` (portfolio holdings)
 *   - `Toast` (notifications)
 */

(function (global) {
  'use strict';

  // ── small utilities ────────────────────────────────────────────────────
  function _esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function _money(v) {
    if (v == null || isNaN(v)) return '—';
    return '$' + (global.fmt ? global.fmt.currency(v) : Number(v).toFixed(2));
  }
  function _pct(v, digits) {
    if (v == null || isNaN(v)) return '—';
    return (Number(v) * 100).toFixed(digits == null ? 1 : digits) + '%';
  }
  function _signedPct(v) {
    if (v == null || isNaN(v)) return '—';
    var s = v > 0 ? '+' : '';
    return s + Number(v).toFixed(2) + '%';
  }

  // ── DEFAULT CONFIG ─────────────────────────────────────────────────────
  var DEFAULTS = {
    n_paths: 5000,
    horizon_days: 90,
    method: 'historical_bootstrap',
  };

  // ── MARKUP TEMPLATE ────────────────────────────────────────────────────
  function _shellHTML(opts) {
    var hOpts = [30, 90, 365].map(function (d) {
      var sel = d === opts.horizon_days ? ' selected' : '';
      return '<option value="' + d + '"' + sel + '>' + d + 'D</option>';
    }).join('');
    var mvnSel = opts.method === 'multivariate_normal' ? ' selected' : '';
    var bsSel  = opts.method === 'historical_bootstrap' ? ' selected' : '';

    return ''
    + '<div class="panel mb-8" data-mc-root="1">'
    +   '<div class="panel-header">'
    +     '<span class="panel-title">MONTE CARLO // PORTFOLIO NAV PROJECTION</span>'
    +     '<div class="flex gap-8 align-center">'
    +       '<span style="font-size:10px;color:var(--text-dim)">HORIZON:</span>'
    +       '<select class="form-select" data-mc-horizon style="font-size:11px;padding:2px 6px">'
    +         hOpts
    +       '</select>'
    +       '<span style="font-size:10px;color:var(--text-dim)">METHOD:</span>'
    +       '<select class="form-select" data-mc-method style="font-size:11px;padding:2px 6px">'
    +         '<option value="historical_bootstrap"' + bsSel + '>Bootstrap</option>'
    +         '<option value="multivariate_normal"' + mvnSel + '>MV-Normal</option>'
    +       '</select>'
    +       '<button class="btn btn-ghost btn-sm" data-mc-run>▶ RUN</button>'
    +     '</div>'
    +   '</div>'
    +   '<div class="panel-body" style="display:grid;grid-template-columns:1fr 320px;gap:12px;align-items:stretch">'
    +     '<div style="position:relative;height:420px;min-width:0">'
    +       '<canvas data-mc-canvas></canvas>'
    +       '<div data-mc-overlay style="position:absolute;inset:0;display:none;align-items:center;justify-content:center;background:rgba(6,21,32,0.55);font-size:11px;color:var(--text-dim);pointer-events:none">'
    +         '<span><span class="spinner"></span> SIMULATING…</span>'
    +       '</div>'
    +     '</div>'
    +     '<div data-mc-side style="display:flex;flex-direction:column;gap:10px;font-size:11px;border-left:1px solid var(--border);padding-left:12px"></div>'
    +   '</div>'
    + '</div>';
  }

  // ── SIDE PANEL ─────────────────────────────────────────────────────────
  function _renderSide(sideEl, sim) {
    if (!sim) {
      sideEl.innerHTML = '<span class="text-dim">No simulation yet.</span>';
      return;
    }
    var td = sim.terminal_distribution || {};
    var sm = sim.summary || {};
    var nav0 = sim.initial_nav || 0;
    function row(k, v) {
      return '<div style="display:flex;justify-content:space-between"><span class="text-dim">' + _esc(k) + '</span><span>' + v + '</span></div>';
    }
    function pctRet(navEnd) {
      if (!nav0) return '—';
      var r = ((navEnd - nav0) / nav0) * 100;
      var cls = r >= 0 ? 'col-positive' : 'col-negative';
      return '<span class="' + cls + '">' + _signedPct(r) + '</span>';
    }
    var dropped = (sim.dropped_symbols || []);
    var warning = sim.warning ? '<div class="text-amber" style="font-size:10px">⚠ ' + _esc(sim.warning) + '</div>' : '';
    var droppedNote = dropped.length
      ? '<div class="text-amber" style="font-size:10px">DROPPED (no history): ' + dropped.map(_esc).join(', ') + '</div>'
      : '';

    sideEl.innerHTML = ''
      + warning
      + droppedNote
      + '<div style="border:1px solid var(--border);padding:8px;border-radius:4px">'
      +   '<div class="text-dim" style="font-size:10px;margin-bottom:4px">INITIAL NAV</div>'
      +   '<div style="font-size:14px;font-weight:600">' + _money(nav0) + '</div>'
      +   '<div class="text-dim" style="font-size:9px;margin-top:2px">' + (sim.horizon_days || '?') + 'D horizon · ' + (sim.n_paths || '?') + ' paths · ' + _esc(sim.method || '') + '</div>'
      + '</div>'

      + '<div style="border:1px solid var(--border);padding:8px;border-radius:4px">'
      +   '<div class="text-dim" style="font-size:10px;margin-bottom:6px">TERMINAL DISTRIBUTION</div>'
      +   row('P05 (worst)',  _money(td.p05) + ' ' + pctRet(td.p05))
      +   row('P25',          _money(td.p25) + ' ' + pctRet(td.p25))
      +   row('P50 (median)', _money(td.p50) + ' ' + pctRet(td.p50))
      +   row('P75',          _money(td.p75) + ' ' + pctRet(td.p75))
      +   row('P95 (best)',   _money(td.p95) + ' ' + pctRet(td.p95))
      +   row('Mean',         _money(td.mean))
      +   row('Std Dev',      _money(td.std))
      + '</div>'

      + '<div style="border:1px solid var(--border);padding:8px;border-radius:4px">'
      +   '<div class="text-dim" style="font-size:10px;margin-bottom:6px">SUMMARY PROBABILITIES</div>'
      +   row('P(loss)',                _pct(sm.prob_loss))
      +   row('P(drawdown ≥10%)',       _pct(sm.prob_drawdown_10pct))
      +   row('P(drawdown ≥20%)',       _pct(sm.prob_drawdown_20pct))
      +   row('P(+15% gain)',           _pct(sm.prob_target_15pct_gain))
      +   row('1d 95% VaR',             (typeof sm.value_at_risk_95_pct === 'number' ? sm.value_at_risk_95_pct.toFixed(2) + '%' : '—'))
      +   row('1d 95% ES',              (typeof sm.expected_shortfall_95_pct === 'number' ? sm.expected_shortfall_95_pct.toFixed(2) + '%' : '—'))
      + '</div>'

      + '<div style="border:1px solid var(--border);padding:8px;border-radius:4px">'
      +   '<div class="text-dim" style="font-size:10px;margin-bottom:6px">PROBABILITY OF HITTING TARGET</div>'
      +   '<div style="display:flex;gap:6px;align-items:center">'
      +     '<input type="number" data-mc-target step="0.5" placeholder="+15" style="width:70px;background:var(--bg-dark);color:var(--text);border:1px solid var(--border);padding:3px 6px;font-family:inherit;font-size:11px">'
      +     '<span class="text-dim">% gain →</span>'
      +     '<button class="btn btn-ghost btn-sm" data-mc-calc-target>CALC</button>'
      +   '</div>'
      +   '<div data-mc-target-result style="margin-top:6px;font-size:11px;min-height:14px"></div>'
      + '</div>';
  }

  // ── CHART RENDERING ────────────────────────────────────────────────────
  function _renderChart(canvas, sim) {
    if (!global.Chart) {
      // Chart.js not loaded — degrade gracefully.
      return null;
    }
    if (canvas._mcChart) {
      try { canvas._mcChart.destroy(); } catch (e) { /* ignore */ }
      canvas._mcChart = null;
    }
    var pc = (sim && sim.percentiles) || {};
    var hd = sim.horizon_days || (pc.p50 ? pc.p50.length : 0);
    if (!hd) return null;

    // Day-index labels. We don't try to convert to calendar dates because
    // the simulation is in *trading days*; users can map mentally.
    var labels = [];
    for (var i = 1; i <= hd; i++) labels.push(i);

    // Datasets: outer band (p5..p95), inner band (p25..p75), median line.
    // Chart.js v4 supports band-fill via `fill: { target: <datasetIndex> }`.
    var grid = '#0f2a3a';
    var dim  = '#3a5a70';
    var ds = [
      {
        // p95 line — invisible, just the band ceiling.
        label: 'P95',
        data: pc.p95 || [],
        borderColor: 'rgba(58,138,180,0)',
        backgroundColor: 'rgba(58,138,180,0.12)',
        borderWidth: 0,
        pointRadius: 0,
        fill: false,
        tension: 0.1,
      },
      {
        // p5 line — fills *up* to p95 (dataset index 0).
        label: 'P05–P95 band',
        data: pc.p05 || [],
        borderColor: 'rgba(58,138,180,0)',
        backgroundColor: 'rgba(58,138,180,0.12)',
        borderWidth: 0,
        pointRadius: 0,
        fill: '-1',
        tension: 0.1,
      },
      {
        label: 'P75',
        data: pc.p75 || [],
        borderColor: 'rgba(80,180,140,0)',
        backgroundColor: 'rgba(80,180,140,0.20)',
        borderWidth: 0,
        pointRadius: 0,
        fill: false,
        tension: 0.1,
      },
      {
        label: 'P25–P75 band',
        data: pc.p25 || [],
        borderColor: 'rgba(80,180,140,0)',
        backgroundColor: 'rgba(80,180,140,0.20)',
        borderWidth: 0,
        pointRadius: 0,
        fill: '-1',
        tension: 0.1,
      },
      {
        label: 'Median (P50)',
        data: pc.p50 || [],
        borderColor: '#52d090',
        backgroundColor: '#52d090',
        borderWidth: 2,
        pointRadius: 0,
        fill: false,
        tension: 0.1,
      },
    ];

    // Initial-NAV reference line as a constant dataset overlay.
    var nav0 = sim.initial_nav || 0;
    if (nav0) {
      var flat = [];
      for (var j = 0; j < hd; j++) flat.push(nav0);
      ds.push({
        label: 'Initial NAV',
        data: flat,
        borderColor: 'rgba(180,180,180,0.6)',
        borderDash: [4, 4],
        borderWidth: 1,
        pointRadius: 0,
        fill: false,
        tension: 0,
      });
    }

    var ctx = canvas.getContext('2d');
    var chart = new global.Chart(ctx, {
      type: 'line',
      data: { labels: labels, datasets: ds },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            display: true,
            position: 'bottom',
            labels: {
              color: dim, font: { size: 10, family: "'JetBrains Mono', monospace" },
              filter: function (item) {
                // Hide invisible "ceiling" datasets — they're just for the band.
                return item.text !== 'P95' && item.text !== 'P75';
              },
            },
          },
          tooltip: {
            backgroundColor: '#061520',
            borderColor: grid,
            borderWidth: 1,
            titleColor: '#9fb6c4',
            bodyColor: '#9fb6c4',
            callbacks: {
              title: function (items) {
                return 'Day ' + (items[0] ? items[0].label : '');
              },
              label: function (ctx) {
                return ctx.dataset.label + ': $' + Number(ctx.parsed.y).toLocaleString('en-US', { maximumFractionDigits: 0 });
              },
            },
          },
        },
        scales: {
          x: {
            title: { display: true, text: 'Trading days', color: dim, font: { size: 10 } },
            ticks: { color: dim, font: { size: 9 }, maxTicksLimit: 12 },
            grid: { color: grid },
          },
          y: {
            title: { display: true, text: 'Portfolio NAV ($)', color: dim, font: { size: 10 } },
            ticks: {
              color: dim,
              font: { size: 9 },
              callback: function (val) {
                return '$' + (val >= 1000 ? (val / 1000).toFixed(0) + 'K' : val.toFixed(0));
              },
            },
            grid: { color: grid },
          },
        },
      },
    });
    canvas._mcChart = chart;
    return chart;
  }

  // ── FETCH HELPERS ──────────────────────────────────────────────────────
  function _fetchPortfolioSimulation(opts) {
    // Prefer the GET endpoint that auto-derives holdings server-side. Fall
    // back to a POST with explicit holdings sourced from State.portfolio
    // if the GET fails (e.g. no portfolio yet, or backend not wired).
    var qs = '?horizon_days=' + encodeURIComponent(opts.horizon_days) +
             '&method=' + encodeURIComponent(opts.method) +
             '&n_paths=' + encodeURIComponent(opts.n_paths);
    return global.API.get('/api/research/montecarlo/portfolio' + qs)
      .catch(function () {
        var holdings = (global.State && global.State.portfolio && global.State.portfolio.holdings) || [];
        var slim = holdings.filter(function (h) { return h && h.symbol && h.market_value; })
          .map(function (h) { return { symbol: h.symbol, market_value: h.market_value }; });
        if (!slim.length) {
          return Promise.reject(new Error('No portfolio holdings to simulate'));
        }
        return global.API.post('/api/research/montecarlo', {
          holdings: slim,
          horizon_days: opts.horizon_days,
          method: opts.method,
          n_paths: opts.n_paths,
        });
      });
  }

  // ── MAIN ENTRY POINT ───────────────────────────────────────────────────
  function renderMonteCarlo(containerEl, options) {
    if (!containerEl) return;
    var opts = Object.assign({}, DEFAULTS, options || {});
    containerEl.innerHTML = _shellHTML(opts);

    var root      = containerEl.querySelector('[data-mc-root]');
    var canvas    = root.querySelector('[data-mc-canvas]');
    var sideEl    = root.querySelector('[data-mc-side]');
    var overlay   = root.querySelector('[data-mc-overlay]');
    var horizonEl = root.querySelector('[data-mc-horizon]');
    var methodEl  = root.querySelector('[data-mc-method]');
    var runBtn    = root.querySelector('[data-mc-run]');

    var currentSim = null;
    var runSeq = 0; // generation token: only the LATEST run may paint results

    function setBusy(b) {
      overlay.style.display = b ? 'flex' : 'none';
      runBtn.disabled = b;
      // Horizon/method 'change' events also fire run(): leaving them enabled
      // mid-flight let a slower stale simulation race the newer one and
      // repaint the cone with mislabeled numbers. Disable them too.
      horizonEl.disabled = b;
      methodEl.disabled = b;
    }

    async function run() {
      var seq = ++runSeq;
      var horizon = parseInt(horizonEl.value, 10) || 90;
      var method  = methodEl.value || 'historical_bootstrap';
      setBusy(true);
      try {
        var sim = await _fetchPortfolioSimulation({
          horizon_days: horizon,
          method: method,
          n_paths: opts.n_paths,
        });
        if (seq !== runSeq) return; // superseded by a newer run — drop result
        currentSim = sim;
        _renderChart(canvas, sim);
        _renderSide(sideEl, sim);
      } catch (e) {
        if (seq !== runSeq) return; // stale failure — a newer run owns the UI
        if (global.Toast) global.Toast.error('Monte Carlo failed: ' + e.message);
        sideEl.innerHTML = '<div class="text-red" style="font-size:11px">' + _esc(e.message) + '</div>';
        // Tear down any prior chart so the user doesn't see a stale cone
        // alongside the new error message — that combination implies the
        // previous result is still valid, which it isn't.
        if (canvas._mcChart) {
          try { canvas._mcChart.destroy(); } catch (_e) { /* ignore */ }
          canvas._mcChart = null;
          var ctx = canvas.getContext('2d');
          if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
        currentSim = null;
      } finally {
        if (seq === runSeq) setBusy(false);
      }
    }

    runBtn.addEventListener('click', run);
    horizonEl.addEventListener('change', run);
    methodEl.addEventListener('change', run);

    // Target probability calculator — uses the current sim's terminal
    // distribution to compute a quick Gaussian approximation locally
    // *and* hits the backend prob_of_target route for the path-touch
    // probability (more accurate, but slower since it re-simulates).
    sideEl.addEventListener('click', async function (ev) {
      var btn = ev.target.closest('[data-mc-calc-target]');
      if (!btn) return;
      var inputEl = sideEl.querySelector('[data-mc-target]');
      var resultEl = sideEl.querySelector('[data-mc-target-result]');
      if (!inputEl || !currentSim) return;
      var pct = parseFloat(inputEl.value);
      if (isNaN(pct)) {
        resultEl.innerHTML = '<span class="text-amber">Enter a % gain</span>';
        return;
      }
      var nav0 = currentSim.initial_nav || 0;
      if (!nav0) {
        resultEl.innerHTML = '<span class="text-amber">No NAV to anchor target</span>';
        return;
      }
      var target = nav0 * (1 + pct / 100);

      // Gaussian fallback from the terminal distribution we already have.
      var td = currentSim.terminal_distribution || {};
      var mu = td.mean, sd = td.std;
      var localProb = null;
      if (mu != null && sd) {
        var z = (target - mu) / sd;
        // 1 - Phi(z) using erfc.
        localProb = 0.5 * _erfc(z / Math.SQRT2);
      }
      resultEl.innerHTML = '<span class="text-dim">Computing…</span>' +
        (localProb != null ? ' <span style="font-size:10px">(quick: ' + _pct(localProb) + ')</span>' : '');

      // Server round-trip for path-touch + more accurate terminal prob.
      try {
        var r = await global.API.post('/api/research/montecarlo', {
          horizon_days: currentSim.horizon_days,
          method: currentSim.method,
          n_paths: currentSim.n_paths,
          holdings: (currentSim.holdings || []).map(function (h) {
            return { symbol: h.symbol, market_value: h.market_value };
          }),
          target_nav: target,
        });
        // If the backend returns a `prob_of_target` block, use it; else
        // fall back to the local Gaussian estimate.
        var pt = (r && r.prob_of_target) || null;
        if (pt) {
          resultEl.innerHTML =
            '<div>Target: <b>' + _money(target) + '</b> (' + _signedPct(pct) + ')</div>' +
            '<div>P(terminal ≥ target): <b>' + _pct(pt.prob_terminal_ge_target) + '</b></div>' +
            '<div>P(touch target any day): <b>' + _pct(pt.prob_touch_target) + '</b></div>';
        } else {
          resultEl.innerHTML =
            '<div>Target: <b>' + _money(target) + '</b> (' + _signedPct(pct) + ')</div>' +
            '<div>P(terminal ≥ target) ≈ <b>' + _pct(localProb) + '</b> <span class="text-dim" style="font-size:9px">(Gaussian)</span></div>';
        }
      } catch (e) {
        resultEl.innerHTML =
          '<div>Target: <b>' + _money(target) + '</b> (' + _signedPct(pct) + ')</div>' +
          '<div>P(terminal ≥ target) ≈ <b>' + _pct(localProb) + '</b> <span class="text-dim" style="font-size:9px">(Gaussian; server unavailable)</span></div>';
      }
    });

    // Auto-run on mount so the panel never sits blank.
    run();
  }

  // ── erfc — used by the local Gaussian prob approximation. Numerical
  //    recipes-style Chebyshev approximation, max error ~ 1.5e-7. Fast,
  //    no external math libs required.
  function _erfc(x) {
    var z = Math.abs(x);
    var t = 1 / (1 + 0.5 * z);
    var ans = t * Math.exp(-z * z - 1.26551223 +
      t * (1.00002368 +
      t * (0.37409196 +
      t * (0.09678418 +
      t * (-0.18628806 +
      t * (0.27886807 +
      t * (-1.13520398 +
      t * (1.48851587 +
      t * (-0.82215223 +
      t * 0.17087277))))))))); // jshint ignore:line
    return x >= 0 ? ans : 2 - ans;
  }

  // ── PUBLIC EXPORTS ─────────────────────────────────────────────────────
  global.renderMonteCarlo = renderMonteCarlo;
  global.MonteCarlo = {
    render: renderMonteCarlo,
    _renderChart: _renderChart,   // exposed for tests / debug
    _renderSide:  _renderSide,
  };

  // Convenience loader used by app.js's view dispatcher (see
  // INTEGRATE_MONTECARLO.md). Idempotent: re-calling rebuilds the panel.
  global.loadMonteCarlo = function loadMonteCarlo() {
    var host = document.getElementById('view-montecarlo');
    if (!host) {
      console.warn('loadMonteCarlo: #view-montecarlo not in DOM');
      return;
    }
    renderMonteCarlo(host, {});
  };
})(window);
