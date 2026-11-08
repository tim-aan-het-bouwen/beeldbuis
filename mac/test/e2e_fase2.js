// Fase 2/3 UI test against the mock provider + mock metadata: Tonight, library, search, detail card, settings.
const { chromium } = require('playwright');
const BASE = process.env.BASE || 'http://127.0.0.1:8765';
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  const errors = [];
  page.on('pageerror', e => errors.push('pageerror: ' + e.message));
  page.on('console', m => { if (m.type() === 'error') errors.push('console: ' + m.text()); });
  const ok = (c, msg) => { console.log((c ? 'PASS ' : 'FAIL ') + msg); if (!c) process.exitCode = 1; };

  await page.goto(BASE);
  await page.waitForSelector('#home', { state: 'visible', timeout: 20000 });
  await page.waitForFunction(() => document.querySelectorAll('#home .tile').length > 10, null, { timeout: 60000 });
  const rows = await page.$$eval('#home .hrow, #home .row', r => r.map(x => (x.querySelector('h2, h3, .rh') || {}).textContent));
  console.log('rows:', rows);
  ok(rows.some(r => /Hot right now/i.test(r || '')), 'Hot right now row present');
  ok(rows.some(r => /New this week/i.test(r || '')), 'New this week row present');
  const tiles = await page.$$('#home .tile');
  ok(tiles.length > 10, 'tiles on home: ' + tiles.length);
  const imgs = await page.$$eval('#home .tile img', i => i.filter(x => x.complete && x.naturalWidth > 0).length);
  ok(imgs > 5, 'posters loaded: ' + imgs);
  await page.screenshot({ path: 'e2e-home.png' });

  // profile chips
  const chips = await page.$$eval('#profiles button, #profiles .chip', c => c.map(x => x.textContent.trim()));
  console.log('profiles:', chips);
  ok(chips.length === 3, 'three profiles');
  await page.click('#profiles >> text=Joanna');
  await page.waitForTimeout(1500);
  const rowsJ = await page.$$eval('#home .hrow, #home .row', r => r.map(x => (x.querySelector('h2, h3, .rh') || {}).textContent));
  ok(!rowsJ.some(r => /For you/i.test(r || '')), 'Joanna (empty taste) has no For you row: ' + rowsJ.join(' | '));
  await page.click('#profiles >> text=Tim');
  await page.waitForTimeout(1200);

  // detail card from a tile
  const hot = await page.$('#home .tile:has(.dots)');
  await hot.click();
  await page.waitForSelector('#dcard', { state: 'visible', timeout: 10000 });
  await page.waitForFunction(() => (document.querySelector('#dcardBody') || {}).textContent.length > 40, null, { timeout: 15000 });
  const dtext = await page.$eval('#dcardBody', e => e.textContent);
  ok(/IMDb|Mock overview|min/i.test(dtext), 'detail card has metadata');
  ok(await page.isVisible('#dPlay'), 'Play button visible');
  await page.screenshot({ path: 'e2e-dcard.png' });
  // feedback thumbs
  await page.click('#dDown');
  await page.waitForTimeout(500);
  await page.click('#dClose');

  // global search
  await page.fill('#gsearchAll', 'dune');
  await page.waitForSelector('#sres', { state: 'visible', timeout: 10000 });
  await page.waitForFunction(() => document.querySelectorAll('#sres .tile, #sres .srow, #sres li').length > 2, null, { timeout: 15000 });
  const stext = await page.$eval('#sres', e => e.textContent);
  ok(/Dune: Part Two/i.test(stext), 'search finds Dune: Part Two');
  await page.screenshot({ path: 'e2e-search.png' });
  await page.fill('#gsearchAll', '');
  await page.waitForTimeout(400);

  // library tab with posters
  await page.click('.tabs button[data-tab="vod"]');
  await page.waitForFunction(() => document.querySelectorAll('#list .item, #list .tile, #list li').length > 20, null, { timeout: 30000 });
  const n = await page.$$eval('#list .item, #list .tile, #list li', x => x.length);
  ok(n > 20, 'movie library items: ' + n);
  await page.fill('#search', 'dune');
  await page.waitForTimeout(900);
  const ltext = await page.$eval('#list', e => e.textContent);
  ok(/Dune/i.test(ltext) && !/Heart Of The Beast/i.test(ltext), 'library search filters to Dune');
  await page.screenshot({ path: 'e2e-library.png' });
  // click a library item -> detail card
  await page.click('#list .item >> nth=0');
  await page.waitForSelector('#dcard', { state: 'visible', timeout: 10000 });
  await page.click('#dClose');

  // series tab + episodes in card
  await page.click('.tabs button[data-tab="series"]');
  await page.fill('#search', 'dune');
  await page.waitForTimeout(900);
  await page.click('#list .item >> nth=0');
  await page.waitForSelector('#dcard', { state: 'visible', timeout: 10000 });
  await page.waitForFunction(() => document.querySelectorAll('#dEpisodes .item').length > 0, null, { timeout: 20000 }).catch(() => {});
  const eps = await page.$$eval('#dEpisodes .item', x => x.length);
  ok(eps > 0, 'episodes listed: ' + eps);
  await page.screenshot({ path: 'e2e-series-card.png' });
  await page.click('#dClose');

  // settings: keys + taste
  await page.click('#btnSettings');
  await page.waitForSelector('#settingsModal', { state: 'visible' });
  const ktext = await page.$eval('#sKeysInfo', e => e.textContent).catch(() => '');
  console.log('keys info:', ktext);
  ok(/TMDb/i.test(ktext), 'keys status shown');
  const anchors = await page.$eval('#sAnchors', e => e.value).catch(() => '');
  ok(/Pluribus/.test(anchors), 'taste anchors loaded in settings');
  await page.screenshot({ path: 'e2e-settings2.png', fullPage: true });
  await page.click('#sCancel');

  // back to Tonight via tab
  await page.click('.tabs button[data-tab="home"]');
  await page.waitForSelector('#home', { state: 'visible' });
  ok(true, 'back to Tonight');

  console.log('errors:', errors);
  ok(errors.filter(e => !/favicon|net::ERR_|Failed to load resource/.test(e)).length === 0, 'no JS errors');
  await browser.close();
})().catch(e => { console.error('E2E CRASH', e); process.exit(1); });
