/**
 * Probabilistic Forecast — front-end renderer.
 *
 * Exposes `renderProbForecast(containerEl, symbol, horizon)` which paints
 * a horizontal density bar (fan chart) showing p5–p95 with the median
 * line and p25/p75 darker shading, a "probability of …" table, and an
 * optional Chart.js histogram below.
 *
 * Backend contract (see research_probforecast.py + INTEGRATE_PROBFORECAST.md):
 *   GET /api/research/probforecast/<symbol>?horizon=N
 *       -> { symbol, horizon_days, distribution{p05..p95,mean,std},
 *             probabilities{up,up_5pct,up_10pct,down_5pct,down_10pct},
 *             histogram[{bin_low,bin_high,count}], n_paths, ... }
 *
 *   GET /api/research/probforecast/<symbol>/vs-point?horizon=N
 *       -> { symbol, horizon_days, probabilistic: {...above...},
 *             point: { forecast_pct, prob_up_20d, source, trend },
 *             spread_vs_median_pct }
 *
 * Depends on globals already provided by index.html / app.js:
 *   - Chart (chart.js@4 UMD)        — optional; histogram only if present
 *   - API.get / fetch               — we use fetch directly to keep
 *                                     coupling minimal
 *   - Toast                         — optional
 *
 * Re-render-safe: any previous Chart instance is destroyed via the
 * `_pfChart` slot on the container.
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
  function _pct(v, digits) {
    if (v == null || isNaN(v)) return '—';
    return (Number(v) * 100).toFixed(digits == null ? 1 : digits) + '%';
  }
  function _signedPct(v, digits) {
    if (v == null || isNaN(v)) return '—';
    var d = digits == null ? 2 : digits;
    var s = v > 0 ? '+' : '';
    return s + Number(v).toFixed(d) + '%';
  }
  function _signClass(v) {
    if (v == null || isNaN(v)) return '';
    if (v > 0) return 'pf-pos';
    if (v < 0) return 'pf-neg';
    return '';
  }

  // ── one-shot scoped styles ─────────────────────────────────────────────
  var STYLE_ID = 'pf-style';
  var STYLE_CSS = ''
    + '.pf-wrap { font-family: var(--font-mono, monospace); color: var(--text-primary, #ddd); }'
    + '.pf-head { display:flex; align-items:baseline; justify-content:space-between; gap:8px; margin-bottom:6px; }'
    + '.pf-title { font-size:11px; letter-spacing:1.5px; text-transform:uppercase; color:var(--text-secondary,#aaa); }'
    + '.pf-meta { font-size:10px; color:var(--text-dim,#888); }'
    + '.pf-fan-row { display:flex; align-items:center; gap:10px; margin: 6px 0 10px 0; }'
    + '.pf-fan-track { position:relative; flex:1; height:34px; background:var(--bg-elevated,#1a1a1a);'
    + '  border:1px solid var(--border,#333); }'
    + '.pf-band-outer, .pf-band-inner {'
    + '  position:absolute; top:0; bottom:0; }'
    + '.pf-band-outer { background:rgba(120, 180, 255, 0.18); }'
    + '.pf-band-inner { background:rgba(120, 180, 255, 0.42); }'
    + '.pf-median {'
    + '  position:absolute; top:-2px; bottom:-2px; width:2px;'
    + '  background:var(--text-primary, #fff);'
    + '}'
    + '.pf-zero {'
    + '  position:absolute; top:-2px; bottom:-2px; width:1px;'
    + '  background:var(--text-dim, #888); opacity:0.6;'
    + '  border-left: 1px dashed var(--text-dim, #888); width:0;'
    + '}'
    + '.pf-axis { display:flex; justify-content:space-between; font-size:9px; color:var(--text-dim,#888); margin-top:2px; }'
    + '.pf-legend { display:flex; gap:14px; font-size:10px; color:var(--text-secondary,#aaa); margin-top:4px; flex-wrap:wrap; }'
    + '.pf-legend .sw { display:inline-block; width:10px; height:10px; margin-right:4px; vertical-align:middle; border:1px solid var(--border,#333); }'
    + '.pf-prob-table { width:100%; border-collapse:collapse; margin-top:10px; font-size:11px; }'
    + '.pf-prob-table th, .pf-prob-table td { padding:4px 6px; text-align:right; border-bottom:1px solid var(--border,#222); }'
    + '.pf-prob-table th:first-child, .pf-prob-table td:first-child { text-align:left; }'
    + '.pf-prob-table th { color:var(--text-dim,#888); font-weight:500; letter-spacing:0.5px; text-transform:uppercase; }'
    + '.pf-pos { color: var(--green, #2bd47d); }'
    + '.pf-neg { color: var(--red, #ff5e5e); }'
    + '.pf-bar { display:inline-block; height:6px; background:var(--blue, #74a8ff); vertical-align:middle; }'
    + '.pf-hist-wrap { margin-top:12px; height:120px; position:relative; }'
    + '.pf-point-row { display:flex; gap:12px; align-items:baseline; font-size:11px; color:var(--text-secondary,#aaa); margin-top:6px; }'
    + '.pf-point-row .pf-point-num { font-size:13px; font-weight:500; }'
    + '.pf-err { color: var(--amber, #f0b400); font-size:11px; padding:8px 0; }';

  function _injectStyle() {
    if (typeof document === 'undefined') return;
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  // ── fan chart drawing ───────────────────────────────────────────────────
  // We render a horizontal "violin lite" — outer band p05–p95, inner
  // band p25–p75, median tick, and a marker for the 0% line.
  function _renderFan(d) {
    if (!d) return '';
    // Guard against partial payloads (any of p05/p95/p25/p75/median missing
    // would otherwise produce NaN widths and a broken fan chart).
    if (d.p05 == null || d.p95 == null || d.p25 == null ||
        d.p75 == null || d.median == null ||
        !isFinite(d.p05) || !isFinite(d.p95)) {
      return '<div class="pf-err">distribution missing percentile data</div>';
    }
    // Domain: pad the [p05, p95] range slightly so the bands don't kiss
    // the edges; also force 0% to live inside the visible range so the
    // "no-change" reference is always shown.
    var lo = Math.min(d.p05, 0);
    var hi = Math.max(d.p95, 0);
    var span = Math.max(0.01, hi - lo);
    var pad  = span * 0.06;
    lo -= pad;
    hi += pad;
    span = hi - lo;

    function _frac(x) { return Math.max(0, Math.min(1, (x - lo) / span)); }
    function _pctL(x) { return (_frac(x) * 100).toFixed(2) + '%'; }
    function _pctW(a, b) {
      var w = (_frac(b) - _frac(a)) * 100;
      return Math.max(0.5, w).toFixed(2) + '%';
    }

    var outerLeft  = _pctL(d.p05);
    var outerWidth = _pctW(d.p05, d.p95);
    var innerLeft  = _pctL(d.p25);
    var innerWidth = _pctW(d.p25, d.p75);
    var medianLeft = _pctL(d.median);
    var zeroLeft   = _pctL(0);

    return ''
      + '<div class="pf-fan-row">'
      +   '<div class="pf-fan-track" title="p5–p95: ' + _signedPct(d.p05) + ' to ' + _signedPct(d.p95) + '">'
      +     '<div class="pf-band-outer" style="left:' + outerLeft + ';width:' + outerWidth + ';" title="p5–p95"></div>'
      +     '<div class="pf-band-inner" style="left:' + innerLeft + ';width:' + innerWidth + ';" title="p25–p75"></div>'
      +     '<div class="pf-median" style="left:calc(' + medianLeft + ' - 1px);" title="median ' + _signedPct(d.median) + '"></div>'
      +     '<div class="pf-zero"   style="left:' + zeroLeft + ';" title="0%"></div>'
      +   '</div>'
      + '</div>'
      + '<div class="pf-axis">'
      +   '<span>' + _signedPct(lo, 1) + '</span>'
      +   '<span>' + _signedPct((lo + hi) / 2, 1) + '</span>'
      +   '<span>' + _signedPct(hi, 1) + '</span>'
      + '</div>'
      + '<div class="pf-legend">'
      +   '<span><span class="sw" style="background:rgba(120,180,255,0.42)"></span>p25–p75</span>'
      +   '<span><span class="sw" style="background:rgba(120,180,255,0.18)"></span>p5–p95</span>'
      +   '<span><span class="sw" style="background:var(--text-primary,#fff);width:2px"></span>median</span>'
      + '</div>';
  }

  // ── probability table ─────────────────────────────────────────────────
  function _renderProbTable(p) {
    if (!p) return '';
    var rows = [
      ['P(up)',          p.up,         'pos'],
      ['P(up &gt; 5%)',  p.up_5pct,    'pos'],
      ['P(up &gt; 10%)', p.up_10pct,   'pos'],
      ['P(down &gt; 5%)',  p.down_5pct,  'neg'],
      ['P(down &gt; 10%)', p.down_10pct, 'neg'],
    ];
    var body = rows.map(function (r) {
      var label = r[0]; var v = r[1]; var cls = r[2] === 'pos' ? 'pf-pos' : 'pf-neg';
      var w = (v == null || isNaN(v)) ? 0 : Math.max(0, Math.min(1, v));
      var pct = _pct(v, 1);
      return ''
        + '<tr>'
        + '<td>' + label + '</td>'
        + '<td><span class="pf-bar ' + cls + '" '
        +     'style="width:' + (w * 100).toFixed(1) + 'px;background:'
        +     (r[2] === 'pos' ? 'var(--green, #2bd47d)' : 'var(--red, #ff5e5e)') + '"></span></td>'
        + '<td class="' + cls + '">' + pct + '</td>'
        + '</tr>';
    }).join('');
    return ''
      + '<table class="pf-prob-table">'
      + '<thead><tr><th>Outcome</th><th></th><th>Probability</th></tr></thead>'
      + '<tbody>' + body + '</tbody>'
      + '</table>';
  }

  // ── small histogram ───────────────────────────────────────────────────
  function _drawHistogram(canvasEl, histogram) {
    if (!canvasEl || !global.Chart) return null;
    var labels = histogram.map(function (b) {
      return ((b.bin_low + b.bin_high) / 2).toFixed(1) + '%';
    });
    var data = histogram.map(function (b) { return b.count; });
    var cssBlue = (global.getComputedStyle
      ? getComputedStyle(document.documentElement).getPropertyValue('--blue').trim()
      : '') || '#74a8ff';

    return new Chart(canvasEl, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          data: data,
          backgroundColor: cssBlue + 'cc',
          borderColor: cssBlue,
          borderWidth: 1,
          barPercentage: 1.0,
          categoryPercentage: 1.0,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              title: function (items) {
                if (!items || !items.length) return '';
                var i = items[0].dataIndex;
                var b = histogram[i];
                return b.bin_low.toFixed(2) + '% to ' + b.bin_high.toFixed(2) + '%';
              },
              label: function (item) { return 'count: ' + item.parsed.y; },
            },
          },
        },
        scales: {
          x: { ticks: { autoSkip: true, maxTicksLimit: 8, font: { size: 9 } },
               grid: { display: false } },
          y: { beginAtZero: true, ticks: { font: { size: 9 } },
               grid: { color: 'rgba(255,255,255,0.05)' } },
        },
      },
    });
  }

  // ── point-vs-probabilistic comparison line (optional) ─────────────────
  function _renderPointComparison(point, spread) {
    if (!point || (point.forecast_pct == null && point.prob_up_20d == null)) {
      return '';
    }
    var html = '<div class="pf-point-row">';
    if (point.forecast_pct != null) {
      html += '<span>POINT FORECAST:</span>'
        + '<span class="pf-point-num ' + _signClass(point.forecast_pct) + '">'
        + _signedPct(point.forecast_pct) + '</span>';
    }
    if (point.prob_up_20d != null) {
      html += '<span style="opacity:0.7">RF P(up,20d):</span>'
        + '<span class="pf-point-num">' + _pct(point.prob_up_20d) + '</span>';
    }
    if (spread != null && !isNaN(spread)) {
      html += '<span style="opacity:0.7;margin-left:auto">vs median:</span>'
        + '<span class="' + _signClass(spread) + '">'
        + _signedPct(spread) + '</span>';
    }
    html += '</div>';
    return html;
  }

  // ── network ────────────────────────────────────────────────────────────
  function _fetchJSON(url) {
    if (global.API && typeof global.API.get === 'function') {
      try { return global.API.get(url); } catch (e) { /* fall through */ }
    }
    return fetch(url, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    });
  }

  // ── main entry point ──────────────────────────────────────────────────
  function renderProbForecast(containerEl, symbol, horizon) {
    if (!containerEl) return;
    _injectStyle();

    symbol = (symbol || '').toString().toUpperCase();
    horizon = parseInt(horizon, 10) || 20;

    // Tear down any prior Chart.js instance attached by a previous render.
    if (containerEl._pfChart && typeof containerEl._pfChart.destroy === 'function') {
      try { containerEl._pfChart.destroy(); } catch (_e) { /* ignore */ }
      containerEl._pfChart = null;
    }

    containerEl.innerHTML = ''
      + '<div class="pf-wrap">'
      +   '<div class="pf-head">'
      +     '<span class="pf-title">PROBABILISTIC FORECAST &nbsp;//&nbsp; '
      +       _esc(symbol) + ' &nbsp;•&nbsp; ' + horizon + 'D</span>'
      +     '<span class="pf-meta" data-pf-meta>loading…</span>'
      +   '</div>'
      +   '<div data-pf-body>'
      +     '<div style="font-size:11px;color:var(--text-dim,#888);padding:8px 0">'
      +       'Running ' + horizon + '-day block-bootstrap simulation…'
      +     '</div>'
      +   '</div>'
      + '</div>';

    if (!symbol) {
      containerEl.querySelector('[data-pf-body]').innerHTML =
        '<div class="pf-err">Symbol required.</div>';
      return;
    }

    var url = '/api/research/probforecast/' + encodeURIComponent(symbol)
            + '?horizon=' + encodeURIComponent(horizon);

    _fetchJSON(url).then(function (resp) {
      // Accept either the bare probforecast payload or the {probabilistic,
      // point, spread_vs_median_pct} envelope from the vs-point endpoint —
      // the caller might switch endpoints later and we want either to render.
      var hasPoint = resp && resp.probabilistic;
      var data = hasPoint ? resp.probabilistic : resp;

      if (!data || data.error) {
        var msg = (data && data.error) || 'No data';
        containerEl.querySelector('[data-pf-body]').innerHTML =
          '<div class="pf-err">' + _esc(msg) + '</div>';
        var meta = containerEl.querySelector('[data-pf-meta]');
        if (meta) meta.textContent = '';
        return;
      }

      var bodyHTML = ''
        + _renderFan(data.distribution)
        + _renderProbTable(data.probabilities);

      if (hasPoint) {
        bodyHTML += _renderPointComparison(resp.point, resp.spread_vs_median_pct);
      }

      // Histogram canvas — drawn after innerHTML is committed.
      if (data.histogram && data.histogram.length && global.Chart) {
        bodyHTML += '<div class="pf-hist-wrap"><canvas data-pf-hist></canvas></div>';
      }

      containerEl.querySelector('[data-pf-body]').innerHTML = bodyHTML;

      var metaEl = containerEl.querySelector('[data-pf-meta]');
      if (metaEl) {
        var paths = data.n_paths || 0;
        var window_ = data.input_window_days || 0;
        var asOf = data.as_of || '';
        metaEl.textContent = paths + ' paths • ' + window_ + 'd window • '
          + (data.method || 'bootstrap')
          + (asOf ? ' • ' + asOf : '');
      }

      var canvas = containerEl.querySelector('[data-pf-hist]');
      if (canvas) {
        containerEl._pfChart = _drawHistogram(canvas, data.histogram);
      }
    }).catch(function (err) {
      containerEl.querySelector('[data-pf-body]').innerHTML =
        '<div class="pf-err">Failed to load: '
        + _esc(err && err.message ? err.message : err) + '</div>';
      var meta = containerEl.querySelector('[data-pf-meta]');
      if (meta) meta.textContent = '';
      if (global.Toast && typeof global.Toast.error === 'function') {
        try { global.Toast.error('Prob forecast failed: ' + (err && err.message || err)); }
        catch (_e) { /* ignore */ }
      }
    });
  }

  // Expose globally so app.js can call it.
  global.renderProbForecast = renderProbForecast;

})(typeof window !== 'undefined' ? window : globalThis);
