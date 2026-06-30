/* synth_macrotranslate.js — Cross-Asset Macro Translator UI.
 *
 * Renders the historical factor / sector response to a macro release and,
 * when a portfolio is supplied, projects the expected 5-day reaction with
 * an IQR / best-case / worst-case bracket.
 *
 * Exposes one global:
 *
 *   window.renderMacroTranslate(containerEl, releaseId, portfolio)
 *
 * `portfolio` is optional; if it's a non-empty list of
 * {symbol, market_value} objects we POST it to the backend so the
 * projection block is computed. Otherwise we GET the average-response
 * view only.
 *
 * No charting libs required — the bars are plain divs so the panel slots
 * into AUGUR's existing design system. We DO use Chart.js when present
 * for the historical-episode SPY 5d distribution.
 */

(function (global) {
  "use strict";

  // Curated release menu (kept in sync with fred_data.SERIES_CATALOG + "FOMC").
  const RELEASES = [
    { id: "CPIAUCSL",  label: "CPI",                category: "inflation" },
    { id: "PAYEMS",    label: "Nonfarm Payrolls",   category: "labor"     },
    { id: "UNRATE",    label: "Unemployment Rate",  category: "labor"     },
    { id: "FEDFUNDS",  label: "Fed Funds Rate",     category: "rates"     },
    { id: "FOMC",      label: "FOMC Meeting",       category: "rates"     },
    { id: "DFII10",    label: "10Y Real Yield",     category: "rates"     },
    { id: "T10Y2Y",    label: "10Y-2Y Spread",      category: "rates"     },
    { id: "PCE",       label: "PCE",                category: "consumer"  },
    { id: "RSAFS",     label: "Retail Sales",       category: "consumer"  },
    { id: "UMCSENT",   label: "UMich Consumer",     category: "consumer"  },
    { id: "GDP",       label: "Nominal GDP",        category: "growth"    },
    { id: "M2SL",      label: "M2 Money Supply",    category: "monetary"  },
    { id: "DCOILWTICO",label: "WTI Crude",          category: "commodity" },
  ];

  // One chart per container (SPY 5d distribution).
  const _CHART_REG = new WeakMap();

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _fmtPct(v, digits) {
    if (v == null || !isFinite(v)) return "—";
    const d = digits == null ? 2 : digits;
    const sign = v > 0 ? "+" : "";
    return sign + Number(v).toFixed(d) + "%";
  }

  function _pnlClass(v) {
    if (v == null || !isFinite(v)) return "";
    return v >= 0 ? "pnl-pos" : "pnl-neg";
  }

  function _maxAbs(values) {
    let m = 0;
    for (const v of values) {
      if (v == null || !isFinite(v)) continue;
      const a = Math.abs(v);
      if (a > m) m = a;
    }
    return m || 1;
  }

  function _bar(label, value, maxAbs, units) {
    // Diverging bar around a 50% centerline.
    const v = (value == null || !isFinite(value)) ? 0 : value;
    const pct = Math.min(50, Math.abs(v) / maxAbs * 50);
    const right = v >= 0;
    const color = right ? "rgba(80, 220, 120, 0.55)" : "rgba(255, 110, 110, 0.55)";
    const valTxt = (value == null || !isFinite(value))
      ? "—"
      : _fmtPct(value, 2) + (units ? " " + units : "");
    return `
      <div class="mt-bar-row" style="display:grid;grid-template-columns:110px 1fr 70px;align-items:center;gap:6px;margin-bottom:3px">
        <div style="font-size:11px;color:var(--text-dim)">${_esc(label)}</div>
        <div style="position:relative;height:14px;background:rgba(255,255,255,0.04);border-radius:2px">
          <div style="position:absolute;top:0;bottom:0;left:50%;width:1px;background:rgba(255,255,255,0.2)"></div>
          <div style="position:absolute;top:1px;bottom:1px;${right ? "left:50%" : "right:50%"};width:${pct}%;background:${color};border-radius:2px"></div>
        </div>
        <div class="${_pnlClass(value)}" style="font-size:11px;text-align:right">${valTxt}</div>
      </div>`;
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

  function _fetchData(releaseId, surprise, portfolio) {
    const api = _api();
    const qs = [];
    if (surprise != null && isFinite(surprise)) qs.push("surprise=" + encodeURIComponent(surprise));
    const q = qs.length ? "?" + qs.join("&") : "";
    if (portfolio && portfolio.length) {
      return api.post(
        `/api/synth/macrotranslate/${encodeURIComponent(releaseId)}/portfolio${q}`,
        { holdings: portfolio, surprise_pct: surprise }
      );
    }
    return api.get(`/api/synth/macrotranslate/${encodeURIComponent(releaseId)}${q}`);
  }

  function _buildSkeleton(containerEl, releaseId) {
    const opts = RELEASES.map(r =>
      `<option value="${r.id}" ${r.id === releaseId ? "selected" : ""}>${_esc(r.label)}</option>`
    ).join("");

    containerEl.innerHTML = `
      <div class="panel" id="mt-panel">
        <div class="panel-header">
          <span class="panel-title">TRANSLATE TO PORTFOLIO</span>
          <div style="display:flex;gap:10px;align-items:center;font-size:11px">
            <label>Release:
              <select id="mt-release" style="background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px">
                ${opts}
              </select>
            </label>
            <label>Surprise (%):
              <input type="number" id="mt-surprise" step="0.1" placeholder="0.0" style="width:60px;background:transparent;color:inherit;border:1px solid var(--border);padding:2px 4px">
            </label>
            <button class="btn btn-ghost btn-sm" id="mt-run">RUN</button>
          </div>
        </div>
        <div class="panel-body">
          <div id="mt-header" style="font-size:11px;color:var(--text-dim);margin-bottom:10px"></div>
          <div id="mt-body" style="display:grid;grid-template-columns:1fr 1fr;gap:14px"></div>
          <div id="mt-history" style="margin-top:14px"></div>
          <div id="mt-status" style="font-size:10px;color:var(--text-dim);margin-top:6px"></div>
        </div>
      </div>`;
  }

  function _renderHeader(release) {
    if (!release) return "";
    const parts = [];
    parts.push(`<b>${_esc(release.name || release.id)}</b>`);
    if (release.latest_date) parts.push(`latest: ${_esc(release.latest_date)}`);
    if (release.latest_value != null) {
      parts.push(`value: ${Number(release.latest_value).toFixed(2)}`);
    }
    if (release.delta_pct != null) {
      const cls = _pnlClass(release.delta_pct);
      parts.push(`Δ: <span class="${cls}">${_fmtPct(release.delta_pct, 2)}</span>`);
    }
    if (release.surprise_pct != null) {
      const cls = _pnlClass(release.surprise_pct);
      parts.push(`surprise: <span class="${cls}">${_fmtPct(release.surprise_pct, 2)}</span>`);
    }
    if (release.frequency) parts.push(`freq: ${_esc(release.frequency)}`);
    return parts.join(" · ");
  }

  function _renderAverage(avg) {
    if (!avg) return _emptyCard("no average response");
    const fac = avg.factor_returns_5d || {};
    const sec = avg.sector_returns_5d || {};
    const facNames = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"];
    const facVals = facNames.map(n => fac[n]);
    const secOrdered = Object.entries(sec).sort((a, b) => (b[1] || 0) - (a[1] || 0));

    const facMax = _maxAbs(facVals);
    const secMax = _maxAbs(secOrdered.map(e => e[1]));

    const facHtml = facNames.map(n => _bar(n, fac[n], facMax)).join("");
    const secHtml = secOrdered.map(([k, v]) => _bar(k, v, secMax)).join("");

    return `
      <div>
        <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">AVG FACTOR RESPONSE — 5d</div>
        ${facHtml}
        <div style="font-size:10px;color:var(--text-dim);margin:10px 0 4px">AVG SECTOR RESPONSE — 5d</div>
        ${secHtml}
        <div style="display:flex;gap:14px;margin-top:10px;font-size:11px">
          <div>
            <div style="font-size:10px;color:var(--text-dim)">AVG SP500 1d</div>
            <div class="${_pnlClass(avg.sp500_1d_pct)}" style="font-size:14px">${_fmtPct(avg.sp500_1d_pct, 2)}</div>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text-dim)">AVG SP500 5d</div>
            <div class="${_pnlClass(avg.sp500_5d_pct)}" style="font-size:14px">${_fmtPct(avg.sp500_5d_pct, 2)}</div>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text-dim)">SP500 5d IQR</div>
            <div style="font-size:13px">${_fmtPct((avg.sp500_5d_iqr||[])[0], 2)} … ${_fmtPct((avg.sp500_5d_iqr||[])[1], 2)}</div>
          </div>
        </div>
      </div>`;
  }

  function _renderProjection(proj) {
    if (!proj) {
      return `
        <div>
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">PORTFOLIO PROJECTION</div>
          <div style="font-size:11px;color:var(--text-dim)">
            Pass a holdings list to project expected reaction for your book.
          </div>
        </div>`;
    }
    if (proj.error) {
      return `
        <div>
          <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">PORTFOLIO PROJECTION</div>
          <div style="font-size:11px;color:var(--bad,#f88)">${_esc(proj.error)}</div>
        </div>`;
    }
    const exp = proj.expected_5d_pct;
    const iqr = proj.iqr_5d || [null, null];
    const wins = (proj.top_winners || []).slice(0, 5);
    const lose = (proj.top_losers || []).slice(0, 5);

    const contribRows = Object.entries(proj.factor_contributions || {})
      .sort((a, b) => (Math.abs(Number(b[1]) || 0)) - (Math.abs(Number(a[1]) || 0)))
      .map(([k, v]) => `
        <tr>
          <td style="padding:2px 4px;color:var(--text-dim)">${_esc(k)}</td>
          <td class="${_pnlClass(v)}" style="padding:2px 4px;text-align:right">${_fmtPct(v, 3)}</td>
        </tr>`).join("");

    const winRows = wins.map(w => `
      <tr>
        <td style="padding:2px 4px">${_esc(w.symbol)}</td>
        <td class="${_pnlClass(w.projected_5d)}" style="padding:2px 4px;text-align:right">${_fmtPct(w.projected_5d, 2)}</td>
      </tr>`).join("");
    const loseRows = lose.map(w => `
      <tr>
        <td style="padding:2px 4px">${_esc(w.symbol)}</td>
        <td class="${_pnlClass(w.projected_5d)}" style="padding:2px 4px;text-align:right">${_fmtPct(w.projected_5d, 2)}</td>
      </tr>`).join("");

    return `
      <div>
        <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">PORTFOLIO PROJECTION</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <div>
            <div style="font-size:10px;color:var(--text-dim)">EXPECTED 5d</div>
            <div class="${_pnlClass(exp)}" style="font-size:20px">${_fmtPct(exp, 2)}</div>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text-dim)">IQR (25–75)</div>
            <div style="font-size:13px">${_fmtPct(iqr[0], 2)} … ${_fmtPct(iqr[1], 2)}</div>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text-dim)">BEST CASE</div>
            <div class="${_pnlClass(proj.best_case)}" style="font-size:14px">${_fmtPct(proj.best_case, 2)}</div>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text-dim)">WORST CASE</div>
            <div class="${_pnlClass(proj.worst_case)}" style="font-size:14px">${_fmtPct(proj.worst_case, 2)}</div>
          </div>
        </div>

        <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">FACTOR CONTRIBUTIONS</div>
        <table style="width:100%;font-size:11px;margin-bottom:8px">${contribRows || ""}</table>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          <div>
            <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">TOP WINNERS</div>
            <table style="width:100%;font-size:11px">${winRows || ""}</table>
          </div>
          <div>
            <div style="font-size:10px;color:var(--text-dim);margin-bottom:2px">TOP LOSERS</div>
            <table style="width:100%;font-size:11px">${loseRows || ""}</table>
          </div>
        </div>
      </div>`;
  }

  function _renderHistory(containerEl, episodes) {
    const host = containerEl.querySelector("#mt-history");
    if (!host) return;
    const eps = (episodes || []).slice().reverse();
    if (!eps.length) {
      host.innerHTML = "";
      return;
    }
    const rows = eps.map(e => `
      <tr>
        <td style="padding:2px 6px">${_esc(e.date)}</td>
        <td style="padding:2px 6px;text-align:right;color:var(--text-dim)">${e.release_value == null ? "—" : Number(e.release_value).toFixed(2)}</td>
        <td class="${_pnlClass(e.sp500_1d_pct)}" style="padding:2px 6px;text-align:right">${_fmtPct(e.sp500_1d_pct, 2)}</td>
        <td class="${_pnlClass(e.sp500_5d_pct)}" style="padding:2px 6px;text-align:right">${_fmtPct(e.sp500_5d_pct, 2)}</td>
      </tr>`).join("");

    host.innerHTML = `
      <details>
        <summary style="cursor:pointer;font-size:11px;color:var(--text-dim)">
          HISTORICAL EPISODES (${eps.length})
        </summary>
        <div style="max-height:240px;overflow-y:auto;margin-top:6px">
          <table class="data-table" style="width:100%;font-size:11px">
            <thead>
              <tr style="color:var(--text-dim)">
                <th style="padding:4px 6px;text-align:left">Date</th>
                <th style="padding:4px 6px;text-align:right">Value</th>
                <th style="padding:4px 6px;text-align:right">SPY 1d</th>
                <th style="padding:4px 6px;text-align:right">SPY 5d</th>
              </tr>
            </thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      </details>`;
  }

  function _emptyCard(msg) {
    return `<div style="color:var(--text-dim);font-size:11px">${_esc(msg || "no data")}</div>`;
  }

  function _renderResult(containerEl, data, portfolio) {
    const headerEl  = containerEl.querySelector("#mt-header");
    const bodyEl    = containerEl.querySelector("#mt-body");
    const statusEl  = containerEl.querySelector("#mt-status");
    if (!bodyEl) return;

    if (!data) {
      bodyEl.innerHTML = _emptyCard("network error");
      return;
    }
    if (data.error && !data.average_response) {
      if (headerEl) headerEl.innerHTML = _renderHeader(data.release);
      bodyEl.innerHTML = _emptyCard(data.error);
      return;
    }

    if (headerEl) headerEl.innerHTML = _renderHeader(data.release);
    bodyEl.innerHTML = _renderAverage(data.average_response) + _renderProjection(data.portfolio_projection);
    _renderHistory(containerEl, data.historical_episodes);

    if (statusEl) {
      const n = (data.historical_episodes || []).length;
      statusEl.textContent = `as of ${data.as_of || "—"} · ${n} historical episodes`;
    }
  }

  async function _runQuery(containerEl, releaseId, surprise, portfolio) {
    const statusEl = containerEl.querySelector("#mt-status");
    if (statusEl) statusEl.textContent = `Translating ${releaseId}…`;
    let data;
    try {
      data = await _fetchData(releaseId, surprise, portfolio);
    } catch (e) {
      const bodyEl = containerEl.querySelector("#mt-body");
      if (bodyEl) bodyEl.innerHTML = _emptyCard("Network error: " + e);
      return;
    }
    _renderResult(containerEl, data, portfolio);
  }

  function renderMacroTranslate(containerEl, releaseId, portfolio) {
    if (!containerEl) return;
    const rid = (releaseId || "CPIAUCSL").toString().toUpperCase();

    _buildSkeleton(containerEl, rid);

    const sel  = containerEl.querySelector("#mt-release");
    const surp = containerEl.querySelector("#mt-surprise");
    const btn  = containerEl.querySelector("#mt-run");

    const runNow = () => {
      const id = sel ? sel.value : rid;
      const s  = surp && surp.value !== "" ? Number(surp.value) : null;
      _runQuery(containerEl, id, s, portfolio);
    };

    if (sel) sel.addEventListener("change", runNow);
    if (btn) btn.addEventListener("click", runNow);

    runNow();
  }

  global.renderMacroTranslate = renderMacroTranslate;
})(window);
