# Beeldbuis

A friendly IPTV player for Xtream Codes subscriptions: live TV with a full guide, catch-up,
recording, movies and series, and a home screen that shows what you actually want to watch.

> Beeldbuis is only a player. It contains no channels, streams or provider details:
> you sign in with your own Xtream Codes subscription.

## Platforms

| Platform | Status | Folder |
| --- | --- | --- |
| macOS (Apple Silicon) | Working (phases 1–3: live, catch-up, recording, library, home screen) | [`mac/`](mac/) |
| Samsung TV (Tizen) | First version, tested in the browser | [`tv/`](tv/) |
| Android TV + phone | First version (TV app in a native wrapper, built by GitHub Actions) | [`android/`](android/) |

The plan: one shared interface everywhere, with a player adapter per device
(Mac: local Python server + ffmpeg → HLS; Samsung: the TV's own player; Android: Media3/ExoPlayer).
Recording stays on the Mac.

## macOS

A small Python 3 server (standard library only) runs in the background and serves the app at
`http://127.0.0.1:8765`; open it in Chrome and choose *Install page as app*. Streams go through
ffmpeg to HLS and play with hls.js.

- `mac/src/server.py` + `mac/src/ui.html` → `python3 mac/build.py` → `mac/pkg/iptv.py`
- `mac/Update Beeldbuis.command`: double-click updater (copies `iptv.py` + `assets/`, restarts the service)
- `mac/test/`: a mock Xtream provider (`BEELDBUIS_UPSTREAM` optional, for real metadata), a mock for TMDb/OMDb/OpenSubtitles/Letterboxd
  (`mock_meta.py`, pointed at with `IPTV_TMDB_BASE` and friends) and two Playwright flows (`e2e.js` for live/catch-up/recording, `e2e_fase2.js` for the home screen)

Since update 8 (v9) the Mac app also has a home screen ("Tonight") with recommendations, posters and ratings,
search across everything, taste profiles and English subtitles from OpenSubtitles. That needs free API keys in a
`sleutels.txt` next to the update folder (`TMDB_API_KEY`, `TMDB_READ_TOKEN`, `OMDB_API_KEY`, `OPENSUBTITLES_API_KEY`,
optional `OPENSUBTITLES_USER`/`OPENSUBTITLES_PASSWORD` and `LETTERBOXD_USER`) and, optionally, a personal `smaak.json`
with taste profiles and a Letterboxd export zip. The updater copies those into the app folder; none of them belong in this repository.

Your login, favourites and settings live in `~/Library/Application Support/IPTVPlayer/config.json`
and never in this repository.

## Credits

hls.js (Apache-2.0) is bundled in `mac/assets/hls.min.js`. Artwork and the Beeldbuis name are
the project's own.
