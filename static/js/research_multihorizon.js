/*
 * research_multihorizon.js — render the 5d / 20d / 60d / 120d forecast panel.
 *
 * Usage:
 *     renderMultiHorizon(document.getElementById('mh-panel'), 'AAPL');
 *
 * Surface:
 *  - 4-cell grid (5D / 20D / 60D / 120D). Each cell shows:
 *      • prob_up as a circular gauge (SVG)
 *      • expected return %  (signed, colored)
 *      • confidence dot
 *      • method label
 *  - Below the grid: "consensus" line, plus an optional divergence callout
 *    when short and long disagree.
 *
 * The function lives on window so app.js (which we can't edit) can call it
 * directly: window.renderMultiHorizon = renderMultiHorizon.
 *
 * Styling: pure inline + a tiny scoped <style> block injected once, on the
 * assumption that the existing terminal.css colour tokens (--green, --red,
 * --amber, --text-primary, ...) are available — see INTEGRATE_MULTIHORIZON.md.
 */

(function () {
  'use strict';

  const STYLE_ID = 'mh-style';
  const STYLE_CSS = `
    .mh-wrap { font-family: var(--font-mono); color: var(--text-primary); }
    .mh-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-top: 4px; }
    .mh-cell {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      padding: 10px;
      display: flex; flex-direction: column; align-items: center;
      gap: 4px;
      position: relative;
    }
    .mh-cell .mh-label {
      font-size: var(--font-size-xs);
      color: var(--text-secondary);
      letter-spacing: 1px;
      text-transform: uppercase;
    }
    .mh-cell .mh-method {
      font-size: 9px;
      color: var(--text-dim);
      letter-spacing: 0.5px;
      text-transform: uppercase;
    }
    .mh-cell .mh-er {
      font-size: var(--font-size-md);
      font-weight: 500;
      margin-top: 2px;
    }
    .mh-cell .mh-conf {
      display: flex; align-items: center; gap: 4px;
      font-size: var(--font-size-xs);
      color: var(--text-secondary);
    }
    .mh-cell .mh-dot {
      width: 6px; height: 6px; border-radius: 50%;
      background: var(--text-dim);
      box-shadow: 0 0 4px currentColor;
    }
    .mh-gauge { width: 64px; height: 64px; }
    .mh-gauge .bg-arc { stroke: var(--border-bright); }
    .mh-gauge .fg-arc { transition: stroke-dashoffset 240ms ease; }
    .mh-gauge .gauge-text {
      fill: var(--text-primary);
      font-family: var(--font-mono);
      font-size: 14px;
      font-weight: 600;
      text-anchor: middle;
      dominant-baseline: central;
    }
    .mh-consensus {
      margin-top: 10px;
      padding: 8px 10px;
      background: var(--bg-elevated);
      border: 1px solid var(--border);
      display: flex; justify-content: space-between; align-items: center;
      font-size: var(--font-size-sm);
      letter-spacing: 0.5px;
    }
    .mh-consensus .mh-c-label { color: var(--text-secondary); text-transform: uppercase; font-size: var(--font-size-xs); }
    .mh-consensus .mh-c-value { font-weight: 600; }
    .mh-diverge {
      margin-top: 6px;
      padding: 6px 10px;
      border-left: 3px solid var(--amber);
      background: var(--amber-glow);
      color: var(--amber);
      font-size: var(--font-size-xs);
      letter-spacing: 0.5px;
    }
    .mh-up    { color: var(--green); }
    .mh-down  { color: var(--red); }
    .mh-mixed { color: var(--amber); }
    .mh-flat  { color: var(--text-secondary); }
    .mh-err {
      padding: 14px;
      color: var(--red);
      font-size: var(--font-size-sm);
      letter-spacing: 0.5px;
    }
    .mh-loading {
      padding: 14px;
      color: var(--text-secondary);
      font-size: var(--font-size-sm);
      letter-spacing: 1px;
    }
  `;

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement('style');
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  function colorForProb(p) {
    if (p == null || isNaN(p)) return 'var(--text-dim)';
    if (p >= 0.6) return 'var(--green)';
    if (p <= 0.4) return 'var(--red)';
    return 'var(--amber)';
  }

  function colorForReturn(pct) {
    if (pct == null || isNaN(pct)) return 'var(--text-dim)';
    if (pct > 0.5) return 'var(--green)';
    if (pct < -0.5) return 'var(--red)';
    return 'var(--text-secondary)';
  }

  function fmtPct(x, withSign) {
    if (x == null || isNaN(x)) return '—';
    const sign = (withSign && x > 0) ? '+' : '';
    return `${sign}${x.toFixed(1)}%`;
  }

  function gaugeSvg(prob) {
    // Three-quarter circle (270deg sweep), starts at 135deg
    const r = 26;
    const cx = 32, cy = 32;
    const circumference = 2 * Math.PI * r;
    const sweep = 0.75; // fraction of circle visible
    const arcLen = circumference * sweep;
    // Fill proportion = prob (0..1)
    const p = Math.max(0, Math.min(1, prob || 0));
    const fillLen = arcLen * p;
    const offset = arcLen - fillLen;

    // Rotate so the arc opens at the bottom (start at 135deg from 12-o'clock)
    const color = colorForProb(prob);
    const label = (p * 100).toFixed(0) + '%';
    return `
      <svg class="mh-gauge" viewBox="0 0 64 64">
        <g transform="rotate(135 ${cx} ${cy})">
          <circle class="bg-arc" cx="${cx}" cy="${cy}" r="${r}"
            fill="none" stroke-width="5"
            stroke-dasharray="${arcLen} ${circumference}" />
          <circle class="fg-arc" cx="${cx}" cy="${cy}" r="${r}"
            fill="none" stroke="${color}" stroke-width="5"
            stroke-linecap="round"
            stroke-dasharray="${arcLen} ${circumference}"
            stroke-dashoffset="${offset}" />
        </g>
        <text class="gauge-text" x="${cx}" y="${cy}" fill="${color}">${label}</text>
      </svg>
    `;
  }

  function confidenceDot(conf) {
    // 0..1 -> color
    let color = 'var(--text-dim)';
    if (conf >= 0.6) color = 'var(--green)';
    else if (conf >= 0.35) color = 'var(--amber)';
    else if (conf > 0) color = 'var(--red-dim)';
    const pct = conf == null ? '—' : (conf * 100).toFixed(0) + '%';
    return `<span class="mh-dot" style="color:${color};background:${color}"></span><span>CONF ${pct}</span>`;
  }

  function renderCell(key, h) {
    const horizonLabel = `${h.horizon_days}D`;
    const method = (h.method || '').replace(/_/g, ' ');
    const prob = h.prob_up;
    const er = h.expected_return_pct;
    const conf = h.confidence;
    const erColor = colorForReturn(er);
    return `
      <div class="mh-cell" data-horizon="${key}">
        <div class="mh-label">${horizonLabel}</div>
        ${gaugeSvg(prob)}
        <div class="mh-er" style="color:${erColor}">${fmtPct(er, true)}</div>
        <div class="mh-conf">${confidenceDot(conf)}</div>
        <div class="mh-method">${method}</div>
      </div>
    `;
  }

  function consensusBlock(consensus, divergence) {
    if (!consensus) {
      return `<div class="mh-consensus"><span class="mh-c-label">Consensus</span><span class="mh-c-value mh-flat">N/A</span></div>`;
    }
    const dir = consensus.direction || 'UNKNOWN';
    const score = consensus.agreement_score;
    let cls = 'mh-flat', labelText;
    if (dir === 'UP') {
      cls = 'mh-up';
      labelText = score >= 0.85 ? 'ALIGNED BULLISH' : 'BIASED BULLISH';
    } else if (dir === 'DOWN') {
      cls = 'mh-down';
      labelText = score >= 0.85 ? 'ALIGNED BEARISH' : 'BIASED BEARISH';
    } else if (dir === 'MIXED') {
      cls = 'mh-mixed';
      labelText = 'MIXED';
    } else if (dir === 'NEUTRAL') {
      cls = 'mh-flat';
      labelText = 'NEUTRAL';
    } else {
      labelText = dir;
    }
    const scoreStr = (score != null) ? (score * 100).toFixed(0) + '%' : '—';

    let divHtml = '';
    if (divergence) {
      let pretty;
      if (divergence === 'SHORT_BEARISH_LONG_BULLISH')
        pretty = 'DIVERGENT: short BEARISH / long BULLISH';
      else if (divergence === 'SHORT_BULLISH_LONG_BEARISH')
        pretty = 'DIVERGENT: short BULLISH / long BEARISH';
      else
        pretty = divergence;
      divHtml = `<div class="mh-diverge">${pretty}</div>`;
    }

    return `
      <div class="mh-consensus">
        <span class="mh-c-label">Consensus · agreement ${scoreStr}</span>
        <span class="mh-c-value ${cls}">${labelText}</span>
      </div>
      ${divHtml}
    `;
  }

  function renderError(containerEl, msg) {
    containerEl.innerHTML = `<div class="mh-err">multi-horizon: ${msg}</div>`;
  }

  /**
   * renderMultiHorizon(containerEl, symbol)
   *   Fetches /api/research/horizons/<symbol> and paints the 4-cell grid +
   *   consensus + divergence callout into containerEl. Safe to call repeatedly
   *   for the same container — it fully replaces innerHTML.
   */
  async function renderMultiHorizon(containerEl, symbol) {
    if (!containerEl) return;
    ensureStyle();

    const sym = (symbol || '').toString().toUpperCase().trim();
    if (!sym) {
      renderError(containerEl, 'no symbol');
      return;
    }

    containerEl.innerHTML = `<div class="mh-loading">Loading multi-horizon forecast for ${sym}…</div>`;

    let data;
    try {
      const r = await fetch(`/api/research/horizons/${encodeURIComponent(sym)}`);
      if (!r.ok) {
        renderError(containerEl, `HTTP ${r.status}`);
        return;
      }
      data = await r.json();
    } catch (e) {
      renderError(containerEl, e && e.message ? e.message : 'fetch failed');
      return;
    }

    if (!data || data.error) {
      renderError(containerEl, (data && data.error) || 'no data');
      return;
    }

    const horizons = data.horizons || {};
    const order = ['h5', 'h20', 'h60', 'h120'];
    const cellsHtml = order.map(k => {
      const h = horizons[k] || {
        horizon_days: parseInt(k.slice(1), 10),
        prob_up: null,
        expected_return_pct: null,
        confidence: null,
        method: 'unavailable'
      };
      return renderCell(k, h);
    }).join('');

    containerEl.innerHTML = `
      <div class="mh-wrap">
        <div class="mh-grid">${cellsHtml}</div>
        ${consensusBlock(data.consensus, data.divergence_flag)}
      </div>
    `;
  }

  // Expose on window so app.js can find it without a module system.
  window.renderMultiHorizon = renderMultiHorizon;
})();
