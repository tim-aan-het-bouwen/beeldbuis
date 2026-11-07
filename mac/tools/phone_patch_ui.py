#!/usr/bin/env python3
"""Update 6: iPhone/iPad web app — mobile layout, touch, native HLS on iOS, pairing, device takeover.
Applies to src/ui.html in place; every replacement must match exactly once."""
import os
import sys

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "ui.html")
src = open(P, encoding="utf-8").read()
if "pairModal" in src:
    sys.exit("already patched")


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (old[:120], n)
    src = src.replace(old, new)


# ---------------------------------------------------------------- head
rep('<meta name="viewport" content="width=device-width, initial-scale=1">',
    '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
    '<meta name="apple-mobile-web-app-capable" content="yes">\n<meta name="mobile-web-app-capable" content="yes">\n'
    '<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">\n<meta name="apple-mobile-web-app-title" content="Beeldbuis">\n'
    '<meta name="format-detection" content="telephone=no">')
rep('<link rel="manifest" href="/manifest.json">', '<link rel="manifest" id="manifestLink" href="/manifest.json">')
rep('<link rel="apple-touch-icon" href="/assets/icon-192.png">', '<link rel="apple-touch-icon" sizes="180x180" href="/assets/icon-180.png">')

# ---------------------------------------------------------------- css
rep('''  .pcard .row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
</style>''', '''  .pcard .row{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex-wrap:wrap}
  /* ---- phone pairing + settings */
  .pairbox{display:flex;gap:16px;align-items:flex-start;flex-wrap:wrap}
  .pairbox .qr{background:#f4e8c8;border-radius:12px;padding:8px;line-height:0;flex:none}
  .pairbox .qr img{width:148px;height:148px;image-rendering:pixelated}
  .pairbox .info{flex:1;min-width:220px;font-size:13px;color:var(--text)}
  .pairbox .code{font:700 26px/1.1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.18em;color:var(--ochre);margin:2px 0 8px}
  .pairbox .url{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;color:var(--cream);word-break:break-all}
  .devs{margin-top:8px}
  .devs .d{display:flex;align-items:center;gap:8px;padding:5px 0;border-top:1px solid var(--line);font-size:13px}
  .devs .d span{flex:1}
  #pairCode{font:700 28px/1.2 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.3em;text-align:center}
  .closeplayer{display:none}
  .tapplay{font-size:15px;padding:12px 22px}
  /* ---- phone layout (iPhone, small windows) */
  @media (max-width: 760px), (pointer: coarse) and (max-height: 520px) {
    html,body{font-size:15px}
    #app{height:100vh;height:100dvh;grid-template-columns:minmax(0,1fr);grid-template-rows:calc(50px + env(safe-area-inset-top)) 1fr;padding-bottom:calc(58px + env(safe-area-inset-bottom))}
    .topbar{padding:env(safe-area-inset-top) max(10px, env(safe-area-inset-right)) 0 max(10px, env(safe-area-inset-left));gap:8px}
    .topbar .wordmark{height:30px}
    .topbar .meta,#btnFull{display:none}
    .tabs{position:fixed;left:0;right:0;bottom:0;z-index:25;margin:0;gap:0;height:calc(58px + env(safe-area-inset-bottom));padding:0 env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);background:var(--panel);border-top:2px solid var(--ink)}
    .tabs button{flex:1;padding:6px 2px 0;border-bottom:0;border-top:3px solid transparent;font-size:13px;-webkit-tap-highlight-color:transparent}
    .tabs button.active{border-top-color:var(--ochre);border-bottom-color:transparent}
    .body{grid-template-columns:minmax(0,1fr);grid-template-rows:auto minmax(0,1fr)}
    main{order:-1}
    body.idle main{display:none}
    .player{flex:none;aspect-ratio:16/9;max-height:56vh}
    .overlay{padding:10px}
    .overlay .box{padding:12px 16px;border-radius:12px}
    .overlay .mascot,.overlay .mascot.small{width:56px;height:56px;border-radius:12px;margin-bottom:8px}
    .overlay .lead{font-size:15px}
    .seek{padding:10px 12px}
    .infobar{min-height:0;padding:8px 12px 10px}
    .infobar .title{font-size:15px}
    .closeplayer{display:inline-block}
    .epg{grid-template-columns:1fr;gap:6px}
    .epg .d{display:none}
    .epg>div:nth-child(2) .lbl{display:inline}
    .epg>div:nth-child(2) .t{display:inline;margin-left:6px;font-weight:500;color:var(--muted)}
    aside{border-right:0}
    .controls{flex-direction:row;padding:10px 10px 6px}
    .controls input{flex:1.4;min-width:0}
    .controls select{flex:1;min-width:0}
    input,select,textarea{font-size:16px !important}
    #list{padding:0 4px 12px;-webkit-overflow-scrolling:touch}
    .item{padding:9px 8px;gap:12px}
    .item img,.item .ph{width:44px;height:44px}
    .item .star{font-size:22px;padding:6px 8px}
    .item .act{font-size:17px;padding:6px 8px}
    .item:hover{background:transparent}
    .item:active{background:var(--panel2)}
    #guide{top:calc(50px + env(safe-area-inset-top));bottom:calc(58px + env(safe-area-inset-bottom))}
    .gbar{padding:8px 10px;gap:6px}
    .gbar .days{order:3;width:100%;overflow-x:auto;flex-wrap:nowrap;-webkit-overflow-scrolling:touch}
    .gbar .days button{flex:none}
    .gbar select{flex:1;max-width:none;min-width:0}
    .gbar input{flex:1;width:auto;min-width:0}
    .gbar .spacer,#gstatus{display:none}
    .grid{grid-template-columns:118px 1fr}
    .gch{padding:0 6px;gap:6px}
    .gch img{width:24px;height:24px}
    .gch .n{font-size:12px}
    .gres{padding:6px 8px}
    .gres .r{flex-wrap:wrap;gap:2px 10px}
    .gres .r .tm,.gres .r .ch{width:auto}
    .gres .r .t{width:100%}
    .pcard{left:0;right:0;top:auto;bottom:0;transform:none;width:auto;max-width:none;border-radius:18px 18px 0 0;max-height:80vh;overflow:auto;padding:18px 16px calc(18px + env(safe-area-inset-bottom))}
    .pcard .row .btn{flex:1 1 40%;padding:11px 12px}
    .modal{align-items:flex-end}
    .card,.card.wide{width:100%;max-width:none;border-radius:18px 18px 0 0;max-height:92vh;padding:18px 16px calc(18px + env(safe-area-inset-bottom))}
    .card{overflow:auto}
    .card .row{flex-wrap:wrap}
    .card .row .btn{padding:10px 14px}
    #toasts{left:12px;right:12px;bottom:auto;top:calc(56px + env(safe-area-inset-top))}
    .toast{max-width:none}
    .menu{max-width:calc(100vw - 20px)}
    .menu .mi{padding:11px 10px}
  }
  @media (pointer: coarse) and (max-height: 520px) and (orientation: landscape) {
    .body{grid-template-columns:minmax(0,2fr) minmax(0,3fr);grid-template-rows:minmax(0,1fr)}
    main{order:0}
    .player{flex:1;aspect-ratio:auto;max-height:none}
    .infobar{padding:6px 10px}
    .infobar #epgbox{display:none}
  }
  @media (hover: none) { .btn:hover{border-color:var(--line)} .gp:hover{border-color:var(--line);background:var(--panel2)} .gch:hover{background:var(--panel)} }
</style>''')

