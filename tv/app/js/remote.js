/* Remote link with Beeldbuis on the Mac: "Play on TV" from a phone or the Mac arrives here.
   The TV keeps one long request open to the Mac (/api/remote/poll); the Mac answers when there is a command. */
(function (root) {
  'use strict';
  var M = root.BBMac, S = root.BBStore;
  var since = -1, running = false, handler = null, failures = 0;

  function loop() {
    if (!M.link()) { running = false; return; }
    running = true;
    var l = M.link();
    var url = l.base + '/api/remote/poll?since=' + since + '&k=' + encodeURIComponent(l.key);
    var done = false;
    var t = setTimeout(function () { if (!done) { done = true; next(5000); } }, 35000);
    fetch(root.BBPlatform.xurl(url)).then(function (r) { return r.json(); }).then(function (d) {
      if (done) return; done = true; clearTimeout(t);
      if (!d || !d.ok) throw new Error('no');
      failures = 0;
      var data = d.data || {};
      since = data.seq || since;
      (data.cmds || []).forEach(function (c) { try { if (handler) handler(c); } catch (e) {} });
      next(200);
    }).catch(function () {
      if (done) return; done = true; clearTimeout(t);
      failures++;
      next(Math.min(60000, 3000 * failures));
    });
  }
  function next(ms) { setTimeout(loop, ms); }
  function start(onCommand) {
    handler = onCommand;
    if (!running) loop();
  }
  function now(info) { if (M.link()) M.post('/api/remote/now', info || {}).catch(function () {}); }
  function record(streamId, nextProg) { return M.post('/api/remote/record', { stream_id: streamId, next: !!nextProg }, 8000); }
  function recordings() { return M.get('/api/recordings', {}, 8000); }
  function recUrl(id) { var l = M.link(); return l.base + '/direct/rec/' + encodeURIComponent(id) + '?k=' + encodeURIComponent(l.key); }
  function recHls(id) {
    var l = M.link();
    return M.get('/api/play', { kind: 'rec', id: id, ext: 'ts', t: 0, native: 1 }, 30000).then(function (d) { return l.base + d.playlist; });
  }
  root.BBRemote = { start: start, now: now, record: record, recordings: recordings, recUrl: recUrl, recHls: recHls };
})(this);
