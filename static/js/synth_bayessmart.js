/*
 * synth_bayessmart.js — Bayesian Smart-Money Composite UI renderer.
 *
 * Exposes ONE global:
 *
 *   window.renderBayesSmart(containerEl, symbol)
 *
 * Paints a side-by-side Static vs Bayes score header (with arrow indicating
 * the direction of change), a component table showing raw value, static
 * weight, posterior weight, and a contribution bar, plus a small
 * weight-shift summary highlighting the three components whose weight
 * moved the most.
 *
 * Backend contract (see synth_bayessmart.py + INTEGRATE_SYNTH_BAYESSMART.md):
 *   GET /api/synth/bayes-smart-money/<symbol>
 *
 * Vanilla JS, no dependencies. Re-render-safe: completely owns the
 * container; clobbers innerHTML on each call.
 */

(function (global) {
  'use strict';

  // ── utilities ─────────────────────────────────────────────────────────
  function _esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function _num(v, d) {
    if (v == null || isNaN(v)) return '—';
    return Number(v).toFixed(d == null ? 2 : d);
  }
  function _pct(v, d) {
    if (v == null || isNaN(v)) return '—';
    return (Number(v) * 100).toFixed(d == null ? 1 : d) + '%';
  }
  function _signedPct(v, d) {
    if (v == null || isNaN(v)) return '—';
    var p = Number(v) * 100;
    var dd = d == null ? 1 : d;
    return (p >= 0 ? '+' : '') + p.toFixed(dd) + '%';
  }
  function _signed(v, d) {
    if (v == null || isNaN(v)) return '—';
    var dd = d == null ? 2 : d;
    return (v >= 0 ? '+' : '') + Number(v).toFixed(dd);
  }
  function _color(v) {
    if (v == null || isNaN(v)) return 'var(--text-dim, #888)';
    if (v > 0) return 'var(--green, #1fb950)';
    if (v < 0) return 'var(--red, #ff4444)';
    return 'var(--text-dim, #888)';
  }
  function _scoreColor(score) {
    if (score == null) return 'var(--text-dim, #888)';
    if (score >= 75) return 'var(--green, #1fb950)';
    if (score >= 60) return 'var(--green, #1fb950)';
    if (score >= 45) return 'var(--yellow, #d4a017)';
    if (score >= 30) return 'var(--amber, #d4a017)';
    return 'var(--red, #ff4444)';
  }

  // ── scoped styles (one-shot) ─────────────────────────────────────────
  var STYLE_ID = 'bs-bayes-style';
  var STYLE_CSS = ''
    + '.bs-wrap { font-family: var(--font-mono, monospace); color: var(--text-primary, #ddd); }'
    + '.bs-hero { display:flex; gap:24px; align-items:center; padding:16px;'
    + '   background: var(--panel-bg, #14181d); border:1px solid var(--border, #2a2f36);'
    + '   border-radius: 4px; margin-bottom: 16px; }'
    + '.bs-score-block { text-align:center; min-width:120px; }'
    + '.bs-score-label { font-size:11px; color: var(--text-dim, #888); text-transform:uppercase; letter-spacing:0.08em; }'
    + '.bs-score-value { font-size:42px; font-weight:700; line-height:1.0; margin-top:4px; font-family: var(--font-mono, monospace); }'
    + '.bs-signal { font-size:10px; margin-top:6px; padding:2px 6px; border-radius:2px;'
    + '   border:1px solid currentColor; display:inline-block; }'
    + '.bs-arrow { font-size:32px; padding:0 8px; }'
    + '.bs-table { width:100%; border-collapse: collapse; font-size:12px; }'
    + '.bs-table th { text-align:left; padding:8px 6px; border-bottom:1px solid var(--border, #2a2f36);'
    + '   color: var(--green, #1fb950); font-weight:600; font-size:11px; }'
    + '.bs-table td { padding:8px 6px; border-bottom:1px solid var(--border, #2a2f36);'
    + '   vertical-align: middle; }'
    + '.bs-table tr.bs-shifted { background: rgba(212, 160, 23, 0.08); }'
    + '.bs-table tr:hover td { background: rgba(31, 185, 80, 0.04); }'
    + '.bs-bar-wrap { width:100%; max-width:160px; height:10px; background: var(--border, #2a2f36);'
    + '   border-radius: 2px; position:relative; overflow: hidden; }'
    + '.bs-bar-fill { position:absolute; top:0; height:100%; }'
    + '.bs-bar-zero { position:absolute; left:50%; top:-2px; bottom:-2px; width:1px;'
    + '   background: var(--text-dim, #555); }'
    + '.bs-shift-row { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }'
    + '.bs-shift-card { flex:1; min-width:180px; padding:10px;'
    + '   background: var(--panel-bg, #14181d); border:1px solid var(--border, #2a2f36); border-radius:4px; }'
    + '.bs-shift-title { font-size:10px; color: var(--text-dim, #888); text-transform:uppercase; }'
    + '.bs-shift-value { font-size:18px; font-weight:700; margin-top:4px; }'
    + '.bs-shift-detail { font-size:10px; color: var(--text-secondary, #aaa); margin-top:2px; }'
    + '.bs-pill { font-size:9px; padding:2px 5px; border-radius:2px; background: var(--border, #2a2f36);'
    + '   color: var(--text-dim, #aaa); margin-left:4px; }'
    + '.bs-meta { font-size:10px; color: var(--text-dim, #888); margin-top: 8px; }'
    + '';

  function _ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  // ── fetch ─────────────────────────────────────────────────────────────
  function _fetchJSON(url) {
    return fetch(url, { headers: { 'Accept': 'application/json' } })
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      });
  }

  // ── renderers ─────────────────────────────────────────────────────────
  function _renderHero(data) {
    var staticScore = data.static_score;
    var bayesScore = data.bayes_score;
    var delta = (staticScore != null && bayesScore != null)
      ? (bayesScore - staticScore)
      : null;
    var arrow = '—';
    var arrowColor = 'var(--text-dim, #888)';
    if (delta != null) {
      if (delta > 0.5) { arrow = '↑'; arrowColor = 'var(--green, #1fb950)'; }
      else if (delta < -0.5) { arrow = '↓'; arrowColor = 'var(--red, #ff4444)'; }
      else { arrow = '≈'; arrowColor = 'var(--text-dim, #888)'; }
    }
    return ''
      + '<div class="bs-hero">'
      + '  <div class="bs-score-block">'
      + '    <div class="bs-score-label">Static Score</div>'
      + '    <div class="bs-score-value" style="color:' + _scoreColor(staticScore) + '">'
      +        (staticScore != null ? Math.round(staticScore) : '—')
      + '    </div>'
      + (data.static_signal
          ? '    <div class="bs-signal" style="color:' + _scoreColor(staticScore) + '">' + _esc(data.static_signal) + '</div>'
          : '')
      + '  </div>'
      + '  <div class="bs-arrow" style="color:' + arrowColor + '">' + arrow + '</div>'
      + '  <div class="bs-score-block">'
      + '    <div class="bs-score-label">Bayes Score</div>'
      + '    <div class="bs-score-value" style="color:' + _scoreColor(bayesScore) + '">'
      +        (bayesScore != null ? Math.round(bayesScore) : '—')
      + '    </div>'
      + (data.bayes_signal
          ? '    <div class="bs-signal" style="color:' + _scoreColor(bayesScore) + '">' + _esc(data.bayes_signal) + '</div>'
          : '')
      + '  </div>'
      + '  <div style="flex:1; padding-left: 16px;">'
      + '    <div style="font-size:11px;color:var(--text-dim,#888);text-transform:uppercase">Score Delta</div>'
      + '    <div style="font-size:22px;font-weight:700;color:' + _color(delta) + '">'
      +        (delta != null ? _signed(delta, 2) : '—')
      + '    </div>'
      + '    <div style="font-size:10px;color:var(--text-secondary,#aaa);margin-top:4px">'
      + '      Bayesian reweighting based on live signal track-records'
      + '    </div>'
      + '  </div>'
      + '</div>';
  }

  function _renderShiftSummary(shifts) {
    if (!shifts || !shifts.length) return '';
    var cards = shifts.map(function (s) {
      return ''
        + '<div class="bs-shift-card">'
        + '  <div class="bs-shift-title">' + _esc(s.label || s.name) + '</div>'
        + '  <div class="bs-shift-value" style="color:' + _color(s.shift) + '">'
        + _signedPct(s.shift, 1)
        + '  </div>'
        + '  <div class="bs-shift-detail">'
        +    _pct(s.static_weight, 1) + ' → ' + _pct(s.posterior_weight, 1)
        + '  </div>'
        + '</div>';
    }).join('');
    return ''
      + '<div style="font-size:11px;color:var(--green,#1fb950);margin-bottom:6px;text-transform:uppercase;letter-spacing:0.05em">Biggest Weight Shifts</div>'
      + '<div class="bs-shift-row">' + cards + '</div>';
  }

  function _renderComponentTable(components, shiftNames) {
    if (!components || !components.length) {
      return '<div style="color:var(--text-dim);padding:16px">No components available.</div>';
    }
    var rows = components.map(function (c) {
      var shifted = shiftNames.indexOf(c.name) >= 0;
      var contrib = c.contribution || 0;
      // The contribution bar spans the [-postW..+postW] range centred on
      // 50%. Width as a fraction of the bar.
      var post = c.posterior_weight || 0;
      var widthFrac = 0;
      if (post > 0) {
        widthFrac = Math.min(1.0, Math.abs(contrib) / post) * 50; // half-width %
      }
      var barLeft = contrib >= 0 ? '50%' : (50 - widthFrac) + '%';
      var barWidth = widthFrac + '%';
      var barColor = _color(contrib);
      var tr = c.track_record || {};
      var hitTxt = tr.hit_rate != null
        ? (_pct(tr.hit_rate, 0) + ' (n=' + (tr.n_directional || tr.n || 0) + ')')
        : ('<span style="color:var(--text-dim,#888)">n=' + (tr.n || 0) + '</span>');
      return ''
        + '<tr class="' + (shifted ? 'bs-shifted' : '') + '">'
        + '  <td><b>' + _esc(c.label) + '</b>'
        + (shifted ? '<span class="bs-pill">SHIFTED</span>' : '')
        + '  </td>'
        + '  <td>' + (c.score != null ? c.score : '—') + '/' + (c.max || '—') + '</td>'
        + '  <td style="color:' + _color(c.normalized) + '">' + _signed(c.normalized, 2) + '</td>'
        + '  <td>' + hitTxt + '</td>'
        + '  <td>' + _pct(c.static_weight, 1) + '</td>'
        + '  <td><b style="color:' + _color((c.posterior_weight - c.static_weight)) + '">'
        +     _pct(c.posterior_weight, 1) + '</b></td>'
        + '  <td>'
        + '    <div class="bs-bar-wrap">'
        + '      <div class="bs-bar-zero"></div>'
        + '      <div class="bs-bar-fill" style="left:' + barLeft + ';width:' + barWidth + ';background:' + barColor + '"></div>'
        + '    </div>'
        + '    <div style="font-size:10px;color:' + _color(contrib) + ';margin-top:2px">' + _signed(contrib, 3) + '</div>'
        + '  </td>'
        + '</tr>';
    }).join('');
    return ''
      + '<table class="bs-table">'
      + '  <thead><tr>'
      + '    <th>Component</th>'
      + '    <th>Score</th>'
      + '    <th>Normalized</th>'
      + '    <th>Track Record</th>'
      + '    <th>Static W</th>'
      + '    <th>Posterior W</th>'
      + '    <th>Contribution</th>'
      + '  </tr></thead>'
      + '  <tbody>' + rows + '</tbody>'
      + '</table>';
  }

  function _renderError(container, sym, msg) {
    container.innerHTML = ''
      + '<div class="bs-wrap" style="padding:24px;color:var(--red,#ff4444)">'
      + '  Bayes Smart Money error for <b>' + _esc(sym) + '</b>: ' + _esc(msg)
      + '</div>';
  }

  async function renderBayesSmart(container, symbol) {
    _ensureStyle();
    var sym = (symbol || '').toString().toUpperCase().trim();
    if (!container) return;
    if (!sym) {
      container.innerHTML = '<div style="padding:24px;color:var(--text-dim)">Enter a symbol to see the Bayesian Smart Money composite.</div>';
      return;
    }
    container.innerHTML = ''
      + '<div class="bs-wrap" style="padding:16px;color:var(--text-dim,#888)">'
      + '  Computing Bayesian composite for <b>' + _esc(sym) + '</b>…'
      + '</div>';

    try {
      var data = await _fetchJSON('/api/synth/bayes-smart-money/' + encodeURIComponent(sym));
      if (!data || data.error) {
        _renderError(container, sym, (data && data.error) || 'no data');
        return;
      }
      var shiftNames = (data.weight_shift_summary || []).map(function (s) { return s.name; });

      var html = ''
        + '<div class="bs-wrap">'
        + '  <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px">'
        + '    <div style="font-size:14px;font-weight:700;color:var(--green,#1fb950)">◈ BAYES SMART MONEY — ' + _esc(sym) + '</div>'
        + '    <div style="font-size:10px;color:var(--text-dim,#888)">'
        +      'as of ' + _esc((data.as_of || '').replace('T', ' ').replace('Z', ' UTC'))
        + '    </div>'
        + '  </div>'
        +   _renderHero(data)
        +   _renderShiftSummary(data.weight_shift_summary)
        +   _renderComponentTable(data.components || [], shiftNames)
        + '  <div class="bs-meta">'
        + '    Prior: Beta(α=' + (data.prior ? data.prior.alpha : 20) + ', β=' + (data.prior ? data.prior.beta : 20)
        + '), min_n=' + (data.prior ? data.prior.min_n : 20)
        + ', weight_exponent=' + (data.prior ? data.prior.weight_exponent : 1.5)
        + '    — components with fewer than min_n scored calls keep their static weight (multiplier = 1.0).'
        + '  </div>'
        + '</div>';
      container.innerHTML = html;
    } catch (e) {
      _renderError(container, sym, e.message || String(e));
    }
  }

  global.renderBayesSmart = renderBayesSmart;
}(window));
