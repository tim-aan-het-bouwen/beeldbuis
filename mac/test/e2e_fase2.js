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
  await page.click('#profiles button >> nth=1');
  await page.waitForTimeout(1500);
  const rowsJ = await page.$$eval('#home .hrow, #home .row', r => r.map(x => (x.querySelector('h2, h3, .rh') || {}).textContent));
  ok(!rowsJ.some(r => /For you/i.test(r || '')), 'second profile (empty taste) has no For you row: ' + rowsJ.join(' | '));
  await page.click('#profiles button >> nth=0');
  await page.waitForTimeout(1200);

  // detail card from a tile
  const hot = await page.$('#home .tile:has(.dots)');
  await hot.click();
  await page.waitForSelector('#dcard', { state: 'visible', timeout: 10000 });
  await page.waitForFunction(() => (document.querySelector('#dcardBody') || {}).textContent.length > 40, null, { timeout: 15000 });
  const dtext = await page.$eval('#dcardBody', e => e.textContent);
  ok(/IMDb|Mock overview|min/i.test(dtext), 'detail card has metadata');
  ok(await page.isVisible('#dPlay') || await page.isVisible('#dEps'), 'Play or Episodes button visible');
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

  // Movies tab opens on shelves (browse); search, genre chips, See all, then the classic list + player
  await page.click('.tabs button[data-tab="vod"]');
  await page.waitForSelector('#browse', { state: 'visible', timeout: 15000 });
  await page.waitForFunction(() => document.querySelectorAll('#bwrap .hrow').length >= 3, null, { timeout: 30000 });
  const shelves = await page.$$eval('#bwrap .hrow h2', x => x.map(e => e.textContent));
  console.log('shelves:', shelves);
  ok(shelves.length >= 3, 'movie shelves: ' + shelves.length);
  ok(await page.$$eval('#bchips .chip', x => x.length) > 3, 'genre chips shown');
  await page.screenshot({ path: 'e2e-browse.png' });
  const more = await page.$('#bwrap .hrow .more');
  if (more) {
    await more.click();
    await page.waitForFunction(() => document.querySelectorAll('#bwrap .bgrid .tile').length > 0, null, { timeout: 15000 });
    ok(true, 'See all opens a grid: ' + await page.$$eval('#bwrap .bgrid .tile', x => x.length));
    await page.click('#bback');
    await page.waitForSelector('#bwrap .hrow', { timeout: 15000 });
  }
  await page.click('#bchips .chip >> nth=1');
  await page.waitForFunction(() => document.querySelectorAll('#bwrap .bgrid .tile').length > 0, null, { timeout: 15000 });
  ok(true, 'genre chip filters: ' + await page.$eval('#bwrap .bhead h2', e => e.textContent));
  await page.fill('#bq', 'dune');
  await page.waitForFunction(() => /Dune/i.test((document.querySelector('#bwrap .bgrid') || {}).textContent || ''), null, { timeout: 15000 });
  const btext = await page.$eval('#bwrap', e => e.textContent);
  ok(/Dune/i.test(btext) && !/Heart Of The Beast/i.test(btext), 'browse search filters to Dune');
  await page.click('#bwrap .bgrid .tile >> nth=0');
  await page.waitForSelector('#dcard', { state: 'visible', timeout: 10000 });
  await page.click('#dClose');
  await page.click('#bList');
  await page.waitForFunction(() => document.querySelectorAll('#list .item').length > 20, null, { timeout: 30000 });
  ok(!(await page.isVisible('#browse')), 'List & player closes the shelves');
  const n = await page.$$eval('#list .item', x => x.length);
  ok(n > 20, 'movie library items: ' + n);
  await page.fill('#search', 'dune');
  await page.waitForTimeout(900);
  const ltext = await page.$eval('#list', e => e.textContent);
  ok(/Dune/i.test(ltext) && !/Heart Of The Beast/i.test(ltext), 'library search filters to Dune');
  await page.screenshot({ path: 'e2e-library.png' });

  // series tab + episodes in card
  await page.click('.tabs button[data-tab="series"]');
  await page.waitForSelector('#browse', { state: 'visible', timeout: 15000 });
  await page.waitForFunction(() => document.querySelectorAll('#bwrap .hrow, #bwrap .hempty').length > 0, null, { timeout: 30000 });
  ok((await page.$eval('#bq', e => e.placeholder)).includes('series'), 'series shelves');
  await page.fill('#bq', 'dune');
  await page.waitForFunction(() => document.querySelectorAll('#bwrap .bgrid .tile').length > 0, null, { timeout: 15000 });
  await page.click('#bwrap .bgrid .tile >> nth=0');
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
  ok(anchors.trim().length > 0, 'taste anchors loaded in settings');
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
