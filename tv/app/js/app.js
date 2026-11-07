/* Beeldbuis for TV: screens, remote handling, playback flow. */
(function (root) {
  'use strict';
  var P = root.BBPlatform, S = root.BBStore, X = root.BBXtream, PL = root.BBPlayer, N = root.BBNav, G = root.BBGroups;
  var VERSION = '0.1.0';
  var $ = function (sel) { return document.querySelector(sel); };
  var screen = $('#screen');

  var view = { tab: null, back: null };            // current screen and how to go back from it
  var play = null;                                   // what is playing: {item, list, index, variant}
  var osdTimer = null, clockTimer = null, saveTimer = null;
  var live = { cats: [], catId: null, groups: [] };  // Live TV state
  var vod = { kind: 'movies', cats: [], catId: null, items: [], shown: 0 };

  // ---------- helpers ----------
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function hhmm(ts) { var d = new Date(ts * 1000); return pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function dur(s) { s = Math.max(0, Math.floor(s || 0)); var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60; return (h ? h + ':' + pad(m) : m) + ':' + pad(x); }
  function busy(on, text) { $('#busy').classList.toggle('hidden', !on); $('#busyText').textContent = text || 'Loading…'; }
  function toast(msg, ms) {
    var t = $('#toast'); t.innerHTML = msg; t.classList.remove('hidden');
    clearTimeout(toast._t); toast._t = setTimeout(function () { t.classList.add('hidden'); }, ms || 4000);
  }
  function img(src, cls) { return src ? '<img class="' + (cls || '') + '" src="' + esc(src) + '" onerror="this.style.visibility=\'hidden\'">' : ''; }
  function empty(title, text) {
    return '<div class="empty"><img src="img/icon-192.png" alt=""><h2>' + esc(title) + '</h2><p>' + text + '</p></div>';
  }
  // "EN - The Long Night (2024)" -> "The Long Night (2024)"
  function title(name) { return String(name || '').replace(/^\s*[A-Z0-9]{2,3}\s*[-|:]\s+/, ''); }
  function catLabel(name) { return String(name || '').replace(/^[^|]{1,7}\|\s*/, ''); }
  function showTop(on) { $('#top').classList.toggle('hidden', !on); }
  function setTab(tab) {
    view.tab = tab;
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) { b.classList.toggle('active', b.getAttribute('data-tab') === tab); });
  }
  function clock() { var d = new Date(); $('#clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes()); }

  function modal(html, onBack) {
    closeModal();
    var m = document.createElement('div');
    m.className = 'modal'; m.id = 'modal';
    m.innerHTML = '<div class="modal-box">' + html + '</div>';
    m._prevFocus = N.current();
    m._onBack = onBack;
    $('#stage').appendChild(m);
    N.first(m);
    return m;
  }
  function closeModal() {
    var m = $('#modal');
    if (!m) return false;
    var prev = m._prevFocus;
    m.parentNode.removeChild(m);
    if (prev && document.body.contains(prev)) N.focus(prev); else N.first();
    return true;
  }
  function errorMessage(e) { return esc((e && e.message) || String(e)); }

  // ---------- sign in ----------
  function showLogin(msg) {
    showTop(false); setTab(null);
    var a = S.get().account || {};
    screen.innerHTML =
      '<div class="login">' +
      '<img class="login-logo" src="img/wordmark-mascot.png" alt="Beeldbuis">' +
      '<p class="login-intro">Sign in with your Xtream Codes subscription. Your details stay on this TV.</p>' +
      '<label>Server<input id="inServer" class="f autofocus" type="text" autocomplete="off" spellcheck="false" placeholder="http://example.com:8080" value="' + esc(a.server || '') + '"></label>' +
      '<label>Username<input id="inUser" class="f" type="text" autocomplete="off" spellcheck="false" value="' + esc(a.username || '') + '"></label>' +
      '<label>Password<input id="inPass" class="f" type="password" autocomplete="off" value="' + esc(a.password || '') + '"></label>' +
      '<button id="btnLogin" class="f btn primary">Sign in</button>' +
      '<p class="login-msg" id="loginMsg">' + (msg ? esc(msg) : 'Tip: type <b>demo</b> as server to look around.') + '</p>' +
      '</div>';
    $('#btnLogin').onclick = doLogin;
    view.back = 'exit';
    N.first(screen);
  }
  function doLogin() {
    var srv = $('#inServer').value, u = $('#inUser').value, pw = $('#inPass').value;
    if (srv.trim().toLowerCase() === 'demo' && !u) { u = 'demo'; pw = 'demo'; }
    busy(true, 'Signing in…');
    X.login(srv, u, pw).then(function (r) {
      var s = S.get();
      var changed = !s.account || s.account.server !== r.account.server || s.account.username !== r.account.username;
      s.account = r.account;
      if (changed) { s.countries = []; S.cacheClear(); }
      S.save();
      busy(false);
      if (!s.countries.length) showCountries(true); else showHome();
    }).catch(function (e) { busy(false); $('#loginMsg').innerHTML = '<span class="err">' + errorMessage(e) + '</span>'; });
  }

  // ---------- choose countries (keeps lists small and relevant) ----------
  function showCountries(firstRun) {
    busy(true, 'Loading channel list…');
    X.liveCategories().then(function (cats) {
      busy(false);
      var count = {};
      cats.forEach(function (c) { var k = G.categoryCountry(c.category_name); count[k] = (count[k] || 0) + 1; });
      var keys = Object.keys(count).sort(function (a, b) { return count[b] - count[a]; });
      var chosen = S.get().countries.slice();
      showTop(!firstRun); setTab('settings');
      screen.innerHTML = '<div class="page"><h1>Which channels do you want to see?</h1>' +
        '<p class="hint">Pick the country groups from your provider. You can change this later in Settings.</p>' +
        '<div class="chips">' + keys.map(function (k) {
          return '<button class="f chip' + (chosen.indexOf(k) >= 0 ? ' on' : '') + '" data-k="' + esc(k) + '">' + esc(k === 'OTHER' ? 'Other' : k) + ' <small>' + count[k] + '</small></button>';
        }).join('') + '</div>' +
        '<button id="btnCountries" class="f btn primary">Continue</button></div>';
      Array.prototype.forEach.call(screen.querySelectorAll('.chip'), function (b) {
        b.onclick = function () {
          var k = b.getAttribute('data-k'), i = chosen.indexOf(k);
          if (i >= 0) chosen.splice(i, 1); else chosen.push(k);
          b.classList.toggle('on', i < 0);
        };
      });
      $('#btnCountries').onclick = function () {
        if (!chosen.length) { toast('Pick at least one group'); return; }
        S.get().countries = chosen; S.save(); live.catId = null; showHome();
      };
      view.back = firstRun ? 'exit' : showSettings;
      N.first(screen);
    }).catch(function (e) { busy(false); showLogin(e.message); });
  }

  // ---------- home ----------
  function tile(item, extra) {
    var cls = item.type === 'live' ? 'tile tile-live' : 'tile tile-poster';
    return '<button class="f ' + cls + '" data-key="' + esc(item.key) + '">' +
      (item.type === 'live' ? '<div class="logo">' + img(item.icon) + '</div>' : img(item.icon, 'poster')) +
      '<div class="tile-name">' + esc(item.name) + '</div>' + (extra || '') + '</button>';
  }
  function row(title, items, extraFn) {
    if (!items.length) return '';
    return '<section class="row"><h2>' + esc(title) + '</h2><div class="row-items">' +
      items.map(function (it) { return tile(it, extraFn ? extraFn(it) : ''); }).join('') + '</div></section>';
  }
  function showHome() {
    showTop(true); setTab('home');
    var s = S.get();
    var cont = Object.keys(s.positions).map(function (k) { return s.positions[k]; })
      .sort(function (a, b) { return b.at - a.at; }).slice(0, 15).map(function (p) { return p.item; });
    var favLive = s.favorites.filter(function (f) { return f.type === 'live'; });
    var favVod = s.favorites.filter(function (f) { return f.type !== 'live'; });
    var recent = s.recent.slice(0, 15);
    var all = {};
    cont.concat(favLive, favVod, recent).forEach(function (it) { all[it.key] = it; });
    var html = row('Continue watching', cont, function (it) {
      var p = s.positions[it.key]; return p && p.d ? '<div class="bar"><div style="width:' + Math.round(100 * p.t / p.d) + '%"></div></div>' : '';
    }) + row('Favourite channels', favLive) + row('Recently watched', recent) + row('My movies & series', favVod);
    screen.innerHTML = '<div class="home">' + (html || empty('Nothing here yet', 'Find a channel, movie or series and press the <b class="k-yellow">yellow</b> button to add it to your favourites.')) + '</div>';
    Array.prototype.forEach.call(screen.querySelectorAll('.tile'), function (b) {
      b.onclick = function () { openItem(all[b.getAttribute('data-key')]); };
    });
    view.back = confirmExit;
    N.first(html ? screen : $('#tabs'));
  }
  function openItem(it) {
    if (!it) return;
    if (it.type === 'live') playLive(it.group || it, [it.group || it], 0);
    else if (it.type === 'movie') showMovie(it);
    else if (it.type === 'episode') playEpisode(it, it.seriesItem, null);
    else if (it.type === 'series') showSeries(it);
  }
  function confirmExit() {
    modal('<h2>Close Beeldbuis?</h2><div class="btns"><button class="f btn primary autofocus" id="mYes">Close</button><button class="f btn" id="mNo">Stay</button></div>');
    $('#mYes').onclick = function () { P.exit(); };
    $('#mNo').onclick = closeModal;
  }

  // ---------- live TV ----------
  function liveItem(g) { return { key: 'live:' + g.key, type: 'live', name: g.name, icon: g.icon, group: g }; }
  function showLive() {
    showTop(true); setTab('live');
    busy(true);
    X.liveCategories().then(function (cats) {
      busy(false);
      var countries = S.get().countries;
      live.cats = cats.filter(function (c) { return countries.indexOf(G.categoryCountry(c.category_name)) >= 0; });
      screen.innerHTML = '<div class="split"><div class="side" id="liveCats">' +
        '<button class="f cat" data-id="fav">★ Favourites</button>' +
        live.cats.map(function (c) { return '<button class="f cat" data-id="' + esc(c.category_id) + '">' + esc(c.category_name) + '</button>'; }).join('') +
        '</div><div class="main" id="liveList"></div></div>';
      Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) {
        b.onclick = function () { loadLiveCat(b.getAttribute('data-id'), true); };
      });
      var start = live.catId || (S.get().favorites.some(function (f) { return f.type === 'live'; }) ? 'fav' : (live.cats[0] && String(live.cats[0].category_id)));
      view.back = showHome;
      if (start) loadLiveCat(String(start), false); else $('#liveList').innerHTML = empty('No channels', 'Choose your channel groups in Settings.');
      var btn = screen.querySelector('.cat[data-id="' + start + '"]');
      N.focus(btn || screen.querySelector('.cat'));
    }).catch(function (e) { busy(false); toast(errorMessage(e)); });
  }
  function loadLiveCat(id, focusList) {
    live.catId = id;
    Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) { b.classList.toggle('active', b.getAttribute('data-id') === id); });
    var list = $('#liveList');
    if (id === 'fav') {
      live.groups = S.get().favorites.filter(function (f) { return f.type === 'live'; }).map(function (f) { return f.group; });
      renderLive(focusList);
      return;
    }
    list.innerHTML = '<div class="loading">Loading…</div>';
    X.liveStreams(id).then(function (streams) {
      if (live.catId !== id) return;
      live.groups = G.buildGroups(live.cats, streams);
      renderLive(focusList);
    }).catch(function (e) { list.innerHTML = empty('Could not load channels', errorMessage(e)); });
  }
  function renderLive(focusList) {
    var list = $('#liveList');
    if (!live.groups.length) {
      list.innerHTML = empty(live.catId === 'fav' ? 'No favourite channels yet' : 'No channels here', live.catId === 'fav' ? 'Press the <b class="k-yellow">yellow</b> button on a channel to add it.' : '');
      return;
    }
    list.innerHTML = live.groups.map(function (g, i) {
      var fav = S.isFav('live:' + g.key);
      return '<button class="f chan" data-i="' + i + '" data-left="#liveCats .cat.active"><div class="logo">' + img(g.icon) + '</div>' +
        '<div class="chan-name">' + esc(g.name) + '</div><div class="chan-meta" id="now' + i + '"></div>' +
        (g.archive ? '<span class="badge">Catch-up</span>' : '') +
        (g.best ? '<span class="badge q">' + esc(g.best === '8K' ? '4K' : g.best) + '</span>' : '') +
        (fav ? '<span class="star">★</span>' : '') + '</button>';
    }).join('');
    Array.prototype.forEach.call(list.querySelectorAll('.chan'), function (b) {
      var i = +b.getAttribute('data-i');
      b.onclick = function () { playLive(live.groups[i], live.groups, i); };
      b.onfocusin2 = function () { fillNow(i); };
      b._fav = function () { var on = S.toggleFav(liveItem(live.groups[i])); toast(on ? 'Added to favourites' : 'Removed from favourites'); var st = b.querySelector('.star'); if (on && !st) b.insertAdjacentHTML('beforeend', '<span class="star">★</span>'); if (!on && st) st.parentNode.removeChild(st); };
    });
    if (focusList) N.focus(list.querySelector('.chan'));
  }
  function fillNow(i) {
    var g = live.groups[i], el = $('#now' + i);
    if (!g || !el || el._done) return;
    el._done = true;
    X.shortEpg(g.variants[0].id).then(function (ps) {
      var now = Date.now() / 1000, p = ps.filter(function (x) { return x.end > now; })[0];
      if (p) el.textContent = hhmm(p.start) + ' ' + p.title;
    });
  }

  // ---------- movies & series ----------
  function showVod(kind) {
    vod.kind = kind;
    showTop(true); setTab(kind);
    busy(true);
    (kind === 'movies' ? X.vodCategories() : X.seriesCategories()).then(function (cats) {
      busy(false);
      vod.cats = cats;
      screen.innerHTML = '<div class="split"><div class="side" id="vodCats">' +
        '<button class="f cat" data-id="fav">★ My ' + (kind === 'movies' ? 'movies' : 'series') + '</button>' +
        cats.map(function (c) { return '<button class="f cat" data-id="' + esc(c.category_id) + '">' + esc(c.category_name) + '</button>'; }).join('') +
        '</div><div class="main grid" id="vodGrid"></div></div>';
      Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) { b.onclick = function () { loadVodCat(b.getAttribute('data-id'), true); }; });
      var start = vod['cat_' + kind] || (cats[0] ? String(cats[0].category_id) : 'fav');
      view.back = showHome;
      loadVodCat(start, false);
      N.focus(screen.querySelector('.cat[data-id="' + start + '"]') || screen.querySelector('.cat'));
    }).catch(function (e) { busy(false); toast(errorMessage(e)); });
  }
  function loadVodCat(id, focusGrid) {
    var kind = vod.kind;
    vod['cat_' + kind] = id; vod.catId = id;
    Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) { b.classList.toggle('active', b.getAttribute('data-id') === id); });
    var grid = $('#vodGrid');
    var type = kind === 'movies' ? 'movie' : 'series';
    if (id === 'fav') {
      vod.items = S.get().favorites.filter(function (f) { return f.type === type; });
      return renderVod(focusGrid);
    }
    grid.innerHTML = '<div class="loading">Loading…</div>';
    (kind === 'movies' ? X.vodStreams(id) : X.series(id)).then(function (items) {
      if (vod.catId !== id || vod.kind !== kind) return;
      vod.items = items.map(function (x) { var o = { key: type + ':' + x.id, type: type }; Object.keys(x).forEach(function (k) { o[k] = x[k]; }); o.name = title(o.name); return o; });
      if (kind === 'movies') vod.items.sort(function (a, b) { return b.added - a.added; });   // newest first
      renderVod(focusGrid);
    }).catch(function (e) { grid.innerHTML = empty('Could not load this list', errorMessage(e)); });
  }
  function renderVod(focusGrid) {
    var grid = $('#vodGrid');
    vod.shown = 0;
    grid.innerHTML = vod.items.length ? '' : empty('Nothing here yet', vod.catId === 'fav' ? 'Press the <b class="k-yellow">yellow</b> button on a title to keep it here.' : '');
    moreVod();
    if (focusGrid) N.focus(grid.querySelector('.tile'));
  }
  function moreVod() {
    var grid = $('#vodGrid'), from = vod.shown, to = Math.min(vod.items.length, from + 60);
    if (from >= to) return;
    var html = '';
    for (var i = from; i < to; i++) {
      var it = vod.items[i];
      html += '<button class="f tile tile-poster" data-i="' + i + '" data-left="#vodCats .cat.active">' + img(it.icon, 'poster') +
        '<div class="tile-name">' + esc(it.name) + '</div>' + (it.rating ? '<div class="rating">★ ' + esc(it.rating) + '</div>' : '') + '</button>';
    }
    grid.insertAdjacentHTML('beforeend', html);
    vod.shown = to;
    Array.prototype.forEach.call(grid.querySelectorAll('.tile'), function (b) {
      if (b._wired) return; b._wired = true;
      var i = +b.getAttribute('data-i');
      b.onclick = function () { var it = vod.items[i]; if (it.type === 'movie') showMovie(it); else showSeries(it); };
      b.onfocusin2 = function () { if (i >= vod.shown - 12) moreVod(); };
      b._fav = function () { var it = vod.items[i]; toast(S.toggleFav(stripItem(it)) ? 'Added to My ' + (it.type === 'movie' ? 'movies' : 'series') : 'Removed'); };
    });
  }
  function stripItem(it) { return { key: it.key, type: it.type, id: it.id, name: it.name, icon: it.icon, ext: it.ext, rating: it.rating }; }

  function showMovie(it) {
    var pos = S.get().positions[it.key];
    var m = modal('<div class="detail">' + img(it.icon, 'poster') + '<div><h2>' + esc(it.name) + '</h2><p class="meta" id="mMeta">' + (it.rating ? '★ ' + esc(it.rating) : '') + '</p>' +
      '<p class="plot" id="mPlot"></p><div class="btns">' +
      (pos ? '<button class="f btn primary autofocus" id="mResume">Resume at ' + dur(pos.t) + '</button>' : '') +
      '<button class="f btn' + (pos ? '' : ' primary autofocus') + '" id="mPlay">' + (pos ? 'From the start' : 'Play') + '</button>' +
      '<button class="f btn" id="mFav">' + (S.isFav(it.key) ? '★ In My movies' : '☆ Add to My movies') + '</button></div></div></div>');
    X.vodInfo(it.id).then(function (r) {
      var i = (r && r.info) || {};
      if (!$('#mPlot')) return;
      $('#mPlot').textContent = i.plot || i.description || '';
      $('#mMeta').textContent = [i.releasedate ? String(i.releasedate).slice(0, 4) : '', i.genre, i.duration, it.rating ? '★ ' + it.rating : ''].filter(Boolean).join(' · ');
      if (r && r.movie_data && r.movie_data.container_extension) it.ext = r.movie_data.container_extension;
    }).catch(function () {});
    if (pos) $('#mResume').onclick = function () { closeModal(); playVod(it, pos.t); };
    $('#mPlay').onclick = function () { closeModal(); playVod(it, 0); };
    $('#mFav').onclick = function () { var on = S.toggleFav(stripItem(it)); $('#mFav').textContent = on ? '★ In My movies' : '☆ Add to My movies'; };
    return m;
  }

  function showSeries(it) {
    busy(true);
    X.seriesInfo(it.id).then(function (r) {
      busy(false);
      var eps = (r && r.episodes) || {}, seasons = Object.keys(eps).sort(function (a, b) { return a - b; });
      var info = (r && r.info) || {};
      if (!seasons.length) { toast('No episodes available'); return; }
      var prevBack = view.back, prevHtml = screen.innerHTML, prevFocusKey = N.current() && N.current().getAttribute('data-i');
      showTop(true);
      screen.innerHTML = '<div class="series"><div class="series-head">' + img(it.icon || info.cover, 'poster') + '<div><h1>' + esc(it.name) + '</h1>' +
        '<p class="meta">' + esc([info.releaseDate ? String(info.releaseDate).slice(0, 4) : '', info.genre, it.rating ? '★ ' + it.rating : ''].filter(Boolean).join(' · ')) + '</p>' +
        '<p class="plot">' + esc(info.plot || it.plot || '') + '</p>' +
        '<button class="f btn" id="sFav">' + (S.isFav(it.key) ? '★ In My series' : '☆ Add to My series') + '</button></div></div>' +
        '<div class="seasons">' + seasons.map(function (s) { return '<button class="f chip season" data-s="' + esc(s) + '">Season ' + esc(s) + '</button>'; }).join('') + '</div>' +
        '<div class="episodes" id="eps"></div></div>';
      $('#sFav').onclick = function () { var on = S.toggleFav(stripItem(it)); $('#sFav').textContent = on ? '★ In My series' : '☆ Add to My series'; };
      function renderSeason(s, focus) {
        Array.prototype.forEach.call(screen.querySelectorAll('.season'), function (b) { b.classList.toggle('on', b.getAttribute('data-s') === s); });
        var list = eps[s] || [];
        $('#eps').innerHTML = list.map(function (e, i) {
          var key = 'episode:' + e.id, p = S.get().positions[key];
          return '<button class="f ep" data-i="' + i + '"><b>' + esc(e.episode_num) + '</b> ' + esc(e.title || ('Episode ' + e.episode_num)) +
            (p && p.d ? '<div class="bar"><div style="width:' + Math.round(100 * p.t / p.d) + '%"></div></div>' : '') + '</button>';
        }).join('');
        Array.prototype.forEach.call($('#eps').querySelectorAll('.ep'), function (b) {
          b.onclick = function () {
            var e = list[+b.getAttribute('data-i')];
            var epItem = { key: 'episode:' + e.id, type: 'episode', id: e.id, ext: e.container_extension || 'mp4', icon: it.icon,
              name: it.name + ' · S' + s + 'E' + e.episode_num, seriesItem: stripItem(it), season: s, episodes: list.map(function (x) { return { id: x.id, num: x.episode_num, title: x.title, ext: x.container_extension }; }) };
            playEpisode(epItem, epItem.seriesItem, S.get().positions[epItem.key]);
          };
        });
        if (focus) N.focus($('#eps').querySelector('.ep'));
      }
      Array.prototype.forEach.call(screen.querySelectorAll('.season'), function (b) { b.onclick = function () { renderSeason(b.getAttribute('data-s'), true); }; });
      // start at the season with the latest watched episode, else the first
      var startS = seasons[0];
      seasons.forEach(function (s) { (eps[s] || []).forEach(function (e) { if (S.get().positions['episode:' + e.id]) startS = s; }); });
      renderSeason(startS, false);
      view.back = function () {
        if (prevHtml && (vod.kind === 'movies' || vod.kind === 'series') && view.tab !== 'home') { showVod(vod.kind); }
        else if (typeof prevBack === 'function') showHome(); else showHome();
      };
      N.focus(screen.querySelector('.season.on'));
    }).catch(function (e) { busy(false); toast(errorMessage(e)); });
  }

  // ---------- settings ----------
  function showSettings() {
    showTop(true); setTab('settings');
    var s = S.get();
    screen.innerHTML = '<div class="page settings">' +
      '<button class="f setting" id="stCountries"><b>Channel groups</b><span>' + esc(s.countries.join(', ') || 'none') + '</span></button>' +
      '<button class="f setting" id="stSubs"><b>English subtitles</b><span>' + (s.englishSubs ? 'On' : 'Off') + '</span></button>' +
      '<button class="f setting" id="stRefresh"><b>Refresh lists</b><span>Fetch channels, movies and series again</span></button>' +
      '<button class="f setting" id="stAccount"><b>Account</b><span>' + esc(s.account ? s.account.username + ' @ ' + s.account.server.replace(/^https?:\/\//, '') : '') + '</span></button>' +
      '<button class="f setting" id="stLogout"><b>Sign out</b><span>Removes your login from this TV</span></button>' +
      '<p class="about">Beeldbuis ' + VERSION + ' · ' + esc(P.name) + '</p></div>';
    $('#stCountries').onclick = function () { showCountries(false); };
    $('#stSubs').onclick = function () { s.englishSubs = !s.englishSubs; S.save(); $('#stSubs span').textContent = s.englishSubs ? 'On' : 'Off'; };
    $('#stRefresh').onclick = function () { S.cacheClear(); toast('Lists will be fetched again'); };
    $('#stAccount').onclick = function () {
      busy(true);
      X.connections().then(function (c) {
        busy(false);
        toast('Connections in use: ' + c.active + ' of ' + c.max + (c.exp ? ' · valid until ' + new Date(c.exp * 1000).toLocaleDateString('en-GB') : ''), 6000);
      }).catch(function (e) { busy(false); toast(errorMessage(e)); });
    };
    $('#stLogout').onclick = function () {
      modal('<h2>Sign out?</h2><p>Your favourites stay on this TV.</p><div class="btns"><button class="f btn primary autofocus" id="mYes">Sign out</button><button class="f btn" id="mNo">Cancel</button></div>');
      $('#mYes').onclick = function () { closeModal(); s.account = null; S.save(); S.cacheClear(); showLogin(); };
      $('#mNo').onclick = closeModal;
    };
    view.back = showHome;
    N.first(screen);
  }

  // ---------- playback ----------
  function startPlayback(url, item, opts) {
    var returnTo = { back: view.back, html: null, focus: N.current() };
    play = { item: item, url: url, opts: opts, returnTo: returnTo, live: !!opts.live, started: Date.now() };
    document.body.classList.add('playing');
    showOsd(true);
    PL.open(url, { live: opts.live, start: opts.start || 0, englishSubs: S.get().englishSubs && !opts.live });
    clearInterval(saveTimer);
    if (!opts.live) saveTimer = setInterval(savePos, 15000);
  }
  function savePos() {
    if (!play || play.live) return;
    var t = PL.position(), d = PL.duration();
    if (t > 0) S.setPosition(play.item, t, d);
  }
  function stopPlayback() {
    if (!play) return;
    savePos();
    clearInterval(saveTimer);
    PL.stop();
    document.body.classList.remove('playing');
    $('#osd').classList.add('hidden');
    $('#subs') && ($('#subs').textContent = '');
    play = null;
  }
  function leavePlayer() {
    var p = play;
    stopPlayback();
    if (p && p.live) { /* back to the channel list we came from */ }
    if (view.tab === 'home') showHome();
    else if (p && p.returnTo.focus && document.body.contains(p.returnTo.focus)) N.focus(p.returnTo.focus);
    else N.first(screen);
  }

  function playLive(g, list, index, variantIdx) {
    if (!g || !g.variants || !g.variants.length) return;
    variantIdx = variantIdx || 0;
    var v = g.variants[variantIdx];
    if (play) { PL.stop(); }
    var item = liveItem(g);
    S.addRecent(item);
    S.get().lastChannel = item; S.save();
    startPlayback(X.liveUrl(v.id), item, { live: true });
    play.list = list; play.index = index; play.variant = variantIdx; play.group = g;
    osdLive();
  }
  function zap(step) {
    if (!play || !play.live || !play.list || play.list.length < 2) return;
    var i = (play.index + step + play.list.length) % play.list.length;
    playLive(play.list[i], play.list, i);
  }
  function osdLive() {
    var g = play.group, v = g.variants[play.variant];
    $('#osdTitle').textContent = g.name;
    $('#osdSub').textContent = (v.q ? (v.q === '8K' ? '4K' : v.q) + ' · ' : '') + (play.list.length > 1 ? 'Channel ' + (play.index + 1) + ' of ' + play.list.length + ' · CH▲▼ to switch' : '');
    $('#osdProgress').classList.add('hidden');
    $('#osdTime').textContent = '';
    X.shortEpg(v.id).then(function (ps) {
      if (!play || play.group !== g) return;
      var now = Date.now() / 1000, cur = ps.filter(function (p) { return p.end > now; });
      if (cur[0]) $('#osdSub').textContent = hhmm(cur[0].start) + '–' + hhmm(cur[0].end) + '  ' + cur[0].title + (cur[1] ? '   ·   Next ' + hhmm(cur[1].start) + ' ' + cur[1].title : '');
    });
  }
  function playVod(it, start) {
    S.addRecent(stripItem(it));
    startPlayback(X.vodUrl(it.id, it.ext), stripItem(it), { live: false, start: start });
    $('#osdTitle').textContent = it.name; $('#osdSub').textContent = '';
  }
  function playEpisode(ep, seriesItem, pos) {
    var start = pos ? pos.t : 0;
    S.addRecent(seriesItem || ep);
    startPlayback(X.episodeUrl(ep.id, ep.ext), ep, { live: false, start: start });
    $('#osdTitle').textContent = ep.name; $('#osdSub').textContent = '';
  }
  function nextEpisode() {
    var ep = play && play.item;
    if (!ep || ep.type !== 'episode' || !ep.episodes) return false;
    var idx = -1;
    ep.episodes.forEach(function (e, i) { if (String(e.id) === String(ep.id)) idx = i; });
    var n = ep.episodes[idx + 1];
    if (!n) return false;
    var next = { key: 'episode:' + n.id, type: 'episode', id: n.id, ext: n.ext || 'mp4', icon: ep.icon, name: ep.seriesItem.name + ' · S' + ep.season + 'E' + n.num,
      seriesItem: ep.seriesItem, season: ep.season, episodes: ep.episodes };
    stopPlayback();
    playEpisode(next, next.seriesItem, null);
    return true;
  }

  function showOsd(force) {
    if (!play) return;
    $('#osd').classList.remove('hidden');
    clearTimeout(osdTimer);
    osdTimer = setTimeout(function () { $('#osd').classList.add('hidden'); }, force ? 6000 : 4000);
  }

  PL.on({
    state: function (st) { if (play) { play.state = st; $('#osdTime').textContent = st === 'buffering' ? 'Loading…' : st === 'paused' ? 'Paused' : (play.live ? 'Live' : $('#osdTime').textContent); } },
    time: function (t, d) {
      if (!play || play.live) return;
      $('#osdProgress').classList.toggle('hidden', !d);
      if (d) $('#osdFill').style.width = (100 * t / d) + '%';
      $('#osdTime').textContent = dur(t) + (d ? ' / ' + dur(d) : '');
    },
    ended: function () { if (play && !play.live) { S.setPosition(play.item, PL.duration(), PL.duration()); if (!nextEpisode()) leavePlayer(); } },
    subtitle: function (text) { var el = $('#subs'); if (!el) { $('#stage').insertAdjacentHTML('beforeend', '<div id="subs"></div>'); el = $('#subs'); } el.textContent = text; },
    error: function (code) {
      if (!play) return;
      // live: try the next quality variant of the same channel first
      if (play.live && play.group && play.variant + 1 < play.group.variants.length && Date.now() - play.started < 20000) {
        var g = play.group, l = play.list, i = play.index, nv = play.variant + 1;
        toast('Trying ' + (g.variants[nv].q || 'another') + ' version…', 2500);
        playLive(g, l, i, nv);
        return;
      }
      var p = play;
      X.connections().then(function (c) {
        if (play !== p) return;
        if (c.active >= c.max) failPlayback('Your subscription is in use on another device (' + c.active + ' of ' + c.max + ' connection' + (c.max > 1 ? 's' : '') + '). Stop watching there, or wait until a recording on the Mac has finished, then try again.');
        else failPlayback('This ' + (p.live ? 'channel' : 'title') + ' does not play right now (' + esc(code) + ').');
      }).catch(function () { failPlayback('This stream does not play right now (' + esc(code) + ').'); });
    }
  });
  function failPlayback(msg) {
    var p = play;
    stopPlayback();
    modal('<img class="modal-mascot" src="img/icon-192.png" alt=""><h2>Can\'t play this</h2><p>' + msg + '</p><div class="btns"><button class="f btn primary autofocus" id="mOk">OK</button></div>',
      null);
    $('#mOk').onclick = function () { closeModal(); if (p && p.returnTo.focus && document.body.contains(p.returnTo.focus)) N.focus(p.returnTo.focus); };
  }

  // ---------- keys ----------
  function onKey(e) {
    var a = P.KEYS[e.keyCode];
    if (a === undefined) return;
    var t = e.target;
    // let the on-screen keyboard / text fields keep their own keys
    if (t && t.tagName === 'INPUT') {
      if (a === 'back' && e.keyCode !== 10009 && e.keyCode !== 27) return;
      if (a === 'left' || a === 'right' || (typeof a === 'number')) return;
      if (a === 'ok') { e.preventDefault(); var inputs = Array.prototype.slice.call(screen.querySelectorAll('input')), k = inputs.indexOf(t); if (k >= 0 && k < inputs.length - 1) N.focus(inputs[k + 1]); else { t.blur(); N.focus($('#btnLogin')); } return; }
      if (a === 'back') { e.preventDefault(); t.blur(); return; }
    }
    if (typeof a === 'number' && t && t.tagName === 'INPUT') return;
    e.preventDefault();

    if (play && !$('#modal')) return playerKey(a);
    if (a === 'back') {
      if (closeModal()) return;
      if ($('#modal') && $('#modal')._onBack) return $('#modal')._onBack();
      if (view.back === 'exit') return P.exit();
      if (typeof view.back === 'function') return view.back();
      return;
    }
    if (a === 'up' || a === 'down' || a === 'left' || a === 'right') {
      if (!N.move(a) && a === 'up' && !$('#modal') && !$('#top').classList.contains('hidden')) N.focus($('.tab.active') || $('.tab'));
      return;
    }
    if (a === 'ok') { var c = N.current(); if (c) { if (c.tagName === 'INPUT') { c.focus(); } else c.click(); } return; }
    if (a === 'yellow') { var f = N.current(); if (f && f._fav) f._fav(); return; }
    if (a === 'red') { showLive(); return; }
    if (a === 'chup' || a === 'chdown') {
      var last = S.get().lastChannel;
      if (last && last.group) playLive(last.group, [last.group], 0);
    }
  }
  function playerKey(a) {
    if (a === 'back' || a === 'stop') { leavePlayer(); return; }
    if (play.live) {
      if (a === 'chup' || a === 'up') zap(-1);
      else if (a === 'chdown' || a === 'down') zap(1);
      else if (a === 'yellow') { var on = S.toggleFav(liveItem(play.group)); toast(on ? 'Added to favourites' : 'Removed from favourites'); }
      showOsd();
      return;
    }
    if (a === 'ok' || a === 'play' || a === 'pause' || a === 'playpause') PL.toggle();
    else if (a === 'right' || a === 'ff') PL.seekBy(a === 'ff' ? 60 : 10);
    else if (a === 'left' || a === 'rw') PL.seekBy(a === 'rw' ? -60 : -10);
    else if (a === 'chup' || a === 'chdown') { if (a === 'chdown') nextEpisode(); }
    showOsd();
  }

  // ---------- start ----------
  function wireTabs() {
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
      b.onclick = function () {
        var t = b.getAttribute('data-tab');
        if (t === 'home') showHome(); else if (t === 'live') showLive(); else if (t === 'movies') showVod('movies');
        else if (t === 'series') showVod('series'); else showSettings();
      };
    });
  }
  function fit() {
    // the stage is designed at 1920x1080; scale it to the window (TVs are 1920x1080 already)
    var s = Math.min(root.innerWidth / 1920, root.innerHeight / 1080);
    var st = $('#stage');
    st.style.transform = s === 1 ? '' : 'scale(' + s + ')';
    st.style.left = Math.max(0, (root.innerWidth - 1920 * s) / 2) + 'px';
  }
  function start() {
    P.registerKeys();
    wireTabs();
    fit(); root.addEventListener('resize', fit);
    document.addEventListener('keydown', onKey);
    document.addEventListener('click', function (e) {          // mouse/pointer (Android phones, browser preview)
      var el = e.target.closest ? e.target.closest('.f') : null;
      if (el) N.focus(el, { noScroll: true });
    }, true);
    clock(); clockTimer = setInterval(clock, 10000);
    document.addEventListener('visibilitychange', function () { if (document.hidden && play) stopPlayback(); });
    S.load();
    if (!S.get().account) return showLogin();
    if (!S.get().countries.length) return showCountries(true);
    showHome();
  }

  // native wrappers (Android) deliver keys the WebView does not pass on, e.g. Back and CH▲▼
  root.BBKey = function (action) {
    var code = 0;
    Object.keys(P.KEYS).forEach(function (k) { if (!code && P.KEYS[k] === action) code = +k; });
    if (action === 'back') code = 27;
    onKey({ keyCode: code, target: document.activeElement, preventDefault: function () {} });
  };
  root.BBApp = { start: start, version: VERSION, debug: function () { return { playing: !!play, tab: view.tab, modal: !!$('#modal') }; } };
  start();
})(this);
