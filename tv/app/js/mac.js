/* Optional link with Beeldbuis on a Mac in the same network. The TV pairs once with the code the Mac shows
   (Settings → iPhone & iPad) and then asks the Mac for its Tonight rows (taste, Letterboxd, genres) and
   title details. Without the link, or when the Mac is off, the TV builds simpler rows on its own. */
(function (root) {
  'use strict';
  var S = root.BBStore, P = root.BBPlatform;

  function link() { return S.get().mac || null; }       // {base: "http://192.168.1.10:8765", key, name}
  function base(addr) {
    addr = String(addr || '').trim().replace(/\/+$/, '');
    if (!addr) return '';
    if (!/^https?:\/\//.test(addr)) addr = 'http://' + addr;
    var m = addr.match(/^(https?:\/\/[^\/]+)(.*)$/);
    if (m && !/:\d+$/.test(m[1])) addr = m[1] + ':8765' + m[2];
    return addr;
  }
  function timeout(p, ms) {
    return new Promise(function (resolve, reject) {
      var t = setTimeout(function () { reject(new Error('The Mac does not answer')); }, ms);
      p.then(function (v) { clearTimeout(t); resolve(v); }, function (e) { clearTimeout(t); reject(e); });
    });
  }
  function get(path, params, ms) {
    var l = link();
    if (!l) return Promise.reject(new Error('Not linked'));
    var qs = Object.keys(params || {}).map(function (k) { return k + '=' + encodeURIComponent(params[k]); });
    qs.push('k=' + encodeURIComponent(l.key));
    return timeout(fetch(P.xurl(l.base + path + '?' + qs.join('&'))), ms || 6000).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || d.ok === false) throw new Error((d && d.error) || 'The Mac said no');
      return d.data !== undefined ? d.data : d;
    });
  }
  function pair(addr, code) {
    var b = base(addr);
    if (!b) return Promise.reject(new Error('Type the address of your Mac'));
    return timeout(fetch(b + '/api/pair', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: String(code || '').trim(), name: 'TV (' + P.name + ')' }) }), 8000)
      .then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) throw new Error((d && d.error) || 'Pairing failed');
        var s = S.get(); s.mac = { base: b, key: d.data.key, name: d.data.name }; S.save();
        return s.mac;
      });
  }
  function post(path, body, ms) {
    var l = link();
    if (!l) return Promise.reject(new Error('Not linked'));
    return timeout(fetch(P.xurl(l.base + path + '?k=' + encodeURIComponent(l.key)), { method: 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }), ms || 6000)
      .then(function (r) { return r.json(); }).then(function (d) {
        if (!d || d.ok === false) throw new Error((d && d.error) || 'The Mac said no');
        return d.data !== undefined ? d.data : d;
      });
  }
  // verdict: 'up' (more like this), 'down' (not for me), 'seen' or 'clear'
  function feedback(key, verdict, profile) { return post('/api/feedback', { profile: profile, key: key, verdict: verdict }); }
  // the watchlist: only what you tick yourself (Mac 27+)
  function watchlist(key, on, profile) { return post('/api/watchlist', { profile: profile, key: key, on: !!on }); }
  function watchKeys(profile, kind) { return get('/api/watchlist', kind ? { profile: profile, kind: kind } : { profile: profile }, 8000); }
  // Resume positions shared with the Mac, so Continue watching works on both
  function progress(body) { return post('/api/progress', body); }
  function positions() { return get('/api/positions', {}, 6000); }
  function unlink() { var s = S.get(); s.mac = null; S.save(); }
  function home(profile) { return get('/api/home', { profile: profile }, 8000); }
  function item(key) { return get('/api/item', { key: key }, 8000); }
  function library(kind, opts) {
    opts = opts || {};
    var p = { kind: kind, limit: opts.limit || 200 };
    if (opts.genre) p.genre = opts.genre;
    if (opts.q) p.q = opts.q;
    return get('/api/library', p, 10000);
  }
  function shelves(kind, profile, shelf, offset, limit) {
    var p = { kind: kind, p: profile };
    if (shelf) { p.shelf = shelf; p.offset = offset || 0; p.limit = limit || 120; }
    return get('/api/shelves', p, 12000);
  }
  // sort & filter on the Mac's whole library (hidden gems, best rated, length, year, …)
  function discover(kind, params, offset, limit) {
    var p = { kind: kind, offset: offset || 0, limit: limit || 300 };
    Object.keys(params || {}).forEach(function (k) { if (params[k] !== '' && params[k] != null) p[k] = params[k]; });
    return get('/api/discover', p, 15000);
  }
  // IMDb / Rotten Tomatoes from the Mac: keys = Mac library keys, ids = provider ids like "movie:123" / "series:45"
  function ratings(keys, ids) {
    var p = {};
    if (keys && keys.length) p.keys = keys.join(',');
    if (ids && ids.length) p.ids = ids.join(',');
    return get('/api/ratings', p, 8000);
  }
  // Mac 28+: the full guide, guide search, subtitles and the shared channel settings
  function guide(ids, from, to) { return get('/api/tv/guide', { ids: ids.join(','), from: Math.floor(from), to: Math.floor(to) }, 15000); }
  function guideSearch(q) { return get('/api/tv/search', { q: q, limit: 60 }, 10000); }
  function channels() { return get('/api/tv/channels', {}, 8000); }
  function fav(key, on) { return post('/api/tv/fav', { key: key, on: !!on }); }
  function subs(p) { return get('/api/tv/subs', p, 25000); }
  function url(path) { var l = link(); return l ? l.base + path + (path.indexOf('?') < 0 ? '?' : '&') + 'k=' + encodeURIComponent(l.key) : ''; }
  var api = { guide: guide, guideSearch: guideSearch, channels: channels, fav: fav, subs: subs, url: url, ratings: ratings, get: get, post: post, progress: progress, positions: positions, feedback: feedback, watchlist: watchlist, watchKeys: watchKeys, shelves: shelves, discover: discover, link: link, base: base, pair: pair, unlink: unlink, home: home, item: item, library: library };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.BBMac = api;
})(this);
