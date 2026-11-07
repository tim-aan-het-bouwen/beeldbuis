# Beeldbuis

A friendly IPTV player for Xtream Codes subscriptions: live TV with a full guide, catch-up,
recording, movies and series, and a home screen that shows what you actually want to watch.

> Beeldbuis is only a player. It contains no channels, streams or provider details:
> you sign in with your own Xtream Codes subscription.

## Platforms

| Platform | Status | Folder |
| --- | --- | --- |
| macOS (Apple Silicon) | Working (phase 1) | [`mac/`](mac/) |
| Samsung TV (Tizen) | Planned | `tv-samsung/` |
| Android phone + Android TV | Planned | `android/` |

The plan: one shared interface everywhere, with a player adapter per device
(Mac: local Python server + ffmpeg → HLS; Samsung: the TV's own player; Android: Media3/ExoPlayer).
Recording stays on the Mac.

## macOS

A small Python 3 server (standard library only) runs in the background and serves the app at
`http://127.0.0.1:8765`; open it in Chrome and choose *Install page as app*. Streams go through
ffmpeg to HLS and play with hls.js.

- `mac/src/server.py` + `mac/src/ui.html` → `python3 mac/build.py` → `mac/pkg/iptv.py`
- `mac/Update Beeldbuis.command`: double-click updater (copies `iptv.py` + `assets/`, restarts the service)
- `mac/test/`: a mock Xtream provider (`BEELDBUIS_UPSTREAM` optional, for real metadata) and a Playwright end-to-end flow

Your login, favourites and settings live in `~/Library/Application Support/IPTVPlayer/config.json`
and never in this repository.

## Credits

hls.js (Apache-2.0) is bundled in `mac/assets/hls.min.js`. Artwork and the Beeldbuis name are
the project's own.
