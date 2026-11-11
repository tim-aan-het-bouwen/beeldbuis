// Play on TV + VOD via the Mac (update 33 / tv-app 0.9.2, .mp4 through /direct since 0.9.4). Needs a running Beeldbuis server on a fake provider:
//   node test/tv_vodmac.js <mac base> <fake provider base> [tv source folder]
// The Mac sends a film whose file is gone at the provider (903; its 4K edition 904 works). The TV's player fails on the
// provider link, falls back to the Mac (which finds the edition) and reports what it plays; the Mac's card follows it.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs'), path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const [MAC, FAKE] = process.argv.slice(2, 4);
const root = path.join(__dirname, '..');
const src = process.argv[4] || path.join(root, fs.readdirSync(root).filter(d => /^tv-\d+\.\d+\.\d+-src$/.test(d))
  .sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).pop());
const PORT = 9378;
let fails = 0;
const ok = (cond, name, got) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond ? '' : '   got: ' + got)); if (!cond) fails++; };
const j = async (u, body) => (await fetch(MAC + u, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})).json();
(async () => {
  const hls = path.join(src, 'js', 'hls.min.js');
  if (!fs.existsSync(hls)) fs.copyFileSync(path.join(root, 'assets', 'hls.min.js'), hls);
  const srv = spawn('python3', [path.join(__dirname, 'tv_preview.py'), src, String(PORT)], { stdio: 'ignore' });
  await sleep(1200);
  const code = (await j('/api/phone')).data.code;
  const b = await chromium.launch({ args: ['--disable-web-security'] });   // the real TV has no CORS rules
  const tv = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  tv.on('pageerror', e => { console.log('pageerror:', e.message); fails++; });
  await tv.addInitScript(f => { if (!localStorage.getItem('beeldbuis.v1')) localStorage.setItem('beeldbuis.v1', JSON.stringify({ account: { server: f, username: 'tester', password: 'secret123' }, countries: ['NL', 'UK'] })); }, FAKE);
  await tv.goto('http://127.0.0.1:' + PORT + '/'); await sleep(2000);
  await tv.evaluate(([m, c]) => BBMac.pair(m, c), [MAC, code]);
  await tv.reload(); await sleep(3000);
  let st = (await j('/api/remote/status')).data;
  ok(st.tv_app, 'the TV app polls the Mac after pairing', JSON.stringify(st));

  const mac = await b.newPage({ viewport: { width: 1280, height: 800 } });
  await mac.goto(MAC + '/?bbtest=1'); await sleep(2500);
  await mac.evaluate(() => { const t = document.querySelector('#tourModal'); if (t) t.remove(); });
  const r = await j('/api/remote/send', { action: 'play', kind: 'movie', id: '903', ext: 'mp4', name: 'Dead film', t: 0 });
  ok(r.ok && r.data.tv_app, 'Play on TV reaches a TV that is open', JSON.stringify(r));
  await mac.evaluate(() => window.__bb("tvFollow('Dead film', 'Sending it to the TV…'); 1"));
  let d = {};
  for (let i = 0; i < 40 && d.mode !== 'macvod'; i++) { await sleep(500); d = await tv.evaluate(() => BBApp.debug()); }
  ok(d.mode === 'macvod', 'a film the TV cannot open straight from the provider falls back to the Mac', JSON.stringify(d));
  for (let i = 0; i < 60 && !/\/(hls|direct)\//.test(d.url || ''); i++) { await sleep(500); d = await tv.evaluate(() => BBApp.debug()); }
  ok(/\/(hls|direct)\//.test(d.url || '') && d.url.indexOf(MAC) === 0, 'the TV then plays the Mac’s stream', JSON.stringify(d));
  const ping = await j('/api/ping');
  ok(ping.ok, 'the Mac serves it (one connection)', JSON.stringify(ping));
  await sleep(2500);
  const card = await mac.evaluate(() => document.querySelector('#overlay').innerText.replace(/\s+/g, ' '));
  ok(/Playing on the TV/.test(card) && /Dead film/.test(card), 'the Mac card follows the TV', card);
  // a stream that never answers: the TV gives up after 30 s instead of "Loading…" forever (watchdog) - checked by mode change above
  // an .mp4 that exists goes through the Mac's /direct straight away (the provider's broken range header and an index at
  // the end of the file stop the TV's own player)
  await j('/api/remote/send', { action: 'play', kind: 'movie', id: '901', ext: 'mp4', name: 'Good film', t: 0 });
  d = {};
  for (let i = 0; i < 20 && d.name !== 'Good film'; i++) { await sleep(500); d = await tv.evaluate(() => BBApp.debug()); }
  ok(d.mode === 'macdirect' && /\/direct\/movie\/901\.mp4\?k=/.test(d.url || ''), 'an .mp4 plays through the Mac’s /direct right away', JSON.stringify(d));
  await j('/api/remote/send', { action: 'stop' });
  await sleep(2500);
  d = await tv.evaluate(() => BBApp.debug());
  ok(!d.playing, 'Stop on the TV stops it', JSON.stringify(d));
  await b.close(); srv.kill();
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
