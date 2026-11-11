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
    var i = 0;
    (function next() {
      if (i >= steps.length) {
        running = false;
        if (M.link()) M.get('/api/stop', {}, 4000).catch(function () {});
        info.done = true; report(out, info); return;
      }
      var st = steps[i++];
      Promise.resolve().then(st[1]).then(function (url) { return tryOne(st[0], url, 25000); }, function (e) { return { test: st[0], result: 'skipped: ' + (e && e.message) }; })
        .then(function (r) {
          out.push(r); report(out, info);
          if (M.link() && /Mac/.test(st[0])) return M.get('/api/stop', {}, 4000).catch(function () {});
        }).then(function () { setTimeout(next, 3000); });
    })();
  }
  root.BBDiag = { run: run, tryOne: tryOne };
})(this);