# ---------------------------------------------------------------- html
rep('<body>\n<div id="app">', '<body class="idle">\n<div id="app">')
rep('''<div class="lead">Pick a channel to start</div><span class="hint"><kbd>↑</kbd>''', '''<div class="lead">Pick a channel to start</div><span class="hint keyhint"><kbd>↑</kbd>''')
rep('''<button class="btn small" id="btnQuality" style="display:none">Quality</button></div>''',
    '''<button class="btn small" id="btnQuality" style="display:none">Quality</button><button class="btn small closeplayer" id="btnClosePlayer" title="Close the player">✕</button></div>''')
rep('''<!-- login -->''', '''<!-- pair an iPhone / iPad -->
<div class="modal hidden" id="pairModal">
  <div class="card">
    <img class="brand" src="/assets/wordmark-mascot.png" alt="Beeldbuis" draggable="false">
    <h2>Connect to Beeldbuis on your Mac</h2>
    <p>On the Mac, open Beeldbuis → Settings (⚙) → <b>iPhone &amp; iPad</b> and type the six-digit code here. You only do this once per device.</p>
    <input id="pairCode" inputmode="numeric" pattern="[0-9]*" maxlength="6" autocomplete="one-time-code" placeholder="000000">
    <div class="msg" id="pairMsg"></div>
    <div class="row"><button class="btn primary" id="btnPair">Connect</button></div>
  </div>
</div>

<!-- login -->''')
rep('''      <h3>TV playlist <span class="hint">Samsung · SmartOne IPTV</span></h3>''', '''      <div id="sPhoneSec" class="maconly">
      <h3>iPhone &amp; iPad <span class="hint">same Wi-Fi as this Mac</span></h3>
      <label><input type="checkbox" id="sPhone" style="accent-color:var(--teal);width:15px;height:15px;vertical-align:-2px"> Allow my iPhone and iPad to use Beeldbuis (each device needs the code once)</label>
      <div class="pairbox" id="sPhoneBox" style="margin-top:10px">
        <div class="qr" id="sPhoneQr"></div>
        <div class="info">
          <div class="hint">Code</div><div class="code" id="sPhoneCode">······</div>
          <div>Scan the square with the iPhone camera, or open this in Safari:</div><div class="url" id="sPhoneUrl"></div>
          <div class="hint" style="margin-top:6px">Then tap Share → <b>Add to Home Screen</b>. The Mac must be on and awake; one connection means the iPhone takes over what the Mac is playing.</div>
          <p style="margin:8px 0 0"><button class="btn small" id="sPhoneNew">New code</button></p>
        </div>
      </div>
      <div class="devs" id="sPhoneDevs"></div>
      </div>
      <div id="sThisDev" class="phoneonly" style="display:none">
      <h3>This device</h3>
      <p class="hint" id="sThisDevInfo"></p>
      <p><button class="btn small danger" id="sLeave">Disconnect this device</button></p>
      </div>
      <div id="sTvSec" class="maconly">
      <h3>TV playlist <span class="hint">Samsung · SmartOne IPTV</span></h3>''')
