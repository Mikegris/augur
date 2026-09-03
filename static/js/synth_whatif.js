/* synth_whatif.js — Portfolio What-If panel for AUGUR.
 *
 * Public entry point:
 *   renderWhatIf(containerEl, opts?)
 *     opts.holdings      — current portfolio [{symbol, market_value}, ...]
 *                          If omitted, GETs /api/portfolio and uses the
 *                          enriched holdings from there.
 *     opts.candidate     — pre-fill the candidate form (optional)
 *
 * The panel POSTs to /api/synth/whatif with body
 *   { current_holdings: [...], candidate: {symbol, market_value, action} }
 * and renders:
 *   - Candidate input form (symbol, $ amount, add/remove/resize_to action)
 *   - Two-column "current vs proposed" KPI grid
 *   - Side-by-side factor exposure bars
 *   - Side-by-side stress-test loss bars (% per scenario)
 *   - Monte Carlo NAV-cone shift readout
 *   - Optimizer verdict callout (color-coded OK / OVERSIZED / UNDERSIZED / AVOID)
 *
 * No external libraries beyond what's already in the page (Chart.js is NOT
 * required — bars are CSS-sized divs). Vanilla ES2017, loaded as <script>.
 */

(function (global) {
  "use strict";

  const POST_PATH = "/api/synth/whatif";
  const PORTFOLIO_PATH = "/api/portfolio";

  const FACTORS = ["Mkt-RF", "SMB", "HML", "RMW", "CMA", "Mom"];

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }
  function _fmt(n, digits) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toFixed(digits == null ? 2 : digits);
  }
  function _fmtPct(n, digits) {
    if (n == null || isNaN(n)) return "—";
    const v = Number(n).toFixed(digits == null ? 2 : digits);
    return v + "%";
  }
  function _fmtMoney(n) {
    if (n == null || isNaN(n)) return "—";
    const x = Number(n);
    const abs = Math.abs(x);
    if (abs >= 1e6) return (x < 0 ? "-" : "") + "$" + (abs / 1e6).toFixed(2) + "M";
    if (abs >= 1e3) return (x < 0 ? "-" : "") + "$" + (abs / 1e3).toFixed(1) + "k";
    return (x < 0 ? "-" : "") + "$" + abs.toFixed(0);
  }
  function _signed(n, digits) {
    if (n == null || isNaN(n)) return "—";
    const v = Number(n).toFixed(digits == null ? 2 : digits);
    return (n > 0 ? "+" : "") + v;
  }
  function _signedMoney(n) {
    if (n == null || isNaN(n)) return "—";
    const s = _fmtMoney(Math.abs(n));
    return (n >= 0 ? "+" : "-") + s.replace(/^[-+]?/, "");
  }

  function _api() {
    return (global.API && typeof global.API.get === "function")
      ? global.API
      : {
          get: (url) => fetch(url).then((r) => r.json()),
          post: (url, body) => fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body || {}),
          }).then((r) => r.json()),
        };
  }

  function _empty(msg) {
    return '<div class="empty-state"><span style="color:var(--text-dim);">'
      + _esc(msg || "no data") + '</span></div>';
  }
  function _spinner(label) {
    return '<div class="loading"><div class="spinner"></div> '
      + _esc(label || "COMPUTING…") + '</div>';
  }

  function _deltaColor(v, invert) {
    // invert=true for fields where "down" is good (e.g. vol, drawdown).
    if (v == null || isNaN(v)) return "var(--text-dim)";
    const pos = invert ? v < 0 : v > 0;
    const neg = invert ? v > 0 : v < 0;
    if (pos) return "var(--accent, #4ade80)";
    if (neg) return "var(--danger, #f87171)";
    return "var(--text-dim)";
  }

  // ── KPI side-by-side card ─────────────────────────────────────────────────
  function _kpiCard(label, currentVal, proposedVal, deltaVal, opts) {
    opts = opts || {};
    const dColor = _deltaColor(deltaVal, opts.invertDelta);
    const fmt = opts.fmt || ((v) => _fmt(v, 2));
    const dFmt = opts.deltaFmt || ((v) => _signed(v, 3));
    return `
      <div class="kpi-card" style="padding:8px;">
        <div class="kpi-label" style="font-size:9px;letter-spacing:0.06em;">${_esc(label)}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:2px;">
          <div>
            <div style="font-size:9px;color:var(--text-dim);">CURRENT</div>
            <div style="font-family:var(--font-mono,monospace);font-size:14px;">${_esc(fmt(currentVal))}</div>
          </div>
          <div>
            <div style="font-size:9px;color:var(--text-dim);">PROPOSED</div>
            <div style="font-family:var(--font-mono,monospace);font-size:14px;">${_esc(fmt(proposedVal))}</div>
          </div>
        </div>
        <div style="font-size:11px;font-family:var(--font-mono,monospace);color:${dColor};margin-top:4px;">
          Δ ${_esc(dFmt(deltaVal))}
        </div>
      </div>`;
  }

  // ── side-by-side factor exposure bars ─────────────────────────────────────
  function _factorBarsSideBySide(curMap, propMap) {
    if (!curMap && !propMap) return _empty("factor exposure unavailable");
    const safeCur = curMap || {};
    const safeProp = propMap || {};
    let maxAbs = 0.1;
    FACTORS.forEach((k) => {
      const a = Math.abs(safeCur[k] || 0);
      const b = Math.abs(safeProp[k] || 0);
      if (a > maxAbs) maxAbs = a;
      if (b > maxAbs) maxAbs = b;
    });

    const rowFor = (name) => {
      const cur = safeCur[name];
      const prop = safeProp[name];
      const renderBar = (val) => {
        if (val == null) return '<div style="color:var(--text-dim);font-size:11px;">—</div>';
        const pct = Math.min(100, (Math.abs(val) / maxAbs) * 100);
        const color = val >= 0 ? "var(--accent, #4ade80)" : "var(--danger, #f87171)";
        const isPos = val >= 0;
        const left = isPos
          ? '<div style="flex:1 1 0;"></div>'
          : `<div style="flex:1 1 0;display:flex;justify-content:flex-end;"><div style="width:${pct}%;height:10px;background:${color};border-radius:2px;"></div></div>`;
        const right = isPos
          ? `<div style="flex:1 1 0;"><div style="width:${pct}%;height:10px;background:${color};border-radius:2px;"></div></div>`
          : '<div style="flex:1 1 0;"></div>';
        return `
          <div style="display:flex;align-items:center;gap:4px;">
            ${left}
            <div style="width:1px;height:14px;background:var(--border,#333);"></div>
            ${right}
            <span style="width:48px;text-align:right;font-family:var(--font-mono,monospace);font-size:11px;color:${color};">${_esc(_fmt(val, 2))}</span>
          </div>`;
      };
      return `
        <div style="display:grid;grid-template-columns:64px 1fr 1fr;gap:8px;align-items:center;padding:3px 0;">
          <div style="text-align:right;color:var(--text-dim);font-size:10px;font-family:var(--font-mono,monospace);">${_esc(name)}</div>
          ${renderBar(cur)}
          ${renderBar(prop)}
        </div>`;
    };
    return `
      <div>
        <div style="display:grid;grid-template-columns:64px 1fr 1fr;gap:8px;font-size:9px;color:var(--text-dim);margin-bottom:4px;">
          <div></div>
          <div style="text-align:center;">CURRENT</div>
          <div style="text-align:center;">PROPOSED</div>
        </div>
        ${FACTORS.map(rowFor).join("")}
      </div>`;
  }

  // ── stress-test side-by-side bars ─────────────────────────────────────────
  const SCENARIO_LABEL = {
    "2008": "2008 Financial Crisis",
    "covid": "2020 COVID Crash",
    "dot_com": "2000 Dot-com",
    "rate_hike": "2022 Rate Hike",
  };
  function _stressBars(cur, prop) {
    if (!cur && !prop) return _empty("stress-test unavailable");
    const safeCur = cur || {};
    const safeProp = prop || {};
    const keys = Object.keys(SCENARIO_LABEL);
    let maxAbs = 5.0;
    keys.forEach((k) => {
      if (safeCur[k] != null) maxAbs = Math.max(maxAbs, Math.abs(safeCur[k]));
      if (safeProp[k] != null) maxAbs = Math.max(maxAbs, Math.abs(safeProp[k]));
    });

    const rowFor = (k) => {
      const c = safeCur[k];
      const p = safeProp[k];
      const bar = (v) => {
        if (v == null) return '<span style="color:var(--text-dim);">—</span>';
        const pct = Math.min(100, (Math.abs(v) / maxAbs) * 100);
        // losses are negative — color always danger
        return `<div style="display:flex;align-items:center;gap:6px;">
            <div style="width:${pct}%;height:8px;background:var(--danger, #f87171);border-radius:2px;"></div>
            <span style="font-family:var(--font-mono,monospace);font-size:11px;color:var(--danger, #f87171);">${_esc(_fmt(v, 1))}%</span>
          </div>`;
      };
      return `
        <div style="display:grid;grid-template-columns:160px 1fr 1fr;gap:8px;padding:3px 0;align-items:center;">
          <div style="font-size:11px;color:var(--text-dim);">${_esc(SCENARIO_LABEL[k] || k)}</div>
          ${bar(c)}
          ${bar(p)}
        </div>`;
    };
    return `
      <div>
        <div style="display:grid;grid-template-columns:160px 1fr 1fr;gap:8px;font-size:9px;color:var(--text-dim);margin-bottom:4px;">
          <div></div>
          <div>CURRENT LOSS</div>
          <div>PROPOSED LOSS</div>
        </div>
        ${keys.map(rowFor).join("")}
      </div>`;
  }

  // ── Monte Carlo NAV cone shift readout ───────────────────────────────────
  function _mcBlock(mcDelta) {
    if (!mcDelta) return _empty("Monte Carlo shift unavailable");
    // WHY (#294): the *_shift values are proposed−current terminal NAV, so
    // POSITIVE = better — _deltaColor's invert=true means "down is good" and
    // painted improvements red / deteriorations green. Only PROB OF LOSS
    // (where a LOWER probability is good) is inverted.
    const rows = [
      ["P05 (worst-case)", mcDelta.p05_shift, false],  // higher worst-case NAV is good
      ["MEDIAN", mcDelta.median_shift, false],
      ["P95 (best-case)", mcDelta.p95_shift, false],
      ["PROB OF LOSS", mcDelta.prob_loss_delta_pct, true],  // invert: a LOWER prob of loss is good
    ];
    return `
      <div style="display:grid;grid-template-columns:repeat(auto-fit, minmax(150px,1fr));gap:8px;">
        ${rows.map(([label, val, invert]) => {
          const color = _deltaColor(val, invert);
          const fmt = label === "PROB OF LOSS"
            ? (label === "PROB OF LOSS" && val != null ? _signed(val, 2) + " pp" : "—")
            : _signedMoney(val);
          return `<div class="kpi-card" style="padding:8px;">
            <div class="kpi-label" style="font-size:9px;">${_esc(label)}</div>
            <div style="font-family:var(--font-mono,monospace);font-size:14px;color:${color};">${_esc(fmt)}</div>
          </div>`;
        }).join("")}
      </div>`;
  }

  // ── Optimizer verdict callout ────────────────────────────────────────────
  function _verdictCallout(opt) {
    if (!opt) {
      return `<div class="empty-state" style="border-left:3px solid var(--text-dim);padding:10px;">
        <strong style="color:var(--text-dim);">OPTIMIZER VERDICT —</strong>
        <span style="color:var(--text-dim);">unavailable</span>
      </div>`;
    }
    const verdict = String(opt.verdict || "—");
    const colors = {
      OK: "var(--accent, #4ade80)",
      OVERSIZED: "var(--danger, #f87171)",
      UNDERSIZED: "var(--warn, #fbbf24)",
      AVOID: "var(--danger, #f87171)",
    };
    const c = colors[verdict] || "var(--text-dim)";
    const optimal = opt.current_optimal_size_pct;
    const yours = opt.your_proposed_size_pct;
    return `
      <div style="border-left:3px solid ${c};padding:10px;background:rgba(255,255,255,0.02);border-radius:3px;">
        <div style="display:flex;align-items:baseline;justify-content:space-between;flex-wrap:wrap;gap:8px;">
          <div>
            <span style="color:var(--text-dim);font-size:10px;letter-spacing:0.08em;">MAX-SHARPE VERDICT —</span>
            <strong style="color:${c};font-size:14px;margin-left:6px;">${_esc(verdict)}</strong>
          </div>
          <div style="font-family:var(--font-mono,monospace);font-size:11px;color:var(--text-dim);">
            optimal: <span style="color:var(--text-fg, #eee);">${_esc(_fmtPct((optimal || 0) * 100, 1))}</span>
            · proposed: <span style="color:var(--text-fg, #eee);">${_esc(_fmtPct((yours || 0) * 100, 1))}</span>
          </div>
        </div>
        <div style="font-size:10px;color:var(--text-dim);margin-top:4px;">
          Optimizer Sharpe: ${_esc(_fmt(opt.optimizer_sharpe, 3))}
          · E[r]: ${_esc(_fmt(opt.optimizer_expected_return_pct, 2))}%
          · σ: ${_esc(_fmt(opt.optimizer_expected_vol_pct, 2))}%
          ${opt.optimizer_converged === false ? ' · <span style="color:var(--warn, #fbbf24);">non-converged</span>' : ""}
        </div>
      </div>`;
  }

  // ── candidate input form ─────────────────────────────────────────────────
  function _formHtml(prefill) {
    const p = prefill || {};
    return `
      <form id="whatif-form" style="display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;align-items:end;margin-bottom:12px;">
        <label style="display:flex;flex-direction:column;font-size:10px;color:var(--text-dim);">
          SYMBOL
          <input type="text" name="symbol" value="${_esc(p.symbol || "")}" placeholder="e.g. NVDA"
            style="padding:6px;background:var(--bg-2,#222);border:1px solid var(--border,#333);color:inherit;font-family:var(--font-mono,monospace);text-transform:uppercase;" required>
        </label>
        <label style="display:flex;flex-direction:column;font-size:10px;color:var(--text-dim);">
          MARKET VALUE ($)
          <input type="number" name="market_value" value="${_esc(p.market_value != null ? p.market_value : 20000)}" min="0" step="100"
            style="padding:6px;background:var(--bg-2,#222);border:1px solid var(--border,#333);color:inherit;font-family:var(--font-mono,monospace);">
        </label>
        <label style="display:flex;flex-direction:column;font-size:10px;color:var(--text-dim);">
          ACTION
          <select name="action" style="padding:6px;background:var(--bg-2,#222);border:1px solid var(--border,#333);color:inherit;">
            <option value="add" ${p.action === "add" || !p.action ? "selected" : ""}>ADD position</option>
            <option value="remove" ${p.action === "remove" ? "selected" : ""}>REMOVE position</option>
            <option value="resize_to" ${p.action === "resize_to" ? "selected" : ""}>RESIZE TO</option>
          </select>
        </label>
        <button type="submit" style="padding:8px 16px;background:var(--accent,#4ade80);color:#000;border:none;cursor:pointer;font-weight:600;">
          RUN WHAT-IF
        </button>
      </form>
    `;
  }

  function _topWeightsBlock(topList) {
    if (!Array.isArray(topList) || topList.length === 0) {
      return '<span style="color:var(--text-dim);font-size:11px;">—</span>';
    }
    return topList.map((row) => {
      const pct = ((row.weight || 0) * 100).toFixed(1) + "%";
      return `<div style="display:flex;justify-content:space-between;gap:6px;font-family:var(--font-mono,monospace);font-size:11px;">
        <span>${_esc(row.symbol)}</span>
        <span style="color:var(--text-dim);">${_esc(pct)}</span>
      </div>`;
    }).join("");
  }

  // ── full panel renderer ──────────────────────────────────────────────────
  function _renderResult(container, data, state) {
    if (!container) return;
    if (!data || data.error) {
      container.innerHTML = _formHtml(state.candidate) + _empty(data && data.error ? data.error : "what-if computation failed");
      _wireForm(container, state);
      return;
    }
    const cur = data.current || {};
    const prop = data.proposed || {};
    const d = data.delta || {};
    const opt = data.optimizer_says;
    const mcd = data.monte_carlo_delta;

    const navDelta = (prop.nav != null && cur.nav != null) ? (prop.nav - cur.nav) : null;

    const kpiGrid = `
      <div class="kpi-grid" style="grid-template-columns:repeat(auto-fit, minmax(170px, 1fr));gap:8px;margin-bottom:12px;">
        ${_kpiCard("NAV", cur.nav, prop.nav, navDelta, { fmt: _fmtMoney, deltaFmt: _signedMoney })}
        ${_kpiCard("EXPECTED RETURN", cur.expected_return_pct, prop.expected_return_pct, d.expected_return_pct, {
          fmt: (v) => v == null ? "—" : _fmt(v, 2) + "%",
          deltaFmt: (v) => v == null ? "—" : _signed(v, 2) + " pp",
        })}
        ${_kpiCard("EXPECTED VOL", cur.expected_vol_pct, prop.expected_vol_pct, d.expected_vol_pct, {
          fmt: (v) => v == null ? "—" : _fmt(v, 2) + "%",
          deltaFmt: (v) => v == null ? "—" : _signed(v, 2) + " pp",
          invertDelta: true,
        })}
        ${_kpiCard("SHARPE", null, null, d.sharpe_ratio_delta, {
          fmt: () => "see Δ",
          deltaFmt: (v) => v == null ? "—" : _signed(v, 3),
        })}
        ${_kpiCard("CONCENTRATION (HHI)", cur.concentration_hhi, prop.concentration_hhi, d.concentration_hhi, {
          fmt: (v) => v == null ? "—" : _fmt(v, 3),
          deltaFmt: (v) => v == null ? "—" : _signed(v, 3),
          invertDelta: true,
        })}
        ${_kpiCard("ML-WEIGHTED RETURN", null, null, d.ml_forecast_weighted_return_delta_pct, {
          fmt: () => "see Δ",
          deltaFmt: (v) => v == null ? "—" : _signed(v, 2) + "%",
        })}
      </div>`;

    const topWeights = `
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;">
        <div class="kpi-card" style="padding:8px;">
          <div class="kpi-label" style="font-size:9px;margin-bottom:4px;">CURRENT TOP 5</div>
          ${_topWeightsBlock(cur.top_5_weights)}
        </div>
        <div class="kpi-card" style="padding:8px;">
          <div class="kpi-label" style="font-size:9px;margin-bottom:4px;">PROPOSED TOP 5</div>
          ${_topWeightsBlock(prop.top_5_weights)}
        </div>
      </div>`;

    const verdict = _verdictCallout(opt);

    const factorBlock = `
      <div style="margin-bottom:12px;">
        <div class="section-header" style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">
          <h4 style="margin:0;">FACTOR EXPOSURE</h4>
          <span style="color:var(--text-dim);font-size:11px;">FF5 + momentum betas</span>
        </div>
        ${_factorBarsSideBySide(cur.factor_exposure, prop.factor_exposure)}
      </div>`;

    const stressBlock = `
      <div style="margin-bottom:12px;">
        <div class="section-header" style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">
          <h4 style="margin:0;">STRESS-TEST LOSSES</h4>
          <span style="color:var(--text-dim);font-size:11px;">historical scenario drawdowns</span>
        </div>
        ${_stressBars(cur.scenario_losses, prop.scenario_losses)}
      </div>`;

    const mcBlock = `
      <div style="margin-bottom:12px;">
        <div class="section-header" style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px;">
          <h4 style="margin:0;">MONTE CARLO SHIFT (1y terminal NAV)</h4>
          <span style="color:var(--text-dim);font-size:11px;">2000 paths · 365d horizon</span>
        </div>
        ${_mcBlock(mcd)}
      </div>`;

    const errBlock = (data.errors && Object.keys(data.errors).length > 0)
      ? `<details style="margin-top:8px;font-size:11px;color:var(--text-dim);">
          <summary style="cursor:pointer;">${Object.keys(data.errors).length} subsystem note(s)</summary>
          <ul style="margin:6px 0 0 16px;padding:0;">
            ${Object.entries(data.errors).map(([k, v]) =>
              `<li><code>${_esc(k)}</code>: ${_esc(v)}</li>`).join("")}
          </ul>
        </details>`
      : "";

    container.innerHTML = `
      ${_formHtml(state.candidate)}
      <div class="whatif-result">
        ${verdict}
        ${kpiGrid}
        ${topWeights}
        ${factorBlock}
        ${stressBlock}
        ${mcBlock}
        <div style="font-size:10px;color:var(--text-dim);margin-top:8px;">
          computed in ${_esc(String(data.elapsed_ms == null ? "—" : data.elapsed_ms))}ms ·
          as of ${_esc(data.as_of || "")}
        </div>
        ${errBlock}
      </div>
    `;
    _wireForm(container, state);
  }

  function _wireForm(container, state) {
    const form = container.querySelector("#whatif-form");
    if (!form) return;
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const fd = new FormData(form);
      const candidate = {
        symbol: String(fd.get("symbol") || "").toUpperCase().trim(),
        market_value: Number(fd.get("market_value") || 0),
        action: String(fd.get("action") || "add"),
      };
      if (!candidate.symbol) return;
      state.candidate = candidate;
      _runWhatIf(container, state);
    });
  }

  function _runWhatIf(container, state) {
    container.innerHTML = _spinner("RUNNING SYNTH WHAT-IF — 5 LENSES");
    const api = _api();
    const body = {
      current_holdings: state.holdings || [],
      candidate: state.candidate,
    };
    const send = (typeof api.post === "function")
      ? api.post(POST_PATH, body)
      : fetch(POST_PATH, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }).then((r) => r.json());
    send
      .then((data) => _renderResult(container, data, state))
      .catch((err) => {
        container.innerHTML = _formHtml(state.candidate)
          + _empty("network error: " + (err && err.message ? err.message : err));
        _wireForm(container, state);
      });
  }

  function renderWhatIf(container, opts) {
    if (!container) return;
    opts = opts || {};
    const state = {
      holdings: Array.isArray(opts.holdings) ? opts.holdings.slice() : null,
      candidate: opts.candidate || { symbol: "", market_value: 20000, action: "add" },
    };

    // If holdings weren't supplied, lazy-load /api/portfolio and use those.
    const renderInitial = () => {
      container.innerHTML = _formHtml(state.candidate) + `
        <div style="padding:20px;text-align:center;color:var(--text-dim);font-size:12px;">
          Enter a candidate above and click <strong>RUN WHAT-IF</strong> to synthesise
          the impact across factor exposure, stress tests, Monte Carlo, optimizer, and ML.
        </div>`;
      _wireForm(container, state);
    };

    if (state.holdings) {
      renderInitial();
      // Auto-run if candidate is fully specified.
      if (state.candidate && state.candidate.symbol) _runWhatIf(container, state);
      return;
    }

    container.innerHTML = _spinner("LOADING CURRENT PORTFOLIO…");
    _api().get(PORTFOLIO_PATH)
      .then((data) => {
        const holdings = (data && Array.isArray(data.holdings)) ? data.holdings : [];
        state.holdings = holdings
          .filter((h) => h && h.symbol && h.market_value != null)  // != null: a real 0 mark isn't "missing"
          .map((h) => ({
            symbol: h.symbol,
            market_value: Number(h.market_value),
            asset_type: h.asset_type,
            shares: h.shares,
            avg_cost: h.avg_cost,
          }));
        renderInitial();
        if (state.candidate && state.candidate.symbol) _runWhatIf(container, state);
      })
      .catch(() => {
        state.holdings = [];
        renderInitial();
      });
  }

  global.renderWhatIf = renderWhatIf;
})(typeof window !== "undefined" ? window : this);
