/* research_eventstudy.js — Event Study panel for AUGUR's Research view.
 *
 * Renders the average / median / interquartile fan from
 *   /api/research/event-study/<symbol>?event_type=...&window_days=...
 * into a Chart.js line chart, plus a KPI strip and a small best/worst table.
 *
 * Exposes a single global: window.renderEventStudy(containerEl, symbol,
 * eventType). The containerEl is whatever the host page reserves for the
 * panel; we own its innerHTML.
 */

(function () {
  'use strict';

  const EVENT_TYPES = [
    { value: 'earnings',    label: 'Earnings' },
    { value: 'fed_fomc',    label: 'FOMC Meeting' },
    { value: 'opex',        label: 'Monthly OPEX' },
    { value: 'ex_dividend', label: 'Ex-Dividend' },
  ];

  // Track one chart instance per container so we can destroy on re-render.
  // Without this Chart.js will refuse to re-use a canvas with "Canvas is
  // already in use".
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

  function _pnlClass(v) {
    if (v == null || !isFinite(v)) return '';
    return v >= 0 ? 'pnl-pos' : 'pnl-neg';
  }

  function _api(symbol, eventType, windowDays, periodYears, benchmark) {
    const q = new URLSearchParams({
      event_type: eventType,
      window_days: String(windowDays || 10),
      period_years: String(periodYears || 10),
      benchmark: benchmark || 'SPY',
    });
    return fetch(`/api/research/event-study/${encodeURIComponent(symbol)}?` + q.toString())
      .then(r => r.json());
  }

  function _buildSkeleton(containerEl, symbol, eventType, windowDays) {
    const opts = EVENT_TYPES.map(et =>
      `<option value="${et.value}" ${et.value === eventType ? 'selected' : ''}>${et.label}</option>`
    ).join('');

    containerEl.innerHTML = `
      <div class="panel" id="es-panel-${_esc(symbol)}">
        <div class="panel-header">
          <span class="panel-title">EVENT STUDY — ${_esc(symbol)}</span>
          <div style="display:flex;gap:8px;align-items:center;font-size:11px">
            <label>Event:
              <select id="es-event-${_esc(symbol)}" style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px">
                ${opts}
              </select>
            </label>
            <label>Window (±days):
              <input type="range" id="es-window-${_esc(symbol)}" min="3" max="30" value="${windowDays}" style="vertical-align:middle">
              <span id="es-window-label-${_esc(symbol)}" style="display:inline-block;min-width:24px;text-align:right">${windowDays}</span>
            </label>
            <button class="btn btn-ghost btn-sm" id="es-refresh-${_esc(symbol)}">RUN</button>
          </div>
        </div>
        <div class="panel-body" style="display:grid;grid-template-columns:2fr 1fr;gap:10px">
          <div>
            <div style="position:relative;height:300px">
              <canvas id="es-chart-${_esc(symbol)}"></canvas>
            </div>
            <div id="es-status-${_esc(symbol)}" style="font-size:10px;color:var(--text-dim);margin-top:6px"></div>
          </div>
          <div id="es-side-${_esc(symbol)}" style="font-size:11px"></div>
        </div>
      </div>
    `;
  }

  function _destroyChart(containerEl) {
    const existing = _CHART_REG.get(containerEl);
    if (existing) {
      try { existing.destroy(); } catch (e) { /* ignore */ }
      _CHART_REG.delete(containerEl);
    }
  }

  function _drawChart(containerEl, symbol, data) {
    if (typeof Chart === 'undefined') {
      return; // Chart.js absent — skip silently
    }
    const canvas = document.getElementById(`es-chart-${symbol}`);
    if (!canvas) return;

    _destroyChart(containerEl);

    const w = data.window_days;
    const xs = [];
    for (let i = -w; i <= w; i++) xs.push(i);

    const avg = data.average_curve || [];
    const med = data.median_curve || [];
    const p25 = data.p25_curve || [];
    const p75 = data.p75_curve || [];

    // p25..p75 band: draw as two lines, the upper filled to the lower with
    // a faint translucent fill. Chart.js needs the 'fill' target to be the
    // dataset index of the line to fill to.
    const datasets = [
      {
        label: 'p75',
        data: p75,
        borderColor: 'rgba(120, 200, 255, 0.35)',
        borderWidth: 1,
        pointRadius: 0,
        fill: '+1',
        backgroundColor: 'rgba(120, 200, 255, 0.10)',
        tension: 0.15,
      },
      {
        label: 'p25',
        data: p25,
        borderColor: 'rgba(120, 200, 255, 0.35)',
        borderWidth: 1,
        pointRadius: 0,
        fill: false,
        tension: 0.15,
      },
      {
        label: 'median',
        data: med,
        borderColor: 'rgba(255, 200, 80, 0.85)',
        borderWidth: 1.5,
        borderDash: [4, 3],
        pointRadius: 0,
        fill: false,
        tension: 0.15,
      },
      {
        label: 'average',
        data: avg,
        borderColor: 'rgba(80, 220, 120, 1.0)',
        borderWidth: 2,
        pointRadius: 0,
        fill: false,
        tension: 0.15,
      },
    ];

    // Abnormal-return overlay (vs benchmark). Show only when the backend
    // produced finite values — otherwise we'd plot a misleading flat line.
    const abn = data.abnormal_return_curve || [];
    const abnFinite = abn.some(v => v != null && isFinite(v));
    if (abnFinite) {
      datasets.push({
        label: `abnormal (vs ${data.benchmark || 'SPY'})`,
        data: abn,
        borderColor: 'rgba(255, 120, 120, 0.9)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: false,
        tension: 0.15,
      });
    }

    const ctx = canvas.getContext('2d');
    const chart = new Chart(ctx, {
      type: 'line',
      data: { labels: xs, datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            title: { display: true, text: 'Trading days from event (T)' },
            grid: { color: 'rgba(255,255,255,0.05)' },
            ticks: { color: 'rgba(255,255,255,0.6)' },
          },
          y: {
            title: { display: true, text: '% return (rel. to T-' + w + ')' },
            grid: { color: 'rgba(255,255,255,0.05)' },
            ticks: {
              color: 'rgba(255,255,255,0.6)',
              callback: v => Number(v).toFixed(1) + '%',
            },
          },
        },
        plugins: {
          legend: { labels: { color: 'rgba(255,255,255,0.8)', boxWidth: 10 } },
          tooltip: {
            callbacks: {
              title: items => 'T' + (items[0].label >= 0 ? '+' : '') + items[0].label,
              label: ctx => `${ctx.dataset.label}: ${_fmtPct(ctx.parsed.y, 2)}`,
            },
          },
        },
      },
    });
    _CHART_REG.set(containerEl, chart);
  }

  function _renderSidePanel(symbol, data) {
    const side = document.getElementById(`es-side-${symbol}`);
    if (!side) return;
    const s = data.summary || {};

    // Build a small table of top/bottom 3 individual instances.
    // When fewer than 6 events are returned, slice(0,3) and slice(-3) overlap
    // and the user sees the same row twice. Carve out the bottom-3 from the
    // tail only after the top-3 have been claimed.
    const evs = (data.events || []).slice();
    evs.sort((a, b) => (b.cumulative_return_at_end || 0) - (a.cumulative_return_at_end || 0));
    const top3 = evs.slice(0, 3);
    const bot3 = evs.length > 3 ? evs.slice(Math.max(3, evs.length - 3)).reverse() : [];

    const row = (e) => `
      <tr>
        <td>${_esc(e.date)}</td>
        <td class="${_pnlClass(e.cumulative_return_at_end)}" style="text-align:right">
          ${_fmtPct(e.cumulative_return_at_end, 2)}
        </td>
      </tr>`;

    side.innerHTML = `
      <div style="margin-bottom:8px">
        <div style="font-size:10px;color:var(--text-dim)">N EVENTS</div>
        <div style="font-size:18px">${data.n_events}</div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
        <div>
          <div style="font-size:10px;color:var(--text-dim)">AVG T0 RETURN</div>
          <div class="${_pnlClass(s.avg_T0_return_pct)}" style="font-size:14px">
            ${_fmtPct(s.avg_T0_return_pct, 2)}
          </div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text-dim)">AVG POST-5d</div>
          <div class="${_pnlClass(s.avg_post_event_5d_pct)}" style="font-size:14px">
            ${_fmtPct(s.avg_post_event_5d_pct, 2)}
          </div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text-dim)">HIT RATE POST</div>
          <div style="font-size:14px">${s.hit_rate_positive_post == null ? '—' : (s.hit_rate_positive_post * 100).toFixed(0) + '%'}</div>
        </div>
        <div>
          <div style="font-size:10px;color:var(--text-dim)">BENCHMARK</div>
          <div style="font-size:14px">${_esc(data.benchmark || 'SPY')}</div>
        </div>
      </div>

      <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">BEST INSTANCES</div>
      <table class="data-table" style="width:100%;font-size:11px;margin-bottom:6px">
        <tbody>${top3.map(row).join('')}</tbody>
      </table>

      <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">WORST INSTANCES</div>
      <table class="data-table" style="width:100%;font-size:11px">
        <tbody>${bot3.map(row).join('')}</tbody>
      </table>
    `;
  }

  function _renderError(symbol, msg) {
    const status = document.getElementById(`es-status-${symbol}`);
    if (status) status.innerHTML = `<span style="color:var(--bad,#f88)">${_esc(msg)}</span>`;
    const side = document.getElementById(`es-side-${symbol}`);
    if (side) side.innerHTML = `<div style="color:var(--text-dim);font-size:11px">${_esc(msg)}</div>`;
  }

  async function _runQuery(containerEl, symbol, eventType, windowDays) {
    const status = document.getElementById(`es-status-${symbol}`);
    if (status) status.innerHTML = `Loading event study for ${_esc(symbol)} / ${_esc(eventType)}…`;
    let data;
    try {
      data = await _api(symbol, eventType, windowDays, 10, 'SPY');
    } catch (e) {
      _renderError(symbol, 'Network error: ' + e);
      return;
    }
    if (!data || data.error) {
      _renderError(symbol, (data && data.error) || 'No data');
      // Still clear the chart so a previous render doesn't linger.
      _destroyChart(containerEl);
      return;
    }
    if (status) {
      status.innerHTML = `As of ${_esc(data.as_of)} · ${data.n_events} events · period ≤ ${data.period_years || 10}y · benchmark ${_esc(data.benchmark || 'SPY')}`;
    }
    _drawChart(containerEl, symbol, data);
    _renderSidePanel(symbol, data);
  }

  function renderEventStudy(containerEl, symbol, eventType) {
    if (!containerEl) return;
    const sym = (symbol || '').toString().toUpperCase();
    if (!sym) {
      containerEl.innerHTML = '';
      return;
    }
    const et = eventType || 'earnings';
    const windowDays = 10;

    _buildSkeleton(containerEl, sym, et, windowDays);

    const evSel  = document.getElementById(`es-event-${sym}`);
    const winSl  = document.getElementById(`es-window-${sym}`);
    const winLbl = document.getElementById(`es-window-label-${sym}`);
    const btn    = document.getElementById(`es-refresh-${sym}`);

    const runNow = () => {
      const curEt = evSel ? evSel.value : 'earnings';
      const curW = winSl ? Number(winSl.value) || 10 : 10;
      _runQuery(containerEl, sym, curEt, curW);
    };

    if (winSl && winLbl) {
      winSl.addEventListener('input', () => { winLbl.textContent = winSl.value; });
    }
    if (evSel) evSel.addEventListener('change', runNow);
    if (btn) btn.addEventListener('click', runNow);

    runNow();
  }

  window.renderEventStudy = renderEventStudy;
})();