rep('''      <p style="margin-top:8px"><button class="btn primary small" id="sTvExport">Export TV playlist</button> <span class="hint" id="sTvInfo"></span></p>''',
    '''      <p style="margin-top:8px"><button class="btn primary small" id="sTvExport">Export TV playlist</button> <span class="hint" id="sTvInfo"></span></p>
      </div>''')
rep('''<script src="/assets/hls.min.js"''', '''<script src="/assets/qrcode.js" defer></script>
<script src="/assets/hls.min.js"''')

# ---------------------------------------------------------------- js: environment + device key
rep('''const ls = { get(k, d){''', '''// iPhone / iPad: Safari plays HLS itself (smoother, AirPlay, picture-in-picture, lock-screen controls)
const IOS = /iP(hone|ad|od)/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const NATIVE_HLS = IOS && !!video.canPlayType('application/vnd.apple.mpegurl');
const TOUCH = matchMedia('(pointer: coarse)').matches;
const MOBILE = matchMedia('(max-width: 760px), (pointer: coarse) and (max-height: 520px)').matches;
let me = { id: 'mac', name: 'Mac', remote: false };
const ls = { get(k, d){''')
rep('''async function api(path, opts) {
  let r;
  try { r = await fetch(path, opts); }
  catch (e) { const err = new Error('No connection to the player'); err.offline = true; throw err; }
  const j = await r.json();
  if (!j.ok) throw new Error(j.error || 'Unknown error');
  return j.data;
}''', '''// a paired iPhone/iPad sends its device key with every request (cookie as well, but home-screen apps keep their own)
let devKey = ls.get('bb_key', '');
(() => { const u = new URL(location.href), k = u.searchParams.get('k');
  if (k) { devKey = k; ls.set('bb_key', k); u.searchParams.delete('k'); history.replaceState(null, '', u.pathname + u.search); } })();
function withKey(url){ return devKey && me.remote ? url + (url.includes('?') ? '&' : '?') + 'k=' + encodeURIComponent(devKey) : url; }
function setManifest(){ if (devKey) $('#manifestLink').href = '/manifest.json?k=' + encodeURIComponent(devKey); }
setManifest();
async function api(path, opts) {
  let r;
  const sentKey = devKey;
  opts = Object.assign({}, opts || {});
  if (devKey) opts.headers = Object.assign({ 'X-Beeldbuis-Key': devKey }, opts.headers || {});
  try { r = await fetch(path, opts); }
  catch (e) { const err = new Error('No connection to the player'); err.offline = true; throw err; }
  let j;
  try { j = await r.json(); } catch { throw new Error('Unexpected answer from the player (HTTP ' + r.status + ')'); }
  if (!j.ok) { const err = new Error(j.error || 'Unknown error'); if (j.pair) { err.pair = true; if (devKey === sentKey) { if (devKey) { devKey = ''; try { localStorage.removeItem('bb_key'); } catch {} } showPair(); } } throw err; }
  return j.data;
}
const sfetch = (path, opts) => fetch(path, Object.assign({}, opts || {}, devKey ? { headers: Object.assign({ 'X-Beeldbuis-Key': devKey }, (opts || {}).headers || {}) } : {}));
function showPair(msg){
  $('#loginModal').classList.add('hidden');
  $('#pairModal').classList.remove('hidden');
  if (msg) $('#pairMsg').textContent = msg;
}
async function doPair(code){
  $('#pairMsg').textContent = 'Connecting…';
  try {
    const r = await fetch('/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ code }) });
    const j = await r.json();
    if (!j.ok) { $('#pairMsg').textContent = j.error || 'That did not work.'; return; }
    devKey = j.data.key; ls.set('bb_key', devKey); setManifest();
    $('#pairMsg').textContent = ''; $('#pairModal').classList.add('hidden');
    toast('Connected as <b>' + esc(j.data.name) + '</b>.' + (IOS && !navigator.standalone ? '<br>Tip: tap Share → <b>Add to Home Screen</b> for a full-screen Beeldbuis app.' : ''), '', 9000);
    checkStatus();
  } catch { $('#pairMsg').textContent = 'No connection to the Mac. Is it on, awake and on the same Wi-Fi?'; }
}
$('#btnPair').onclick = () => doPair($('#pairCode').value.trim());
$('#pairCode').addEventListener('input', e => { const v = e.target.value.replace(/\\D/g, '').slice(0, 6); e.target.value = v; if (v.length === 6) doPair(v); });
(() => { const u = new URL(location.href), c = u.searchParams.get('pair');
  if (c) { history.replaceState(null, '', u.pathname); if (!devKey) setTimeout(() => doPair(c), 0); } })();''')
