/* Self-test for the iPhone/iPad web app, run in Safari on a Mac (same video engine as the iPhone).
   Only active as index.html?selftest on a local test server that answers /c (login + test ids) and /log (results):
   see ios/test/selftest_server.py. Plays one thing at a time (subscriptions allow one connection). */
(function (root) {
  'use strict';
  if (!/[?&]selftest\b/.test(location.search)) return;
  var A, S = root.BBStore, X = root.BBXtream, PL = root.BBPlayer, v = function () { return document.getElementById('video'); };
  var T0 = Date.now(), cur = '';
  function log(m, extra) {
    var d = { t: cur, m: m, at: Math.round((Date.now() - T0) / 100) / 10, ct: +(v().currentTime || 0).toFixed(1), rs: v().readyState };
    try { var g = A.debug(); d.mode = g.mode; d.playing = g.playing; d.modal = g.modal; } catch (e) {}
    if (extra) d.x = extra;
    fetch('/log', { method: 'POST', body: JSON.stringify(d) }).catch(function () {});
  }
  function wait(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function watch(name, secs, start) {
    cur = name; log('start');
    var first = 0, iv = setInterval(function () {
      if (!first && v().currentTime > 0.5) { first = 1; log('FIRST FRAME'); }
      var m = document.querySelector('#modal h2'); if (m) { log('modal: ' + m.textContent, (document.querySelector('#modal p') || {}).textContent); clearInterval(iv); }
    }, 500);
    start();
    return wait(secs * 1000).then(function () {
      clearInterval(iv);
      log('end', { src: String(v().currentSrc || '').replace(/\/(live|movie|series|timeshift)\/[^/]+\/[^/]+\//, '/$1/U/P/') });
      A.test.stop(); var x = v(); x.removeAttribute('src'); try { x.load(); } catch (e) {} return wait(4000);
    });
  }
  root.addEventListener('load', function () {
    A = root.BBApp;
    fetch('/c').then(function (r) { return r.json(); }).then(function (c) {
      return X.login(c.s, c.u, c.p).then(function (r) {
        var s = S.get(); s.account = r.account; if (!s.countries.length) s.countries = ['NL']; S.save(); log('login ok');
        var steps = Promise.resolve();
        (c.tests || []).forEach(function (t) {
          steps = steps.then(function () {
            if (t.kind === 'live') return watch(t.name, t.secs || 40, function () { A.test.live(t.id, t.name); });
            if (t.kind === 'movie') return watch(t.name, t.secs || 30, function () { A.test.vod({ key: 'vod:' + t.id, type: 'movie', id: t.id, ext: t.ext, name: t.title || t.name, cat: t.cat, icon: '' }); });
            if (t.kind === 'url') return watch(t.name, t.secs || 30, function () { var x = v(); x.classList.remove('hidden'); x.src = t.url.replace('{S}', c.s).replace('{U}', encodeURIComponent(c.u)).replace('{P}', encodeURIComponent(c.p)); x.play().catch(function (e) { log('play() ' + e.name); }); });
          });
        });
        return steps.then(function () { log('DONE'); });
      });
    }).catch(function (e) { log('ERROR ' + (e && e.message)); log('DONE'); });
  });
})(this);
