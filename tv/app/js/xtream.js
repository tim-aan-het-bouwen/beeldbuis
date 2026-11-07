/* Xtream Codes client, straight from the device to the provider. */
(function (root) {
  'use strict';
  var P = root.BBPlatform, S = root.BBStore;
  var HOUR = 3600 * 1000;

  function acct() { var a = S.get().account; if (!a) throw new Error('Not signed in'); return a; }
  function base(a) { return a.server.replace(/\/+$/, ''); }

  function normalizeServer(s) {
    s = String(s || '').trim();
    if (!s) return '';
    if (s.toLowerCase() === 'demo') return location.origin + '/demo';
    if (!/^https?:\/\//i.test(s)) s = 'http://' + s;
    return s.replace(/\/(player_api\.php|get\.php).*$/i, '').replace(/\/+$/, '');
  }

  function getJSON(url, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var x = new XMLHttpRequest(), done = false;
      x.open('GET', P.xurl(url));
      x.timeout = timeoutMs || 60000;
      x.onload = function () {
        if (done) return; done = true;
        if (x.status < 200 || x.status >= 300) return reject(new Error('The server answered ' + x.status));
        try { resolve(JSON.parse(x.responseText)); }
        catch (e) { reject(new Error('The server did not return a valid answer (is this an Xtream Codes server?)')); }
      };
      x.onerror = function () { if (!done) { done = true; reject(new Error('Cannot reach the server')); } };
      x.ontimeout = function () { if (!done) { done = true; reject(new Error('The server is not responding')); } };
      x.send();
    });
  }

  function api(action, params, a) {
    a = a || acct();
    var q = 'username=' + encodeURIComponent(a.username) + '&password=' + encodeURIComponent(a.password);
    if (action) q += '&action=' + action;
    Object.keys(params || {}).forEach(function (k) { q += '&' + k + '=' + encodeURIComponent(params[k]); });
    return getJSON(base(a) + '/player_api.php?' + q);
  }

  function login(server, username, password) {
    var a = { server: normalizeServer(server), username: String(username || '').trim(), password: String(password || '') };
    if (!a.server || !a.username) return Promise.reject(new Error('Fill in server and username'));
    return api(null, null, a).then(function (r) {
      var ui = r && r.user_info;
      if (!ui || String(ui.auth) !== '1') throw new Error('Wrong username or password');
      if (ui.status && ui.status !== 'Active') throw new Error('This subscription is ' + String(ui.status).toLowerCase());
      return { account: a, info: r };
    });
  }

  // How many connections are in use right now (for the "already in use" message)
  function connections() {
    return api(null).then(function (r) {
      var ui = (r && r.user_info) || {};
      return { active: parseInt(ui.active_cons, 10) || 0, max: parseInt(ui.max_connections, 10) || 1, exp: parseInt(ui.exp_date, 10) || 0 };
    });
  }

  // Lists are cached in IndexedDB with a time-to-live; parallel requests for the same list share one fetch.
  var inflight = {};
  function cached(name, ttl, fetcher) {
    var key = 'cache:' + acct().username + '@' + base(acct()) + ':' + name;
    if (inflight[key]) return inflight[key];
    var p = root.BBIdb.get(key).then(function (c) {
      if (c && Date.now() - c.t < ttl) return c.v;
      return fetcher().then(function (x) { root.BBIdb.set(key, { t: Date.now(), v: x }); return x; });
    });
    inflight[key] = p;
    var done = function () { delete inflight[key]; };
    p.then(done, done);
    return p;
  }
  function list(x) { return Array.isArray(x) ? x : []; }

  // Adult categories stay hidden unless switched on in Settings.
  var ADULT = /(^|[^a-z])(adult|xxx|porn|18\+|\+18)([^a-z]|$)/i;
  function isAdult(c) { return String(c.is_adult) === '1' || ADULT.test(c.category_name || ''); }
  function family(cats) { return S.get().showAdult ? cats : cats.filter(function (c) { return !isAdult(c); }); }
  function liveCategories() { return cached('livecats', 12 * HOUR, function () { return api('get_live_categories').then(list); }).then(family); }
  function vodCategories() { return cached('vodcats', 12 * HOUR, function () { return api('get_vod_categories').then(list); }).then(family); }
  function seriesCategories() { return cached('sercats', 12 * HOUR, function () { return api('get_series_categories').then(list); }).then(family); }

  // Only the categories the viewer asked for are fetched: a full list can be 50,000+ channels,
  // too much for a TV. Each category is cached on its own.
  function liveStreams(catId) {
    return cached('live:' + catId, 12 * HOUR, function () {
      return api('get_live_streams', { category_id: catId }).then(function (r) {
        return list(r).map(function (s) {
          return { stream_id: s.stream_id, name: s.name, category_id: s.category_id, stream_icon: s.stream_icon || '',
            epg_channel_id: s.epg_channel_id || '', tv_archive: s.tv_archive, tv_archive_duration: s.tv_archive_duration };
        });
      });
    });
  }
  function vodStreams(catId) {
    return cached('vod:' + catId, 12 * HOUR, function () {
      return api('get_vod_streams', { category_id: catId }).then(function (r) {
        return list(r).map(function (s) {
          return { id: s.stream_id, name: s.name, icon: s.stream_icon || '', rating: s.rating || '', added: parseInt(s.added, 10) || 0,
            ext: s.container_extension || 'mp4' };
        });
      });
    });
  }
  function series(catId) {
    return cached('series:' + catId, 12 * HOUR, function () {
      return api('get_series', { category_id: catId }).then(function (r) {
        return list(r).map(function (s) {
          return { id: s.series_id, name: s.name, icon: s.cover || '', rating: s.rating || '', plot: s.plot || '', genre: s.genre || '',
            year: (s.releaseDate || s.release_date || '').slice(0, 4) };
        });
      });
    });
  }
  function seriesInfo(id) { return api('get_series_info', { series_id: id }); }
  function vodInfo(id) { return api('get_vod_info', { vod_id: id }); }

  function b64(s) { try { return decodeURIComponent(escape(root.atob(s || ''))); } catch (e) { return s || ''; } }
  // Now/next from the provider (empty at some providers; the app then simply shows no programme info)
  function shortEpg(streamId) {
    return api('get_short_epg', { stream_id: streamId, limit: 2 }).then(function (r) {
      return list(r && r.epg_listings).map(function (p) {
        return { title: b64(p.title), desc: b64(p.description), start: parseInt(p.start_timestamp, 10) || 0, end: parseInt(p.stop_timestamp, 10) || 0 };
      });
    }).catch(function () { return []; });
  }

  // Samsung's player wants the raw MPEG-TS; browsers need HLS.
  function liveUrl(id) {
    var a = acct(), ext = P.name === 'browser' ? 'm3u8' : 'ts';
    return base(a) + '/live/' + encodeURIComponent(a.username) + '/' + encodeURIComponent(a.password) + '/' + id + '.' + ext;
  }
  function vodUrl(id, ext) { var a = acct(); return base(a) + '/movie/' + encodeURIComponent(a.username) + '/' + encodeURIComponent(a.password) + '/' + id + '.' + (ext || 'mp4'); }
  function episodeUrl(id, ext) { var a = acct(); return base(a) + '/series/' + encodeURIComponent(a.username) + '/' + encodeURIComponent(a.password) + '/' + id + '.' + (ext || 'mp4'); }

  root.BBXtream = { isAdult: isAdult, normalizeServer: normalizeServer, login: login, connections: connections, liveCategories: liveCategories,
    vodCategories: vodCategories, seriesCategories: seriesCategories, liveStreams: liveStreams, vodStreams: vodStreams,
    series: series, seriesInfo: seriesInfo, vodInfo: vodInfo, shortEpg: shortEpg, liveUrl: liveUrl, vodUrl: vodUrl, episodeUrl: episodeUrl };
})(this);
