/* One player interface, three engines:
   - Samsung TV: webapis.avplay (plays MPEG-TS, MKV, MP4 natively; English subtitles via its text tracks)
   - Android:    native Media3 player behind the BeeldbuisAndroid bridge
   - Browser:    <video> + hls.js (preview on the laptop) */
(function (root) {
  'use strict';
  var P = root.BBPlatform;
  var listener = { ready: function () {}, state: function () {}, time: function () {}, ended: function () {}, error: function () {}, subtitle: function () {} };

  function on(handlers) { Object.keys(handlers).forEach(function (k) { listener[k] = handlers[k]; }); }

  function pickEnglish(tracks) {
    for (var i = 0; i < tracks.length; i++) {
      var l = String(tracks[i].lang || '').toLowerCase();
      if (l === 'en' || l === 'eng' || l.indexOf('english') >= 0) return tracks[i];
    }
    return null;
  }

  // ---------- Samsung (Tizen) ----------
  var tizenEngine = {
    open: function (url, opts) {
      var av = root.webapis.avplay, self = this;
      try { av.stop(); av.close(); } catch (e) {}
      document.getElementById('avplayer').classList.remove('hidden');
      av.open(url);
      av.setDisplayRect(0, 0, 1920, 1080);
      try { av.setDisplayMethod('PLAYER_DISPLAY_MODE_LETTER_BOX'); } catch (e) {}
      av.setListener({
        onbufferingstart: function () { listener.state('buffering'); },
        onbufferingcomplete: function () { listener.state('playing'); },
        oncurrentplaytime: function (ms) { listener.time(ms / 1000, self.duration()); },
        onstreamcompleted: function () { listener.ended(); },
        onerror: function (type) { listener.error(String(type || 'PLAYER_ERROR')); },
        onsubtitlechange: function (dur, text) { listener.subtitle(text || '', dur); }
      });
      if (!opts.live) { try { av.setTimeoutForBuffering(15); } catch (e) {} }
      listener.state('buffering');
      av.prepareAsync(function () {
        if (opts.start) { try { av.seekTo(Math.floor(opts.start * 1000)); } catch (e) {} }
        var subs = opts.englishSubs ? self.englishSubs() : false;
        av.play();
        listener.state('playing');
        listener.ready({ subs: subs });
      }, function (err) { listener.error(String((err && err.name) || err || 'PLAYER_ERROR')); });
    },
    englishSubs: function () {
      var av = root.webapis.avplay;
      try {
        var tracks = av.getTotalTrackInfo().filter(function (t) { return t.type === 'TEXT'; }).map(function (t) {
          var info = {}; try { info = JSON.parse(t.extra_info || '{}'); } catch (e) {}
          return { index: t.index, lang: info.track_lang || info.language || '' };
        });
        var en = pickEnglish(tracks);
        if (en) { av.setSelectTrack('TEXT', en.index); av.setSilentSubtitle(false); }
        return !!en;
      } catch (e) { return false; }
    },
    subDelay: function (sec) { try { root.webapis.avplay.setSubtitlePosition(Math.round(sec * 1000)); } catch (e) {} },
    subtitles: function (on) {
      if (on) return this.englishSubs();
      try { root.webapis.avplay.setSilentSubtitle(true); } catch (e) {}
      return false;
    },
    stop: function () {
      try { root.webapis.avplay.stop(); root.webapis.avplay.close(); } catch (e) {}
      document.getElementById('avplayer').classList.add('hidden');
    },
    toggle: function () {
      var av = root.webapis.avplay;
      if (av.getState() === 'PLAYING') { av.pause(); listener.state('paused'); } else { av.play(); listener.state('playing'); }
    },
    setPaused: function (on) { var av = root.webapis.avplay; try { if (on) { av.pause(); listener.state('paused'); } else { av.play(); listener.state('playing'); } } catch (e) {} },
    seekBy: function (s) { try { if (s > 0) root.webapis.avplay.jumpForward(s * 1000); else root.webapis.avplay.jumpBackward(-s * 1000); } catch (e) {} },
    duration: function () { try { return root.webapis.avplay.getDuration() / 1000; } catch (e) { return 0; } },
    position: function () { try { return root.webapis.avplay.getCurrentTime() / 1000; } catch (e) { return 0; } }
  };

  // ---------- Android (bridge to the native app; events come back via window.BBPlayerEvent) ----------
  var androidEngine = {
    open: function (url, opts) { androidEngine._paused = false; root.BeeldbuisAndroid.play(url, JSON.stringify(opts || {})); listener.state('buffering'); },
    stop: function () { try { root.BeeldbuisAndroid.stop(); } catch (e) {} },
    toggle: function () { root.BeeldbuisAndroid.toggle(); },
    setPaused: function (on) { if (on !== (androidEngine._paused || false)) { androidEngine._paused = on; root.BeeldbuisAndroid.toggle(); } },
    seekBy: function (s) { root.BeeldbuisAndroid.seekBy(s); },
    duration: function () { return androidEngine._d || 0; },
    position: function () { return androidEngine._t || 0; }
  };
  root.BBPlayerEvent = function (type, a, b) {
    if (type === 'time') { androidEngine._t = a; androidEngine._d = b; listener.time(a, b); }
    else if (type === 'state') listener.state(a);
    else if (type === 'ended') listener.ended();
    else if (type === 'error') listener.error(a);
  };

  // ---------- Browser ----------
  var hls = null;
  var browserEngine = {
    open: function (url, opts) {
      var v = document.getElementById('video');
      browserEngine.stop();
      v.classList.remove('hidden');
      v.onplaying = function () { listener.state('playing'); };
      v.onwaiting = function () { listener.state('buffering'); };
      v.onpause = function () { if (!v.ended) listener.state('paused'); };
      v.ontimeupdate = function () { listener.time(v.currentTime, isFinite(v.duration) ? v.duration : 0); };
      v.onended = function () { listener.ended(); };
      v.onerror = function () { listener.error('MEDIA_ERROR'); };
      listener.state('buffering');
      // the Mac's live buffer as one TS stream is for TV players; a browser plays the same session as HLS
      url = url.replace(/\/buffer\.ts\?behind=\d+$/, '/index.m3u8');
      if (/\.m3u8(\?|$)/.test(url) && root.Hls && root.Hls.isSupported()) {
        hls = new root.Hls({ liveSyncDurationCount: 4, maxBufferLength: 30 });
        hls.on(root.Hls.Events.ERROR, function (e, d) { if (d.fatal) listener.error(d.details || 'HLS_ERROR'); });
        hls.loadSource(P.xurl(url));
        hls.attachMedia(v);
      } else {
        v.src = P.xurl(url);
      }
      v.addEventListener('loadedmetadata', function once() {
        v.removeEventListener('loadedmetadata', once);
        if (opts.start) v.currentTime = opts.start;
        listener.ready({ subs: false });
      });
      var p = v.play(); if (p && p.catch) p.catch(function () {});
    },
    stop: function () {
      var v = document.getElementById('video');
      if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
      v.onerror = null; v.removeAttribute('src'); try { v.load(); } catch (e) {}
      v.classList.add('hidden');
    },
    toggle: function () { var v = document.getElementById('video'); if (v.paused) v.play(); else v.pause(); },
    setPaused: function (on) { var v = document.getElementById('video'); if (on) v.pause(); else { var p = v.play(); if (p && p.catch) p.catch(function () {}); } },
    seekBy: function (s) { var v = document.getElementById('video'); v.currentTime = Math.max(0, v.currentTime + s); },
    duration: function () { var v = document.getElementById('video'); return isFinite(v.duration) ? v.duration : 0; },
    position: function () { return document.getElementById('video').currentTime || 0; }
  };

  var engine = P.isTizen ? tizenEngine : P.isAndroid ? androidEngine : browserEngine;

  root.BBPlayer = {
    on: on,
    open: function (url, opts) { engine.open(url, opts || {}); },
    stop: function () { engine.stop(); },
    toggle: function () { engine.toggle(); },
    setPaused: function (on) { engine.setPaused(on); },
    seekBy: function (s) { engine.seekBy(s); },
    duration: function () { return engine.duration(); },
    position: function () { return engine.position(); },
    subtitles: function (on) { return engine.subtitles ? engine.subtitles(on) : false; },
    subDelay: function (sec) { if (engine.subDelay) engine.subDelay(sec); },
    pickEnglish: pickEnglish
  };
})(this);
