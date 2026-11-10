// iPhone web-app flow: pairing, mobile layout, native HLS (Chromium's own HLS player stands in for Safari),
// guide, movie with English subtitles, takeover between Mac and iPhone (one connection), settings.
// Usage: node e2e_phone.js http://<non-loopback ip>:8765   (server + mock provider running, logged in)
const { chromium, devices } = require('playwright');
const sleep = ms => new Promise(r => setTimeout(r, ms));
const REMOTE = process.argv[2], LOCAL = 'http://127.0.0.1:8765', OUT = __dirname + '/shots/';
(async () => {
  const b = await chromium.launch({ args: ['--autoplay-policy=no-user-gesture-required'] });
  const ctx = await b.newContext({ ...devices['iPhone 13'] });
  const p = await ctx.newPage();
  const log = (...a) => console.log(...a);
  p.on('pageerror', e => log('pageerror:', e.message));
  p.on('dialog', d => d.accept());
  const vs = pg => pg.evaluate(() => { const v = document.querySelector('#video'); return { t: +v.currentTime.toFixed(1), paused: v.paused, rs: v.readyState, src: (v.currentSrc || '').replace(/^https?:\/\/[^/]+/, '').slice(0, 40),
    overlay: document.querySelector('#overlay').classList.contains('hidden') ? '' : document.querySelector('#overlay').textContent.trim().slice(0, 80), title: document.querySelector('#title').textContent,
    cc: document.querySelector('#btnCC').style.display !== 'none' ? document.querySelector('#btnCC').textContent : null, tracks: [...v.textTracks].map(t => t.kind + ':' + t.language + ':' + t.mode) }; });
  await p.goto(REMOTE + '/'); await sleep(1500);
  log('pair modal shown:', await p.isVisible('#pairModal .card'));
  await p.screenshot({ path: OUT + 'p1-pair.png' });
  await p.fill('#pairCode', '000000'); await sleep(800); log('wrong code msg:', await p.textContent('#pairMsg'));
  const code = (await (await fetch(LOCAL + '/api/phone')).json()).data.code;
  await p.goto(REMOTE + '/?pair=' + code); await sleep(4000);
  log('paired; url now', p.url(), 'key stored', await p.evaluate(() => !!localStorage.getItem('bb_key')), 'manifest', await p.getAttribute('#manifestLink', 'href').then(h => h.slice(0, 22)));
  log('Account hidden', await p.evaluate(() => getComputedStyle(document.querySelector('#btnAccount')).display), 'tabs at bottom', await p.evaluate(() => Math.round(document.querySelector('.tabs').getBoundingClientRect().bottom) + '/' + innerHeight));
  await p.screenshot({ path: OUT + 'p2-list.png' });
  const val = await p.evaluate(() => [...document.querySelectorAll('#cat option')].find(o => o.textContent.startsWith('ALGEMEEN'))?.value);
  await p.selectOption('#cat', val); await sleep(1500);
  log('== LIVE (native HLS)'); await p.tap('#list .item:nth-child(1) .name'); await sleep(10000);
  log(await vs(p)); await p.screenshot({ path: OUT + 'p3-live.png' });
  log('== GUIDE'); await p.tap('#btnGuide'); await sleep(3000); await p.screenshot({ path: OUT + 'p4-guide.png' });
  const gp = await p.$('.gp.live'); if (gp) { await gp.tap(); await sleep(500); await p.screenshot({ path: OUT + 'p5-pcard.png' }); log('pcard', await p.evaluate(() => [...document.querySelectorAll('#pcard .row button')].map(b => b.textContent))); await p.tap('#pclose'); }
  await p.tap('#gclose'); await sleep(500);
  log('== MOVIE (subtitles)'); await p.tap('.tabs button[data-tab="vod"]'); await sleep(3000);
  await p.tap('#list .item:nth-child(2) .name'); await sleep(12000); log(await vs(p)); await p.screenshot({ path: OUT + 'p6-movie.png' });
  log('== TAKEOVER: the Mac starts live TV');
  const mac = await (await b.newContext({ viewport: { width: 1300, height: 820 } })).newPage();
  mac.on('pageerror', e => log('mac pageerror:', e.message));
  await mac.goto(LOCAL + '/'); await sleep(4000); await mac.selectOption('#cat', val); await sleep(1200);
  await mac.click('#list .item:nth-child(2)'); await sleep(9000);
  log('mac', await vs(mac)); log('phone', await vs(p)); await p.screenshot({ path: OUT + 'p7-taken.png' });
  log('== phone takes it back'); await p.tap('#takeBack'); await sleep(10000);
  log('phone', await vs(p)); await sleep(5000); log('mac', await vs(mac)); await mac.screenshot({ path: OUT + 'm1-taken.png' });
  log('== SETTINGS phone'); await p.tap('#btnSettings'); await sleep(800);
  log('tv section', await p.isVisible('#sTvSec'), 'phone section', await p.isVisible('#sPhoneSec'), 'this device', await p.textContent('#sThisDevInfo'));
  await p.screenshot({ path: OUT + 'p8-settings.png' }); await p.tap('#sCancel');
  log('== SETTINGS mac'); await mac.click('#btnSettings'); await sleep(1500);
  await mac.evaluate(() => document.querySelector('#sPhoneSec').scrollIntoView()); await sleep(300);
  log('qr', await mac.evaluate(() => !!document.querySelector('#sPhoneQr img')), 'devices', await mac.textContent('#sPhoneDevs'));
  await mac.screenshot({ path: OUT + 'm2-settings.png' });
  log('== HOME-SCREEN APP (no cookie, start_url with key)');
  const key = await p.evaluate(() => JSON.parse(localStorage.getItem('bb_key')));
  const p2 = await (await b.newContext({ ...devices['iPhone 13'] })).newPage();
  await p2.goto(REMOTE + '/?k=' + key); await sleep(3000);
  log('pair modal hidden', !(await p2.isVisible('#pairModal .card')), 'items', await p2.$$eval('#list .item', l => l.length), 'url', p2.url());
  log('== LANDSCAPE'); await p.setViewportSize({ width: 844, height: 390 }); await sleep(800); await p.screenshot({ path: OUT + 'p9-landscape.png' });
  await b.close();
})().catch(e => { console.log('E2E FAIL', e); process.exit(1); });
