/* Search across channels (your country groups), movies and series.
   Xtream has no search call, so the lists are fetched per category (cached for 12 hours in IndexedDB)
   and searched on the device. The first search after a refresh takes a moment; later ones are instant. */
(function (root) {
  'use strict';
  var X = root.BBXtream, G = root.BBGroups, S = root.BBStore;
  var index = { live: null, movies: null, series: null };
  var building = {};

  function norm(s) {
    s = String(s || '').toLowerCase();
    try { s = s.normalize('NFD').replace(/[̀-ͯ]/g, ''); } catch (e) {}
    return s.replace(/^\s*[a-z0-9]{2,3}\s*[-|:]\s+/, '').replace(/[^a-z0-9]+/g, ' ').trim();
  }

  // fetch lists for all categories, a few at a time, reporting progress
  function eachCategory(cats, fetchOne, onProgress) {
    var i = 0, done = 0, out = [], N = cats.length, workers = Math.min(3, N || 1);
    return new Promise(function (resolve) {
      if (!N) return resolve(out);
      function next() {
        if (i >= N) return;
        var c = cats[i++];
        fetchOne(c).then(function (list) { out.push.apply(out, list); }, function () {}).then(function () {
          done++;
          if (onProgress) onProgress(done, N);
          if (done === N) resolve(out); else next();
        });
      }
      for (var w = 0; w < workers; w++) next();
    });
  }

  function build(kind, onProgress) {
    if (index[kind]) return Promise.resolve(index[kind]);
    if (building[kind]) return building[kind];
    var p;
    if (kind === 'live') {
      p = X.liveCategories().then(function (cats) {
        var countries = S.get().countries;
        var mine = cats.filter(function (c) { return countries.indexOf(G.categoryCountry(c.category_name)) >= 0; });
        return eachCategory(mine, function (c) { return X.liveStreams(c.category_id); }, onProgress).then(function (streams) {
          return G.buildGroups(mine, streams).map(function (g) { return { n: norm(g.name), g: g }; });
        });
      });
    } else {
      var isMovie = kind === 'movies';
      p = (isMovie ? X.vodCategories() : X.seriesCategories()).then(function (cats) {
        return eachCategory(cats, function (c) { return isMovie ? X.vodStreams(c.category_id) : X.series(c.category_id); }, onProgress)
          .then(function (items) {
            var seen = {};
            return items.filter(function (x) { if (seen[x.id]) return false; seen[x.id] = 1; return true; })
              .map(function (x) { return { n: norm(x.name), x: x }; });
          });
      });
    }
    building[kind] = p.then(function (ix) { index[kind] = ix; delete building[kind]; return ix; },
      function (e) { delete building[kind]; throw e; });
    return building[kind];
  }

  // all words must appear; names that start with the query come first
  function find(kind, query, limit) {
    var ix = index[kind];
    var q = norm(query);
    if (!ix || q.length < 2) return [];
    var words = q.split(' '), hits = [];
    for (var i = 0; i < ix.length; i++) {
      var n = ix[i].n, ok = true;
      for (var w = 0; w < words.length; w++) if (n.indexOf(words[w]) < 0) { ok = false; break; }
      if (ok) hits.push(ix[i]);
    }
    hits.sort(function (a, b) {
      var as = a.n.indexOf(q) === 0 ? 0 : 1, bs = b.n.indexOf(q) === 0 ? 0 : 1;
      return (as - bs) || (a.n.length - b.n.length);
    });
    return hits.slice(0, limit || 40).map(function (h) { return h.g || h.x; });
  }

  function reset() { index = { live: null, movies: null, series: null }; }
  function ready(kind) { return !!index[kind]; }

  var api = { build: build, find: find, reset: reset, ready: ready, norm: norm };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.BBSearch = api;
})(this);
