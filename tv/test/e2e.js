// End-to-end check of the TV app in the browser preview with the demo provider, using only "remote" keys.
// Run: python3 tv/tools/serve.py --port 8091 & node tv/test/e2e.js [shotsDir]
const { chromium } = require('playwright');
const URL = process.env.BB_URL || 'http://localhost:8091/';
const shots = process.argv[2];
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 1920, height: 1080 } });
  const errors = [];
  p.on('pageerror', e => errors.push(e.message));
  p.on('console', m => { if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(m.text()); });
  const key = async (k, n = 1) => { for (let i = 0; i < n; i++) { await p.keyboard.press(k); await p.waitForTimeout(120); } };
  const focused = () => p.evaluate(() => { const f = document.querySelector('.focus'); return f ? (f.id || f.className) + ' | ' + f.textContent.trim().slice(0, 40) : null; });
  const shot = async n => { if (shots) await p.screenshot({ path: `${shots}/${n}.png` }); };
  const step = (name, ok) => { console.log((ok ? 'ok   ' : 'FAIL ') + name); if (!ok) process.exitCode = 1; };

  await p.goto(URL);
  await p.evaluate(() => localStorage.clear()); await p.reload();
  await p.waitForSelector('#inServer');
  await shot('01-login');
  step('login screen focuses server field', (await focused()).startsWith('inServer'));
  await p.keyboard.type('demo');
  await key('Enter'); await key('Enter');        // to username, password
  await key('Enter');                              // to Sign in button
  step('Enter walks to Sign in', (await focused()).startsWith('btnLogin'));
  await key('Enter');
  await p.waitForSelector('.chip');
  await shot('02-countries');
  // pick NL (first chip has most categories... pick by text)
  const chips = await p.$$eval('.chip', cs => cs.map(c => c.dataset.k));
  step('country groups listed', chips.includes('NL') && chips.includes('UK'));
  while (!(await focused()).includes('NL')) await key('ArrowRight');
  await key('Enter');
  while (!(await focused()).includes('UK')) await key('ArrowRight');
  await key('Enter');
  await key('ArrowDown'); await key('Enter');
  await p.waitForSelector('.home');
  await shot('03-home-empty');
  step('empty home shows mascot', await p.$('.empty img') !== null);

  // Live TV via tabs
  await key('ArrowRight');
  step('tabs reachable', (await focused()).includes('Live TV'));
  await key('Enter');
  await p.waitForSelector('.chan');
  await shot('04-live');
  const names = await p.$$eval('.chan-name', e => e.map(x => x.textContent));
  step('NPO 1 HD+FHD merged into one channel', names.filter(n => n === 'NPO 1').length === 1);
  await key('ArrowRight');
  step('right moves into channel list', (await focused()).startsWith('f chan'));
  await key('y');  // not mapped: just make sure nothing breaks
  await p.keyboard.press('Enter');
  await p.waitForFunction(() => document.body.classList.contains('playing'));
  await p.waitForFunction(() => !document.getElementById('video').paused && document.getElementById('video').currentTime > 0.5, null, { timeout: 15000 });
  await shot('05-playing-live');
  step('live channel plays', true);
  await key('PageDown');
  await p.waitForTimeout(1500);
  step('channel down zaps', (await p.textContent('#osdTitle')) !== 'NPO 1');
  await key('Escape');
  step('back leaves player', !(await p.evaluate(() => document.body.classList.contains('playing'))));

  // favourite with yellow (keyCode 405) on focused channel
  await p.evaluate(() => document.dispatchEvent(new KeyboardEvent('keydown', { keyCode: 405, bubbles: true })));
  await p.waitForTimeout(200);
  step('yellow adds favourite', (await p.evaluate(() => JSON.parse(localStorage.getItem('beeldbuis.v1')).favorites.length)) === 1);

  // Movies
  await p.click('.tab[data-tab="movies"]');
  await p.waitForSelector('#vodGrid .tile');
  await shot('06-movies');
  await key('ArrowRight'); await key('Enter');
  await p.waitForSelector('#modal');
  await p.waitForTimeout(400);
  await shot('07-movie-detail');
  await key('Enter');
  await p.waitForFunction(() => document.getElementById('video').currentTime > 1, null, { timeout: 15000 });
  await key('ArrowRight');
  await p.waitForTimeout(400);
  await shot('08-playing-movie');
  await p.waitForTimeout(2000);
  await key('Escape');
  step('movie plays and back returns', !(await p.evaluate(() => document.body.classList.contains('playing'))));

  // Series
  await p.click('.tab[data-tab="series"]');
  await p.waitForFunction(() => /Blue Fox/.test(document.getElementById('vodGrid') ? document.getElementById('vodGrid').textContent : ''));
  await key('ArrowRight');
  await key('Enter');
  await p.waitForSelector('.ep');
  await shot('09-series');
  await key('ArrowDown');
  step('down from seasons reaches episodes', (await focused()).startsWith('f ep'));
  await key('Enter');
  await p.waitForFunction(() => document.getElementById('video').currentTime > 0.5, null, { timeout: 15000 });
  step('episode plays', (await p.textContent('#osdTitle')).includes('S1E1'));
  await key('Escape');
  await key('Escape');

  // Home now has rows
  await p.click('.tab[data-tab="home"]');
  await p.waitForSelector('.row');
  await shot('10-home');
  const rows = await p.$$eval('.row h2', e => e.map(x => x.textContent));
  step('home rows: ' + rows.join(', '), rows.includes('Favourite channels') && rows.includes('Recently watched'));

  // Search
  await p.click('.tab[data-tab="search"]');
  await p.waitForSelector('#sq');
  step('search field focused', (await focused()).startsWith('sq'));
  await p.keyboard.type('fox');
  await p.waitForFunction(() => /Blue Fox/.test(document.getElementById('sResults').textContent), null, { timeout: 15000 });
  await p.waitForFunction(() => !document.getElementById('sStatus').textContent, null, { timeout: 15000 });
  await shot('11-search');
  await key('Enter');
  step('OK in search field jumps to results', (await focused()).includes('Blue Fox'));
  await key('Enter');
  await p.waitForSelector('.ep');
  await key('Escape');
  await p.waitForSelector('#sq');
  step('back from series returns to search', (await p.inputValue('#sq')) === 'fox');
  await p.fill('#sq', 'npo');
  await p.waitForFunction(() => /NPO 1/.test(document.getElementById('sResults').textContent), null, { timeout: 15000 });
  step('search finds channels', true);

  // Settings
  await p.click('.tab[data-tab="settings"]');
  await p.waitForSelector('.setting');
  await shot('12-settings');
  await key('Escape');
  step('back from settings goes home', await p.$('.home') !== null);
  await key('Escape');
  step('back on home asks to close', await p.$('#modal') !== null);
  await shot('13-exit');

  step('no script errors' + (errors.length ? ': ' + errors.join(' / ') : ''), errors.length === 0);
  await b.close();
})();
