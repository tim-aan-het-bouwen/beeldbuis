// Beeldbuis UI smoke test: every tab and overlay on a desktop and a phone viewport, against a running server.
//   BASE=http://127.0.0.1:8801 OUT=/tmp/shots node test/ui_smoke.js
// Fails on page errors, console errors, a page that is wider than the window (update 27), empty tabs and a player
// that does not start. Screenshots of every step go to OUT for a visual check.
const { chromium, devices } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8765';
const OUT = process.env.OUT || '.';
let failed = 0;
const ok = (c, msg) => { console.log((c ? 'PASS ' : 'FAIL ') + msg); if (!c) failed++; };

async function noSideways(page, where) {
  const w = await page.evaluate(() => ({ doc: document.documentElement.scrollWidth, win: window.innerWidth,
    wide: [...document.querySelectorAll('body *')].filter(e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
      return r.right > window.innerWidth + 2 && r.width > 0 && cs.position !== 'fixed' && !e.closest('.hscroll, .hrow .tiles, .strip, .rowtiles, [data-hscroll], .gwrap, #guide, .tabs, .chips, .gbody, .ggrid, .sp-row, .cards, .scroller') ; }).slice(0, 3).map(e => e.tagName + '.' + e.className) }));
  ok(w.doc <= w.win + 1, `${where}: page not wider than window (${w.doc} vs ${w.win}) ${w.wide.join(' ')}`);
}

async function run(name, ctxOpts) {
  const browser = await chromium.launch();
  const ctx = await browser.newContext(ctxOpts);
  const page = await ctx.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error' && !/Failed to load resource|ERR_|net::/.test(m.text())) errors.push('console: ' + m.text()); });
  const shot = async (n) => page.screenshot({ path: `${OUT}/${name}-${n}.png` });
  await page.goto(BASE);
  await page.waitForTimeout(2500);
  // the setup tour opens on a fresh install; close it
  if (await page.$('#tourModal:not(.hidden)')) { await shot('tour'); await page.keyboard.press('Escape'); await page.evaluate(() => { const m = document.querySelector('#tourModal'); if (m) m.classList.add('hidden'); }); }
  await page.waitForSelector('#home', { state: 'visible', timeout: 20000 }).catch(() => {});
  await page.waitForFunction(() => document.querySelectorAll('#home .tile, #home .card, #home .sc').length > 3, null, { timeout: 60000 }).catch(() => {});
  const nTiles = await page.$$eval('#home .tile', t => t.length);
  ok(nTiles > 3, `${name} Tonight shows titles (${nTiles})`);
  await shot('tonight');
  await noSideways(page, name + ' Tonight');

  const tabs = await page.$$eval('[data-tab]', t => t.filter(x => x.offsetParent).map(x => x.dataset.tab));
  for (const tab of tabs) {
    await page.click(`[data-tab="${tab}"]:visible`).catch(e => ok(false, `${name} click tab ${tab}: ${e.message.split('\n')[0]}`));
    await page.waitForTimeout(tab === 'vod' || tab === 'series' ? 4000 : 2500);
    const txt = await page.evaluate(() => document.body.innerText.length);
    ok(txt > 120, `${name} tab ${tab} renders (${txt} chars)`);
    await shot('tab-' + tab);
    await noSideways(page, `${name} tab ${tab}`);
  }

  // live: start the first channel
  await page.click('[data-tab="live"]:visible');
  await page.waitForTimeout(2000);
  const ch = await page.$('#list .ch, #list .item, .chlist .ch, #channels .ch');
  if (ch) {
    await ch.click();
    const played = await page.waitForFunction(() => { const v = document.querySelector('video'); return v && v.readyState >= 2 && v.currentTime > 0.5; }, null, { timeout: 30000 }).then(() => true).catch(() => false);
    ok(played, `${name} live channel plays`);
    await shot('playing');
    // mini player when going to Tonight
    await page.click('[data-tab="home"]:visible');
    await page.waitForTimeout(1500);
    const mini = await page.evaluate(() => document.body.classList.contains('mini'));
    ok(mini, `${name} mini player shows on Tonight`);
    await shot('mini');
    await noSideways(page, name + ' mini player');
  } else ok(false, `${name} no channel row found in Live`);

  // guide
  const g = await page.$('#btnGuide:visible');
  if (g) {
    await g.click();
    await page.waitForTimeout(3000);
    const rows = await page.$$eval('.gp', x => x.length);
    ok(rows > 10, `${name} guide shows programmes (${rows})`);
    await shot('guide');
    await page.keyboard.press('Escape');
  }
  // search
  await page.keyboard.press('Escape');
  await page.keyboard.press('/');
  await page.waitForTimeout(500);
  await page.keyboard.type('inter');
  await page.waitForTimeout(3500);
  await shot('search');
  await page.keyboard.press('Escape');
  // settings
  const st = await page.$('#btnSettings:visible');
  if (st) { await st.click(); await page.waitForTimeout(1500); await shot('settings'); await noSideways(page, name + ' settings'); await page.keyboard.press('Escape'); }
  ok(errors.length === 0, `${name} no JavaScript errors` + (errors.length ? ':\n   ' + [...new Set(errors)].slice(0, 8).join('\n   ') : ''));
  await browser.close();
}

(async () => {
  await run('mac', { viewport: { width: 1440, height: 900 } });
  await run('mac1280', { viewport: { width: 1280, height: 760 } });
  await run('phone', { ...devices['iPhone 13 Pro'] });
  console.log(failed ? `${failed} failed` : 'all passed');
  process.exit(failed ? 1 : 0);
})();