rep('''  showOverlay('<img class="mascot small" src="/assets/icon-192.png" alt=""><div class="err">The player service is not reachable.</div>Open Beeldbuis again from its app icon; this window will reconnect by itself.<br><br><span class="spin"></span>Reconnecting…');''',
    '''  showOverlay('<img class="mascot small" src="/assets/icon-192.png" alt=""><div class="err">The player service is not reachable.</div>' + (me.remote ? 'Is the Mac on, awake and on the same Wi-Fi? This screen reconnects by itself.' : 'Open Beeldbuis again from its app icon; this window will reconnect by itself.') + '<br><br><span class="spin"></span>Reconnecting…');
  document.body.classList.remove('idle');''')
rep('''    try { await api('/api/ping'); clearInterval(offlineTimer); offlineTimer = null; hideOverlay(); checkStatus(); } catch {}''',
    '''    try { await api('/api/ping'); clearInterval(offlineTimer); offlineTimer = null; hideOverlay(); if (!current) document.body.classList.add('idle'); checkStatus(); } catch (e) { if (e.pair) { clearInterval(offlineTimer); offlineTimer = null; hideOverlay(); } }''')

# ---------------------------------------------------------------- js: status knows which device this is
rep('''    const s = status = await api('/api/status');
    loggedIn = s.logged_in;''', '''    const s = status = await api('/api/status');
    if (s.client) { me = s.client; applyDevice(); }
    loggedIn = s.logged_in;''')
rep('''    } else $('#loginModal').classList.remove('hidden');
  } catch (e) {
    if (e.offline) return goOffline();''', '''    } else if (me.remote) {
      showOverlay('<img class="mascot small" src="/assets/icon-192.png" alt=""><div class="lead">Not signed in yet</div>Sign in to your IPTV provider in Beeldbuis on the Mac first, then come back here.');
      document.body.classList.remove('idle');
    } else $('#loginModal').classList.remove('hidden');
  } catch (e) {
    if (e.offline) return goOffline();
    if (e.pair) return;''')
