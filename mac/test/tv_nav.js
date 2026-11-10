// TV app remote-control test (update 32 / tv-app 0.9.1): arrows, Back, typing in search, subtitle menu.
// Usage: node test/tv_nav.js [tv source folder]   (default: the newest tv-*-src next to test/)
const { chromium } = require('playwright');
const { spawn } = require('child_process');
const fs = require('fs'), path = require('path');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const root = path.join(__dirname, '..');
const src = process.argv[2] || path.join(root, fs.readdirSync(root).filter(d => /^tv-\d+\.\d+\.\d+-src$/.test(d))
  .sort((a, b) => a.localeCompare(b, undefined, { numeric: true })).pop());
const PORT = 9377;
let fails = 0;
const ok = (cond, name, got) => { console.log((cond ? 'PASS  ' : 'FAIL  ') + name + (cond ? '' : '   got: ' + got)); if (!cond) fails++; };
(async () => {
  const hls = path.join(src, 'js', 'hls.min.js');
  if (!fs.existsSync(hls) && fs.existsSync(path.join(root, 'assets', 'hls.min.js'))) fs.copyFileSync(path.join(root, 'assets', 'hls.min.js'), hls);
  const srv = spawn('python3', [path.join(__dirname, 'tv_preview.py'), src, String(PORT)], { stdio: 'ignore' });
  await sleep(1200);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  p.on('pageerror', e => { console.log('pageerror:', e.message); fails++; });
  await p.addInitScript(() => { if (!localStorage.getItem('beeldbuis.v1')) localStorage.setItem('beeldbuis.v1', JSON.stringify({ account: { server: 'http://prov.test', username: 'u', password: 'p' }, countries: ['NL', 'UK'] })); });
  await p.goto('http://127.0.0.1:' + PORT + '/'); await sleep(2500);
  const cur = () => p.evaluate(() => { const c = BBNav.current(); return c ? { cls: c.className, text: (c.textContent || '').trim().slice(0, 40), id: c.id, zone: BBNav.zone(), i: c.getAttribute('data-i') } : {}; });
  const key = async (k, n = 1) => { for (let i = 0; i < n; i++) { await p.keyboard.press(k); await sleep(200); } return cur(); };
  const tab = async t => { await p.evaluate(t => document.querySelector('.tab[data-tab="' + t + '"]').click(), t); await sleep(2000); };
  let c;

  await tab('movies');
  c = await key('ArrowRight'); c = await key('ArrowDown');
  ok(/tile/.test(c.cls) && c.i === '0', 'movies: right then down lands on the first tile', JSON.stringify(c));
  c = await key('ArrowRight', 2); ok(c.i === '2', 'movies: right twice moves along the row', JSON.stringify(c));
  c = await key('ArrowLeft'); ok(c.i === '1', 'movies: left goes one tile left, not to the sidebar', JSON.stringify(c));
  c = await key('ArrowLeft'); ok(c.i === '0', 'movies: left again reaches the first column', JSON.stringify(c));
  c = await key('ArrowLeft'); ok(/cat/.test(c.cls) && /active/.test(c.cls), 'movies: left from the first column goes to the active category', JSON.stringify(c));
  c = await key('ArrowRight'); ok(c.i === '0', 'movies: right from the sidebar returns to the tile you were on', JSON.stringify(c));
  c = await key('ArrowRight', 2); c = await key('ArrowDown'); ok(c.i === '7', 'movies: down keeps the column', JSON.stringify(c));
  c = await key('ArrowUp', 3); ok(c.zone === 'top' && /active/.test(c.cls), 'movies: up from the top row goes to the active tab', JSON.stringify(c));
  c = await key('ArrowDown'); ok(/sb/.test(c.cls) || /tile/.test(c.cls), 'movies: down from the tab bar goes back into the page', JSON.stringify(c));
  c = await key('ArrowDown'); c = await key('ArrowRight');
  c = await key('Escape'); ok(/cat/.test(c.cls) && /active/.test(c.cls), 'Back from the grid goes to the sidebar first', JSON.stringify(c));
  c = await key('Escape'); ok(c.zone === 'top' && /Movies/.test(c.text), 'Back from the sidebar goes to the tab bar', JSON.stringify(c));
  c = await key('Escape'); ok(/Tonight/.test(c.text) || (await p.evaluate(() => BBApp.debug().tab)) === 'home', 'Back from the tab bar goes to Tonight', JSON.stringify(c));

  await tab('live');
  c = await key('ArrowRight'); ok(/chan/.test(c.cls), 'live: right from the groups goes to the channels', JSON.stringify(c));
  c = await key('ArrowDown', 2); c = await key('ArrowLeft'); ok(/cat/.test(c.cls) && /active/.test(c.cls), 'live: left from a channel goes to its group', JSON.stringify(c));
  c = await key('ArrowRight'); ok(/chan/.test(c.cls) && c.i === '2', 'live: right returns to the same channel', JSON.stringify(c));

  await tab('search');
  await p.keyboard.type('film action 1'); await sleep(1500);
  const v = await p.evaluate(() => document.querySelector('#sq').value);
  ok(v === 'film action 1', 'search: space and the letter i can be typed', v);
  c = await key('Enter'); ok(/tile/.test(c.cls), 'search: OK jumps to the results', JSON.stringify(c));
  c = await key('ArrowRight'); c = await key('ArrowLeft'); ok(/tile/.test(c.cls), 'search: left/right stay in the results row', JSON.stringify(c));

  await tab('settings');
  await p.evaluate(() => document.querySelector('#stSubStyle').click()); await sleep(500);
  c = await cur(); ok(c.zone === 'modal' && /subtab/.test(c.cls) && /Presets/.test(c.text), 'subtitles: editor opens on the Presets tab', JSON.stringify(c));
  ok(await p.evaluate(() => getComputedStyle(document.querySelector('#subs')).display !== 'none'), 'subtitles: a sample subtitle shows full size while editing', '');
  c = await key('ArrowRight'); ok(/Text/.test(c.text) && await p.evaluate(() => !!document.querySelector('.srow[data-k="size"]')), 'subtitles: right on the tabs opens the Text tab', JSON.stringify(c));
  c = await key('ArrowDown'); ok(/srow/.test(c.cls) && /Size/.test(c.text), 'subtitles: down goes to the first setting', JSON.stringify(c));
  c = await key('ArrowRight'); ok(/110%/.test(c.text), 'subtitles: right makes the text bigger (focus stays on the row)', JSON.stringify(c));
  c = await key('ArrowDown'); c = await key('ArrowRight'); ok(/Yellow/.test(c.text), 'subtitles: colour changes with right', JSON.stringify(c));
  c = await key('ArrowUp', 3); ok(/subtab/.test(c.cls) && /Text/.test(c.text), 'subtitles: up returns to the current tab', JSON.stringify(c));
  c = await key('ArrowRight'); c = await key('ArrowDown'); c = await key('ArrowRight', 2);
  ok(/Navy/.test(c.text), 'subtitles: background colour can be set', JSON.stringify(c));
  const st = await p.evaluate(() => JSON.parse(localStorage.getItem('beeldbuis.v1')).subStyles.p1);
  ok(st && st.size === 110 && st.color === 'yellow' && st.bg === 'navy', 'subtitles: choices are saved per profile', JSON.stringify(st));
  const span = await p.evaluate(() => document.querySelector('#subs span').getAttribute('style'));
  ok(/rgba\(255,225,77/.test(span) && /rgba\(14,15,18,0.6\)/.test(span), 'subtitles: the subtitle on screen follows the choices', span);
  await p.evaluate(() => document.querySelector('.subtab[data-t="presets"]').click()); await sleep(300);
  await p.evaluate(() => document.querySelector('.spreset[data-p="cinema"]').click()); await sleep(300);
  c = await cur(); ok(/spreset/.test(c.cls) && /Cinema/.test(c.text), 'subtitles: a preset applies and keeps focus', JSON.stringify(c));
  await key('Escape');
  ok(/Cinema/.test(await p.evaluate(() => document.querySelector('#stSubStyle').textContent)), 'subtitles: Settings shows the chosen style', '');
  ok(await p.evaluate(() => getComputedStyle(document.querySelector('#subs')).display === 'none'), 'subtitles: the sample disappears after closing', '');

  await b.close(); srv.kill();
  console.log(fails ? fails + ' failed' : 'all passed');
  process.exit(fails ? 1 : 0);
})().catch(e => { console.log('FAIL', e); process.exit(1); });
