/**
 * Browser e2e for AUGUR — drives the locally installed Chrome (puppeteer-core,
 * no bundled browser) through EVERY SPA view and the Jarvis surfaces,
 * collecting uncaught page errors and console errors.
 *
 * Contract: zero page errors (uncaught exceptions) and zero same-origin 5xx
 * responses. Console errors from EXTERNAL origins (Yahoo rate limits, ad
 * blockers, favicon) are warnings, not failures.
 *
 * Usage: node e2e_browser.js [baseUrl]   (default http://127.0.0.1:5099)
 * Exit:  0 when zero failures, 1 otherwise.
 */
const puppeteer = require('puppeteer-core');

const BASE = process.argv[2] || 'http://127.0.0.1:5099';
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

const failures = [];
const warnings = [];

function fail(where, what) { failures.push(`[${where}] ${what}`); console.log(`  ✗ [${where}] ${what}`); }
function warn(where, what) { warnings.push(`[${where}] ${what}`); }
function pass(what) { console.log(`  ✓ ${what}`); }

(async () => {
  const browser = await puppeteer.launch({
    executablePath: CHROME,
    headless: 'new',
    args: ['--no-first-run', '--disable-extensions', '--window-size=1500,950'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1500, height: 950 });

  let currentView = 'boot';
  page.on('pageerror', (err) => fail(currentView, `pageerror: ${String(err).slice(0, 220)}`));
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // External-origin noise (quote upstreams, favicons) is not our failure.
    if (/net::|favicon|ERR_BLOCKED|fonts\.g/i.test(text)) { warn(currentView, text.slice(0, 160)); return; }
    if (/Failed to load resource/i.test(text)) { warn(currentView, text.slice(0, 160)); return; }
    fail(currentView, `console.error: ${text.slice(0, 220)}`);
  });
  page.on('response', (resp) => {
    try {
      const url = resp.url();
      if (url.startsWith(BASE) && resp.status() >= 500) {
        fail(currentView, `HTTP ${resp.status()} ${url.replace(BASE, '')}`);
      }
    } catch (e) { /* detached */ }
  });

  // ── boot ──
  await page.goto(BASE, { waitUntil: 'domcontentloaded', timeout: 60000 });
  await new Promise(r => setTimeout(r, 4000)); // overview + jarvis briefing settle
  const booted = await page.evaluate(() =>
    typeof navigate === 'function' && typeof NAV_GROUPS === 'object' && !!window.Jarvis);
  if (!booted) { fail('boot', 'app globals missing (navigate/NAV_GROUPS/Jarvis)'); }
  else pass('boot: app globals present');

  // ── walk every view in NAV_GROUPS ──
  const views = await page.evaluate(() => {
    const out = [];
    for (const g of Object.values(NAV_GROUPS)) for (const it of g.items) out.push(it.view);
    return out;
  });
  pass(`discovered ${views.length} views`);

  for (const v of views) {
    currentView = v;
    try {
      await page.evaluate((view) => navigate(view), v);
    } catch (e) {
      fail(v, `navigate() threw: ${String(e).slice(0, 160)}`);
      continue;
    }
    const probe = (view) => {
      const el = document.getElementById('view-' + view);
      if (!el) return { ok: false, why: 'view container missing' };
      if (!el.classList.contains('active')) return { ok: false, why: 'not activated' };
      if (el.innerHTML.trim().length < 40) return { ok: false, why: 'rendered nearly empty' };
      // A view still showing ONLY a bare spinner with no text means its loader
      // never ran (typically a missing case in navigate()'s switch). Slow async
      // loaders legitimately show a spinner briefly, so this is RE-CHECKED after
      // a longer wait below before failing.
      const txt = (el.innerText || el.textContent || '').replace(/\s+/g, '');
      if (txt.length === 0 && /spinner/.test(el.innerHTML)) {
        return { ok: false, why: 'loader never populated (bare spinner) — wired into navigate()?' };
      }
      return { ok: true };
    };
    await new Promise(r => setTimeout(r, 1500));
    let state = await page.evaluate(probe, v);
    if (!state.ok && state.why.indexOf('bare spinner') >= 0) {
      // give a slow async loader more time before declaring it stuck
      await new Promise(r => setTimeout(r, 3000));
      state = await page.evaluate(probe, v);
    }
    if (!state.ok) fail(v, state.why);
    else pass(`view ${v} renders`);
  }

  // ── Jarvis surfaces ──
  currentView = 'jarvis-palette';
  await page.keyboard.down('Meta'); await page.keyboard.press('k'); await page.keyboard.up('Meta');
  await new Promise(r => setTimeout(r, 600));
  const paletteOpen = await page.evaluate(() => {
    const el = document.getElementById('jarvis-palette');
    return !!el && el.style.display !== 'none' && el.offsetParent !== null;
  });
  if (paletteOpen) pass('palette opens on ⌘K');
  else {
    // fall back: some headless environments eat Meta — try the "/" shortcut
    await page.keyboard.press('Escape');
    await page.keyboard.press('/');
    await new Promise(r => setTimeout(r, 600));
    const open2 = await page.evaluate(() => {
      const el = document.getElementById('jarvis-palette');
      return !!el && el.offsetParent !== null;
    });
    if (open2) pass('palette opens on "/"');
    else fail('jarvis-palette', 'palette did not open via ⌘K or /');
  }
  await page.keyboard.press('Escape');
  await page.keyboard.press('Escape');

  // Ask a rule-based question through the real streaming path.
  currentView = 'jarvis-ask';
  const askResult = await page.evaluate(async () => {
    const resp = await fetch('/api/jarvis/ask/stream', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: 'is the market open' }),
    });
    const text = await resp.text();
    return { status: resp.status, hasAnswer: text.includes('"type": "answer"') };
  });
  if (askResult.status === 200 && askResult.hasAnswer) pass('streaming ask answers in-browser');
  else fail('jarvis-ask', `stream status=${askResult.status} answer=${askResult.hasAnswer}`);

  // Command center: send a message through the UI and await a bubble.
  currentView = 'jarvis-cc';
  await page.evaluate(() => navigate('jarvis'));
  await new Promise(r => setTimeout(r, 1200));
  const ccOk = await page.evaluate(async () => {
    const input = document.getElementById('jv-input');
    const send = document.getElementById('jv-send');
    if (!input || !send) return { ok: false, why: 'input/send missing' };
    input.value = 'whats on my watchlist';
    send.click();
    for (let i = 0; i < 40; i++) {
      await new Promise(r => setTimeout(r, 250));
      const bubbles = document.querySelectorAll('#jv-chat .jv-bubble.assistant');
      const last = bubbles[bubbles.length - 1];
      if (last && !last.querySelector('.jv-thinking') && last.textContent.length > 10) {
        return { ok: true, text: last.textContent.slice(0, 60) };
      }
    }
    return { ok: false, why: 'no answer bubble within 10s' };
  });
  if (ccOk.ok) pass(`command center answers: "${ccOk.text}…"`);
  else fail('jarvis-cc', ccOk.why);

  await browser.close();

  console.log('\n' + '='.repeat(60));
  console.log(`  Browser e2e: ${failures.length} failure(s), ${warnings.length} warning(s)`);
  console.log('='.repeat(60));
  for (const f of failures) console.log('  FAIL ' + f);
  if (warnings.length) console.log(`  (${warnings.length} external-origin warnings suppressed)`);
  process.exit(failures.length ? 1 : 0);
})().catch((e) => { console.error('harness crashed:', e); process.exit(1); });