rep('''// ---------- channel catalogue (live)''', '''function applyDevice(){
  document.querySelectorAll('.maconly').forEach(el => el.style.display = me.remote ? 'none' : '');
  document.querySelectorAll('.phoneonly').forEach(el => el.style.display = me.remote ? '' : 'none');
  $('#btnAccount').style.display = me.remote ? 'none' : '';
}
if (TOUCH) document.querySelectorAll('.keyhint').forEach(el => el.textContent = 'Tap a channel; the guide is in the bar below.');

// ---------- channel catalogue (live)''')
rep('''  if (!rows.length) { listEl.innerHTML = '<div class="empty"><img src="/assets/icon-192.png" alt="">' + (catEl.value==='__fav' ? 'No favourites yet. Click ☆ next to a channel.' : 'Nothing found') + '</div>'; return; }''',
    '''  if (!rows.length) { listEl.innerHTML = '<div class="empty"><img src="/assets/icon-192.png" alt="">' + (catEl.value==='__fav' ? 'No favourites yet. ' + (TOUCH ? 'Tap' : 'Click') + ' ☆ next to a channel.' : 'Nothing found') + '</div>'; return; }''')

# ---------------------------------------------------------------- js: playback
rep('''function stopPlayback(){
  playToken++;''', '''function stopPlayback(keepServer){
  playToken++;''')
rep('''  $('#btnCC').style.display = 'none';
  fetch('/api/stop').catch(()=>{});
}''', '''  $('#btnCC').style.display = 'none';
  if (!keepServer) sfetch('/api/stop').catch(()=>{});
}
function closePlayer(){ stopPlayback(); current = null; $('#title').textContent = 'Nothing playing'; document.body.classList.add('idle'); document.querySelectorAll('.item.active').forEach(el => el.classList.remove('active')); }
$('#btnClosePlayer').onclick = closePlayer;
// another device (Mac / iPhone) started something: the single connection moved there
function takenOver(by){
  if (!current || current.takenShown) return;
  current.takenShown = true;
  const x = current.x, t = current.kind !== 'live' ? current.offset + (video.currentTime || 0) : undefined;
  saveProgress(true);
  stopPlayback(true);
  showOverlay(`<img class="mascot small" src="/assets/icon-192.png" alt=""><div class="lead">Now playing on ${esc(by || 'another device')}</div><div style="margin-bottom:12px" class="hint">Your subscription allows one connection, so playback moved there.</div><button class="btn primary" id="takeBack">Watch here instead</button>`);
  $('#takeBack').onclick = () => play(x, t !== undefined ? { t } : undefined);
}
async function checkTaken(){
  await pingLoop();
  return !!(current && current.takenShown);
}
// iOS only allows sound after a tap: "unlock" the video element on the first touch
let unlocked = false;
document.addEventListener('touchend', () => { if (!unlocked && IOS) { unlocked = true; try { const p = video.play(); if (p) p.catch(()=>{}); } catch {} } }, { capture: true, passive: true });
function startVideo(token){
  const p = video.play();
  if (p) p.catch(e => {
    if (token !== playToken || !e || e.name !== 'NotAllowedError') return;
    showOverlay('<button class="btn primary tapplay" id="tapPlay">▶ Tap to play</button>');
    $('#tapPlay').onclick = () => { hideOverlay(); video.play().catch(()=>{}); };
  });
}''')
rep('''  current = { x, group, variant, key, kind, ext, sid,''', '''  document.body.classList.remove('idle');
  current = { x, group, variant, key, kind, ext, sid,''')
rep('''  if (current.cu) url += `&start=${current.cu.start}&dur=${current.cu.dur}`;''', '''  if (current.cu) url += `&start=${current.cu.start}&dur=${current.cu.dur}`;
  if (NATIVE_HLS) url += '&native=1';''')
rep('''  if (!window.Hls || !Hls.isSupported()) { fail('This browser does not support HLS playback (use Chrome).', x); return; }''',
    '''  if (!NATIVE_HLS && (!window.Hls || !Hls.isSupported())) { fail('This browser does not support HLS playback (use Chrome or Safari).', x); return; }''')
rep('''    fetch('/api/stop').catch(()=>{});
    current.offset = 0;
    video.onerror = () => { if (token===playToken) play(x, { t: current.offset + (video.currentTime||0), force: true }); };
    video.onloadeddata = () => { if (token===playToken) { hideOverlay(); if (resumeFrom || opts.t) video.currentTime = opts.t || resumeFrom; } };
    video.src = res.direct;
    video.play().catch(()=>{});''', '''    sfetch('/api/stop').catch(()=>{});
    current.offset = 0;
    video.onerror = async () => { if (token!==playToken) return; if (await checkTaken()) return; if (token===playToken) play(x, { t: current.offset + (video.currentTime||0), force: true }); };
    video.onloadeddata = () => { if (token===playToken) { hideOverlay(); if (resumeFrom || opts.t) video.currentTime = opts.t || resumeFrom; } };
    video.src = withKey(res.direct);
    startVideo(token);''')
