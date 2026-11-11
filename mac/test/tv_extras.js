// Update 35 / tv-app 0.9.5. Started by test/tv_extras.py:  node test/tv_extras.js <mac lan base> <fake provider> <mock base> <mac local base> [shots dir]
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs'), path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const [MAC, FAKE, MOCK, LOCAL, SHOTS] = process.argv.slice(2, 7);
const root = path.join(__dirname, '..');
const newest = fs.readdirSync(root).filter(d => /^tv-\d+\.\d+\.\d+-src$/.test(d)).sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).pop();
const src = process.env.TV_SRC || (newest ? path.join(root, newest) : path.join(root, '..', 'tv', 'app'));   // the repository keeps the TV app in tv/app
const PORT = 9379;
let fails = 0;
const ok = (cond, name, got) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond ? '' : '   got: ' + got)); if (!cond) fails++; };
const j = async (u, body) => (await fetch(LOCAL + u, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})).json();
const tvlog = async () => (await fetch(MOCK + '/__tvlog')).json();
const b64 = s => Buffer.from(s, 'utf8').toString('base64');
(async () => {
  const hls = path.join(src, 'js', 'hls.min.js');
  if (!fs.existsSync(hls)) fs.copyFileSync(path.join(root, 'assets', 'hls.min.js'), hls);
  const srv = spawn('python3', [path.join(__dirname, 'tv_preview.py'), src, String(PORT)], { stdio: 'ignore' });
  await sleep(1200);
  const b = await chromium.launch({ args: ['--disable-web-security', '--autoplay-policy=no-user-gesture-required'] });
  const tv = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  const shot = async n => { if (SHOTS) await tv.screenshot({ path: path.join(SHOTS, n + '.png') }); };
  tv.on('pageerror', e => { console.log('pageerror:', e.message); fails++; });
  await tv.addInitScript(f => { if (!localStorage.getItem('beeldbuis.v1')) localStorage.setItem('beeldbuis.v1', JSON.stringify({ account: { server: f, username: 'tester', password: 'secret123' }, countries: ['NL', 'UK'] })); }, FAKE);
  await tv.goto('http://127.0.0.1:' + PORT + '/'); await sleep(2000);
  const dbg = () => tv.evaluate(() => Object.assign(BBApp.debug(), { focus: (BBNav.current() || {}).textContent, html: document.querySelector('#screen').innerText.replace(/\s+/g, ' ').slice(0, 600) }));
  const key = async (k, n = 1) => { for (let i = 0; i < n; i++) { await tv.keyboard.press(k); await sleep(250); } };

  // ---- calm home, not linked yet
  let d = await dbg();
  ok(d.tab === 'calm' && /Tonight/.test(d.html) && /Live TV/.test(d.html) && /Settings/.test(d.html), 'the TV app starts on the calm home screen', JSON.stringify(d));
  ok(await tv.evaluate(() => /^\d\d:\d\d$/.test(document.querySelector('#calmClock').textContent)), 'calm home shows the clock', '');
  await key('ArrowRight', 2); await key('Enter'); await sleep(1500);
  d = await dbg(); ok(d.tab === 'sport', 'OK on a calm tile opens that part (Sport)', d.tab);
  for (let i = 0; i < 4 && (await dbg()).tab !== 'home'; i++) await key('Escape');
  d = await dbg(); ok(d.tab === 'home', 'Back from another tab goes to Tonight', d.tab);
  for (let i = 0; i < 4 && (await dbg()).tab === 'home'; i++) await key('Escape');
  d = await dbg(); ok(d.tab === 'calm', 'Back from Tonight returns to the calm home', d.tab + ' modal=' + d.modal);

  // ---- link with the Mac: weather, inputs, apps
  const code = (await j('/api/phone')).data.code;
  await tv.evaluate(([m, c]) => BBMac.pair(m, c), [MAC, code]);
  await tv.reload(); await sleep(5000);
  d = await dbg();
  ok(/12°/.test(d.html) && /Light rain/.test(d.html) && /Testville/.test(d.html), 'calm home shows the weather from the Mac', d.html);
  ok(/Game console/.test(d.html) && /Netflix/.test(d.html), 'calm home shows the inputs and apps the Mac knows', d.html);
  await shot('calm');
  await key('ArrowDown'); d = await dbg();
  ok(/Game console/.test(d.focus || ''), 'down from the tiles lands on the first input', d.focus);
  const before = (await tvlog()).length;
  await key('Enter');
  let log = [];
  for (let i = 0; i < 40 && !log.some(m => m.params && m.params.DataOfCmd === 'KEY_ENTER'); i++) { await sleep(500); log = (await tvlog()).slice(before); }
  const keys = log.filter(m => m.params && m.params.TypeOfRemote === 'SendRemoteKey').map(m => m.params.DataOfCmd);
  ok(keys[0] === 'KEY_SOURCE' && keys.filter(k => k === 'KEY_RIGHT').length === 3 && keys[keys.length - 1] === 'KEY_ENTER', 'OK on an input makes the Mac walk the TV\'s source menu', JSON.stringify(keys));
  await key('ArrowRight'); await key('Enter'); await sleep(2500);
  log = (await tvlog()).slice(before);
  ok(log.some(m => m.http && /applications\/11101200001/.test(m.http)), 'OK on an app makes the Mac start it on the TV', JSON.stringify(log.slice(-2)));

  // ---- typing from the phone: in the Beeldbuis TV app
  let r = await j('/api/remote/send', { action: 'text', text: 'the fall' });
  ok(r.ok && r.data.tv_app && r.data.tv, 'typing: the Mac sends text to the TV app and to the TV keyboard', JSON.stringify(r));
  await sleep(1800);
  d = await dbg();
  let val = await tv.evaluate(() => (document.querySelector('#sq') || {}).value);
  ok(d.tab === 'search' && val === 'the fall', 'typing: text from the phone opens Search on the TV with that text', d.tab + ' / ' + val);
  r = await j('/api/remote/send', { action: 'text', text: 'the fall 2', enter: true }); await sleep(1800);
  val = await tv.evaluate(() => (document.querySelector('#sq') || {}).value);
  ok(val === 'the fall 2', 'typing: more letters replace the text in the focused field', val);
  log = await tvlog();
  const txt = log.filter(m => m.params && m.params.TypeOfRemote === 'SendInputString').map(m => m.params.Cmd);
  ok(txt.includes(b64('the fall')) && txt.includes(b64('the fall 2')) && log.some(m => m.params && m.params.TypeOfRemote === 'SendInputEnd'),
    'typing: the TV keyboard of other apps gets the same text (base64) and Done', JSON.stringify(txt));
  // the Mac page: the field in the TV remote
  const mac = await b.newPage({ viewport: { width: 1280, height: 800 } });
  mac.on('pageerror', e => { console.log('mac pageerror:', e.message); fails++; });
  await mac.goto(LOCAL + '/'); await sleep(2500);
  await mac.evaluate(() => { const t = document.querySelector('#tourModal'); if (t) t.remove(); document.querySelector('#btnRemote').click(); });
  await sleep(800);
  await mac.fill('#rmtText', ''); await mac.type('#rmtText', 'héllo tv', { delay: 40 }); await sleep(1800);
  val = await tv.evaluate(() => (document.querySelector('#sq') || {}).value);
  ok(val === 'héllo tv', 'typing: the field in the TV remote on the Mac page types on the TV', val);
  if (SHOTS) await mac.screenshot({ path: path.join(SHOTS, 'mac-remote.png') });
  const stale = await j('/api/remote/send', { action: 'text', text: '' });
  ok(stale.ok, 'typing: an empty text clears the field', JSON.stringify(stale));

  // ---- match data next to the picture (the test browser has no H.264: a stand-in player that only shows the picture box)
  await tv.evaluate(() => { BBPlayer.open = () => { document.querySelector('#video').classList.remove('hidden'); BBPlayer.rect(window.__rect || null); }; const r = BBPlayer.rect; BBPlayer.rect = x => { window.__rect = x; return r(x); }; });
  await j('/api/remote/send', { action: 'play', kind: 'live', id: '100', ext: 'ts', name: 'Redtown v Bluevale', key: 'x' });
  d = {};
  for (let i = 0; i < 60 && !d.playing; i++) { await sleep(500); d = await dbg(); }
  ok(d.playing, 'a live channel plays on the TV', JSON.stringify(d));
  await sleep(2500);
  await tv.keyboard.down('Enter'); await sleep(900); await tv.keyboard.up('Enter'); await sleep(2500);
  d = await dbg();
  let side = await tv.evaluate(() => document.querySelector('#side').innerText.replace(/\s+/g, ' '));
  const rect = await tv.evaluate(() => { const v = document.querySelector('#video').getBoundingClientRect(); return [v.left, v.top, v.width, v.height].map(Math.round).join(','); });
  ok(d.side === 1 && rect === '0,0,1440,810', 'holding OK shrinks the picture and opens match data', d.side + ' ' + rect);
  ok(/Redtown/.test(side) && /Bluevale/.test(side) && /2 – 1/.test(side) && /67'/.test(side) && /S\. Triker/.test(side), 'match data: the match you watch live shows score, clock and goals', side);
  ok(/Northham – Southpool/.test(side) && !/0–3/.test(side) && /scores hidden/i.test(side), 'match data: other matches are listed without scores', side);
  await shot('side');
  await tv.keyboard.press('Enter'); await sleep(1200);                 // a short press still pauses
  side = await tv.evaluate(() => document.querySelector('#side').innerText.replace(/\s+/g, ' '));
  ok(/behind live/.test(side) && !/2 – 1/.test(side) && !/S\. Triker/.test(side), 'match data: paused (behind live) hides the score of your own match', side);
  await shot('side-behind');
  await tv.keyboard.down('Enter'); await sleep(900); await tv.keyboard.up('Enter'); await sleep(1500);
  side = await tv.evaluate(() => document.querySelector('#side').innerText.replace(/\s+/g, ' '));
  d = await dbg();
  ok(d.side === 2 && /2 – 1/.test(side) && /0–3/.test(side) && /ahead of your picture/.test(side), 'holding OK again shows every score, with a warning', side);
  await tv.keyboard.down('Enter'); await sleep(900); await tv.keyboard.up('Enter'); await sleep(800);
  d = await dbg();
  const full = await tv.evaluate(() => { const v = document.querySelector('#video').getBoundingClientRect(); return Math.round(v.width); });
  ok(d.side === 0 && full === 1920, 'a third hold closes match data and the picture is full screen again', d.side + ' ' + full);
  r = await j('/api/remote/send', { action: 'panel' }); await sleep(1200);
  d = await dbg(); ok(r.ok && d.side === 1, '"Match data on TV" from the phone opens it too', JSON.stringify(r) + d.side);
  await key('Escape'); d = await dbg(); ok(d.side === 0 && d.playing, 'Back closes match data first, the channel keeps playing', JSON.stringify(d));
  await key('Escape'); await sleep(1500);
  d = await dbg(); ok(!d.playing, 'Back again stops the channel', JSON.stringify(d));

  // ---- screensaver
  const amb = await j('/api/tv/ambient');
  ok(amb.data.photos === 3 && !amb.data.photo_error, 'the Mac finds the photos in the chosen folder (not the text file)', JSON.stringify(amb.data));
  const ph = await fetch(MAC + '/api/tv/photo?i=1&k=' + encodeURIComponent(await tv.evaluate(() => BBMac.link().key)));
  const buf = Buffer.from(await ph.arrayBuffer());
  ok(ph.status === 200 && buf[0] === 0xFF && buf[1] === 0xD8, 'a photo arrives as a JPEG', ph.status + ' ' + buf.slice(0, 4).toString('hex'));
  const noKey = await fetch(MAC + '/api/tv/photo?i=1');
  ok(noKey.status === 401 || noKey.status === 403, 'photos are not served without the device key', noKey.status);
  await tv.evaluate(m => {      // tonight's picks need film art; the test Mac has none, so hand the TV two picks
    const real = BBMac.home;
    BBMac.home = p => real(p).then(d => { d.img_base = m + '/img'; d.rows = (d.rows || []).concat([{ id: 'foryou', kind: 'tiles', title: 'For you', tiles: [
      { key: 'm1', kind: 'movie', title: 'Pick One', year: 2021, meta: { backdrop: '/one.jpg', title: 'Pick One' } }] }]); return d; });
  }, MOCK);
  await tv.evaluate(() => BBApp.ambient(true)); await sleep(2500);
  await tv.evaluate(() => BBApp.calm()); await sleep(1000);
  await tv.evaluate(() => BBApp.saver()); await sleep(3500);
  d = await dbg();
  let sv = await tv.evaluate(() => ({ cls: document.querySelector('#saver').className, text: document.querySelector('#saver').innerText.replace(/\s+/g, ' '),
    bg: [0, 1].map(i => document.querySelector('#sv' + i).className + ' ' + document.querySelector('#sv' + i).style.backgroundImage).join(' | ') }));
  ok(d.saver && /sv-photo/.test(sv.cls) && /api\/tv\/photo\?i=0/.test(sv.bg) && /on/.test(sv.bg), 'the screensaver starts with a photo from the Mac', JSON.stringify(sv));
  ok(/\d\d:\d\d/.test(sv.text) && /12°/.test(sv.text), 'the screensaver shows the clock and the weather', sv.text);
  await shot('saver-photo');
  // slides change every 20 s: third slide is a pick
  await sleep(41000);
  sv = await tv.evaluate(() => ({ cls: document.querySelector('#saver').className, text: document.querySelector('#saver').innerText.replace(/\s+/g, ' ') }));
  ok(/sv-pick/.test(sv.cls) && /For you/i.test(sv.text) && /Pick One/.test(sv.text), 'after two photos the screensaver shows one of tonight\'s picks', JSON.stringify(sv));
  await shot('saver-pick');
  await key('ArrowDown'); await sleep(600);
  d = await dbg();
  ok(!d.saver && d.tab === 'calm' && await tv.evaluate(() => document.querySelector('#saver').classList.contains('hidden')), 'any key ends the screensaver and does nothing else', JSON.stringify(d));
  // it starts by itself after the quiet time, and not while something plays
  await tv.evaluate(() => { const s = JSON.parse(localStorage.getItem('beeldbuis.v1')); s.saverMin = 0.05; localStorage.setItem('beeldbuis.v1', JSON.stringify(s)); });
  await tv.reload(); await sleep(6500);
  d = await dbg(); ok(d.saver, 'the screensaver starts by itself after the quiet time', JSON.stringify(d));
  await tv.evaluate(() => { BBPlayer.open = () => { document.querySelector('#video').classList.remove('hidden'); }; });
  await j('/api/remote/send', { action: 'play', kind: 'live', id: '100', ext: 'ts', name: 'Channel', key: 'x' });
  d = {};
  for (let i = 0; i < 40 && !d.playing; i++) { await sleep(500); d = await dbg(); }
  ok(d.playing && !d.saver, 'Play on TV wakes the screen', JSON.stringify(d));
  await sleep(6000);
  d = await dbg(); ok(d.playing && !d.saver, 'no screensaver while something is playing', JSON.stringify(d));
  await j('/api/remote/send', { action: 'stop' }); await sleep(1500);

  // ---- settings on the TV
  await tv.evaluate(() => document.querySelector('.tab[data-tab="settings"]').click()); await sleep(600);
  await tv.evaluate(() => document.querySelector('#stStart').click());
  const st = await tv.evaluate(() => [document.querySelector('#stStart span').textContent, JSON.parse(localStorage.getItem('beeldbuis.v1')).startScreen]);
  ok(/Tonight/.test(st[0]) && st[1] === 'tonight', 'Settings: the start screen can be set back to Tonight', JSON.stringify(st));
  await tv.reload(); await sleep(2500);
  d = await dbg(); ok(d.tab === 'home', 'with that setting the app starts on Tonight', d.tab);

  await b.close(); srv.kill();
  console.log(fails ? fails + ' FAILED' : 'ALL PASS');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
