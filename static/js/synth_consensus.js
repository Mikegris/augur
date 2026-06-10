/* synth_consensus.js — Cross-Source Consensus Score UI.
 *
 * Exports a single helper:
 *
 *   renderConsensus(container, symbol)
 *
 * Fetches /api/synth/consensus/<symbol> and paints:
 *   - one big colour-coded score number with the categorical label
 *   - per-contributor horizontal bars (red = bearish, green = bullish),
 *     sized in proportion to |weight * normalized| so the most-influential
 *     signals are visually loudest
 *   - hover tooltip on each bar exposes weight, static weight, hit-rate,
 *     and number of scored calls used to derive the dynamic weight
 *   - a "MISSING" panel for any contributors that errored or returned
 *     no data, so the user can tell "score is low confidence because half
 *     the inputs are silent" from "score is low because all inputs agree
 *     it's bearish".
 *
 * No external libs.  Bars are plain <div>s scaled in %.
 */

(function (global) {
  "use strict";

  // ── color palette ───────────────────────────────────────────────────────
  // Borrows AUGUR's existing CSS variables, with safe hex fallbacks so the
  // panel still renders if the variables aren't in scope.
  const COL_BULL  = "var(--accent, #4ade80)";
  const COL_BEAR  = "var(--danger, #f87171)";
  const COL_NEUT  = "var(--text-dim, #9ca3af)";

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _fmt(n, digits) {
    if (n == null || isNaN(n)) return "—";
    return Number(n).toFixed(digits == null ? 2 : digits);
  }

  function _fmtPct(r, digits) {
    if (r == null || isNaN(r)) return "—";
    return (Number(r) * 100).toFixed(digits == null ? 1 : digits) + "%";
  }

  function _api() {
    return (global.API && typeof global.API.get === "function")
      ? global.API
      : { get: (url) => fetch(url).then((r) => r.json()) };
  }

  function _empty(msg) {
    return '<div class="empty-state"><span style="color:var(--text-dim)">' +
           _esc(msg || "no data") + '</span></div>';
  }

  function _spinner(label) {
    return '<div class="loading"><div class="spinner"></div> ' +
           _esc(label || "LOADING…") + '</div>';
  }

  function _scoreColor(score) {
    if (score == null) return COL_NEUT;
    if (score >= 60)   return COL_BULL;
    if (score <= 40)   return COL_BEAR;
    return COL_NEUT;
  }

  function _labelColor(label) {
    const L = String(label || "").toUpperCase();
    if (L.indexOf("BULL") >= 0) return COL_BULL;
    if (L.indexOf("BEAR") >= 0) return COL_BEAR;
    return COL_NEUT;
  }

  /** Map a contributor's normalized value (in [-1,+1]) to a color. */
  function _barColor(n) {
    if (n == null) return COL_NEUT;
    if (n >  0.05) return COL_BULL;
    if (n < -0.05) return COL_BEAR;
    return COL_NEUT;
  }

  /** Build the central-anchored bar row for one contributor. */
  function _contributorRow(c, maxAbsContrib) {
    const norm = (c.normalized == null) ? 0 : Number(c.normalized);
    const weight = c.weight == null ? 0 : Number(c.weight);
    const contrib = weight * norm;
    const pct = maxAbsContrib > 0
      ? Math.min(100, (Math.abs(contrib) / maxAbsContrib) * 100)
      : 0;
    const isPos = contrib >= 0;
    const color = _barColor(norm);
    const leftBar  = !isPos
      ? `<div style="width:${pct}%;height:13px;background:${color};margin-left:auto;border-radius:2px;"></div>`
      : "";
    const rightBar = isPos
      ? `<div style="width:${pct}%;height:13px;background:${color};border-radius:2px;"></div>`
      : "";

    const valTxt = (c.value == null) ? "—" : String(c.value);
    const hrTxt = (c.hit_rate == null)
      ? "(no live track record)"
      : `hit-rate ${_fmtPct(c.hit_rate, 0)} over ${c.n_calls} scored calls`;
    const wTxt = `weight ${_fmt(weight, 3)}` +
                 (c.static_weight != null ? ` (static ${_fmt(c.static_weight, 2)})` : "");
    const tip = `${c.name}\nvalue: ${valTxt}\nnormalized: ${_fmt(norm, 3)}\n${wTxt}\n${hrTxt}`;

    return `
      <div title="${_esc(tip)}"
           style="display:grid;grid-template-columns:140px 1fr 1px 1fr 78px;
                  align-items:center;gap:6px;padding:3px 0;font-size:11px;">
        <div style="text-align:right;color:var(--text-dim);font-family:var(--font-mono,monospace);">
          ${_esc(c.name)}
        </div>
        <div style="display:flex;justify-content:flex-end;">${leftBar}</div>
        <div style="background:var(--border,#333);height:18px;"></div>
        <div style="display:flex;justify-content:flex-start;">${rightBar}</div>
        <div style="font-family:var(--font-mono,monospace);text-align:right;">
          <span style="color:${color};font-weight:600;">${_esc(_fmt(norm, 2))}</span>
        </div>
      </div>`;
  }

  function _missingBlock(missing) {
    if (!Array.isArray(missing) || missing.length === 0) return "";
    const chips = missing.map((m) =>
      `<span style="display:inline-block;background:var(--bg-dim,#1f2937);
                     color:var(--text-dim);font-family:var(--font-mono,monospace);
                     font-size:10px;padding:2px 6px;margin:2px;border-radius:3px;">
         ${_esc(m)}
       </span>`).join("");
    return `
      <details style="margin-top:10px;font-size:11px;">
        <summary style="cursor:pointer;color:var(--text-dim);">
          MISSING (${missing.length}) — contributors that returned no data
        </summary>
        <div style="margin-top:4px;">${chips}</div>
      </details>`;
  }

  function _renderResult(container, data, symbol) {
    if (!container) return;
    if (!data || data.error) {
      container.innerHTML = _empty(
        (data && data.error) ? data.error : "consensus unavailable");
      return;
    }
    const score = data.score;
    const label = data.label || "—";
    const n = data.n_contributors || 0;
    const contribs = Array.isArray(data.contributors) ? data.contributors : [];

    // Compute max |weight * normalized| for scale.
    let maxAbs = 0.001;
    for (const c of contribs) {
      const w = (c.weight == null) ? 0 : Number(c.weight);
      const v = (c.normalized == null) ? 0 : Number(c.normalized);
      const a = Math.abs(w * v);
      if (a > maxAbs) maxAbs = a;
    }

    const rowsHtml = contribs.map((c) => _contributorRow(c, maxAbs)).join("");
    const scoreTxt = (score == null) ? "—" : Number(score).toFixed(1);
    const scoreCol = _scoreColor(score);
    const labelCol = _labelColor(label);

    container.innerHTML = `
      <div class="consensus-panel">
        <div class="section-header"
             style="display:flex;align-items:baseline;justify-content:space-between;">
          <h3 style="margin:0;">CONSENSUS — ${_esc(String(symbol).toUpperCase())}</h3>
          <span style="color:var(--text-dim);font-size:11px;">
            ${_esc(n)} contributors · ${_esc(data.as_of || "")}
          </span>
        </div>
        <div style="display:flex;align-items:center;gap:18px;margin:14px 0;">
          <div style="font-family:var(--font-mono,monospace);
                      font-size:54px;font-weight:700;color:${scoreCol};
                      line-height:1;">
            ${_esc(scoreTxt)}
          </div>
          <div>
            <div style="display:inline-block;padding:4px 12px;border-radius:3px;
                        background:${labelCol};color:#0b0b0b;
                        font-family:var(--font-mono,monospace);font-weight:700;
                        font-size:12px;letter-spacing:0.05em;">
              ${_esc(label)}
            </div>
            <div style="margin-top:6px;color:var(--text-dim);font-size:11px;">
              0 = max bearish · 50 = neutral · 100 = max bullish
            </div>
          </div>
        </div>
        <div class="consensus-contributors">
          ${rowsHtml || _empty("no contributors produced data")}
        </div>
        ${_missingBlock(data.missing)}
        <div style="margin-top:8px;color:var(--text-dim);font-size:10px;">
          Bars are anchored at zero; width scales with each signal's
          |weight × normalized|.  Hover for full attribution.
        </div>
      </div>`;
  }

  function renderConsensus(container, symbol) {
    if (!container) return;
    if (!symbol) {
      container.innerHTML = _empty("no symbol");
      return;
    }
    container.innerHTML = _spinner(
      `BLENDING SIGNALS FOR ${String(symbol).toUpperCase()}…`);
    const url = `/api/synth/consensus/${encodeURIComponent(symbol)}`;
    _api().get(url)
      .then((data) => _renderResult(container, data, symbol))
      .catch((err) => {
        container.innerHTML = _empty(
          "network error: " + (err && err.message ? err.message : err));
      });
  }

  global.renderConsensus = renderConsensus;
})(typeof window !== "undefined" ? window : this);
