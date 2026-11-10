/* IMDb and Rotten Tomatoes ratings from OMDb (www.omdbapi.com). The free key is typed in once on the device
   (Settings) and never leaves it. Answers are cached for 30 days in IndexedDB. */
(function (root) {
  'use strict';
  var S = root.BBStore, P = root.BBPlatform, DB = root.BBIdb;
  var DAY = 86400000, inflight = {}, queue = [], running = 0;

  function key() { return (S.get().omdbKey || '').trim(); }
  function base() { return S.get().omdbBase || 'https://www.omdbapi.com/'; }
  // "EN - The Long Night (2024)" -> {t: "The Long Night", y: "2024"}
  function parse(name, year) {
    var s = String(name || '').replace(/^\s*[A-Z0-9]{2,3}(\s*[-|:]\s*[A-Z0-9]{2,6})*\s*[-|:]\s+/, '').replace(/\[[^\]]*\]/g, ' ');
    var m = s.match(/\((\d{4})\)/) || s.match(/\s(\d{4})\s*$/);
    var y = year || (m ? m[1] : '');
    s = s.replace(/\(\d{4}\)/, ' ').replace(/\s\d{4}\s*$/, ' ').replace(/\s+/g, ' ').trim();
    return { t: s, y: y };
  }
  function fetchOne(q, type) {
    var url = base() + '?apikey=' + encodeURIComponent(key()) + '&t=' + encodeURIComponent(q.t) + (q.y ? '&y=' + q.y : '') + '&type=' + (type === 'series' ? 'series' : 'movie');
    return fetch(P.xurl(url)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || d.Response === 'False') {
        if (d && /limit|invalid api key/i.test(d.Error || '')) throw new Error(d.Error);
        return {};
      }
      var rt = (d.Ratings || []).filter(function (x) { return x.Source === 'Rotten Tomatoes'; })[0];
      return { imdb: d.imdbRating && d.imdbRating !== 'N/A' ? d.imdbRating : '', rt: rt ? rt.Value : '', id: d.imdbID || '',
        genre: d.Genre && d.Genre !== 'N/A' ? d.Genre : '', poster: d.Poster && d.Poster !== 'N/A' ? d.Poster : '' };
    });
  }
  // a few at a time, so a row of 24 tiles doesn't fire 24 requests at once
  function pump() {
    while (running < 3 && queue.length) {
      var job = queue.shift(); running++;
      job().then(function () { running--; pump(); }, function () { running--; pump(); });
    }
  }
  function ratings(name, type, year) {
    if (!key()) return Promise.resolve(null);
    var q = parse(name, year), id = 'omdb:' + (type === 'series' ? 's' : 'm') + ':' + q.t.toLowerCase() + ':' + q.y;
    if (!q.t) return Promise.resolve(null);
    if (inflight[id]) return inflight[id];
    var p = DB.get(id).then(function (c) {
      if (c && Date.now() - c.t < 30 * DAY) return c.v;
      return new Promise(function (resolve) {
        queue.push(function () {
          return fetchOne(q, type).then(function (v) { DB.set(id, { t: Date.now(), v: v }); resolve(v); }, function () { resolve(null); });
        });
        pump();
      });
    });
    inflight[id] = p;
    p.then(function () { delete inflight[id]; });
    return p;
  }
  function label(v) {
    if (!v) return '';
    return [v.imdb ? 'IMDb ' + v.imdb : '', v.rt ? 'RT ' + v.rt : ''].filter(Boolean).join(' · ');
  }
  var api = { ratings: ratings, label: label, parse: parse, enabled: function () { return !!key(); } };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.BBOmdb = api;
})(this);
