/* research_factors.js — Fama-French 5 + momentum factor exposure UI.
 *
 * Two exported helpers:
 *
 *   renderFactorExposure(container, symbol [, opts])
 *       Per-symbol horizontal bar chart of factor betas with t-stats as
 *       side labels, plus KPI cards for annualised alpha, R², and
 *       idiosyncratic vol. Fetches /api/research/factors/<symbol>.
 *
 *   renderPortfolioFactors(container, holdings [, opts])
 *       Same visual layout for the aggregated portfolio, fed from
 *       /api/research/factors/portfolio (POST). Also shows a small block
 *       of "weighted-average vs direct-regression" exposures so the user
 *       can see how much of the picture is just position-weighted betas.
 *
 * Both renderers are defensive: missing API helpers fall back to fetch();
 * any error renders an empty-state card with the upstream message.
 *
 * No external libraries — the bars are <div>s, sized in % of the panel
 * width, so they slot cleanly into the existing AUGUR design system.
 */

(function (global) {
  "use strict";

  const FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"];
  const FACTOR_HINT = {
    "Mkt-RF": "Equity market premium (overall stock-market beta)",
    "SMB":    "Size: small minus big (positive = small-cap tilt)",
    "HML":    "Value: high minus low B/P (positive = value tilt)",
    "RMW":    "Profitability: robust minus weak (positive = quality)",
    "CMA":    "Investment: conservative minus aggressive",
    "Mom":    "Momentum: prior-12-month winners minus losers",
  };

  // Significance asterisks like academic regression tables.
  function _stars(p) {
    if (p == null || isNaN(p)) return "";
    if (p < 0.001) return "***";
    if (p < 0.01)  return "**";
    if (p < 0.05)  return "*";
    return "";
  }

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _fmt(n, digits) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toFixed(digits == null ? 2 : digits);
  }

  function _api() {
    return (global.API && typeof global.API.get === "function")
      ? global.API
      : { get: (url) => fetch(url).then((r) => r.json()),
          post: (url, body) => fetch(url, {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(body || {}),
          }).then((r) => r.json()) };
  }

  function _emptyCard(msg) {
    return '<div class="empty-state"><span style="color:var(--text-dim)">' +
           _esc(msg || "no data") + '</span></div>';
  }

  function _spinner(label) {
    return '<div class="loading"><div class="spinner"></div> ' + _esc(label || "LOADING…") + '</div>';
  }

  /** Build the horizontal bar HTML for a {factor → {beta,t_stat,p_value}} map.
   *  Bars are anchored at a central zero line: positive betas extend right,
   *  negative extend left. Max abs beta drives the scale so the most-loaded
   *  factor uses the full half-width.
   */
  function _factorBarsHtml(exposures) {
    if (!exposures || typeof exposures !== "object") {
      return _emptyCard("no factor exposures");
    }
    // Find max |beta| for scaling (floor at 0.1 so a flat portfolio's tiny
    // exposures don't look exaggerated).
    let maxAbs = 0.1;
    for (const k of FACTORS) {
      const v = exposures[k];
      const b = v && typeof v === "object" ? v.beta : v;
      if (b != null && Math.abs(b) > maxAbs) maxAbs = Math.abs(b);
    }

    const rows = FACTORS.map((name) => {
      const v = exposures[name];
      let beta, tStat, pVal;
      if (v && typeof v === "object") {
        beta = v.beta; tStat = v.t_stat; pVal = v.p_value;
      } else {
        beta = v; tStat = null; pVal = null;
      }
      const pct = beta == null ? 0 : Math.min(100, (Math.abs(beta) / maxAbs) * 100);
      const isPos = (beta || 0) >= 0;
      const color = isPos ? "var(--accent, #4ade80)" : "var(--danger, #f87171)";
      // Left half of the row (negative bars) vs right half (positive bars).
      // Layout: [label 90px][bar half-track][zero line][bar half-track][stats]
      const leftBar  = !isPos
        ? `<div style="width:${pct}%;height:14px;background:${color};margin-left:auto;border-radius:2px;"></div>`
        : "";
      const rightBar = isPos
        ? `<div style="width:${pct}%;height:14px;background:${color};border-radius:2px;"></div>`
        : "";
      const stars = _stars(pVal);
      const tTxt = tStat == null ? "" : `t=${_fmt(tStat, 1)}${stars}`;
      const betaTxt = beta == null ? "—" : _fmt(beta, 3);
      return `
        <div style="display:grid;grid-template-columns:96px 1fr 1px 1fr 110px;align-items:center;gap:6px;padding:3px 0;font-size:11px;">
          <div title="${_esc(FACTOR_HINT[name] || name)}" style="text-align:right;color:var(--text-dim);font-family:var(--font-mono,monospace);">${_esc(name)}</div>
          <div style="display:flex;justify-content:flex-end;">${leftBar}</div>
          <div style="background:var(--border,#333);height:18px;"></div>
          <div style="display:flex;justify-content:flex-start;">${rightBar}</div>
          <div style="font-family:var(--font-mono,monospace);"><span style="color:${color};font-weight:600;">${_esc(betaTxt)}</span> <span style="color:var(--text-dim);font-size:10px;">${_esc(tTxt)}</span></div>
        </div>`;
    }).join("");
    return `<div class="factor-bars">${rows}</div>`;
  }

  function _kpiBlock(data) {
    const r2 = data.r_squared;
    const alpha = data.alpha_annual_pct;
    const alphaT = data.alpha_t_stat;
    const alphaP = data.alpha_p_value;
    const iv = data.idiosyncratic_vol_pct;
    const n = data.n_obs;
    const stars = _stars(alphaP);
    const r2Pct = r2 == null ? "—" : (Number(r2) * 100).toFixed(1) + "%";
    const ivPct = iv == null ? "—" : Number(iv).toFixed(1) + "%";
    const alphaTxt = alpha == null ? "—" : (alpha > 0 ? "+" : "") + Number(alpha).toFixed(2) + "%";
    const alphaColor = alpha == null
      ? "var(--text-dim)"
      : (alpha > 0 ? "var(--accent, #4ade80)" : "var(--danger, #f87171)");
    return `
      <div class="kpi-grid" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr));margin-bottom:10px;">
        <div class="kpi-card">
          <div class="kpi-label">ALPHA (ANNUAL)</div>
          <div class="kpi-value" style="color:${alphaColor};">${_esc(alphaTxt)}${_esc(stars)}</div>
          <div class="kpi-sub" style="font-size:9px;">t=${_fmt(alphaT, 1)} · p=${_fmt(alphaP, 3)}</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">R²</div>
          <div class="kpi-value">${_esc(r2Pct)}</div>
          <div class="kpi-sub" style="font-size:9px;">explained by factors</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">IDIOSYNCRATIC σ</div>
          <div class="kpi-value">${_esc(ivPct)}</div>
          <div class="kpi-sub" style="font-size:9px;">annualised residual vol</div>
        </div>
        <div class="kpi-card">
          <div class="kpi-label">OBSERVATIONS</div>
          <div class="kpi-value">${_esc(n == null ? "—" : String(n))}</div>
          <div class="kpi-sub" style="font-size:9px;">daily, as of ${_esc(data.as_of || "")}</div>
        </div>
      </div>`;
  }

  function _renderResult(container, data, opts) {
    if (!container) return;
    if (!data || data.error) {
      container.innerHTML = _emptyCard(data && data.error ? data.error : "factor regression unavailable");
      return;
    }
    const title = opts && opts.title ? opts.title : "FACTOR EXPOSURE";
    const sub = opts && opts.subtitle ? opts.subtitle : "";
    const exposures = data.exposures || {};
    const weighted = data.weighted_exposures;
    let weightedHtml = "";
    if (weighted && weighted.exposures) {
      const rows = FACTORS.map((name) => {
        const wb = weighted.exposures[name];
        const db = exposures[name] && exposures[name].beta;
        return `<tr>
          <td style="color:var(--text-dim);padding:2px 8px 2px 0;">${_esc(name)}</td>
          <td style="text-align:right;font-family:var(--font-mono,monospace);">${_esc(_fmt(wb, 3))}</td>
          <td style="text-align:right;font-family:var(--font-mono,monospace);color:var(--text-dim);">${_esc(_fmt(db, 3))}</td>
        </tr>`;
      }).join("");
      weightedHtml = `
        <details style="margin-top:8px;font-size:11px;">
          <summary style="cursor:pointer;color:var(--text-dim);">weighted-avg vs direct-regression betas</summary>
          <table style="width:auto;margin-top:6px;border-collapse:collapse;">
            <thead><tr style="font-size:9px;color:var(--text-dim);">
              <th style="text-align:left;padding-right:8px;">FACTOR</th>
              <th style="text-align:right;padding-right:8px;">WEIGHTED</th>
              <th style="text-align:right;">DIRECT</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </details>`;
    }
    container.innerHTML = `
      <div class="factor-panel">
        <div class="section-header" style="display:flex;align-items:baseline;justify-content:space-between;">
          <h3 style="margin:0;">${_esc(title)}</h3>
          <span style="color:var(--text-dim);font-size:11px;">${_esc(sub)}</span>
        </div>
        ${_kpiBlock(data)}
        ${_factorBarsHtml(exposures)}
        ${weightedHtml}
        ${data.note ? `<div style="color:var(--text-dim);font-size:10px;margin-top:6px;">${_esc(data.note)}</div>` : ""}
      </div>`;
  }

  function renderFactorExposure(container, symbol, opts) {
    if (!container) return;
    if (!symbol) {
      container.innerHTML = _emptyCard("no symbol");
      return;
    }
    container.innerHTML = _spinner(`REGRESSING ${symbol.toUpperCase()} ON FF5+MOM…`);
    const years = (opts && opts.years) ? Number(opts.years) : 3;
    const url = `/api/research/factors/${encodeURIComponent(symbol)}?years=${encodeURIComponent(years)}`;
    _api().get(url)
      .then((data) => _renderResult(container, data, {
        title: `FACTOR EXPOSURE — ${symbol.toUpperCase()}`,
        subtitle: `${years}y daily · FF5 + momentum`,
      }))
      .catch((err) => {
        container.innerHTML = _emptyCard("network error: " + (err && err.message ? err.message : err));
      });
  }

  function renderPortfolioFactors(container, holdings, opts) {
    if (!container) return;
    if (!Array.isArray(holdings) || holdings.length === 0) {
      container.innerHTML = _emptyCard("no holdings");
      return;
    }
    container.innerHTML = _spinner("REGRESSING PORTFOLIO ON FF5+MOM…");
    const years = (opts && opts.years) ? Number(opts.years) : 3;
    const api = _api();
    const url = `/api/research/factors/portfolio?years=${encodeURIComponent(years)}`;
    // Some hosts may prefer GET semantics; we POST the holdings list as JSON.
    const sender = typeof api.post === "function"
      ? api.post(url, { holdings: holdings })
      : fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ holdings: holdings }),
        }).then((r) => r.json());
    sender
      .then((data) => _renderResult(container, data, {
        title: "PORTFOLIO FACTOR EXPOSURE",
        subtitle: `${years}y daily · ${holdings.length} holding${holdings.length === 1 ? "" : "s"}`,
      }))
      .catch((err) => {
        container.innerHTML = _emptyCard("network error: " + (err && err.message ? err.message : err));
      });
  }

  // Expose
  global.renderFactorExposure = renderFactorExposure;
  global.renderPortfolioFactors = renderPortfolioFactors;
})(typeof window !== "undefined" ? window : this);
