/*
 * synth_cluster.js — Multi-Source Signal Cluster Detection UI.
 *
 * Usage:
 *     renderClusterScan(document.getElementById('cluster-panel'), {
 *         direction: 'bullish',
 *         min_sources: 4,
 *         universe: 'sp500_top100',
 *     });
 *
 * Surface:
 *  - Top control bar: direction toggle, min-sources slider, scan button
 *  - Card grid below: one card per cluster (symbol + which sources fired)
 *  - Click a card -> expanded detail view inline (per-source notes + values)
 *
 * Exposed on window as window.renderClusterScan so app.js (which the
 * integration step adds a hook into) can call it directly.
 *
 * Styling notes: uses the terminal.css custom-property tokens
 * (--green, --red, --amber, --text-primary, --bg-panel, --border, ...).
 * One scoped <style> block is injected on first call.
 */

(function () {
  'use strict';

  const STYLE_ID = 'sc-style';
  const STYLE_CSS = `
    .sc-wrap { font-family: var(--font-mono); color: var(--text-primary); }
    .sc-controls {
      display: flex; flex-wrap: wrap; gap: 12px; align-items: center;
      padding: 10px 12px;
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      margin-bottom: 10px;
    }
    .sc-controls > * { font-family: var(--font-mono); }
    .sc-label {
      font-size: var(--font-size-xs);
      letter-spacing: 1px;
      text-transform: uppercase;
      color: var(--text-secondary);
    }
    .sc-dir-toggle { display: flex; gap: 0; border: 1px solid var(--border); }
    .sc-dir-toggle button {
      padding: 6px 12px;
      background: transparent;
      border: none;
      color: var(--text-secondary);
      font-family: var(--font-mono);
      font-size: var(--font-size-sm);
      letter-spacing: 1px;
      text-transform: uppercase;
      cursor: pointer;
    }
    .sc-dir-toggle button.active.bull {
      background: var(--green);
      color: var(--bg-base);
    }
    .sc-dir-toggle button.active.bear {
      background: var(--red);
      color: var(--bg-base);
    }
    .sc-slider-wrap { display: flex; align-items: center; gap: 8px; }
    .sc-slider-wrap input[type=range] { width: 140px; }
    .sc-slider-val {
      min-width: 16px;
      font-weight: 600;
      color: var(--text-primary);
      font-size: var(--font-size-sm);
    }
    .sc-scan-btn {
      padding: 7px 16px;
      background: var(--bg-panel);
      border: 1px solid var(--border-bright);
      color: var(--text-primary);
      font-family: var(--font-mono);
      font-size: var(--font-size-sm);
      letter-spacing: 1px;
      text-transform: uppercase;
      cursor: pointer;
    }
    .sc-scan-btn:disabled { opacity: 0.4; cursor: wait; }
    .sc-scan-btn:hover:not(:disabled) {
      background: var(--bg-elevated);
      border-color: var(--text-primary);
    }
    .sc-status {
      font-size: var(--font-size-xs);
      color: var(--text-secondary);
      margin-left: auto;
    }

    .sc-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
      gap: 8px;
    }
    .sc-card {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      border-left-width: 3px;
      padding: 10px 12px;
      cursor: pointer;
      transition: background 120ms ease, border-color 120ms ease;
    }
    .sc-card:hover { background: var(--bg-elevated); border-color: var(--border-bright); }
    .sc-card.bull { border-left-color: var(--green); }
    .sc-card.bear { border-left-color: var(--red); }
    .sc-card.expanded {
      grid-column: 1 / -1;
      background: var(--bg-elevated);
    }

    .sc-card-head {
      display: flex; justify-content: space-between; align-items: baseline;
      gap: 8px; margin-bottom: 6px;
    }
    .sc-symbol {
      font-size: var(--font-size-lg);
      font-weight: 600;
      letter-spacing: 1px;
    }
    .sc-score {
      font-size: var(--font-size-md);
      font-weight: 600;
    }
    .sc-score.bull { color: var(--green); }
    .sc-score.bear { color: var(--red); }
    .sc-n-fired {
      font-size: var(--font-size-xs);
      color: var(--text-secondary);
      letter-spacing: 1px;
    }
    .sc-source-row {
      display: flex; align-items: center; gap: 6px;
      font-size: var(--font-size-xs);
      letter-spacing: 0.5px;
      padding: 2px 0;
    }
    .sc-dot {
      width: 8px; height: 8px; border-radius: 50%;
      flex-shrink: 0;
    }
    .sc-dot.fired.bull  { background: var(--green); box-shadow: 0 0 4px var(--green); }
    .sc-dot.fired.bear  { background: var(--red);   box-shadow: 0 0 4px var(--red); }
    .sc-dot.unfired { background: var(--border-bright); }
    .sc-src-name {
      flex: 0 0 auto;
      color: var(--text-primary);
      min-width: 110px;
    }
    .sc-src-name.dim { color: var(--text-dim); }
    .sc-src-value {
      color: var(--text-secondary);
      font-size: 11px;
      flex: 1 1 auto;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }
    .sc-src-note {
      color: var(--text-dim);
      font-size: 10px;
      letter-spacing: 0.5px;
      margin-left: 14px;
      margin-bottom: 6px;
    }
    .sc-empty {
      padding: 20px;
      text-align: center;
      color: var(--text-secondary);
      font-size: var(--font-size-sm);
      border: 1px dashed var(--border);
      letter-spacing: 1px;
    }
    .sc-loading {
      padding: 14px;
      color: var(--text-secondary);
      font-size: var(--font-size-sm);
      letter-spacing: 1px;
    }
    .sc-err {
      padding: 14px;
      color: var(--red);
      font-size: var(--font-size-sm);
      letter-spacing: 0.5px;
    }
  `;

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  function escHtml(str) {
    if (str == null) return '';
    return String(str)
      .replaceAll('&', '&amp;')
      .replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;')
      .replaceAll('"', '&quot;')
      .replaceAll("'", '&#39;');
  }

  function fetchScan(direction, minSources, universe) {
    const params = new URLSearchParams({
      direction: direction,
      min_sources: String(minSources),
    });
    if (universe && universe !== 'sp500_top100') {
      params.set('universe', universe);
    }
    const url = `/api/synth/cluster-scan?${params.toString()}`;
    return fetch(url, { method: 'GET' }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
  }

  function renderCard(cluster, direction) {
    const dirClass = direction === 'bearish' ? 'bear' : 'bull';
    const sourceRows = (cluster.sources || []).map((s) => {
      const fired = !!s.fired;
      const dotCls = fired ? `sc-dot fired ${dirClass}` : 'sc-dot unfired';
      const nameCls = fired ? 'sc-src-name' : 'sc-src-name dim';
      return `
        <div class="sc-source-row">
          <span class="${dotCls}"></span>
          <span class="${nameCls}">${escHtml(s.name)}</span>
          <span class="sc-src-value">${escHtml(s.value || '')}</span>
        </div>`;
    }).join('');

    const composite = (typeof cluster.composite_score === 'number')
      ? cluster.composite_score.toFixed(2)
      : '—';

    return `
      <div class="sc-card ${dirClass}" data-symbol="${escHtml(cluster.symbol)}">
        <div class="sc-card-head">
          <span class="sc-symbol">${escHtml(cluster.symbol)}</span>
          <span class="sc-score ${dirClass}">${composite}</span>
        </div>
        <div class="sc-n-fired">${cluster.n_sources_firing}/10 sources firing</div>
        <div class="sc-sources">${sourceRows}</div>
      </div>`;
  }

  function renderExpandedDetail(cluster, direction) {
    const dirClass = direction === 'bearish' ? 'bear' : 'bull';
    const composite = (typeof cluster.composite_score === 'number')
      ? cluster.composite_score.toFixed(2)
      : '—';

    const sourceRows = (cluster.sources || []).map((s) => {
      const fired = !!s.fired;
      const dotCls = fired ? `sc-dot fired ${dirClass}` : 'sc-dot unfired';
      const nameCls = fired ? 'sc-src-name' : 'sc-src-name dim';
      return `
        <div class="sc-source-row">
          <span class="${dotCls}"></span>
          <span class="${nameCls}">${escHtml(s.name)}</span>
          <span class="sc-src-value">${escHtml(s.value || '')}</span>
        </div>
        <div class="sc-src-note">${escHtml(s.note || '')}</div>`;
    }).join('');

    return `
      <div class="sc-card ${dirClass} expanded" data-symbol="${escHtml(cluster.symbol)}" data-expanded="1">
        <div class="sc-card-head">
          <span class="sc-symbol">${escHtml(cluster.symbol)} — detail</span>
          <span class="sc-score ${dirClass}">${composite}</span>
        </div>
        <div class="sc-n-fired">${cluster.n_sources_firing}/10 sources firing — click to collapse</div>
        <div class="sc-sources">${sourceRows}</div>
      </div>`;
  }

  /**
   * renderClusterScan(containerEl, options)
   * options: {direction='bullish', min_sources=4, universe='sp500_top100'}
   */
  async function renderClusterScan(containerEl, options) {
    ensureStyle();
    const opts = options || {};
    let direction   = opts.direction || 'bullish';
    let minSources  = parseInt(opts.min_sources, 10);
    if (!Number.isFinite(minSources)) minSources = 4;
    const universe  = opts.universe || 'sp500_top100';

    containerEl.classList.add('sc-wrap');
    containerEl.innerHTML = `
      <div class="sc-controls">
        <span class="sc-label">Direction</span>
        <div class="sc-dir-toggle">
          <button class="bull" data-dir="bullish">BULL</button>
          <button class="bear" data-dir="bearish">BEAR</button>
        </div>
        <span class="sc-label" style="margin-left:8px">Min sources</span>
        <div class="sc-slider-wrap">
          <input type="range" min="2" max="7" step="1" value="${minSources}" />
          <span class="sc-slider-val">${minSources}</span>
        </div>
        <button class="sc-scan-btn">Scan</button>
        <span class="sc-status"></span>
      </div>
      <div class="sc-grid sc-results">
        <div class="sc-loading">Loading initial scan…</div>
      </div>`;

    const bullBtn   = containerEl.querySelector('button[data-dir="bullish"]');
    const bearBtn   = containerEl.querySelector('button[data-dir="bearish"]');
    const slider    = containerEl.querySelector('input[type="range"]');
    const sliderVal = containerEl.querySelector('.sc-slider-val');
    const scanBtn   = containerEl.querySelector('.sc-scan-btn');
    const statusEl  = containerEl.querySelector('.sc-status');
    const resultsEl = containerEl.querySelector('.sc-results');

    function syncDirButtons() {
      bullBtn.classList.toggle('active', direction === 'bullish');
      bearBtn.classList.toggle('active', direction === 'bearish');
    }
    syncDirButtons();

    slider.addEventListener('input', () => {
      minSources = parseInt(slider.value, 10) || 4;
      sliderVal.textContent = String(minSources);
    });

    bullBtn.addEventListener('click', () => { direction = 'bullish'; syncDirButtons(); });
    bearBtn.addEventListener('click', () => { direction = 'bearish'; syncDirButtons(); });

    async function runScan() {
      scanBtn.disabled = true;
      statusEl.textContent = 'Scanning… can take 30-60s on cold cache';
      resultsEl.innerHTML = '<div class="sc-loading">Running cluster scan…</div>';
      try {
        const data = await fetchScan(direction, minSources, universe);
        renderResults(data);
      } catch (err) {
        resultsEl.innerHTML = `<div class="sc-err">Scan failed: ${escHtml(err.message || err)}</div>`;
        statusEl.textContent = '';
      } finally {
        scanBtn.disabled = false;
      }
    }
    scanBtn.addEventListener('click', runScan);

    function renderResults(data) {
      if (!data || data.error) {
        resultsEl.innerHTML = `<div class="sc-err">${escHtml((data && data.error) || 'No data')}</div>`;
        statusEl.textContent = '';
        return;
      }
      const clusters = data.clusters || [];
      statusEl.textContent =
        `${data.n_symbols_scanned} symbols, ${clusters.length} clusters @ ≥${data.min_sources} sources` +
        (data.elapsed_seconds ? ` — ${data.elapsed_seconds}s` : '');

      if (clusters.length === 0) {
        resultsEl.innerHTML = `
          <div class="sc-empty">
            No symbols had ${data.min_sources}+ ${data.direction} sources firing.<br/>
            Try lowering the minimum or flipping the direction.
          </div>`;
        return;
      }
      const cards = clusters.map((c) => renderCard(c, data.direction)).join('');
      resultsEl.innerHTML = cards;
      resultsEl._lastClusters = clusters;
      resultsEl._lastDirection = data.direction;
    }

    // Click handler: expand/collapse card
    resultsEl.addEventListener('click', (ev) => {
      const card = ev.target.closest('.sc-card');
      if (!card) return;
      const sym = card.getAttribute('data-symbol');
      const clusters = resultsEl._lastClusters || [];
      const cluster = clusters.find((c) => c.symbol === sym);
      if (!cluster) return;
      const dir = resultsEl._lastDirection || direction;
      const isExpanded = card.getAttribute('data-expanded') === '1';

      // Collapse any currently expanded card (only one at a time)
      const expanded = resultsEl.querySelector('.sc-card.expanded');
      if (expanded) {
        const expSym = expanded.getAttribute('data-symbol');
        const expCluster = clusters.find((c) => c.symbol === expSym);
        if (expCluster) {
          expanded.outerHTML = renderCard(expCluster, dir);
        }
      }

      // If clicked card was the expanded one, we're done (collapsed it)
      if (isExpanded) return;

      // Otherwise replace clicked card with expanded view
      const fresh = resultsEl.querySelector(`.sc-card[data-symbol="${CSS.escape(sym)}"]`);
      if (fresh) fresh.outerHTML = renderExpandedDetail(cluster, dir);
    });

    // Initial fetch
    await runScan();
  }

  // Expose globally so app.js can call it
  window.renderClusterScan = renderClusterScan;
})();
