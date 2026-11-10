# Beeldbuis for TV

One TV app for **Samsung TVs (Tizen)** and, wrapped in a small native app, **Android TV**. It talks to the
Xtream Codes provider directly, so no computer has to be on to watch.

What it does now (v0.2):

- sign in with your own Xtream Codes details (kept on the TV only); type `demo` as server to look around
- choose which country groups to show, so the TV never has to load 50,000 channels
- Live TV per category, quality variants merged into one channel; Full HD is tried first, with a fallback to another quality if one fails
- search across channels, movies and series
- adult categories hidden
- Movies and Series with posters, details, seasons/episodes, resume and next episode
- Home: continue watching, favourite channels, recently watched, my movies & series
- remote only: arrows, OK, Back, CH▲▼ zaps, yellow = favourite, red = Live TV, ◀▶ seeks 10 s, ⏪⏩ 60 s
- English subtitles switched on when a film has them (Samsung player)
- one connection: before giving up on a stream it checks whether the subscription is in use elsewhere and says so

Not yet: the full TV guide, catch-up and recordings (those live in the Mac app for now).

## Try it in a browser

```sh
python3 tv/tools/serve.py --port 8091   # http://localhost:8091, server "demo"
node tv/test/e2e.js                      # remote-key walkthrough (Playwright) against that preview
python3 tv/test/test_groups_parity.py    # channel merging matches the Mac app
```

Keys in the preview: arrows, Enter = OK, Esc/Backspace = Back, PageUp/PageDown = CH▲▼, space = play/pause.
A real provider works too: the preview server passes its requests through `/proxy`.

## Samsung TV

`python3 tv/tools/build_wgt.py` makes `dist/Beeldbuis.wgt`. A Samsung TV only installs it signed, set up once per TV:

1. On the TV: *Apps* → type `12345` on the remote → Developer mode **On**, enter the computer's IP address, restart the TV.
2. On the computer: Tizen Studio with the *Samsung TV extension* and *Samsung Certificate Extension*;
   Certificate Manager → new Samsung certificate profile (needs a Samsung account) that includes the TV's DUID.
3. `sdb connect <tv-ip>` → `tizen package -t wgt -s <profile> -- dist/Beeldbuis.wgt` → `tizen install -n Beeldbuis.wgt -t <tv>`.

The app then sits in the TV's app list. It does not replace the Samsung home screen.

On a 2018 TV (Tizen 4) the current Samsung certificate profiles can be too new for the TV to accept. Signing with the
default Tizen author certificate from Tizen Studio works for a TV in developer mode.

Every release on GitHub also carries a ready-made `Beeldbuis.wgt` (unsigned; sign it as above before installing).

Written for the 2018 models (Tizen 4, Chromium 56): flexbox only (no CSS grid or flex `gap`), ES2017 JavaScript
(no `?.`, `??` or object spread). hls.js is only used by the browser preview and is left out of the `.wgt`.

## Android TV and phone

[`android/`](../android/) wraps this same interface in a WebView and plays streams with Media3 (ExoPlayer) through
the `BeeldbuisAndroid` bridge in `js/player.js`. GitHub Actions builds the APK; see the Android README for installing it.
