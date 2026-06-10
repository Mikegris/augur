/* synth_catalyst.js — Catalyst Timeline panel for AUGUR.
 *
 * Renders a unified forward-looking timeline of upcoming earnings, FOMC,
 * monthly OPEX, and ex-dividend events for the user's portfolio (or a
 * custom symbol set). Each event row carries the market-implied move from
 * the options RND, the historical event-study average for the same name,
 * and the current signal stack (smart_money / ml_prob_up / narrative
 * phase / IV skew), plus a synthesized edge score that highlights cases
 * where the options market and historical behavior diverge.
 *
 * Backend: GET /api/synth/catalyst?days_ahead=N      (uses portfolio)
 *          POST /api/synth/catalyst {symbols, days_ahead}  (custom list)
 *
 * Exposes a single global:
 *   window.renderCatalystTimeline(containerEl, options)
 *     options = { symbols: string[] | null,   // null ⇒ portfolio
 *                 days_ahead: number          // default 60 }
 *
 * Owns the container's innerHTML; safe to call repeatedly.
 */

(function () {
  'use strict';

  // Event-type metadata: label, color band, short tag.
  const TYPE_META = {
    earnings:    { label: 'Earnings',     color: '#fbbf24', tag: 'EPS'  },
    fed_fomc:    { label: 'FOMC',         color: '#60a5fa', tag: 'FOMC' },
    opex:        { label: 'Monthly OPEX', color: '#a78bfa', tag: 'OPEX' },
    ex_dividend: { label: 'Ex-Dividend',  color: '#34d399', tag: 'DIV'  },
  };

  // Track chart instance per container so re-renders don't error.
  const _CHART_REG = new WeakMap();

  // ── Tiny helpers ───────────────────────────────────────────────────────

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

  function _fmtMove(v) {
    if (v == null || !isFinite(v)) return '—';
    return Number(v).toFixed(2) + '%';
  }

  function _fmtEdge(v) {
    if (v == null || !isFinite(v)) return '—';
    const sign = v > 0 ? '+' : '';
    return sign + Number(v).toFixed(2);
  }

  function _edgeColor(v) {
    if (v == null || !isFinite(v)) return 'rgba(160,160,160,0.7)';
    // Green → straddle cheap (positive edge), Red → expensive.
    if (v >= 0.5)  return '#22c55e';
    if (v >= 0.15) return '#86efac';
    if (v >= -0.15) return '#facc15';
    if (v >= -0.5) return '#fb923c';
    return '#ef4444';
  }

  function _pnlClass(v) {
    if (v == null || !isFinite(v)) return '';
    return v >= 0 ? 'pnl-pos' : 'pnl-neg';
  }

  function _typeMeta(t) {
    return TYPE_META[t] || { label: t || '—', color: '#94a3b8', tag: t || '?' };
  }

  // ── Fetch ──────────────────────────────────────────────────────────────

  async function _fetchTimeline(symbols, daysAhead) {
    const da = Math.max(1, Math.min(180, Number(daysAhead) || 60));
    try {
      if (!symbols || symbols.length === 0) {
        const r = await fetch(`/api/synth/catalyst?days_ahead=${da}`);
        return await r.json();
      } else {
        const r = await fetch('/api/synth/catalyst', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ symbols: symbols, days_ahead: da }),
        });
        return await r.json();
      }
    } catch (e) {
      return { error: 'Network error: ' + e };
    }
  }

  // ── Skeleton ───────────────────────────────────────────────────────────

  function _buildSkeleton(containerEl, opts) {
    const da = opts.days_ahead || 60;
    const symsStr = (opts.symbols && opts.symbols.length)
      ? opts.symbols.join(',') : '';

    containerEl.innerHTML = `
      <div class="panel" id="sc-panel">
        <div class="panel-header">
          <span class="panel-title">CATALYST TIMELINE</span>
          <div style="display:flex;gap:8px;align-items:center;font-size:11px;flex-wrap:wrap">
            <label>Symbols:
              <input type="text" id="sc-symbols" placeholder="(portfolio)"
                value="${_esc(symsStr)}"
                style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 6px;width:240px;font-family:inherit">
            </label>
            <label>Horizon (days):
              <input type="number" id="sc-days" min="7" max="180" step="1"
                value="${da}" style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px;width:60px">
            </label>
            <button class="btn btn-ghost btn-sm" id="sc-refresh">RUN</button>
          </div>
        </div>
        <div class="panel-body">
          <!-- Filter strip -->
          <div id="sc-filters" style="display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-bottom:10px;font-size:11px;color:var(--text-dim)">
            <span>Event types:</span>
            <label><input type="checkbox" class="sc-ftype" value="earnings" checked> Earnings</label>
            <label><input type="checkbox" class="sc-ftype" value="fed_fomc" checked> FOMC</label>
            <label><input type="checkbox" class="sc-ftype" value="opex" checked> OPEX</label>
            <label><input type="checkbox" class="sc-ftype" value="ex_dividend" checked> Ex-Div</label>
            <span style="margin-left:12px">Symbols:</span>
            <span id="sc-sym-filter" style="display:inline-flex;gap:6px;flex-wrap:wrap"></span>
          </div>

          <!-- Horizontal timeline -->
          <div style="position:relative;height:160px;background:rgba(255,255,255,0.02);
                      border:1px solid var(--border);border-radius:4px;margin-bottom:10px">
            <canvas id="sc-chart"></canvas>
          </div>

          <!-- Status + legend -->
          <div id="sc-status" style="font-size:10px;color:var(--text-dim);margin-bottom:6px"></div>

          <!-- Sortable table -->
          <div style="overflow-x:auto">
            <table class="data-table" id="sc-table" style="width:100%;font-size:11px">
              <thead>
                <tr>
                  <th data-sort="date" style="cursor:pointer">DATE ▾</th>
                  <th data-sort="days_until" style="cursor:pointer">DAYS</th>
                  <th data-sort="symbol" style="cursor:pointer">SYMBOL</th>
                  <th data-sort="type" style="cursor:pointer">TYPE</th>
                  <th data-sort="implied_move_pct" style="cursor:pointer;text-align:right">IMPLIED</th>
                  <th data-sort="historical_avg_move_pct" style="cursor:pointer;text-align:right">HIST AVG</th>
                  <th style="text-align:right">N</th>
                  <th style="text-align:right">HIT %</th>
                  <th data-sort="smart_money" style="cursor:pointer;text-align:right">SM</th>
                  <th data-sort="ml_prob_up" style="cursor:pointer;text-align:right">ML</th>
                  <th>NARRATIVE</th>
                  <th style="text-align:right">SKEW</th>
                  <th data-sort="edge_score" style="cursor:pointer;text-align:right">EDGE</th>
                  <th>NOTE</th>
                </tr>
              </thead>
              <tbody id="sc-tbody"></tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  }

  // ── Timeline chart ─────────────────────────────────────────────────────

  function _destroyChart(containerEl) {
    const existing = _CHART_REG.get(containerEl);
    if (existing) {
      try { existing.destroy(); } catch (e) { /* ignore */ }
      _CHART_REG.delete(containerEl);
    }
  }

  function _drawTimeline(containerEl, events) {
    const canvas = document.getElementById('sc-chart');
    if (!canvas) return;
    if (typeof Chart === 'undefined') {
      canvas.parentElement.innerHTML =
        '<div style="padding:14px;font-size:11px;color:var(--text-dim)">' +
        'Chart.js not loaded — timeline chart skipped (table still works).</div>';
      return;
    }
    _destroyChart(containerEl);

    // Build a scatter dataset per event type so the legend toggles them.
    const byType = {};
    events.forEach(e => {
      if (!byType[e.type]) byType[e.type] = [];
      byType[e.type].push(e);
    });

    const knownTypes = Object.keys(TYPE_META);
    const datasets = Object.keys(byType).map(t => {
      const meta = _typeMeta(t);
      // Stable y-stagger per type so re-renders don't jitter. Also fix the
      // earlier `indexOf(t) || 0` bug — for unknown types indexOf returns
      // -1, which is truthy, so the marker landed at y=-0.9 (below the
      // visible y-axis min of -0.5).
      let typeIdx = knownTypes.indexOf(t);
      if (typeIdx < 0) typeIdx = knownTypes.length; // unknown types stack above the known ones
      const yBase = typeIdx * 0.9;
      return {
        label: meta.label,
        data: byType[t].map((e, i) => ({
          x: e.date,
          // Spread events within a type using a deterministic fractional
          // offset based on their index so two earnings on the same day
          // don't sit on top of each other but stay put across re-renders.
          y: yBase + ((i * 0.13) % 0.6),
          symbol: e.symbol,
          edge: e.edge_score,
          implied: e.implied_move_pct,
          hist: e.historical_avg_move_pct,
          type: t,
          note: e.note,
        })),
        backgroundColor: byType[t].map(e => _edgeColor(e.edge_score)),
        borderColor: meta.color,
        borderWidth: 1.5,
        pointRadius: 7,
        pointHoverRadius: 9,
        showLine: false,
      };
    });

    const ctx = canvas.getContext('2d');
    const chart = new Chart(ctx, {
      type: 'scatter',
      data: { datasets: datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: {
          x: {
            type: 'time',
            time: { unit: 'day', tooltipFormat: 'yyyy-MM-dd', displayFormats: { day: 'MMM dd' } },
            grid: { color: 'rgba(255,255,255,0.04)' },
            ticks: { color: 'rgba(255,255,255,0.6)', maxRotation: 0 },
          },
          y: { display: false, min: -0.5, max: 4.5 },
        },
        plugins: {
          legend: { labels: { color: 'rgba(255,255,255,0.85)', boxWidth: 10 } },
          tooltip: {
            callbacks: {
              title: items => { const r0 = (items[0] && items[0].raw) || {}; return (r0.symbol || '?') + ' — ' + _typeMeta(r0.type).label; },
              label: it => {
                const r = it.raw;
                const lines = [
                  'Date: ' + r.x,
                  'Implied: ' + (r.implied == null ? '—' : r.implied + '%'),
                  'Historical: ' + (r.hist == null ? '—' : r.hist + '%'),
                  'Edge: ' + (r.edge == null ? '—' : (r.edge > 0 ? '+' : '') + r.edge),
                ];
                return lines;
              },
            },
          },
        },
      },
    });
    _CHART_REG.set(containerEl, chart);
  }

  // ── Table render ───────────────────────────────────────────────────────

  let _STATE = {
    events: [],
    sort: { key: 'date', dir: 'asc' },
    typeFilter: new Set(['earnings', 'fed_fomc', 'opex', 'ex_dividend']),
    symbolFilter: new Set(),
  };

  function _getSortVal(e, k) {
    if (k === 'smart_money') return (e.current_signals && e.current_signals.smart_money) || null;
    if (k === 'ml_prob_up')  return (e.current_signals && e.current_signals.ml_prob_up) || null;
    return e[k];
  }

  function _cmp(a, b, dir) {
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (a < b) return dir === 'asc' ? -1 : 1;
    if (a > b) return dir === 'asc' ? 1 : -1;
    return 0;
  }

  function _renderRow(e) {
    const meta = _typeMeta(e.type);
    const sig = e.current_signals || {};
    const edge = e.edge_score;
    const edgeColor = _edgeColor(edge);
    return `
      <tr>
        <td>${_esc(e.date)}</td>
        <td style="text-align:right">${e.days_until == null ? '—' : e.days_until}</td>
        <td><strong>${_esc(e.symbol)}</strong></td>
        <td>
          <span style="display:inline-block;padding:1px 6px;border-radius:3px;
                       background:${meta.color}22;color:${meta.color};
                       font-size:10px;border:1px solid ${meta.color}55">
            ${_esc(meta.tag)}
          </span>
        </td>
        <td style="text-align:right">${_fmtMove(e.implied_move_pct)}</td>
        <td style="text-align:right">${_fmtMove(e.historical_avg_move_pct)}</td>
        <td style="text-align:right;color:var(--text-dim)">${e.historical_n_events == null ? '—' : e.historical_n_events}</td>
        <td style="text-align:right">${e.historical_hit_rate_positive_post == null ? '—' : (e.historical_hit_rate_positive_post * 100).toFixed(0) + '%'}</td>
        <td style="text-align:right">${sig.smart_money == null ? '—' : sig.smart_money}</td>
        <td style="text-align:right">${sig.ml_prob_up == null ? '—' : (sig.ml_prob_up * 100).toFixed(0) + '%'}</td>
        <td style="font-size:10px">${_esc(sig.narrative_phase || '—')}</td>
        <td style="text-align:right;font-size:10px">${_esc(sig.iv_skew || '—')}</td>
        <td style="text-align:right;color:${edgeColor};font-weight:bold">${_fmtEdge(edge)}</td>
        <td style="font-size:10px;color:var(--text-dim);max-width:340px">${_esc(e.note || '')}</td>
      </tr>
    `;
  }

  function _applyFiltersAndSort() {
    let rows = _STATE.events.slice();
    rows = rows.filter(e => _STATE.typeFilter.has(e.type));
    if (_STATE.symbolFilter.size > 0) {
      rows = rows.filter(e => _STATE.symbolFilter.has(e.symbol));
    }
    const { key, dir } = _STATE.sort;
    rows.sort((a, b) => _cmp(_getSortVal(a, key), _getSortVal(b, key), dir));
    return rows;
  }

  function _renderTable() {
    const tbody = document.getElementById('sc-tbody');
    if (!tbody) return;
    const rows = _applyFiltersAndSort();
    if (rows.length === 0) {
      tbody.innerHTML = '<tr><td colspan="14" style="text-align:center;padding:18px;color:var(--text-dim)">No events match current filters.</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(_renderRow).join('');
  }

  function _renderSymbolFilter(events) {
    const host = document.getElementById('sc-sym-filter');
    if (!host) return;
    const syms = Array.from(new Set(events.map(e => e.symbol))).sort();
    if (syms.length === 0) {
      host.innerHTML = '<span style="color:var(--text-dim)">none</span>';
      return;
    }
    host.innerHTML = syms.map(s =>
      `<label><input type="checkbox" class="sc-fsym" value="${_esc(s)}" checked> ${_esc(s)}</label>`
    ).join('');
    // Default: all selected (empty filter set means "all").
    _STATE.symbolFilter = new Set();
    host.querySelectorAll('.sc-fsym').forEach(cb => {
      cb.addEventListener('change', () => {
        const all = Array.from(host.querySelectorAll('.sc-fsym'));
        const checked = all.filter(c => c.checked).map(c => c.value);
        // If every box checked, treat as "all" (empty set).
        if (checked.length === all.length) {
          _STATE.symbolFilter = new Set();
        } else {
          _STATE.symbolFilter = new Set(checked);
        }
        _renderTable();
      });
    });
  }

  function _bindSortHandlers() {
    document.querySelectorAll('#sc-table th[data-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const key = th.dataset.sort;
        if (_STATE.sort.key === key) {
          _STATE.sort.dir = _STATE.sort.dir === 'asc' ? 'desc' : 'asc';
        } else {
          _STATE.sort.key = key;
          _STATE.sort.dir = key === 'edge_score' ? 'desc' : 'asc';
        }
        // Repaint headers with arrow.
        document.querySelectorAll('#sc-table th[data-sort]').forEach(t => {
          let label = t.textContent.replace(/[▴▾]/g, '').trim();
          if (t.dataset.sort === _STATE.sort.key) {
            label += _STATE.sort.dir === 'asc' ? ' ▾' : ' ▴';
          }
          t.textContent = label;
        });
        _renderTable();
      });
    });
  }

  function _bindTypeFilter() {
    document.querySelectorAll('.sc-ftype').forEach(cb => {
      cb.addEventListener('change', () => {
        const v = cb.value;
        if (cb.checked) _STATE.typeFilter.add(v);
        else _STATE.typeFilter.delete(v);
        _renderTable();
      });
    });
  }

  // ── Main render ────────────────────────────────────────────────────────

  async function _runQuery(containerEl, opts) {
    const status = document.getElementById('sc-status');
    if (status) status.innerHTML = 'Loading catalyst timeline…';
    const data = await _fetchTimeline(opts.symbols, opts.days_ahead);
    if (!data || data.error) {
      if (status) status.innerHTML =
        `<span style="color:var(--bad,#f88)">${_esc((data && data.error) || 'Unknown error')}</span>`;
      _STATE.events = [];
      _renderTable();
      return;
    }
    _STATE.events = Array.isArray(data.events) ? data.events : [];
    const dt = new Date();
    const tsStr = dt.toISOString().split('T')[0];
    const symList = (data.symbols || []).join(', ');
    if (status) {
      const errCount = (data.errors || []).length;
      const errSuffix = errCount > 0 ? ` · ${errCount} warnings` : '';
      status.innerHTML =
        `As of ${_esc(data.as_of || tsStr)} · horizon ${data.days_ahead || opts.days_ahead}d · ` +
        `${_STATE.events.length} events · ${_esc(symList) || '(portfolio)'}${errSuffix}`;
    }
    _renderSymbolFilter(_STATE.events);
    _renderTable();
    _drawTimeline(containerEl, _STATE.events);
  }

  function _readOptsFromUI(defaults) {
    const symRaw = (document.getElementById('sc-symbols')?.value || '').trim();
    const symbols = symRaw
      ? symRaw.split(/[,\s]+/).map(s => s.trim().toUpperCase()).filter(Boolean)
      : null;
    const days = Number(document.getElementById('sc-days')?.value) || (defaults.days_ahead || 60);
    return { symbols: symbols, days_ahead: days };
  }

  // ── Public entry ───────────────────────────────────────────────────────

  function renderCatalystTimeline(containerEl, options) {
    if (!containerEl) return;
    const opts = Object.assign({ symbols: null, days_ahead: 60 }, options || {});

    // Reset module-level UI state so a re-render doesn't carry over the
    // previous panel's sort direction, filter chips, or stale events.
    _STATE.events = [];
    _STATE.sort = { key: 'date', dir: 'asc' };
    _STATE.typeFilter = new Set(['earnings', 'fed_fomc', 'opex', 'ex_dividend']);
    _STATE.symbolFilter = new Set();

    _buildSkeleton(containerEl, opts);
    _bindSortHandlers();
    _bindTypeFilter();

    const refresh = document.getElementById('sc-refresh');
    if (refresh) {
      refresh.addEventListener('click', () => {
        _runQuery(containerEl, _readOptsFromUI(opts));
      });
    }
    // Enter-key submit on inputs.
    ['sc-symbols', 'sc-days'].forEach(id => {
      const el = document.getElementById(id);
      if (el) {
        el.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            _runQuery(containerEl, _readOptsFromUI(opts));
          }
        });
      }
    });

    _runQuery(containerEl, opts);
  }

  window.renderCatalystTimeline = renderCatalystTimeline;
})();
