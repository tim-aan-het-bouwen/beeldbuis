// Beeldbuis UI: the player and the Mac keep each other informed (update 34), against a running server with the fake provider.
//   BASE=http://127.0.0.1:8801 node test/ui_session.js
// Headless Chromium cannot decode h264, so this checks the requests and the state of the player, not the picture.
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8765';
let failed = 0;
const ok = (c, msg) => { console.log((c ? 'PASS ' : 'FAIL ') + msg); if (!c) failed++; };
const get = async p => (await fetch(BASE + p)).json();

(async () => {
  const browser = await chromium.launch();
  const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
  const errors = [], reqs = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('request', r => { const u = r.url().replace(BASE, ''); if (/^\/api\/(play|ping|stop)/.test(u)) reqs.push(r.method() + ' ' + u); });
  await page.goto(BASE + '/?bbtest=1');
  const bb = code => page.evaluate(c => window.__bb(c), code);
  const until = (code, t) => page.waitForFunction(c => window.__bb && window.__bb(c), code, { timeout: t || 30000 });
  await until('catalog.groups.length > 0', 60000);
  // no h264 here: a stand-in for hls.js that accepts everything, so the player logic around it can be followed
  await page.evaluate(() => { const real = window.Hls; window.Hls = class { static isSupported() { return true; } loadSource() {} attachMedia() {} on() {} destroy() {} startLoad() {} };
    window.Hls.Events = real.Events; window.Hls.ErrorTypes = real.ErrorTypes; });
  await page.evaluate(() => { const m = document.querySelector('#tourModal'); if (m) m.classList.add('hidden'); });

  // 1. a playing stream is named in every ping, so the Mac keeps it while the film is paused
  await bb('play(catalog.groups[0]); 1');
  await until('!!(current && current.hlsSid)');
  const sid = await bb('current.hlsSid');
  await page.waitForTimeout(6000);
  ok(reqs.some(r => r.includes('/api/ping?s=' + sid)), 'ping names the stream it is showing');

  // 2. the stream disappears on the Mac (update, restart): the player reopens it by itself, once
  const before = reqs.filter(r => r.includes('/api/play')).length;
  await bb('current.at -= 60; 1');
  await get('/api/stop');
  await until(`!!(current && current.hlsSid && current.hlsSid !== '${sid}')`).catch(() => {});
  const sid2 = await bb('current && current.hlsSid');
  ok(sid2 && sid2 !== sid, 'a lost live stream at the live edge reopens by itself');
  ok(reqs.filter(r => r.includes('/api/play')).length === before + 1, 'exactly one new start request');
  ok(await bb("current.sessRetry === 1 && current.kind === 'live'"), 'still the same channel, marked as a retry');

  // 2b. paused two minutes behind live when the stream disappears: continue at that moment via catch-up, never at live
  const sid3 = sid2;
  await bb('current.at -= 60; current.wall = Date.now() / 1000 - 120; 1');
  await get('/api/stop');
  await until(`!!(current && current.hlsSid && current.hlsSid !== '${sid3}')`).catch(() => {});
  ok(await bb("current.kind === 'catchup'"), 'behind live: reopens at the same moment via catch-up, not at live');
  await bb('play(catalog.groups[0]); 1');
  await until("!!(current && current.kind === 'live' && current.hlsSid)");
  const before3 = reqs.filter(r => r.includes('/api/play')).length;

  // 3. after a sleep the player asks instead of jumping to live or into hours of catch-up
  await bb('current.at -= 60; current.wall = Date.now() / 1000 - 7200; pingLoop.lastAt = Date.now() - 3 * 3600 * 1000; 1');
  await get('/api/stop');
  await page.waitForSelector('#ovLive', { timeout: 30000 }).catch(() => {});
  const txt = await page.evaluate(() => document.querySelector('#overlay').innerText);
  ok(/Welcome back/.test(txt) && /Go to live/.test(txt), 'after a sleep: a choice, no jump (' + txt.replace(/\s+/g, ' ').slice(0, 90) + ')');
  ok(/Continue from \d\d:\d\d/.test(txt), 'offers to continue from the moment on screen via catch-up');
  ok(reqs.filter(r => r.includes('/api/play')).length === before3, 'nothing was started without asking');
  await page.screenshot({ path: (process.env.OUT || '.') + '/session-welcome-back.png' });
  await page.click('#ovStop2');

  // 4. a film: lost stream reopens at the same spot, paused stays paused
  const mv = await get('/api/library?kind=movie&q=interstellar');
  const it = mv.data.items[0], item = await get('/api/item?key=' + encodeURIComponent(it.key));
  const v = item.data.variants[0];
  await bb(`play({ _kind: 'movie', stream_id: ${v.id}, container_extension: '${v.ext || 'mkv'}', name: 'Interstellar', _key: 'test:film' }, { t: 60, force: true }); 1`);
  await until("!!(current && current.kind === 'movie' && current.hlsSid)", 60000);
  const fs1 = await bb('current.at -= 60; video.pause(); current.hlsSid');
  await get('/api/stop');
  await until(`!!(current && current.hlsSid && current.hlsSid !== '${fs1}')`).catch(() => {});
  const last = reqs.filter(r => r.includes('/api/play')).pop() || '';
  ok(/kind=movie/.test(last) && /[?&]t=6\d(\.\d+)?(&|$)/.test(last) && /force=1/.test(last), 'a lost film reopens at the same spot (' + last + ')');

  // 5. closing the window frees the provider connection straight away
  const n = reqs.filter(r => r.startsWith('POST /api/stop')).length;
  await page.evaluate(() => window.dispatchEvent(new Event('pagehide')));
  await page.waitForTimeout(1500);
  ok(reqs.filter(r => r.startsWith('POST /api/stop')).length === n + 1, 'closing the window tells the Mac to stop the stream');
  await page.waitForTimeout(6000);
  const p = await get('/api/ping?s=' + (await bb('current && current.hlsSid') || 'x'));
  ok(p.alive === false, 'the stream is gone on the Mac');

  ok(errors.length === 0, 'no JavaScript errors' + (errors.length ? ': ' + [...new Set(errors)].slice(0, 5).join(' | ') : ''));
  await browser.close();
  console.log(failed ? failed + ' failed' : 'all passed');
  process.exit(failed ? 1 : 0);
})();