rep('''  hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 4, liveMaxLatencyDurationCount: 30,''', '''  if (NATIVE_HLS) {                 // iPhone / iPad: Safari's own HLS player
    current.native = true;
    video.onerror = async () => {
      if (token !== playToken) return;
      if (await checkTaken() || token !== playToken) return;
      const at = current.offset + (video.currentTime || 0);
      if (!opts.force && kind !== 'live') { play(x, { t: at, force: true, variant, tried: current.tried }); return; }
      if (!tryFallback('failed')) fail('This stream could not be played' + (video.error && video.error.message ? ' (' + video.error.message + ')' : '') + '.', x);
    };
    video.textTracks.onaddtrack = () => { if (token === playToken) setupNativeSubs(); };
    video.onloadeddata = () => { if (token === playToken) { hideOverlay(); setupNativeSubs(); if (typeof localSeek !== 'undefined' && localSeek > 0.5) video.currentTime = localSeek; } };
    video.src = res.playlist;
    startVideo(token);
  } else {
  hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 4, liveMaxLatencyDurationCount: 30,''')
rep('''  hls.on(Hls.Events.ERROR, (ev, d) => {
    if (token !== playToken || !d.fatal) return;
    console.warn('hls fatal', d.type, d.details);''', '''  hls.on(Hls.Events.ERROR, async (ev, d) => {
    if (token !== playToken || !d.fatal) return;
    console.warn('hls fatal', d.type, d.details);
    if (d.type === Hls.ErrorTypes.NETWORK_ERROR && (await checkTaken() || token !== playToken)) return;''')
rep('''    if (!tryFallback('failed')) fail('This stream could not be played (' + d.details + ').', x);
  });
''', '''    if (!tryFallback('failed')) fail('This stream could not be played (' + d.details + ').', x);
  });
  }
''')
rep('''  hls.on(Hls.Events.MANIFEST_PARSED, () => { if (token===playToken) video.play().catch(()=>{}); });''',
    '''  hls.on(Hls.Events.MANIFEST_PARSED, () => { if (token===playToken) startVideo(token); });''')
rep('''  if (hls && video.buffered.length && local >= 0 && local < video.duration - 1) { video.currentTime = local; return; }''',
    '''  if ((hls || current.native) && video.buffered.length && local >= 0 && local < (isFinite(video.duration) ? video.duration : video.buffered.end(video.buffered.length - 1)) - 1) { video.currentTime = local; return; }''')
rep('''$('#btnCC').onclick = () => {
  if (!hls) return;''', '''function nativeSubTrack(){ return [...video.textTracks].find(t => (t.kind === 'subtitles' || t.kind === 'captions') && /^en/i.test(t.language || t.label || '')) || [...video.textTracks].find(t => t.kind === 'subtitles'); }
function setupNativeSubs(){
  const t = nativeSubTrack(); if (!t) return;
  const on = settings.subs !== false, cc = $('#btnCC');
  [...video.textTracks].forEach(x => { if (x !== t && x.kind === 'subtitles') x.mode = 'disabled'; });
  t.mode = on ? 'showing' : 'disabled';
  cc.style.display = ''; cc.classList.toggle('on', on); cc.textContent = 'CC ' + (on ? 'on' : 'off');
}
$('#btnCC').onclick = () => {
  if (current && current.native) {
    const t = nativeSubTrack(); if (!t) return;
    const on = t.mode !== 'showing'; t.mode = on ? 'showing' : 'disabled';
    $('#btnCC').classList.toggle('on', on); $('#btnCC').textContent = 'CC ' + (on ? 'on' : 'off');
    settings.subs = on; post('/api/settings', { subs: on }).catch(()=>{});
    return;
  }
  if (!hls) return;''')
rep('''function fullscreen(){ const el = document.querySelector('.player'); if (document.fullscreenElement) document.exitFullscreen(); else el.requestFullscreen && el.requestFullscreen(); }''',
    '''function fullscreen(){
  if (IOS && video.webkitEnterFullscreen && !document.documentElement.requestFullscreen) { try { video.webkitEnterFullscreen(); } catch {} return; }
  const el = document.querySelector('.player'); if (document.fullscreenElement) document.exitFullscreen(); else el.requestFullscreen && el.requestFullscreen();
}''')

