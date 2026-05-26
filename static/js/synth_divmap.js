/* synth_divmap.js — Divergence Map UI.
 *
 * Exports:
 *
 *   renderDivergenceMap(container, opts = {universe, top_n})
 *       Top: pair filter (multi-select) + top_n slider + refresh button.
 *       Card list: each card shows the two opposed signals as a bidirectional
 *       bar (left for the negative-norm signal, right for the positive),
 *       magnitude badge (colored by intensity), and a human interpretation.
 *
 * The scan endpoint is /api/synth/divergence-map. Custom universes go via
 * POST. Re-renders preserve the user's pair filter + top_n choice.
 *
 * No external libraries — bars are CSS-sized divs, slot into the design system.
 */

(function (global) {
  "use strict";

  const PAIR_LABELS = {
    "insider_form4_vs_smart_money": "Insider Form 4 vs Smart Money",
    "ml_forecast_vs_factor_alpha":  "ML Forecast vs Factor Alpha",
    "narrative_vs_options_pcr":     "Narrative vs Options PCR",
    "congress_vs_smart_money":      "Congress vs Smart Money",
    "reflexivity_vs_narrative_velocity": "Reflexivity vs Narrative Velocity",
  };
  const ALL_PAIRS = Object.keys(PAIR_LABELS);

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _api() {
    if (global.API && typeof global.API.get === "function") return global.API;
    return {
      get: (url) => fetch(url).then((r) => r.json()),
      post: (url, body) => fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
      }).then((r) => r.json()),
    };
  }

  function _emptyCard(msg) {
    return '<div class="empty-state" style="padding:24px;text-align:center;color:var(--text-dim);">' +
           _esc(msg || "no divergences found") + "</div>";
  }

  function _spinner(label) {
    return '<div class="loading" style="padding:24px;text-align:center;color:var(--text-dim);">' +
           '<div class="spinner"></div> ' + _esc(label || "SCANNING UNIVERSE…") + "</div>";
  }

  /** Magnitude → color band. The higher the magnitude (max ~2.0), the
   *  brighter the badge. Below 1.0 we use a muted amber; 1.0-1.5 amber;
   *  1.5+ red. This keeps the "look at this one" cases visually obvious.
   */
  function _magColor(mag) {
    if (mag >= 1.5) return "#ef4444";   // bright red
    if (mag >= 1.2) return "#f97316";   // orange
    if (mag >= 1.0) return "#eab308";   // yellow
    return "#a3a3a3";                   // muted grey
  }

  function _magBgFade(mag) {
    // Faint background tint matching the badge color, very low alpha.
    if (mag >= 1.5) return "rgba(239,68,68,0.08)";
    if (mag >= 1.2) return "rgba(249,115,22,0.06)";
    if (mag >= 1.0) return "rgba(234,179,8,0.05)";
    return "rgba(163,163,163,0.03)";
  }

  /** Render a single divergence row as a card.
   *
   *  Layout (top to bottom):
   *    [SYMBOL]      [PAIR LABEL]      [MAGNITUDE BADGE]
   *    [signal_a label]                    [signal_b label]
   *    ┌────────────────[ZERO]────────────────┐
   *    │ ←—bar for signal_a (negative side)   │   bar for signal_b (positive)—→
   *    └────────────────[ZERO]────────────────┘
   *    interpretation prose
   */
  function _divergenceCard(row) {
    const sym = row.symbol || "";
    const pair = row.pair || "";
    const pairLabel = PAIR_LABELS[pair] || pair;
    const mag = Number(row.magnitude) || 0;
    const color = _magColor(mag);
    const bg = _magBgFade(mag);
    const interp = row.interpretation || "";

    const a = row.signal_a || {};
    const b = row.signal_b || {};
    const aNorm = Math.max(-1, Math.min(1, Number(a.norm) || 0));
    const bNorm = Math.max(-1, Math.min(1, Number(b.norm) || 0));

    // Bars are rendered around a central zero line. Width is |norm| * 50%
    // of the row width — so a norm of ±1 fills the half-track entirely.
    const aWidth = Math.abs(aNorm) * 100;
    const bWidth = Math.abs(bNorm) * 100;
    const aColor = aNorm < 0 ? "#f87171" : "#4ade80";  // green for positive
    const bColor = bNorm < 0 ? "#f87171" : "#4ade80";

    // Left side of the bar shows signal A; right side shows signal B.
    // If a signal's norm is positive, it visually points to the right; if
    // negative, to the left. We place them in two half-tracks separated
    // by a zero line so the opposition is immediately visible.
    const aLeftBar  = aNorm < 0
      ? `<div style="width:${aWidth}%;height:14px;background:${aColor};margin-left:auto;border-radius:2px;"></div>`
      : "";
    const aRightBar = aNorm > 0
      ? `<div style="width:${aWidth}%;height:14px;background:${aColor};border-radius:2px;"></div>`
      : "";
    const bLeftBar  = bNorm < 0
      ? `<div style="width:${bWidth}%;height:14px;background:${bColor};margin-left:auto;border-radius:2px;"></div>`
      : "";
    const bRightBar = bNorm > 0
      ? `<div style="width:${bWidth}%;height:14px;background:${bColor};border-radius:2px;"></div>`
      : "";

    const aLabel = a.interpretation || a.name || "signal A";
    const bLabel = b.interpretation || b.name || "signal B";
    const aVal = a.value == null ? "" : String(a.value);
    const bVal = b.value == null ? "" : String(b.value);

    return `
      <div class="divmap-card" style="
        background:${bg};
        border:1px solid var(--border,#333);
        border-left:3px solid ${color};
        border-radius:6px;
        padding:14px;
        margin-bottom:10px;
        font-size:12px;
      ">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
          <div style="display:flex;align-items:baseline;gap:12px;">
            <span style="font-weight:700;font-size:16px;font-family:var(--font-mono,monospace);">${_esc(sym)}</span>
            <span style="color:var(--text-dim);font-size:11px;letter-spacing:0.5px;">${_esc(pairLabel.toUpperCase())}</span>
          </div>
          <div style="
            background:${color};
            color:#000;
            font-weight:700;
            padding:3px 10px;
            border-radius:3px;
            font-family:var(--font-mono,monospace);
            font-size:11px;
          " title="z-score of disagreement">
            Δ ${mag.toFixed(2)}
          </div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:6px;font-size:10px;color:var(--text-dim);">
          <div style="text-align:right;">${_esc(aLabel)} ${aVal ? '<span style="color:var(--text);">[' + _esc(aVal) + ']</span>' : ''}</div>
          <div style="text-align:left;">${_esc(bLabel)} ${bVal ? '<span style="color:var(--text);">[' + _esc(bVal) + ']</span>' : ''}</div>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1px 1fr;gap:4px;align-items:center;height:18px;background:rgba(255,255,255,0.02);border-radius:3px;padding:2px;">
          <div style="display:flex;justify-content:flex-end;align-items:center;height:100%;padding-right:2px;">
            ${aLeftBar}${aRightBar /* A may be on either side */}
          </div>
          <div style="background:var(--border,#444);height:14px;width:1px;"></div>
          <div style="display:flex;justify-content:flex-start;align-items:center;height:100%;padding-left:2px;">
            ${bLeftBar}${bRightBar}
          </div>
        </div>

        <div style="margin-top:10px;color:var(--text);font-size:12px;line-height:1.4;">
          ${_esc(interp)}
        </div>
      </div>`;
  }

  /** Toolbar markup: pair multi-select, top_n slider, refresh button.
   *  Uses checkbox <input>s for pair filter so the user can quickly
   *  uncheck pairs that aren't interesting; default = all on.
   */
  function _toolbarHtml(state) {
    const pairChips = ALL_PAIRS.map((p) => {
      const checked = state.pairs.has(p) ? "checked" : "";
      return `
        <label class="divmap-chip" style="
          display:inline-flex;align-items:center;gap:4px;
          padding:3px 8px;border:1px solid var(--border,#333);
          border-radius:12px;font-size:10px;cursor:pointer;
          background:${state.pairs.has(p) ? 'rgba(74,222,128,0.08)' : 'transparent'};
          color:${state.pairs.has(p) ? 'var(--text)' : 'var(--text-dim)'};
        ">
          <input type="checkbox" data-divmap-pair="${_esc(p)}" ${checked}
                 style="margin:0;accent-color:var(--accent,#4ade80);" />
          ${_esc(PAIR_LABELS[p])}
        </label>`;
    }).join("");

    return `
      <div class="divmap-toolbar" style="
        display:flex;flex-direction:column;gap:8px;
        padding:10px;background:rgba(255,255,255,0.02);
        border:1px solid var(--border,#333);border-radius:4px;
        margin-bottom:12px;
      ">
        <div style="display:flex;flex-wrap:wrap;gap:4px;align-items:center;">
          <span style="color:var(--text-dim);font-size:10px;letter-spacing:0.5px;margin-right:4px;">PAIRS:</span>
          ${pairChips}
        </div>
        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">
          <label style="display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text-dim);">
            TOP <span data-divmap-topn-display style="color:var(--text);font-family:var(--font-mono,monospace);font-weight:600;min-width:24px;text-align:right;">${state.topN}</span>
            <input type="range" min="5" max="50" step="1" value="${state.topN}"
                   data-divmap-topn
                   style="width:140px;accent-color:var(--accent,#4ade80);" />
          </label>
          <button data-divmap-refresh class="btn btn-sm" style="
            padding:4px 12px;border:1px solid var(--border,#333);
            background:transparent;color:var(--text);border-radius:3px;
            cursor:pointer;font-size:11px;
          ">REFRESH</button>
          <span data-divmap-status style="color:var(--text-dim);font-size:10px;margin-left:auto;"></span>
        </div>
      </div>`;
  }

  function _filterRows(rows, state) {
    return rows.filter((r) => state.pairs.has(r.pair));
  }

  function _renderResults(container, state) {
    const list = container.querySelector("[data-divmap-list]");
    const statusEl = container.querySelector("[data-divmap-status]");
    if (!list) return;
    const filtered = _filterRows(state.allRows, state).slice(0, state.topN);
    if (filtered.length === 0) {
      list.innerHTML = _emptyCard(
        state.allRows.length === 0
          ? "No divergences found in this scan."
          : "No divergences match the selected pairs."
      );
    } else {
      list.innerHTML = filtered.map(_divergenceCard).join("");
    }
    if (statusEl) {
      const total = state.allRows.length;
      const asOf = state.lastScan ? new Date(state.lastScan).toLocaleString() : "";
      const universe = state.universeLabel || "default";
      statusEl.innerHTML = `${_esc(String(filtered.length))}/${_esc(String(total))} shown · universe=${_esc(universe)} · ${_esc(asOf)}`;
    }
  }

  function _bindToolbar(container, state) {
    // Pair toggles
    container.querySelectorAll("[data-divmap-pair]").forEach((cb) => {
      cb.addEventListener("change", (e) => {
        const p = e.target.getAttribute("data-divmap-pair");
        if (e.target.checked) state.pairs.add(p);
        else state.pairs.delete(p);
        // Refresh chip backgrounds
        const chip = e.target.closest(".divmap-chip");
        if (chip) {
          chip.style.background = e.target.checked ? "rgba(74,222,128,0.08)" : "transparent";
          chip.style.color = e.target.checked ? "var(--text)" : "var(--text-dim)";
        }
        _renderResults(container, state);
      });
    });

    // Top-N slider
    const slider = container.querySelector("[data-divmap-topn]");
    const display = container.querySelector("[data-divmap-topn-display]");
    if (slider) {
      slider.addEventListener("input", (e) => {
        state.topN = Number(e.target.value) || 20;
        if (display) display.textContent = String(state.topN);
        _renderResults(container, state);
      });
    }

    // Refresh button — hits the API again rather than filtering client-side.
    const refreshBtn = container.querySelector("[data-divmap-refresh]");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", () => _fetchAndRender(container, state, true));
    }
  }

  function _fetchAndRender(container, state, force) {
    const list = container.querySelector("[data-divmap-list]");
    if (list) list.innerHTML = _spinner("SCANNING UNIVERSE…");

    const params = new URLSearchParams();
    if (state.universe && typeof state.universe === "string") {
      params.set("universe", state.universe);
    } else {
      params.set("universe", "sp500_top100");
    }
    // Always pull the max (so the slider filters in-browser without re-hitting
    // the API). Cap matches the server's hard ceiling.
    params.set("top_n", String(Math.max(state.topN, 50)));
    if (force) params.set("_t", String(Date.now()));

    const api = _api();
    const isCustom = Array.isArray(state.universe);

    let p;
    if (isCustom) {
      p = (typeof api.post === "function"
        ? api.post("/api/synth/divergence-map", { universe: state.universe, top_n: Math.max(state.topN, 50) })
        : fetch("/api/synth/divergence-map", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ universe: state.universe, top_n: Math.max(state.topN, 50) }),
          }).then((r) => r.json()));
    } else {
      p = api.get("/api/synth/divergence-map?" + params.toString());
    }

    p.then((data) => {
      if (!data || data.error) {
        if (list) list.innerHTML = _emptyCard((data && data.error) || "no data");
        return;
      }
      state.allRows = Array.isArray(data.divergences) ? data.divergences : [];
      state.lastScan = data.as_of || new Date().toISOString();
      state.universeLabel = data.universe || state.universeLabel;
      _renderResults(container, state);
    }).catch((err) => {
      if (list) list.innerHTML = _emptyCard("network error: " + (err && err.message ? err.message : err));
    });
  }

  function renderDivergenceMap(container, opts) {
    if (!container) return;
    opts = opts || {};
    const state = {
      universe: opts.universe || "sp500_top100",
      universeLabel: typeof opts.universe === "string" ? opts.universe : "custom",
      topN: opts.top_n != null ? Number(opts.top_n) : 20,
      pairs: new Set(opts.pairs || ALL_PAIRS),
      allRows: [],
      lastScan: null,
    };

    container.innerHTML = `
      <div class="divmap-wrap">
        <div class="section-header" style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;">
          <h3 style="margin:0;">DIVERGENCE MAP</h3>
          <span style="color:var(--text-dim);font-size:11px;">opposed signals · top picks</span>
        </div>
        ${_toolbarHtml(state)}
        <div data-divmap-list></div>
      </div>`;

    _bindToolbar(container, state);
    _fetchAndRender(container, state, false);
  }

  global.renderDivergenceMap = renderDivergenceMap;
})(typeof window !== "undefined" ? window : this);
