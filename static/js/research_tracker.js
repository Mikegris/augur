/*
 * research_tracker.js — live signal track-record UI helpers.
 *
 * Exposes two globals the rest of the app can call after rendering a signal
 * panel. Both are no-ops if the API or container element is missing, so
 * they're safe to fire on every render without try/catch around them.
 *
 *   window.renderTrackBadge(el, signalName)
 *       Tiny inline summary, e.g.   [●] 58% (n=143)
 *       Coloring:  >=55%  green
 *                  45-55  yellow
 *                  <45    red
 *
 *   window.renderTrackRecord(containerEl, signalName)
 *       Full panel with a sparkline of cumulative hits and a table of the
 *       last 20 scored calls.
 *
 * Both endpoints come from research_tracker.py via INTEGRATE_TRACKER.md:
 *   GET /api/research/track/<signal_name>
 *   GET /api/research/track/<signal_name>/calls
 *
 * No dependencies — vanilla JS, fetch only, ES2017+. Matches the in-line
 * style the rest of app.js uses (string concatenation rather than templates
 * where it keeps the file linear).
 */
(function() {
  'use strict';

  // ── HTML-escape (mirrors app.js _esc) ─────────────────────────────────────
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, function(c) {
      return ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'})[c];
    });
  }

  function fmtPct(x, digits) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    return (x * 100).toFixed(digits == null ? 1 : digits) + '%';
  }

  function fmtSignedPct(x) {
    if (x === null || x === undefined || isNaN(x)) return '—';
    var v = x * 100;
    return (v >= 0 ? '+' : '') + v.toFixed(2) + '%';
  }

  function hitRateColor(rate) {
    if (rate == null) return 'var(--text-dim, #888)';
    if (rate >= 0.55) return 'var(--green, #1fb950)';
    if (rate >= 0.45) return 'var(--yellow, #d4a017)';
    return 'var(--red, #ff4444)';
  }

  function dotColor(rate, n) {
    // Don't trust hit rate without a sample size — show a gray dot if
    // we've got fewer than 10 scored calls.
    if (n < 10 || rate == null) return 'var(--text-dim, #888)';
    return hitRateColor(rate);
  }

  // Lightweight fetch with a 6s timeout. The track-record endpoint is a
  // tiny SQLite query so it should be sub-100ms; anything longer means
  // app.py is down and we shouldn't block the parent panel.
  async function fetchJSON(url) {
    var ctl = new AbortController();
    var to = setTimeout(function() { ctl.abort(); }, 6000);
    try {
      var r = await fetch(url, { signal: ctl.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return await r.json();
    } finally {
      clearTimeout(to);
    }
  }

  // ── small inline badge ────────────────────────────────────────────────────
  async function renderTrackBadge(el, signalName) {
    if (!el || !signalName) return;
    el.innerHTML = '<span style="font-size:9px;color:var(--text-dim, #888)">track…</span>';
    try {
      var data = await fetchJSON('/api/research/track/' + encodeURIComponent(signalName));
      var n = data.n || 0;
      // Prefer last-100 over all-time so the badge reflects recent skill.
      var rate = data.last_100_hit_rate != null ? data.last_100_hit_rate : data.hit_rate;
      var avg = data.avg_return;
      var color = dotColor(rate, n);

      if (!n) {
        el.innerHTML = ''
          + '<span title="No scored calls yet" '
          + '   style="font-size:10px;color:var(--text-dim, #888);font-family:monospace">'
          + '<span style="color:' + color + '">●</span> no track record</span>';
        return;
      }

      var tooltip = signalName + ' — n=' + n
        + ', hit ' + fmtPct(data.hit_rate, 1)
        + ', avg ' + fmtSignedPct(avg)
        + (data.sharpe_of_signal_returns != null
            ? ', sharpe ' + data.sharpe_of_signal_returns.toFixed(2) : '');

      el.innerHTML = ''
        + '<span title="' + esc(tooltip) + '" '
        + '   style="display:inline-flex;align-items:center;gap:4px;'
        + '          font-size:10px;font-family:monospace;'
        + '          color:var(--text-secondary, #aaa)">'
        + '  <span style="color:' + color + ';font-size:11px">●</span>'
        + '  <span style="color:' + hitRateColor(rate) + ';font-weight:600">'
        +     fmtPct(rate, 0) + '</span>'
        + '  <span style="color:var(--text-dim, #888)">'
        + '    (n=' + n + (avg != null ? ', ' + fmtSignedPct(avg) + ' avg' : '') + ')'
        + '  </span>'
        + '</span>';
    } catch (e) {
      // Silent failure — don't make a network blip break the parent panel.
      el.innerHTML = '<span style="font-size:9px;color:var(--text-dim, #888)" '
        + 'title="' + esc('track record unavailable: ' + e.message) + '">'
        + '<span style="color:var(--text-dim, #888)">●</span> n/a</span>';
    }
  }

  // ── tiny sparkline of cumulative hit rate ─────────────────────────────────
  function sparkline(calls) {
    var directional = (calls || []).filter(function(c) {
      return c.hit === 0 || c.hit === 1;
    });
    if (!directional.length) return '';

    // Walk newest-last so the sparkline reads left=oldest, right=newest.
    directional = directional.slice().reverse();
    var w = 140, h = 28, pad = 2;
    var hits = 0;
    var pts = [];
    for (var i = 0; i < directional.length; i++) {
      if (directional[i].hit) hits++;
      var rate = hits / (i + 1);
      var x = pad + (w - 2 * pad) * (i / Math.max(directional.length - 1, 1));
      var y = h - pad - (h - 2 * pad) * rate;
      pts.push(x.toFixed(1) + ',' + y.toFixed(1));
    }
    var path = pts.join(' ');
    var lastRate = hits / directional.length;
    var color = hitRateColor(lastRate);

    return ''
      + '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '"'
      + '     style="display:block">'
      + '  <line x1="' + pad + '" y1="' + (h / 2).toFixed(1) + '" '
      + '        x2="' + (w - pad) + '" y2="' + (h / 2).toFixed(1) + '"'
      + '        stroke="var(--border, #333)" stroke-dasharray="2,2" stroke-width="0.5"/>'
      + '  <polyline points="' + path + '" fill="none" stroke="' + color + '"'
      + '            stroke-width="1.4" stroke-linejoin="round"/>'
      + '</svg>';
  }

  // ── full panel: stats + sparkline + table ────────────────────────────────
  async function renderTrackRecord(containerEl, signalName) {
    if (!containerEl || !signalName) return;
    containerEl.innerHTML =
      '<div style="font-size:10px;color:var(--text-dim, #888);padding:8px">'
      + 'Loading track record for ' + esc(signalName) + '…</div>';

    var stats, calls;
    try {
      var pair = await Promise.all([
        fetchJSON('/api/research/track/' + encodeURIComponent(signalName)),
        fetchJSON('/api/research/track/' + encodeURIComponent(signalName) + '/calls?limit=20'),
      ]);
      stats = pair[0] || {};
      calls = (pair[1] && pair[1].calls) || pair[1] || [];
    } catch (e) {
      containerEl.innerHTML =
        '<div style="font-size:11px;color:var(--red, #ff4444);padding:8px">'
        + 'Track record unavailable: ' + esc(e.message) + '</div>';
      return;
    }

    if (!stats.n) {
      containerEl.innerHTML =
        '<div style="font-size:11px;color:var(--text-dim, #888);padding:8px">'
        + 'No scored calls yet for <code>' + esc(signalName) + '</code>. '
        + 'Track record builds as forecasts age past their horizon.</div>';
      return;
    }

    var hitRate = stats.hit_rate;
    var rateColor = hitRateColor(hitRate);
    var nSuffix = stats.n_directional !== stats.n ? ' / ' + stats.n + ' total' : '';
    var avgColor = (stats.avg_return != null && stats.avg_return >= 0)
      ? 'var(--green, #1fb950)' : 'var(--red, #ff4444)';
    var sharpeText = (stats.sharpe_of_signal_returns != null)
      ? 'sharpe ' + stats.sharpe_of_signal_returns.toFixed(2)
      : 'sharpe —';

    var html = ''
      + '<div style="display:flex;gap:16px;align-items:center;padding:8px 4px;'
      + '            border-bottom:1px solid var(--border, #333);margin-bottom:8px">'
      + '  <div>'
      + '    <div style="font-size:9px;color:var(--text-dim, #888);text-transform:uppercase;'
      + '                letter-spacing:0.05em">Hit rate</div>'
      + '    <div style="font-size:20px;font-weight:700;color:' + rateColor + ';'
      + '                font-family:monospace">' + fmtPct(hitRate, 1) + '</div>'
      + '    <div style="font-size:9px;color:var(--text-dim, #888)">n='
      +       stats.n_directional + nSuffix + '</div>'
      + '  </div>'
      + '  <div>'
      + '    <div style="font-size:9px;color:var(--text-dim, #888);text-transform:uppercase;'
      + '                letter-spacing:0.05em">Avg return</div>'
      + '    <div style="font-size:20px;font-weight:700;font-family:monospace;'
      + '                color:' + avgColor + '">'
      +       fmtSignedPct(stats.avg_return) + '</div>'
      + '    <div style="font-size:9px;color:var(--text-dim, #888)">' + sharpeText + '</div>'
      + '  </div>'
      + '  <div style="flex:1;text-align:right">'
      + '    <div style="font-size:9px;color:var(--text-dim, #888);text-transform:uppercase;'
      + '                letter-spacing:0.05em;margin-bottom:2px">'
      + '      Cumulative hit-rate · last ' + Math.min(calls.length, 100) + ' calls</div>'
      +       sparkline(calls)
      + '  </div>'
      + '</div>';

    // ── recent calls table ─────────────────────────────────────────────────
    html += '<table style="width:100%;border-collapse:collapse;font-size:10px;'
      +     'font-family:monospace">';
    html += '<thead><tr style="color:var(--text-dim, #888);text-transform:uppercase">'
      +     '<th style="text-align:left;padding:4px;font-weight:500">Symbol</th>'
      +     '<th style="text-align:left;padding:4px;font-weight:500">Dir</th>'
      +     '<th style="text-align:right;padding:4px;font-weight:500">Issued</th>'
      +     '<th style="text-align:right;padding:4px;font-weight:500">Horizon</th>'
      +     '<th style="text-align:right;padding:4px;font-weight:500">Realized</th>'
      +     '<th style="text-align:center;padding:4px;font-weight:500">Hit</th>'
      +     '</tr></thead><tbody>';
    for (var i = 0; i < calls.length; i++) {
      var c = calls[i];
      var dirCol = c.direction === 'BUY' || c.direction === 'STRONG BUY' || c.direction === 'LONG'
        ? 'var(--green, #1fb950)'
        : (c.direction === 'SELL' || c.direction === 'SHORT'
            ? 'var(--red, #ff4444)' : 'var(--text-secondary, #aaa)');
      var retCol = c.realized_return != null && c.realized_return >= 0
        ? 'var(--green, #1fb950)' : 'var(--red, #ff4444)';
      var hitGlyph = c.hit === 1
        ? '<span style="color:var(--green, #1fb950)">✓</span>'
        : (c.hit === 0
            ? '<span style="color:var(--red, #ff4444)">✗</span>'
            : '<span style="color:var(--text-dim, #888)">—</span>');
      var issuedShort = (c.issued_at || '').slice(0, 10);

      html += '<tr style="border-top:1px solid var(--border, #2a2a2a)">'
        + '<td style="padding:4px;color:var(--text-primary, #fff)">' + esc(c.symbol) + '</td>'
        + '<td style="padding:4px;color:' + dirCol + '">' + esc(c.direction || '—') + '</td>'
        + '<td style="padding:4px;text-align:right;color:var(--text-dim, #888)">' + esc(issuedShort) + '</td>'
        + '<td style="padding:4px;text-align:right;color:var(--text-secondary, #aaa)">'
        +   esc(c.horizon_days) + 'd</td>'
        + '<td style="padding:4px;text-align:right;color:' + retCol + '">'
        +   fmtSignedPct(c.realized_return) + '</td>'
        + '<td style="padding:4px;text-align:center">' + hitGlyph + '</td>'
        + '</tr>';
    }
    html += '</tbody></table>';

    containerEl.innerHTML = html;
  }

  // ── publish on window so app.js can call straight in ──────────────────────
  window.renderTrackBadge = renderTrackBadge;
  window.renderTrackRecord = renderTrackRecord;
})();
