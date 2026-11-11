/* Player self-test (0.9.3): plays a handful of sources one by one with the TV's own player and reports to the Mac
   (POST /api/remote/now {name:'Beeldbuis self-test', diag:[...]}), so playback can be diagnosed without anyone at the TV.
   Started by a Play on TV command named "Beeldbuis self-test". Never two provider streams at once. */
(function (root) {
  'use strict';
  var S = root.BBStore, M = root.BBMac, R = root.BBRemote;
  var running = false;

  function acct() { return S.get().account || {}; }
  function srv(scheme) { var b = String(acct().server || '').replace(/\/+$/, ''); return scheme ? b.replace(/^https?:/i, scheme + ':') : b; }
  function path(kind, id, ext) { var a = acct(); return '/' + kind + '/' + encodeURIComponent(a.username) + '/' + encodeURIComponent(a.password) + '/' + id + '.' + ext; }
  function redact(u) {
    var a = acct(); u = String(u || '');
    if (a.username) u = u.split(encodeURIComponent(a.username)).join('U');
    if (a.password) u = u.split(encodeURIComponent(a.password)).join('P');
    if (M.link() && M.link().key) u = u.split(encodeURIComponent(M.link().key)).join('K');
    return u.replace(/^(https?:\/\/)[^\/:]+/i, function (m, s) { return s + (u.indexOf(M.link() ? M.link().base : '#') === 0 ? 'mac' : 'provider'); });
  }
  function errText(e) { if (!e) return 'null'; var o = []; ['name', 'message', 'code', 'type'].forEach(function (k) { try { if (e[k] !== undefined) o.push(k + '=' + e[k]); } catch (x) {} }); return o.join(' ') || String(e); }

  // one source: open, prepareAsync, play; watch for the first play time for up to `ms`
  function tryOne(label, url, ms) {
    return new Promise(function (resolve) {
      var av = root.webapis && root.webapis.avplay, t0 = Date.now(), r = { test: label, url: redact(url), ev: [] }, done = false;
      function ev(s) { r.ev.push(Math.round((Date.now() - t0) / 100) / 10 + 's ' + s); }
      function finish(result) {
        if (done) return; done = true; clearTimeout(timer);
        r.result = result; r.secs = Math.round((Date.now() - t0) / 100) / 10;
        try { r.state = av.getState(); } catch (e) {}
        try { av.stop(); av.close(); } catch (e) {}
        resolve(r);
      }
      var timer = setTimeout(function () { finish('timeout ' + ms / 1000 + 's'); }, ms);
      if (!av) { finish('no avplay'); return; }
      try { av.stop(); av.close(); } catch (e) {}
      try {
        av.open(url); ev('open ok');
        try { av.setDisplayRect(0, 0, 640, 360); } catch (e) {}
        av.setListener({
          onbufferingstart: function () { ev('buffering'); },
          onbufferingcomplete: function () { ev('buffered'); },
          oncurrentplaytime: function (msT) { if (msT > 0) { ev('time ' + msT); finish('PLAYS'); } },
          onstreamcompleted: function () { ev('completed'); finish('completed'); },
          onerror: function (type) { ev('onerror ' + type); finish('error ' + type); },
          onevent: function (type, data) { ev('event ' + type + ' ' + String(data || '').slice(0, 80)); }
        });
        try { av.setTimeoutForBuffering(20); } catch (e) {}
        av.prepareAsync(function () {
          ev('prepared'); try { r.duration = av.getDuration(); } catch (e) {}
          try { av.play(); ev('play called'); } catch (e) { ev('play threw ' + errText(e)); finish('play threw'); }
        }, function (e) { ev('prepare failed ' + errText(e)); finish('prepare failed'); });
      } catch (e) { ev('threw ' + errText(e)); finish('threw ' + errText(e)); }
    });
  }

  // the same file in the TV's browser player (<video>) instead of AVPlay
  function tryVideo(label, url, ms) {
    return new Promise(function (resolve) {
      var v = document.getElementById('video'), t0 = Date.now(), r = { test: label, url: redact(url), ev: [] }, done = false;
      function ev(s) { r.ev.push(Math.round((Date.now() - t0) / 100) / 10 + 's ' + s); }
      var names = ['loadedmetadata', 'canplay', 'playing', 'waiting', 'stalled', 'error'];
      function on(e) {
        if (e.type === 'error') { ev('error code ' + (v.error && v.error.code) + ' ' + ((v.error && v.error.message) || '')); finish('error'); return; }
        ev(e.type + (e.type === 'loadedmetadata' ? ' dur ' + Math.round(v.duration) + ' ' + v.videoWidth + 'x' + v.videoHeight : ''));
      }
      function tu() { if (v.currentTime > 0.2) { ev('time ' + v.currentTime.toFixed(1)); finish('PLAYS'); } }
      function finish(result) {
        if (done) return; done = true; clearTimeout(timer);
        names.forEach(function (n) { v.removeEventListener(n, on); }); v.removeEventListener('timeupdate', tu);
        r.result = result; r.secs = Math.round((Date.now() - t0) / 100) / 10;
        try { v.pause(); v.removeAttribute('src'); v.load(); } catch (e) {}
        v.classList.add('hidden');
        resolve(r);
      }
      var timer = setTimeout(function () { finish('timeout ' + ms / 1000 + 's'); }, ms);
      if (!v) { finish('no video element'); return; }
      try { root.webapis.avplay.stop(); root.webapis.avplay.close(); } catch (e) {}
      names.forEach(function (n) { v.addEventListener(n, on); }); v.addEventListener('timeupdate', tu);
      v.classList.remove('hidden');
      try { v.src = url; var pr = v.play(); if (pr && pr.catch) pr.catch(function (e) { ev('play() rejected ' + errText(e)); }); ev('src set'); }
      catch (e) { ev('threw ' + errText(e)); finish('threw'); }
    });
  }
  // where the provider sends the player, and what it says about byte ranges
  function probe(url) {
    return new Promise(function (resolve) {
      var x = new XMLHttpRequest(), done = false, t0 = Date.now();
      function fin(o) { if (done) return; done = true; try { x.abort(); } catch (e) {} o.ms = Date.now() - t0; resolve(o); }
      x.open('GET', url); x.setRequestHeader('Range', 'bytes=0-1');
      x.onreadystatechange = function () {
        if (x.readyState >= 2) fin({ status: x.status, final: x.responseURL || '', ranges: x.getResponseHeader('Accept-Ranges'), crange: x.getResponseHeader('Content-Range'), type: x.getResponseHeader('Content-Type') });
      };
      x.onerror = function () { fin({ error: 'xhr error' }); };
      setTimeout(function () { fin({ error: 'timeout' }); }, 15000);
      x.send();
    });
  }

  function report(out, extra) { if (R) R.now({ name: 'Beeldbuis self-test', kind: 'diag', diag: out, info: extra }); }

  function run(opts) {
    if (running) return; running = true;
    opts = opts || {};
    var out = [], info = { version: root.BBApp ? root.BBApp.version : '?', server: redact(srv()), mac: !!M.link(), ua: navigator.userAgent };
    try { info.avplay = root.webapis.avplay.getVersion ? root.webapis.avplay.getVersion() : ''; } catch (e) { info.avplay = 'n/a'; }
    try { info.fw = root.webapis.productinfo.getFirmware(); info.model = root.webapis.productinfo.getRealModel(); } catch (e) {}
    report(out, info);
    var last = S.get().lastChannel, chan = opts.live || (last && last.group && last.group.variants && last.group.variants[0] && last.group.variants[0].id);
    var ep = opts.episode || '1726186', ext = opts.ext || 'mp4';
    var steps = [];
    if (chan) {
      steps.push(['live http', function () { return srv('http') + path('live', chan, 'ts'); }]);
      steps.push(['live https', function () { return srv('https') + path('live', chan, 'ts'); }]);
    }
    steps.push(['episode http', function () { return srv('http') + path('series', ep, ext); }]);
    steps.push(['episode https', function () { return srv('https') + path('series', ep, ext); }]);
    if (M.link()) {
      steps.push(['recording via Mac (no provider)', function () {
        return R.recordings().then(function (d) {
          var r = ((d && d.items) || []).filter(function (x) { return x.file && x.status === 'done'; })[0];
          if (!r) throw new Error('no recording');
          return R.recUrl(r.id);
        });
      }]);
      steps.push(['episode via Mac', function () {
        return M.get('/api/play', { kind: 'series', id: ep, ext: ext, t: 0, native: 1 }, 45000).then(function (d) {
          var l = M.link();
          return d.direct ? l.base + d.direct + (d.direct.indexOf('?') >= 0 ? '&' : '?') + 'k=' + encodeURIComponent(l.key) : l.base + d.playlist;
        });
      }]);
      if (chan) steps.push(['live via Mac buffer', function () {
        return M.get('/api/play', { kind: 'live', id: chan, ext: 'ts', t: 0 }, 30000).then(function (d) {
          return M.link().base + '/hls/' + String(d.playlist || '').split('/')[2] + '/buffer.ts?behind=0';
        });
      }]);
    }
    if (opts.vod) {                       // 0.9.6: only the film/episode file, every way the TV could open it by itself
      var kind = opts.movie ? 'movie' : 'series', vid = opts.movie || ep, vurl = function (sch) { return srv(sch) + path(kind, vid, ext); }, fin = '';
      steps = [
        ['probe http (redirect + ranges)', function () { return probe(vurl('http')).then(function (o) { fin = o.final; o.final = redact(o.final); throw new Error(JSON.stringify(o)); }); }],
        ['avplay final url', function () { if (!fin) throw new Error('no final url'); return fin; }],
        ['html5 http', function () { return vurl('http'); }, 'video'],
        ['html5 https', function () { return vurl('https'); }, 'video'],
        ['html5 final url', function () { if (!fin) throw new Error('no final url'); return fin; }, 'video'],
        ['avplay http (again, 40 s)', function () { return vurl('http'); }, 'avplay40']
      ];
    }
    var i = 0;
    (function next() {
      if (i >= steps.length) {
        running = false;
        if (M.link()) M.get('/api/stop', {}, 4000).catch(function () {});
        info.done = true; report(out, info); return;
      }
      var st = steps[i++];
      Promise.resolve().then(st[1]).then(function (url) {
        return st[2] === 'video' ? tryVideo(st[0], url, 25000) : tryOne(st[0], url, st[2] === 'avplay40' ? 40000 : 25000);
      }, function (e) { return { test: st[0], result: 'info/skipped: ' + (e && e.message) }; })
        .then(function (r) {
          out.push(r); report(out, info);
          if (M.link() && /Mac/.test(st[0])) return M.get('/api/stop', {}, 4000).catch(function () {});
        }).then(function () { setTimeout(next, 3000); });
    })();
  }
  root.BBDiag = { run: run, tryOne: tryOne };
})(this);
