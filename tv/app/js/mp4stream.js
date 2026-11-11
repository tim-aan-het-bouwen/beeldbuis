/* Plays an .mp4 whose index ('moov') sits at the END of the file, straight from the provider, without the Mac (0.9.9).
   Samsung's own player asks the provider for the file's tail in a way the provider's server refuses, so it never starts.
   Here the TV reads the index itself (an explicit byte range from the end), cuts the file into small fragments with
   mp4box.js and feeds those to the TV browser's <video> through Media Source Extensions, fetching ~4 MB at a time and
   keeping only a few minutes in memory. English mov_text subtitles in the file are read along the way.
   BBMp4Stream(video, events) -> { open(url, start, wantSubs), seek(t), stop() }
   events: state(s), time(t, d), ended(), error(code), ready(info), subtitle(text) */
(function (root) {
  'use strict';
  var P = root.BBPlatform;
  var CHUNK = (root.BB_MP4_CHUNK || 4 * 1024 * 1024), AHEAD = root.BB_MP4_AHEAD || 60, BEHIND = 30;

  function supported() {
    return !!(root.MediaSource && root.MP4Box && root.MediaSource.isTypeSupported && root.MediaSource.isTypeSupported('video/mp4; codecs="avc1.4d401f"'));
  }

  function range(url, from, to, timeoutMs) {
    var x = new XMLHttpRequest();
    var p = new Promise(function (resolve, reject) {
      x.open('GET', P.xurl(url)); x.responseType = 'arraybuffer'; x.timeout = timeoutMs || 30000;
      x.setRequestHeader('Range', 'bytes=' + from + '-' + to);
      x.onload = function () {
        if (x.status !== 206 && !(x.status === 200 && from === 0)) return reject(new Error('HTTP ' + x.status));
        var total = 0, cr = x.getResponseHeader('Content-Range') || '', m = cr.match(/\/(\d+)\s*$/);
        if (m) total = +m[1];
        resolve({ buf: x.response, total: total });
      };
      x.onerror = function () { reject(new Error('network')); };
      x.ontimeout = function () { reject(new Error('timeout')); };
      x.send();
    });
    p.xhr = x;
    return p;
  }

  // top-level boxes in the first bytes: where mdat starts and how long it is
  function boxes(buf) {
    var b = new DataView(buf), pos = 0, n = b.byteLength, out = [];
    while (pos + 8 <= n) {
      var size = b.getUint32(pos), type = String.fromCharCode(b.getUint8(pos + 4), b.getUint8(pos + 5), b.getUint8(pos + 6), b.getUint8(pos + 7));
      if (size === 1) { if (pos + 16 > n) break; size = b.getUint32(pos + 8) * 4294967296 + b.getUint32(pos + 12); }
      out.push({ type: type, start: pos, size: size });
      if (size < 8 || type === 'mdat') break;
      pos += size;
    }
    return out;
  }

  // tx3g / mov_text sample: 2-byte length + UTF-8 text
  function tx3g(data) {
    if (!data || data.length < 2) return '';
    var len = (data[0] << 8) | data[1], s = '';
    for (var i = 2; i < 2 + len && i < data.length; i++) s += String.fromCharCode(data[i]);
    try { return decodeURIComponent(escape(s)); } catch (e) { return s; }
  }

  function create(video, ev) {
    var gen = 0, st = null;

    function stop() {
      gen++;
      if (st) {
        try { if (st.req && st.req.xhr) st.req.xhr.abort(); } catch (e) {}
        try { st.mp4.stop(); } catch (e) {}
        clearInterval(st.timer);
        video.ontimeupdate = video.onplaying = video.onwaiting = video.onpause = video.onended = video.onerror = null;
        try { video.pause(); video.removeAttribute('src'); video.load(); } catch (e) {}
        try { if (st.url) URL.revokeObjectURL(st.url); } catch (e) {}
      }
      st = null;
    }

    function fail(code) { var g = gen; stop(); if (g + 1 === gen) ev.error(code); }

    function pump(s) {                       // append queued fragments to their SourceBuffer, one at a time
      s.tracks.forEach(function (t) {
        if (t.sb.updating || !t.queue.length) return;
        try { t.sb.appendBuffer(t.queue[0]); t.queue.shift(); }
        catch (e) {
          if (e && e.name === 'QuotaExceededError') trim(s, true);      // full: drop what was played, retry on updateend/timer
          else fail('MSE_APPEND ' + (e && e.name));
        }
      });
      if (s.eof && !s.ended && s.tracks.every(function (t) { return !t.queue.length && !t.sb.updating; })) {
        s.ended = true; try { s.ms.endOfStream(); } catch (e) {}
      }
    }

    function trim(s, hard) {                 // drop what was played long ago: the TV has little memory
      var cut = video.currentTime - (hard ? 5 : BEHIND);
      if (cut <= 1) return;
      s.tracks.forEach(function (t) {
        try { if (!t.sb.updating && t.sb.buffered.length && t.sb.buffered.start(0) < cut - 1) t.sb.remove(0, cut); } catch (e) {}
      });
    }

    function ahead() {
      var b = video.buffered, t = video.currentTime;
      for (var i = 0; i < b.length; i++) if (b.start(i) <= t + 0.5 && b.end(i) > t) return b.end(i) - t;
      return 0;
    }

    function fetchLoop(s, g) {
      if (g !== gen) return;
      if (s.pos >= s.dataEnd) { s.eof = true; try { s.mp4.flush(); } catch (e) {} pump(s); return; }
      var queued = s.tracks.reduce(function (n, t) { return n + t.queue.length; }, 0);
      if (ahead() > AHEAD || queued > 6) { setTimeout(function () { fetchLoop(s, g); }, 800); return; }
      var to = Math.min(s.pos + CHUNK, s.dataEnd) - 1, from = s.pos;
      s.req = range(s.src, from, to);
      s.req.then(function (r) {
        if (g !== gen) return;
        s.fails = 0;
        var ab = r.buf; ab.fileStart = from;
        s.pos = from + ab.byteLength;
        s.mp4.appendBuffer(ab);
        gc(s);
        pump(s); trim(s);
        fetchLoop(s, g);
      }, function (e) {
        if (g !== gen) return;
        if (++s.fails > 4) { fail('NETWORK ' + e.message); return; }
        setTimeout(function () { fetchLoop(s, g); }, 1500 * s.fails);
      });
    }

    // mp4box keeps a downloaded chunk until every byte in it was read; bytes of tracks we don't play (other audio
    // languages, 25 subtitle tracks) are never read, so without this every chunk of a 2 GB film would stay in memory.
    function gc(s) {
      var mp4 = s.mp4, minNext = Infinity;
      s.ids.forEach(function (id) {
        var tk = mp4.getTrackById(id), smp = tk && tk.samples[tk.nextSample];
        if (smp && smp.offset < minNext) minNext = smp.offset;
      });
      var bufs = mp4.stream.buffers || [];
      for (var i = 0; i < bufs.length; i++) {
        var b = bufs[i];
        if (b.fileStart >= s.dataStart && b.fileStart < s.dataEnd && b.fileStart + b.byteLength <= minNext) b.usedBytes = b.byteLength;
      }
      if (mp4.stream.cleanBuffers) mp4.stream.cleanBuffers();
    }

    function inBuffer(t) {
      var b = video.buffered;
      for (var i = 0; i < b.length; i++) if (b.start(i) <= t && t < b.end(i) - 0.5) return true;
      return false;
    }
    // mp4box's seek() answers the earliest time over ALL tracks, subtitles included; sparse subtitles can point
    // far back (seek 80 s gave 40 s). Take time from the video keyframe and the byte offset from video+audio only.
    function videoTime(s) {
      var v = null;
      s.tracks.forEach(function (tr) {
        var tk = tr.video && s.mp4.getTrackById(tr.id), smp = tk && tk.samples[tk.nextSample];
        if (smp) v = smp.cts / smp.timescale;
      });
      return v;
    }
    function seekAt(s, t) {
      var r = s.mp4.seek(Math.max(0, t), true), off = Infinity, time = null, vt = videoTime(s);
      if (vt !== null && vt < t - 0.05) r = s.mp4.seek(vt, true);  // audio must start at the video keyframe, not at t
      (s.mp4.fragmentedTracks || []).forEach(function (f) { f.segmentStream = null; });   // half-built segment from before the jump
      s.tracks.forEach(function (tr) {
        var tk = s.mp4.getTrackById(tr.id), smp = tk && tk.samples[tk.nextSample];
        if (!smp) return;
        if (smp.offset < off) off = smp.offset;
        if (tr.video) time = smp.cts / smp.timescale;
      });
      return { offset: off < Infinity ? off : r.offset, time: time !== null ? time : r.time };
    }

    function seekTo(s, t, g) {
      if (inBuffer(t)) { video.currentTime = t; return; }          // already here: just jump
      var r = seekAt(s, t);
      s.mp4.stop();
      try { if (s.req && s.req.xhr) s.req.xhr.abort(); } catch (e) {}
      s.tracks.forEach(function (t) {
        t.queue = [];
        try { if (t.sb.updating) t.sb.abort(); if (t.sb.buffered.length) t.sb.remove(0, Infinity); } catch (e) {}
      });
      s.eof = s.ended = false;
      s.pos = Math.max(s.dataStart, Math.min(r.offset, s.dataEnd));
      s.mp4.start();
      video.currentTime = r.time;
      fetchLoop(s, g);
    }

    function open(src, start, wantSubs) {
      stop();
      var g = ++gen, s = st = { src: src, tracks: [], ids: [], pos: 0, fails: 0, cues: [], cueAt: -1 };
      ev.state('buffering');
      if (!supported()) { setTimeout(function () { if (g === gen) fail('MSE_UNSUPPORTED'); }, 0); return; }
      var mp4 = s.mp4 = root.MP4Box.createFile();
      mp4.onError = function (e) { if (g === gen) fail('MP4_PARSE ' + e); };
      range(src, 0, 65535, 15000).then(function (r) {
        if (g !== gen) return;
        var bx = boxes(r.buf), mdat = bx.filter(function (b) { return b.type === 'mdat'; })[0];
        if (!r.total || !mdat) throw new Error('NO_MDAT');
        s.size = r.total; s.dataStart = mdat.start; s.dataEnd = Math.min(r.total, mdat.start + mdat.size);
        var head = r.buf.slice(0, Math.min(r.buf.byteLength, mdat.start + 16)); head.fileStart = 0;
        mp4.appendBuffer(head);
        var moovFrom = s.dataEnd;                   // everything after mdat: moov (and maybe a few small boxes)
        if (moovFrom >= r.total) throw new Error('NO_MOOV');
        return range(src, moovFrom, r.total - 1, 60000).then(function (m) {
          if (g !== gen) return;
          var ab = m.buf; ab.fileStart = moovFrom;
          mp4.onReady = function (info) { if (g === gen) setup(s, g, info, start, wantSubs); };
          mp4.appendBuffer(ab);
          if (!mp4.readySent) throw new Error('NO_MOOV_PARSED');
        });
      }).catch(function (e) { if (g === gen) fail(e.message || String(e)); });
    }

    function setup(s, g, info, start, wantSubs) {
      var mp4 = s.mp4, ms = s.ms = new root.MediaSource();
      var vtrack = info.videoTracks[0], atrack = info.audioTracks[0];
      if (!vtrack) { fail('NO_VIDEO'); return; }
      var want = [vtrack].concat(atrack ? [atrack] : []);
      var subTrack = null;
      if (wantSubs) (info.subtitleTracks || []).concat(info.otherTracks || []).forEach(function (t) {
        if (!subTrack && /^(eng|en)$/i.test(t.language || '') && /tx3g|mov_text|text|sbtl/i.test((t.codec || '') + (t.type || ''))) subTrack = t;
      });
      s.duration = info.duration / info.timescale;
      ev.ready({ subs: !!subTrack, duration: s.duration, remux: true });
      s.url = URL.createObjectURL(ms);
      ms.addEventListener('sourceopen', function () {
        if (g !== gen || s.opened) return;     // fires again after endOfStream() when a seek reopens the stream
        s.opened = true;
        try { ms.duration = s.duration; } catch (e) {}
        for (var i = 0; i < want.length; i++) {
          var t = want[i], mime = (t === vtrack ? 'video' : 'audio') + '/mp4; codecs="' + t.codec + '"';
          if (!root.MediaSource.isTypeSupported(mime)) { if (t === vtrack) { fail('CODEC ' + t.codec); return; } continue; }
          var sb = ms.addSourceBuffer(mime), tr = { id: t.id, sb: sb, queue: [], video: t === vtrack };
          sb.addEventListener('updateend', function () { if (st === s) pump(s); });
          sb.addEventListener('error', function () { if (st === s) fail('MSE_SB'); });
          s.tracks.push(tr);
          mp4.setSegmentOptions(t.id, tr, { nbSamples: t === vtrack ? 120 : 200 });
        }
        if (subTrack) mp4.setExtractionOptions(subTrack.id, null, { nbSamples: 20 });
        s.ids = s.tracks.map(function (t) { return t.id; }).concat(subTrack ? [subTrack.id] : []);
        mp4.onSegment = function (id, tr, buffer, sampleNum) {
          if (st !== s) return;
          tr.queue.push(buffer);
          mp4.releaseUsedSamples(id, sampleNum);
          pump(s);
        };
        mp4.onSamples = function (id, user, samples) {
          var ts = subTrack ? subTrack.timescale : 1000;
          samples.forEach(function (x) {
            var txt = tx3g(x.data);
            s.cues.push({ s: x.cts / ts, e: (x.cts + x.duration) / ts, t: txt });
          });
          mp4.releaseUsedSamples(id, samples[samples.length - 1].number + 1);
        };
        mp4.initializeSegmentation().forEach(function (seg) { seg.user.queue.push(seg.buffer); });
        pump(s);
        var r = seekAt(s, start || 0);
        s.pos = Math.max(s.dataStart, Math.min(r.offset, s.dataEnd));
        mp4.start();
        if (start) video.currentTime = r.time;
        fetchLoop(s, g);
      });
      video.onplaying = function () { ev.state('playing'); };
      video.onwaiting = function () { ev.state('buffering'); };
      video.onpause = function () { if (!video.ended) ev.state('paused'); };
      video.onended = function () { if (s.eof && video.currentTime >= (s.duration || 0) - 2) ev.ended(); };   // not the brief 'ended' while a seek refills
      video.onerror = function () { fail('MEDIA ' + (video.error && video.error.code)); };
      video.ontimeupdate = function () {
        var t = video.currentTime;
        ev.time(t, s.duration);
        if (s.cues.length) {                        // subtitles from the file, shown at the right moment
          var cur = '';
          for (var i = 0; i < s.cues.length; i++) if (s.cues[i].s <= t && t < s.cues[i].e) { cur = s.cues[i].t; break; }
          if (cur !== s.cueText) { s.cueText = cur; ev.subtitle(cur); }
          if (s.cues.length > 400) s.cues = s.cues.filter(function (c) { return c.e > t - 60; });
        }
      };
      s.timer = setInterval(function () { if (st === s) { pump(s); trim(s); } }, 2000);
      video.src = s.url;
      var p = video.play(); if (p && p.catch) p.catch(function () {});
    }

    return {
      open: open,
      stop: stop,
      seek: function (t) { if (st && st.mp4 && st.mp4.moov) seekTo(st, Math.min(t, (st.duration || t) - 1), gen); },
      position: function () { return video.currentTime || 0; },
      stats: function () { return st && st.mp4 ? { buffers: (st.mp4.stream.buffers || []).length, bytes: (st.mp4.stream.buffers || []).reduce(function (n, b) { return n + b.byteLength; }, 0), pos: st.pos } : null; },
      duration: function () { return st ? st.duration || 0 : 0; }
    };
  }

  root.BBMp4Stream = { create: create, supported: supported, boxes: boxes };
})(this);
