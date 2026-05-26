/* research_backtest.js — Walk-forward backtest panel for AUGUR.
 *
 * Renders /api/research/backtest output: a metrics strip, a Chart.js equity
 * curve, and best/worst signal tables.
 *
 * Public entry point: window.renderBacktest(containerEl, symbol, signalName, params)
 *   - containerEl: a DOM element the panel can own (innerHTML is replaced)
 *   - symbol:      ticker string
 *   - signalName:  one of "momentum" | "mean_reversion" | "ml_forecast"
 *   - params:      optional { start?, end?, horizon_override? } (extra keys
 *                  are passed to the API as-is)
 */

(function () {
  'use strict';

  const SIGNALS = [
    { value: 'momentum',       label: 'Momentum (20d/quartile)' },
    { value: 'mean_reversion', label: 'Mean Reversion (z>1.5)' },
    { value: 'ml_forecast',    label: 'ML Forecast (RF) — best-effort' },
  ];

  // One Chart.js instance per container so re-renders don't double-allocate
  // the canvas (Chart.js refuses to reuse a bound canvas otherwise).
  const _CHART_REG = new WeakMap();

  function _esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  function _fmtPct(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    const d = (digits == null) ? 2 : digits;
    const sign = v > 0 ? '+' : '';
    return sign + Number(v).toFixed(d) + '%';
  }

  function _fmtNum(v, digits) {
    if (v == null || !isFinite(v)) return '—';
    const d = (digits == null) ? 3 : digits;
    return Number(v).toFixed(d);
  }

  function _pnlClass(v) {
    if (v == null || !isFinite(v)) return '';
    return v >= 0 ? 'pnl-pos' : 'pnl-neg';
  }

  function _slug(s) {
    return String(s == null ? '' : s).replace(/[^A-Za-z0-9_-]+/g, '_');
  }

  function _api(symbol, signalName, params) {
    const q = new URLSearchParams({ symbol: symbol, signal: signalName });
    if (params) {
      Object.keys(params).forEach(k => {
        const v = params[k];
        if (v != null && v !== '') q.set(k, String(v));
      });
    }
    return fetch('/api/research/backtest?' + q.toString())
      .then(r => r.json());
  }

  function _buildSkeleton(containerEl, symbol, signalName) {
    const sym = _esc(symbol);
    const sl  = _slug(symbol);
    const opts = SIGNALS.map(s =>
      `<option value="${s.value}" ${s.value === signalName ? 'selected' : ''}>${s.label}</option>`
    ).join('');
    containerEl.innerHTML = `
      <div class="panel" id="bt-panel-${sl}">
        <div class="panel-header">
          <span class="panel-title">BACKTEST — ${sym}</span>
          <div style="display:flex;gap:8px;align-items:center;font-size:11px">
            <label>Signal:
              <select id="bt-signal-${sl}" style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px">
                ${opts}
              </select>
            </label>
            <label>Start:
              <input type="date" id="bt-start-${sl}" value=""
                style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px;font-family:inherit">
            </label>
            <label>End:
              <input type="date" id="bt-end-${sl}" value=""
                style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px;font-family:inherit">
            </label>
            <button class="btn btn-ghost btn-sm" id="bt-run-${sl}">RUN</button>
          </div>
        </div>
        <div class="panel-body">
          <div id="bt-metrics-${sl}" style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px;font-size:11px;padding:6px 0;border-bottom:1px solid var(--border)"></div>
          <div style="position:relative;height:260px;margin-top:8px">
            <canvas id="bt-chart-${sl}"></canvas>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:8px;font-size:11px">
            <div>
              <div style="color:var(--green);font-size:10px;letter-spacing:0.06em;margin-bottom:4px">BEST 5 SIGNALS</div>
              <div id="bt-best-${sl}"></div>
            </div>
            <div>
              <div style="color:var(--red);font-size:10px;letter-spacing:0.06em;margin-bottom:4px">WORST 5 SIGNALS</div>
              <div id="bt-worst-${sl}"></div>
            </div>
          </div>
          <div id="bt-status-${sl}" style="font-size:10px;color:var(--text-dim);margin-top:6px"></div>
        </div>
      </div>
    `;
  }

  function _setStatus(sl, msg, klass) {
    const el = document.getElementById(`bt-status-${sl}`);
    if (!el) return;
    el.className = klass ? klass : '';
    el.style.fontSize = '10px';
    el.style.marginTop = '6px';
    el.textContent = msg || '';
  }

  function _renderMetrics(sl, data) {
    const el = document.getElementById(`bt-metrics-${sl}`);
    if (!el) return;
    // Each tile: small dim label above, larger value below. Match the
    // pnl-pos/pnl-neg classes used elsewhere in AUGUR for tone.
    const tile = (label, val, klass) =>
      `<div>
         <div style="color:var(--text-dim);font-size:10px;letter-spacing:0.06em">${label}</div>
         <div class="${klass || ''}" style="font-size:14px;font-weight:600">${val}</div>
       </div>`;
    el.innerHTML = [
      tile('SIGNALS',     String(data.n_signals != null ? data.n_signals : '—')),
      tile('HIT RATE',    data.hit_rate != null && isFinite(data.hit_rate)
                            ? (data.hit_rate * 100).toFixed(1) + '%'
                            : '—',
                          data.hit_rate >= 0.55 ? 'pnl-pos' : (data.hit_rate <= 0.45 ? 'pnl-neg' : '')),
      tile('AVG / SIG',   _fmtPct(data.avg_return_per_signal), _pnlClass(data.avg_return_per_signal)),
      tile('TOTAL RET',   _fmtPct(data.total_return_pct),       _pnlClass(data.total_return_pct)),
      tile('SHARPE',      _fmtNum(data.sharpe, 2),              _pnlClass(data.sharpe)),
      tile('MAX DD',      _fmtPct(data.max_drawdown_pct),       'pnl-neg'),
    ].join('');
  }

  function _destroyChart(containerEl) {
    const existing = _CHART_REG.get(containerEl);
    if (existing) {
      try { existing.destroy(); } catch (e) { /* ignore */ }
      _CHART_REG.delete(containerEl);
    }
  }

  function _drawChart(containerEl, sl, data) {
    if (typeof Chart === 'undefined') return;
    const canvas = document.getElementById(`bt-chart-${sl}`);
    if (!canvas) return;
    _destroyChart(containerEl);

    const points = (data.equity_curve || []).map(p => ({ x: p.date, y: p.nav }));
    if (!points.length) {
      // Wipe the canvas if we got no data — leaves an empty rect rather than
      // a stale chart from the previous run.
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      return;
    }

    const isPositive = points[points.length - 1].y >= points[0].y;
    const lineColor = isPositive
      ? (getComputedStyle(document.documentElement).getPropertyValue('--green').trim() || '#00ff9f')
      : (getComputedStyle(document.documentElement).getPropertyValue('--red').trim()   || '#ff3355');
    const dim = getComputedStyle(document.documentElement).getPropertyValue('--text-dim').trim() || '#3a5a70';
    const grid = getComputedStyle(document.documentElement).getPropertyValue('--border').trim() || '#0f2a3a';

    const chart = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: points.map(p => p.x),
        datasets: [{
          label: 'NAV',
          data: points.map(p => p.y),
          borderColor: lineColor,
          borderWidth: 1.2,
          pointRadius: 0,
          fill: true,
          backgroundColor: lineColor + '22',
          tension: 0.15,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: (ctx) => 'NAV ' + Number(ctx.parsed.y).toFixed(4),
            },
          },
        },
        scales: {
          x: {
            ticks: { color: dim, font: { size: 9, family: 'monospace' }, maxTicksLimit: 6 },
            grid: { color: grid },
          },
          y: {
            ticks: { color: dim, font: { size: 9, family: 'monospace' } },
            grid: { color: grid },
          },
        },
      },
    });
    _CHART_REG.set(containerEl, chart);
  }

  function _renderTable(elId, signals) {
    const el = document.getElementById(elId);
    if (!el) return;
    if (!signals || !signals.length) {
      el.innerHTML = '<div style="color:var(--text-dim);font-size:10px">— No signals —</div>';
      return;
    }
    // Each row: date | direction | confidence | realised return.
    // Hover tooltip shows entry/exit price + exit date.
    const rows = signals.map(s => {
      const r = (s.realized_return == null) ? null : s.realized_return * 100;
      // For SELL signals positive return is a *loss*, so colour by directional pnl.
      const directional = (s.direction === 'SELL') ? -(s.realized_return || 0)
                                                   :  (s.realized_return || 0);
      const cls = directional >= 0 ? 'pnl-pos' : 'pnl-neg';
      const title = [
        'Entry: ' + (s.entry_price != null ? s.entry_price : '—'),
        'Exit:  ' + (s.exit_price  != null ? s.exit_price  : '—'),
        'Exit date: ' + (s.exit_date || '—'),
        'Horizon: ' + (s.horizon_days != null ? s.horizon_days + 'd' : '—'),
        'Confidence: ' + (s.confidence != null ? s.confidence.toFixed(2) : '—'),
      ].join('\n');
      return `<tr title="${_esc(title)}">
                <td style="color:var(--text-secondary)">${_esc(s.issued_at || '')}</td>
                <td style="color:${s.direction === 'BUY' ? 'var(--green)' : 'var(--red)'}">${_esc(s.direction || '')}</td>
                <td style="color:var(--text-dim);text-align:right">${s.confidence != null ? s.confidence.toFixed(2) : '—'}</td>
                <td class="${cls}" style="text-align:right">${_fmtPct(r)}</td>
              </tr>`;
    }).join('');
    el.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-family:monospace;font-size:11px">
        <thead>
          <tr style="color:var(--text-dim);font-size:10px;letter-spacing:0.06em">
            <th style="text-align:left;padding:2px 4px">DATE</th>
            <th style="text-align:left;padding:2px 4px">DIR</th>
            <th style="text-align:right;padding:2px 4px">CONF</th>
            <th style="text-align:right;padding:2px 4px">RET</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  function _bestAndWorst(signals) {
    if (!signals || !signals.length) return { best: [], worst: [] };
    // Rank by *directional* pnl so SELLs that worked are counted as wins.
    const scored = signals
      .filter(s => s && typeof s.realized_return === 'number' && isFinite(s.realized_return))
      .map(s => ({
        s: s,
        d: (s.direction === 'SELL') ? -s.realized_return : s.realized_return,
      }));
    scored.sort((a, b) => b.d - a.d);
    return {
      best:  scored.slice(0, 5).map(x => x.s),
      worst: scored.slice(-5).reverse().map(x => x.s),
    };
  }

  function _renderError(containerEl, symbol, message) {
    containerEl.innerHTML = `
      <div class="panel">
        <div class="panel-header"><span class="panel-title">BACKTEST — ${_esc(symbol)}</span></div>
        <div class="panel-body">
          <div style="color:var(--red);font-size:12px">⚠ ${_esc(message || 'Backtest failed')}</div>
        </div>
      </div>
    `;
  }

  function _wireControls(containerEl, symbol, sl, params) {
    const runBtn   = document.getElementById(`bt-run-${sl}`);
    const sigSel   = document.getElementById(`bt-signal-${sl}`);
    const startEl  = document.getElementById(`bt-start-${sl}`);
    const endEl    = document.getElementById(`bt-end-${sl}`);
    if (params && params.start && startEl) startEl.value = params.start;
    if (params && params.end   && endEl)   endEl.value   = params.end;

    const run = () => {
      const nextSig = sigSel ? sigSel.value : 'momentum';
      const nextParams = Object.assign({}, params || {}, {
        start: startEl && startEl.value ? startEl.value : undefined,
        end:   endEl   && endEl.value   ? endEl.value   : undefined,
      });
      _fetchAndRender(containerEl, symbol, nextSig, nextParams);
    };
    if (runBtn) runBtn.addEventListener('click', run);
    if (sigSel) sigSel.addEventListener('change', run);
  }

  function _fetchAndRender(containerEl, symbol, signalName, params) {
    const sl = _slug(symbol);
    _setStatus(sl, 'Running backtest...', '');
    _api(symbol, signalName, params).then(data => {
      if (!data || data.error) {
        _setStatus(sl, data && data.error ? data.error : 'No data', 'pnl-neg');
        _renderMetrics(sl, data || {});
        _drawChart(containerEl, sl, { equity_curve: [] });
        _renderTable(`bt-best-${sl}`, []);
        _renderTable(`bt-worst-${sl}`, []);
        return;
      }
      _renderMetrics(sl, data);
      _drawChart(containerEl, sl, data);
      const bw = _bestAndWorst(data.signals || []);
      _renderTable(`bt-best-${sl}`,  bw.best);
      _renderTable(`bt-worst-${sl}`, bw.worst);
      const start = data.start || '—';
      const end   = data.end   || '—';
      _setStatus(sl, `Signal: ${data.signal || signalName} · Window: ${start} → ${end}`
                     + (data.signals_truncated_to ? ` · signals truncated to ${data.signals_truncated_to}` : ''));
    }).catch(err => {
      _renderError(containerEl, symbol, (err && err.message) || String(err));
    });
  }

  function renderBacktest(containerEl, symbol, signalName, params) {
    if (!containerEl) return;
    symbol = String(symbol || '').toUpperCase().trim();
    if (!symbol) {
      _renderError(containerEl, '', 'No symbol provided');
      return;
    }
    signalName = (signalName || 'momentum').toLowerCase();
    _buildSkeleton(containerEl, symbol, signalName);
    _wireControls(containerEl, symbol, _slug(symbol), params || {});
    _fetchAndRender(containerEl, symbol, signalName, params || {});
  }

  // Expose the entry point in the same style as the other research_*.js files.
  window.renderBacktest = renderBacktest;
})();
