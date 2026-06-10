/* synth_sectorflow.js — Sector-rotation multi-signal heatmap UI.
 *
 * Exports a single helper:
 *
 *   renderSectorFlow(containerEl)
 *       Fetches /api/synth/sectorflow and renders an 11-row table plus a
 *       leader / laggard summary panel on the side. Every cell is colour-
 *       coded on a divergent red→neutral→green scale; any column header
 *       can be clicked to sort by it.
 *
 * No external dependencies — table + bars are pure DOM. Designed to slot
 * into a full-width `view-` container.
 */

(function (global) {
  "use strict";

  // ── helpers ────────────────────────────────────────────────────────

  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  function _api() {
    return (global.API && typeof global.API.get === "function")
      ? global.API
      : { get: (url) => fetch(url).then((r) => r.json()) };
  }

  function _fmtPct(v, digits) {
    if (v == null || isNaN(v)) return "—";
    const n = Number(v);
    const s = n > 0 ? "+" : "";
    return `${s}${n.toFixed(digits == null ? 2 : digits)}%`;
  }

  function _fmtNum(v, digits) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toFixed(digits == null ? 2 : digits);
  }

  function _fmtCompactUsd(v) {
    if (v == null || isNaN(v)) return "—";
    const n = Number(v);
    const abs = Math.abs(n);
    const sign = n < 0 ? "-" : (n > 0 ? "+" : "");
    if (abs >= 1e9) return `${sign}$${(abs / 1e9).toFixed(1)}B`;
    if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(1)}M`;
    if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}K`;
    return `${sign}$${abs.toFixed(0)}`;
  }

  // Divergent colour scale. `v` is the raw value, `scale` is the half-width
  // of the [-scale, +scale] band that saturates the colour. Returns a CSS
  // background colour string.
  function _divergentBg(v, scale) {
    if (v == null || isNaN(v)) return "transparent";
    const n = Number(v);
    const clamped = Math.max(-1, Math.min(1, n / scale));
    if (clamped === 0) return "transparent";
    // Green for positive, red for negative — match the rest of AUGUR.
    if (clamped > 0) {
      const a = (0.10 + 0.55 * clamped).toFixed(2);
      return `rgba(74, 222, 128, ${a})`;
    } else {
      const a = (0.10 + 0.55 * -clamped).toFixed(2);
      return `rgba(248, 113, 113, ${a})`;
    }
  }

  // Color of cell text for contrast over coloured background. Light cells
  // get the default; saturated cells get a dark text.
  function _divergentFg(v, scale) {
    if (v == null || isNaN(v)) return "inherit";
    const intensity = Math.min(1, Math.abs(Number(v) / scale));
    return intensity > 0.55 ? "#0b0d10" : "inherit";
  }

  function _phaseColor(phase) {
    if (!phase) return "var(--text-dim)";
    const p = String(phase).toUpperCase();
    if (p.includes("BULL")) return "var(--accent, #4ade80)";
    if (p.includes("BEAR")) return "var(--danger, #f87171)";
    if (p.includes("CONSENSUS") || p.includes("EUPHOR")) return "#fbbf24";
    return "var(--text-dim, #888)";
  }

  function _emptyCard(msg) {
    return `<div class="empty-state" style="padding:24px;color:var(--text-dim,#888);">${_esc(msg || "no data")}</div>`;
  }

  function _spinner(label) {
    return `<div class="loading"><div class="spinner"></div> ${_esc(label || "LOADING…")}</div>`;
  }

  // ── columns ────────────────────────────────────────────────────────

  // Each column spec drives both the header and the per-row render. `scale`
  // is the divergent-scale half-width; the `accessor` returns the raw
  // numeric value used for sorting and colouring; `render` is the cell
  // HTML; `sortDesc` defaults to true (bigger = first).
  const COLUMNS = [
    {
      key: "sector",
      label: "SECTOR",
      sortable: true,
      sortDesc: false,
      accessor: (r) => r.sector,
      render: (r) => `
        <div style="display:flex;flex-direction:column;line-height:1.15;">
          <span style="font-weight:600;">${_esc(r.sector)}</span>
          <span style="color:var(--text-dim);font-size:9px;letter-spacing:.5px;">${_esc(r.etf)}</span>
        </div>`,
      scale: null,
    },
    {
      key: "price_1d_pct",
      label: "1D",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.price_1d_pct,
      render: (r) => _fmtPct(r.price_1d_pct),
      scale: 1.5,
    },
    {
      key: "price_5d_pct",
      label: "5D",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.price_5d_pct,
      render: (r) => _fmtPct(r.price_5d_pct),
      scale: 3.0,
    },
    {
      key: "price_1mo_pct",
      label: "1MO",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.price_1mo_pct,
      render: (r) => _fmtPct(r.price_1mo_pct),
      scale: 8.0,
    },
    {
      key: "rs_vs_spy_1mo",
      label: "RS 1MO",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.rs_vs_spy_1mo,
      render: (r) => _fmtPct(r.rs_vs_spy_1mo),
      scale: 5.0,
    },
    {
      key: "narrative_phase",
      label: "NARRATIVE",
      sortable: true,
      sortDesc: false,
      accessor: (r) => r.narrative_phase || "",
      render: (r) => `
        <span style="color:${_phaseColor(r.narrative_phase)};font-size:10px;letter-spacing:.5px;">
          ${_esc(r.narrative_phase || "—")}
        </span>
        <span style="color:var(--text-dim);font-size:9px;display:block;">v=${_fmtNum(r.narrative_velocity, 2)}</span>`,
      scale: null,
    },
    {
      key: "insider_buy_ratio_30d",
      label: "INSIDERS",
      sortable: true,
      sortDesc: true,
      // Centre at 0.5 so colour goes red below / green above
      accessor: (r) => r.insider_buy_ratio_30d,
      colorVal: (r) => r.insider_buy_ratio_30d == null ? null : (r.insider_buy_ratio_30d - 0.5),
      render: (r) => r.insider_buy_ratio_30d == null ? "—" : (r.insider_buy_ratio_30d * 100).toFixed(0) + "%",
      scale: 0.4,
    },
    {
      key: "congress_net_30d_usd",
      label: "CONGRESS",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.congress_net_30d_usd,
      // log10 the dollar amount for the colour scale so $1M and $50M don't
      // both saturate the cell.
      colorVal: (r) => {
        const v = r.congress_net_30d_usd;
        if (v == null || v === 0) return null;
        const sign = v > 0 ? 1 : -1;
        return sign * Math.log10(Math.max(1, Math.abs(v)));
      },
      render: (r) => _fmtCompactUsd(r.congress_net_30d_usd),
      scale: 6.0, // log-10 units
    },
    {
      key: "reddit_mention_delta_pct",
      label: "REDDIT",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.reddit_mention_delta_pct,
      render: (r) => _fmtPct(r.reddit_mention_delta_pct, 0),
      scale: 60.0,
    },
    {
      key: "hn_polarity",
      label: "HN",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.hn_polarity,
      render: (r) => _fmtNum(r.hn_polarity, 2),
      scale: 1.0,
    },
    {
      key: "wiki_attention_z",
      label: "WIKI",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.wiki_attention_z,
      render: (r) => _fmtNum(r.wiki_attention_z, 2),
      scale: 2.0,
    },
    {
      key: "options_pcr",
      label: "PCR",
      sortable: true,
      sortDesc: false, // lower PCR = more bullish, so sort asc by default
      accessor: (r) => r.options_pcr,
      // Centre at 0.6 (typical baseline), invert so low PCR = green
      colorVal: (r) => r.options_pcr == null ? null : (0.6 - r.options_pcr),
      render: (r) => _fmtNum(r.options_pcr, 2),
      scale: 0.3,
    },
    {
      key: "composite_flow_score",
      label: "COMPOSITE",
      sortable: true,
      sortDesc: true,
      accessor: (r) => r.composite_flow_score,
      render: (r) => {
        const v = r.composite_flow_score;
        if (v == null) return "—";
        const sign = v > 0 ? "+" : "";
        return `<strong>${sign}${Number(v).toFixed(2)}</strong>`;
      },
      scale: 2.0,
    },
  ];

  // ── render ────────────────────────────────────────────────────────

  function _topContributors(row) {
    // Pick the biggest absolute drivers in the composite for the side panel.
    const items = [
      { key: "1mo RS",    val: row.rs_vs_spy_1mo,     scale: 5.0,   suffix: "%" },
      { key: "5d RS",     val: row.rs_vs_spy_5d,      scale: 2.0,   suffix: "%" },
      { key: "Narrative", val: row.narrative_velocity, scale: 2.0,   suffix: "" },
      { key: "Insiders",  val: row.insider_buy_ratio_30d == null ? null : (row.insider_buy_ratio_30d - 0.5),
                          scale: 0.4, suffix: "" },
      { key: "Congress",  val: row.congress_net_30d_usd == null ? null
                                : (row.congress_net_30d_usd > 0 ? 1 : -1) *
                                  Math.log10(Math.max(1, Math.abs(row.congress_net_30d_usd))),
                          scale: 6.0, suffix: "" },
      { key: "Reddit",    val: row.reddit_mention_delta_pct, scale: 60.0, suffix: "%" },
      { key: "Wiki",      val: row.wiki_attention_z,  scale: 2.0,   suffix: "σ" },
      { key: "HN",        val: row.hn_polarity,       scale: 1.0,   suffix: "" },
      { key: "PCR",       val: row.options_pcr == null ? null : (0.6 - row.options_pcr),
                          scale: 0.3, suffix: "" },
    ];
    return items
      .filter((x) => x.val != null && !isNaN(x.val))
      .map((x) => ({ ...x, norm: Math.abs(x.val / x.scale) }))
      .sort((a, b) => b.norm - a.norm)
      .slice(0, 4);
  }

  function _sideCard(label, row, sentiment) {
    if (!row) {
      return `
        <div style="background:var(--bg-card,#15171c);border:1px solid var(--border,#272a31);padding:12px;border-radius:4px;margin-bottom:12px;">
          <div style="font-size:10px;letter-spacing:.5px;color:var(--text-dim);">${_esc(label)}</div>
          <div style="margin-top:4px;color:var(--text-dim);">no data</div>
        </div>`;
    }
    const color = sentiment === "leader" ? "var(--accent, #4ade80)" : "var(--danger, #f87171)";
    const score = row.composite_flow_score;
    const scoreTxt = score == null ? "—" : (score > 0 ? "+" : "") + score.toFixed(2);
    const drivers = _topContributors(row);
    const driverHtml = drivers.length === 0
      ? `<div style="color:var(--text-dim);font-size:11px;">no significant drivers</div>`
      : drivers.map((d) => {
          const sign = d.val > 0 ? "+" : "";
          const _v = Number(d.val) || 0;
          const valTxt = Math.abs(_v) < 1e6 ? _v.toFixed(2) : _v.toExponential(1);
          return `<div style="display:flex;justify-content:space-between;font-size:11px;padding:2px 0;">
            <span style="color:var(--text-dim);">${_esc(d.key)}</span>
            <span style="font-family:var(--font-mono,monospace);">${sign}${valTxt}${_esc(d.suffix)}</span>
          </div>`;
        }).join("");
    return `
      <div style="background:var(--bg-card,#15171c);border:1px solid var(--border,#272a31);padding:12px;border-radius:4px;margin-bottom:12px;">
        <div style="display:flex;justify-content:space-between;align-items:baseline;">
          <div>
            <div style="font-size:10px;letter-spacing:.5px;color:var(--text-dim);">${_esc(label)}</div>
            <div style="font-size:16px;font-weight:600;color:${color};margin-top:2px;">${_esc(row.sector)}</div>
            <div style="font-size:10px;color:var(--text-dim);">${_esc(row.etf)}</div>
          </div>
          <div style="text-align:right;">
            <div style="font-size:9px;color:var(--text-dim);">COMPOSITE</div>
            <div style="font-family:var(--font-mono,monospace);font-size:18px;color:${color};font-weight:600;">${_esc(scoreTxt)}</div>
          </div>
        </div>
        <div style="margin-top:10px;border-top:1px solid var(--border,#272a31);padding-top:8px;">
          <div style="font-size:9px;color:var(--text-dim);letter-spacing:.5px;margin-bottom:4px;">TOP DRIVERS</div>
          ${driverHtml}
        </div>
      </div>`;
  }

  function _buildTable(rows, sortKey, sortDesc) {
    const headers = COLUMNS.map((c) => {
      const isSort = c.key === sortKey;
      const arrow = isSort ? (sortDesc ? " ▼" : " ▲") : "";
      const cursor = c.sortable ? "cursor:pointer;" : "";
      return `<th data-key="${_esc(c.key)}"
                   style="${cursor}padding:6px 8px;text-align:left;font-size:9px;letter-spacing:.5px;color:var(--text-dim);border-bottom:1px solid var(--border,#272a31);white-space:nowrap;">
                ${_esc(c.label)}${arrow}
              </th>`;
    }).join("");

    const trs = rows.map((r) => {
      const tds = COLUMNS.map((c) => {
        const colVal = c.colorVal ? c.colorVal(r) : c.accessor(r);
        const bg = c.scale == null ? "transparent" : _divergentBg(colVal, c.scale);
        const fg = c.scale == null ? "inherit" : _divergentFg(colVal, c.scale);
        return `<td style="padding:8px;font-size:11px;background:${bg};color:${fg};font-family:var(--font-mono,monospace);white-space:nowrap;">${c.render(r)}</td>`;
      }).join("");
      return `<tr>${tds}</tr>`;
    }).join("");

    return `
      <div style="overflow-x:auto;background:var(--bg-card,#15171c);border:1px solid var(--border,#272a31);border-radius:4px;">
        <table style="width:100%;border-collapse:collapse;">
          <thead><tr>${headers}</tr></thead>
          <tbody>${trs}</tbody>
        </table>
      </div>`;
  }

  function _sortRows(rows, sortKey, sortDesc) {
    const col = COLUMNS.find((c) => c.key === sortKey) || COLUMNS[0];
    const cmp = (a, b) => {
      const va = col.accessor(a);
      const vb = col.accessor(b);
      // nulls always go to the bottom regardless of direction
      const aNull = va == null || (typeof va === "number" && isNaN(va));
      const bNull = vb == null || (typeof vb === "number" && isNaN(vb));
      if (aNull && bNull) return 0;
      if (aNull) return 1;
      if (bNull) return -1;
      if (typeof va === "number" && typeof vb === "number") {
        return sortDesc ? (vb - va) : (va - vb);
      }
      const sa = String(va).toLowerCase();
      const sb = String(vb).toLowerCase();
      if (sa < sb) return sortDesc ? 1 : -1;
      if (sa > sb) return sortDesc ? -1 : 1;
      return 0;
    };
    return rows.slice().sort(cmp);
  }

  function _bindSorting(host, data) {
    const ths = host.querySelectorAll("thead th[data-key]");
    ths.forEach((th) => {
      th.addEventListener("click", () => {
        const key = th.getAttribute("data-key");
        const col = COLUMNS.find((c) => c.key === key);
        if (!col || !col.sortable) return;
        // Flip direction if clicking same column, else use column's default.
        if (host._sortKey === key) {
          host._sortDesc = !host._sortDesc;
        } else {
          host._sortKey = key;
          host._sortDesc = col.sortDesc !== false;
        }
        _paint(host, data);
      });
    });
  }

  function _paint(host, data) {
    const sortKey = host._sortKey || "composite_flow_score";
    const sortDesc = host._sortDesc == null ? true : host._sortDesc;
    const sorted = _sortRows(data.sectors || [], sortKey, sortDesc);

    const tableHost = host.querySelector("[data-sectorflow-table]");
    if (tableHost) {
      tableHost.innerHTML = _buildTable(sorted, sortKey, sortDesc);
      _bindSorting(tableHost, data);
    }
  }

  function renderSectorFlow(container) {
    if (!container) return;
    container.innerHTML = _spinner("FETCHING SECTOR FLOW…");

    const api = _api();
    api.get("/api/synth/sectorflow")
      .then((data) => {
        if (!data || data.error) {
          container.innerHTML = _emptyCard(
            data && data.error ? data.error : "sector flow data unavailable",
          );
          return;
        }

        const sectors = data.sectors || [];
        const leaderName = data.leader_sector;
        const laggardName = data.laggard_sector;
        const leader = sectors.find((r) => r.sector === leaderName);
        const laggard = sectors.find((r) => r.sector === laggardName);

        const asOf = data.as_of ? new Date(data.as_of) : null;
        const stamp = asOf && !isNaN(asOf.getTime())
          ? asOf.toLocaleString("en-US", { hour12: false })
          : "—";

        container.innerHTML = `
          <div style="padding:16px;">
            <div style="display:flex;align-items:baseline;justify-content:space-between;margin-bottom:12px;">
              <div>
                <div style="font-size:18px;font-weight:600;letter-spacing:.5px;">SECTOR FLOW</div>
                <div style="font-size:10px;color:var(--text-dim);">multi-signal rotation heatmap · ${_esc(sectors.length)} sectors · as-of ${_esc(stamp)}</div>
              </div>
              <div style="font-size:9px;color:var(--text-dim);letter-spacing:.5px;text-align:right;">
                composite ∈ [-3,+3] · click any header to sort
              </div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 280px;gap:14px;align-items:start;">
              <div data-sectorflow-table></div>
              <div data-sectorflow-side>
                ${_sideCard("LEADER",  leader,  "leader")}
                ${_sideCard("LAGGARD", laggard, "laggard")}
                <div style="background:var(--bg-card,#15171c);border:1px solid var(--border,#272a31);padding:12px;border-radius:4px;font-size:10px;color:var(--text-dim);line-height:1.4;">
                  <div style="letter-spacing:.5px;margin-bottom:6px;color:var(--text-dim);">LEGEND</div>
                  <div>RS = relative strength vs SPY (pp)</div>
                  <div>Insiders = Finviz buy / (buy+sell) by sector</div>
                  <div>Congress = net 30d House PTR $ flow</div>
                  <div>Reddit = mention vs trailing baseline</div>
                  <div>HN = polarity-weighted score</div>
                  <div>Wiki = pageview z-score</div>
                  <div>PCR = put/call ratio (lower=bullish)</div>
                </div>
              </div>
            </div>
          </div>`;

        // Initial paint + sort binding
        container._sortKey = "composite_flow_score";
        container._sortDesc = true;
        _paint(container, data);
      })
      .catch((err) => {
        container.innerHTML = _emptyCard("sector flow fetch failed: " + (err && err.message ? err.message : err));
      });
  }

  global.renderSectorFlow = renderSectorFlow;
  global.loadSectorFlow = function loadSectorFlow() {
    const host = document.getElementById("view-sectorflow")
              || document.getElementById("sectorflow-host");
    if (host) renderSectorFlow(host);
  };

})(typeof window !== "undefined" ? window : this);