# ---------------------------------------------------------------- js: ping notices takeovers
rep('''    const r = await fetch('/api/ping'); const j = await r.json(); lastPing = j;''', '''    const r = await sfetch('/api/ping'); const j = await r.json();
    if (!j.ok) { if (j.pair && !devKey) showPair(); return; }
    lastPing = j;
    if (j.taken && current && !current.takenShown && j.taken.t >= current.at - 2) takenOver(j.taken.by);''')

# ---------------------------------------------------------------- js: guide sizes on a phone
rep('''const PX = 4;                     // pixels per minute''', '''const PX = MOBILE ? 3 : 4;        // pixels per minute
const CHW = MOBILE ? 118 : 220;   // width of the channel column''')
rep('''nl.style.left = (220 + (now - from) / 60 * PX) + 'px';''', '''nl.style.left = (CHW + (now - from) / 60 * PX) + 'px';''')
rep('''  else if (from <= now && now < to) wrap.scrollLeft = Math.max(0, (now - from) / 60 * PX - 200);''',
    '''  else if (from <= now && now < to) wrap.scrollLeft = Math.max(0, (now - from) / 60 * PX - (MOBILE ? 60 : 200));''')

# ---------------------------------------------------------------- js: settings (phone section)
rep('''  loadTv();
  $('#settingsModal').classList.remove('hidden');
}''', '''  if (me.remote) $('#sThisDevInfo').textContent = 'Connected to Beeldbuis on the Mac as “' + me.name + '”.';
  else { loadTv(); loadPhone(); }
  $('#settingsModal').classList.remove('hidden');
}
// ---------- iPhone & iPad
let phone = {};
function qrImg(text){
  if (!window.qrcode) return '';
  const q = qrcode(0, 'M'); q.addData(text); q.make();
  return q.createImgTag(4, 0, 'QR code');
}
async function loadPhone(){
  try { phone = await api('/api/phone'); } catch (e) { phone = {}; }
  $('#sPhone').checked = phone.enabled !== false;
  $('#sPhoneBox').style.opacity = phone.enabled === false ? .4 : 1;
  const url = (phone.urls || [])[0] || '';
  $('#sPhoneCode').textContent = phone.code || '······';
  $('#sPhoneUrl').innerHTML = (phone.urls || []).map(esc).join('<br>') + ((phone.tailscale || []).length ? '<br><span class="hint">Tailscale: </span>' + phone.tailscale.map(esc).join(' ') : '');
  $('#sPhoneQr').innerHTML = url && phone.code ? qrImg(url + '/?pair=' + phone.code) : '';
  $('#sPhoneQr').style.display = url ? '' : 'none';
  const devs = phone.devices || [];
  $('#sPhoneDevs').innerHTML = devs.length ? '<div class="hint">Connected devices</div>' + devs.map(d => `<div class="d"><span>${esc(d.name)} <span class="hint">· last used ${esc(ago(d.seen))}</span></span><button class="btn small danger" data-id="${esc(d.id)}">Remove</button></div>`).join('') : '';
  $('#sPhoneDevs').querySelectorAll('button').forEach(b => b.onclick = async () => { await post('/api/phone/forget', { id: b.dataset.id }); loadPhone(); });
}
$('#sPhoneNew').onclick = async () => { await post('/api/phone/new-code'); loadPhone(); toast('New code made. Devices that are already connected stay connected.'); };
$('#sPhone').onchange = async () => { settings.phone = $('#sPhone').checked; await post('/api/settings', { phone: settings.phone }); loadPhone(); };
$('#sLeave').onclick = async () => {
  if (!confirm('Disconnect this device from Beeldbuis? You need the code from the Mac to connect again.')) return;
  await post('/api/phone/leave').catch(()=>{}); devKey = ''; try { localStorage.removeItem('bb_key'); } catch {}
  stopPlayback(); $('#settingsModal').classList.add('hidden'); showPair();
};''')
rep('''  Object.assign(settings, tvFields());
  await post('/api/settings', settings);''', '''  if (!me.remote) { Object.assign(settings, tvFields()); settings.phone = $('#sPhone').checked; }
  await post('/api/settings', settings);''')

open(P, "w", encoding="utf-8").write(src)
print("patched", P)
