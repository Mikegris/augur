// ── JARVIS — unified assistant layer (frontend) ───────────────────────────────
// Two surfaces:
//   1. Briefing panel — rendered at the top of the Overview by loadOverview()
//      via Jarvis.renderBriefing('ov-jarvis'). Prioritized insight cards with
//      deep-link actions into the app's views.
//   2. Command palette — ⌘K / Ctrl+K anywhere. Fuzzy view navigation, instant
//      symbol open, and natural-language questions answered inline by
//      POST /api/jarvis/ask.
// Classic script: reads the app's global lexical declarations (NAV_GROUPS,
// navigate, openResearch, _esc) by name, guarded with typeof checks.
(function (global) {
  'use strict';

  const esc = (s) => (typeof _esc === 'function' ? _esc(s) : String(s == null ? '' : s));

  // Safe mini-markdown: runs AFTER esc(), on the already-escaped string, so
  // the only tags that can appear are the three we emit here. No links, no
  // other tags, no attributes.
  //   **bold** → <b>   *italic* → <i>   `code` → <code>
  function mdLite(escaped) {
    return String(escaped)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .replace(/\*([^*\n]+)\*/g, '<i>$1</i>');
  }

  // ── reasoning trace + clarify chips (shared by palette + command center) ──
  // The backend attaches {reasoning: {plan, consulted, angle}} when the agent
  // planned/consulted, and intent "clarify" (+ choices) when Jarvis asked a
  // question instead of guessing. These render the collapsible "how I got
  // here" block and the quick-reply chips.
  function reasoningHtml(r) {
    const rs = r && r.reasoning;
    if (!rs) return '';
    const plan = rs.plan || [], consulted = rs.consulted || [];
    if (!plan.length && !consulted.length) return '';
    const steps = plan.map(s =>
      `<div class="jv-reason-row">▹ ${esc(String(s.tool || '').replace(/_/g, ' '))}${s.why ? ` <span>— ${esc(s.why)}</span>` : ''}</div>`).join('');
    const used = consulted.map(c =>
      `<div class="jv-reason-row">✓ ${esc(String(c.tool || '').replace(/_/g, ' '))}${c.note ? ` <span>— ${esc(c.note)}</span>` : ''}</div>`).join('');
    const angle = rs.angle ? `<div class="jv-reason-row">◆ <span>${esc(rs.angle)}</span></div>` : '';
    return `<div class="jv-reason">
      <button class="jv-reason-toggle" type="button" aria-expanded="false">◉ how I got here ▾</button>
      <div class="jv-reason-body" hidden>
        ${steps ? `<div class="jv-reason-h">planned</div>${steps}` : ''}
        ${used ? `<div class="jv-reason-h">consulted</div>${used}` : ''}
        ${angle}
      </div></div>`;
  }

  function wireReasoning(rootEl) {
    if (!rootEl) return;
    rootEl.querySelectorAll('.jv-reason-toggle').forEach(btn => {
      btn.addEventListener('click', () => {
        const body = btn.parentElement && btn.parentElement.querySelector('.jv-reason-body');
        if (!body) return;
        const opening = body.hidden;
        body.hidden = !opening;
        btn.setAttribute('aria-expanded', String(opening));
        btn.textContent = opening ? '◉ how I got here ▴' : '◉ how I got here ▾';
      });
    });
  }

  function clarifyChipsHtml(r) {
    if (!r || r.intent !== 'clarify' || !(r.choices || []).length) return '';
    return `<div class="jv-starters jv-clarify-chips">${r.choices.slice(0, 6).map(c =>
      `<button class="jv-starter jv-chip-reply" type="button" data-q="${esc(c)}">${esc(c)}</button>`).join('')}</div>`;
  }

  // ── answer decorations (shared by palette + command center) ───────────────
  // confidence: {level: "high"|"moderate"|"low", note} → small tinted chip
  // next to the answer text; the note rides in the title attribute.
  function confidenceChipHtml(r) {
    const c = r && r.confidence;
    if (!c || !c.level) return '';
    const lv = String(c.level).toLowerCase();
    const color = lv === 'high' ? 'var(--green, #3fb950)'
      : lv === 'low' ? 'var(--red, #f85149)'
      : 'var(--amber, #d29922)';
    return ` <span class="jv-chip jv-confidence" title="${esc(c.note || '')}"`
      + ` style="color:${color};border:1px solid ${color};background:transparent;`
      + `font-size:9px;vertical-align:middle;white-space:nowrap">`
      + `${esc(lv.toUpperCase())} CONFIDENCE</span>`;
  }

  // debate: {bull, bear, verdict} → stacked bull/bear blocks + verdict line,
  // under the main answer.
  function debateHtml(r) {
    const d = r && r.debate;
    if (!d || (!d.bull && !d.bear && !d.verdict)) return '';
    let h = '<div class="jv-debate" style="margin-top:8px">';
    if (d.bull) h += `<div class="jv-obs pos" style="border-left:2px solid var(--green, #3fb950);padding-left:6px;margin:4px 0"><b>▲ BULL CASE</b> — ${esc(d.bull)}</div>`;
    if (d.bear) h += `<div class="jv-obs warn" style="border-left:2px solid var(--red, #f85149);padding-left:6px;margin:4px 0"><b>▼ BEAR CASE</b> — ${esc(d.bear)}</div>`;
    if (d.verdict) h += `<div class="jv-obs info" style="margin:4px 0"><b>⚖ VERDICT</b> — ${esc(d.verdict)}</div>`;
    return h + '</div>';
  }

  // salvaged: true → dim notice that the stream died but the answer was
  // assembled from what had already been gathered.
  function salvagedHtml(r) {
    return (r && r.salvaged)
      ? '<div class="jv-salvaged" style="opacity:.55;font-size:10px;margin-top:6px">'
        + '⚠ the line dropped mid-analysis — answered from what I’d already gathered</div>'
      : '';
  }

  // Batch proposals: payload.proposals = [{tool, args, label}…] (distinct
  // from the singular payload.proposal, which keeps its own CONFIRM flow).
  // Renders a checklist (all checked by default) with one CONFIRM (n) button;
  // wireProposals() attaches the toggle/confirm behavior after innerHTML.
  function proposalsChecklistHtml(r) {
    const list = (r && Array.isArray(r.proposals)) ? r.proposals : [];
    if (!list.length) return '';
    const rows = list.map((p, i) =>
      `<label class="jv-batch-row" style="display:flex;gap:6px;align-items:center;margin:3px 0;cursor:pointer">
         <input type="checkbox" class="jv-batch-ck" data-i="${i}" checked>
         <span>⚡ ${esc(p.label || p.tool || '')}</span>
       </label>`).join('');
    return `<div class="jp-proposal jv-batch" style="display:block">${rows}
      <button class="jarvis-go jv-batch-confirm" type="button">⚡ CONFIRM (${list.length})</button>
    </div>`;
  }

  function wireProposals(rootEl, r) {
    if (!rootEl || !r || !Array.isArray(r.proposals) || !r.proposals.length) return;
    const box = rootEl.querySelector('.jv-batch');
    if (!box) return;
    const btn = box.querySelector('.jv-batch-confirm');
    if (!btn) return;
    const sync = () => {
      const n = box.querySelectorAll('.jv-batch-ck:checked').length;
      btn.textContent = `⚡ CONFIRM (${n})`;
      btn.disabled = n === 0;
    };
    box.querySelectorAll('.jv-batch-ck').forEach(ck => ck.addEventListener('change', sync));
    btn.addEventListener('click', async () => {
      const actions = [];
      box.querySelectorAll('.jv-batch-ck:checked').forEach(ck => {
        const p = r.proposals[Number(ck.dataset.i)];
        if (p) actions.push({ tool: p.tool, args: p.args });
      });
      if (!actions.length) return;
      btn.disabled = true;
      btn.textContent = '⚡ WORKING…';
      try {
        // {actions: [{tool, args}…]} → {results: [{tool, ok, label|error}…], n_ok}
        const out = await API.post('/api/jarvis/act', { actions });
        const results = (out && out.results) || [];
        box.innerHTML = results.length
          ? results.map(res =>
              `<div class="jv-obs ${res.ok ? 'pos' : 'warn'}">${res.ok ? '✓' : '✗'} ${esc(res.ok ? (res.label || res.tool || '') : ((res.error || 'failed') + (res.tool ? ' — ' + res.tool : '')))}</div>`).join('')
          : `<div class="jv-obs pos">✓ ${out && out.n_ok != null ? out.n_ok : actions.length} done.</div>`;
        if (typeof Toast === 'object' && Toast.success && out && out.n_ok != null) {
          Toast.success('◉ JARVIS: ' + out.n_ok + ' of ' + actions.length + ' done.');
        }
      } catch (e) {
        box.innerHTML = `<div class="jv-obs warn">✗ ${esc(e.message)}</div>`;
      }
    });
  }

  // True when the keystroke belongs to a text field — global shortcuts must
  // never hijack typing.
  function isTypingTarget(t) {
    if (!t) return false;
    const tag = (t.tagName || '').toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || t.isContentEditable;
  }

  // Clipboard with the execCommand fallback for older engines.
  function copyToClipboard(text) {
    const legacy = () => {
      try {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.appendChild(ta);
        ta.select();
        const ok = document.execCommand('copy');
        ta.remove();
        if (ok && typeof Toast === 'object') Toast.success('Copied to clipboard');
      } catch (e) { /* clipboard unavailable — stay silent */ }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        () => { if (typeof Toast === 'object') Toast.success('Copied to clipboard'); },
        legacy);
    } else {
      legacy();
    }
  }

  // Poll for a DOM hook after navigate() — views render async. ~10 tries ×
  // 100ms, then give up silently (the plain navigation already happened).
  function pollFor(getEl, run, tries) {
    let left = tries == null ? 10 : tries;
    const tick = () => {
      const el = getEl();
      if (el) { run(el); return; }
      if (--left > 0) setTimeout(tick, 100);
    };
    setTimeout(tick, 100);
  }

  // Navigate to a view, then — if the view exposes the right hook — run an
  // action inside it. Degrades to plain navigate() when hooks never appear.
  function navigateAndRun(view, getHook, run) {
    if (typeof navigate !== 'function') return;
    navigate(view);
    pollFor(getHook, run);
  }

  function runAction(action) {
    if (!action || !action.view) return;
    const sym = action.symbol ? String(action.symbol).toUpperCase() : null;
    if (sym && action.view === 'research' && typeof openResearch === 'function') {
      openResearch(sym); // openResearch is already navigate-and-run
      return;
    }
    if (sym && action.view === 'forecast' && typeof analyzeForecast === 'function') {
      navigateAndRun('forecast',
        () => document.getElementById('forecast-symbol'),
        (input) => { input.value = sym; analyzeForecast(sym); });
      return;
    }
    if (typeof navigate === 'function') navigate(action.view);
  }

  // "stress test" palette command — open the view and press its run button.
  function runStressCommand() {
    navigateAndRun('stress',
      () => document.getElementById('stress-run-btn'),
      (btn) => btn.click());
  }

  const TONE_COLOR = { pos: 'var(--green)', neg: 'var(--red)', warn: 'var(--amber)', info: 'var(--blue)' };

  // ── Streaming ask ───────────────────────────────────────────────────────────
  // POST /api/jarvis/ask/stream over a fetch ReadableStream: SSE-formatted
  // frames, `status` events narrate what Jarvis is doing (tool consults,
  // research, synthesis), one terminal `answer` frame carries the same
  // payload /api/jarvis/ask returns. Throws on any transport/parse trouble so
  // callers fall back to the plain POST — streaming is strictly an upgrade.
  async function askStream(body, onStatus, signal) {
    const resp = await fetch('/api/jarvis/ask/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    });
    if (!resp.ok || !resp.body) throw new Error('stream unavailable (' + resp.status + ')');
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const frame = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 2);
          if (!frame.startsWith('data:')) continue; // comment heartbeat
          let ev;
          try { ev = JSON.parse(frame.slice(5)); } catch (e) { continue; }
          if (ev.type === 'status') {
            if (onStatus) { try { onStatus(ev.text || ''); } catch (e) {} }
          } else if (ev.type === 'answer') {
            return ev.payload || {};
          }
        }
      }
    } finally {
      try { reader.cancel(); } catch (e) {}
    }
    throw new Error('stream ended without an answer');
  }

  // ── Briefing panel ──────────────────────────────────────────────────────────
  const Briefing = {
    _last: null,
    _digest: null, _digestFetched: false,

    // "While you were away" — fetched ONCE per page load (the fetch advances
    // the server-side last-seen watermark, so SPA re-navigations must not
    // re-mark or the recap would self-erase between views).
    async _fetchDigest() {
      if (this._digestFetched) return;
      this._digestFetched = true;
      try { this._digest = await API.get('/api/jarvis/digest'); }
      catch (e) { this._digest = null; this._digestFetched = false; } // allow a retry after a failed fetch
    },

    async render(containerId, force) {
      const el = document.getElementById(containerId);
      if (!el) return;
      el.innerHTML = `
        <div class="panel jarvis-panel">
          <div class="panel-header">
            <span class="panel-title jarvis-title">◉ JARVIS</span>
            <span class="jarvis-hint">⌘K to ask anything</span>
            <span class="jarvis-hint" id="jarvis-updated"></span>
            <button class="btn btn-ghost btn-sm" id="jarvis-speak" title="Read the briefing aloud" aria-label="Read the briefing aloud">🔊</button>
            <button class="btn btn-ghost btn-sm" id="jarvis-refresh">↻</button>
          </div>
          <div class="panel-body" id="jarvis-body">
            <div class="loading"><div class="spinner"></div> Synthesizing your briefing...</div>
          </div>
        </div>`;
      const refreshBtn = document.getElementById('jarvis-refresh');
      if (refreshBtn) refreshBtn.addEventListener('click', () => this.render(containerId, true));
      const speakBtn = document.getElementById('jarvis-speak');
      if (speakBtn) {
        if (!Voice.ttsAvailable()) speakBtn.style.display = 'none';
        speakBtn.addEventListener('click', () => {
          const b = this._last;
          if (b) Voice.speak((b.greeting ? b.greeting + ' ' : '') + (b.voice || b.headline || ''), null, true);
        });
      }
      try {
        const [b] = await Promise.all([
          API.get('/api/jarvis/briefing' + (force ? '?refresh=1' : '')),
          this._fetchDigest(),
        ]);
        this._last = b;
        this._renderBody(b);
        this._startStamp(b.generated_at);
      } catch (e) {
        const body = document.getElementById('jarvis-body');
        if (body) body.innerHTML = `<div class="empty-state"><span>Briefing unavailable: ${esc(e.message)}</span></div>`;
      }
    },

    // Staleness stamp: "updated Xs/Xm ago" in the panel header, derived from
    // the briefing's generated_at and re-painted every 30s while the tab is
    // visible. Self-cancels once the span leaves the DOM (view re-render).
    _stampAt: null, _stampTimer: null,
    _startStamp(generatedAt) {
      const t = generatedAt ? new Date(generatedAt).getTime() : NaN;
      this._stampAt = isNaN(t) ? Date.now() : t;
      if (this._stampTimer) { clearInterval(this._stampTimer); this._stampTimer = null; }
      const paint = () => {
        const node = document.getElementById('jarvis-updated');
        if (!node) {
          if (this._stampTimer) { clearInterval(this._stampTimer); this._stampTimer = null; }
          return;
        }
        const s = Math.max(0, Math.round((Date.now() - this._stampAt) / 1000));
        node.textContent = 'updated ' + (s < 60 ? s + 's' : Math.floor(s / 60) + 'm') + ' ago';
      };
      paint();
      this._stampTimer = setInterval(() => { if (!document.hidden) paint(); }, 30000);
    },

    _renderBody(b) {
      const body = document.getElementById('jarvis-body');
      if (!body) return;
      const cards = (b.insights || []).map((c, i) => {
        const color = TONE_COLOR[c.tone] || 'var(--blue)';
        const pri = c.priority === 1
          ? '<span class="jarvis-pri p1">P1</span>'
          : (c.priority === 2 ? '<span class="jarvis-pri p2">P2</span>' : '');
        const go = c.action ? `<button class="jarvis-go" data-idx="${i}">OPEN →</button>` : '';
        return `<div class="jarvis-card" style="border-left-color:${color}">
          <div class="jarvis-card-head">${pri}<span class="jarvis-card-title">${esc(c.title)}</span>${go}</div>
          <div class="jarvis-card-detail">${esc(c.detail)}</div>
        </div>`;
      }).join('');
      // Prefer the LLM-polished voice when present (it carries the same
      // numbers); the rule-based headline is the keyless/capped fallback.
      const headline = b.voice
        ? `<div class="jarvis-headline jarvis-voice">${esc(b.voice)}</div>`
        : `<div class="jarvis-headline">${esc(b.headline || '')}</div>`;
      const d = this._digest;
      const digest = (d && d.count && d.line)
        ? `<div class="jarvis-digest"><span class="jarvis-digest-h">◉ WHILE YOU WERE OUT</span> ${esc(d.line)}
             <button class="btn btn-ghost btn-sm" id="jarvis-digest-speak" title="Read aloud" aria-label="Read the digest aloud">🔊</button>
             <button class="btn btn-ghost btn-sm" id="jarvis-digest-more" aria-expanded="false">▾ all ${d.count}</button>
             <div id="jarvis-digest-items" hidden></div>
           </div>`
        : '';
      body.innerHTML = `
        <div class="jarvis-greeting">${esc(b.greeting || '')}</div>
        ${digest}
        ${headline}
        ${cards ? `<div class="jarvis-cards">${cards}</div>`
                : '<div class="jarvis-allclear">All clear — nothing needs your attention right now.</div>'}`;
      body.querySelectorAll('.jarvis-go').forEach(btn => {
        btn.addEventListener('click', () => {
          const c = (this._last.insights || [])[Number(btn.dataset.idx)];
          if (c) runAction(c.action);
        });
      });
      if (d && d.count) {
        const spk = document.getElementById('jarvis-digest-speak');
        if (spk) {
          if (!Voice.ttsAvailable()) spk.style.display = 'none';
          spk.addEventListener('click', () => Voice.speak(d.line, null, true));
        }
        const more = document.getElementById('jarvis-digest-more');
        const items = document.getElementById('jarvis-digest-items');
        if (more && items) more.addEventListener('click', () => {
          const opening = items.hidden;
          items.hidden = !opening;
          more.setAttribute('aria-expanded', String(opening));
          if (opening && !items.innerHTML) {
            items.innerHTML = (d.insights || []).slice(0, 10).map((r, i) =>
              `<div class="jv-obs ${esc(r.tone || 'info')}">• ${esc(r.title)}${r.view ? ` <button class="jarvis-go jd-go" data-i="${i}">OPEN →</button>` : ''}</div>`).join('');
            items.querySelectorAll('.jd-go').forEach(btn => btn.addEventListener('click', () => {
              const r = (d.insights || [])[Number(btn.dataset.i)];
              if (r) runAction({ view: r.view, symbol: r.symbol || undefined });
            }));
          }
        });
      }
    },
  };

  // ── Command palette ─────────────────────────────────────────────────────────
  // Client-side palette commands, parsed before falling through to /ask.
  // e.g. "alert NVDA above 190", "alert tsla < 200", "alert AAPL 250"
  const ALERT_CMD = /^alert\s+([A-Za-z.\-]{1,10})\s+(above|below|at|>|<)?\s*\$?(\d+(?:\.\d+)?)$/i;
  const STRESS_CMD = /^stress\s*test$/i;
  const RECAP_CMD = /^(recap|digest|what did i miss\??)$/i;
  const EXPORT_CMD = /^export( conversation| chat)?$/i;
  const WATCHES_CMD = /^(watches|my watches|list watches)$/i;

  // ── Voice — speech out (TTS) and speech in (STT), browser-native ────────────
  // speechSynthesis works everywhere; SpeechRecognition is webkit-prefixed
  // (Chrome/Edge/Safari). Both degrade silently: no STT → no mic button,
  // no TTS → speak() is a no-op that still fires its callback so the
  // conversation loop never stalls.
  const Voice = {
    SR: window.SpeechRecognition || window.webkitSpeechRecognition || null,
    enabled: false,
    _recog: null, _voice: null, _warned: false,

    init() {
      try { this.enabled = localStorage.getItem('jarvis-voice-out') === '1'; } catch (e) {}
      // Voice list loads async in some browsers
      if ('speechSynthesis' in window && speechSynthesis.onvoiceschanged === null) {
        speechSynthesis.onvoiceschanged = () => { this._voice = null; };
      }
    },

    ttsAvailable() { return 'speechSynthesis' in window; },
    sttAvailable() { return !!this.SR; },

    setEnabled(on) {
      this.enabled = !!on;
      try { localStorage.setItem('jarvis-voice-out', on ? '1' : '0'); } catch (e) {}
      if (!on && this.ttsAvailable()) speechSynthesis.cancel();
    },

    _pickVoice() {
      if (this._voice) return this._voice;
      const voices = speechSynthesis.getVoices() || [];
      // Safari/Chrome return [] on the very first call before voices load —
      // don't cache a null pick; leave _voice unset so the next call retries
      // (the system default is used for this one utterance).
      if (!voices.length) return null;
      const prefer = ['Daniel', 'Google UK English Male', 'Samantha', 'Alex'];
      for (const name of prefer) {
        const v = voices.find(v => v.name === name);
        if (v) { this._voice = v; return v; }
      }
      this._voice = voices.find(v => (v.lang || '').startsWith('en')) || voices[0] || null;
      return this._voice;
    },

    // Speak text aloud. `force` bypasses the enabled toggle (explicit 🔊
    // clicks). onend always fires exactly once — TTS missing, disabled,
    // cancelled or errored included — so callers can chain safely.
    // Premium path: server-side OpenAI TTS through the user's key
    // (/api/jarvis/tts). 404 = keyless/capped → browser speech, and we stop
    // asking for the rest of the session. Strictly an upgrade.
    _serverTts: null,   // null = untested, true/false = known
    _audio: null,

    speak(text, onend, force) {
      const done = (() => { let fired = false;
        return () => { if (!fired) { fired = true; if (onend) onend(); } }; })();
      if (!text || (!this.enabled && !force)) { done(); return; }
      this._stopAudio();
      if (this._serverTts !== false) {
        this._speakServer(text, done);
      } else {
        this._speakBrowser(text, done);
      }
    },

    async _speakServer(text, done) {
      // Abort any in-flight synthesis from a previous speak() — without this,
      // two rapid calls fetched two clips and both played over each other,
      // leaking the first Audio (only the second was tracked in _audio).
      if (this._fetchAbort) { try { this._fetchAbort.abort(); } catch (e) {} }
      const ctrl = ('AbortController' in window) ? new AbortController() : null;
      this._fetchAbort = ctrl;
      try {
        const r = await fetch('/api/jarvis/tts', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text: text.slice(0, 600) }),
          signal: ctrl ? ctrl.signal : undefined,
        });
        if (this._fetchAbort === ctrl) this._fetchAbort = null;
        if (!r.ok) {
          if (r.status === 404) this._serverTts = false;  // keyless — stop probing
          this._speakBrowser(text, done);
          return;
        }
        this._serverTts = true;
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        // If a newer speak() superseded us while we awaited the blob, revoke
        // and bail — don't leak the objectURL or start stale audio.
        if (ctrl && this._fetchAbort && this._fetchAbort !== ctrl) {
          URL.revokeObjectURL(url); done(); return;
        }
        this._stopAudio();  // a later call may have started while we awaited
        const a = new Audio(url);
        this._audio = a;
        let revoked = false;
        const finish = () => {
          if (!revoked) { revoked = true; URL.revokeObjectURL(url); }
          if (this._audio === a) this._audio = null;
          done();
        };
        // Cleanup that does NOT fire done — used on the play()-rejection path so
        // the real done can be handed to the browser-TTS fallback instead.
        const cleanup = () => {
          if (!revoked) { revoked = true; URL.revokeObjectURL(url); }
          if (this._audio === a) this._audio = null;
        };
        a.onended = finish;
        a.onerror = finish;
        a.play().catch(() => {
          // Only fall back to browser TTS if playback never actually began —
          // otherwise the server clip is already audible and a second
          // utterance would talk over it.
          if (a.currentTime > 0 && !a.paused) return;  // already playing; let it finish
          cleanup();                       // revoke/clear WITHOUT firing done
          this._speakBrowser(text, done);  // hand the real (unfired) done to fallback
        });
      } catch (e) {
        if (e && e.name === 'AbortError') { done(); return; }  // superseded — don't double-speak
        this._speakBrowser(text, done);
      }
    },

    _speakBrowser(text, done) {
      if (!this.ttsAvailable()) { done(); return; }
      try {
        speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(text);
        const v = this._pickVoice();
        if (v) u.voice = v;
        u.rate = 1.05;
        u.onend = done;
        u.onerror = done;
        speechSynthesis.speak(u);
      } catch (e) { done(); }
    },

    _stopAudio() {
      if (this._fetchAbort) { try { this._fetchAbort.abort(); } catch (e) {} this._fetchAbort = null; }
      if (this._audio) {
        try { this._audio.pause(); this._audio.src = ''; } catch (e) {}
        this._audio = null;
      }
    },

    // One push-to-talk capture. onResult(transcript) on success;
    // onNoSpeech() when nothing usable was heard (timeout, denial, silence).
    listen(onResult, onNoSpeech) {
      if (!this.SR) { Toast.info('Speech input isn’t supported in this browser.'); if (onNoSpeech) onNoSpeech(); return; }
      this.stopListening();
      const r = new this.SR();
      this._recog = r;
      r.lang = navigator.language || 'en-US';
      r.interimResults = false;
      r.maxAlternatives = 1;
      let got = false;
      r.onresult = (ev) => {
        if (this._recog !== r) return; // superseded by a newer listen()
        got = true;
        const t = ev.results[0] && ev.results[0][0] ? ev.results[0][0].transcript.trim() : '';
        if (t) onResult(t); else if (onNoSpeech) onNoSpeech();
      };
      r.onerror = (ev) => {
        if (this._recog !== r) return; // superseded by a newer listen()
        if (ev.error === 'not-allowed' && !this._warned) {
          this._warned = true;
          Toast.warn('Microphone access denied — allow it in your browser to talk to Jarvis.');
        }
        if (!got && onNoSpeech) onNoSpeech();
        got = true;
      };
      r.onend = () => {
        if (this._recog !== r) return; // a newer session owns _recog — don't clobber it
        this._recog = null;
        if (!got && onNoSpeech) onNoSpeech();
        got = true;
      };
      try { r.start(); } catch (e) { if (this._recog === r) this._recog = null; if (onNoSpeech) onNoSpeech(); }
    },

    stopListening() {
      if (this._recog) { try { this._recog.abort(); } catch (e) {} this._recog = null; }
    },

    stopAll() {
      this.stopListening();
      this._stopAudio();
      if (this.ttsAvailable()) try { speechSynthesis.cancel(); } catch (e) {}
    },
  };

  const Palette = {
    el: null, input: null, list: null, answer: null,
    _items: [], _sel: 0, _asking: false, _recent: [], _prevFocus: null,
    _history: [],   // fallback turns {q, a, symbol} if the server thread is unavailable
    _convId: null,  // server-persisted conversation id (v1.4 statefulness)
    _resumeSeq: 0,  // guards _resumeConversation against stale overwrites (F6)
    _askAbort: null, // AbortController for the in-flight _ask stream (F7)

    SUGGESTIONS: [
      'why am I down today',
      "how's my portfolio",
      'what did I miss',
      'how risky is my book',
      'how are markets',
      'am I beating the market',
      'any earnings coming up',
      'any ideas',
      'set a policy: max 25% per name',
      "what if i hadn't sold my last sale",
    ],

    init() {
      if (this.el) return;
      const wrap = document.createElement('div');
      wrap.id = 'jarvis-palette-overlay';
      wrap.innerHTML = `
        <div id="jarvis-palette" role="dialog" aria-modal="true" aria-label="Jarvis command palette">
          <div class="jp-input-row">
            <span class="jp-glyph">◉</span>
            <input id="jp-input" type="text" placeholder="Ask Jarvis or jump anywhere... (e.g. 'forecast NVDA', 'biggest loser today')"
                   autocomplete="off" spellcheck="false">
            <button id="jp-mic" type="button" title="Talk to Jarvis" aria-label="Talk to Jarvis (push to talk)" aria-pressed="false">🎙</button>
            <button id="jp-voice" type="button" title="Jarvis speaks replies aloud" aria-label="Toggle spoken replies" aria-pressed="false">🔊</button>
          </div>
          <div id="jp-answer"></div>
          <div id="jp-list" role="listbox"></div>
          <div class="jp-footer">↑↓ NAVIGATE &nbsp;·&nbsp; ENTER RUN &nbsp;·&nbsp; ESC CLOSE
            <button id="jp-new-thread" type="button" title="Start a fresh conversation">⌫ NEW THREAD</button>
          </div>
        </div>`;
      document.body.appendChild(wrap);
      this.el = wrap;
      this.input = document.getElementById('jp-input');
      this.list = document.getElementById('jp-list');
      this.answer = document.getElementById('jp-answer');

      wrap.addEventListener('click', (e) => { if (e.target === wrap) this.close(); });
      wrap.addEventListener('keydown', (e) => this._trapTab(e));
      this.input.addEventListener('input', () => this._refresh());
      this.input.addEventListener('keydown', (e) => this._onKey(e));

      // Voice controls — mic hidden when the browser has no SpeechRecognition
      Voice.init();
      const mic = document.getElementById('jp-mic');
      const spk = document.getElementById('jp-voice');
      if (!Voice.sttAvailable()) mic.style.display = 'none';
      if (!Voice.ttsAvailable()) spk.style.display = 'none';
      this._syncVoiceBtn();
      mic.addEventListener('click', () => this._listenTurn());
      spk.addEventListener('click', () => {
        Voice.setEnabled(!Voice.enabled);
        this._syncVoiceBtn();
        Toast.info(Voice.enabled ? '◉ JARVIS: voice replies on.' : '◉ JARVIS: voice replies off.');
      });
      document.getElementById('jp-new-thread').addEventListener('click', async () => {
        try {
          this._resumeSeq++; // invalidate any in-flight resume so it can't repaint the old thread
          const r = await API.post('/api/jarvis/conversation/new', {});
          this._convId = r.conversation_id;
          this._history = [];
          this.answer.innerHTML = '';
          Toast.info('◉ JARVIS: fresh thread.');
          this.input.focus();
        } catch (e) { Toast.error(e.message); }
      });
      document.addEventListener('keydown', (e) => {
        if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
          e.preventDefault();
          this.isOpen() ? this.close() : this.open();
        } else if (e.key === '/' && !e.metaKey && !e.ctrlKey && !e.altKey && !e.shiftKey
                   && !this.isOpen() && !isTypingTarget(e.target)) {
          // "/" opens the palette like ⌘K — but never while typing in any
          // input/textarea/contenteditable (app.js's "/" handler defers to us).
          e.preventDefault();
          this.open();
        } else if (e.key === 'Escape' && this.isOpen()) {
          // First Escape clears a typed query; second Escape closes.
          if (this.input && this.input.value) {
            this.input.value = '';
            this._refresh();
          } else {
            this.close();
          }
        }
      });
    },

    isOpen() { return !!(this.el && this.el.classList.contains('open')); },
    open() {
      this._prevFocus = document.activeElement;
      this.el.classList.add('open');
      this.input.value = '';
      this.answer.innerHTML = '';
      this._refresh();
      this.input.focus();
      this._resumeConversation();
    },

    // v1.4 statefulness: pick up the server-persisted thread so the palette
    // reopens mid-conversation, even across page reloads and app restarts.
    async _resumeConversation() {
      const seq = ++this._resumeSeq;
      const convAtStart = this._convId;
      try {
        const c = await API.get('/api/jarvis/conversation');
        // A NEW THREAD (or a newer resume/ask) ran while we awaited — discard
        // this stale response so it can't repaint the old conversation.
        if (seq !== this._resumeSeq || this._convId !== convAtStart) return;
        this._convId = c.conversation_id;
        const msgs = c.messages || [];
        // Rebuild fallback turns and show the tail of the thread, dimmed.
        const turns = [];
        for (let i = 0; i < msgs.length - 1; i++) {
          if (msgs[i].role === 'user' && msgs[i + 1].role === 'assistant') {
            turns.push({ q: msgs[i].content, a: msgs[i + 1].content, symbol: msgs[i + 1].symbol || null });
          }
        }
        this._history = turns.slice(-6);
        // Don't paint the resume tail over anything the user already started:
        // an in-flight ask, a rendered answer, or even typed input.
        if (turns.length && this.isOpen() && !this.answer.innerHTML
            && !this._asking && !this.input.value) {
          const tail = turns.slice(-2).map(t =>
            `<div class="jp-resume-turn"><span class="jp-resume-q">⟩ ${esc(t.q)}</span><br>${esc(t.a.slice(0, 180))}${t.a.length > 180 ? '…' : ''}</div>`).join('');
          this.answer.innerHTML = `<div class="jp-resume">${tail}<div class="jp-resume-hint">— continuing this thread; ⌫ NEW THREAD to reset —</div></div>`;
        }
      } catch (e) { this._convId = null; /* stateless fallback */ }
    },
    close() {
      this.el.classList.remove('open');
      if (this._askAbort) { try { this._askAbort.abort(); } catch (e) {} this._askAbort = null; }
      Voice.stopAll();
      this._setListening(false);
      const prev = this._prevFocus;
      this._prevFocus = null;
      if (prev && typeof prev.focus === 'function' && document.contains(prev)) prev.focus();
    },

    _syncVoiceBtn() {
      const spk = document.getElementById('jp-voice');
      if (!spk) return;
      spk.classList.toggle('on', Voice.enabled);
      spk.setAttribute('aria-pressed', String(Voice.enabled));
    },

    _setListening(on) {
      const row = this.el && this.el.querySelector('.jp-input-row');
      if (row) row.classList.toggle('listening', !!on);
      // Mic button itself: pulsing class (jarvis-pulse keyframes via the
      // existing .jarvis-core rule — globally stilled under
      // prefers-reduced-motion) + aria-pressed for AT users.
      const mic = document.getElementById('jp-mic');
      if (mic) {
        mic.classList.toggle('jarvis-core', !!on);
        mic.setAttribute('aria-pressed', String(!!on));
      }
      if (this.input) this.input.placeholder = on
        ? 'Listening…'
        : "Ask Jarvis or jump anywhere... (e.g. 'forecast NVDA', 'biggest loser today')";
    },

    // One conversational turn: listen → transcribe into the input → ask.
    // After a spoken answer, _ask() re-opens the mic so the exchange keeps
    // flowing until silence, Esc, close — or the auto-turn cap. The cap
    // breaks speakerphone echo loops (TTS output re-captured by the mic
    // would otherwise keep the exchange alive indefinitely).
    _autoTurns: 0,
    _AUTO_TURN_CAP: 6,

    _listenTurn(auto) {
      if (!this.isOpen()) return;
      if (!auto) this._autoTurns = 0;          // manual mic press resets the budget
      else if (++this._autoTurns > this._AUTO_TURN_CAP) {
        Toast.info('◉ JARVIS: pausing the conversation — tap the mic to continue.');
        this._setListening(false);
        return;
      }
      this._setListening(true);
      Voice.listen(
        (transcript) => {
          this._setListening(false);
          this.input.value = transcript;
          this._refresh();
          this._ask(transcript, { spoken: true });
        },
        () => { this._setListening(false); }  // silence/denied → drop back to typing
      );
    },

    // Focus trap: while the palette is open, Tab / Shift+Tab cycle within it.
    _trapTab(e) {
      if (e.key !== 'Tab' || !this.isOpen()) return;
      const focusables = Array.from(
        this.el.querySelectorAll('input, button, [tabindex]:not([tabindex="-1"])')
      ).filter(el => !el.disabled && el.offsetParent !== null);
      if (!focusables.length) { e.preventDefault(); return; }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (e.shiftKey && (active === first || !this.el.contains(active))) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && (active === last || !this.el.contains(active))) {
        e.preventDefault(); first.focus();
      }
    },

    _views() {
      const out = [];
      if (typeof NAV_GROUPS !== 'object') return out;
      for (const g of Object.values(NAV_GROUPS)) {
        for (const it of g.items) out.push({ group: g.label, view: it.view, label: it.label });
      }
      return out;
    },

    _refresh() {
      const q = this.input.value.trim();
      const ql = q.toLowerCase();
      const items = [];

      if (!q) {
        items.push(...this._recent.map(s => ({ kind: 'ask', label: '↺ ' + s, query: s })));
        items.push(...this.SUGGESTIONS
          .filter(s => !this._recent.includes(s))
          .map(s => ({ kind: 'ask', label: s, query: s })));
      } else {
        // Client-side commands — parsed before everything else
        const am = q.match(ALERT_CMD);
        if (am) {
          const sym = am[1].toUpperCase();
          const dir = (am[2] || '').toLowerCase();
          const type = (dir === 'below' || dir === '<') ? 'below' : 'above';
          const price = parseFloat(am[3]);
          items.push({
            kind: 'alert', symbol: sym, alertType: type, price,
            label: `Set alert: ${sym} ${type} ${am[3]}`,
          });
        }
        if (STRESS_CMD.test(q)) {
          items.push({ kind: 'stress', label: 'Run stress test' });
        }
        if (RECAP_CMD.test(q)) {
          items.push({ kind: 'recap', label: 'While you were out — replay the digest' });
        }
        if (EXPORT_CMD.test(q)) {
          items.push({ kind: 'export', label: 'Export conversation as Markdown' });
        }
        if (WATCHES_CMD.test(q)) {
          items.push({ kind: 'watches', label: 'Standing watches — list & manage' });
        }
        // View navigation matches
        for (const v of this._views()) {
          if (v.label.toLowerCase().includes(ql) || v.view.includes(ql)) {
            items.push({ kind: 'nav', label: `${v.group} / ${v.label}`, view: v.view });
          }
        }
        // Ticker-shaped input → instant research open
        if (/^\$?[A-Za-z][A-Za-z.\-]{0,5}$/.test(q)) {
          const sym = q.replace(/^\$/, '').toUpperCase();
          items.push({ kind: 'symbol', label: `Open research: ${sym}`, symbol: sym });
        }
        // Always offer the question route
        items.push({ kind: 'ask', label: `Ask Jarvis: “${q}”`, query: q });
      }

      this._items = items.slice(0, 9);
      this._sel = 0;
      this._renderList();
    },

    _renderList() {
      const ICON = { nav: '→', symbol: '◇', ask: '◉', alert: '⚡', stress: '⚡',
                     recap: '◉', export: '⇩', watches: '◎' };
      const CMD = { alert: 1, stress: 1 };
      this.list.innerHTML = this._items.map((it, i) =>
        `<div class="jp-item${CMD[it.kind] ? ' jp-cmd' : ''}${i === this._sel ? ' sel' : ''}" data-i="${i}" role="option" aria-selected="${i === this._sel}">
           <span class="jp-icon">${ICON[it.kind]}</span>${esc(it.label)}
         </div>`).join('');
      this.list.querySelectorAll('.jp-item').forEach(el => {
        el.addEventListener('click', () => { this._sel = Number(el.dataset.i); this._run(); });
        el.addEventListener('mousemove', () => {
          if (this._sel !== Number(el.dataset.i)) { this._sel = Number(el.dataset.i); this._renderList(); }
        });
      });
    },

    _onKey(e) {
      const n = this._items.length;
      // Arrow navigation wraps around both ends of the list.
      if (e.key === 'ArrowDown') { e.preventDefault(); if (n) { this._sel = (this._sel + 1) % n; this._renderList(); } }
      else if (e.key === 'ArrowUp') { e.preventDefault(); if (n) { this._sel = (this._sel - 1 + n) % n; this._renderList(); } }
      else if (e.key === 'Enter') { e.preventDefault(); this._run(); }
      else if (e.altKey && !e.metaKey && !e.ctrlKey && /^[1-9]$/.test(e.key)) {
        // Alt+1..9 quick-select. Cmd/Ctrl+1..9 is the browser's tab-switch
        // shortcut, so we bind Alt instead; the modifier still keeps plain
        // digits typed into a query from being hijacked.
        const idx = Number(e.key) - 1;
        if ((this.input.value.trim() !== '' || n > 0) && idx < n) {
          e.preventDefault();
          this._sel = idx;
          this._renderList();
          this._run();
        }
      }
    },

    _run() {
      const it = this._items[this._sel];
      if (!it) return;
      // typeof-guard the app.js globals — jarvis.js is a classic script that
      // reads them by name, and an app.js parse error must not crash the
      // palette with a ReferenceError.
      if (it.kind === 'nav') { this.close(); if (typeof navigate === 'function') navigate(it.view); }
      else if (it.kind === 'symbol') { this.close(); if (typeof openResearch === 'function') openResearch(it.symbol); }
      else if (it.kind === 'alert') { this._setAlert(it); }
      else if (it.kind === 'stress') { this.close(); if (typeof runStressCommand === 'function') runStressCommand(); }
      else if (it.kind === 'recap') { this._recap(); }
      else if (it.kind === 'watches') { this._watches(); }
      else if (it.kind === 'export') { this.close(); window.open('/api/jarvis/conversation/export', '_blank'); }
      else if (it.kind === 'ask') { this._ask(it.query); }
    },

    async _recap() {
      this.answer.innerHTML = '<div class="jp-answer thinking"><div class="spinner"></div> Replaying the tape...</div>';
      try {
        // Peek only — the palette recap must not advance the seen-watermark
        // (the Overview digest card owns that).
        const d = await API.get('/api/jarvis/digest?mark_seen=0');
        if (!d.count) {
          this.answer.innerHTML = '<div class="jp-answer"><div class="jp-answer-text">Nothing new since your last look — no alerts tripped, no notable moves logged.</div></div>';
          return;
        }
        const rows = (d.insights || []).slice(0, 8).map(r =>
          `<div class="jv-obs ${esc(r.tone || 'info')}">• ${esc(r.title)}</div>`).join('');
        this.answer.innerHTML = `<div class="jp-answer"><div class="jp-answer-text">${esc(d.line || '')}</div>${rows}</div>`;
      } catch (e) {
        this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
      }
    },

    // "watches" palette command — list standing watches with delete / re-arm.
    // GET /api/jarvis/watches → rows; DELETE /api/jarvis/watches/<id>;
    // POST /api/jarvis/watches/<id>/rearm when a watch has triggered.
    async _watches() {
      this.answer.innerHTML = '<div class="jp-answer thinking"><div class="spinner"></div> Checking the watch floor...</div>';
      let rows;
      try {
        const d = await API.get('/api/jarvis/watches');
        rows = Array.isArray(d) ? d : ((d && (d.watches || d.items)) || []);
      } catch (e) {
        this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
        return;
      }
      if (!rows.length) {
        this.answer.innerHTML = '<div class="jp-answer"><div class="jp-answer-text">No standing watches. Ask me to watch something — “tell me if NVDA drops below 150” — and I’ll keep an eye out.</div></div>';
        return;
      }
      const html = rows.map(w => {
        // Backend emits armed(0/1) + triggered_at; w.state/w.triggered don't exist.
        const triggered = !w.armed || !!w.triggered_at;
        return `<div class="jv-obs ${triggered ? 'warn' : 'info'}" style="display:flex;align-items:center;gap:6px">
          <span style="flex:1">${triggered ? '◆' : '◉'} ${esc(w.description || w.label || ('watch #' + w.id))}
            <span class="jv-chip" style="margin-left:6px">${triggered ? 'TRIGGERED' : 'ARMED'}</span></span>
          ${triggered ? `<button class="jarvis-go jv-watch-rearm" type="button" data-id="${esc(String(w.id))}">RE-ARM</button>` : ''}
          <button class="jarvis-go jp-dismiss jv-watch-del" type="button" data-id="${esc(String(w.id))}" title="Delete watch" aria-label="Delete watch">✕</button>
        </div>`;
      }).join('');
      this.answer.innerHTML = `<div class="jp-answer"><div class="jp-answer-detail">◎ standing watches — ${rows.length}</div>${html}</div>`;
      this.answer.querySelectorAll('.jv-watch-del').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await API.del('/api/jarvis/watches/' + encodeURIComponent(b.dataset.id)); this._watches(); }
        catch (e) { if (typeof Toast === 'object' && Toast.error) Toast.error(e.message); b.disabled = false; }
      }));
      this.answer.querySelectorAll('.jv-watch-rearm').forEach(b => b.addEventListener('click', async () => {
        b.disabled = true;
        try { await API.post('/api/jarvis/watches/' + encodeURIComponent(b.dataset.id) + '/rearm', {}); this._watches(); }
        catch (e) { if (typeof Toast === 'object' && Toast.error) Toast.error(e.message); b.disabled = false; }
      }));
    },

    async _setAlert(it) {
      try {
        // Body shape matches app.js submitAddAlert(): POST /api/alerts
        await API.post('/api/alerts', { symbol: it.symbol, alert_type: it.alertType, price: it.price });
        Toast.success(`Alert set: ${it.symbol} ${it.alertType} $${it.price}`);
        this.close();
      } catch (e) {
        this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
      }
    },

    async _ask(query, opts) {
      if (this._asking) return;
      this._asking = true;
      // Abortable: close() aborts the in-flight stream so a late resolve can't
      // speak/paint after the palette is dismissed.
      if (this._askAbort) { try { this._askAbort.abort(); } catch (e) {} }
      const abort = ('AbortController' in window) ? new AbortController() : null;
      this._askAbort = abort;
      this._recent = [query].concat(this._recent.filter(s => s !== query)).slice(0, 4);
      this.answer.innerHTML = '<div class="jp-answer thinking"><div class="spinner"></div> Working on it...</div>';
      try {
        // Conversation memory: prefer the server-persisted thread (the
        // backend loads its own history); send explicit turns only as the
        // stateless fallback when no conversation id resolved.
        const body = this._convId
          ? { query, conversation_id: this._convId }
          : { query, history: this._history.slice(-6) };
        let r;
        try {
          r = await askStream(body, (t) => {
            if (this.answer) {
              this.answer.innerHTML = `<div class="jp-answer thinking"><div class="spinner"></div> ${esc(t)}</div>`;
            }
          }, abort ? abort.signal : undefined);
        } catch (streamErr) {
          if (streamErr && streamErr.name === 'AbortError') return; // palette closed mid-ask
          r = await API.post('/api/jarvis/ask', body);
        }
        if (r.conversation_id) this._convId = r.conversation_id;
        this._history.push({ q: query, a: r.answer, symbol: r.symbol || null });
        if (this._history.length > 8) this._history.shift();
        const action = r.action
          ? `<button class="jarvis-go" id="jp-answer-go">OPEN →</button>` : '';
        // Agent proposals: mutating actions await explicit confirmation here.
        const proposal = r.proposal
          ? `<div class="jp-proposal">
               <span>⚡ ${esc(r.proposal.label)}</span>
               <button class="jarvis-go" id="jp-confirm">CONFIRM</button>
               <button class="jarvis-go jp-dismiss" id="jp-dismiss">DISMISS</button>
             </div>` : '';
        const usedLine = (r.used && r.used.length)
          ? `<div class="jp-answer-detail">◉ consulted: ${esc([...new Set(r.used)].join(', ').replace(/_/g, ' '))}</div>` : '';
        const citesLine = (r.citations && r.citations.length)
          ? `<div class="jv-cites"><span class="jv-cites-h">sources</span>${r.citations.slice(0,6).map(c =>
                (/^https?:\/\//i.test(c.url || '')
                  ? `<a class="jv-cite" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.title || c.url)}</a>`
                  : `<span class="jv-cite">${esc(c.title || c.url || '')}</span>`)).join('')}</div>` : '';
        this.answer.innerHTML = `
          <div class="jp-answer${r.intent === 'clarify' ? ' jv-clarify' : ''}">
            <div class="jp-answer-text">${mdLite(esc(r.answer))}${confidenceChipHtml(r)}</div>
            ${clarifyChipsHtml(r)}
            ${debateHtml(r)}
            ${salvagedHtml(r)}
            ${r.detail ? `<div class="jp-answer-detail">${esc(r.detail)}</div>` : ''}
            ${citesLine}
            ${reasoningHtml(r)}
            ${usedLine}
            ${proposal}
            ${proposalsChecklistHtml(r)}
            ${action}
          </div>`;
        wireReasoning(this.answer);
        wireProposals(this.answer, r);
        this.answer.querySelectorAll('.jv-chip-reply').forEach(b =>
          b.addEventListener('click', () => this._ask(b.dataset.q)));
        const go = document.getElementById('jp-answer-go');
        if (go) go.addEventListener('click', () => { this.close(); runAction(r.action); });
        const confirmBtn = document.getElementById('jp-confirm');
        if (confirmBtn) confirmBtn.addEventListener('click', async () => {
          try {
            const out = await API.post('/api/jarvis/act', { tool: r.proposal.tool, args: r.proposal.args });
            Toast.success('◉ JARVIS: done — ' + (out.label || r.proposal.label));
            this.answer.innerHTML = `<div class="jp-answer"><div class="jp-answer-text">Done. ${esc(out.label || r.proposal.label)}.</div></div>`;
          } catch (e) {
            this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
          }
        });
        const dismissBtn = document.getElementById('jp-dismiss');
        if (dismissBtn) dismissBtn.addEventListener('click', () => { this.answer.innerHTML = ''; });
        // Speak the reply; after a spoken question, hand the mic back for
        // the follow-up — that's the back-and-forth loop.
        const spoken = opts && opts.spoken;
        // Don't speak into a closed palette (the ask may have resolved after close()).
        if (this.isOpen()) {
          Voice.speak(r.answer, () => {
            if (spoken && Voice.enabled && this.isOpen()) this._listenTurn(true);
          }, spoken);
        }
      } catch (e) {
        if (e && e.name === 'AbortError') return; // palette closed mid-ask
        this.answer.innerHTML = `<div class="jp-answer error">${esc(e.message)}</div>`;
      } finally {
        if (this._askAbort === abort) this._askAbort = null;
        this._asking = false;
      }
    },
  };

  // ── Context strip — Jarvis speaks on every view ─────────────────────────────
  // A persistent line under the sub-nav, refreshed on each navigate() with a
  // short typewriter reveal. Research view includes the active symbol.
  const Strip = {
    el: null, _seq: 0, _timer: null,

    ensure() {
      if (this.el && document.body.contains(this.el)) return;
      // Recreating after a DOM replacement — stop any typewriter still
      // ticking against the detached node.
      this._stop();
      const subNav = document.getElementById('sub-nav');
      if (!subNav || !subNav.parentNode) return;
      const bar = document.createElement('div');
      bar.id = 'jarvis-strip';
      // The typed span rewrites textContent every frame — keep it silent for
      // screen readers; the sr-only sibling receives the full line exactly
      // once, when typing completes.
      bar.innerHTML = '<span class="js-glyph" aria-hidden="true">◉</span>'
        + '<span id="jarvis-strip-text" aria-live="off" aria-hidden="true"></span>'
        + '<span class="js-caret" aria-hidden="true"></span>'
        + '<span id="jarvis-strip-sr" class="sr-only" role="status" aria-live="polite"></span>';
      subNav.parentNode.insertBefore(bar, subNav.nextSibling);
      this.el = bar;
    },

    async update(view) {
      this.ensure();
      if (!this.el) return;
      const seq = ++this._seq;
      let url = '/api/jarvis/context/' + encodeURIComponent(view);
      if (view === 'research' && State.researchSymbol) {
        url += '?symbol=' + encodeURIComponent(State.researchSymbol);
      }
      try {
        const r = await API.get(url);
        if (seq !== this._seq || !r || !r.line) return;
        this._type(r.line, r.tone || 'info');
      } catch (e) { /* server busy — keep the previous line */ }
    },

    _type(text, tone) {
      const out = document.getElementById('jarvis-strip-text');
      if (!out) return;
      this._stop();
      const sr = document.getElementById('jarvis-strip-sr');
      if (sr) sr.textContent = '';
      this.el.dataset.tone = tone;
      // Respect prefers-reduced-motion: skip the typewriter reveal and set the
      // full line immediately (no rAF loop / no "typing" animation state).
      if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        out.textContent = text;
        this.el.classList.remove('typing');
        if (sr) sr.textContent = text;
        this._timer = null;
        return;
      }
      this.el.classList.add('typing');
      let i = 0;
      out.textContent = '';
      // rAF instead of setInterval(16ms): coalesces to the real display
      // refresh (one paint per frame, not a fixed timer that can stack two
      // writes into one frame) and self-suspends on a backgrounded tab, so
      // a mid-type navigate-away stops doing work immediately. _timer holds
      // the rAF handle; _stop() cancels via the matching path.
      const step = () => {
        i += 3; // 3 chars/frame ≈ fast but visibly "spoken"
        out.textContent = text.slice(0, i);
        if (i >= text.length) {
          this._timer = null;
          this.el.classList.remove('typing');
          if (sr) sr.textContent = text; // announce the full line once
          return;
        }
        this._timer = requestAnimationFrame(step);
      };
      this._timer = requestAnimationFrame(step);
    },

    // Cancel an in-flight typewriter. _timer only ever holds a rAF handle, so
    // cancel solely via cancelAnimationFrame — calling clearInterval on a rAF
    // id could clear an unrelated app interval that shares the numeric id.
    _stop() {
      if (this._timer == null) return;
      cancelAnimationFrame(this._timer);
      this._timer = null;
    },
  };

  // ── Activity engine — every API call is Jarvis working; show it ────────────
  // Wraps the global API helper so each request becomes a visible operation
  // in the neural-activity panel, with in-flight / done / failed states and
  // timings. The orb (bottom-right) breathes when idle, spins up when busy.
  const OP_PATTERNS = [
    [/^\/api\/quote\/([^/?]+)/,            (m) => 'Pulling live quote · ' + m[1].toUpperCase()],
    [/^\/api\/quotes/,                     () => 'Refreshing quote board'],
    [/^\/api\/chart\/([^/?]+)/,            (m) => 'Rendering price history · ' + m[1].toUpperCase()],
    [/^\/api\/fundamentals\/([^/?]+)/,     (m) => 'Studying fundamentals · ' + m[1].toUpperCase()],
    [/^\/api\/news/,                       () => 'Scanning the wires'],
    [/^\/api\/market\/indices/,            () => 'Sampling global indices'],
    [/^\/api\/market\/sectors/,            () => 'Mapping sector performance'],
    [/^\/api\/market\/movers/,             () => 'Hunting market movers'],
    [/^\/api\/portfolio\/ai-analysis/,     () => 'Deep-reading your portfolio'],
    [/^\/api\/portfolio/,                  () => 'Auditing your book'],
    [/^\/api\/analytics/,                  () => 'Crunching portfolio math'],
    [/^\/api\/forecast\/ensemble\/([^/?]+)/, (m) => 'Fusing forecast engines · ' + m[1].toUpperCase()],
    [/^\/api\/forecast\/accountability/,   () => 'Auditing my own track record'],
    [/^\/api\/jarvis\/briefing/,           () => 'Synthesizing your briefing'],
    [/^\/api\/jarvis\/ask/,                () => 'Reasoning on your question'],
    [/^\/api\/jarvis\/context/,            () => 'Reading the room'],
    [/^\/api\/jarvis\/activity/,           null],  // don't log the logger
    [/^\/api\/intel/,                      () => 'Decoding SEC filings'],
    [/^\/api\/earnings/,                   () => 'Checking the earnings calendar'],
    [/^\/api\/crypto/,                     () => 'Polling crypto markets'],
    [/^\/api\/synth\//,                    () => 'Running synthesis engines'],
    [/^\/api\/research\//,                 () => 'Working the research lab'],
    [/^\/api\/(options-flow|gex)/,         () => 'Parsing options flow'],
    [/^\/api\/alerts/,                     () => 'Checking tripwires'],
    [/^\/api\/watchlist/,                  () => 'Sweeping the watchlist'],
    [/^\/api\/macro/,                      () => 'Reading macro indicators'],
    [/^\/api\/stress-test/,                () => 'Stress-testing your book'],
    [/^\/api\/smart-money/,                () => 'Tracing smart money'],
    [/^\/api\/congress/,                   () => 'Reading congressional filings'],
    [/^\/api\/search/,                     () => 'Searching the universe'],
  ];

  const FAIL_WINDOW_MS = 60 * 1000;       // rolling failure window
  const FAIL_THRESHOLD = 4;               // failures of one feed within the window
  const FAIL_ANNOUNCE_COOLDOWN_MS = 5 * 60 * 1000;

  const Activity = {
    ops: [], _id: 0, _inflight: 0, _raf: false,
    _failures: {},                        // feed key -> [timestamps]
    _lastFailAnnounce: 0,

    describe(path) {
      for (const [re, fn] of OP_PATTERNS) {
        const m = path.match(re);
        if (m) return fn ? fn(m) : null;
      }
      return 'Processing ' + path.replace(/^\/api\//, '').split('?')[0];
    },

    begin(path) {
      const desc = this.describe(path);
      if (desc === null) return null;
      const op = { id: ++this._id, desc, t0: performance.now(), state: 'run', ms: null };
      this.ops.unshift(op);
      if (this.ops.length > 40) this.ops.pop();
      this._inflight++;
      this._schedule();
      return op;
    },

    end(op, ok) {
      if (!op) return;
      op.state = ok ? 'ok' : 'err';
      op.ms = Math.round(performance.now() - op.t0);
      this._inflight = Math.max(0, this._inflight - 1);
      this._pruneFailures();              // keep the window memory bounded
      if (!ok) this._noteFailure(op.desc);
      this._schedule();
    },

    // ── Error awareness — Jarvis speaks up when a feed keeps failing ─────────
    // Failures are keyed by the feed name: the op description's first
    // word-group (the part before the " · SYMBOL" suffix), so e.g. all
    // "Pulling live quote · NVDA/AAPL/..." failures pool together.
    _failKey(desc) {
      return String(desc || '').split('·')[0].trim() || 'API feed';
    },

    _noteFailure(desc) {
      const key = this._failKey(desc);
      const now = Date.now();
      const arr = this._failures[key] || (this._failures[key] = []);
      arr.push(now);
      if (arr.length >= FAIL_THRESHOLD
          && now - this._lastFailAnnounce > FAIL_ANNOUNCE_COOLDOWN_MS) {
        this._lastFailAnnounce = now;
        if (typeof Toast === 'object' && Toast.warn) {
          Toast.warn('◉ JARVIS: ' + key + ' is struggling — data may be stale or rate-limited.');
        }
      }
    },

    _pruneFailures() {
      const cutoff = Date.now() - FAIL_WINDOW_MS;
      for (const key of Object.keys(this._failures)) {
        const kept = this._failures[key].filter(t => t >= cutoff);
        if (kept.length) this._failures[key] = kept;
        else delete this._failures[key];
      }
    },

    instrument() {
      if (API.__jarvis) return;
      API.__jarvis = true;
      for (const method of ['get', 'post', 'put', 'del']) {
        const orig = API[method].bind(API);
        API[method] = async (path, body) => {
          const op = this.begin(String(path));
          try {
            const r = await orig(path, body);
            this.end(op, true);
            return r;
          } catch (e) {
            this.end(op, false);
            throw e;
          }
        };
      }
    },

    _schedule() {
      if (this._raf) return;
      this._raf = true;
      requestAnimationFrame(() => { this._raf = false; Orb.render(); });
    },
  };

  // ── Orb dock + neural activity panel ────────────────────────────────────────
  const Orb = {
    el: null, panel: null, _open: false, _bgTimer: null,
    _es: null, _sseFailed: false, _flashTimer: null,

    init() {
      if (this.el) return;
      const orb = document.createElement('button');
      orb.id = 'jarvis-orb';
      orb.title = 'Jarvis neural activity';
      orb.setAttribute('aria-label', 'Toggle Jarvis activity panel');
      orb.setAttribute('aria-expanded', 'false');
      orb.setAttribute('aria-controls', 'jarvis-activity-panel');
      orb.innerHTML = '<span class="orb-ring"></span><span class="orb-core"></span><span class="orb-count" id="orb-count"></span>';
      orb.addEventListener('click', () => this.toggle());
      document.body.appendChild(orb);
      this.el = orb;

      const panel = document.createElement('div');
      panel.id = 'jarvis-activity-panel';
      panel.setAttribute('role', 'region');
      panel.setAttribute('aria-label', 'Jarvis neural activity');
      panel.innerHTML = `
        <div class="jap-header"><span class="jarvis-core">◉</span> JARVIS // NEURAL ACTIVITY
          <button class="jap-close" aria-label="Close">×</button></div>
        <div class="jap-bg" id="jap-bg">Background systems nominal</div>
        <div class="jap-list" id="jap-list"><div class="jap-empty">Standing by. Operations appear here as I work.</div></div>`;
      panel.querySelector('.jap-close').addEventListener('click', () => this.toggle(false));
      document.body.appendChild(panel);
      this.panel = panel;
    },

    toggle(force) {
      this._open = force !== undefined ? force : !this._open;
      this.panel.classList.toggle('open', this._open);
      if (this.el) this.el.setAttribute('aria-expanded', String(this._open));
      if (this._open) {
        this.render();
        this._sseFailed = false; // reopening the panel re-earns an SSE attempt
        this._startBackground();
      } else {
        this._stopBackground();
      }
    },

    // ── Background section: SSE first, 60s polling fallback ──────────────────
    // The server emits the first snapshot immediately on connect, then only
    // on change; the connection hard-stops server-side every 10 min and the
    // browser's EventSource transparently reconnects.
    _startBackground() {
      if (typeof EventSource === 'undefined' || this._sseFailed) {
        this._startPolling();
        return;
      }
      let es;
      try {
        es = new EventSource('/api/jarvis/activity/stream');
      } catch (e) {
        this._sseFailed = true;
        this._startPolling();
        return;
      }
      this._es = es;
      es.onmessage = (ev) => {
        try {
          const a = JSON.parse(ev.data);
          this._renderBackground(a);
          this._flash();
        } catch (err) { /* malformed frame — keep previous */ }
      };
      es.onerror = () => {
        // CONNECTING = the browser is auto-reconnecting (e.g. the server's
        // 10-min hard stop) — let it. CLOSED = SSE is genuinely unavailable
        // (404/500/wrong mimetype): fall back to polling and don't retry
        // SSE until the panel is reopened.
        if (this._es && this._es.readyState !== EventSource.CLOSED) return;
        this._stopSse();
        this._sseFailed = true;
        if (this._open) this._startPolling();
      };
    },

    _startPolling() {
      if (this._bgTimer) return;
      this._refreshBackground();
      this._bgTimer = setInterval(() => {
        if (!document.hidden) this._refreshBackground();
      }, 60000);
    },

    _stopSse() {
      if (this._es) {
        try { this._es.close(); } catch (e) { /* already dead */ }
        this._es = null;
      }
    },

    _stopBackground() {
      this._stopSse();
      if (this._bgTimer) { clearInterval(this._bgTimer); this._bgTimer = null; }
      if (this._flashTimer) { clearTimeout(this._flashTimer); this._flashTimer = null; }
      if (this.el) this.el.classList.remove('sse-flash');
    },

    // Live-data touch: flash the orb for ~600ms when an SSE frame lands.
    _flash() {
      if (!this.el) return;
      if (global.matchMedia
          && global.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      if (this._flashTimer) clearTimeout(this._flashTimer);
      this.el.classList.add('sse-flash');
      this._flashTimer = setTimeout(() => {
        this._flashTimer = null;
        if (this.el) this.el.classList.remove('sse-flash');
      }, 600);
    },

    // Shared renderer for the background section (SSE frames + poll results).
    _renderBackground(a) {
      const bg = document.getElementById('jap-bg');
      if (!bg || !a) return;
      const lines = (a.background || []).slice(0, 4)
        .map(b => `<div class="jap-bg-row">▹ ${esc(b.label)} <span>${esc(b.when || '')}</span></div>`).join('');
      bg.innerHTML = `<div class="jap-bg-sum">${esc(a.summary || '')}</div>${lines}`;
    },

    async _refreshBackground() {
      try {
        const a = await API.get('/api/jarvis/activity');
        this._renderBackground(a);
      } catch (e) { /* leave previous */ }
      this._refreshHealth();
    },

    // Systems vitals: AI budget meter + warmer/cache liveness, rendered as a
    // strip under the background section while the panel is open.
    async _refreshHealth() {
      try {
        const h = await API.get('/api/jarvis/health');
        const bg = document.getElementById('jap-bg');
        if (!bg || !h) return;
        let row = document.getElementById('jap-health');
        if (!row) {
          row = document.createElement('div');
          row.id = 'jap-health';
          row.className = 'jap-health';
          bg.insertAdjacentElement('afterend', row);
        }
        const bits = [];
        const ai = h.ai || {};
        if (ai.key && ai.daily_cap) {
          const used = ai.calls_today || 0;
          const pct = Math.min(100, Math.round(used / ai.daily_cap * 100));
          bits.push(`<span class="jap-meter" title="AI calls today">AI ${used}/${ai.daily_cap}` +
            `<span class="jap-meter-bar"><span style="width:${pct}%"${pct >= 90 ? ' class="hot"' : ''}></span></span></span>`);
        } else {
          bits.push('<span class="jap-meter">AI OFFLINE — local engines only</span>');
        }
        const w = h.warmer || {};
        bits.push(`<span>WARMER ${w.alive ? '◉ LIVE' : '○ idle'}</span>`);
        if (h.cache && h.cache.on_disk) bits.push(`<span>${Number(h.cache.on_disk).toLocaleString()} facts</span>`);
        if (h.memory_facts) bits.push(`<span>${h.memory_facts} memories</span>`);
        row.innerHTML = bits.join('<span class="jap-dot">·</span>');
      } catch (e) { /* vitals are decorative — never break the panel */ }
    },

    render() {
      if (!this.el) return;
      this.el.classList.toggle('busy', Activity._inflight > 0);
      const count = document.getElementById('orb-count');
      if (count) {
        count.textContent = Activity._inflight > 0 ? String(Activity._inflight) : '';
      }
      if (!this._open) return;
      const list = document.getElementById('jap-list');
      if (!list) return;
      const ICON = { run: '<span class="jap-run"></span>', ok: '✓', err: '✗' };
      list.innerHTML = Activity.ops.length
        ? Activity.ops.map(o =>
            `<div class="jap-row ${o.state}">
               <span class="jap-ic">${ICON[o.state]}</span>
               <span class="jap-desc">${esc(o.desc)}</span>
               <span class="jap-ms">${o.ms != null ? o.ms + 'ms' : ''}</span>
             </div>`).join('')
        : '<div class="jap-empty">Standing by. Operations appear here as I work.</div>';
    },
  };

  // ── Proactive watch — Jarvis speaks up when a NEW urgent insight appears ────
  // Polls the (server-cached) briefing every 5 min and toasts P1 items it
  // hasn't announced yet. First poll seeds the seen-set silently so a page
  // load doesn't re-announce what the overview panel already shows.
  const Watch = {
    _seen: null,
    _timer: null,
    start() {
      if (this._timer) return;
      const tick = async () => {
        // A backgrounded tab does zero network — this hits the AI-budgeted
        // briefing endpoint, so guard it like app.js guards its timers.
        if (document.hidden) return;
        try {
          const b = await API.get('/api/jarvis/briefing');
          const p1 = (b.insights || []).filter(c => c.priority === 1);
          if (this._seen === null) {
            this._seen = new Set(p1.map(c => c.title));
            return;
          }
          for (const c of p1) {
            if (!this._seen.has(c.title)) {
              this._seen.add(c.title);
              // Actionable toast: "OPEN →" deep-links into the insight's view
              // (Toast.show's optional 4th arg, added in app.js).
              Toast.show('◉ JARVIS: ' + c.title, 'amber', 8000,
                c.action ? { label: 'OPEN →', onClick: () => runAction(c.action) } : undefined);
            }
          }
        } catch (e) { /* server asleep or offline — stay quiet */ }
      };
      tick();
      this._timer = setInterval(tick, 5 * 60 * 1000);
    },
  };

  // ── Digest freshness — re-fetch "while you were away" after a long absence ──
  // If the tab was hidden for more than 30 minutes, re-pull the digest on
  // return (default mark_seen, advancing the watermark), toast a one-liner
  // when something happened, and refresh the Overview briefing if that view
  // is what the user comes back to.
  const AWAY_THRESHOLD_MS = 30 * 60 * 1000;
  const Freshness = {
    _hiddenAt: null,   // module-level timestamp of when the page went hidden
    init() {
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) { this._hiddenAt = Date.now(); return; }
        const away = this._hiddenAt ? Date.now() - this._hiddenAt : 0;
        this._hiddenAt = null;
        if (away < AWAY_THRESHOLD_MS) return;
        this._refetch();
      });
    },
    async _refetch() {
      try {
        const d = await API.get('/api/jarvis/digest');
        if (!d || !d.count) return;
        // Hand the fresh digest to the briefing panel (it normally fetches
        // only once per page load) before re-rendering.
        Briefing._digest = d;
        Briefing._digestFetched = true;
        if (typeof Toast === 'object' && Toast.info) {
          Toast.info('◉ JARVIS: while you were away — ' + (d.line || (d.count + ' things happened.')));
        }
        if (typeof State === 'object' && State && State.activeView === 'overview'
            && document.getElementById('ov-jarvis')) {
          Briefing.render('ov-jarvis');
        }
      } catch (e) { /* server asleep — stay quiet */ }
    },
  };

  // ── JARVIS command center — a full view, not just a palette ────────────────
  // Left: persistent chat with the same conversation thread the palette uses
  // (mic, premium voice, proposals with confirm). Right: the investor lenses —
  // macro brief, temperament check, position review, memory manager.
  const CommandCenter = {
    _convId: null, _busy: false, _chatGen: 0,

    async load() {
      const view = document.getElementById('view-jarvis');
      if (!view) return;
      // One Voice consumer at a time: close the palette and stop any of its
      // in-flight speech/mic before this view takes over — otherwise both
      // surfaces drive the single Voice singleton and cross-talk.
      if (Palette.isOpen && Palette.isOpen()) Palette.close();
      Voice.stopAll();
      // load() runs on every navigation to the view and rebuilds the DOM.
      // Bump the generation so a still-in-flight _loadChat from a prior
      // visit can't overwrite the fresh chat, and clear any stranded send
      // lock (a send awaiting when the user navigated away leaves _busy true).
      this._chatGen++;
      // Abort a still-streaming ask from a prior visit before clearing the lock,
      // otherwise its late resolve would paint into the rebuilt DOM / break STOP.
      if (this._abort) { try { this._abort.abort(); } catch (e) {} this._abort = null; }
      this._busy = false;
      view.innerHTML = `
        <div class="jv-grid">
          <div class="panel jv-chat-panel">
            <div class="panel-header"><span class="panel-title jarvis-title">◉ JARVIS</span>
              <span class="jarvis-hint">your analyst, on the record</span>
              <button class="btn btn-ghost btn-sm" id="jv-voice" title="Read replies aloud" aria-label="Toggle spoken replies" aria-pressed="false">🔊</button>
              <button class="btn btn-ghost btn-sm" id="jv-new-thread">⌫ NEW THREAD</button>
            </div>
            <div class="jv-chat" id="jv-chat" role="log" aria-live="polite" aria-label="Jarvis conversation" aria-busy="false"><div class="loading"><div class="spinner"></div></div></div>
            <div class="jv-input-row">
              <input id="jv-input" type="text" placeholder="Ask anything — your book, a business, the macro picture…"
                     autocomplete="off" spellcheck="false">
              <button id="jv-mic" type="button" title="Talk" aria-label="Talk to Jarvis" aria-pressed="false">🎙</button>
              <button id="jv-send" type="button" class="btn btn-green btn-sm">ASK</button>
            </div>
          </div>
          <div class="jv-side">
            <div class="panel"><div class="panel-header"><span class="panel-title">MACRO BRIEF</span>
              <button class="btn btn-ghost btn-sm" id="jv-macro-speak" title="Read aloud">🔊</button></div>
              <div class="panel-body" id="jv-macro"><div class="loading"><div class="spinner"></div></div></div></div>
            <div class="panel"><div class="panel-header"><span class="panel-title">TEMPERAMENT</span></div>
              <div class="panel-body" id="jv-temperament"><div class="loading"><div class="spinner"></div></div></div></div>
            <div class="panel"><div class="panel-header"><span class="panel-title">POSITION REVIEW</span></div>
              <div class="panel-body" id="jv-review">
                <div style="display:flex;gap:6px"><input class="form-input" id="jv-review-symbol" placeholder="Symbol…" style="flex:1">
                <button class="btn btn-green btn-sm" id="jv-review-go">REVIEW</button></div>
                <div id="jv-review-picks" style="margin-top:6px"></div>
                <div id="jv-review-out"></div></div></div>
            <div class="panel"><div class="panel-header"><span class="panel-title">JARVIS REMEMBERS</span></div>
              <div class="panel-body" id="jv-memory"><div class="loading"><div class="spinner"></div></div></div></div>
          </div>
        </div>`;

      document.getElementById('jv-send').addEventListener('click', () => {
        // Busy → the button is ■ STOP: abort the stream instead of re-asking.
        if (this._busy) { if (this._abort) this._abort.abort(); return; }
        this._send();
      });
      document.getElementById('jv-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') this._send();
      });
      const jvMic = document.getElementById('jv-mic');
      if (!Voice.sttAvailable()) {
        jvMic.style.display = 'none';  // dead button on browsers without STT
      } else {
        // Listening state: pulsing class (existing .jarvis-core rule, stilled
        // globally under prefers-reduced-motion) + aria-pressed, cleared on
        // result, silence and error alike (Voice.listen fires exactly one of
        // the two callbacks).
        const micState = (on) => {
          const m = document.getElementById('jv-mic');
          if (!m) return;
          m.classList.toggle('jarvis-core', on);
          m.setAttribute('aria-pressed', String(on));
        };
        jvMic.addEventListener('click', () => {
          micState(true);
          Voice.listen(
            (t) => { micState(false); const inp = document.getElementById('jv-input'); if (inp) inp.value = t; this._send(true); },
            () => micState(false));
        });
      }
      // Copy affordance: hovering any assistant bubble lazily attaches a ⧉
      // button (delegated, so bubbles created or rewritten later get it too).
      const chatBox = document.getElementById('jv-chat');
      if (chatBox) chatBox.addEventListener('mouseover', (e) => {
        const b = e.target.closest && e.target.closest('.jv-bubble.assistant');
        if (b && !b.querySelector('.jv-copy-btn')) this._attachCopy(b);
      });
      // Spoken-replies toggle — shares the same persisted flag as the palette,
      // so "voice on" is consistent across both Jarvis surfaces and the
      // command center finally has its own control for it.
      const jvVoice = document.getElementById('jv-voice');
      if (jvVoice) {
        if (!Voice.ttsAvailable()) jvVoice.style.display = 'none';
        const sync = () => { jvVoice.classList.toggle('on', Voice.enabled);
                             jvVoice.setAttribute('aria-pressed', String(Voice.enabled)); };
        sync();
        jvVoice.addEventListener('click', () => {
          Voice.setEnabled(!Voice.enabled); sync();
          Toast.info(Voice.enabled ? '◉ JARVIS: voice replies on.' : '◉ JARVIS: voice replies off.');
        });
      }
      document.getElementById('jv-new-thread').addEventListener('click', async () => {
        try {
          const r = await API.post('/api/jarvis/conversation/new', {});
          this._convId = r.conversation_id;
          document.getElementById('jv-chat').innerHTML =
            '<div class="jv-bubble assistant">Fresh thread. What are we working on?</div>';
        } catch (e) { Toast.error(e.message); }
      });
      document.getElementById('jv-review-go').addEventListener('click', () => this._review());
      document.getElementById('jv-review-symbol').addEventListener('keydown', (e) => {
        if (e.key === 'Enter') this._review();
      });

      this._loadChat();
      this._loadSide();
    },

    // ⧉ copy button for an assistant bubble — JS-created, shown on hover,
    // copies the bubble's textContent (minus the button itself).
    _attachCopy(bubble) {
      // The button lives inside the bubble and is wiped whenever the bubble's
      // innerHTML is rewritten (e.g. a streaming answer replacing the spinner),
      // so create it unconditionally here. But the bubble-level hover listeners
      // must attach ONCE — guard with dataset.copyWired so repeated re-attaches
      // (after each rewrite) don't pile listeners onto the bubble.
      if (bubble.querySelector('.jv-copy-btn')) return;
      const btn = document.createElement('button');
      btn.className = 'jv-copy-btn';
      btn.type = 'button';
      btn.textContent = '⧉';
      btn.title = 'Copy';
      btn.setAttribute('aria-label', 'Copy this reply');
      btn.style.cssText = 'position:absolute;top:2px;right:2px;'
        + 'background:none;border:1px solid var(--border,#1c1c1c);border-radius:2px;'
        + 'color:var(--text-dim,#888);font-size:10px;line-height:1;padding:2px 4px;'
        + 'cursor:pointer;opacity:0;transition:opacity .15s';
      if (!bubble.style.position) bubble.style.position = 'relative';
      bubble.appendChild(btn);
      const show = () => { btn.style.opacity = '1'; };
      const hide = () => { btn.style.opacity = '0'; };
      // Focus/blur on the (always-created, focusable) button so it's reachable
      // by keyboard — Tab into it reveals it even without a mouse.
      btn.addEventListener('focus', show);
      btn.addEventListener('blur', hide);
      if (!bubble.dataset.copyWired) {
        bubble.dataset.copyWired = '1';
        bubble.addEventListener('mouseenter', () => {
          const b = bubble.querySelector('.jv-copy-btn'); if (b) b.style.opacity = '1';
        });
        bubble.addEventListener('mouseleave', () => {
          const b = bubble.querySelector('.jv-copy-btn'); if (b) b.style.opacity = '0';
        });
      }
      show();  // attached mid-hover — make it visible right away
      btn.addEventListener('click', () => {
        const clone = bubble.cloneNode(true);
        clone.querySelectorAll('.jv-copy-btn').forEach(n => n.remove());
        copyToClipboard((clone.textContent || '').trim());
      });
    },

    // "— Jun 12 —" label from a YYYY-MM-DD prefix; parsed by parts so the
    // label never shifts a day across timezones.
    _dayLabel(day) {
      const p = String(day).split('-').map(Number);
      if (p.length !== 3 || p.some(isNaN)) return day;
      return new Date(p[0], p[1] - 1, p[2])
        .toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    },

    async _loadChat() {
      const gen = this._chatGen;
      try {
        const c = await API.get('/api/jarvis/conversation');
        if (gen !== this._chatGen) return;  // a newer load() superseded us
        this._convId = c.conversation_id;
        const el = document.getElementById('jv-chat');
        if (!el) return;
        const msgs = (c.messages || []).slice(-20);
        if (msgs.length) {
          // Dim day separators between messages from different days
          // (created_at is the server's timestamp on each persisted message).
          let html = '';
          let prevDay = null;
          for (const m of msgs) {
            const day = (m.created_at || '').slice(0, 10);
            if (day && prevDay && day !== prevDay) {
              html += `<div class="jv-day-sep" style="text-align:center;color:var(--text-dim,#666);`
                + `font-size:9px;letter-spacing:.1em;margin:8px 0">— ${esc(this._dayLabel(day))} —</div>`;
            }
            if (day) prevDay = day;
            const body = m.role === 'user' ? esc(m.content) : mdLite(esc(m.content));
            html += `<div class="jv-bubble ${m.role === 'user' ? 'user' : 'assistant'}">${body}</div>`;
          }
          el.innerHTML = html;
        } else {
          // Empty thread → offer clickable starters so the feature is
          // discoverable instead of a blank prompt.
          const starters = ["Why am I down today?", "What did I miss?",
                            "How risky is my book?", "Is AAPL a good business to own?",
                            "Am I beating the market?", "What would you check before earnings?"];
          el.innerHTML = '<div class="jv-bubble assistant">At your service. Ask about your book, a '
            + 'business, or the macro picture — or try one of these:'
            + '<div class="jv-starters">'
            + starters.map(s => `<button class="jv-starter">${esc(s)}</button>`).join('')
            + '</div></div>';
          el.querySelectorAll('.jv-starter').forEach(b => b.addEventListener('click', () => {
            const input = document.getElementById('jv-input');
            if (input) { input.value = b.textContent; this._send(); }
          }));
        }
        el.scrollTop = el.scrollHeight;
      } catch (e) {
        // Don't leave the spinner spinning forever — show an error bubble so the
        // surface is usable (the user can still type/send).
        if (gen !== this._chatGen) return;
        const el = document.getElementById('jv-chat');
        if (el) el.innerHTML = `<div class="jv-bubble assistant"><span class="col-negative">Couldn’t load the conversation — ${esc(e.message)}.</span> Ask me something to start a fresh thread.</div>`;
      }
    },

    _append(role, html) {
      const el = document.getElementById('jv-chat');
      if (!el) return null;
      const div = document.createElement('div');
      div.className = 'jv-bubble ' + role;
      div.innerHTML = html;
      el.appendChild(div);
      el.scrollTop = el.scrollHeight;
      return div;
    },

    async _send(spoken) {
      const input = document.getElementById('jv-input');
      const q = (input.value || '').trim();
      if (!q || this._busy) return;
      this._busy = true;
      input.value = '';
      // Invalidate any in-flight _loadChat so its late resolve can't paint
      // the old thread over the message we're about to send.
      this._chatGen++;
      // Capture our own generation: a later load()/send() bumps _chatGen, and a
      // stale continuation here must not overwrite _convId, speak, or break STOP.
      const gen = this._chatGen;
      this._append('user', esc(q));
      const chatEl = document.getElementById('jv-chat');
      if (chatEl) chatEl.setAttribute('aria-busy', 'true');
      const thinking = this._append('assistant', '<span class="jv-thinking" aria-label="Jarvis is working">◉ working…</span>');
      // While busy, the ASK button becomes ■ STOP and aborts the stream.
      const sendBtn = document.getElementById('jv-send');
      const abort = new AbortController();
      this._abort = abort;
      if (sendBtn) { sendBtn.textContent = '■ STOP'; sendBtn.classList.add('jv-stop'); }
      try {
        const body = { query: q, conversation_id: this._convId };
        let r;
        const trail = [];
        try {
          // Live narration: each status frame appends to a fading trail of
          // what Jarvis is doing ("Reading your portfolio…", "Researching
          // the open web…") with the current step highlighted.
          r = await askStream(body, (t) => {
            if (!thinking || !document.body.contains(thinking)) return;
            trail.push(t);
            const prev = trail.slice(-4, -1).map(s => `<div class="jv-trail">${esc(s)}</div>`).join('');
            thinking.innerHTML = prev + `<span class="jv-thinking">◉ ${esc(t)}</span>`;
          }, abort.signal);
        } catch (streamErr) {
          if (abort.signal.aborted) {
            // User hit STOP — the server finishes and persists the exchange
            // in the background; the thread shows it on next load.
            if (thinking) thinking.innerHTML = '<span class="jv-trail">■ stopped — I\'ll keep the answer in the thread.</span>';
            return;
          }
          // Stream path failed (old server, proxy buffering) — the plain
          // POST is the answer of record.
          r = await API.post('/api/jarvis/ask', body);
        }
        // Superseded by a newer load()/send() while we awaited — don't touch
        // _convId, don't paint, don't speak. The finally also skips cleanup.
        if (gen !== this._chatGen) return;
        if (r.conversation_id) this._convId = r.conversation_id;
        let html = mdLite(esc(r.answer)) + confidenceChipHtml(r);
        html += clarifyChipsHtml(r);
        html += debateHtml(r);
        html += salvagedHtml(r);
        if (r.citations && r.citations.length) {
          html += '<div class="jv-cites"><span class="jv-cites-h">sources</span>'
            + r.citations.slice(0, 6).map(c =>
                (/^https?:\/\//i.test(c.url || '')
                  ? `<a class="jv-cite" href="${esc(c.url)}" target="_blank" rel="noopener noreferrer">${esc(c.title || c.url)}</a>`
                  : `<span class="jv-cite">${esc(c.title || c.url || '')}</span>`)).join('')
            + '</div>';
        }
        html += reasoningHtml(r);
        if (r.used && r.used.length) {
          html += `<div class="jv-used">◉ consulted: ${esc([...new Set(r.used)].join(', ').replace(/_/g, ' '))}</div>`;
        }
        html += proposalsChecklistHtml(r);
        if (thinking) {
          thinking.innerHTML = html;
          if (r.intent === 'clarify') thinking.classList.add('jv-clarify');
          this._attachCopy(thinking); // re-create the copy button the innerHTML rewrite wiped
          wireReasoning(thinking);
          wireProposals(thinking, r);
          thinking.querySelectorAll('.jv-chip-reply').forEach(b =>
            b.addEventListener('click', () => {
              const inp = document.getElementById('jv-input');
              if (inp) { inp.value = b.dataset.q; this._send(); }
            }));
        }
        if (r.proposal) {
          const prop = this._append('assistant',
            `<div class="jp-proposal"><span>⚡ ${esc(r.proposal.label)}</span>
             <button class="jarvis-go jv-confirm">CONFIRM</button></div>`);
          if (prop) prop.querySelector('.jv-confirm').addEventListener('click', async () => {
            // Don't act if the user navigated away (node detached) — the
            // confirmation would run with no visible result.
            if (!document.body.contains(prop)) return;
            try {
              const out = await API.post('/api/jarvis/act', { tool: r.proposal.tool, args: r.proposal.args });
              if (document.body.contains(prop)) prop.innerHTML = `✓ Done — ${esc(out.label || r.proposal.label)}.`;
              else Toast.success('◉ JARVIS: done — ' + (out.label || r.proposal.label));
            } catch (e) { if (document.body.contains(prop)) prop.innerHTML = `<span class="col-negative">${esc(e.message)}</span>`; }
          });
        }
        Voice.speak(r.answer, null, spoken);
      } catch (e) {
        if (gen === this._chatGen && thinking) thinking.innerHTML = `<span class="col-negative">${esc(e.message)}</span>`;
      } finally {
        // Only the current send owns the lock/button — a superseded send must
        // not clear the newer send's _busy/_abort or reset its STOP button.
        if (gen === this._chatGen) {
          this._busy = false;
          this._abort = null;
          const sb = document.getElementById('jv-send');
          if (sb) { sb.textContent = 'ASK'; sb.classList.remove('jv-stop'); }
          if (chatEl) chatEl.setAttribute('aria-busy', 'false');
        }
      }
    },

    async _loadSide() {
      // Capture the generation so a slow lens fetch from a prior visit can't
      // overwrite the current panel after a quick navigate-away-and-back.
      const gen = this._chatGen;
      API.get('/api/jarvis/lens/macro').then(m => {
        if (gen !== this._chatGen) return;
        const el = document.getElementById('jv-macro');
        if (!el) return;
        const chips = [];
        if (m.liquidity && m.liquidity.regime) chips.push(`LIQUIDITY ${esc(String(m.liquidity.regime).toUpperCase())}`);
        if (m.regime && m.regime.regime) chips.push(`VOL ${esc(m.regime.regime)}`);
        el.innerHTML = `<div class="jv-macro-text${m.voice ? ' jarvis-voice' : ''}">${esc(m.voice || m.narrative)}</div>`
          + (chips.length ? `<div class="jv-chips">${chips.map(c => `<span class="jv-chip">${c}</span>`).join('')}</div>` : '');
        const spk = document.getElementById('jv-macro-speak');
        if (spk) spk.onclick = () => Voice.speak(m.voice || m.narrative, null, true);
      }).catch(e => {
        if (gen !== this._chatGen) return;
        const el = document.getElementById('jv-macro');
        if (el) el.innerHTML = `<span class="text-dim" style="font-size:11px">${esc(e.message)}</span>`;
      });

      API.get('/api/jarvis/lens/temperament').then(t => {
        if (gen !== this._chatGen) return;
        const el = document.getElementById('jv-temperament');
        if (!el) return;
        // Glyph carries polarity too (not color alone) for colorblind users.
        const glyph = (tone) => tone === 'warn' ? '▲ ' : (tone === 'pos' ? '✓ ' : '• ');
        el.innerHTML = `<div class="jv-verdict">${esc(t.verdict)}</div>`
          + (t.observations || []).map(o =>
              `<div class="jv-obs ${o.tone}">${glyph(o.tone)}${esc(o.text)}</div>`).join('');
      }).catch(() => {});

      // Position-review quick-picks only need the holdings list. The overview
      // (loadOverview) already fetched /api/portfolio into State.portfolio, so
      // reuse it when present and skip a redundant round-trip on the common
      // overview→jarvis path; fall back to a fetch only if it isn't cached.
      const renderPicks = (p) => {
        // data-attribute + delegated listener, NOT an inline onclick — esc()
        // is an HTML escaper and does not protect a single-quoted JS string
        // (a "'" in a symbol would break out of the argument).
        const picks = ((p && p.holdings) || []).slice(0, 6).map(h =>
          `<button class="btn btn-ghost btn-sm jv-pick" data-sym="${esc(h.symbol)}">${esc(h.symbol)}</button>`).join(' ');
        const el = document.getElementById('jv-review-picks');
        if (gen !== this._chatGen || !el) return;
        el.innerHTML = picks;
        el.querySelectorAll('.jv-pick').forEach(b =>
          b.addEventListener('click', () => this._review(b.dataset.sym)));
      };
      const cached = (typeof State === 'object' && State) ? State.portfolio : null;
      if (cached && cached.holdings) renderPicks(cached);
      else API.get('/api/portfolio').then(renderPicks).catch(() => {});

      this._loadMemory();
    },

    async _review(sym) {
      const input = document.getElementById('jv-review-symbol');
      const symbol = (sym || (input ? input.value : '') || '').trim().toUpperCase();
      if (!symbol) return;
      if (input) input.value = symbol;
      const out = document.getElementById('jv-review-out');
      if (out) out.innerHTML = '<div class="loading"><div class="spinner"></div></div>';
      try {
        const r = await API.get('/api/jarvis/lens/review/' + encodeURIComponent(symbol));
        if (!out) return;
        const q = r.quality || {}; const v = r.valuation || {}; const o = r.ownership || {};
        out.innerHTML = `
          <div class="jv-review">
            <div class="jv-review-head"><b>${esc(r.business.name)}</b>
              <span class="jv-chip">${esc(q.verdict || '—')} ${q.score != null ? q.score : ''}</span>
              <span class="jv-chip">${esc((v.tone || '—').toUpperCase())}</span></div>
            <div class="jv-review-sum">${esc(r.business.summary || '')}</div>
            ${(q.reasons || []).map(x => `<div class="jv-obs pos">● ${esc(x)}</div>`).join('')}
            ${(v.flags || []).map(x => `<div class="jv-obs info">◆ ${esc(x)}</div>`).join('')}
            ${o.held ? `<div class="jv-obs info">◈ You hold ${o.shares} @ $${o.avg_cost}${o.unrealized_pct != null ? ' (' + (o.unrealized_pct >= 0 ? '+' : '') + o.unrealized_pct + '%)' : ''}${o.holding_days != null ? ', ' + o.holding_days + ' days' : ''}</div>` : ''}
            <div class="jv-take">${esc(r.take)}</div>
          </div>`;
      } catch (e) {
        if (out) out.innerHTML = `<span class="col-negative" style="font-size:11px">${esc(e.message)}</span>`;
      }
    },

    async _loadMemory() {
      const el = document.getElementById('jv-memory');
      if (!el) return;
      try {
        const m = await API.get('/api/jarvis/memory');
        const rows = (m.memories || []).map(f =>
          `<div class="jv-mem"><span>${esc(f.fact)}</span>
           <button class="jv-mem-x" data-id="${f.id}" title="Forget" aria-label="Forget">×</button></div>`).join('');
        el.innerHTML = (rows || '<div class="text-dim" style="font-size:11px">Nothing yet. Tell Jarvis “remember: …” and it sticks.</div>')
          + `<div style="display:flex;gap:6px;margin-top:8px">
             <input class="form-input" id="jv-mem-add" placeholder="Teach Jarvis a fact…" style="flex:1">
             <button class="btn btn-ghost btn-sm" id="jv-mem-save">REMEMBER</button></div>`;
        el.querySelectorAll('.jv-mem-x').forEach(btn => btn.addEventListener('click', async () => {
          try { await API.del('/api/jarvis/memory/' + btn.dataset.id); this._loadMemory(); }
          catch (e) { Toast.error(e.message); }
        }));
        const save = document.getElementById('jv-mem-save');
        if (save) save.addEventListener('click', async () => {
          const inp = document.getElementById('jv-mem-add');
          const fact = (inp.value || '').trim();
          if (!fact) return;
          inp.value = '';  // clear on submit
          try {
            // Attach to the active thread so the "remember" turn lands in the
            // user's conversation, not a stateless one.
            await API.post('/api/jarvis/ask',
              { query: 'remember: ' + fact, conversation_id: this._convId });
            this._loadMemory();
            Toast.success('◉ JARVIS: noted.');
          } catch (e) { Toast.error(e.message); }
        });
      } catch (e) {
        el.innerHTML = `<span class="text-dim" style="font-size:11px">${esc(e.message)}</span>`;
      }
    },
  };

  const Jarvis = {
    renderBriefing: (id, force) => Briefing.render(id, force),
    loadView: () => CommandCenter.load(),
    reviewSymbol: (s) => CommandCenter._review(s),
    initPalette: () => Palette.init(),
    openPalette: () => { Palette.init(); Palette.open(); },
    // Called by navigate() / loadResearchFor(). Deferred a tick so callers
    // that set State.researchSymbol right after navigating are picked up.
    onNavigate: (view) => { setTimeout(() => Strip.update(view), 40); },
  };

  global.Jarvis = Jarvis;
  const boot = () => { Activity.instrument(); Orb.init(); Palette.init(); Watch.start(); Freshness.init(); };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})(window);
