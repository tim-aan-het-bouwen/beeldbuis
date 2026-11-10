/* Beeldbuis for TV: screens, remote handling, playback flow. */
(function (root) {
  'use strict';
  var P = root.BBPlatform, S = root.BBStore, X = root.BBXtream, PL = root.BBPlayer, N = root.BBNav, G = root.BBGroups, SR = root.BBSearch, OM = root.BBOmdb, MAC = root.BBMac;
  var VERSION = '0.9.5';
  var $ = function (sel) { return document.querySelector(sel); };
  var screen = $('#screen');

  var view = { tab: null, back: null };            // current screen and how to go back from it
  var play = null;                                   // what is playing: {item, list, index, variant}
  var osdTimer = null, clockTimer = null, saveTimer = null;
  var live = { cats: [], catId: null, groups: [] };  // Live TV state
  var vod = { kind: 'movies', cats: [], catId: null, items: [], shown: 0, facets: {} };

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
  function clock() {
    var d = new Date(), t = pad(d.getHours()) + ':' + pad(d.getMinutes());
    $('#clock').textContent = t;
    if ($('#calmClock')) { $('#calmClock').textContent = t; $('#calmDate').textContent = longDate(d); }
  }

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
  function tile(item, extra, sub) {
    var cls = (item.type === 'live' ? 'tile tile-live' : 'tile tile-poster') + (sub !== undefined ? ' has-sub' : '');
    return '<button class="f ' + cls + '" data-key="' + esc(item.key) + '">' +
      (item.type === 'live' ? '<div class="logo">' + img(item.icon) + '</div>' : img(item.icon, 'poster')) +
      '<div class="tile-name">' + esc(item.name) + '</div>' + (sub !== undefined ? '<div class="tile-sub">' + esc(sub) + '</div>' : '') + (extra || '') + '</button>';
  }
  function row(title, items, extraFn, opts) {
    opts = opts || {};
    if (!items.length) return '';
    return '<section class="row"' + (opts.id ? ' id="' + opts.id + '"' : '') + '><h2>' + esc(title) + (opts.hint ? ' <span class="hint">' + esc(opts.hint) + '</span>' : '') + '</h2><div class="row-items">' +
      items.map(function (it) { return tile(it, extraFn ? extraFn(it) : '', opts.sub ? opts.sub(it) : undefined); }).join('') + '</div></section>';
  }

  // ---------- home ("Tonight", same rows as the Mac app where the TV can do them on its own) ----------
  var DAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
  var MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
  var NEWS_CAT = /(^|[^a-z])(news|nieuws|info|journaal|nachrichten|actualit)/i;
  var home = { items: {}, gen: 0 };
  function showHome() {
    showTop(true); setTab('home');
    var s = S.get(), gen = ++home.gen;
    var cont = Object.keys(s.positions).map(function (k) { return s.positions[k]; })
      .sort(function (a, b) { return b.at - a.at; }).slice(0, 15).map(function (p) { return p.item; });
    var favLive = liveFavs().map(liveItem);
    var watchlist = s.favorites.filter(function (f) { return f.type !== 'live'; });
    var recent = s.recent.slice(0, 15);
    home.items = {};
    cont.concat(favLive, watchlist, recent).forEach(function (it) { home.items[it.key] = it; });
    var d = new Date();
    var html = '<div class="home-head"><h1>Tonight</h1><span>' + DAYS[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()] + '</span><div class="profiles">' +
        S.PROFILE_IDS.map(function (id) { return '<button class="f chip prof' + (id === s.profile ? ' on' : '') + '" data-p="' + id + '">' + esc(S.profileName(id)) + '</button>'; }).join('') + '</div></div>' +
      '<div id="hero"></div><div id="rowLive"></div>' +
      row('On now', favLive, null, { id: 'rowOnNow', hint: 'Your channels', sub: function () { return ' '; } }) +
      '<div id="rowNews"></div>' +
      row('Continue watching', cont, function (it) {
        var p = s.positions[it.key]; return p && p.d ? '<div class="bar"><div style="width:' + Math.round(100 * p.t / p.d) + '%"></div></div>' : '';
      }) +
      '<div id="rowRec"></div>' +
      '<div id="rowWl">' + row('Watchlist', watchlist) + '</div>' +
      '<div id="rowForYou"></div><div id="rowMakers"></div><div id="rowCircle"></div><div id="rowHot"></div><div id="rowWild"></div><div id="rowNew"></div>' +
      row('Recently watched', recent);
    screen.innerHTML = '<div class="home">' + html + '<div id="homeEmpty"></div></div>';
    if (!cont.length && !favLive.length && !watchlist.length && !recent.length)
      $('#homeEmpty').innerHTML = empty('Your Tonight screen fills up as you watch', 'Press the <b class="k-yellow">yellow</b> button on a channel, movie or series to add it here. On a movie or series that puts it on your watchlist.');
    wireHome(screen); rateTiles(screen);
    homeRecordings(gen);
    Array.prototype.forEach.call(screen.querySelectorAll('.prof'), function (b) {
      b.onclick = function () { S.switchProfile(b.getAttribute('data-p')); showHome(); N.focus(screen.querySelector('.prof.on')); toast('Watching as ' + S.profileName()); };
    });
    view.back = S.get().startScreen === 'calm' ? showCalm : confirmExit;
    N.first(screen.querySelector('.home .tile') ? screen.querySelector('.home .row') : $('#tabs'));
    // the rows below need the provider; they appear when ready
    homeNow('#rowOnNow', favLive.map(function (f) { return f.group; }), gen);
    syncChannels(false);
    homeNews(gen);
    if (!home.pulled) pullPositions(gen);
    home.pulled = false;
    homeMac(gen, function () {          // the Mac knows your taste; without it the TV makes its own rows
      homeForYou(gen, recent.concat(watchlist, cont));
      homeNew(gen);
    });
  }
  // ---------- live sport from the Mac: "Live now" on Tonight and the Sport tab (only what is being played right now) ----------
  var SPORT_COLOR = { soccer: '#1d9e99', nfl: '#e07a2f', f1: '#d4572a', fight: '#a8432c', darts: '#178a85', tennis: '#b8962a', cycling: '#2f9e6a', basketball: '#d9772f', hockey: '#3c7fa8', golf: '#4f8f45', rugby: '#7a5c2e', cricket: '#5f8a3a', snooker: '#2c7a4f', skating: '#5a8fb8', other: '#6b6f7a' };
  var SPORT_EMOJI = { soccer: '⚽', nfl: '🏈', f1: '🏎️', fight: '🥊', darts: '🎯', tennis: '🎾', cycling: '🚴', basketball: '🏀', hockey: '🏒', golf: '⛳', rugby: '🏉', cricket: '🏏', snooker: '🎱', skating: '⛸️', other: '🏟️' };
  var sportView = { scores: false, filter: '', items: {} };
  function liveGroupFromMac(t, feed) {
    var f = feed || { gkey: t.gkey, name: t.channel, icon: t.icon, sid: t.sid };
    return { key: 'mac:' + f.gkey, name: f.name, icon: f.icon || '', variants: [{ id: f.sid, q: 'FHD', ext: 'ts', _i: 0, epg: '' }] };
  }
  function tagChip(tag) {
    if (!tag) return '';
    var cls = tag === 'Dutch interest' ? ' orange' : tag === 'Your team' ? ' teal' : tag === 'Final' || tag === 'Knock-out' ? '' : ' creme';
    return '<span class="lchip' + cls + '">' + (tag === 'Final' ? '🔥 ' : '') + esc(tag) + '</span>';
  }
  function liveCardHtml(t, big) {
    var m = t.match || {}, teams = m.teams || [], now = Date.now() / 1000;
    var pct = t.stop > t.start ? Math.max(2, Math.min(100, (now - t.start) / (t.stop - t.start) * 100)) : 0;
    var mid;
    if (teams.length === 2 && teams[0].name && teams[1].name) {
      var o = /^(nfl|basketball|hockey)$/.test(t.sport) ? [teams[1], teams[0]] : teams;
      var sc = sportView.scores ? '<b>' + esc(o[0].score == null ? '' : o[0].score) + '–' + esc(o[1].score == null ? '' : o[1].score) + '</b>' : '';
      mid = '<div class="lteams">' + [0, 1].map(function (i) {
        return (i ? '<div class="lvs">' + sc + esc(m.state === 'pre' ? 'Starting' : (m.detail || 'vs')) + '</div>' : '') + '<div class="ltm">' + (o[i].logo ? img(o[i].logo) : '') + '<span>' + esc(o[i].short || o[i].name) + '</span></div>';
      }).join('') + '</div>';
    } else {
      mid = '<span class="lem">' + (SPORT_EMOJI[t.sport] || '🏟️') + '</span><div class="lt">' + esc(t.title) + '</div>';
    }
    var info = [m.league_name, !teams.length && m.detail ? m.detail : ''].filter(Boolean).join(' · ') || ('until ' + hhmm(t.stop));
    return '<button class="f lcard' + (big ? ' big' : '') + '" data-lk="' + esc(t.gkey) + '" style="background-color:' + (SPORT_COLOR[t.sport] || SPORT_COLOR.other) + '">' +
      '<div class="ltop"><span class="llogo">' + (t.icon ? img(t.icon) : '📺') + '</span><span class="lch">' + esc(t.channel) + '</span><span class="llive">● LIVE</span></div>' +
      '<div class="lmid">' + mid + '</div><div class="lbot">' + tagChip(t.tag) + '<span class="linfo">' + (teams.length ? esc(t.title) + ' · ' : '') + esc(info) +
      (t.feeds && t.feeds.length > 1 ? ' · +' + (t.feeds.length - 1) + ' feeds' : '') + '</span></div><div class="lprog"><div style="width:' + pct + '%"></div></div></button>';
  }
  function liveRowHtml(title, items, opts) {
    opts = opts || {};
    return '<section class="row"><h2><span class="mk" style="background:' + (opts.color || '#e8604f') + '"></span>' + esc(title) + (opts.hint ? ' <span class="hint">' + esc(opts.hint) + '</span>' : '') + '</h2><div class="row-items">' +
      items.map(function (t) { return liveCardHtml(t, opts.big); }).join('') + (opts.tail || '') + '</div></section>';
  }
  function wireLive(el, map) {
    Array.prototype.forEach.call(el.querySelectorAll('.lcard'), function (b) {
      if (b._wired) return; b._wired = true;
      b.onclick = function () {
        var t = map[b.getAttribute('data-lk')]; if (!t || !t.sid) return toast('This channel is not available on the TV');
        playLive(liveGroupFromMac(t), null, 0);
      };
      b._fav = function () {                       // yellow on a live card: pick another feed
        var t = map[b.getAttribute('data-lk')]; if (!t || !t.feeds || t.feeds.length < 2) return;
        var m = modal('<h2>' + esc(t.title) + '</h2><p class="hint">Watch on</p><div class="btns">' + t.feeds.map(function (f, i) {
          return '<button class="f btn' + (i ? '' : ' primary') + '" data-i="' + i + '">' + esc(f.name) + (f.country ? ' · ' + esc(f.country) : '') + '</button>'; }).join('') + '</div>');
        Array.prototype.forEach.call(m.querySelectorAll('[data-i]'), function (x) {
          x.onclick = function () { closeModal(); playLive(liveGroupFromMac(t, t.feeds[Number(x.getAttribute('data-i'))]), null, 0); };
        });
      };
    });
  }
  // the big picture at the top of Tonight: a pick from For you, Hot, Wildcard with its backdrop
  function heroHtml(d) {
    var picks = [], seen = {}, lists = [['foryou', 'For you', ''], ['hot', 'Hot right now', ' orange'], ['wild', 'Wildcard', ' teal'], ['new', 'New this week', ' creme']];
    for (var i = 0; i < 6 && picks.length < 5; i++) lists.forEach(function (l) {
      var r = (d.rows || []).filter(function (x) { return x.id === l[0]; })[0];
      var t = r && r.tiles.filter(function (x) { return x.meta && x.meta.backdrop; })[i];
      if (t && !seen[t.key] && picks.length < 5) { seen[t.key] = 1; picks.push({ t: t, label: l[1], cls: l[2] }); }
    });
    return picks;
  }
  function renderHero(d, gen) {
    var el = $('#hero'); if (!el || gen !== home.gen) return;
    var picks = heroHtml(d); if (!picks.length) return;
    var base = (d.img_base || 'https://image.tmdb.org/t/p').replace(/\/w\d+$/, '');
    var i = 0;
    function show() {
      var p = picks[i], t = p.t, m = t.meta || {};
      var subs = [t.year, (m.genres || []).slice(0, 3).join(' · '), m.imdb_rating ? 'IMDb ' + Number(m.imdb_rating).toFixed(1) : '', m.rt != null ? '🍅 ' + m.rt + '%' : ''].filter(Boolean).join('  ·  ');
      el.innerHTML = '<div class="hero"><img class="hbg" src="' + esc(base + '/w1280' + m.backdrop) + '" onerror="this.style.visibility=\'hidden\'"><div class="hshade"></div>' +
        '<div class="htxt"><span class="lchip' + p.cls + '">' + esc(p.label) + '</span><h1>' + esc(m.title || t.title) + '</h1><div class="hsub">' + esc(subs) + '</div>' +
        '<div class="hov">' + esc(m.overview || t.reason || '') + '</div><button class="f btn primary" id="heroGo">▶ Watch</button>' +
        (picks.length > 1 ? '<button class="f btn" id="heroNext">Next pick ▸</button>' : '') + '</div></div>';
      var it = macTile(t); home.items[it.key] = it;
      $('#heroGo').onclick = function () { openMacItem(it); };
      if ($('#heroNext')) $('#heroNext').onclick = function () { i = (i + 1) % picks.length; show(); N.focus($('#heroNext')); };
    }
    show();
  }
  function renderLiveNow(d, gen) {
    var el = $('#rowLive'); if (!el || gen !== home.gen) return;
    var r = (d.rows || []).filter(function (x) { return x.id === 'live'; })[0];
    var items = (r && r.tiles) || [], map = {};
    items.forEach(function (t) { map[t.gkey] = t; });
    var focusWasHere = N.current() && el.contains(N.current());
    var tail = '<button class="f lcard lmore" id="liveAll"><span class="lem">🏟️</span><div class="lt">' + (items.length ? 'All live sport' + (r.total > items.length ? ' (' + r.total + ')' : '') : 'No live sport worth showing right now') + '</div><div class="linfo">Open the Sport overview ▸</div></button>';
    el.innerHTML = liveRowHtml('Live now', items, { hint: 'Being played right now', tail: tail });
    wireLive(el, map);
    $('#liveAll').onclick = showSport;
    if (focusWasHere) N.first(el);
  }
  // ---------- Sport tab ----------
  function showSport() {
    showTop(true); setTab('sport');
    view.back = showHome;
    if (!MAC.link()) {
      screen.innerHTML = '<div class="page">' + empty('Live sport comes from Beeldbuis on your Mac', 'Link the TV with your Mac in <b>Settings → Beeldbuis on your Mac</b>. The Mac checks the guide and live scores and shows only what is being played right now.') + '</div>';
      N.first(screen); return;
    }
    if (!screen.querySelector('.sport')) screen.innerHTML = '<div class="home sport"><div class="loading">Looking for live sport…</div></div>';
    MAC.get('/api/sport', { profile: S.get().profile || 'p1' }, 15000).then(function (d) {
      if (view.tab !== 'sport') return;
      sportView.data = d; renderSportTv();
    }).catch(function (e) { if (view.tab === 'sport') screen.innerHTML = '<div class="page">' + empty('Beeldbuis on your Mac does not answer', errorMessage(e)) + '</div>'; });
    clearTimeout(sportView.timer);
    sportView.timer = setTimeout(function () { if (view.tab === 'sport' && !document.body.classList.contains('playing') && !$('#modal')) showSport(); }, 60000);
  }
  function renderSportTv() {
    var d = sportView.data, map = {};
    var groups = d.groups.map(function (g) { return { id: g.id, label: g.label, emoji: g.emoji, items: g.items.filter(function (t) { return t.local; }) }; }).filter(function (g) { return g.items.length; });
    var n = groups.reduce(function (a, g) { return a + g.items.length; }, 0);
    groups.forEach(function (g) { g.items.forEach(function (t) { map[t.gkey] = t; }); });
    var fid = N.current() && N.current().id;
    var html = '<div class="sp-head"><img src="img/icon-192.png" alt=""><div><h1>Live sport</h1><span>' + (n ? n + ' events being played right now' : 'Nothing being played right now') + ' · ' + hhmm(d.t) + '</span></div></div>' +
      '<div class="chips sp-chips"><button class="f chip' + (sportView.filter ? '' : ' on') + '" id="spf_" data-f="">All <small>' + n + '</small></button>' +
      groups.map(function (g) { return '<button class="f chip' + (sportView.filter === g.id ? ' on' : '') + '" id="spf_' + g.id + '" data-f="' + g.id + '">' + g.emoji + ' ' + esc(g.label) + ' <small>' + g.items.length + '</small></button>'; }).join('') +
      '<button class="f chip' + (sportView.scores ? ' on' : '') + '" id="spScores">' + (sportView.scores ? 'Scores shown' : 'Show scores') + '</button></div>';
    if (!n) html += empty('No live sport on your channels at the moment', 'This page refreshes by itself every minute.');
    if (!sportView.filter) {
      var top = [].concat.apply([], groups.map(function (g) { return g.items; })).filter(function (t) { return t.tag || t.match; }).sort(function (a, b) { return b.score - a.score; }).slice(0, 4);
      if (top.length) html += liveRowHtml('Big right now', top, { color: '#f0b429', big: true });
    }
    groups.filter(function (g) { return !sportView.filter || g.id === sportView.filter; }).forEach(function (g) {
      html += liveRowHtml(g.emoji + ' ' + g.label, g.items, { color: SPORT_COLOR[g.id] || SPORT_COLOR.other });
    });
    screen.innerHTML = '<div class="home sport">' + html + '</div>';
    wireLive(screen, map);
    Array.prototype.forEach.call(screen.querySelectorAll('[data-f]'), function (b) {
      b.onclick = function () { sportView.filter = b.getAttribute('data-f'); renderSportTv(); N.focus($('#spf_' + sportView.filter)); };
    });
    $('#spScores').onclick = function () { sportView.scores = !sportView.scores; renderSportTv(); N.focus($('#spScores')); };
    if (fid && $('#' + fid)) N.focus($('#' + fid)); else N.first(screen.querySelector('.lcard') ? screen.querySelector('.row') : screen);
  }
  // ---------- resume positions shared with the Mac ----------
  // The Mac keys positions as vod:<stream id> and ep:<episode id>; the TV as movie:<id> and episode:<id>.
  function macPosKey(item) { return item.type === 'movie' ? 'vod:' + item.id : item.type === 'episode' ? 'ep:' + item.id : ''; }
  function pushPos(item, t, d) {
    var key = item && MAC.link() && macPosKey(item);
    if (!key || t < 20) return;
    if (d && t > d * 0.96) { MAC.progress({ key: key, remove: true }).catch(function () {}); return; }
    var prof = S.get().profile || 'p1', si = item.seriesItem;
    MAC.progress({ key: key, profile: prof, item: { kind: item.type === 'movie' ? 'movie' : 'series', name: item.name, sid: item.id, ext: item.ext || 'mp4',
      extra: '', icon: item.icon || '', series: si ? si.id : null, nextKey: null, gkey: item.macKey || (si && si.macKey) || null, profile: prof,
      t: Math.floor(t), dur: Math.floor(d || 0) } }).then(function () {
      var mine = S.get().positions[item.key];
      if (mine && !mine.synced) { mine.synced = true; S.save(); }
    }).catch(function () {});
  }
  function pullPositions(gen) {
    if (!MAC.link()) return;
    MAC.positions().then(function (all) {
      if (gen !== home.gen || !all) return;
      var s = S.get(), prof = s.profile || 'p1', changed = false, seen = {};
      Object.keys(all).forEach(function (k) {
        var v = all[k], m = /^(vod|ep):(.+)$/.exec(k);
        if (!v || !m || (v.profile && v.profile !== prof && v.profile !== 'both')) return;
        var movie = m[1] === 'vod', lk = (movie ? 'movie:' : 'episode:') + m[2], at = (v.updated || 0) * 1000, mine = s.positions[lk];
        seen[lk] = true;
        if (mine && mine.at >= at) return;
        var item = movie ? { key: lk, type: 'movie', id: m[2], name: v.name, icon: v.icon, ext: v.ext || 'mp4', macKey: v.gkey || undefined }
          : { key: lk, type: 'episode', id: m[2], name: v.name, icon: v.icon, ext: v.ext || 'mp4',
              seriesItem: v.series ? { key: 'series:' + v.series, type: 'series', id: v.series, name: String(v.name || '').split(' · ')[0], icon: v.icon } : null };
        s.positions[lk] = { item: item, t: Math.floor(v.t || 0), d: Math.floor(v.dur || 0), at: at, synced: true };
        changed = true;
      });
      // a synced position the Mac no longer has was finished there; one the Mac never had (watched before linking) is sent now
      Object.keys(s.positions).forEach(function (lk) {
        var mine = s.positions[lk];
        if (!/^(movie|episode):/.test(lk) || seen[lk]) return;
        if (mine.synced) { delete s.positions[lk]; changed = true; } else pushPos(mine.item, mine.t, mine.d);
      });
      if (!changed) return;
      S.save();
      if (!$('#modal') && !document.body.classList.contains('playing') && screen.querySelector('.home')) { home.pulled = true; showHome(); }
    }).catch(function () {});
  }

  // ---------- watchlist: only what you tick yourself. With the Mac linked it lives on the Mac (shared with the
  // Mac and iPhone, per profile); without it, on this TV.
  var macWl = { profile: null, keys: {} };
  function macWlLoad() {
    var p = S.get().profile || 'p1';
    if (!MAC.link()) return;
    MAC.watchKeys(p).then(function (d) {
      var k = {}; (d.keys || []).forEach(function (x) { k[x] = 1; });
      macWl = { profile: p, keys: k };
    }).catch(function () {});
  }
  function inWatchlist(it) {
    if (it.macKey && MAC.link()) return it.wl != null ? !!it.wl : !!macWl.keys[it.macKey];
    return S.isFav(it.key);
  }
  function toggleWatch(it, done) {
    if (it.macKey && MAC.link()) {
      var on = !inWatchlist(it);
      MAC.watchlist(it.macKey, on, S.get().profile || 'p1').then(function () {
        it.wl = on; if (on) macWl.keys[it.macKey] = 1; else delete macWl.keys[it.macKey];
        toast(on ? 'Added to your watchlist' : 'Removed from your watchlist');
        if (done) done(on);
      }).catch(function (e) { toast('Beeldbuis on your Mac does not answer (' + errorMessage(e) + ')'); });
      return;
    }
    var o = S.toggleFav(stripItem(it));
    toast(o ? 'Added to your watchlist' : 'Removed from your watchlist');
    if (done) done(o);
  }
  function wlLabel(on) { return on ? '✓ On your watchlist' : '+ Watchlist'; }
  // the Mac's watchlist row, plus titles saved on this TV that the Mac does not know
  function homeWatchlist(gen, tiles, local) {
    var mac = tiles.map(function (t) { var o = macTile(t); o.wl = true; return o; });
    var k = {}; mac.forEach(function (o) { k[o.macKey] = 1; k[String(o.name).toLowerCase()] = 1; });
    macWl = { profile: S.get().profile || 'p1', keys: {} };
    mac.forEach(function (o) { macWl.keys[o.macKey] = 1; });
    macWlLoad();
    var items = mac.concat(local.filter(function (it) { return !(it.macKey && k[it.macKey]) && !k[String(it.name).toLowerCase()]; }));
    var el = $('#rowWl');
    if (!el || gen !== home.gen) return;
    if (!items.length) { el.innerHTML = ''; return; }
    homeRow('#rowWl', gen, 'Watchlist', items, { hint: 'Yellow button adds or removes', sub: function (o) { return o.macKey ? '' : 'On this TV'; } });
  }
  function wireHome(el) {
    Array.prototype.forEach.call(el.querySelectorAll('.tile'), function (b) {
      if (b._wired) return; b._wired = true;
      b.onclick = function () { openItem(home.items[b.getAttribute('data-key')]); };
      b._fav = function () {
        var it = home.items[b.getAttribute('data-key')]; if (!it) return;
        if (it.type !== 'live') return toggleWatch(it);
        favToast(toggleLiveFav(it.group || it));
      };
    });
  }
  // IMDb + Rotten Tomatoes on every movie and series tile: from the Mac when paired (it looks up what it does not
  // have yet, so we ask again once a little later), otherwise from OMDb with the key typed on this TV.
  function fmtImdb(v) { var n = parseFloat(v); return isNaN(n) ? '' : n.toFixed(1); }
  function rateBadge(imdb, rt) {
    var i = fmtImdb(imdb), r = rt === '' || rt == null ? '' : String(rt).replace('%', '');
    if (!i && !r) return '';
    return '<span class="rate">' + (i ? 'IMDb ' + esc(i) : '') + (r ? '<em class="rt ' + (parseInt(r, 10) >= 60 ? 'rt-f' : 'rt-r') + '">' + esc(r) + '%</em>' : '') + '</span>';
  }
  function rateLabel(imdb, rt) {
    var i = fmtImdb(imdb), r = rt === '' || rt == null ? '' : String(rt).replace('%', '');
    return [i ? 'IMDb ' + i : '', r ? 'Rotten Tomatoes ' + r + '%' : ''].filter(Boolean).join(' · ');
  }
  function provId(it) { return it.id ? (it.type === 'series' ? 'series:' : 'movie:') + it.id : ''; }
  function homeItem(b) { return home.items[b.getAttribute('data-key')] || {}; }
  function vodGridItem(b) { return vod.items[+b.getAttribute('data-i')] || {}; }
  function rateTiles(el, again, lookup) {
    var want = [];
    lookup = lookup || homeItem;
    Array.prototype.forEach.call(el.querySelectorAll('.tile-poster'), function (b) {
      if (b.querySelector('.rate') || b.classList.contains('more') || b._rated) return;
      var it = lookup(b);
      if (it.type !== 'movie' && it.type !== 'series') return;
      if (it.imdb || it.rt !== '' && it.rt != null) { b.insertAdjacentHTML('beforeend', rateBadge(it.imdb, it.rt)); return; }
      want.push({ b: b, it: it });
    });
    if (!want.length) return;
    if (MAC.link()) {
      var keys = [], ids = [];
      want.forEach(function (w) { if (w.it.macKey) keys.push(w.it.macKey); else if (provId(w.it)) ids.push(provId(w.it)); });
      MAC.ratings(keys.slice(0, 200), ids.slice(0, 200)).then(function (d) {
        var missing = 0;
        want.forEach(function (w) {
          var r = d[w.it.macKey || provId(w.it)];
          if (r && (r.imdb_rating || r.rt != null)) {
            w.it.imdb = r.imdb_rating || ''; w.it.rt = r.rt != null ? r.rt : '';
            if (document.body.contains(w.b) && !w.b.querySelector('.rate')) w.b.insertAdjacentHTML('beforeend', rateBadge(w.it.imdb, w.it.rt));
          } else missing++;
        });
        if (missing && !again) setTimeout(function () { if (document.body.contains(el)) rateTiles(el, true, lookup); }, 15000);
        else want.forEach(function (w) { w.b._rated = true; });
      }).catch(function () { omdbTiles(want); });
    } else omdbTiles(want);
  }
  function omdbTiles(want) {
    if (!OM.enabled()) return;
    want.forEach(function (w) {
      OM.ratings(w.it.name, w.it.type, w.it.year).then(function (v) {
        if (v && (v.imdb || v.rt) && document.body.contains(w.b) && !w.b.querySelector('.rate')) w.b.insertAdjacentHTML('beforeend', rateBadge(v.imdb, v.rt));
      });
    });
  }
  // ---------- recordings on the Mac ----------
  function homeRecordings(gen) {
    if (!MAC.link() || !root.BBRemote) return;
    root.BBRemote.recordings().then(function (d) {
      var items = ((d && d.items) || []).filter(function (r) { return r.file && (r.status === 'done' || r.status === 'recording'); })
        .slice(0, 20).map(function (r) { return { key: 'rec:' + r.id, type: 'rec', id: r.id, name: r.title, icon: r.icon || '', rec: r }; });
      homeRow('#rowRec', gen, 'Recordings', items, { hint: 'Recorded on your Mac', sub: function (it) {
        var r = it.rec, d = new Date(r.start * 1000);
        return (r.status === 'recording' ? '● Recording now · ' : '') + (r.channel ? r.channel + ' · ' : '') + DAYS[d.getDay()].slice(0, 3) + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()].slice(0, 3);
      } });
    }).catch(function () {});
  }
  function playRec(it) {
    var R = root.BBRemote, r = it.rec || {}, item = { key: it.key, type: 'rec', id: it.id, name: it.name, icon: it.icon, rec: r };
    if (r.status === 'recording') {
      busy(true);
      R.recHls(it.id).then(function (url) { busy(false); startPlayback(url, item, { live: true }); $('#osdTitle').textContent = it.name; $('#osdSub').textContent = 'Recording in progress on the Mac'; })
        .catch(function (e) { busy(false); toast(errorMessage(e)); });
      return;
    }
    var pos = S.get().positions[it.key];
    startPlayback(R.recUrl(it.id), item, { live: false, start: pos ? pos.t : 0 });
    $('#osdTitle').textContent = it.name; $('#osdSub').textContent = (r.channel ? r.channel + '  ·  ' : '') + '▲ Subtitles';
  }
  function recordNow(nextProg) {
    if (!play || !play.live || !play.group || !MAC.link()) { toast('Recording needs Beeldbuis on your Mac (Settings → Beeldbuis on your Mac)'); return; }
    var v = order(play.group)[play.variant || 0];
    toast('Asking the Mac to record…');
    root.BBRemote.record(v.id, nextProg).then(function (rec) {
      var d = new Date(rec.start * 1000);
      toast('● Recording “' + esc(rec.title) + '” on the Mac' + (rec.start > Date.now() / 1000 + 60 ? ' at ' + hhmm(rec.start) : '') + (rec.notes && rec.notes.length ? '<br>' + esc(rec.notes[0]) : ''), 6000);
    }).catch(function (e) { toast(errorMessage(e), 7000); });
  }
  // ---------- commands from the phone or the Mac ("Play on TV") ----------
  function onRemote(c) {
    if (!c || !c.action) return;
    saverStop(); saverArm();
    if (c.action === 'text') return remoteText(c);
    if (c.action === 'panel') { if ($('#modal')) closeModal(); return sideCycle(); }
    if ($('#modal')) closeModal();
    if (c.action === 'play' && c.name === 'Beeldbuis self-test' && root.BBDiag) {   // diagnosis from the Mac (0.9.3)
      if (play) stopPlayback();
      toast('Player self-test running (about 3 minutes)…', 8000);
      root.BBDiag.run({ live: c.kind === 'live' ? c.id : null, episode: c.kind === 'episode' ? c.id : null, ext: c.ext });
      return;
    }
    if (c.action === 'play') {
      var from = c.from ? ' from ' + esc(c.from) : '';
      if (c.kind === 'live') {
        var g = { key: 'mac:' + (c.key || c.id), name: c.name || 'Channel', icon: c.icon || '', variants: [{ id: c.id, q: '' }] };
        if (play) stopPlayback();
        playLive(g, [g], 0);
      } else if (c.kind === 'movie') {
        if (play) stopPlayback();
        playVod({ key: 'vod:' + c.id, type: 'movie', id: c.id, ext: c.ext || 'mp4', name: c.name || 'Movie', icon: c.icon || '' }, c.t || 0);
      } else if (c.kind === 'episode') {
        if (play) stopPlayback();
        playEpisode({ key: 'ep:' + c.id, type: 'episode', id: c.id, ext: c.ext || 'mp4', name: c.name || 'Episode', icon: c.icon || '' }, null, c.t ? { t: c.t } : null);
      }
      toast('▶ ' + esc(c.name || '') + from);
    } else if (c.action === 'stop') {
      if (play) leavePlayer();
    } else if (c.action === 'rec-start') {
      if (play && play.live && play.item && play.item.type !== 'rec') {        // one connection: the Mac's recording takes over
        stopPlayback();
        toast('● The Mac started recording “' + esc(c.title) + '”. Showing the recording.', 7000);
        playRec({ key: 'rec:' + c.id, id: c.id, name: c.title, rec: { status: 'recording', channel: c.channel } });
      } else if (play && play.item && play.item.type !== 'rec') {              // a film or episode straight from the provider
        var it = play.item, at = PL.position ? PL.position() : 0;
        if (at > 20) { S.setPosition(it, at, PL.duration ? PL.duration() : 0); pushPos(it, at, PL.duration ? PL.duration() : 0); }
        stopPlayback();
        recBlocks({ id: c.id, title: c.title, channel: c.channel }, null, true);
      }
    }
  }

  function homeRow(slot, gen, title, items, opts) {
    var el = $(slot);
    if (!el || gen !== home.gen || !items.length) return;
    items.forEach(function (it) { home.items[it.key] = it; });
    var focusWasHere = N.current() && el.contains(N.current());
    el.innerHTML = row(title, items, null, opts);
    wireHome(el);
    rateTiles(el);
    if (focusWasHere) N.first(el);
    if (!N.current() || !document.body.contains(N.current())) N.first(screen);
  }
  // Now/next for a channel. The provider's short EPG only works for streams that carry a guide id, and the
  // versions of one channel differ (ESPN 1 LQ has none, ESPN 1 HD has one): try the preferred version, then the
  // others that do have an id, then the Mac's guide (which also matches channels by name) when the TV is linked.
  function chanEpg(g, v) {
    var vs = (g && g.variants) || [], tried = [];
    if (v && v.epg !== '') tried.push(v);
    vs.forEach(function (x) { if (x.epg && tried.indexOf(x) < 0 && tried.length < 3) tried.push(x); });
    if (!tried.length && v) tried.push(v);
    function next(i) {
      if (i >= tried.length) return viaMac();
      return X.shortEpg(tried[i].id).then(function (ps) {
        var now = Date.now() / 1000;
        return ps.some(function (p) { return p.end > now; }) ? ps : next(i + 1);
      });
    }
    function viaMac() {
      if (!MAC || !MAC.link() || !vs.length) return Promise.resolve([]);
      return MAC.get('/api/epg', { id: (v || vs[0]).id }, 8000).then(function (list) {
        return (list || []).map(function (p) { return { title: p.title, desc: p.description || '', start: p.start, end: p.stop }; });
      }).catch(function () { return []; });
    }
    return next(0);
  }
  // current programme under each channel tile
  function homeNow(slot, groups, gen) {
    var el = $(slot); if (!el) return;
    var tiles = el.querySelectorAll('.tile-sub');
    groups.forEach(function (g, i) {
      if (!g || !g.variants || !tiles[i]) return;
      chanEpg(g, null).then(function (ps) {
        if (gen !== home.gen) return;
        var now = Date.now() / 1000, p = ps.filter(function (x) { return x.end > now; })[0];
        tiles[i].textContent = p ? (p.start <= now ? 'Now · ' : hhmm(p.start) + ' · ') + p.title : '';
      });
    });
  }
  function homeNews(gen) {
    var countries = S.get().countries;
    X.liveCategories().then(function (cats) {
      var mine = myLiveCats(cats);
      var news = mine.filter(function (c) { return NEWS_CAT.test(catLabel(c.category_name)); }).slice(0, 3);
      return Promise.all(news.map(function (c) { return X.liveStreams(c.category_id); })).then(function (lists) {
        var streams = [].concat.apply([], lists);
        var groups = G.buildGroups(news, streams).slice(0, 16);
        var items = groups.map(liveItem);
        homeRow('#rowNews', gen, 'News', items, { hint: 'Live news on your channels', sub: function () { return ' '; } });
        homeNow('#rowNews', groups, gen);
      });
    }).catch(function () {});
  }
  function homeMac(gen, fallback) {
    if (!MAC.link()) return fallback();
    var slots = { foryou: '#rowForYou', makers: '#rowMakers', circle: '#rowCircle', hot: '#rowHot', wild: '#rowWild', 'new': '#rowNew' };
    var hints = { foryou: 'From your taste', makers: 'More from the people behind your favourites', circle: 'Writers, cinematographers and studios you keep coming back to', hot: 'Talked about right now', wild: 'Something different', 'new': 'New at your provider' };
    MAC.home(S.get().profile).then(function (d) {
      if (gen !== home.gen) return;
      if (d.img_base) macImg = String(d.img_base).replace(/\/+$/, '') + '/w342';   // the Mac sends the base without a size
      renderHero(d, gen); renderLiveNow(d, gen);
      if (d.names) S.PROFILE_IDS.forEach(function (id) {       // take the names from the Mac while the TV still has the defaults
        if (d.names[id] && /^(Profile [12]|Together)$/.test(S.profileName(id))) {
          S.setProfileName(id, d.names[id]);
          var b = screen.querySelector('.prof[data-p="' + id + '"]'); if (b) b.textContent = S.profileName(id);
        }
      });
      var any = false;
      (d.rows || []).forEach(function (r) {
        if (r.id === 'watchlist' && r.kind === 'tiles') return homeWatchlist(gen, r.tiles || [], S.get().favorites.filter(function (f) { return f.type !== 'live'; }));
        if (!slots[r.id] || r.kind !== 'tiles' || !(r.tiles || []).length) return;
        any = true;
        homeRow(slots[r.id], gen, r.title, r.tiles.map(macTile), { hint: hints[r.id], sub: function (o) { return o.reason || ''; } });
      });
      if (!any) fallback();
    }).catch(function () { if (gen === home.gen) fallback(); });
  }
  // "For you" without an online taste service: more from the categories you watch and save, best rated first
  // items saved by older versions don't know their category: look it up once and remember it
  function withCats(seeds) {
    var need = [], done = {};
    seeds.forEach(function (it) {
      var src = it.seriesItem || it;
      if ((src.type === 'movie' || src.type === 'series') && !src.cat && !done[src.key] && need.length < 4) { done[src.key] = 1; need.push(src); }
    });
    if (!need.length) return Promise.resolve(seeds);
    return Promise.all(need.map(function (src) {
      return (src.type === 'movie' ? X.vodInfo(src.id) : X.seriesInfo(src.id)).then(function (r) {
        var c = r && ((r.movie_data && r.movie_data.category_id) || (r.info && r.info.category_id) || r.category_id);
        if (c) remember(src.key, String(c));
      }, function () {});
    })).then(function () { return seeds; });
  }
  function remember(key, cat) {
    var s = S.get();
    function fix(it) { if (!it) return; if (it.key === key) it.cat = cat; if (it.seriesItem && it.seriesItem.key === key) it.seriesItem.cat = cat; }
    s.favorites.forEach(fix); s.recent.forEach(fix);
    Object.keys(s.positions).forEach(function (k) { fix(s.positions[k].item); });
    S.save();
  }
  function homeForYou(gen, seeds) {
    withCats(seeds).then(function () { if (gen === home.gen) forYouRow(gen, seeds); });
  }
  function forYouRow(gen, seeds) {
    var s = S.get(), seen = {}, cats = [], why = {};
    Object.keys(s.positions).concat(s.recent.map(function (r) { return r.key; }), s.favorites.map(function (f) { return f.key; }))
      .forEach(function (k) { seen[k] = 1; });
    seeds.forEach(function (it) {
      var src = it.seriesItem || it;
      if ((src.type === 'movie' || src.type === 'series') && src.cat) {
        var k = src.type + '|' + src.cat;
        if (cats.indexOf(k) < 0 && cats.length < 3) { cats.push(k); why[k] = src.name; }
      }
    });
    if (!cats.length) return;
    Promise.all(cats.map(function (k) {
      var type = k.split('|')[0], id = k.split('|')[1];
      return (type === 'movie' ? X.vodStreams(id) : X.series(id)).then(function (xs) {
        return xs.map(function (x) { var o = vodItem(x, type); o.cat = o.cat || id; o.because = why[k]; return o; })
          .filter(function (o) { return !seen[o.key] && parseFloat(o.rating) > 0; })
          .sort(function (a, b) { return (parseFloat(b.rating) - parseFloat(a.rating)) || (b.added - a.added); }).slice(0, 12);
      }, function () { return []; });
    })).then(function (lists) {
      var out = [], used = {};
      for (var i = 0; i < 12; i++) lists.forEach(function (l) { var o = l[i]; if (o && !used[o.key]) { used[o.key] = 1; out.push(o); } });
      homeRow('#rowForYou', gen, 'For you', out.slice(0, 24), { hint: 'More like what you watch', sub: function (o) { return 'Like ' + o.because; } });
    });
  }
  // newest additions at the provider, from categories that say "new" (else the first few)
  function homeNew(gen) {
    X.vodCategories().then(function (cats) {
      var pick = cats.filter(function (c) { return /(^|[^a-z])(new|nieuw|recent|latest|added)/i.test(c.category_name); });
      if (!pick.length) pick = cats.slice(0, 3);
      return Promise.all(pick.slice(0, 3).map(function (c) { return X.vodStreams(c.category_id).catch(function () { return []; }); })).then(function (lists) {
        var weekAgo = Date.now() / 1000 - 14 * 86400, used = {};
        var all = [].concat.apply([], lists).filter(function (x) { if (used[x.id]) return false; used[x.id] = 1; return true; })
          .sort(function (a, b) { return b.added - a.added; });
        var fresh = all.filter(function (x) { return x.added > weekAgo; });
        var items = (fresh.length >= 6 ? fresh : all).slice(0, 24).map(function (x) { return vodItem(x, 'movie'); });
        homeRow('#rowNew', gen, fresh.length >= 6 ? 'New this week' : 'Recently added', items, { hint: 'New at your provider' });
      });
    }).catch(function () {});
  }
  function openItem(it) {
    if (!it) return;
    if (it.macKey && !it.id) return openMacItem(it);
    if (it.type === 'live') playLive(it.group || it, [it.group || it], 0);
    else if (it.type === 'movie') showMovie(it);
    else if (it.type === 'episode') playEpisode(it, it.seriesItem, S.get().positions[it.key]);
    else if (it.type === 'series') showSeries(it);
    else if (it.type === 'rec') playRec(it);
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
      live.cats = myLiveCats(cats);
      screen.innerHTML = '<div class="split"><div class="side" id="liveCats">' +
        (MAC.link() ? '<button class="f cat gcat" data-id="guide">▦ TV guide</button>' : '') +
        '<button class="f cat" data-id="fav">★ Favourites</button>' +
        live.cats.map(function (c) { return '<button class="f cat" data-id="' + esc(c.category_id) + '">' + esc(c.category_name) + '</button>'; }).join('') +
        '</div><div class="main" id="liveList"></div></div>';
      Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) {
        b.onclick = function () {
          var id = b.getAttribute('data-id');
          if (id === 'guide') { var c = screen.querySelector('.cat.active'); showGuide(live.groups, c ? c.textContent : ''); return; }
          loadLiveCat(id, true);
        };
      });
      var start = live.catId || (liveFavs().length ? 'fav' : (live.cats[0] && String(live.cats[0].category_id)));
      view.back = showHome;
      if (start) loadLiveCat(String(start), false); else $('#liveList').innerHTML = empty('No channels', 'Choose your channel groups in Settings.');
      var btn = screen.querySelector('.cat[data-id="' + start + '"]');
      N.focus(btn || screen.querySelector('.cat'));
      syncChannels(false).then(function (changed) { if (changed && view.tab === 'live' && $('#liveCats') && !play) showLive(); });
    }).catch(function (e) { busy(false); toast(errorMessage(e)); });
  }
  function loadLiveCat(id, focusList) {
    live.catId = id;
    Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) { b.classList.toggle('active', b.getAttribute('data-id') === id); });
    var list = $('#liveList');
    if (id === 'fav') {
      live.groups = liveFavs().slice();
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
      list.innerHTML = empty(live.catId === 'fav' ? 'No favourite channels yet' : 'No channels here', live.catId === 'fav' ? 'Press the <b class="k-yellow">yellow</b> button on a channel to add it' + (MAC.link() ? ', or star channels on the Mac: the list is the same.' : '.') : '');
      return;
    }
    list.innerHTML = live.groups.map(function (g, i) {
      var fav = isLiveFav(g.key);
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
      b._fav = function () { var on = toggleLiveFav(live.groups[i]); favToast(on); var st = b.querySelector('.star'); if (on && !st) b.insertAdjacentHTML('beforeend', '<span class="star">★</span>'); if (!on && st) st.parentNode.removeChild(st); };
    });
    if (focusList) N.focus(list.querySelector('.chan'));
  }
  function fillNow(i) {
    var g = live.groups[i], el = $('#now' + i);
    if (!g || !el || el._done) return;
    el._done = true;
    chanEpg(g, null).then(function (ps) {
      var now = Date.now() / 1000, p = ps.filter(function (x) { return x.end > now; })[0];
      if (p) el.textContent = hhmm(p.start) + ' ' + p.title;
    });
  }

  // ---------- channel settings shared with the Mac (favourite channels, hidden categories) ----------
  var chanSync = { at: 0, busy: null };
  function syncChannels(force) {
    if (!MAC.link()) return Promise.resolve(false);
    if (chanSync.busy) return chanSync.busy;
    if (!force && Date.now() - chanSync.at < 60000) return Promise.resolve(false);
    chanSync.busy = MAC.channels().then(function (d) {
      chanSync.busy = null; chanSync.at = Date.now();
      if (!d || !d.ready) return false;                   // the Mac is still loading its channel list
      var s = S.get(), before = JSON.stringify([s.macHidden || [], (s.macFavs || []).map(function (g) { return g.key; })]);
      if (!s.favPushed) {                                  // once: favourites made on this TV join the Mac's list
        var sends = [];
        s.favorites.filter(function (f) { return f.type === 'live' && f.group; }).forEach(function (f) {
          if (d.fav_keys.indexOf(f.group.key) < 0) { d.favs.push(f.group); d.fav_keys.push(f.group.key); sends.push(MAC.fav(f.group.key, true)); }
        });
        Promise.all(sends).then(function () { S.get().favPushed = true; S.save(); }, function () { chanSync.at = 0; });
      }
      s.macFavs = d.favs; s.macHidden = d.hidden || []; S.save();
      var changed = before !== JSON.stringify([s.macHidden, s.macFavs.map(function (g) { return g.key; })]);
      if (changed) SR.reset();
      return changed;
    }).catch(function () { chanSync.busy = null; return false; });
    return chanSync.busy;
  }
  function liveFavs() {
    var s = S.get();
    if (MAC.link() && s.macFavs) return s.macFavs;
    return s.favorites.filter(function (f) { return f.type === 'live' && f.group; }).map(function (f) { return f.group; });
  }
  function isLiveFav(key) { return liveFavs().some(function (g) { return g.key === key; }); }
  function toggleLiveFav(g) {
    var s = S.get();
    if (!g) return false;
    if (!(MAC.link() && s.macFavs)) return S.toggleFav(liveItem(g));
    var on = !isLiveFav(g.key);
    s.macFavs = on ? s.macFavs.concat([g]) : s.macFavs.filter(function (x) { return x.key !== g.key; });
    if (S.isFav('live:' + g.key) !== on) S.toggleFav(liveItem(g)); else S.save();
    MAC.fav(g.key, on).catch(function () { toast('The Mac did not answer: this change is on the TV only for now'); });
    return on;
  }
  function favToast(on) { toast(on ? '★ Added to your channels' + (MAC.link() ? ' (also on the Mac)' : '') : 'Removed from your channels'); }
  // the channel groups to show: the countries chosen on this TV, minus what is hidden on the Mac
  function myLiveCats(cats) {
    var countries = S.get().countries, hidden = MAC.link() ? (S.get().macHidden || []) : [];
    return cats.filter(function (c) { return countries.indexOf(G.categoryCountry(c.category_name)) >= 0 && hidden.indexOf(String(c.category_id)) < 0; });
  }

  // ---------- TV guide (the Mac's guide, as a grid: channels down, time across) ----------
  var GW = 1440, GSPAN = 3 * 3600, GPX = GW / GSPAN;
  var guide = { groups: [], title: '', t0: 0, day: 0, data: {}, loaded: {}, recs: [], token: 0 };
  function dayStart(off) { var d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() + off); return d.getTime() / 1000; }
  function dayLabel(off) {
    if (off === 0) return 'Today'; if (off === -1) return 'Yesterday'; if (off === 1) return 'Tomorrow';
    var d = new Date(dayStart(off) * 1000); return DAYS[d.getDay()].slice(0, 3) + ' ' + d.getDate();
  }
  function showGuide(groups, title) {
    if (!MAC.link()) { toast('The TV guide comes from Beeldbuis on your Mac (Settings → Beeldbuis on your Mac)', 6000); return; }
    if (groups && groups.length) { guide.groups = groups.slice(0, 150); guide.title = title || ''; }
    if (!guide.groups.length) { guide.groups = liveFavs().slice(0, 150); guide.title = 'Your channels'; }
    if (!guide.groups.length) { toast('Open a channel group first, or add channels with the yellow button'); return; }
    showTop(true); setTab('live');
    var now = Date.now() / 1000;
    guide.day = 0; guide.t0 = Math.floor(now / 1800) * 1800 - 1800;
    guide.data = {}; guide.loaded = {};
    view.back = showLive;
    screen.innerHTML = '<div class="guide"><div class="g-top"><div class="g-days" id="gDays"></div><div class="g-info" id="gInfo"><b>' + esc(guide.title) + '</b><span>Loading the guide…</span></div></div>' +
      '<div class="g-ruler" id="gRuler"></div><div class="g-rows" id="gRows"></div></div>';
    drawDays();
    MAC.get('/api/recordings', {}, 6000).then(function (r) { guide.recs = (r && r.items) || r || []; markRecs(); }).catch(function () {});
    loadGuideDay(0, true);
  }
  function drawDays() {
    var back = Math.min(3, Math.max.apply(null, guide.groups.map(function (g) { return g.archive || 0; }).concat([0])));
    var html = '';
    for (var d = -back; d <= 3; d++) html += '<button class="f chip gday' + (d === guide.day ? ' on' : '') + '" data-d="' + d + '">' + dayLabel(d) + '</button>';
    html += '<button class="f chip gnow">Now</button>';
    $('#gDays').innerHTML = html;
    Array.prototype.forEach.call(screen.querySelectorAll('.gday'), function (b) {
      b.onclick = function () {
        var d = +b.getAttribute('data-d'), now = Date.now() / 1000;
        guide.day = d;
        guide.t0 = d === 0 ? Math.floor(now / 1800) * 1800 - 1800 : dayStart(d) + 19 * 3600;
        $('#gInfo').innerHTML = '<b>' + esc(guide.title || 'TV guide') + '</b><span>' + dayLabel(d) + '</span>';
        drawDays(); loadGuideDay(d, false, true);
      };
    });
    screen.querySelector('.gnow').onclick = function () { guide.day = 0; guide.t0 = Math.floor(Date.now() / 1800000) * 1800 - 1800; drawDays(); loadGuideDay(0, true); };
  }
  function loadGuideDay(d, focusNow, focusDay) {
    var tok = ++guide.token;
    if (guide.loaded[d]) { renderGuide(focusNow ? 'now' : null, focusDay); return; }
    var a = dayStart(d) - 3 * 3600, b = dayStart(d + 1) + 4 * 3600;
    var ids = guide.groups.map(function (g) { return g.variants[0].id; }), parts = [];
    for (var i = 0; i < ids.length; i += 50) parts.push(ids.slice(i, i + 50));
    if (!$('#gRows').innerHTML) $('#gRows').innerHTML = '<div class="loading">Loading the guide…</div>';
    Promise.all(parts.map(function (p) { return MAC.guide(p, a, b); })).then(function (res) {
      if (tok !== guide.token || !$('#gRows')) return;
      res.forEach(function (r) { Object.keys(r || {}).forEach(function (sid) {
        var lst = guide.data[sid] || [], seen = {};
        lst.forEach(function (p) { seen[p.start] = 1; });
        r[sid].forEach(function (p) { if (!seen[p.start]) lst.push(p); });
        lst.sort(function (x, y) { return x.start - y.start; });
        guide.data[sid] = lst;
      }); });
      guide.loaded[d] = true;
      var after = guide.after; guide.after = null;
      renderGuide(focusNow ? 'now' : after, focusDay);
    }).catch(function (e) { if (tok === guide.token && $('#gRows')) $('#gRows').innerHTML = empty('The guide is not available', errorMessage(e)); });
  }
  function gProgs(g) { return guide.data[String(g.variants[0].id)] || []; }
  function renderGuide(focusWhat, focusDay) {
    var rows = $('#gRows'); if (!rows) return;
    var t0 = guide.t0, t1 = t0 + GSPAN, now = Date.now() / 1000, keepTop = rows.scrollTop;
    var ruler = '<div class="g-chcol">' + esc(dayLabel(guide.day)) + '</div><div class="g-times">';
    for (var t = t0; t < t1; t += 1800) ruler += '<span style="left:' + Math.round((t - t0) * GPX) + 'px">' + hhmm(t) + '</span>';
    if (now > t0 && now < t1) ruler += '<i class="g-now" style="left:' + Math.round((now - t0) * GPX) + 'px"></i>';
    $('#gRuler').innerHTML = ruler + '</div>';
    rows.innerHTML = guide.groups.map(function (g, r) {
      var ps = gProgs(g).filter(function (p) { return p.stop > t0 && p.start < t1; });
      var cells = ps.map(function (p) {
        var a = Math.max(p.start, t0), b = Math.min(p.stop, t1), x = Math.round((a - t0) * GPX), w = Math.max(6, Math.round((b - a) * GPX) - 6);
        var cls = p.stop <= now ? ' past' : p.start <= now ? ' now' : '';
        return '<button class="f gp' + cls + '" data-r="' + r + '" data-s="' + p.start + '" style="left:' + x + 'px;width:' + w + 'px"><b>' + esc(p.title) + '</b><small>' + hhmm(p.start) + '–' + hhmm(p.stop) + '</small></button>';
      }).join('');
      if (!cells) cells = '<button class="f gp none" data-r="' + r + '" style="left:0;width:' + (GW - 6) + 'px"><b>No guide information</b><small>OK plays the channel</small></button>';
      return '<div class="g-row"><div class="g-ch">' + img(g.icon) + '<span>' + esc(g.name) + '</span></div><div class="g-progs">' +
        (now > t0 && now < t1 ? '<i class="g-now" style="left:' + Math.round((now - t0) * GPX) + 'px"></i>' : '') + cells + '</div></div>';
    }).join('');
    rows.scrollTop = keepTop;
    Array.prototype.forEach.call(rows.querySelectorAll('.gp'), function (b) {
      var g = guide.groups[+b.getAttribute('data-r')], st = b.getAttribute('data-s'), p = null;
      if (st) gProgs(g).forEach(function (x) { if (String(x.start) === st) p = x; });
      b._g = g; b._p = p;
      b.onclick = function () { guideAction(g, p); };
      b.onfocusin2 = function () { guideInfo(g, p); };
      b._fav = function () { favToast(toggleLiveFav(g)); };
    });
    markRecs();
    if (focusDay) { N.focus(screen.querySelector('.gday.on')); return; }
    if (focusWhat === 'now') {
      var first = rows.querySelector('.gp.now') || rows.querySelector('.gp');
      N.focus(first);
    } else if (focusWhat && focusWhat.r !== undefined) {
      N.focus(guideCellAt(focusWhat.r, focusWhat.t) || rows.querySelector('.gp'));
    }
  }
  function guideCellAt(r, t) {
    var best = null, bestD = Infinity;
    Array.prototype.forEach.call($('#gRows').querySelectorAll('.gp[data-r="' + r + '"]'), function (b) {
      var p = b._p, d = !p ? 0 : (t >= p.start && t < p.stop) ? 0 : Math.min(Math.abs(t - p.start), Math.abs(t - p.stop));
      if (d < bestD) { bestD = d; best = b; }
    });
    return best;
  }
  // at the left or right edge of the grid, the window moves 90 minutes
  function guideEdge(dir) {
    var c = N.current();
    if (!c || !c.classList.contains('gp') || !$('#gRows')) return false;
    var r = +c.getAttribute('data-r'), step = dir === 'right' ? 5400 : -5400;
    var edge = dir === 'right' ? guide.t0 + GSPAN + 60 : guide.t0 - 60;
    var t0 = guide.t0 + step, lo = dayStart(-3), hi = dayStart(4) - GSPAN;
    if (t0 < lo || t0 > hi) return true;
    guide.t0 = t0;
    var mid = t0 + GSPAN / 2, d = Math.floor((mid - dayStart(0)) / 86400);
    var keep = { r: r, t: edge };
    if (d !== guide.day) { guide.day = d; drawDays(); }
    if (!guide.loaded[d]) { loadGuideDay(d, false); guide.after = keep; return true; }
    renderGuide(keep);
    return true;
  }
  function guideInfo(g, p) {
    var el = $('#gInfo'); if (!el) return;
    if (!p) { el.innerHTML = '<b>' + esc(g.name) + '</b><span>No guide information · OK plays the channel</span>'; return; }
    var now = Date.now() / 1000, when = p.stop <= now ? (g.archive ? 'Catch-up' : 'Over') : p.start <= now ? 'On now' : '';
    el.innerHTML = '<b>' + esc(p.title) + '</b><span>' + esc(g.name) + ' · ' + dayLabel(Math.floor((p.start - dayStart(0)) / 86400)) + ' ' + hhmm(p.start) + '–' + hhmm(p.stop) +
      (when ? ' · ' + when : '') + (recFor(g, p) ? ' · ● Recording' : '') + '</span><p>' + esc(String(p.description || '').slice(0, 260)) + '</p>';
  }
  function recFor(g, p) {
    if (!p) return null;
    return guide.recs.filter(function (r) { return r.status !== 'cancelled' && r.status !== 'failed' && r.group === g.key && Math.abs(r.start - p.start) < 300; })[0] || null;
  }
  function markRecs() {
    var rows = $('#gRows'); if (!rows) return;
    Array.prototype.forEach.call(rows.querySelectorAll('.gp'), function (b) { b.classList.toggle('rec', !!(b._p && recFor(b._g, b._p))); });
  }
  // OK on a programme: watch, watch from the start, record, or cancel a recording
  function guideAction(g, p) {
    var now = Date.now() / 1000, idx = guide.groups.indexOf(g), list = guide.groups.length ? guide.groups : [g];
    if (idx < 0) { list = [g]; idx = 0; }
    if (!p) { playLive(g, list, idx); return; }
    var past = p.stop <= now, onNow = p.start <= now && !past, rec = recFor(g, p);
    var canCu = !!g.archive && now - p.start < g.archive * 86400;
    var btns = [];
    if (onNow) btns.push(['watch', '▶ Watch live']);
    if ((onNow || past) && canCu) btns.push(['start', past ? '▶ Watch (catch-up)' : '↺ Watch from the start']);
    if (!past) btns.push(rec ? ['cancel', '✕ Cancel the recording'] : ['rec', onNow ? '● Record the rest' : '● Record']);
    if (!onNow) btns.push(['watch', '▶ Watch ' + g.name + ' now']);
    modal('<h2>' + esc(p.title) + '</h2><p>' + esc(g.name) + ' · ' + dayLabel(Math.floor((p.start - dayStart(0)) / 86400)) + ' ' + hhmm(p.start) + '–' + hhmm(p.stop) +
      (past && !canCu ? ' · already over, and this channel has no catch-up' : '') + '</p>' + (p.description ? '<p class="dim">' + esc(String(p.description).slice(0, 400)) + '</p>' : '') +
      '<div class="btns">' + btns.map(function (b, i) { return '<button class="f btn' + (i ? '' : ' primary autofocus') + '" data-a="' + b[0] + '">' + esc(b[1]) + '</button>'; }).join('') +
      '<button class="f btn" data-a="close">Close</button></div>');
    Array.prototype.forEach.call($('#modal').querySelectorAll('.btn'), function (b) {
      b.onclick = function () {
        var a = b.getAttribute('data-a');
        closeModal();
        if (a === 'watch') playLive(g, list, idx);
        else if (a === 'start') playLive(g, list, idx, 0, { catchup: p.start });
        else if (a === 'rec') guideRecord(g, p);
        else if (a === 'cancel') MAC.post('/api/record/cancel', { id: rec.id }).then(function () { rec.status = 'cancelled'; markRecs(); toast('Recording cancelled'); }, function (e) { toast(errorMessage(e)); });
      };
    });
  }
  function guideRecord(g, p) {
    toast('Asking the Mac to record…');
    MAC.post('/api/remote/record', { stream_id: order(g)[0].id, start: p.start, stop: p.stop, title: p.title, description: p.description || '' }, 8000).then(function (rec) {
      guide.recs.push(rec); markRecs();
      toast('● “' + esc(rec.title) + '” will be recorded on the Mac' + (rec.start > Date.now() / 1000 + 60 ? ' (' + dayLabel(Math.floor((rec.start - dayStart(0)) / 86400)) + ' ' + hhmm(rec.start) + ')' : '') + (rec.notes && rec.notes.length ? '<br>' + esc(rec.notes[0]) : ''), 6000);
    }).catch(function (e) { toast(errorMessage(e), 7000); });
  }

  // ---------- English subtitles from the Mac (OpenSubtitles, or made by Whisper) when the file has none ----------
  function vttTime(s) { var m = String(s).trim().replace(',', '.').split(':'), sec = parseFloat(m.pop()) || 0, mi = +(m.pop() || 0), h = +(m.pop() || 0); return h * 3600 + mi * 60 + sec; }
  function parseVtt(txt) {
    var out = [];
    String(txt || '').replace(/\r/g, '').split(/\n\s*\n/).forEach(function (block) {
      var lines = block.split('\n'), i = 0;
      while (i < lines.length && lines[i].indexOf('-->') < 0) i++;
      if (i >= lines.length) return;
      var t = lines[i].split('-->');
      var text = lines.slice(i + 1).join('\n').replace(/<[^>]+>/g, '').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim();
      if (text) out.push([vttTime(t[0]), vttTime(String(t[1]).trim().split(/\s+/)[0]), text]);
    });
    out.sort(function (a, b) { return a[0] - b[0]; });
    return out;
  }
  // ---------- subtitles: drawing and style (js/substyle.js holds the style model, shared with the Mac) ----------
  var SS = root.BBSubStyle;
  var subEd = null;                                  // the open subtitle editor: {scene, tab}
  var SUB_DEMO = 'This is how your subtitles look.\nChange them with ◀ ▶ and see it straight away.';
  function subsEl() {
    var el = $('#subs');
    if (!el) { $('#stage').insertAdjacentHTML('beforeend', '<div id="subs"><span></span></div>'); el = $('#subs'); el._text = ''; applySubStyle(); }
    return el;
  }
  function subStyle() {
    var s = S.get(), all = s.subStyles || {};
    return SS.normalize(all[s.profile] || s.subStyle || {});
  }
  function saveSubStyle(st) {
    var s = S.get();
    s.subStyles = s.subStyles || {};
    s.subStyles[s.profile] = st;
    S.save();
  }
  function osdUp() { return !!play && !$('#osd').classList.contains('hidden'); }
  function applySubStyle() {
    var el = $('#subs'); if (!el) return;
    var c = SS.css(subStyle(), { h: 1080, osd: osdUp() && !subEd, osdPct: 20 });
    el._css = c;
    el.style.cssText = c.box + 'z-index:' + (subEd ? 21 : 9) + ';';
    drawSub(el);
  }
  function drawSub(el) {
    var text = el._text || (subEd ? SUB_DEMO : '');
    var on = !!S.get().englishSubs || (subEd && !play);
    el.style.display = text && on ? '' : 'none';
    var sp = el.firstChild;
    sp.setAttribute('style', (el._css && el._css.text) || '');
    sp.textContent = text;
  }
  function setSub(text) {
    var el = subsEl();
    if (el._text === text) return;
    el._text = text;
    drawSub(el);
  }
  function subSummary() { return SS.summary(subStyle()); }

  // The editor: a panel over the picture (the film itself while playing, or a bright/dark scene), the subtitle
  // shows full size underneath. Tabs on top; on a setting ◀ ▶ change it; Back closes.
  var SUB_TABS = [['presets', 'Presets']].concat(SS.GROUPS.map(function (g) { return [g[0], g[1]]; })).concat([['timing', 'Timing']]);
  function showSubMenu() {
    var back = N.current();
    subEd = { tab: subEd ? subEd.tab : 'presets', scene: S.get().subScene || 'dark' };
    var m = modal('<div class="sub-scene" id="subScene"></div><div class="sub-panel" id="subPanel">' +
      '<div class="sub-head"><b>Subtitles</b><span>Style for ' + esc(S.profileName()) + ' · ◀ ▶ change · Back closes</span></div>' +
      '<div class="sub-tabs">' + SUB_TABS.map(function (t) { return '<button class="f chip subtab" data-t="' + t[0] + '">' + t[1] + '</button>'; }).join('') + '</div>' +
      '<div class="sub-rows" id="subRows"></div></div>', closeSubMenu);
    m.classList.add('subed');
    if (play) m.classList.add('over-film');
    Array.prototype.forEach.call(m.querySelectorAll('.subtab'), function (b) {
      b.onfocusin2 = function () { if (subEd && subEd.tab !== b.getAttribute('data-t')) { subEd.tab = b.getAttribute('data-t'); subRows(false); } };
      b.onclick = function () { subRows(true); };
    });
    subRows(false);
    subsEl(); applySubStyle();
    N.focus(m.querySelector('.subtab[data-t="' + subEd.tab + '"]'));
    function closeSubMenu() {
      subEd = null;
      closeModal();
      applySubStyle();
      if ($('#stSubStyle')) $('#stSubStyle span').textContent = subSummary();
      if ($('#stSubs')) $('#stSubs span').textContent = S.get().englishSubs ? 'On' : 'Off';
      if (back && document.body.contains(back)) N.focus(back);
    }
  }
  function subRow(k, label, value, adjust) {
    return { html: '<button class="f srow" data-k="' + k + '"><b>' + esc(label) + '</b><span class="sv"><i>◀</i><em>' + esc(value) + '</em><i>▶</i></span></button>', k: k, adjust: adjust };
  }
  function subRows(focusFirst) {
    var box = $('#subRows'); if (!box || !subEd) return;
    var s = S.get(), st = subStyle(), rows = [], html = '';
    Array.prototype.forEach.call(document.querySelectorAll('.subtab'), function (b) { b.classList.toggle('on', b.getAttribute('data-t') === subEd.tab); });
    if (subEd.tab === 'presets') {
      rows.push(subRow('on', 'English subtitles', s.englishSubs ? 'On' : 'Off', function () { s.englishSubs = !s.englishSubs; S.save(); subsToggled(); }));
      if (!play) rows.push(subRow('scene', 'Preview on', subEd.scene === 'dark' ? 'A dark scene' : 'A bright scene', function () { subEd.scene = s.subScene = subEd.scene === 'dark' ? 'bright' : 'dark'; S.save(); }));
      html = rows.map(function (r) { return r.html; }).join('') + '<div class="sub-presets">' + SS.PRESETS.map(function (p) {
        var cur = SS.summary(st) === p[1];
        return '<button class="f spreset' + (cur ? ' on' : '') + '" data-p="' + p[0] + '"><b>' + esc(p[1]) + '</b><small>' + esc(p[2]) + '</small></button>';
      }).join('') + '</div>';
    } else if (subEd.tab === 'timing') {
      if (!play || play.live) html = '<p class="sub-note">Timing works while a film, episode or recording plays: press ▲ during playback and open this tab. It moves the subtitles for that film only.</p>';
      else {
        var d = play.subDelay || 0;
        rows.push(subRow('delay', 'Subtitles', d === 0 ? 'In sync' : (Math.abs(d).toFixed(2).replace(/\.?0+$/, '') + ' s ' + (d > 0 ? 'later' : 'earlier')), function (dir) {
          play.subDelay = Math.round(((play.subDelay || 0) + dir * 0.25) * 100) / 100;
          PL.subDelay(play.subDelay);
          if (play.cues) showCue(play, PL.position());
        }));
        html = rows.map(function (r) { return r.html; }).join('') + '<p class="sub-note">◀ shows them earlier, ▶ later, in steps of a quarter second. Just for this film; it starts in sync next time.</p>';
      }
    } else {
      var g = SS.GROUPS.filter(function (x) { return x[0] === subEd.tab; })[0];
      g[2].forEach(function (o) {
        rows.push(subRow(o[0], o[1], SS.label(st, o[0]), function (dir) { saveSubStyle(SS.step(subStyle(), o[0], dir)); }));
      });
      html = rows.map(function (r) { return r.html; }).join('') + '<button class="f srow sreset" data-k="reset"><b>Reset this tab</b><span class="sv"><em>OK</em></span></button>';
    }
    box.innerHTML = html;
    rows.forEach(function (r) {
      var el = box.querySelector('.srow[data-k="' + r.k + '"]');
      el._adjust = function (dir) { r.adjust(dir); refreshSub(r.k); };
      el.onclick = function () { el._adjust(1); };
    });
    Array.prototype.forEach.call(box.querySelectorAll('.spreset'), function (b) {
      b.onclick = function () {
        var p = SS.PRESETS.filter(function (x) { return x[0] === b.getAttribute('data-p'); })[0];
        saveSubStyle(SS.normalize(p[3])); refreshSub(null);
        toast(p[1] + ' style', 1500);
      };
    });
    var rs = box.querySelector('.sreset');
    if (rs) rs.onclick = function () {
      var g2 = SS.GROUPS.filter(function (x) { return x[0] === subEd.tab; })[0], cur = subStyle();
      g2[2].forEach(function (o) { cur[o[0]] = SS.DEFAULT[o[0]]; });
      saveSubStyle(cur); refreshSub(null);
    };
    subScene();
    if (focusFirst) N.focus(box.querySelector('.f'));
  }
  // after a change: redraw values and the subtitle, keep focus on the same row
  function refreshSub(k) {
    var cur = N.current(), key = cur && cur.getAttribute('data-k'), preset = cur && cur.getAttribute('data-p');
    subRows(false);
    applySubStyle();
    var again = key ? $('#subRows .srow[data-k="' + key + '"]') : preset ? $('#subRows .spreset[data-p="' + preset + '"]') : null;
    if (again) N.focus(again, { noScroll: false });
  }
  function subScene() {
    var sc = $('#subScene'), pn = $('#subPanel'); if (!sc || !subEd) return;
    sc.className = 'sub-scene ' + (play ? 'film' : subEd.scene);
    // the panel moves out of the way of the subtitle: top when subtitles sit low, bottom when they sit high
    pn.classList.toggle('low', subStyle().lift > 44);
  }
  // subtitles switched on or off while something plays
  function subsToggled() {
    applySubStyle();
    if (!play || play.live) return;
    var on = !!S.get().englishSubs;
    var had = PL.subtitles(on);
    if (on && !had && !play.cues) extSubs(play, 0);
  }
  function showCue(p, t) {
    t -= p.subDelay || 0;
    var c = p.cues, lo = 0, hi = c.length - 1, k = -1;
    while (lo <= hi) { var m = (lo + hi) >> 1; if (c[m][0] <= t) { k = m; lo = m + 1; } else hi = m - 1; }
    setSub(k >= 0 && c[k][1] > t ? c[k][2] : '');
  }
  function extSubs(p, tries) {
    var it = p.item;
    if (!MAC.link() || !it || (it.type !== 'movie' && it.type !== 'episode')) return;
    var q = it.type === 'movie' ? { kind: 'movie', id: it.id, ext: it.ext || 'mp4' }
      : { kind: 'series', id: it.id, ext: it.ext || 'mp4', series: (it.seriesItem && it.seriesItem.name) || '', season: it.season || 0, episode: it.num || 0 };
    MAC.subs(q).then(function (d) {
      if (play !== p) return;
      if (d && d.url) {
        return fetch(P.xurl(MAC.url(d.url))).then(function (r) { return r.text(); }).then(function (txt) {
          if (play !== p) return;
          var cues = parseVtt(txt);
          if (cues.length) { p.cues = cues; toast('English subtitles from your Mac', 3000); }
        });
      }
      if (d && d.pending && (tries || 0) < 40) {
        if (!tries) toast('Your Mac is making English subtitles; they appear by themselves when ready', 5000);
        p.subTimer = setTimeout(function () { if (play === p) extSubs(p, (tries || 0) + 1); }, 90000);
      }
    }).catch(function () {});
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
        (MAC.link() ? '<button class="f cat" data-id="shelves">✦ Browse</button>' : '') +
        '<button class="f cat" data-id="fav">✓ Watchlist</button>' +
        (MAC.link() ? discoverSide(kind) : '') +
        '<div id="vodGenres"></div>' +
        cats.map(function (c) { return '<button class="f cat" data-id="' + esc(c.category_id) + '">' + esc(c.category_name) + '</button>'; }).join('') +
        '</div><div class="main grid" id="vodGrid"></div></div>';
      Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) { b.onclick = function () { loadVodCat(b.getAttribute('data-id'), true); }; });
      var start = vod['cat_' + kind] || (MAC.link() ? 'shelves' : cats[0] ? String(cats[0].category_id) : 'fav');
      view.back = showHome;
      if (/^(g|shelf|d):/.test(start) && !MAC.link() || /^(g|shelf):/.test(start) || (start === 'shelves' && !MAC.link())) start = MAC.link() ? 'shelves' : cats[0] ? String(cats[0].category_id) : 'fav';
      loadVodCat(start, false);
      N.focus(screen.querySelector('.cat[data-id="' + start + '"]') || screen.querySelector('.cat'));
      vodGenres(kind);
    }).catch(function (e) { busy(false); toast(errorMessage(e)); });
  }
  // ---------- sort & filter (Discover, via Beeldbuis on the Mac; plain sorting for provider lists) ----------
  var DPICKS = {
    movie: [['gems', '💎 Hidden gems', { sort: 'gem' }], ['best', '🏆 Best rated', { sort: 'best', min: '7.5' }], ['critics', "🍅 Critics' favourites", { sort: 'rt', rt: '90' }],
            ['short', '⏱ Short & great', { sort: 'best', len: 'short', min: '7' }], ['world', '🌍 World gems', { sort: 'gem', ol: 'foreign' }],
            ['newgood', '✨ New & good', { sort: 'new', added: '30', min: '7' }], ['random', '🎲 Surprise me', { sort: 'random' }]],
    series: [['gems', '💎 Hidden gems', { sort: 'gem' }], ['best', '🏆 Best rated', { sort: 'best', min: '8' }], ['critics', "🍅 Critics' favourites", { sort: 'rt', rt: '90' }],
             ['mini', '📦 One season', { sort: 'best', seasons: '1', min: '7' }], ['quick', '⏱ Short episodes', { sort: 'best', ep: 'short', min: '7' }],
             ['world', '🌍 World gems', { sort: 'gem', ol: 'foreign' }], ['random', '🎲 Surprise me', { sort: 'random' }]]
  };
  var DCYCLE = {
    sort: [['best', 'Best rated'], ['gem', 'Hidden gems'], ['imdb', 'IMDb'], ['rt', 'Rotten Tomatoes'], ['popular', 'Popular'], ['new', 'Newly added'], ['year', 'Newest'], ['old', 'Oldest'], ['short', 'Shortest'], ['long', 'Longest'], ['az', 'A–Z'], ['random', 'Surprise me']],
    min: [['', 'Any'], ['6', '6+'], ['7', '7+'], ['7.5', '7.5+'], ['8', '8+'], ['8.5', '8.5+']],
    rt: [['', 'Any'], ['75', '75%+'], ['90', '90%+']],
    ep: [['', 'Any length'], ['short', 'Up to 30 min'], ['mid', '30–60 min'], ['long', 'Over an hour']],
    added: [['', 'Any time'], ['7', 'This week'], ['30', 'This month'], ['90', 'Last 3 months']],
    k4: [['', 'Any'], ['1', '4K only']],
    len: [['', 'Any'], ['short', '< 1h30'], ['mid', '1h30–2h'], ['long', '2h–2h30'], ['epic', '> 2h30']],
    seasons: [['', 'Any'], ['1', '1 season'], ['few', '2–4'], ['many', '5+']],
    yr: [['', 'Any'], ['last2', 'Last 2 years'], ['2020', '2020s'], ['2010', '2010s'], ['2000', '2000s'], ['1990', '1990s'], ['1980', '1980s'], ['old', 'Before 1970']],
    fame: [['', 'Anyone'], ['hidden', 'Few people'], ['middle', 'A fair crowd'], ['known', 'Everyone']],
    ol: [['', 'Any'], ['foreign', 'Not English'], ['en', 'English']],
    seen: [['', 'Hidden'], ['1', 'Shown']]
  };
  var DEXTRA = ['genre', 'xgenre', 'rt', 'ep', 'added', 'k4'];
  var DLABEL = { sort: 'Sort', min: 'Rating', len: 'Length', seasons: 'Seasons', yr: 'Year', fame: 'Known by', ol: 'Language', seen: 'Seen' };
  var LSORT = [['new', 'Newly added'], ['rating', 'Rating'], ['az', 'A–Z']];   // provider lists without the Mac
  var dState = {};
  function dF(type) { if (!dState[type]) dState[type] = { sort: 'best', min: '', len: '', seasons: '', yr: '', fame: '', ol: '', seen: '', genre: '', xgenre: '', rt: '', ep: '', added: '', k4: '', base: {} }; return dState[type]; }
  function dParams(type) {
    var f = dF(type), p = {}, y = new Date().getFullYear();
    Object.keys(f.base).forEach(function (k) { p[k] = f.base[k]; });
    ['sort', 'min', 'fame', 'ol', 'seen', 'genre', 'xgenre', 'rt', 'added', 'k4'].forEach(function (k) { if (f[k]) p[k] = f[k]; });
    if (type === 'movie' && f.len) p.len = f.len;
    if (type === 'series' && f.seasons) p.seasons = f.seasons;
    if (type === 'series' && f.ep) p.ep = f.ep;
    if (f.yr === 'last2') p.y0 = y - 1; else if (f.yr === 'old') p.y1 = 1969; else if (f.yr) { p.y0 = f.yr; p.y1 = +f.yr + 9; }
    p.p = S.get().profile || 'p1';
    return p;
  }
  function discoverSide(kind) {
    var type = kind === 'movies' ? 'movie' : 'series';
    return '<div class="side-h">Discover</div>' + DPICKS[type].map(function (pk) { return '<button class="f cat" data-id="d:' + pk[0] + '">' + esc(pk[1]) + '</button>'; }).join('');
  }
  // the bar above the grid: OK on a button steps to the next value
  function sortBar() {
    var id = vod.catId || '', type = vod.kind === 'movies' ? 'movie' : 'series';
    if (id === 'shelves') return '';
    if (/^(d|g|shelf):/.test(id)) {
      var f = dF(type), keys = ['sort', 'min', type === 'movie' ? 'len' : 'seasons', 'yr', 'fame', 'ol', 'seen'];
      return '<div class="sortbar">' + keys.map(function (k) {
        var cur = DCYCLE[k].filter(function (o) { return o[0] === f[k]; })[0] || DCYCLE[k][0];
        return '<button class="f sb' + (f[k] && !(k === 'sort' && f[k] === 'best') ? ' on' : '') + '" data-k="' + k + '" data-left="#vodCats .cat.active"><small>' + DLABEL[k] + '</small>' + esc(cur[1]) + '</button>';
      }).join('') + (function () {
        var n = DEXTRA.filter(function (k) { return f[k] && !(k === 'ep' && type === 'movie'); }).length;
        return '<button class="f sb more' + (n ? ' on' : '') + '" data-k="more" data-left="#vodCats .cat.active"><small>More</small>' + (n ? n + ' more filter' + (n > 1 ? 's' : '') : '⚙ All filters') + '</button>';
      })() + (vod.total ? '<span class="sbn">' + vod.total.toLocaleString() + ' titles</span>' : '') + '</div>';
    }
    var ls = vod.lsort || 'new', c = LSORT.filter(function (o) { return o[0] === ls; })[0];
    return '<div class="sortbar"><button class="f sb" data-k="lsort" data-left="#vodCats .cat.active"><small>Sort</small>' + esc(c[1]) + '</button></div>';
  }
  function wireSortBar(grid) {
    Array.prototype.forEach.call(grid.querySelectorAll('.sortbar .sb'), function (b) {
      b.onclick = function () {
        var k = b.getAttribute('data-k'), type = vod.kind === 'movies' ? 'movie' : 'series';
        if (k === 'more') return showFilters();
        if (k === 'lsort') {
          var i = LSORT.map(function (o) { return o[0]; }).indexOf(vod.lsort || 'new');
          vod.lsort = LSORT[(i + 1) % LSORT.length][0];
          localSort(); renderVod(false); return N.focus($('#vodGrid .sb[data-k="lsort"]'));
        }
        var f = dF(type), list = DCYCLE[k], j = list.map(function (o) { return o[0]; }).indexOf(f[k]);
        f[k] = list[(j + 1) % list.length][0];
        loadVodCat(vod.catId, false, k);
      };
    });
  }
  // all the Mac's filters, in one panel (the bar above keeps the quick ones)
  var MORE = {
    movie: ['genre', 'xgenre', 'min', 'rt', 'fame', 'yr', 'len', 'ol', 'added', 'k4', 'seen'],
    series: ['genre', 'xgenre', 'min', 'rt', 'fame', 'yr', 'seasons', 'ep', 'ol', 'added', 'k4', 'seen']
  };
  var MLABEL = { genre: 'Genre', xgenre: 'Leave out', min: 'Rating', rt: 'Rotten Tomatoes', fame: 'Known by', yr: 'Year', len: 'Length', seasons: 'Seasons',
    ep: 'Episodes', ol: 'Original language', added: 'Added', k4: 'Picture', seen: 'Seen titles' };
  function moreOptions(type, k) {
    var fc = vod.facets[type] || { genres: [], langs: [] };
    if (k === 'genre') return [['', 'Any genre']].concat((fc.genres || []).map(function (g) { return [g.name, g.name]; }));
    if (k === 'xgenre') return [['', 'Nothing']].concat((fc.genres || []).map(function (g) { return [g.name, g.name]; }));
    if (k === 'ol') return [['', 'Any'], ['en', 'English'], ['foreign', 'Not English']].concat((fc.langs || []).filter(function (l) { return l.code !== 'en'; }).map(function (l) { return [l.code, l.name]; }));
    return DCYCLE[k];
  }
  function showFilters() {
    var type = vod.kind === 'movies' ? 'movie' : 'series', f = dF(type), back = N.current();
    var html = '<h2>Sort &amp; filter</h2><div class="mf">' + ['sort'].concat(MORE[type]).map(function (k) {
      return '<div class="mf-row"><div class="mf-l">' + esc(k === 'sort' ? 'Sort by' : MLABEL[k]) + '</div><div class="mf-o">' + moreOptions(type, k).map(function (o) {
        return '<button class="f chip mfo' + ((f[k] || '') === o[0] ? ' on' : '') + '" data-k="' + k + '" data-v="' + esc(o[0]) + '">' + esc(o[1]) + '</button>';
      }).join('') + '</div></div>';
    }).join('') + '</div><div class="btns"><button class="f btn primary" id="mfDone">Show titles</button><button class="f btn" id="mfReset">Reset</button></div>';
    var m = modal(html, null);
    m.classList.add('wide');
    var changed = false;
    Array.prototype.forEach.call(m.querySelectorAll('.mfo'), function (b) {
      b.onclick = function () {
        var k = b.getAttribute('data-k'), v = b.getAttribute('data-v');
        f[k] = v; changed = true;
        Array.prototype.forEach.call(m.querySelectorAll('.mfo[data-k="' + k + '"]'), function (x) { x.classList.toggle('on', x === b); });
      };
    });
    N.focus(m.querySelector('.mfo.on') || m.querySelector('.mfo'));
    function done() { closeModal(); if (changed) loadVodCat(vod.catId, false, 'more'); else if (back && document.body.contains(back)) N.focus(back); }
    $('#mfDone').onclick = done;
    m._onBack = done;
    $('#mfReset').onclick = function () {
      Object.keys(f).forEach(function (k) { if (k !== 'base') f[k] = ''; }); f.sort = 'best'; changed = true;
      Array.prototype.forEach.call(m.querySelectorAll('.mfo'), function (x) { x.classList.toggle('on', (f[x.getAttribute('data-k')] || '') === x.getAttribute('data-v')); });
    };
  }
  function localSort() {
    var s = vod.lsort || 'new';
    vod.items.sort(s === 'rating' ? function (a, b) { return (parseFloat(b.imdb || b.rating) || 0) - (parseFloat(a.imdb || a.rating) || 0); }
      : s === 'az' ? function (a, b) { return String(a.name).localeCompare(String(b.name)); }
      : function (a, b) { return (b.added || 0) - (a.added || 0); });
  }
  function loadDiscover(id, kind, focusGrid, keepKey) {
    var type = kind === 'movies' ? 'movie' : 'series', f = dF(type);
    if (/^d:/.test(id)) {
      var pk = DPICKS[type].filter(function (x) { return 'd:' + x[0] === id; })[0];
      if (!keepKey) { ['min', 'len', 'seasons', 'yr', 'fame', 'ol', 'genre', 'xgenre', 'rt', 'ep', 'added', 'k4'].forEach(function (k) { f[k] = ''; }); f.sort = 'best'; f.base = {}; Object.keys(pk[2]).forEach(function (k) { if (k in DCYCLE) f[k] = pk[2][k]; else f.base[k] = pk[2][k]; }); }
    } else if (!keepKey) {
      f.base = /^g:/.test(id) ? { genre: id.slice(2) } : { shelf: id.slice(6) };
      f.sort = /^shelf:(gems|world)$/.test(id) ? 'gem' : 'best';
    }
    MAC.discover(type, dParams(type), 0, 300).then(function (d) {
      if (vod.catId !== id || vod.kind !== kind) return;
      vod.total = d.total || 0;
      if (d.facets) vod.facets[type] = d.facets;
      vod.items = (d.items || []).map(macTile).map(function (o) { o.rating = o.imdb; return o; });
      renderVod(focusGrid);
      if (keepKey) N.focus($('#vodGrid .sb[data-k="' + keepKey + '"]'));
    }).catch(function (e) { $('#vodGrid').innerHTML = empty('Beeldbuis on your Mac does not answer', errorMessage(e)); });
  }
  // genre list (Documentary, Animation, …) from Beeldbuis on the Mac, when linked
  function vodGenres(kind) {
    if (!MAC.link()) return;
    MAC.library(kind === 'movies' ? 'movie' : 'series', { limit: 1 }).then(function (d) {
      var box = $('#vodGenres');
      if (!box || vod.kind !== kind || !(d.genres || []).length) return;
      box.innerHTML = '<div class="side-h">Genres</div>' + d.genres.map(function (g) {
        var name = g.name || g;
        return '<button class="f cat" data-id="g:' + esc(name) + '">' + esc(name) + (g.n ? ' <small>' + g.n + '</small>' : '') + '</button>';
      }).join('') + '<div class="side-h">Provider</div>';
      Array.prototype.forEach.call(box.querySelectorAll('.cat'), function (b) { b.onclick = function () { loadVodCat(b.getAttribute('data-id'), true); }; });
    }).catch(function () {});
  }
  // shelves from the Mac: rows by theme (new, top rated, genres, decades…), each with "See all"
  function loadShelves(kind, type, focusGrid) {
    var grid = $('#vodGrid');
    MAC.shelves(type, S.get().profile).then(function (d) {
      if (vod.catId !== 'shelves' || vod.kind !== kind) return;
      home.items = home.items || {};
      var html = '';
      (d.rows || []).forEach(function (r) {
        var items = (r.items || []).map(macTile);
        if (!items.length) return;
        items.forEach(function (it) { home.items[it.key] = it; });
        html += '<section class="row"><h2>' + esc(r.title) + (r.hint ? ' <span class="hint">' + esc(r.hint) + '</span>' : '') + '</h2><div class="row-items">' +
          items.map(function (it) { return tile(it, '', it.reason || ''); }).join('') +
          (r.n > items.length ? '<button class="f tile tile-poster more" data-shelf="' + esc(r.id) + '" data-title="' + esc(r.title) + '"><div class="more-in">See all<br><small>' + r.n + '</small></div></button>' : '') +
          '</div></section>';
      });
      grid.classList.add('shelves');
      grid.innerHTML = html || empty('Nothing to show yet', 'Beeldbuis on your Mac is still fetching the library.');
      Array.prototype.forEach.call(grid.querySelectorAll('.tile'), function (b) {
        if (b.classList.contains('more')) { b.onclick = function () { openShelf(b.getAttribute('data-shelf'), b.getAttribute('data-title')); }; return; }
        b.onclick = function () { openMacItem(home.items[b.getAttribute('data-key')]); };
        b.setAttribute('data-left', '#vodCats .cat.active');
      });
      rateTiles(grid);
      if (focusGrid) N.focus(grid.querySelector('.tile'));
    }).catch(function (e) { grid.innerHTML = empty('Beeldbuis on your Mac does not answer', errorMessage(e) + '<br>Pick a provider category on the left instead.'); });
  }
  function openShelf(id, title) {
    var box = $('#vodGenres');
    if (box && !screen.querySelector('.cat[data-id="shelf:' + id + '"]')) {
      box.insertAdjacentHTML('afterbegin', '<button class="f cat" data-id="shelf:' + esc(id) + '">' + esc(title) + '</button>');
      var b = box.querySelector('.cat'); b.onclick = function () { loadVodCat(b.getAttribute('data-id'), true); };
    }
    loadVodCat('shelf:' + id, true);
  }
  function loadVodCat(id, focusGrid, keepKey) {
    var kind = vod.kind;
    if (!keepKey) vod.total = 0;
    vod['cat_' + kind] = id; vod.catId = id;
    Array.prototype.forEach.call(screen.querySelectorAll('.cat'), function (b) { b.classList.toggle('active', b.getAttribute('data-id') === id); });
    var grid = $('#vodGrid');
    grid.classList.remove('shelves');
    var type = kind === 'movies' ? 'movie' : 'series';
    if (id === 'fav') {
      var local = S.get().favorites.filter(function (f) { return f.type === type; });
      if (!MAC.link()) { vod.items = local; return renderVod(focusGrid); }
      grid.innerHTML = '<div class="loading">Loading…</div>';
      return MAC.watchKeys(S.get().profile || 'p1', type).then(function (d) {
        if (vod.catId !== 'fav') return;
        var mac = (d.items || []).map(function (t) { var o = macTile(t); o.wl = true; return o; });
        var k = {}; mac.forEach(function (o) { k[o.macKey] = 1; k[String(o.name).toLowerCase()] = 1; });
        vod.items = mac.concat(local.filter(function (it) { return !(it.macKey && k[it.macKey]) && !k[String(it.name).toLowerCase()]; }));
        renderVod(focusGrid);
      }).catch(function () { vod.items = local; renderVod(focusGrid); });
    }
    if (!keepKey) grid.innerHTML = '<div class="loading">Loading…</div>';
    if (id === 'shelves') return loadShelves(kind, type, focusGrid);
    if (/^(d|g|shelf):/.test(id)) return loadDiscover(id, kind, focusGrid, keepKey);
    if (/^shelf:/.test(id)) {
      MAC.shelves(type, S.get().profile, id.slice(6), 0, 300).then(function (d) {
        if (vod.catId !== id || vod.kind !== kind) return;
        vod.items = (d.items || []).map(macTile).map(function (o) { o.rating = o.imdb; return o; });
        renderVod(focusGrid);
      }).catch(function (e) { grid.innerHTML = empty('Beeldbuis on your Mac does not answer', errorMessage(e)); });
      return;
    }
    if (/^g:/.test(id)) {
      MAC.library(type, { genre: id.slice(2), limit: 300 }).then(function (d) {
        if (vod.catId !== id || vod.kind !== kind) return;
        vod.items = (d.items || []).map(macTile).map(function (o) { o.rating = o.imdb; return o; });
        renderVod(focusGrid);
      }).catch(function (e) { grid.innerHTML = empty('Beeldbuis on your Mac does not answer', errorMessage(e)); });
      return;
    }
    (kind === 'movies' ? X.vodStreams(id) : X.series(id)).then(function (items) {
      if (vod.catId !== id || vod.kind !== kind) return;
      vod.items = items.map(function (x) { var o = vodItem(x, type); o.cat = o.cat || String(id); return o; });
      localSort();   // newest first unless the sort button says otherwise
      renderVod(focusGrid);
    }).catch(function (e) { grid.innerHTML = empty('Could not load this list', errorMessage(e)); });
  }
  function renderVod(focusGrid) {
    var grid = $('#vodGrid');
    vod.shown = 0;
    grid.innerHTML = sortBar() + (vod.items.length ? '' : empty('Nothing here yet', vod.catId === 'fav' ? 'Press the <b class="k-yellow">yellow</b> button on a title to keep it here.' : /^d:/.test(vod.catId) ? 'Nothing matches yet. Try another filter above; more titles get ratings in the background.' : ''));
    wireSortBar(grid);
    moreVod();
    if (focusGrid) N.focus(grid.querySelector('.tile') || grid.querySelector('.sb'));
  }
  function moreVod() {
    var grid = $('#vodGrid'), from = vod.shown, to = Math.min(vod.items.length, from + 60);
    if (from >= to) return;
    var html = '';
    for (var i = from; i < to; i++) {
      var it = vod.items[i];
      html += '<button class="f tile tile-poster" data-i="' + i + '" data-left="#vodCats .cat.active">' + img(it.icon, 'poster') +
        '<div class="tile-name">' + esc(it.name) + '</div>' + (it.reason && it.macKey && /^(d|g|shelf):/.test(vod.catId || '') ? '<div class="tile-sub">' + esc(it.reason) + '</div>' : '') + (it.imdb || it.rt ? rateBadge(it.imdb, it.rt) : '') + '</button>';
    }
    grid.insertAdjacentHTML('beforeend', html);
    vod.shown = to;
    rateTiles(grid, false, vodGridItem);
    Array.prototype.forEach.call(grid.querySelectorAll('.tile'), function (b) {
      if (b._wired) return; b._wired = true;
      var i = +b.getAttribute('data-i');
      b.onclick = function () { var it = vod.items[i]; if (it.macKey) openMacItem(it); else if (it.type === 'movie') showMovie(it); else showSeries(it); };
      b.onfocusin2 = function () { if (i >= vod.shown - 12) moreVod(); };
      b._fav = function () { toggleWatch(vod.items[i]); };
    });
  }
  function stripItem(it) { return { key: it.key, type: it.type, id: it.id, name: it.name, icon: it.icon, ext: it.ext, rating: it.rating, cat: it.cat, macKey: it.macKey, imdb: it.imdb, rt: it.rt }; }

  // ---------- titles that come from Beeldbuis on the Mac ----------
  var macImg = 'https://image.tmdb.org/t/p/w342';
  function macTile(t) {
    var m = t.meta || {}, type = t.kind === 'series' ? 'series' : 'movie';
    return { key: 'mac:' + t.key, macKey: t.key, type: type, name: t.title, year: t.year || '',
      icon: m.poster ? (/^https?:/.test(m.poster) ? m.poster : macImg + m.poster) : (t.icon || ''),
      imdb: m.imdb_rating || '', rt: m.rt != null ? m.rt : '', reason: t.reason || (m.genres || []).slice(0, 2).join(', ') };
  }
  // turn a Mac title into a playable provider item (same account, so the stream ids match)
  function openMacItem(it) {
    busy(true);
    MAC.item(it.macKey).then(function (r) {
      busy(false);
      var v = (r.variants || [])[0];
      if (!v) return toast('This title is not available any more');
      var o = { key: it.type + ':' + v.id, type: it.type, id: v.id, name: it.name, icon: it.icon, ext: v.ext || 'mp4', cat: String(v.cat || ''), year: it.year,
        macKey: it.macKey, imdb: it.imdb, rt: it.rt, fb: (r.feedback || {})[S.get().profile || 'p1'] || '',
        wl: (S.get().profile === 'both' ? Object.keys(r.watchlist || {}).length > 0 : !!(r.watchlist || {})[S.get().profile || 'p1']) };
      if (it.type === 'movie') showMovie(o); else showSeries(o);
    }).catch(function (e) { busy(false); toast('Beeldbuis on your Mac does not answer (' + errorMessage(e) + ')'); });
  }

  // Taste buttons for titles the Mac knows: the same 👍 / 👎 / Seen / Watchlist as on the Mac, per profile.
  var VERDICTS = [['up', 'More like this'], ['down', 'Not for me'], ['seen', 'Seen it']];
  var VERDICT_MSG = { up: 'More like this, noted', down: 'Not for you, noted. It will not come back.', seen: 'Marked as seen', clear: 'Cleared' };
  function verdictHtml(it) {
    if (!it.macKey || !MAC.link()) return '';
    return '<div class="btns verdicts" id="verdicts">' + VERDICTS.map(function (v) {
      return '<button class="f btn verdict' + (it.fb === v[0] ? ' on' : '') + '" data-v="' + v[0] + '">' + (it.fb === v[0] ? '✓ ' : '') + v[1] + '</button>';
    }).join('') + '</div>';
  }
  function wireVerdicts(it) {
    var box = $('#verdicts');
    if (!box) return;
    Array.prototype.forEach.call(box.querySelectorAll('.verdict'), function (b) {
      b.onclick = function () {
        var v = b.getAttribute('data-v'), send = it.fb === v ? 'clear' : v;
        MAC.feedback(it.macKey, send, S.get().profile || 'p1').then(function () {
          it.fb = send === 'clear' ? '' : send;
          Array.prototype.forEach.call(box.querySelectorAll('.verdict'), function (x) {
            var on = x.getAttribute('data-v') === it.fb;
            x.classList.toggle('on', on);
            x.textContent = (on ? '✓ ' : '') + VERDICTS.filter(function (d) { return d[0] === x.getAttribute('data-v'); })[0][1];
          });
          toast(VERDICT_MSG[send]);
        }).catch(function (e) { toast('Beeldbuis on your Mac does not answer (' + errorMessage(e) + ')'); });
      };
    });
  }

  function addRatings(sel, it, year) {
    function put(l) { var el = $(sel); if (l && el && !el.querySelector('.ratings')) el.insertAdjacentHTML('beforeend', '<span class="ratings">' + esc(l) + '</span>'); }
    function viaOmdb() { OM.ratings(it.name, it.type, year).then(function (v) { if (v) put(rateLabel(v.imdb, v.rt)); }); }
    if (it.imdb || (it.rt !== '' && it.rt != null)) return put(rateLabel(it.imdb, it.rt));
    if (!MAC.link()) return viaOmdb();
    var k = it.macKey, id = provId(it);
    MAC.ratings(k ? [k] : [], k ? [] : [id]).then(function (d) {
      var r = d[k || id];
      if (r && (r.imdb_rating || r.rt != null)) { it.imdb = r.imdb_rating || ''; it.rt = r.rt != null ? r.rt : ''; put(rateLabel(it.imdb, it.rt) + (r.mc != null ? ' · Metacritic ' + r.mc : '')); }
      else viaOmdb();
    }).catch(viaOmdb);
  }
  // "01:52:00" -> "1 h 52 min"; providers sometimes send nonsense like "00:01:52" for a feature film: leave that out
  function niceDuration(secs, txt) {
    var t = parseInt(secs, 10);
    if (!(t > 0) && txt) { var m = String(txt).match(/^(\d+):(\d\d)(?::(\d\d))?$/); if (m) t = (+m[1]) * 3600 + (+m[2]) * 60 + (+(m[3] || 0)); }
    if (!(t >= 600)) return '';
    var h = Math.floor(t / 3600), mi = Math.round((t % 3600) / 60);
    return (h ? h + ' h ' : '') + mi + ' min';
  }
  function showMovie(it) {
    var pos = S.get().positions[it.key];
    var m = modal('<div class="detail">' + img(it.icon, 'poster') + '<div><h2>' + esc(it.name) + '</h2><p class="meta" id="mMeta">' + (it.rating ? '★ ' + esc(it.rating) : '') + '</p>' +
      '<p class="plot" id="mPlot"></p><div class="btns">' +
      (pos ? '<button class="f btn primary autofocus" id="mResume">Resume at ' + dur(pos.t) + '</button>' : '') +
      '<button class="f btn' + (pos ? '' : ' primary autofocus') + '" id="mPlay">' + (pos ? 'From the start' : 'Play') + '</button>' +
      '<button class="f btn' + (inWatchlist(it) ? ' on' : '') + '" id="mFav">' + wlLabel(inWatchlist(it)) + '</button></div>' + verdictHtml(it) + '</div></div>');
    X.vodInfo(it.id).then(function (r) {
      var i = (r && r.info) || {};
      if (!$('#mPlot')) return;
      $('#mPlot').textContent = i.plot || i.description || '';
      $('#mMeta').textContent = [i.releasedate ? String(i.releasedate).slice(0, 4) : '', i.genre, niceDuration(i.duration_secs, i.duration), it.imdb || it.rt ? '' : it.rating ? '★ ' + it.rating : ''].filter(Boolean).join(' · ');
      if (r && r.movie_data && r.movie_data.container_extension) it.ext = r.movie_data.container_extension;
      if (r && r.movie_data && r.movie_data.category_id && !it.cat) it.cat = String(r.movie_data.category_id);
      addRatings('#mMeta', it, i.releasedate ? String(i.releasedate).slice(0, 4) : '');
    }).catch(function () {});
    if (pos) $('#mResume').onclick = function () { closeModal(); playVod(it, pos.t); };
    $('#mPlay').onclick = function () { closeModal(); playVod(it, 0); };
    $('#mFav').onclick = function () { toggleWatch(it, function (on) { var b = $('#mFav'); if (b) { b.textContent = wlLabel(on); b.classList.toggle('on', on); } }); };
    wireVerdicts(it);
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
        '<p class="meta" id="sMeta">' + esc([info.releaseDate ? String(info.releaseDate).slice(0, 4) : '', info.genre, it.rating ? '★ ' + it.rating : ''].filter(Boolean).join(' · ')) + '</p>' +
        '<p class="plot">' + esc(info.plot || it.plot || '') + '</p>' +
        '<button class="f btn' + (inWatchlist(it) ? ' on' : '') + '" id="sFav">' + wlLabel(inWatchlist(it)) + '</button>' + verdictHtml(it) + '</div></div>' +
        '<div class="seasons">' + seasons.map(function (s) { return '<button class="f chip season" data-s="' + esc(s) + '">Season ' + esc(s) + '</button>'; }).join('') + '</div>' +
        '<div class="episodes" id="eps"></div></div>';
      if (info.category_id && !it.cat) it.cat = String(info.category_id);
      addRatings('#sMeta', it, info.releaseDate ? String(info.releaseDate).slice(0, 4) : '');
      $('#sFav').onclick = function () { toggleWatch(it, function (on) { var b = $('#sFav'); if (b) { b.textContent = wlLabel(on); b.classList.toggle('on', on); } }); };
      wireVerdicts(it);
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
              name: it.name + ' · S' + s + 'E' + e.episode_num, num: e.episode_num, seriesItem: stripItem(it), season: s, episodes: list.map(function (x) { return { id: x.id, num: x.episode_num, title: x.title, ext: x.container_extension }; }) };
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
        if (view.tab === 'search') showSearch();
        else if (prevHtml && (vod.kind === 'movies' || vod.kind === 'series') && view.tab !== 'home') { showVod(vod.kind); }
        else if (typeof prevBack === 'function') showHome(); else showHome();
      };
      N.focus(screen.querySelector('.season.on'));
    }).catch(function (e) { busy(false); toast(errorMessage(e)); });
  }

  // ---------- search ----------
  var search = { q: '', timer: null };
  function showSearch() {
    showTop(true); setTab('search');
    screen.innerHTML = '<div class="page search">' +
      '<div class="search-bar"><input class="f" id="sq" type="text" placeholder="' + (MAC.link() ? 'Search programmes, channels, movies and series' : 'Search channels, movies and series') + '" autocomplete="off" autocorrect="off" spellcheck="false"></div>' +
      '<div id="sStatus" class="search-status"></div><div id="sResults" class="rows"></div></div>';
    var input = $('#sq');
    input.value = search.q;
    input.oninput = function () { clearTimeout(search.timer); search.timer = setTimeout(runSearch, 400); };
    input._done = function () { runSearch(); var first = $('#sResults .f'); if (first) N.focus(first); };
    view.back = showHome;
    N.focus(input);
    if (search.q) runSearch();
    else $('#sStatus').innerHTML = '<p>Press OK to type. Results show channels from your groups, and all movies and series.</p>';
  }
  function runSearch() {
    var input = $('#sq');
    if (!input) return;
    var q = input.value.trim();
    search.q = q;
    if (SR.norm(q).length < 2) { $('#sResults').innerHTML = ''; $('#sStatus').innerHTML = q ? '<p>Type at least two letters.</p>' : ''; return; }
    var kinds = ['live', 'movies', 'series'], labels = { live: 'channels', movies: 'movies', series: 'series' }, prog = {};
    function status() {
      var waiting = kinds.filter(function (k) { return !SR.ready(k); });
      $('#sStatus').innerHTML = waiting.length ? '<p class="dim">Getting the lists ready (only the first time): ' + waiting.map(function (k) {
        return labels[k] + (prog[k] ? ' ' + Math.round(100 * prog[k][0] / prog[k][1]) + '%' : '');
      }).join(' · ') + '</p>' : '';
    }
    function render() {
      var box = $('#sResults'); if (!box) return;
      var had = N.current() && box.contains(N.current());
      var ch = SR.find('live', q, 30).map(liveItem);
      var mv = SR.find('movies', q, 40).map(function (x) { return vodItem(x, 'movie'); });
      var se = SR.find('series', q, 40).map(function (x) { return vodItem(x, 'series'); });
      var all = {};
      ch.concat(mv, se).forEach(function (it) { all[it.key] = it; });
      var html = (search.progs && search.progs.q === q ? progRow(search.progs.items) : '') + row('Channels', ch) + row('Movies', mv) + row('Series', se);
      var ready = kinds.every(function (k) { return SR.ready(k); });
      box.innerHTML = html || (ready ? empty('Nothing found', 'Try fewer or other words.') : '');
      wireProgRow(box);
      Array.prototype.forEach.call(box.querySelectorAll('.tile:not(.gs)'), function (b) {
        var it = all[b.getAttribute('data-key')];
        b.onclick = function () {
          if (it.type === 'live') { var gs = ch.map(function (c) { return c.group; }); playLive(it.group, gs, gs.indexOf(it.group)); }
          else if (it.type === 'movie') showMovie(it); else showSeries(it);
        };
        b._fav = function () { if (it.type !== 'live') return toggleWatch(it); favToast(toggleLiveFav(it.group)); };
      });
      if (had) { var f = box.querySelector('.f'); N.focus(f || $('#sq')); }
    }
    status(); render();
    if (MAC.link() && !(search.progs && search.progs.q === q)) {
      MAC.guideSearch(q).then(function (items) {
        if (search.q !== q || !$('#sq')) return;
        search.progs = { q: q, items: items || [] };
        render();
      }).catch(function () {});
    }
    kinds.forEach(function (k) {
      if (SR.ready(k)) return;
      SR.build(k, function (d, n) { prog[k] = [d, n]; if ($('#sq')) status(); }).then(function () {
        if ($('#sq') && search.q === q) { status(); render(); }
      }, function (e) { if ($('#sq')) $('#sStatus').innerHTML = '<p class="err">Could not load ' + labels[k] + ': ' + errorMessage(e) + '</p>'; });
    });
  }
  // "On TV": programmes in the Mac's guide (now, later, and catch-up), OK opens watch / record
  function progRow(items) {
    if (!items || !items.length) return '';
    var now = Date.now() / 1000;
    return '<section class="row"><h2>On TV <span class="hint">From the guide · OK to watch or record</span></h2><div class="row-items">' + items.map(function (x, i) {
      var day = Math.floor((x.start - dayStart(0)) / 86400), when = x.start <= now && x.stop > now ? 'Now' : dayLabel(day) + ' ' + hhmm(x.start);
      return '<button class="f tile tile-live gs has-sub' + (x.past ? ' past' : '') + '" data-gi="' + i + '"><div class="logo">' + img(x.group.icon) + '</div><div class="tile-name">' + esc(x.title) + '</div>' +
        '<div class="tile-sub">' + esc(when + ' · ' + x.group.name + (x.past ? ' · catch-up' : '')) + '</div></button>';
    }).join('') + '</div></section>';
  }
  function wireProgRow(box) {
    var items = (search.progs && search.progs.items) || [];
    Array.prototype.forEach.call(box.querySelectorAll('.tile.gs'), function (b) {
      var x = items[+b.getAttribute('data-gi')]; if (!x) return;
      b.onclick = function () { guideAction(x.group, x); };
      b._fav = function () { favToast(toggleLiveFav(x.group)); };
    });
  }
  function vodItem(x, type) {
    var o = { key: type + ':' + x.id, type: type };
    Object.keys(x).forEach(function (k) { o[k] = x[k]; });
    o.name = title(o.name);
    return o;
  }

  // ---------- settings ----------
  function startLabel() { return S.get().startScreen === 'calm' ? 'Calm home (clock, consoles and apps)' : 'Tonight'; }
  function saverLabel() { var m = saverMinutes(); return m ? 'After ' + m + ' minutes · your photos and tonight\'s picks' : 'Off'; }
  function showSettings() {
    showTop(true); setTab('settings');
    var s = S.get();
    screen.innerHTML = '<div class="page settings">' +
      '<button class="f setting" id="stCountries"><b>Channel groups</b><span>' + esc(s.countries.join(', ') || 'none') + '</span></button>' +
      '<button class="f setting" id="stSubs"><b>English subtitles</b><span>' + (s.englishSubs ? 'On' : 'Off') + '</span></button>' +
      '<button class="f setting" id="stSubStyle"><b>Subtitle style</b><span>' + esc(subSummary()) + '</span></button>' +
      '<button class="f setting" id="stQuality"><b>Picture quality</b><span>' + (s.quality === 'best' ? 'Best available (4K first)' : 'Full HD first (steadier)') + '</span></button>' +
      '<button class="f setting" id="stAdult"><b>Adult categories</b><span>' + (s.showAdult ? 'Shown' : 'Hidden') + '</span></button>' +
      '<button class="f setting" id="stProfiles"><b>Profiles</b><span>' + S.PROFILE_IDS.map(function (id) { return esc(S.profileName(id)); }).join(', ') + '</span></button>' +
      '<button class="f setting" id="stStart"><b>Start screen</b><span>' + startLabel() + '</span></button>' +
      '<button class="f setting" id="stSaver"><b>Screensaver</b><span>' + saverLabel() + '</span></button>' +
      '<button class="f setting" id="stMac"><b>Beeldbuis on your Mac</b><span>' + (s.mac ? 'Linked · ' + esc(s.mac.base.replace(/^https?:\/\//, '')) : 'Not linked · For you, Letterboxd and genres') + '</span></button>' +
      '<button class="f setting" id="stOmdb"><b>IMDb ratings</b><span>' + (s.omdbKey ? 'On (OMDb key set)' : 'Off · add a free OMDb key') + '</span></button>' +
      '<button class="f setting" id="stRefresh"><b>Refresh lists</b><span>Fetch channels, movies and series again</span></button>' +
      '<button class="f setting" id="stAccount"><b>Account</b><span>' + esc(s.account ? s.account.username + ' @ ' + s.account.server.replace(/^https?:\/\//, '') : '') + '</span></button>' +
      '<button class="f setting" id="stLogout"><b>Sign out</b><span>Removes your login from this TV</span></button>' +
      '<p class="about">Beeldbuis ' + VERSION + ' · ' + esc(P.name) + '</p></div>';
    $('#stCountries').onclick = function () { showCountries(false); };
    $('#stSubs').onclick = function () { s.englishSubs = !s.englishSubs; S.save(); $('#stSubs span').textContent = s.englishSubs ? 'On' : 'Off'; };
    $('#stSubStyle').onclick = showSubMenu;
    $('#stStart').onclick = function () { s.startScreen = s.startScreen === 'calm' ? 'tonight' : 'calm'; S.save(); $('#stStart span').textContent = startLabel(); };
    $('#stSaver').onclick = function () { var o = [0, 2, 5, 10, 20], i = o.indexOf(saverMinutes()); s.saverMin = o[(i + 1) % o.length]; S.save(); $('#stSaver span').textContent = saverLabel(); saverArm(); };
    $('#stQuality').onclick = function () { s.quality = s.quality === 'best' ? 'fhd' : 'best'; S.save(); $('#stQuality span').textContent = s.quality === 'best' ? 'Best available (4K first)' : 'Full HD first (steadier)'; };
    $('#stAdult').onclick = function () { s.showAdult = !s.showAdult; S.save(); SR.reset(); live.catId = null; vod.catId = null; $('#stAdult span').textContent = s.showAdult ? 'Shown' : 'Hidden'; };
    $('#stProfiles').onclick = function () {
      modal('<h2>Profile names</h2><p>Each profile has its own favourites, history and “For you”.</p>' +
        S.PROFILE_IDS.map(function (id) { return '<label class="field">' + esc({ p1: 'Profile 1', p2: 'Profile 2', both: 'Together' }[id]) + '<input class="f" id="pn_' + id + '" value="' + esc(S.profileName(id)) + '"></label>'; }).join('') +
        '<div class="btns"><button class="f btn primary" id="mSave">Save</button><button class="f btn" id="mNo">Cancel</button></div>');
      $('#mSave').onclick = function () { S.PROFILE_IDS.forEach(function (id) { S.setProfileName(id, $('#pn_' + id).value); }); closeModal(); showSettings(); };
      $('#mNo').onclick = closeModal;
    };
    $('#stMac').onclick = function () {
      if (s.mac) {
        modal('<h2>Beeldbuis on your Mac</h2><p>This TV is linked to ' + esc(s.mac.base) + '.</p><div class="btns"><button class="f btn" id="mUnlink">Unlink</button><button class="f btn primary autofocus" id="mNo">Close</button></div>');
        $('#mUnlink').onclick = function () { MAC.unlink(); closeModal(); showSettings(); };
        $('#mNo').onclick = closeModal;
        return;
      }
      modal('<h2>Link with Beeldbuis on your Mac</h2><p>On the Mac, open Beeldbuis → Settings → iPhone &amp; iPad. Type the address and the code it shows.</p>' +
        '<label class="field">Address of the Mac<input class="f" id="macAddr" placeholder="192.168.1.20:8765" autocomplete="off" spellcheck="false"></label>' +
        '<label class="field">Code<input class="f" id="macCode" autocomplete="off" spellcheck="false"></label>' +
        '<p class="err" id="macMsg"></p><div class="btns"><button class="f btn primary" id="mPair">Link</button><button class="f btn" id="mNo">Cancel</button></div>');
      $('#mPair').onclick = function () {
        $('#macMsg').textContent = 'Linking…';
        MAC.pair($('#macAddr').value, $('#macCode').value).then(function () { closeModal(); showSettings(); toast('Linked. Tonight now uses your taste and Letterboxd.'); },
          function (e) { $('#macMsg').textContent = (e && e.message) || String(e); });
      };
      $('#mNo').onclick = closeModal;
    };
    $('#stOmdb').onclick = function () {
      modal('<h2>IMDb ratings</h2><p>Get a free key at omdbapi.com (choose FREE, it arrives by e-mail) and type it here. It stays on this TV.</p>' +
        '<label class="field">OMDb key<input class="f" id="omKey" value="' + esc(s.omdbKey || '') + '" autocomplete="off" spellcheck="false"></label>' +
        '<div class="btns"><button class="f btn primary" id="mSave">Save</button><button class="f btn" id="mNo">Cancel</button></div>');
      $('#mSave').onclick = function () { s.omdbKey = $('#omKey').value.trim(); S.save(); closeModal(); showSettings(); toast(s.omdbKey ? 'Ratings will appear on movies and series' : 'Ratings off'); };
      $('#mNo').onclick = closeModal;
    };
    $('#stRefresh').onclick = function () { S.cacheClear(); SR.reset(); toast('Lists will be fetched again'); };
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
    if (!opts.live) { saveTimer = setInterval(savePos, 15000); if (item.type === 'movie' || item.type === 'episode') vodWatch(play); }
    if (root.BBRemote) root.BBRemote.now({ name: item.name, kind: item.type });
  }
  function savePos() {
    if (!play || play.live) return;
    var t = PL.position(), d = PL.duration();
    if (play.offset && t > 0) { t += play.offset; d = d ? d + play.offset : d; }
    if (t > 0) { S.setPosition(play.item, t, d); pushPos(play.item, t, d); }
  }
  function stopPlayback() {
    if (!play) return;
    if (play.mode === 'mac' || play.mode === 'macvod') macStop();
    if (side.on) sideSet(0, true);
    side.data = null; clearTimeout(okHold.timer); okHold.down = 0;
    clearTimeout(play.seekTimer); clearTimeout(play.subTimer); clearTimeout(play.watch);
    savePos();
    clearInterval(saveTimer);
    PL.stop();
    document.body.classList.remove('playing');
    $('#osd').classList.add('hidden');
    setSub('');
    play = null;
    if (root.BBRemote) root.BBRemote.now({});
  }
  function leavePlayer() {
    var p = play;
    stopPlayback();
    if (p && p.live) { /* back to the channel list we came from */ }
    if (view.tab === 'home') showHome();
    else if (p && p.returnTo.focus && document.body.contains(p.returnTo.focus)) N.focus(p.returnTo.focus);
    else N.first(screen);
  }

  // which versions of a channel to try, in order. Full HD first (steadier than 4K/RAW on most lines), unless "Best available" is set.
  var PREF = { FHD: 0, HD: 1, '': 1, SD: 2, '4K': 3, RAW: 4, '8K': 5 };
  function order(g) {
    var mode = S.get().quality;
    if (g._order && g._orderMode === mode) return g._order;
    g._orderMode = mode;
    g._order = mode === 'best' ? g.variants.slice() : g.variants.slice().sort(function (a, b) {
      var pa = PREF[a.q] !== undefined ? PREF[a.q] : 1, pb = PREF[b.q] !== undefined ? PREF[b.q] : 1;
      return (pa - pb) || (a._i - b._i);
    });
    return g._order;
  }
  // Live TV. When the TV is linked to the Mac, the Mac fetches the channel (still one connection) and keeps the last
  // minutes on disk; the TV plays that buffer as one stream, so it can pause, rewind and go back to live. Without the
  // Mac the TV plays the provider directly and uses the provider's catch-up for pause and rewind (catch-up channels).
  var liveToken = 0, macStopping = Promise.resolve();
  function macStop() { macStopping = MAC.get('/api/stop', {}, 4000).catch(function () {}); return macStopping; }
  function macLive(id) {
    return macStopping.then(function () {
      return MAC.get('/api/play', { kind: 'live', id: id, ext: 'ts', t: 0 }, 30000);
    }).then(function (d) { return String(d.playlist || '').split('/')[2]; });
  }
  function bufUrl(sid, behind) { return MAC.link().base + '/hls/' + sid + '/buffer.ts?behind=' + Math.max(0, Math.round(behind)); }
  function playLive(g, list, index, variantIdx, opts) {
    if (!g || !g.variants || !g.variants.length) return;
    opts = opts || {};
    variantIdx = variantIdx || 0;
    var v = order(g)[variantIdx];
    if (play) { PL.stop(); }
    var item = liveItem(g), token = ++liveToken;
    S.addRecent(item);
    S.get().lastChannel = item; S.save();
    function begin(url, mode, sid) {
      busy(false);
      if (play) PL.stop();
      startPlayback(url, item, { live: true });
      play.list = list; play.index = index; play.variant = variantIdx; play.group = g;
      play.mode = mode; play.sid = sid || null; play.behind = 0; play.pausedAt = 0;
      osdLive();
      if (side.on) { side.data = null; sideSet(side.on, true); } else if (!opts.catchup) sideOffer(play);
      if (MAC.link() && !playLive.hinted) { playLive.hinted = true; setTimeout(function () { toast('<b>OK</b> pause · <b>◀</b> 15 s back · <b>▶</b> 30 s on · <b class="k-blue">Blue</b> from the start · <b class="k-red">Red</b> records on your Mac', 6000); }, 2500); }
    }
    if (opts.catchup) {                    // a programme from the guide that is on now or already over: the provider's catch-up
      var av = null, vs = order(g), now = Date.now() / 1000;
      for (var ai = 0; ai < vs.length && !av; ai++) if (vs[ai].archive) av = vs[ai];
      if (!av) { noRewind(); return; }
      var cs = Math.max(now - av.archive * 86400 + 120, Math.floor(opts.catchup / 60) * 60), mins = Math.ceil((now - cs) / 60) + 240;
      busy(true, 'Opening catch-up…');
      recGate().then(function (r) {
        if (r) { busy(false); recBlocks(r); throw 'rec'; }
        return MAC.link() ? macStop() : null;
      }).then(function () {
        if (token !== liveToken) return;
        begin(X.timeshiftUrl(av.id, cs, mins), 'cu');
        play.behind = now - cs; play.started = Date.now(); liveOsd();
      }).catch(function () {});
      return;
    }
    if (MAC.link() && !opts.direct) {
      busy(true, 'Tuning in to ' + esc(g.name) + '…');
      macLive(v.id).then(function (sid) {
        if (token !== liveToken) return;
        if (!sid) throw new Error('No stream');
        begin(bufUrl(sid, 0), 'mac', sid);
      }).catch(function (e) {
        if (token !== liveToken) return;
        busy(false);
        if (/one connection/i.test(e.message || '')) { failPlayback(esc(e.message) + ''); return; }
        begin(X.liveUrl(v.id), 'direct');       // the Mac is off or busy: straight from the provider
      });
      return;
    }
    begin(X.liveUrl(v.id), 'direct');
  }
  // ---------- pause and rewind live TV ----------
  function archiveVariant() {
    if (!play || !play.group) return null;
    var vs = order(play.group), cur = vs[play.variant || 0];
    if (cur && cur.archive) return cur;
    for (var i = 0; i < vs.length; i++) if (vs[i].archive) return vs[i];
    return null;
  }
  function liveBehind() {
    if (!play || !play.live) return 0;
    var b = play.seekTarget !== undefined ? play.seekTarget : (play.behind || 0);
    if (play.pausedAt && play.seekTarget === undefined) b += (Date.now() - play.pausedAt) / 1000;
    return Math.max(0, b);
  }
  function liveOsd() {
    if (!play || !play.live || play.item.type === 'rec') return;
    var b = liveBehind(), paused = !!play.pausedAt;
    if (side.on && side.data && (b > 45 || play.mode === 'cu' || paused) !== side.late) sideDraw();
    var where = b < 15 && !paused ? '● Live' : '−' + dur(b) + (play.mode === 'cu' ? ' (catch-up)' : '') + (paused ? ' · Paused' : '');
    var can = play.mode !== 'direct' || archiveVariant();
    $('#osdTime').innerHTML = esc(where) + (can ? '<span class="osd-keys">◀ 15 s back · ▶ 30 s on · OK pause' + (b >= 15 ? ' · ▶▶ back to live' : '') + ' · <b class="k-blue">Blue</b> from the start</span>' : '');
  }
  function noRewind() {
    toast('Rewinding this channel needs Beeldbuis on your Mac (Settings → Beeldbuis on your Mac), or a channel with catch-up', 6000);
  }
  function livePause(want) {
    if (!play || !play.live) return;
    var paused = !!play.pausedAt;
    if (want === undefined) want = !paused;
    if (want === paused) return;
    if (want) { PL.setPaused(true); play.pausedAt = Date.now(); liveOsd(); return; }
    var away = (Date.now() - play.pausedAt) / 1000, b = liveBehind();
    play.pausedAt = 0;
    if (away < 20) { play.behind = b; PL.setPaused(false); liveOsd(); return; }
    // a long pause: carry on exactly where we stopped (a paused network stream is not to be trusted to resume)
    if (play.mode === 'direct' && !archiveVariant()) { askLive(play, 'You paused for ' + dur(away) + '. This channel can only continue live.'); return; }
    jumpTo(b);
  }
  function seekStep(delta) {
    if (!play || !play.live) return;
    if (play.mode === 'direct' && !archiveVariant()) { noRewind(); return; }
    if (play.mode !== 'mac' && Math.abs(delta) < 60) delta = delta < 0 ? -60 : 60;    // catch-up works in whole minutes
    play.seekTarget = Math.max(0, liveBehind() - delta);
    liveOsd(); showOsd();
    clearTimeout(play.seekTimer);
    play.seekTimer = setTimeout(function () { var t = play && play.seekTarget; if (t === undefined) return; play.seekTarget = undefined; jumpTo(t); }, 800);
  }
  function goLive() {
    if (!play || !play.live) return;
    if (play.mode === 'mac') { jumpTo(0); return; }
    if (play.mode === 'cu') { playLive(play.group, play.list, play.index, play.variant); return; }
  }
  function jumpTo(behind) {
    if (!play || !play.live) return;
    var p = play;
    behind = Math.max(0, behind);
    p.pausedAt = 0;
    if (p.mode === 'mac') {
      if (behind < 8) { p.behind = 0; PL.open(bufUrl(p.sid, 0), { live: true }); liveOsd(); return; }
      MAC.get('/api/buffer', { sid: p.sid }, 5000).then(function (b) {
        if (play !== p) return;
        var have = (b && b.length) || 0;
        if (behind > have - 4) {
          if (archiveVariant()) { openCatchup(Date.now() / 1000 - behind); return; }
          behind = Math.max(0, have - 4);
          toast('That is as far back as the buffer goes (' + dur(have) + ')');
        }
        p.behind = behind; PL.open(bufUrl(p.sid, behind), { live: true }); liveOsd();
      }).catch(function () { if (play !== p) return; if (behind >= 15) stayBehind(p, 'The Mac does not answer'); else playLive(p.group, p.list, p.index, p.variant); });
      return;
    }
    if (behind < 45) { playLive(p.group, p.list, p.index, p.variant); return; }   // catch-up or direct: back to live
    openCatchup(Date.now() / 1000 - behind);
  }
  // ---- live sport without spoilers: never land on the live edge by itself while the viewer is behind live.
  // Pick up at the same moment (Mac buffer or catch-up); when that is impossible, stop and ask.
  function stayBehind(p, why) {
    if (play !== p) return;
    var behind = liveBehind();
    if (behind < 15) { playLive(p.group, p.list, p.index, p.variant, p.mode === 'mac' ? {} : { direct: p.mode === 'direct' }); return; }
    if (archiveVariant()) { toast(esc(why) + ' · carrying on where you were via catch-up', 5000); openCatchup(Date.now() / 1000 - behind); return; }
    askLive(p, why + '. You were ' + dur(behind) + ' behind live and this channel has no catch-up.');
  }
  function askLive(p, msg) {
    var g = p.group, l = p.list, i = p.index, v = p.variant;
    stopPlayback();
    modal('<img class="modal-mascot" src="img/icon-192.png" alt=""><h2>The stream stopped</h2><p>' + esc(msg) + '</p><p class="hint">Going live shows what is happening now (possible spoilers).</p>' +
      '<div class="btns"><button class="f btn primary autofocus" id="mLive">Go to live</button><button class="f btn" id="mStop">Stop</button></div>', null);
    $('#mLive').onclick = function () { closeModal(); playLive(g, l, i, v); };
    $('#mStop').onclick = function () { closeModal(); leavePlayer(); };
  }
  function cuTrouble(p, what) {
    if (play !== p) return;
    var behind = liveBehind();
    if (behind < 20) { playLive(p.group, p.list, p.index, p.variant); return; }   // caught up with live anyway
    if (p.started && Date.now() - p.started > 120000) p.cuRetry = 0;     // it played fine for a while: fresh tries
    p.cuRetry = (p.cuRetry || 0) + 1;
    if (p.cuRetry <= 2) { toast('Catch-up ' + what + ' · reopening at the same moment', 3000); openCatchup(Date.now() / 1000 - behind); return; }
    askLive(p, 'Catch-up ' + what + ' a few times at −' + dur(behind) + '.');
  }
  // the stream from the Mac broke off: still there (hiccup) → pick up again; stopped (another device or a recording
  // took the one connection) → leave it, never grab it back; Mac gone → straight from the provider
  function macLost() {
    var p = play;
    if (!p || p.macLost) return;
    p.macLost = true;
    MAC.get('/api/buffer', { sid: p.sid }, 5000).then(function () {
      if (play !== p) return;
      p.macLost = false;
      if (p.macRetries = (p.macRetries || 0) + 1, p.macRetries <= 3) jumpTo(liveBehind());
      else if (liveBehind() >= 15) stayBehind(p, 'The Mac buffer keeps stopping');
      else { toast('The Mac buffer keeps stopping · playing straight from the provider'); playLive(p.group, p.list, p.index, p.variant, { direct: true }); }
    }).catch(function (e) {
      if (play !== p) return;
      if (/stopped/i.test(e.message || '')) {
        p.mode = 'gone'; leavePlayer();
        toast('Live TV stopped on the TV: the subscription’s one connection is now used elsewhere (the Mac, the iPhone or a recording)', 8000);
        return;
      }
      if (liveBehind() >= 15) { stayBehind(p, 'The Mac does not answer'); return; }
      toast('The Mac does not answer · playing straight from the provider');
      playLive(p.group, p.list, p.index, p.variant, { direct: true });
    });
  }
  // play the provider's catch-up from a moment in the past, running on into live
  function openCatchup(start) {
    var v = archiveVariant(), p = play;
    if (!v || !p) { noRewind(); return; }
    var now = Date.now() / 1000, maxBack = (v.archive || 1) * 86400;
    start = Math.max(now - maxBack + 120, Math.floor(start / 60) * 60);
    var minutes = Math.ceil((now - start) / 60) + 240;
    var go = function () {
      if (play !== p) return;
      return recGate().then(function (r) {
        if (play !== p) return;
        if (r) { stopPlayback(); recBlocks(r); return; }
        openCu();
      });
    };
    var openCu = function () {
      PL.open(X.timeshiftUrl(v.id, start, minutes), { live: true });
      p.mode = 'cu'; p.sid = null; p.behind = now - start; p.pausedAt = 0; p.started = Date.now();
      liveOsd(); showOsd();
    };
    if (p.mode === 'mac') { PL.stop(); macStop().then(go); } else go();      // one connection: the Mac lets go first
  }
  function fromStart() {
    if (!play || !play.live || !play.group) return;
    var p = play, g = p.group;
    chanEpg(g, order(g)[p.variant || 0]).then(function (ps) {
      if (play !== p) return;
      var now = Date.now() / 1000, cur = ps.filter(function (x) { return x.start <= now && x.end > now; })[0];
      if (!cur) { toast('No programme information for this channel'); return; }
      var back = now - cur.start + 30;
      toast('↺ From the start: ' + esc(cur.title));
      if (p.mode === 'mac') { jumpTo(back); return; }
      if (!archiveVariant()) { noRewind(); return; }
      openCatchup(cur.start);
    });
  }
  function zap(step) {
    if (!play || !play.live || !play.list || play.list.length < 2) return;
    var i = (play.index + step + play.list.length) % play.list.length;
    playLive(play.list[i], play.list, i);
  }
  function osdLive() {
    var g = play.group, v = order(g)[play.variant];
    $('#osdTitle').textContent = g.name;
    $('#osdSub').textContent = (v.q ? (v.q === '8K' ? '4K' : v.q) + ' · ' : '') + (play.list.length > 1 ? 'Channel ' + (play.index + 1) + ' of ' + play.list.length + ' · CH▲▼ to switch' : '');
    $('#osdProgress').classList.add('hidden');
    liveOsd();
    chanEpg(g, v).then(function (ps) {
      if (!play || play.group !== g) return;
      var now = Date.now() / 1000, cur = ps.filter(function (p) { return p.end > now; });
      if (cur[0]) $('#osdSub').textContent = hhmm(cur[0].start) + '–' + hhmm(cur[0].end) + '  ' + cur[0].title + (cur[1] ? '   ·   Next ' + hhmm(cur[1].start) + ' ' + cur[1].title : '');
    });
  }
  // One connection: while the Mac records, anything the TV would fetch straight from the provider (films, episodes,
  // catch-up) would knock the recording out. Ask the Mac first (0.9.0).
  function recGate() {
    if (!MAC.link()) return Promise.resolve(null);
    return MAC.get('/api/ping', {}, 2500).then(function (d) { return (d && d.rec && d.rec.active) || null; }).catch(function () { return null; });
  }
  function recBlocks(r, then, stopped) {
    var until = r.stop ? ' until ' + hhmm(r.stop) : '';
    modal('<img class="modal-mascot" src="img/icon-192.png" alt=""><h2>The Mac is recording</h2><p>“' + esc(r.title || 'A recording') + '”' + (r.channel ? ' on ' + esc(r.channel) : '') +
      ' is being recorded' + until + '. Your subscription allows one connection, ' + (stopped ? 'so what you were watching has stopped; your place is saved.' : 'so this has to wait; starting it now would break the recording.') + '</p>' +
      '<div class="btns"><button class="f btn primary autofocus" id="mRecW">Watch the recording</button><button class="f btn" id="mRecOk">OK</button></div>', null);
    $('#mRecW').onclick = function () { closeModal(); playRec({ key: 'rec:' + r.id, id: r.id, name: r.title, rec: { status: 'recording', channel: r.channel } }); };
    $('#mRecOk').onclick = function () { closeModal(); if (then) then(); };
  }
  function guarded(fn) {
    return function () {
      var args = arguments, self = this;
      return recGate().then(function (r) { if (r) recBlocks(r); else fn.apply(self, args); });
    };
  }
  var playVod = guarded(playVodNow), playEpisode = guarded(playEpisodeNow);
  // Films and episodes in .mp4 often keep their index at the END of the file, and the provider answers byte-range
  // requests with a broken header ("Accept-Ranges: 0-<size>" instead of "bytes"): the TV's player then never starts.
  // .mkv plays straight from the provider; .mp4 goes through Beeldbuis on the Mac (/direct passes the file through
  // with proper ranges) when the TV is linked to it.
  function vodSrc(kind, id, ext) {
    ext = ext || 'mp4';
    var l = MAC.link();
    if (l && /^(mp4|m4v|mov)$/i.test(ext))
      return { mode: 'macdirect', url: l.base + '/direct/' + kind + '/' + encodeURIComponent(id) + '.' + ext + '?k=' + encodeURIComponent(l.key) };
    return { mode: 'direct', url: kind === 'movie' ? X.vodUrl(id, ext) : X.episodeUrl(id, ext) };
  }
  function playVodNow(it, start) {
    S.addRecent(stripItem(it));
    var src = vodSrc('movie', it.id, it.ext);
    startPlayback(src.url, stripItem(it), { live: false, start: start });
    play.mode = src.mode;
    play.vod = { kind: 'movie', id: it.id, ext: it.ext || 'mp4' };
    $('#osdTitle').textContent = it.name; $('#osdSub').textContent = '▲ Subtitles';
  }
  function playEpisodeNow(ep, seriesItem, pos) {
    var start = pos ? pos.t : 0;
    S.addRecent(seriesItem || ep);
    var src = vodSrc('series', ep.id, ep.ext);
    startPlayback(src.url, ep, { live: false, start: start });
    play.mode = src.mode;
    play.vod = { kind: 'series', id: ep.id, ext: ep.ext || 'mp4' };
    $('#osdTitle').textContent = ep.name; $('#osdSub').textContent = '▲ Subtitles';
  }
  // A film or episode that does not open straight from the provider (the TV's player can't always follow the provider's
  // secure links and redirects, or the file is missing): the Mac fetches it instead, finds a working copy (other
  // extension or edition) and hands the TV a plain stream on the home network.
  function vodViaMac(p, why) {
    if (!p || !p.vod || p.mode === 'macvod' || !MAC.link()) return false;
    var at = Math.max(p.opts.start || 0, PL.position ? PL.position() : 0), item = p.item, ret = p.returnTo;
    p.mode = 'macvod';
    PL.stop();
    $('#osdTime').textContent = 'Loading via your Mac…';
    showOsd(true);
    MAC.get('/api/play', { kind: p.vod.kind, id: p.vod.id, ext: p.vod.ext, t: Math.floor(at), native: 1 }, 45000).then(function (d) {
      if (play !== p) return;
      var l = MAC.link(), start = 0;
      if (d && d.fallback && !/^(mp4|m4v|mov)$/i.test(d.fallback.ext || '')) {   // another copy that the TV can open itself
        MAC.get('/api/stop', {}, 4000).catch(function () {});
        p.url = p.vod.kind === 'movie' ? X.vodUrl(d.fallback.id, d.fallback.ext) : X.episodeUrl(d.fallback.id, d.fallback.ext);
        p.offset = 0; start = at;
      } else if (d && d.fallback && d.fallback.id) {                              // another .mp4: through the Mac as well
        MAC.get('/api/stop', {}, 4000).catch(function () {});
        p.url = vodSrc(p.vod.kind, d.fallback.id, d.fallback.ext).url;
        p.offset = 0; start = at;
      } else if (d && d.direct) {                    // the Mac passes the file through as it is (seekable): start where we were
        p.url = l.base + d.direct + (d.direct.indexOf('?') >= 0 ? '&' : '?') + 'k=' + encodeURIComponent(l.key);
        p.offset = 0; start = at;
      } else if (d && d.playlist) {           // the Mac converts it: its stream starts at the requested time
        p.url = l.base + d.playlist;
        p.offset = d.offset || Math.floor(at);
      } else throw new Error('The Mac did not start it');
      if (d.fallback) toast('Playing ' + esc(d.fallback.label || 'another copy') + ' (the usual file is missing at the provider)', 5000);
      p.started = Date.now(); p.progressAt = 0;
      PL.open(p.url, { live: false, start: start, englishSubs: S.get().englishSubs });
      vodWatch(p);
    }).catch(function (e) {
      if (play !== p) return;
      failPlayback('This ' + (item.type === 'episode' ? 'episode' : 'film') + ' does not play right now (' + esc(why) + '; via the Mac: ' + esc(e.message || e) + ').');
    });
    p.returnTo = ret;
    return true;
  }
  // AVPlay sometimes never answers at all ("Loading…" forever): no progress within 30 s counts as an error
  function vodWatch(p) {
    clearTimeout(p.watch);
    p.watch = setTimeout(function () {
      if (play !== p || p.progressAt) return;
      var why = 'no picture after 30 s';
      if (!vodViaMac(p, why)) failPlayback(MAC.link() || !/\.(mp4|m4v|mov)$/i.test(p.url || '') ? 'This stream does not start (' + why + ').'
        : 'This file does not start on the TV straight from the provider. Link Beeldbuis on your Mac (Settings → Beeldbuis on your Mac) and it plays through the Mac.');
    }, 30000);
  }
  function nextEpisode() {
    var ep = play && play.item;
    if (!ep || ep.type !== 'episode' || !ep.episodes) return false;
    var idx = -1;
    ep.episodes.forEach(function (e, i) { if (String(e.id) === String(ep.id)) idx = i; });
    var n = ep.episodes[idx + 1];
    if (!n) return false;
    var next = { key: 'episode:' + n.id, type: 'episode', id: n.id, ext: n.ext || 'mp4', icon: ep.icon, name: ep.seriesItem.name + ' · S' + ep.season + 'E' + n.num,
      num: n.num, seriesItem: ep.seriesItem, season: ep.season, episodes: ep.episodes };
    stopPlayback();
    playEpisode(next, next.seriesItem, null);
    return true;
  }

  setInterval(function () { if (play && play.live && !$('#osd').classList.contains('hidden') && play.item.type !== 'rec' && play.state !== 'buffering') liveOsd(); }, 1000);
  function showOsd(force) {
    if (!play) return;
    $('#osd').classList.remove('hidden');
    clearTimeout(osdTimer);
    osdTimer = setTimeout(function () { $('#osd').classList.add('hidden'); applySubStyle(); }, force ? 6000 : 4000);
    applySubStyle();
  }

  PL.on({
    state: function (st) {
      if (!play) return;
      play.state = st;
      if (play.live && play.item.type !== 'rec') { if (st === 'buffering') $('#osdTime').textContent = 'Loading…'; else liveOsd(); return; }
      $('#osdTime').textContent = st === 'buffering' ? 'Loading…' : st === 'paused' ? 'Paused' : (play.live ? 'Live' : $('#osdTime').textContent);
    },
    ready: function (info) {
      if (!play || play.live || play.cues || !S.get().englishSubs || (info && info.subs)) return;
      extSubs(play, 0);
    },
    time: function (t, d) {
      if (!play || play.live) return;
      if (t > 0 && !play.progressAt) { play.progressAt = Date.now(); clearTimeout(play.watch); }
      if (play.offset) t += play.offset;
      if (play.cues) showCue(play, t);
      $('#osdProgress').classList.toggle('hidden', !d);
      if (d) $('#osdFill').style.width = (100 * t / d) + '%';
      $('#osdTime').textContent = dur(t) + (d ? ' / ' + dur(d) : '');
    },
    ended: function () {
      if (play && play.live && play.mode === 'cu') { cuTrouble(play, 'stopped'); return; }   // never straight back to live
      if (play && play.live && play.mode === 'mac') { macLost(); return; }
      if (play && !play.live) { S.setPosition(play.item, PL.duration(), PL.duration()); pushPos(play.item, PL.duration(), PL.duration()); if (!nextEpisode()) leavePlayer(); } },
    subtitle: function (text) { if (!play || !play.cues) setSub(text); },
    error: function (code) {
      if (!play) return;
      if (play.live && play.mode === 'mac') { macLost(); return; }
      if (play.live && play.mode === 'cu') { cuTrouble(play, 'did not play'); return; }
      // live: try the next quality variant of the same channel first
      if (play.live && play.group && play.variant + 1 < play.group.variants.length && Date.now() - play.started < 20000) {
        var g = play.group, l = play.list, i = play.index, nv = play.variant + 1;
        toast('Trying ' + (order(g)[nv].q || 'another') + ' version…', 2500);
        playLive(g, l, i, nv);
        return;
      }
      var p = play;
      if (!p.live && !p.progressAt && vodViaMac(p, code)) return;
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
    if (root.BBRemote) root.BBRemote.now({ name: p && p.item ? p.item.name : '', error: String(msg).replace(/<[^>]*>/g, '') });
    modal('<img class="modal-mascot" src="img/icon-192.png" alt=""><h2>Can\'t play this</h2><p>' + msg + '</p><div class="btns"><button class="f btn primary autofocus" id="mOk">OK</button></div>',
      null);
    $('#mOk').onclick = function () { closeModal(); if (p && p.returnTo.focus && document.body.contains(p.returnTo.focus)) N.focus(p.returnTo.focus); };
  }

  // ======================= 0.9.5: typing from the phone, match data, calm home, screensaver =======================

  // ---------- typing from the phone or the Mac: the text lands in the field that has focus, or opens Search ----------
  function remoteText(c) {
    var t = document.activeElement, text = String(c.text || '');
    if (!(t && t.tagName === 'INPUT')) {
      if (play) { toast('Typing from your phone works in Search. Stop what is playing first.', 5000); return; }
      closeModal();
      search.q = text; showSearch(); t = $('#sq');
      if (!t) return;
    }
    t.value = text;
    if (t.oninput) t.oninput();
    if (c.enter) { t.blur(); if (t._done) t._done(); }
  }

  // ---------- match data next to the picture (live sport; the Mac matches the channel to live data) ----------
  // 0 = off, 1 = spoiler-free (the match you watch live shows its score; nothing else does), 2 = every score
  var side = { on: 0, timer: null, data: null, over: false };
  var okHold = { down: 0, fired: false, timer: null };
  function sideCan() { return !!(play && play.live && play.item.type !== 'rec' && MAC.link()); }
  function sideSet(n, quiet) {
    if (n && !sideCan()) {
      if (!quiet) toast(MAC.link() ? 'Match data is for live TV' : 'Match data needs Beeldbuis on your Mac (Settings → Beeldbuis on your Mac)', 5000);
      return;
    }
    side.on = n;
    clearInterval(side.timer); side.timer = null;
    var el = $('#side');
    side.over = !PL.rect(n ? [0, 0, 1440, 810] : null);          // players that cannot shrink the picture get the panel on top
    document.body.classList.toggle('sided', !!n && !side.over);
    el.classList.toggle('over', !!n && side.over);
    el.classList.toggle('hidden', !n);
    if (!n) { el.innerHTML = ''; return; }
    if (side.data) sideDraw(); else el.innerHTML = '<div class="sd-col"><div class="sd-none">Looking for match data…</div></div>';
    sideLoad(); side.timer = setInterval(sideLoad, 20000);
  }
  function sideCycle() { sideSet((side.on + 1) % 3); }
  function sideSid(p) { var v = p.group && order(p.group)[p.variant || 0]; return v ? v.id : ''; }
  function sideLoad() {
    if (!side.on || !sideCan()) return;
    var p = play;
    MAC.get('/api/companion', { sid: sideSid(p), kind: 'live', name: p.item.name }, 12000).then(function (d) {
      if (play === p && side.on) { side.data = d; side.for = p.item.key; sideDraw(); }
    }).catch(function (e) {
      if (play === p && side.on && !side.data) $('#side').innerHTML = '<div class="sd-col"><div class="sd-none">' + errorMessage(e) + '</div></div>';
    });
  }
  function sdScore(t, show) { return show ? esc(t.score == null ? '' : t.score) : '·'; }
  function sdElsewhere(e, all) {
    var name = e.sport === 'racing' ? e.name : (e.teams || []).map(function (t) { return t.short || t.name; }).join(' – ');
    var sc = all && e.teams ? (e.teams || []).map(function (t) { return t.score; }).join('–') + ' · ' : '';
    return '<span class="sd-else"><b>' + esc(name) + '</b> ' + esc(sc + (e.detail || '')) + '</span>';
  }
  function sideDraw() {
    var d = side.data, el = $('#side'); if (!d || !el || !play) return;
    var behind = liveBehind(), late = behind > 45 || play.mode === 'cu' || !!play.pausedAt;
    var all = side.on === 2, show = all || !late;
    side.late = late;
    var m = d.match, c = d.context || {};
    var h = '<div class="sd-col"><div class="sd-ctx"><b>' + esc(c.channel || title(play.item.name)) + '</b>' + (c.title ? '<span>' + esc(c.title) + '</span>' : '') + '</div>';
    if (late && !all) h += '<div class="sd-note">You are ' + Math.max(1, Math.round(behind / 60)) + ' min behind live. Scores stay hidden.</div>';
    if (late && all) h += '<div class="sd-note warn">These are the live scores: ahead of your picture.</div>';
    if (!m) h += '<div class="sd-none">No live match data for this channel right now.</div>';
    else if (m.sport === 'racing') {
      h += '<div class="sd-lg">' + esc(m.league_name || '') + (m.session ? ' · ' + esc(m.session) : '') + (m.state === 'in' ? ' · live' : '') + '</div><div class="sd-race">' + esc(m.name || '') + '</div>';
      if (show) h += '<div class="sd-list">' + (m.order || []).slice(0, 20).map(function (o) { return '<div><i>' + esc(o.pos == null ? '' : o.pos) + '</i>' + esc(o.name || '') + '</div>'; }).join('') + '</div>';
      else h += '<div class="sd-none">Positions hidden.</div>';
      if (m.next_session && m.next_session.start) h += '<div class="sd-lg">Next: ' + esc(m.next_session.name || '') + ' · ' + hhmm(Date.parse(m.next_session.start) / 1000) + '</div>';
    } else {
      var t = m.teams || [], a = t[0] || {}, b = t[1] || {};
      h += '<div class="sd-lg">' + esc(m.league_name || '') + (m.state === 'in' ? ' · live' : m.state === 'pre' && m.start ? ' · ' + hhmm(Date.parse(m.start) / 1000) : '') + '</div>' +
        '<div class="sd-teams"><div class="sd-tm">' + (a.logo ? img(a.logo) : '') + '<span>' + esc(a.short || a.name || '') + '</span></div>' +
        '<div class="sd-sc">' + sdScore(a, show) + ' – ' + sdScore(b, show) + '</div>' +
        '<div class="sd-tm">' + (b.logo ? img(b.logo) : '') + '<span>' + esc(b.short || b.name || '') + '</span></div></div>';
      if (show && m.detail) h += '<div class="sd-det">' + esc(m.detail) + '</div>';
      if (show && m.situation) h += '<div class="sd-det dim">' + esc(m.situation) + '</div>';
      if (show && m.last_play) h += '<div class="sd-play">' + esc(m.last_play) + '</div>';
      var ev = show ? (m.events || []) : [];
      if (ev.length) h += '<div class="sd-list ev">' + ev.slice(-9).map(function (e) {
        var team = t.filter(function (x) { return x.id === e.team; })[0];
        return '<div><i>' + esc(e.min || '') + '</i>' + (e.goal ? '⚽ ' : e.red ? '🟥 ' : e.yellow ? '🟨 ' : '• ') + esc(e.who || e.type || '') + (team ? ' <u>' + esc(team.short || '') + '</u>' : '') + '</div>';
      }).join('') + '</div>';
      var lu = m.lineups || [];
      if (lu.length && ev.length < 5) h += '<div class="sd-lu">' + lu.slice(0, 2).map(function (l) {
        return '<div><b>' + esc(l.team || '') + (l.formation ? ' · ' + esc(l.formation) : '') + '</b>' + (l.players || []).slice(0, 11).map(function (p) { return '<span>' + esc(p.no || '') + ' ' + esc(p.name || '') + '</span>'; }).join('') + '</div>';
      }).join('') + '</div>';
    }
    h += '</div><div class="sd-strip"><div class="sd-hint">Hold <b>OK</b>: ' + (side.on === 1 ? 'show all scores' : 'close') + '</div>';
    var els = d.live_elsewhere || [];
    h += els.length ? '<div class="sd-lg">Also live' + (all ? '' : ' · scores hidden') + '</div><div class="sd-elses">' + els.slice(0, 8).map(function (e) { return sdElsewhere(e, all); }).join('') + '</div>'
      : '<div class="sd-lg">Nothing else live in your competitions right now.</div>';
    el.innerHTML = h + '</div>';
  }
  // once per channel: tell the viewer when there is data for the match on screen
  function sideOffer(p) {
    if (!MAC.link() || side.on) return;
    setTimeout(function () {
      if (play !== p || side.on || !sideCan()) return;
      MAC.get('/api/companion', { sid: sideSid(p), kind: 'live', name: p.item.name }, 12000).then(function (d) {
        if (play !== p || side.on || !d || !d.match) return;
        side.data = d;
        toast('📊 Match data for this game: hold <b>OK</b>', 6000);
      }).catch(function () {});
    }, 12000);
  }

  // ---------- what the calm home screen and the screensaver show: weather, photos and the next recording from the Mac ----------
  var ambient = { t: 0, amb: null, home: null, busy: null };
  function ambientLoad(force) {
    if (!MAC.link()) return Promise.resolve(ambient);
    if (ambient.busy) return ambient.busy;
    if (!force && ambient.amb && Date.now() - ambient.t < 600000) return Promise.resolve(ambient);
    var a = MAC.get('/api/tv/ambient', {}, 12000).then(function (d) { ambient.amb = d; }, function () {});
    var h = MAC.home(S.get().profile).then(function (d) { ambient.home = d; if (d.img_base) macImg = String(d.img_base).replace(/\/+$/, '') + '/w342'; }, function () {});
    ambient.busy = Promise.all([a, h]).then(function () { ambient.t = Date.now(); ambient.busy = null; return ambient; });
    return ambient.busy;
  }
  function ambientLive() {
    var r = ambient.home && (ambient.home.rows || []).filter(function (x) { return x.id === 'live'; })[0];
    return (r && r.tiles) || [];
  }
  function wxHtml(w) {
    if (!w || w.error || w.temp == null) return '';
    return '<span class="wx-i">' + esc(w.icon || '') + '</span><span class="wx-t">' + Math.round(w.temp) + '°</span><span class="wx-d"><b>' + esc(w.text || '') + '</b>' +
      esc(w.place || '') + (w.max != null ? ' · ' + Math.round(w.min) + '° / ' + Math.round(w.max) + '°' : '') + (w.rain != null ? ' · rain ' + Math.round(w.rain) + '%' : '') + '</span>';
  }
  function recLine(r) {
    if (!r) return '';
    return r.status === 'recording' ? '● Recording now: ' + r.title : 'Next recording: ' + r.title + ' · ' + (new Date(r.start * 1000).toDateString() === new Date().toDateString() ? '' : DAYS[new Date(r.start * 1000).getDay()].slice(0, 3) + ' ') + hhmm(r.start);
  }
  function longDate(d) { return DAYS[d.getDay()] + ' ' + d.getDate() + ' ' + MONTHS[d.getMonth()]; }

  // ---------- calm home: the clock, where to go, the game consoles and apps; nothing moves, nothing shouts ----------
  var APP_LABEL = { netflix: 'Netflix', youtube: 'YouTube', prime: 'Prime Video', disney: 'Disney+', spotify: 'Spotify' };
  var calmGen = 0;
  function showCalm() {
    showTop(false); setTab(null); view.tab = 'calm';
    var gen = ++calmGen, d = new Date(), last = S.get().lastChannel;
    var places = [['home', 'Tonight', 'What to watch'], ['live', 'Live TV', last && last.name ? title(last.name) : 'All channels'], ['sport', 'Sport', 'Live right now'],
      ['movies', 'Movies', ''], ['series', 'Series', ''], ['search', 'Search', 'Or type on your phone']];
    screen.innerHTML = '<div class="calm"><div class="calm-top"><div><div class="calm-clock" id="calmClock">' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + '</div>' +
      '<div class="calm-date" id="calmDate">' + longDate(d) + '</div></div><div class="calm-wx" id="calmWx"></div></div>' +
      '<div class="calm-row">' + places.map(function (p) {
        return '<button class="f ctile" data-go="' + p[0] + '"><b>' + p[1] + '</b><span id="calmSub_' + p[0] + '">' + esc(p[2]) + '</span></button>'; }).join('') + '</div>' +
      '<div class="calm-row dev" id="calmDev"></div>' +
      '<div class="calm-foot"><div id="calmNote"></div><img src="img/wordmark-mascot.png" alt="Beeldbuis"></div></div>';
    Array.prototype.forEach.call(screen.querySelectorAll('.ctile'), function (b) {
      b.onclick = function () { var t = document.querySelector('.tab[data-tab="' + b.getAttribute('data-go') + '"]'); if (t) t.click(); };
    });
    view.back = confirmExit;
    N.first(screen);
    calmDev(null);
    ambientLoad().then(function () { if (gen === calmGen && view.tab === 'calm' && $('#calmDev')) calmFill(); });
  }
  function calmDev(amb) {
    var el = $('#calmDev'); if (!el) return;
    var had = N.current() && el.contains(N.current()) ? N.current().getAttribute('data-id') : null;
    var h = '';
    if (amb) {
      h += (amb.inputs || []).map(function (n) { return '<button class="f cdev" data-id="in:' + esc(n) + '" data-in="' + esc(n) + '"><i>🎮</i>' + esc(n) + '</button>'; }).join('');
      h += (amb.apps || []).map(function (a) { return '<button class="f cdev" data-id="app:' + esc(a) + '" data-app="' + esc(a) + '"><i>▶</i>' + esc(APP_LABEL[a] || a) + '</button>'; }).join('');
    }
    h += '<button class="f cdev" data-id="set" id="calmSet"><i>⚙</i>Settings</button>';
    el.innerHTML = h;
    Array.prototype.forEach.call(el.querySelectorAll('[data-in]'), function (b) {
      b.onclick = function () { toast('Switching to ' + esc(b.getAttribute('data-in')) + '…', 8000); MAC.post('/api/remote/send', { action: 'input', name: b.getAttribute('data-in') }, 40000).catch(function (e) { toast(errorMessage(e), 6000); }); };
    });
    Array.prototype.forEach.call(el.querySelectorAll('[data-app]'), function (b) {
      b.onclick = function () { toast('Opening ' + esc(APP_LABEL[b.getAttribute('data-app')] || b.getAttribute('data-app')) + '…', 6000); MAC.post('/api/remote/send', { action: 'app', app: b.getAttribute('data-app') }, 40000).catch(function (e) { toast(errorMessage(e), 6000); }); };
    });
    $('#calmSet').onclick = showSettings;
    if (had) { var again = el.querySelector('[data-id="' + had + '"]'); if (again) N.focus(again); }
  }
  function calmFill() {
    var amb = ambient.amb || {};
    $('#calmWx').innerHTML = wxHtml(amb.weather);
    calmDev(amb);
    var picks = ambient.home ? heroHtml(ambient.home) : [], lv = ambientLive();
    if (picks.length && $('#calmSub_home')) $('#calmSub_home').textContent = picks[0].t.title || 'What to watch';
    if ($('#calmSub_sport')) $('#calmSub_sport').textContent = lv.length ? lv.length + ' live now' : 'Nothing live right now';
    $('#calmNote').textContent = recLine(amb.next_rec);
  }

  // ---------- screensaver: after a few quiet minutes the TV shows your photos and tonight's picks; any key brings you back ----------
  var saver = { on: false, idle: null, tick: null, n: 0, slides: [], cur: null, since: 0, layer: 0 };
  function saverMinutes() { var m = S.get().saverMin; return m == null ? 5 : m; }
  function saverArm() {
    clearTimeout(saver.idle);
    if (saver.on || !saverMinutes()) return;
    saver.idle = setTimeout(function () {
      var t = document.activeElement;
      if (play || document.hidden || !S.get().account || !$('#busy').classList.contains('hidden') || (t && t.tagName === 'INPUT')) return saverArm();
      saverStart();
    }, saverMinutes() * 60000);
  }
  function systemSaver(on) {
    try { var ac = root.webapis.appcommon; ac.setScreenSaver(on ? ac.AppCommonScreenSaverState.SCREEN_SAVER_ON : ac.AppCommonScreenSaverState.SCREEN_SAVER_OFF); } catch (e) {}
  }
  function saverSlides() {
    var out = [], amb = ambient.amb || {}, picks = ambient.home ? heroHtml(ambient.home) : [], l = MAC.link();
    var base = ((ambient.home && ambient.home.img_base) || 'https://image.tmdb.org/t/p').replace(/\/w\d+$/, '').replace(/\/+$/, '');
    var ph = [], i;
    for (i = 0; l && i < Math.min(amb.photos || 0, 60); i++) ph.push({ type: 'photo', url: l.base + '/api/tv/photo?i=' + i + '&k=' + encodeURIComponent(l.key) });
    var pk = picks.map(function (p) { return { type: 'pick', t: p.t, label: p.label, url: base + '/w1280' + p.t.meta.backdrop }; });
    // two photos, then a pick; without photos only picks; without either a plain clock
    if (ph.length) { var b = 0; ph.forEach(function (p, a) { out.push(p); if (a % 2 === 1 && pk.length) out.push(pk[b++ % pk.length]); }); }
    else out = pk;
    return out.length ? out : [{ type: 'plain' }];
  }
  function saverStart() {
    if (saver.on) return;
    saver.on = true; saver.since = Date.now(); saver.n = 0; saver.layer = 0; saver.cur = null;
    clearTimeout(saver.idle);
    var el = $('#saver');
    el.innerHTML = '<div class="sv-img" id="sv0"></div><div class="sv-img" id="sv1"></div><div class="sv-shade"></div>' +
      '<div class="sv-box" id="svBox"><div class="sv-clock" id="svClock"></div><div class="sv-date" id="svDate"></div><div class="sv-wx" id="svWx"></div>' +
      '<div class="sv-cap" id="svCap"></div><div class="sv-line" id="svLine"></div></div><img class="sv-mascot" id="svMascot" src="img/icon-192.png" alt="">';
    el.classList.remove('hidden');
    systemSaver(false);
    saver.slides = saverSlides();
    saverShow();
    saver.tick = setInterval(saverShow, 20000);
    ambientLoad().then(function () { if (saver.on) saver.slides = saverSlides(); });
  }
  function saverStop() {
    if (!saver.on) return;
    saver.on = false;
    clearInterval(saver.tick);
    var el = $('#saver'); el.classList.add('hidden'); el.innerHTML = '';
    systemSaver(true);
    saverArm();
  }
  var SV_POS = [[110, 640], [1180, 640], [1180, 90], [110, 90]];
  function saverShow() {
    if (!saver.on) return;
    var d = new Date(), amb = ambient.amb || {}, box = $('#svBox');
    var rest = Date.now() - saver.since > 3 * 3600000;            // after three hours: only the clock, and the TV may dim or sleep
    if (rest && !saver.rest) systemSaver(true);
    saver.rest = rest;
    var s = rest ? { type: 'plain' } : saver.slides[saver.n % saver.slides.length];
    saver.cur = s;
    var pos = SV_POS[saver.n % 4];
    box.style.left = pos[0] + 'px'; box.style.top = pos[1] + 'px';
    $('#svClock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
    $('#svDate').textContent = longDate(d);
    $('#svWx').innerHTML = rest ? '' : wxHtml(amb.weather);
    $('#svCap').innerHTML = s.type === 'pick' ? '<span class="lchip">' + esc(s.label) + '</span> <b>' + esc((s.t.meta && s.t.meta.title) || s.t.title) + '</b>' + (s.t.year ? ' (' + esc(s.t.year) + ')' : '') + '<i>OK opens it</i>' : '';
    var lv = ambientLive(), lines = [];
    if (!rest) {
      if (lv.length) { var t = lv[saver.n % lv.length]; lines.push('● Live now: ' + t.title + (t.channel ? ' · ' + t.channel : '')); }
      if (amb.next_rec) lines.push(recLine(amb.next_rec));
    }
    $('#svLine').textContent = lines.length ? lines[saver.n % lines.length] : '';
    $('#svMascot').classList.toggle('hidden', s.type !== 'plain');
    document.getElementById('saver').className = 'sv-' + s.type;
    var n = saver.n++;
    var next = $('#sv' + (saver.layer ^ 1)), prev = $('#sv' + saver.layer);
    function swap(url) {
      if (!saver.on || saver.n !== n + 1) return;
      next.style.backgroundImage = url ? 'url("' + url + '")' : 'none';
      next.className = 'sv-img on' + (s.type === 'photo' ? ' photo' : '');
      prev.className = prev.className.replace(' on', '');
      saver.layer ^= 1;
    }
    if (s.url) {
      var im = new Image();
      im.onload = function () { saver.errs = 0; swap(s.url); };
      im.onerror = function () {            // a picture that does not load: on to the next one, and a plain clock when none do
        if (!saver.on || saver.n !== n + 1) return;
        saver.errs = (saver.errs || 0) + 1;
        if (saver.errs <= saver.slides.length && saver.errs < 6) saverShow(); else { saver.cur = { type: 'plain' }; $('#svCap').innerHTML = ''; swap(''); }
      };
      im.src = s.url;
    }
    else swap('');
    if (saver.n % 30 === 0) ambientLoad(true).then(function () { if (saver.on) saver.slides = saverSlides(); });
  }

  // ---------- keys ----------
  function onKeyUp(e) {
    if (P.KEYS[e.keyCode] !== 'ok' || !okHold.down) return;
    clearTimeout(okHold.timer); okHold.down = 0;
    if (!okHold.fired && play && play.live && !$('#modal')) { livePause(); showOsd(); }
  }
  function onKey(e) {
    var a = P.KEYS[e.keyCode];
    saverArm();
    if (saver.on) {                       // any key wakes the screen; OK on a pick opens it
      if (e.preventDefault) e.preventDefault();
      var sv = saver.cur; saverStop();
      if (a === 'ok' && sv && sv.type === 'pick' && !play) openMacItem(macTile(sv.t));
      return;
    }
    if (a === undefined) return;
    var t = e.target;
    // let the on-screen keyboard / text fields keep their own keys
    if (t && t.tagName === 'INPUT') {
      // only OK, Back and up/down belong to the app; every other key is typing (space, i, digits, Backspace, ←/→ in the text)
      if (a === 'back' && e.keyCode !== 10009 && e.keyCode !== 27) return;
      if (a !== 'ok' && a !== 'back' && a !== 'up' && a !== 'down') return;
      if (a === 'ok') { e.preventDefault(); var box = $('#modal') || screen, inputs = Array.prototype.slice.call(box.querySelectorAll('input')), k = inputs.indexOf(t); if (k >= 0 && k < inputs.length - 1) N.focus(inputs[k + 1]); else { t.blur(); if (t._done) t._done(); else N.focus($('#btnLogin') || box.querySelector('.btn')); } return; }
      if (a === 'back') { e.preventDefault(); t.blur(); return; }
    }
    if (typeof a === 'number' && t && t.tagName === 'INPUT') return;
    e.preventDefault();

    if (play && !$('#modal')) return playerKey(a, e);
    if (a === 'back') {
      if ($('#modal') && $('#modal')._onBack) return $('#modal')._onBack();
      if (closeModal()) return;
      // on a tab's own screen Back first steps out: content -> sidebar -> tab bar, then leaves
      if ((view.back === showHome || view.back === confirmExit || view.back === showCalm) && N.climb()) return;
      if (view.back === 'exit') return P.exit();
      if (typeof view.back === 'function') return view.back();
      return;
    }
    if ((a === 'left' || a === 'right') && N.current() && N.current()._adjust) { N.current()._adjust(a === 'left' ? -1 : 1); return; }
    if (a === 'up' || a === 'down' || a === 'left' || a === 'right') {
      if (N.move(a)) return;
      if ((a === 'left' || a === 'right') && !$('#modal') && guideEdge(a)) return;
      if (a === 'up' && !$('#modal') && !$('#top').classList.contains('hidden')) N.focus($('.tab.active') || $('.tab'));
      return;
    }
    if (a === 'ok') { var c = N.current(); if (c) { if (c.tagName === 'INPUT') { c.focus(); } else c.click(); } return; }
    if (a === 'yellow') { var f = N.current(); if (f && f._fav) f._fav(); return; }
    if (a === 'red') { showLive(); return; }
    if (a === 'guide') { if (!$('#modal')) showGuide(view.tab === 'live' && $('#liveCats') ? live.groups : null, ''); return; }
    if (a === 'chup' || a === 'chdown') {
      var last = S.get().lastChannel;
      if (last && last.group) playLive(last.group, [last.group], 0);
    }
  }
  function playerKey(a, e) {
    if (a === 'back' && side.on) { sideSet(0); return; }
    if (a === 'back' || a === 'stop') { leavePlayer(); return; }
    if (play.live && play.item.type !== 'rec') {
      // a short press on OK pauses; holding it opens match data next to the picture (needs the Mac)
      if (a === 'ok' && e && e.type === 'keydown' && sideCan()) {
        if (!okHold.down) { okHold.down = Date.now(); okHold.fired = false; okHold.timer = setTimeout(function () { okHold.fired = true; sideCycle(); }, 650); }
        return;
      }
      if (a === 'info') { sideCycle(); return; }
      if (a === 'ok' || a === 'playpause') { livePause(); showOsd(); return; }
      if (a === 'pause') { livePause(true); showOsd(); return; }
      if (a === 'play') { if (play.pausedAt) livePause(false); else if (liveBehind() >= 15) goLive(); showOsd(); return; }
      if (a === 'left' || a === 'rw') { seekStep(a === 'rw' ? -60 : -15); return; }
      if (a === 'right' || a === 'ff') { if (liveBehind() < 15 && !play.pausedAt) { liveOsd(); showOsd(); return; } if (liveBehind() < (a === 'ff' ? 75 : 40)) { play.seekTarget = undefined; goLive(); showOsd(); return; } seekStep(a === 'ff' ? 60 : 30); return; }
      if (a === 'blue') { fromStart(); showOsd(); return; }
    }
    if (play.live && a === 'guide' && MAC.link()) { var gl = play.list; leavePlayer(); showGuide(gl && gl.length > 1 ? gl : null, ''); return; }
    if (play.live) {
      if (a === 'chup' || a === 'up') zap(-1);
      else if (a === 'chdown' || a === 'down') zap(1);
      else if (a === 'red') { recordNow(false); return; }
      else if (a === 'green') { recordNow(true); return; }
      else if (a === 'yellow') { favToast(toggleLiveFav(play.group)); }
      showOsd();
      return;
    }
    if (a === 'up' || a === 'info') { showSubMenu(); return; }
    if (a === 'ok' || a === 'play' || a === 'pause' || a === 'playpause') PL.toggle();
    else if (a === 'right' || a === 'ff') PL.seekBy(a === 'ff' ? 60 : 30);
    else if (a === 'left' || a === 'rw') PL.seekBy(a === 'rw' ? -60 : -15);
    else if (a === 'chup' || a === 'chdown') { if (a === 'chdown') nextEpisode(); }
    showOsd();
  }

  // ---------- start ----------
  function wireTabs() {
    Array.prototype.forEach.call(document.querySelectorAll('.tab'), function (b) {
      b.onclick = function () {
        var t = b.getAttribute('data-tab');
        if (t === 'home') showHome(); else if (t === 'sport') showSport(); else if (t === 'live') showLive(); else if (t === 'movies') showVod('movies');
        else if (t === 'series') showVod('series'); else if (t === 'search') showSearch(); else showSettings();
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
    document.addEventListener('keyup', onKeyUp);
    document.addEventListener('click', function (e) {          // mouse/pointer (Android phones, browser preview)
      var el = e.target.closest ? e.target.closest('.f') : null;
      if (el) N.focus(el, { noScroll: true });
    }, true);
    clock(); clockTimer = setInterval(clock, 10000);
    document.addEventListener('visibilitychange', function () { if (document.hidden) saverStop(); if (document.hidden && play) stopPlayback(); });
    saverArm();
    S.load();
    if (root.BBRemote) root.BBRemote.start(function (c) { if (S.get().account) onRemote(c); });
    if (!S.get().account) return showLogin();
    if (!S.get().countries.length) return showCountries(true);
    if (S.get().startScreen === 'calm') showCalm(); else showHome();
    setTimeout(function () { syncChannels(true); }, 4000);
  }

  // native wrappers (Android) deliver keys the WebView does not pass on, e.g. Back and CH▲▼
  root.BBKey = function (action) {
    var code = 0;
    Object.keys(P.KEYS).forEach(function (k) { if (!code && P.KEYS[k] === action) code = +k; });
    if (action === 'back') code = 27;
    onKey({ keyCode: code, target: document.activeElement, preventDefault: function () {} });
  };
  root.BBApp = { start: start, version: VERSION, saver: saverStart, calm: showCalm, ambient: ambientLoad, debug: function () { return { side: side.on, saver: saver.on, playing: !!play, tab: view.tab, modal: !!$('#modal'), mode: play && play.mode, url: play && play.url, name: play && play.item && play.item.name }; } };
  start();
})(this);
