/* AUGUR — AJ Insights
 * Self-contained, NON-module visualization layer for the trading agent.
 * Loaded via <script defer src=".../aj_insights.js"></script>.
 * Exposes window.AJInsights = { render(rootEl), _charts:{} }.
 *
 * Depends only on page globals: window.API (API.get -> parsed JSON, throws on
 * error) and window.Chart (Chart.js v4 UMD). Everything else is local.
 *
 * Design notes / data mapping live at the bottom of each loader.
 */
(function () {
  "use strict";

  var GREEN = "#2ecc71", RED = "#e74c3c", AMBER = "#f1c40f",
      ACCENT = "#4ea1ff", MUTED = "#888";

  // ── tiny helpers ─────────────────────────────────────────────────────────
  function esc(s) {
    if (s === null || s === undefined) return "";
    return String(s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  function num(v, d) {
    var n = Number(v);
    return isFinite(n) ? n : (d === undefined ? null : d);
  }
  function fmtPct(v, dp) {
    var n = num(v);
    if (n === null) return "—";
    return n.toFixed(dp === undefined ? 1 : dp) + "%";
  }
  function money(v) {
    var n = num(v);
    if (n === null) return "—";
    var sign = n < 0 ? "-" : "";
    var a = Math.abs(n);
    return sign + "$" + a.toLocaleString(undefined, { maximumFractionDigits: 2 });
  }
  function hhmm(ts) {
    if (!ts) return "--:--";
    try {
      var d = new Date(ts);
      if (isNaN(d.getTime())) {
        // ISO-ish without tz / odd strings: best effort slice
        var m = String(ts).match(/(\d{2}):(\d{2})/);
        return m ? (m[1] + ":" + m[2]) : "--:--";
      }
      return ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    } catch (e) { return "--:--"; }
  }
  function el(html) {
    var d = document.createElement("div");
    d.innerHTML = html.trim();
    return d.firstChild;
  }
  function placeholder(node, msg) {
    if (node) node.innerHTML = '<div class="muted" style="padding:12px 4px;font-size:13px;">' + esc(msg) + "</div>";
  }
  // a labeled horizontal progress bar (value/max -> colored fill, amber->red near cap)
  function bar(label, value, max, opts) {
    opts = opts || {};
    var v = num(value, 0), m = num(max, 0);
    var frac = m > 0 ? Math.max(0, Math.min(1, Math.abs(v) / m)) : 0;
    var color = opts.color;
    if (!color) {
      if (frac >= 0.9) color = RED;
      else if (frac >= 0.7) color = AMBER;
      else color = GREEN;
    }
    var right = opts.right !== undefined ? opts.right : (m > 0 ? (value + " / " + max) : String(value));
    return '<div style="margin:8px 0;">' +
      '<div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:3px;">' +
        '<span class="muted">' + esc(label) + '</span>' +
        '<span style="color:' + color + ';font-weight:600;">' + esc(right) + '</span></div>' +
      '<div style="background:#1b1b1b;border-radius:4px;height:10px;overflow:hidden;">' +
        '<div style="height:100%;width:' + (frac * 100).toFixed(1) + '%;background:' + color + ';"></div>' +
      '</div></div>';
  }
  // a 0..1 probability bar centered at .5 (left of center red-ish, right green-ish)
  function probBar(p) {
    var n = num(p);
    if (n === null) return '<span class="muted">n/a</span>';
    n = Math.max(0, Math.min(1, n));
    var pct = (n * 100).toFixed(0);
    var col = n >= 0.55 ? GREEN : (n <= 0.45 ? RED : MUTED);
    return '<div style="position:relative;background:#1b1b1b;border-radius:3px;height:9px;width:120px;">' +
      '<div style="position:absolute;left:50%;top:0;bottom:0;width:1px;background:#444;"></div>' +
      '<div style="position:absolute;left:0;top:0;bottom:0;width:' + pct + '%;background:' + col + ';border-radius:3px;opacity:.85;"></div>' +
      '</div>';
  }
  function chartCtx(id) {
    var c = document.getElementById(id);
    if (!c) return null;
    // destroy any prior chart bound to this canvas id
    if (W._charts[id]) { try { W._charts[id].destroy(); } catch (e) {} delete W._charts[id]; }
    if (typeof Chart === "undefined") return null;
    return c.getContext ? c.getContext("2d") : null;
  }
  function keepChart(id, chart) { if (chart) W._charts[id] = chart; }

  // ── panel scaffold ───────────────────────────────────────────────────────
  function card(title, id, sub) {
    return '' +
      '<section class="panel" style="display:flex;flex-direction:column;min-width:0;">' +
        '<div class="panel-header"><span class="panel-title">' + esc(title) + '</span>' +
          (sub ? '<span class="muted" style="font-size:11px;margin-left:8px;">' + esc(sub) + '</span>' : '') +
        '</div>' +
        '<div class="panel-body" id="' + esc(id) + '" style="min-height:60px;">' +
          '<div class="muted" style="padding:12px 4px;">loading…</div>' +
        '</div>' +
      '</section>';
  }

  // ──────────────────────────────────────────────────────────────────────────
  //  PANEL LOADERS — each independent + try/catch; never throws out of render.
  // ──────────────────────────────────────────────────────────────────────────

  // 1. DECISION FUNNEL
  // /api/aj/cycles?limit=1 -> {cycles:[{result_json:{code:count}, scanned,
  //   with_signal, executed}], latest_scan:[...]}. Funnel stages derived from
  //   the latest cycle; non-executed result codes shown as drop-off bars.
  var DROP_CODES = ["no_signal", "no_edge", "cost_blocked", "council_veto",
    "event_blackout", "unsizable", "cooldown", "just_exited"];
  async function loadFunnel(body) {
    try {
      var d = await API.get("/api/aj/cycles?limit=1");
      var cyc = (d && d.cycles && d.cycles[0]) || null;
      if (!cyc) return placeholder(body, "no cycles yet — run the agent once");
      var rj = cyc.result_json || {};
      var scanned = num(cyc.scanned, 0), withSig = num(cyc.with_signal, 0),
          executed = num(cyc.executed, 0);
      var blocked = 0;
      DROP_CODES.forEach(function (k) {
        if (k !== "no_signal" && k !== "no_edge") blocked += num(rj[k], 0);
      });
      var cleared = Math.max(0, withSig - blocked);
      var stages = [
        ["Scanned", scanned, ACCENT],
        ["Had signal", withSig, ACCENT],
        ["Cleared gates", cleared, AMBER],
        ["Executed", executed, GREEN]
      ];
      var top = scanned || withSig || 1;
      var html = '<div style="font-size:12px;">';
      stages.forEach(function (s) {
        var frac = top > 0 ? Math.min(1, s[1] / top) : 0;
        html += '<div style="margin:5px 0;">' +
          '<div style="display:flex;justify-content:space-between;font-size:11px;margin-bottom:2px;">' +
            '<span>' + esc(s[0]) + '</span><span style="color:' + s[2] + ';font-weight:600;">' + s[1] + '</span></div>' +
          '<div style="background:#1b1b1b;border-radius:3px;height:14px;">' +
            '<div style="height:100%;width:' + (frac * 100).toFixed(1) + '%;background:' + s[2] + ';border-radius:3px;"></div></div>' +
          '</div>';
      });
      // drop-off detail
      var drops = DROP_CODES.filter(function (k) { return num(rj[k], 0) > 0; });
      if (drops.length) {
        html += '<div class="muted" style="margin-top:10px;font-size:11px;">drop-offs</div>';
        drops.forEach(function (k) {
          html += '<div style="display:flex;justify-content:space-between;border-bottom:1px solid #1e1e1e;padding:2px 0;">' +
            '<span class="muted">' + esc(k.replace(/_/g, " ")) + '</span>' +
            '<span style="color:' + RED + ';">' + num(rj[k], 0) + '</span></div>';
        });
      }
      html += '</div>';
      body.innerHTML = html;
    } catch (e) { placeholder(body, "decision funnel unavailable"); }
  }

  // 2. ACTIVITY TIMELINE
  // /api/aj/cycles?limit=20 (newest-first feed) + /api/aj/status scheduler line.
  async function loadTimeline(body) {
    try {
      var hdr = "";
      try {
        var st = await API.get("/api/aj/status");
        var sch = (st && st.scheduler) || {};
        var mins = num(sch.next_fire_in_min);
        if (mins !== null) {
          hdr = '<div style="font-size:12px;color:' + ACCENT + ';margin-bottom:8px;">next run in ~' +
            Math.max(0, Math.round(mins)) + 'm' +
            (sch.cadence ? ' (' + esc(sch.cadence) + ')' : '') + '</div>';
        }
      } catch (e) {}
      var d = await API.get("/api/aj/cycles?limit=20");
      var cycles = (d && d.cycles) || [];
      if (!cycles.length) { body.innerHTML = hdr || ""; return placeholderInto(body, hdr, "no activity yet"); }
      var html = hdr + '<div style="font-size:12px;">';
      cycles.forEach(function (c) {
        var rj = c.result_json || {};
        // dominant non-executed drop reason
        var best = null, bestN = 0;
        DROP_CODES.forEach(function (k) {
          var n = num(rj[k], 0);
          if (n > bestN) { bestN = n; best = k; }
        });
        html += '<div style="display:flex;gap:8px;padding:4px 0;border-bottom:1px solid #1b1b1b;align-items:baseline;">' +
          '<span style="color:' + MUTED + ';width:42px;flex:none;">' + esc(hhmm(c.ts)) + '</span>' +
          '<span style="flex:1;min-width:0;">scanned ' + num(c.scanned, 0) +
            ' · <span style="color:' + GREEN + ';">exec ' + num(c.executed, 0) + '</span>' +
            ' · exits ' + num(c.exits, 0) + '</span>' +
          (best ? '<span class="muted" style="flex:none;font-size:11px;">' + esc(best.replace(/_/g, " ")) + '</span>' : '') +
          '</div>';
      });
      html += '</div>';
      body.innerHTML = html;
    } catch (e) { placeholder(body, "activity timeline unavailable"); }
  }
  function placeholderInto(body, hdr, msg) {
    body.innerHTML = (hdr || "") + '<div class="muted" style="padding:8px 4px;">' + esc(msg) + '</div>';
  }

  // 3. ANNOTATED EQUITY CURVE
  // /api/aj/analytics -> .equity_curve = [{date, equity_usd, day_pnl_usd,...}]
  //   (observed shape; we adapt by probing date/ts and equity/equity_usd/[1]).
  // /api/aj/orders?limit=200 -> {orders:[{created_at|submitted_at,side,symbol}]}
  //   overlaid as buy ▲ / sell ▼ markers.
  function ecPoint(r) {
    if (Array.isArray(r)) return { t: r[0], v: num(r[1]) };
    if (r && typeof r === "object") {
      var t = r.ts || r.date || r.time || r.t;
      var v = r.equity !== undefined ? r.equity
            : (r.equity_usd !== undefined ? r.equity_usd
            : (r.v !== undefined ? r.v : r.value));
      return { t: t, v: num(v) };
    }
    return { t: null, v: null };
  }
  async function loadEquity(body) {
    try {
      var d = await API.get("/api/aj/analytics");
      var curve = (d && d.equity_curve) || [];
      if (!curve.length) return placeholder(body, "accruing — needs more cycles");
      var pts = curve.map(ecPoint).filter(function (p) { return p.v !== null; });
      if (!pts.length) return placeholder(body, "accruing — needs more cycles");

      var orders = [];
      try {
        var od = await API.get("/api/aj/orders?limit=200");
        orders = (od && od.orders) || [];
      } catch (e) {}

      body.innerHTML = '<canvas id="aj-ins-equity" height="150"></canvas>';
      var ctx = chartCtx("aj-ins-equity");
      if (!ctx) return placeholder(body, "chart engine unavailable");

      var labels = pts.map(function (p) { return p.t; });
      var line = pts.map(function (p) { return p.v; });
      // map order timestamps onto nearest curve x by time
      function tof(s) { var t = new Date(s).getTime(); return isNaN(t) ? null : t; }
      var curveT = pts.map(function (p) { return tof(p.t); });
      function nearestIdx(t) {
        if (t === null) return null;
        var bi = 0, bd = Infinity;
        for (var i = 0; i < curveT.length; i++) {
          if (curveT[i] === null) continue;
          var dd = Math.abs(curveT[i] - t);
          if (dd < bd) { bd = dd; bi = i; }
        }
        return bi;
      }
      var buys = [], sells = [];
      orders.forEach(function (o) {
        var t = tof(o.created_at || o.submitted_at);
        var idx = nearestIdx(t);
        if (idx === null) return;
        var pt = { x: labels[idx], y: line[idx] };
        if (String(o.side).toLowerCase() === "buy") buys.push(pt);
        else if (String(o.side).toLowerCase() === "sell") sells.push(pt);
      });

      var chart = new Chart(ctx, {
        type: "line",
        data: {
          labels: labels,
          datasets: [
            { label: "equity", data: line, borderColor: ACCENT, backgroundColor: "rgba(78,161,255,.12)",
              borderWidth: 2, pointRadius: 0, tension: .2, fill: true },
            { label: "buy", data: buys, type: "scatter", showLine: false,
              pointStyle: "triangle", pointRadius: 7, backgroundColor: GREEN, borderColor: GREEN },
            { label: "sell", data: sells, type: "scatter", showLine: false,
              pointStyle: "triangle", rotation: 180, pointRadius: 7, backgroundColor: RED, borderColor: RED }
          ]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { labels: { color: MUTED, boxWidth: 10, font: { size: 10 } } } },
          scales: {
            x: { ticks: { color: MUTED, maxTicksLimit: 6, font: { size: 9 } }, grid: { color: "#1b1b1b" } },
            y: { ticks: { color: MUTED, font: { size: 9 } }, grid: { color: "#1b1b1b" } }
          }
        }
      });
      keepChart("aj-ins-equity", chart);
    } catch (e) { placeholder(body, "equity curve unavailable"); }
  }

  // 4. WHY THIS TRADE
  // /api/aj/proposals?limit=20 -> {proposals:[{symbol,side,thesis,created_at}]}
  //   clickable; on click /api/forecast/ensemble/<sym> ->
  //   {ensemble:{prob_up,edge_pct_pts,conviction}, signals:[{name,prob_up,weight,detail}]}
  async function loadProposals(body) {
    try {
      var d = await API.get("/api/aj/proposals?limit=20");
      var props = (d && d.proposals) || [];
      if (!props.length) return placeholder(body, "no proposals yet");
      var listId = "aj-ins-prop-list", detId = "aj-ins-prop-det";
      var html = '<div style="display:flex;gap:10px;flex-wrap:wrap;">' +
        '<div id="' + listId + '" style="flex:1;min-width:160px;max-height:220px;overflow:auto;font-size:12px;"></div>' +
        '<div id="' + detId + '" style="flex:2;min-width:200px;font-size:12px;">' +
          '<span class="muted">pick a proposal to see its signal breakdown</span></div></div>';
      body.innerHTML = html;
      var list = body.querySelector("#" + listId);
      var det = body.querySelector("#" + detId);
      props.forEach(function (p) {
        var sideCol = String(p.side).toLowerCase() === "buy" ? GREEN : RED;
        var row = el('<div class="aj-ins-prop-row" style="cursor:pointer;padding:5px 6px;border-bottom:1px solid #1b1b1b;border-radius:3px;">' +
          '<span style="color:' + sideCol + ';font-weight:600;">' + esc(String(p.side || "").toUpperCase()) + '</span> ' +
          '<strong>' + esc(p.symbol) + '</strong>' +
          '<div class="muted" style="font-size:11px;">' + esc(hhmm(p.created_at)) +
            (p.thesis ? ' · ' + esc(String(p.thesis).slice(0, 60)) : '') + '</div></div>');
        row.addEventListener("click", function () {
          Array.prototype.forEach.call(list.children, function (c) { c.style.background = ""; });
          row.style.background = "#1b2738";
          showProposalDetail(det, p.symbol);
        });
        list.appendChild(row);
      });
    } catch (e) { placeholder(body, "proposals unavailable"); }
  }
  async function showProposalDetail(det, symbol) {
    det.innerHTML = '<div class="muted">loading ' + esc(symbol) + '…</div>';
    try {
      var f = await API.get("/api/forecast/ensemble/" + encodeURIComponent(symbol));
      var ens = (f && f.ensemble) || null;
      var sigs = (f && f.signals) || [];
      if (!ens && !sigs.length) return placeholder(det, "no forecast for " + symbol);
      var html = '<div style="margin-bottom:6px;"><strong>' + esc(symbol) + '</strong>';
      if (ens) {
        var ec = num(ens.edge_pct_pts);
        var col = ec === null ? MUTED : (ec >= 0 ? GREEN : RED);
        html += ' <span class="muted">P(up) ' +
          (num(ens.prob_up) !== null ? (num(ens.prob_up) * 100).toFixed(0) + "%" : "—") + '</span>' +
          ' · <span style="color:' + col + ';">edge ' + (ec === null ? "—" : ec.toFixed(1) + "pp") + '</span>' +
          ' · ' + esc(ens.conviction || "");
      }
      html += '</div>';
      if (!sigs.length) html += '<span class="muted">no component signals</span>';
      sigs.forEach(function (s) {
        html += '<div style="display:flex;align-items:center;gap:8px;padding:3px 0;border-bottom:1px solid #1b1b1b;">' +
          '<span style="width:110px;flex:none;" title="' + esc(s.detail || "") + '">' + esc(s.name) + '</span>' +
          probBar(s.prob_up) +
          '<span class="muted" style="flex:none;width:42px;text-align:right;">' +
            (num(s.weight) !== null ? (num(s.weight) * 100).toFixed(0) + "%" : "—") + '</span>' +
          '</div>';
      });
      det.innerHTML = html;
    } catch (e) { placeholder(det, "forecast unavailable for " + symbol); }
  }

  // 5. SIGNAL SKILL
  // /api/aj/signal_skill -> {signals:{name:{skill:{n,hit_rate,brier_skill,ic,
  //   available}, promoted, reason}}, gate_on, multi_factor}
  async function loadSignalSkill(body) {
    try {
      var d = await API.get("/api/aj/signal_skill");
      var sigs = (d && d.signals) || {};
      var names = Object.keys(sigs);
      if (!names.length) return placeholder(body, "no signal skill data");
      var head = '<div class="muted" style="font-size:11px;margin-bottom:6px;">' +
        'IC gate: <span style="color:' + (d.gate_on ? GREEN : MUTED) + ';">' + (d.gate_on ? "on" : "off") + '</span>' +
        ' · multi-factor: <span style="color:' + (d.multi_factor ? GREEN : MUTED) + ';">' + (d.multi_factor ? "on" : "off") + '</span></div>';
      var rows = '<table class="data-table" style="width:100%;font-size:11px;">' +
        '<thead><tr><th>signal</th><th>n</th><th>hit</th><th>brier</th><th>IC</th><th>status</th></tr></thead><tbody>';
      names.forEach(function (n) {
        var s = sigs[n] || {};
        var sk = s.skill || {};
        var promoted = s.promoted === true;
        var avail = sk.available;
        var badge = promoted
          ? '<span style="color:' + GREEN + ';">PROMOTED ✓</span>'
          : '<span class="muted">not yet</span>';
        function cell(v, mult, dp) {
          var x = num(v);
          if (x === null) return '<span class="muted">—</span>';
          return (mult ? (x * mult).toFixed(dp === undefined ? 0 : dp) + (mult === 100 ? "%" : "") : x.toFixed(dp || 0));
        }
        rows += '<tr title="' + esc(s.reason || "") + '">' +
          '<td>' + esc(n) + '</td>' +
          '<td>' + (avail === false ? '<span class="muted">0</span>' : num(sk.n, 0)) + '</td>' +
          '<td>' + cell(sk.hit_rate, 100, 0) + '</td>' +
          '<td>' + cell(sk.brier_skill, 1, 3) + '</td>' +
          '<td>' + cell(sk.ic, 1, 3) + '</td>' +
          '<td>' + badge + '</td></tr>';
      });
      rows += '</tbody></table>';
      body.innerHTML = head + rows;
    } catch (e) { placeholder(body, "signal skill unavailable"); }
  }

  // 6. RISK COCKPIT
  // /api/aj/status -> .day_pnl, .config{max_daily_loss_usd,max_trades_per_day,...},
  //   .halted, .audit_chain.ok, .orders
  async function loadRisk(body) {
    try {
      var st = await API.get("/api/aj/status");
      var cfg = st.config || {};
      var html = "";
      // halted banner
      var halted = st.halted === true;
      html += '<div style="text-align:center;padding:8px;border-radius:4px;margin-bottom:8px;font-weight:700;background:' +
        (halted ? "rgba(231,76,60,.15)" : "rgba(46,204,113,.12)") + ';color:' +
        (halted ? RED : GREEN) + ';">' + (halted ? "HALTED" : "ARMED / LIVE-OK") + '</div>';
      // day P&L vs max daily loss
      var pnl = num(st.day_pnl);
      var maxLoss = num(cfg.max_daily_loss_usd);
      if (maxLoss && maxLoss > 0) {
        var loss = pnl !== null && pnl < 0 ? Math.abs(pnl) : 0;
        html += bar("day P&L vs loss limit", loss, maxLoss, {
          right: (pnl === null ? "—" : money(pnl)) + " / -" + money(maxLoss),
          color: pnl !== null && pnl >= 0 ? GREEN : undefined
        });
      } else if (pnl !== null) {
        html += '<div style="font-size:12px;margin:6px 0;">day P&L: <span style="color:' +
          (pnl >= 0 ? GREEN : RED) + ';">' + money(pnl) + '</span></div>';
      }
      // open positions vs trades cap (best-effort proxy)
      var openN = num((st.orders || {}).open, null);
      if (openN === null) openN = num((st.orders || {}).by_state && st.orders.by_state.filled, null);
      var cap = num(cfg.max_trades_per_day);
      if (openN !== null && cap) {
        html += bar("orders today vs daily cap", openN, cap, { right: openN + " / " + cap });
      } else if (cap) {
        html += '<div class="muted" style="font-size:12px;margin:6px 0;">daily trade cap: ' + cap + '</div>';
      }
      // audit chain integrity
      var chainOk = st.audit_chain && st.audit_chain.ok;
      html += '<div style="font-size:12px;margin-top:8px;">audit chain: <span style="color:' +
        (chainOk ? GREEN : RED) + ';font-weight:600;">' + (chainOk ? "VERIFIED ✓" : "BROKEN ✗") + '</span></div>';
      // cumulative pnl if present
      var cum = num(st.cumulative_pnl);
      if (cum !== null) {
        html += '<div style="font-size:12px;">cumulative P&L: <span style="color:' +
          (cum >= 0 ? GREEN : RED) + ';">' + money(cum) + '</span></div>';
      }
      body.innerHTML = html;
    } catch (e) { placeholder(body, "risk cockpit unavailable"); }
  }

  // 7. POSITION DRILL-DOWN
  // /api/aj/positions -> {positions:[{symbol,qty,avg_cost,mark,...}], count,...}
  //   clickable; on click /api/aj/position/<sym> -> {position, entry:{thesis,
  //   created_at}, forecast:{prob_up,edge_pct_pts,conviction}, mark,
  //   levels:{take_profit,stop_loss}, time_stop:{...}, profit_ladder:[...]}
  async function loadPositions(body) {
    try {
      var d = await API.get("/api/aj/positions");
      var rows = posList(d);
      if (!rows.length) return placeholder(body, "agent holds no positions yet");
      var listId = "aj-ins-pos-list", detId = "aj-ins-pos-det";
      body.innerHTML = '<div style="display:flex;gap:10px;flex-wrap:wrap;">' +
        '<div id="' + listId + '" style="flex:1;min-width:140px;max-height:220px;overflow:auto;font-size:12px;"></div>' +
        '<div id="' + detId + '" style="flex:2;min-width:200px;font-size:12px;"><span class="muted">pick a position</span></div></div>';
      var list = body.querySelector("#" + listId), det = body.querySelector("#" + detId);
      rows.forEach(function (p) {
        var up = num(p.unrealized_pct);
        var col = up === null ? MUTED : (up >= 0 ? GREEN : RED);
        var row = el('<div style="cursor:pointer;padding:5px 6px;border-bottom:1px solid #1b1b1b;display:flex;justify-content:space-between;">' +
          '<strong>' + esc(p.symbol) + '</strong>' +
          '<span style="color:' + col + ';">' + (up === null ? "" : fmtPct(up)) + '</span></div>');
        row.addEventListener("click", function () {
          Array.prototype.forEach.call(list.children, function (c) { c.style.background = ""; });
          row.style.background = "#1b2738";
          showPositionDetail(det, p.symbol, p);
        });
        list.appendChild(row);
      });
    } catch (e) { placeholder(body, "positions unavailable"); }
  }
  function posList(d) {
    if (!d) return [];
    var p = d.positions !== undefined ? d.positions : d;
    if (Array.isArray(p)) return p;
    if (p && typeof p === "object") {
      return Object.keys(p).map(function (k) {
        var v = p[k] || {};
        return Object.assign({ symbol: v.symbol || k }, v);
      });
    }
    return [];
  }
  async function showPositionDetail(det, sym, summary) {
    det.innerHTML = '<div class="muted">loading ' + esc(sym) + '…</div>';
    try {
      var d = await API.get("/api/aj/position/" + encodeURIComponent(sym));
      var entry = d.entry || {}, fc = d.forecast || {}, lv = d.levels || {};
      var mark = num(d.mark);
      var pos = d.position || summary || {};
      var avg = num(pos.avg_cost);
      var html = '<div style="margin-bottom:4px;"><strong>' + esc(sym) + '</strong>';
      if (mark !== null) html += ' <span class="muted">mark ' + money(mark) + '</span>';
      if (avg !== null) html += ' · avg ' + money(avg);
      html += '</div>';
      if (entry.thesis) html += '<div class="muted" style="font-size:11px;margin-bottom:6px;">entry: ' + esc(entry.thesis) + '</div>';
      // current edge
      var edge = num(fc.edge_pct_pts);
      if (edge !== null) {
        html += '<div style="font-size:12px;">current edge: <span style="color:' + (edge >= 0 ? GREEN : RED) + ';">' +
          edge.toFixed(1) + 'pp</span>' + (fc.conviction ? ' · ' + esc(fc.conviction) : '') + '</div>';
      }
      // levels
      var tp = num(lv.take_profit), sl = num(lv.stop_loss);
      if (tp !== null || sl !== null) {
        html += '<div style="font-size:12px;margin-top:4px;">' +
          (tp !== null ? 'TP <span style="color:' + GREEN + ';">' + money(tp) + '</span> ' : '') +
          (sl !== null ? '· SL <span style="color:' + RED + ';">' + money(sl) + '</span>' : '') + '</div>';
      }
      // time stop
      var ts = d.time_stop || {};
      if (ts && (ts.exit !== undefined || ts.reason)) {
        html += '<div style="font-size:11px;margin-top:4px;" class="muted">time-stop: ' +
          (ts.exit ? '<span style="color:' + AMBER + ';">EXIT</span> ' : '') + esc(ts.reason || "") + '</div>';
      }
      // profit ladder rungs
      var ladder = d.profit_ladder || [];
      if (ladder.length) {
        html += '<div style="font-size:11px;margin-top:6px;" class="muted">profit ladder</div>';
        ladder.forEach(function (r) {
          var trig = r.triggered === true;
          html += '<div style="display:flex;justify-content:space-between;font-size:11px;border-bottom:1px solid #1b1b1b;padding:1px 0;">' +
            '<span>+' + (num(r.gain_pct) !== null ? num(r.gain_pct).toFixed(0) : "?") + '% → trim ' +
              (num(r.trim_frac) !== null ? (num(r.trim_frac) * 100).toFixed(0) + "%" : "?") + '</span>' +
            '<span style="color:' + (trig ? GREEN : MUTED) + ';">' + (trig ? "hit ✓" : "pending") + '</span></div>';
        });
      }
      det.innerHTML = html;
    } catch (e) { placeholder(det, "drill-down unavailable for " + sym); }
  }

  // 8. AUDIT EXPLORER
  // /api/aj/audit?limit=100 -> {audit:[{ts,kind,actor,symbol,cycle_id,...}],
  //   chain:{ok,...}}. Client-side text filter + chain badge.
  async function loadAudit(body) {
    try {
      var d = await API.get("/api/aj/audit?limit=100");
      var rows = (d && d.audit) || [];
      var ok = d && d.chain && d.chain.ok;
      var badge = '<span style="float:right;color:' + (ok ? GREEN : RED) + ';font-weight:700;font-size:11px;">' +
        (ok ? "CHAIN VERIFIED ✓" : "CHAIN BROKEN ✗") + '</span>';
      body.innerHTML = badge +
        '<input id="aj-ins-audit-q" placeholder="filter…" style="width:60%;background:#161616;border:1px solid #2a2a2a;color:#ddd;border-radius:4px;padding:4px 6px;font-size:12px;margin-bottom:6px;">' +
        '<div id="aj-ins-audit-tbl" style="max-height:240px;overflow:auto;"></div>';
      var tbl = body.querySelector("#aj-ins-audit-tbl");
      var q = body.querySelector("#aj-ins-audit-q");
      function draw() {
        var term = (q.value || "").toLowerCase();
        var filtered = !term ? rows : rows.filter(function (r) {
          return JSON.stringify(r).toLowerCase().indexOf(term) !== -1;
        });
        if (!filtered.length) { tbl.innerHTML = '<div class="muted" style="padding:8px;">no matching events</div>'; return; }
        var h = '<table class="data-table" style="width:100%;font-size:11px;"><thead><tr>' +
          '<th>ts</th><th>kind</th><th>actor</th><th>sym</th><th>cycle</th></tr></thead><tbody>';
        filtered.slice(0, 200).forEach(function (r) {
          h += '<tr><td>' + esc(hhmm(r.ts)) + '</td><td>' + esc(r.kind || "") + '</td>' +
            '<td>' + esc(r.actor || "") + '</td><td>' + esc(r.symbol || "") + '</td>' +
            '<td class="muted">' + esc(String(r.cycle_id || "").slice(0, 10)) + '</td></tr>';
        });
        h += '</tbody></table>';
        tbl.innerHTML = h;
      }
      q.addEventListener("input", draw);
      if (!rows.length) { tbl.innerHTML = '<div class="muted" style="padding:8px;">no audit events yet</div>'; }
      else draw();
    } catch (e) { placeholder(body, "audit explorer unavailable"); }
  }

  // 9. EXPOSURE TREEMAP (weighted bars)
  // /api/aj/allocation -> {sym:{current_w,target_w,drift}} (may be {} when
  //   portfolio_construction off). Fallback: compute current weights from
  //   /api/aj/positions marks (weight_pct).
  async function loadExposure(body) {
    try {
      var alloc = {};
      try { alloc = await API.get("/api/aj/allocation") || {}; } catch (e) { alloc = {}; }
      var keys = alloc && typeof alloc === "object" ? Object.keys(alloc) : [];
      var note = "";
      var items = [];
      if (keys.length) {
        keys.forEach(function (k) {
          var a = alloc[k] || {};
          items.push({ sym: k, cur: num(a.current_w, 0), tgt: num(a.target_w), drift: num(a.drift) });
        });
      } else {
        note = "enable Portfolio construction to see target weights";
        // fallback: current weights from positions
        try {
          var d = await API.get("/api/aj/positions");
          posList(d).forEach(function (p) {
            items.push({ sym: p.symbol, cur: num(p.weight_pct, 0) / 100, tgt: null, drift: null });
          });
        } catch (e) {}
      }
      if (!items.length) return placeholder(body, note || "no exposure data");
      items.sort(function (a, b) { return (b.cur || 0) - (a.cur || 0); });
      var maxW = Math.max.apply(null, items.map(function (i) { return i.cur || 0; })) || 1;
      var html = note ? '<div class="muted" style="font-size:11px;margin-bottom:6px;">' + esc(note) + '</div>' : "";
      items.forEach(function (i) {
        var frac = maxW > 0 ? (i.cur || 0) / maxW : 0;
        var driftCol = i.drift === null ? ACCENT : (i.drift > 0.001 ? GREEN : (i.drift < -0.001 ? RED : MUTED));
        // target marker position relative to bar (target as fraction of maxW)
        var tgtFrac = i.tgt === null || i.tgt === undefined ? null : Math.max(0, Math.min(1, i.tgt / maxW));
        html += '<div style="margin:6px 0;font-size:11px;">' +
          '<div style="display:flex;justify-content:space-between;margin-bottom:2px;">' +
            '<strong>' + esc(i.sym) + '</strong>' +
            '<span style="color:' + driftCol + ';">' + fmtPct((i.cur || 0) * 100) +
              (i.tgt !== null && i.tgt !== undefined ? ' → ' + fmtPct(i.tgt * 100) : '') + '</span></div>' +
          '<div style="position:relative;background:#1b1b1b;border-radius:3px;height:14px;">' +
            '<div style="height:100%;width:' + (frac * 100).toFixed(1) + '%;background:' + driftCol + ';border-radius:3px;opacity:.85;"></div>' +
            (tgtFrac !== null ? '<div style="position:absolute;top:-2px;bottom:-2px;left:' + (tgtFrac * 100).toFixed(1) +
              '%;width:2px;background:#fff;"></div>' : '') +
          '</div></div>';
      });
      body.innerHTML = html;
    } catch (e) { placeholder(body, "exposure unavailable"); }
  }

  // 10. SELECTIVITY SCATTER
  // latest_scan from /api/aj/cycles?limit=1 -> [{symbol,edge,conviction,result}]
  //   scatter x=edge, y=conviction(0/.5/1), executed=green; vertical line at
  //   min_edge_pct_pts from /api/aj/config.
  function convToY(c) {
    if (typeof c === "number") return Math.max(0, Math.min(1, c));
    var s = String(c || "").toLowerCase();
    if (s.indexOf("high") !== -1) return 1;
    if (s.indexOf("mod") !== -1 || s.indexOf("med") !== -1) return 0.5;
    if (s.indexOf("low") !== -1) return 0;
    var n = num(c);
    return n === null ? 0.5 : Math.max(0, Math.min(1, n));
  }
  async function loadScatter(body) {
    try {
      var d = await API.get("/api/aj/cycles?limit=1");
      var scan = (d && d.latest_scan) || [];
      if (!scan.length) return placeholder(body, "no candidates scanned yet");
      var minEdge = null;
      try {
        var cfg = await API.get("/api/aj/config");
        var c = (cfg && cfg.config) || cfg || {};
        minEdge = num(c.min_edge_pct_pts);
      } catch (e) {}

      var exec = [], other = [];
      scan.forEach(function (s) {
        var pt = { x: num(s.edge, 0), y: convToY(s.conviction),
                   sym: s.symbol, result: s.result };
        if (String(s.result) === "executed") exec.push(pt); else other.push(pt);
      });
      body.innerHTML = '<canvas id="aj-ins-scatter" height="160"></canvas>';
      var ctx = chartCtx("aj-ins-scatter");
      if (!ctx) return placeholder(body, "chart engine unavailable");

      var datasets = [
        { label: "other", data: other, backgroundColor: MUTED, pointRadius: 5 },
        { label: "executed", data: exec, backgroundColor: GREEN, pointRadius: 6 }
      ];
      var ann = [];
      var chart = new Chart(ctx, {
        type: "scatter",
        data: { datasets: datasets },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: {
            legend: { labels: { color: MUTED, boxWidth: 10, font: { size: 10 } } },
            title: { display: true, text: "candidates this cycle vs the edge bar",
                     color: MUTED, font: { size: 11 } },
            tooltip: { callbacks: { label: function (i) {
              var p = i.raw || {};
              return (p.sym || "?") + " · edge " + num(p.x, 0).toFixed(1) + "pp · " + (p.result || "");
            } } }
          },
          scales: {
            x: { title: { display: true, text: "edge (pp)", color: MUTED, font: { size: 10 } },
                 ticks: { color: MUTED, font: { size: 9 } }, grid: { color: "#1b1b1b" } },
            y: { min: -0.1, max: 1.1,
                 title: { display: true, text: "conviction", color: MUTED, font: { size: 10 } },
                 ticks: { color: MUTED, font: { size: 9 },
                   callback: function (v) { return v === 0 ? "low" : (v === 0.5 ? "med" : (v === 1 ? "high" : "")); } },
                 grid: { color: "#1b1b1b" } }
          }
        }
      });
      // draw the edge-bar vertical line via an afterDraw hook (no plugin dep)
      if (minEdge !== null) {
        var plugin = {
          id: "aj-edgebar-" + Math.random().toString(36).slice(2),
          afterDraw: function (ch) {
            try {
              var xs = ch.scales.x, area = ch.chartArea, c = ch.ctx;
              var px = xs.getPixelForValue(minEdge);
              if (px < area.left || px > area.right) return;
              c.save();
              c.strokeStyle = ACCENT; c.lineWidth = 1.5; c.setLineDash([5, 4]);
              c.beginPath(); c.moveTo(px, area.top); c.lineTo(px, area.bottom); c.stroke();
              c.restore();
            } catch (e) {}
          }
        };
        ch_addPlugin(chart, plugin);
      }
      keepChart("aj-ins-scatter", chart);
    } catch (e) { placeholder(body, "selectivity scatter unavailable"); }
  }
  function ch_addPlugin(chart, plugin) {
    try {
      chart.config.plugins = chart.config.plugins || [];
      chart.config.plugins.push(plugin);
      chart.update();
    } catch (e) {}
  }

  // ──────────────────────────────────────────────────────────────────────────
  //  render
  // ──────────────────────────────────────────────────────────────────────────
  var PANELS = [
    ["1 · Decision Funnel",        "aj-ins-funnel",    "where candidates drop out", loadFunnel],
    ["2 · Activity Timeline",      "aj-ins-timeline",  "recent cycles",             loadTimeline],
    ["3 · Annotated Equity Curve", "aj-ins-equity-b",  "equity + trade markers",    loadEquity],
    ["4 · Why This Trade",         "aj-ins-prop",      "per-decision signals",      loadProposals],
    ["5 · Signal Skill",           "aj-ins-skill",     "realized IC / promotion",   loadSignalSkill],
    ["6 · Risk Cockpit",           "aj-ins-risk",      "limits & integrity",        loadRisk],
    ["7 · Position Drill-Down",    "aj-ins-pos",       "held names",                loadPositions],
    ["8 · Audit Explorer",         "aj-ins-audit",     "tamper-evident log",        loadAudit],
    ["9 · Exposure",               "aj-ins-exposure",  "current vs target weight",  loadExposure],
    ["10 · Selectivity Scatter",   "aj-ins-scatter-b", "edge vs conviction",        loadScatter]
  ];

  function render(rootEl) {
    if (!rootEl) return;
    // destroy any charts from a prior render
    Object.keys(W._charts).forEach(function (id) {
      try { W._charts[id].destroy(); } catch (e) {}
      delete W._charts[id];
    });
    if (typeof API === "undefined" || !API || typeof API.get !== "function") {
      rootEl.innerHTML = '<div class="muted" style="padding:16px;">API layer unavailable — cannot load insights.</div>';
      return;
    }
    var grid = '<div class="aj-insights-grid" style="display:grid;' +
      'grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px;align-items:start;">';
    PANELS.forEach(function (p) { grid += card(p[0], p[1], p[2]); });
    grid += '</div>';
    rootEl.innerHTML = grid;

    // kick off each loader independently — one failure can't break the others
    PANELS.forEach(function (p) {
      var body = document.getElementById(p[1]);
      if (!body) return;
      try {
        Promise.resolve()
          .then(function () { return p[3](body); })
          .catch(function () { placeholder(body, "panel unavailable"); });
      } catch (e) { placeholder(body, "panel unavailable"); }
    });
  }

  var W = (window.AJInsights = window.AJInsights || {});
  W._charts = W._charts || {};
  W.render = render;
})();
