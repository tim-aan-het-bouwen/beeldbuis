# Beeldbuis tests (update 30)

Run these before every package. Both run offline in the cloud or on a Mac with ffmpeg, Python 3 and (for the UI) Node + Playwright.

```
python3 build.py
python3 test/regress.py                 # ~6 min: static checks, recordings, playback, settings (30 tests)
python3 test/regress.py rec             # one suite: static | rec | play | settings | guard
BASE=http://127.0.0.1:8765 node test/ui_smoke.js   # every tab on Mac 1440/1280 and iPhone, against a running server
node test/tv_nav.js                     # TV app remote control (arrows, Back, typing, subtitle menu) in a browser, ~20 s
BASE=http://127.0.0.1:8765 node test/ui_session.js # player and Mac keep each other informed: keep-alive ping, lost stream, sleep, closing the window (update 34)
node test/tv_vodmac.js <mac base> <fake provider base>   # Play on TV and films via the Mac (update 33)
python3 test/tv_extras.py                # typing on the TV, match data, calm home, screensaver (update 35), ~3 min
python3 test/tv_pair.py                  # pairing with a TV that asks for permission: secured port, token, new name (update 36), ~20 s
```

- `test/fake_provider.py` is a fake Xtream server that behaves like a one-connection subscription: a second stream
  silently ends the oldest one, like the real provider. It logs every stream connection, so a test can prove that
  Beeldbuis never needed two. Faults on demand: `/__ctl?fail=N` (503s), `stall=S` (no data), `drop=1` (EOF).
  Without settings it serves a small made-up catalogue; with `BEELDBUIS_UPSTREAM`, `BEELDBUIS_UP_USER`,
  `BEELDBUIS_UP_PASS` and `FAKE_CACHE` it caches your own provider's lists and guide once (streams stay local).
  Test films are generated with ffmpeg into `test/media/` on first start.
- `test/regress.py` starts its own fake provider and a fresh server in a temporary folder for each suite.
  Every bug that reached a real install gets a test here (empty `vod_langs` of update 24, two connections while
  recording, a recording that gave up after a provider hiccup, a recording that played from its live end, ...).
- `test/mock_meta.py` fakes TMDb/OMDb/OpenSubtitles/Letterboxd for the library and Tonight.

Update 34 added the `guard` suite (library while loading, paused film kept by the ping, POST /api/stop, requests from
another website refused on the Mac itself, provider unreachable) and `play: an episode whose file is broken ...`.
The UI tests need `pkg/assets` (a link to `../assets` is enough: `ln -s ../assets pkg/assets`), otherwise hls.js
does not load and nothing plays. `?bbtest=1` on the page URL exposes `window.__bb(code)` for the player tests.

Update 35 added `test/tv_extras.py` + `test/tv_extras.js` (39 checks). The Python part starts everything: the fake provider,
a Beeldbuis server on the LAN address, a made-up Samsung TV on 127.0.0.2:8001 that writes down the remote-API messages it
gets, made-up scores, weather and three photos. The test browser has no H.264, so the player is replaced by a stand-in
that only shows the picture box. Pass a folder as the first argument to keep screenshots.
