/* synth_peerdiv.js — Peer Divergence Detector UI.
 *
 * One exported helper:
 *
 *     renderPeerDiv(container, symbol [, opts])
 *
 *         container    DOM element to render into
 *         symbol       primary ticker (the one being compared against peers)
 *         opts         { n: integer peer count, default 5 }
 *
 * The renderer fetches /api/synth/peerdiv/<symbol>?n=<n> and renders:
 *   - a peers strip (chips listing each peer ticker)
 *   - a "TOP DIVERGENCES" callout block (top-5 by |z|)
 *   - a wide divergence matrix (rows = metrics, cols = sym | peer1..N |
 *     peer_median | z). Rows sorted by |z_score| desc, so the most
 *     interesting metric is on top.
 *
 * No external dependencies — all styling uses the AUGUR terminal CSS tokens
 * (--bg-panel, --border, --green, --red, --amber, --text-primary, etc.).
 */

(function (global) {
  "use strict";

  const STYLE_ID = "synth-peerdiv-style";
  const STYLE_CSS = `
    .pdv-wrap { font-family: var(--font-mono); color: var(--text-primary); font-size: 12px; }
    .pdv-head { display: flex; align-items: baseline; justify-content: space-between; margin-bottom: 8px; }
    .pdv-head h3 { margin: 0; letter-spacing: 1px; }
    .pdv-head .pdv-sub { color: var(--text-dim); font-size: 10px; }
    .pdv-peers { display: flex; flex-wrap: wrap; gap: 4px; margin: 6px 0 12px; }
    .pdv-chip {
      background: var(--bg-panel);
      border: 1px solid var(--border);
      padding: 2px 8px;
      font-size: 11px;
      color: var(--text-secondary);
      letter-spacing: 0.5px;
    }
    .pdv-chip.pdv-anchor { background: var(--bg-elevated, var(--bg-panel)); color: var(--text-primary); border-color: var(--text-primary); }
    .pdv-section-title {
      font-size: 10px;
      letter-spacing: 1.5px;
      color: var(--text-dim);
      margin: 12px 0 4px;
      text-transform: uppercase;
    }
    .pdv-top {
      background: var(--bg-elevated, var(--bg-panel));
      border: 1px solid var(--border);
      padding: 8px 10px;
      margin-bottom: 12px;
    }
    .pdv-top-row {
      display: grid;
      grid-template-columns: 200px 60px 1fr;
      gap: 8px;
      padding: 3px 0;
      align-items: baseline;
      border-bottom: 1px dashed var(--border);
    }
    .pdv-top-row:last-child { border-bottom: 0; }
    .pdv-top-metric { font-weight: 500; }
    .pdv-top-z { text-align: right; font-variant-numeric: tabular-nums; }
    .pdv-top-z.pdv-pos { color: var(--green); }
    .pdv-top-z.pdv-neg { color: var(--red); }
    .pdv-top-interp { color: var(--text-secondary); font-size: 11px; }

    .pdv-table-wrap { overflow-x: auto; border: 1px solid var(--border); }
    table.pdv-matrix {
      border-collapse: collapse;
      width: 100%;
      font-size: 11px;
    }
    table.pdv-matrix th, table.pdv-matrix td {
      padding: 4px 8px;
      text-align: right;
      border-bottom: 1px solid var(--border);
      white-space: nowrap;
      font-variant-numeric: tabular-nums;
    }
    table.pdv-matrix th {
      text-align: right;
      color: var(--text-dim);
      font-weight: 500;
      letter-spacing: 0.5px;
      background: var(--bg-panel);
      position: sticky;
      top: 0;
    }
    table.pdv-matrix th.pdv-metric-col, table.pdv-matrix td.pdv-metric-col {
      text-align: left;
      color: var(--text-primary);
    }
    table.pdv-matrix td.pdv-anchor-cell { color: var(--text-primary); font-weight: 500; background: var(--bg-elevated, transparent); }
    table.pdv-matrix td.pdv-median-cell { color: var(--text-dim); }
    table.pdv-matrix td.pdv-z-cell { font-weight: 500; }
    table.pdv-matrix td.pdv-z-pos { color: var(--green); }
    table.pdv-matrix td.pdv-z-neg { color: var(--red); }
    table.pdv-matrix td.pdv-z-strong-pos { color: var(--green); background: rgba(0,160,80,0.10); }
    table.pdv-matrix td.pdv-z-strong-neg { color: var(--red); background: rgba(200,40,40,0.10); }
    table.pdv-matrix td.pdv-null { color: var(--text-dim); }
    .pdv-empty {
      padding: 16px;
      color: var(--text-dim);
      border: 1px dashed var(--border);
      text-align: center;
    }
    .pdv-loading { padding: 16px; color: var(--text-secondary); }
    .pdv-err { padding: 12px; color: var(--red); border: 1px solid var(--red); }
    .pdv-foot {
      color: var(--text-dim);
      font-size: 10px;
      margin-top: 8px;
      display: flex; justify-content: space-between;
    }
  `;

  function _ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const s = document.createElement("style");
    s.id = STYLE_ID;
    s.textContent = STYLE_CSS;
    document.head.appendChild(s);
  }

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  /** Smart number formatter — picks decimals based on magnitude.
   *  - |x| >= 1000  -> integer
   *  - |x| >= 10    -> 1 decimal
   *  - otherwise    -> 2 decimals
   *  Returns "—" for null/undefined/NaN.
   */
  function _fmt(x) {
    if (x == null || (typeof x === "number" && isNaN(x))) return "—";
    const n = Number(x);
    if (!isFinite(n)) return "—";
    const abs = Math.abs(n);
    if (abs >= 1000) return n.toFixed(0);
    if (abs >= 10) return n.toFixed(1);
    if (abs >= 0.01 || abs === 0) return n.toFixed(2);
    return n.toFixed(3);
  }

  function _fmtZ(z) {
    if (z == null || (typeof z === "number" && isNaN(z))) return "—";
    const n = Number(z);
    if (!isFinite(n)) return "—";
    return (n >= 0 ? "+" : "") + n.toFixed(2);
  }

  function _api() {
    return (global.API && typeof global.API.get === "function")
      ? global.API
      : { get: (url) => fetch(url).then((r) => r.json()) };
  }

  function _spinner(label) {
    return `<div class="pdv-loading">⟳ ${_esc(label || "LOADING…")}</div>`;
  }

  /** Bucket a z-score into a CSS class for the matrix cell. */
  function _zClass(z) {
    if (z == null) return "pdv-null";
    const a = Math.abs(z);
    if (a >= 1.5) return z > 0 ? "pdv-z-strong-pos" : "pdv-z-strong-neg";
    if (a >= 0.5) return z > 0 ? "pdv-z-pos" : "pdv-z-neg";
    return "";
  }

  function _renderPeersChips(symbol, peers) {
    const sym = String(symbol || "").toUpperCase();
    const chips = [`<span class="pdv-chip pdv-anchor">${_esc(sym)}</span>`].concat(
      (peers || []).map((p) => `<span class="pdv-chip">${_esc(String(p).toUpperCase())}</span>`)
    );
    return `<div class="pdv-peers">${chips.join("")}</div>`;
  }

  function _renderTopBlock(top, sym) {
    if (!Array.isArray(top) || top.length === 0) {
      return "";
    }
    const rows = top.map((d) => {
      const z = Number(d.z_score);
      const zCls = isFinite(z) ? (z >= 0 ? "pdv-pos" : "pdv-neg") : "";
      return `
        <div class="pdv-top-row">
          <div class="pdv-top-metric">${_esc(d.metric)}</div>
          <div class="pdv-top-z ${zCls}">${_fmtZ(d.z_score)}</div>
          <div class="pdv-top-interp">${_esc(d.interpretation || "")}</div>
        </div>`;
    }).join("");
    return `
      <div class="pdv-section-title">TOP DIVERGENCES — WHERE ${_esc(sym)} BREAKS FROM PEERS</div>
      <div class="pdv-top">${rows}</div>`;
  }

  /** Sort metric names by |z_score| desc, missing z's go last. */
  function _sortMetricsByZ(metrics) {
    const keys = Object.keys(metrics || {});
    keys.sort((a, b) => {
      const za = metrics[a] && metrics[a].z_score;
      const zb = metrics[b] && metrics[b].z_score;
      const sa = za == null ? -1 : Math.abs(za);
      const sb = zb == null ? -1 : Math.abs(zb);
      if (sa === sb) return a.localeCompare(b);
      return sb - sa;
    });
    return keys;
  }

  function _renderMatrix(data) {
    const sym = String(data.symbol || "").toUpperCase();
    const peers = (data.peers || []).map((p) => String(p).toUpperCase());
    const metrics = data.metrics || {};
    const descriptions = data.metric_descriptions || {};
    const order = _sortMetricsByZ(metrics);
    if (order.length === 0) {
      return `<div class="pdv-empty">no metrics returned</div>`;
    }

    const headerCells = [
      `<th class="pdv-metric-col">METRIC</th>`,
      `<th>${_esc(sym)}</th>`,
    ].concat(
      peers.map((p) => `<th>${_esc(p)}</th>`)
    ).concat([
      `<th>peer med</th>`,
      `<th>z</th>`,
    ]);

    const rows = order.map((metric) => {
      const block = metrics[metric] || {};
      const sv = block[sym];
      const peerVals = peers.map((p) => block[p]);
      const med = block.peer_median;
      const z = block.z_score;
      const zNum = z == null ? null : Number(z);
      const zCls = _zClass(zNum);
      const tip = descriptions[metric] ? `title="${_esc(descriptions[metric])}"` : "";

      const symCell = `<td class="pdv-anchor-cell">${_fmt(sv)}</td>`;
      const peerCells = peerVals.map((v) =>
        v == null ? `<td class="pdv-null">—</td>` : `<td>${_fmt(v)}</td>`
      ).join("");
      const medCell = `<td class="pdv-median-cell">${_fmt(med)}</td>`;
      const zCell = `<td class="pdv-z-cell ${zCls}">${_fmtZ(zNum)}</td>`;

      return `<tr><td class="pdv-metric-col" ${tip}>${_esc(metric)}</td>${symCell}${peerCells}${medCell}${zCell}</tr>`;
    }).join("");

    return `
      <div class="pdv-section-title">DIVERGENCE MATRIX (rows sorted by |z|)</div>
      <div class="pdv-table-wrap">
        <table class="pdv-matrix">
          <thead><tr>${headerCells.join("")}</tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>`;
  }

  function _renderEnvelope(container, data) {
    _ensureStyle();
    if (!data || typeof data !== "object") {
      container.innerHTML = `<div class="pdv-err">empty response</div>`;
      return;
    }
    if (data.error && !data.peers) {
      container.innerHTML = `<div class="pdv-err">${_esc(data.error)}</div>`;
      return;
    }
    const sym = String(data.symbol || "").toUpperCase();
    const nPeers = data.n_peers || (data.peers ? data.peers.length : 0);
    const asOf = data.as_of || "";
    const errCount = (data.errors || []).length;

    const html = `
      <div class="pdv-wrap">
        <div class="pdv-head">
          <h3>PEER DIVERGENCE — ${_esc(sym)}</h3>
          <span class="pdv-sub">${_esc(nPeers)} peers · cross-source synthesis</span>
        </div>
        ${_renderPeersChips(sym, data.peers || [])}
        ${_renderTopBlock(data.top_divergences || [], sym)}
        ${_renderMatrix(data)}
        <div class="pdv-foot">
          <span>as of ${_esc(asOf)}</span>
          ${errCount > 0 ? `<span>${errCount} per-fetch warning${errCount === 1 ? "" : "s"}</span>` : ""}
        </div>
      </div>`;
    container.innerHTML = html;
  }

  function renderPeerDiv(container, symbol, opts) {
    if (!container) return;
    _ensureStyle();
    if (!symbol) {
      container.innerHTML = `<div class="pdv-empty">no symbol</div>`;
      return;
    }
    const sym = String(symbol).toUpperCase();
    const n = (opts && Number.isFinite(Number(opts.n))) ? Number(opts.n) : 5;
    container.innerHTML = _spinner(`COMPUTING PEER DIVERGENCE FOR ${sym}…`);

    const url = `/api/synth/peerdiv/${encodeURIComponent(sym)}?n=${encodeURIComponent(n)}`;
    _api().get(url)
      .then((data) => _renderEnvelope(container, data))
      .catch((err) => {
        const msg = (err && err.message) ? err.message : String(err);
        container.innerHTML = `<div class="pdv-err">network error: ${_esc(msg)}</div>`;
      });
  }

  // Expose
  global.renderPeerDiv = renderPeerDiv;
})(typeof window !== "undefined" ? window : this);
