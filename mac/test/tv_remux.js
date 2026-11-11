// tv-app 0.9.9: an .mp4 with its index at the END plays on the TV without the Mac (mp4box.js + Media Source).
//   node test/tv_remux.js <fake provider base> [tv source folder]
// Uses the fake provider's 907 (index at the end) and 901 (index first). No Mac link.
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs'), path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const FAKE = process.argv[2];
const root = path.join(__dirname, '..');
const src = process.argv[3] || path.join(root, fs.readdirSync(root).filter(d => /^tv-\d+\.\d+\.\d+-src$/.test(d))
  .sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).pop());
const PORT = 9379;
let fails = 0;
const ok = (cond, name, got) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond ? '' : '   got: ' + got)); if (!cond) fails++; };
(async () => {
  const hls = path.join(src, 'js', 'hls.min.js'), copied = !fs.existsSync(hls);
  if (copied) fs.copyFileSync(path.join(root, 'assets', 'hls.min.js'), hls);
  const srv = spawn('python3', [path.join(__dirname, 'tv_preview.py'), src, String(PORT)], { stdio: 'ignore' });
  await sleep(1200);
  const b = await chromium.launch({ args: ['--disable-web-security', '--autoplay-policy=no-user-gesture-required'] });
  const tv = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  tv.on('pageerror', e => { console.log('pageerror:', e.message); fails++; });
  await tv.addInitScript(f => (window.BB_MP4_CHUNK = 131072, window.BB_MP4_AHEAD = 10, 0) || localStorage.setItem('beeldbuis.v1', JSON.stringify({ account: { server: f, username: 'tester', password: 'secret123' }, countries: ['NL', 'UK'], englishSubs: true })), FAKE);
  await tv.goto('http://127.0.0.1:' + PORT + '/'); await sleep(2500);
  ok(await tv.evaluate(() => BBPlayer.remuxSupported()), 'Media Source + mp4box are available');
  const dbg = () => tv.evaluate(() => BBApp.debug());
  const remote = c => tv.evaluate(c => BBApp.remote(c), c);
  // play 907 (index at the end) the way Play on TV would
  await remote({ action: 'play', kind: 'movie', id: '907', ext: 'mp4', name: 'Index at the end', t: 0 });
  let d = {};
  for (let i = 0; i < 40 && !d.progress; i++) { await sleep(500); d = await dbg(); }
  ok(d.mode === 'remux' && d.progress, 'an .mp4 with its index at the end plays on the TV by itself (no Mac)', JSON.stringify(d));
  const p0 = d.pos;
  await tv.evaluate(() => BBPlayer.seekBy(8));
  for (let i = 0; i < 20; i++) { await sleep(500); d = await dbg(); if (d.pos > p0 + 6) break; }
  ok(d.pos > p0 + 6 && d.playing, 'seeking forward works', JSON.stringify(d) + ' from ' + p0);
  await tv.evaluate(() => BBPlayer.seekBy(-6));
  await sleep(2500); d = await dbg();
  ok(d.pos >= 0 && d.playing && d.mode === 'remux', 'seeking back works', JSON.stringify(d));
  // far jump past what is buffered (small chunks above keep the buffer short): must land there and keep playing
  const p1 = d.pos;
  await tv.evaluate(() => BBPlayer.seekBy(60));
  for (let i = 0; i < 20; i++) { await sleep(500); d = await dbg(); if (d.pos > p1 + 55) break; }
  await sleep(2500); const d2 = await dbg();
  ok(d.pos > p1 + 55 && d2.playing && d2.mode === 'remux' && d2.pos > d.pos, 'a far jump past the buffer lands there and keeps playing', JSON.stringify(d2) + ' from ' + p1);
  await remote({ action: 'stop' }); await sleep(800);
  await remote({ action: 'play', kind: 'movie', id: '901', ext: 'mp4', name: 'Index first', t: 0 });
  d = {};
  for (let i = 0; i < 20 && d.name !== 'Index first'; i++) { await sleep(500); d = await dbg(); }
  ok(d.mode === 'direct', 'an .mp4 with its index first still plays straight from the provider (normal player)', JSON.stringify(d));
  await remote({ action: 'stop' });
  await b.close(); srv.kill(); if (copied) fs.unlinkSync(hls);
  console.log(fails ? fails + ' FAILED' : 'all passed');
  process.exit(fails ? 1 : 0);
})();
