#!/usr/bin/env python3
"""
Beeldbuis — local Xtream Codes player (server side).

Runs as a launchd background service; the window is a Chrome app on http://127.0.0.1:8765.
Options: --no-browser   do not open a browser
         --auto-exit    stop when the window has been closed for a while
         --ffmpeg PATH  path to ffmpeg (default: search PATH)
         --port N       port (default 8765)

Credentials are stored locally only, in:
  ~/Library/Application Support/IPTVPlayer/config.json
"""
import base64
import calendar
import secrets
import socket
import socketserver
import datetime
import gzip
import json
import os
import re
import shutil
import signal
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ARGS = sys.argv[1:]


def arg(name, default=None):
    if name in ARGS:
        i = ARGS.index(name)
        return ARGS[i + 1] if i + 1 < len(ARGS) else default
    return default


PORT = int(arg("--port") or os.environ.get("IPTV_PORT", "8765"))
HOST = "127.0.0.1"          # address the Mac itself uses
BIND = os.environ.get("IPTV_BIND", "::")   # listen on all networks so an iPhone on the same Wi-Fi can connect
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Beeldbuis/15.0"
AUTO_EXIT = "--auto-exit" in ARGS
VERSION = 36

def _config_dir():
    """Where settings, caches and personal files live. An existing install keeps its folder (the first
    installs were called IPTVPlayer); a fresh install gets a Beeldbuis folder. IPTV_CONFIG_DIR overrides."""
    if os.environ.get("IPTV_CONFIG_DIR"):
        return os.path.expanduser(os.environ["IPTV_CONFIG_DIR"])
    if sys.platform == "darwin":
        cands = ["~/Library/Application Support/Beeldbuis", "~/Library/Application Support/IPTVPlayer"]
    else:
        cands = ["~/.config/beeldbuis", "~/.config/iptvplayer"]
    for c in cands:
        if os.path.isdir(os.path.expanduser(c)):
            return os.path.expanduser(c)
    return os.path.expanduser(cands[0])


CONFIG_DIR = _config_dir()
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "ffmpeg.log")
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

FFMPEG = arg("--ffmpeg") or os.environ.get("IPTV_FFMPEG") or shutil.which("ffmpeg")
if FFMPEG and not os.path.isfile(FFMPEG):
    FFMPEG = None

STATE = {"server": "", "username": "", "password": "", "favorites": [], "recent": [], "settings": {}, "positions": {}, "server_tz": "",
         "devices": {}, "pair_code": "", "dead_vod": {}, "account": {}}
LOCK = threading.Lock()
PROBE_CACHE = {}
PROCS = set()
SESSIONS = {}   # sid -> {"proc","dir","t"}
SESS_LOCK = threading.Lock()
HLS_DIR = os.path.join(CONFIG_DIR, "hls")
LAST_PING = {"t": time.time()}
LAST_REQ = {"t": time.time()}
START = time.time()
WINDOW = {"proc": None}

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE


def _api_ssl_ctx():
    """Verified HTTPS for TMDb, OMDb, OpenSubtitles, Letterboxd and ESPN. The Python that launchd starts often has no
    CA certificates of its own (python.org builds), so every https call failed silently: use certifi or macOS's own bundle."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa
        pass
    ctx = ssl.create_default_context()
    try:
        if not ctx.cert_store_stats().get("x509_ca"):
            for f in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem", "/opt/homebrew/etc/ca-certificates/cert.pem", "/usr/local/etc/openssl@3/cert.pem"):
                if os.path.isfile(f):
                    ctx.load_verify_locations(cafile=f)
                    break
    except Exception:  # noqa
        pass
    return ctx


API_SSL = _api_ssl_ctx()


# ---------------------------------------------------------------- config ----
def load_config():
    """Reads config.json. A file that cannot be read (half written by an older version, disk trouble) must not cost the
    login and every setting: the copy made at the last good start (config.json.bak) is used instead."""
    for fp in (CONFIG_PATH, CONFIG_PATH + ".bak"):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a config")
        except (OSError, ValueError) as e:
            if os.path.exists(fp):
                log_write("[config] %s could not be read (%s)\n" % (os.path.basename(fp), e))
            continue
        with LOCK:
            for k in STATE:
                if k in data and isinstance(data[k], type(STATE[k])):
                    STATE[k] = data[k]
            # only films were ever marked here before update 34; a failed episode used the series' number range
            STATE["dead_vod"] = {k: t for k, t in STATE["dead_vod"].items() if not str(k).startswith("series:")}
            good = bool(STATE["server"])
        if fp == CONFIG_PATH and good:
            try:
                shutil.copyfile(fp, CONFIG_PATH + ".bak.tmp")
                os.chmod(CONFIG_PATH + ".bak.tmp", 0o600)
                os.replace(CONFIG_PATH + ".bak.tmp", CONFIG_PATH + ".bak")
            except OSError:
                pass
        elif fp != CONFIG_PATH:
            log_write("[config] restored from the backup of the last good start\n")
        break
    settings_repair()


def settings_repair():
    """Updates 24-29 could save Settings before the form was filled (two loadTaste functions in the UI): that wrote
    vod_langs=[] (every movie and series hidden) and an empty profile. An empty language list is the sign; undo it once."""
    with LOCK:
        st = STATE.get("settings")
        if not isinstance(st, dict) or st.get("vod_langs") != []:
            return
        st.pop("vod_langs", None)
        if st.get("profile") == "":
            st.pop("profile", None)


SAVE_LOCK = threading.Lock()


def save_config():
    """Writes config.json. Serialised while holding LOCK (another thread may be changing the settings), written to a
    file of its own and swapped in under SAVE_LOCK, so two saves at once can neither clash nor leave half a file."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with SAVE_LOCK:
        with LOCK:
            text = json.dumps(STATE, indent=2)
        tmp = "%s.%d.%d.tmp" % (CONFIG_PATH, os.getpid(), threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, CONFIG_PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass


# ---------------------------------------------------------------- xtream ----
def open_url(url, extra_headers=None, timeout=30):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def xtream(action=None, _timeout=90, **params):
    with LOCK:
        server, user, pw = STATE["server"], STATE["username"], STATE["password"]
    if not server:
        raise RuntimeError("Not logged in")
    q = {"username": user, "password": pw}
    if action:
        q["action"] = action
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    url = server.rstrip("/") + "/player_api.php?" + urllib.parse.urlencode(q)
    with open_url(url, timeout=_timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("The server did not return a valid answer (is this an Xtream Codes server?)")


ACCOUNT = {"t": 0, "ui": None, "err": None, "busy": False}
ACCOUNT_LOCK = threading.Lock()
ACCOUNT_KEEP = ("auth", "status", "exp_date", "max_connections", "active_cons", "is_trial")


def account_set(info):
    ui = info.get("user_info", {}) if isinstance(info, dict) else {}
    ui = {k: ui.get(k) for k in ACCOUNT_KEEP if k in ui}
    with ACCOUNT_LOCK:
        ACCOUNT.update(t=time.time(), ui=ui, err=None)
    with LOCK:
        old = STATE.get("account") or {}
        new = {k: v for k, v in ui.items() if k != "active_cons"}
        changed = new != old
        if changed:
            STATE["account"] = new
    if changed:
        try:
            save_config()
        except OSError:
            pass
    return ui


ACCOUNT_AGE = float(os.environ.get("IPTV_ACCOUNT_AGE", "300"))


def account_info(max_age=None, timeout=8):
    """(account record, error): the provider's own record of this subscription (status, end date, connections).
    Asked at most every few minutes and with a short timeout, because the window waits for it when it opens. When the
    provider does not answer, the last known record comes back with the error: recordings, the guide and the library
    are on this Mac and must keep working while the provider (or the internet) is away."""
    max_age = ACCOUNT_AGE if max_age is None else max_age
    with ACCOUNT_LOCK:
        ui, age, err = ACCOUNT["ui"], time.time() - ACCOUNT["t"], ACCOUNT["err"]
    if ui is not None and age < (30 if err else max_age):
        return ui, err
    try:
        return account_set(xtream(_timeout=timeout)), None
    except RuntimeError as e:
        err = str(e)
    except urllib.error.HTTPError as e:
        err = "Your provider answered with HTTP %s" % e.code
    except Exception as e:  # noqa
        err = "Your provider does not answer (%s)" % redact(str(getattr(e, "reason", e)))[:120]
    with LOCK:
        last = dict(STATE.get("account") or {})
    with ACCOUNT_LOCK:
        ACCOUNT.update(t=time.time(), ui=last, err=err)
    return last, err


def stream_url(kind, sid, ext):
    with LOCK:
        server, user, pw = STATE["server"], STATE["username"], STATE["password"]
    return "%s/%s/%s/%s/%s.%s" % (server.rstrip("/"), kind, user, pw, sid, ext)


def redact(s):
    """Hide the provider login in anything we log or show (stream URLs contain it)."""
    user, pw = STATE.get("username") or "", STATE.get("password") or ""
    for secret in (pw, user):
        if secret and len(secret) >= 3:
            s = s.replace(secret, "***")
    return s


def log_write(text):
    try:
        with open(LOG_PATH, "ab") as f:
            f.write(redact(text).encode("utf-8", "replace"))
    except OSError:
        pass


def log_pipe(proc):
    """Copy ffmpeg's stderr to ffmpeg.log with the login hidden."""
    def run():
        try:
            for line in iter(proc.stderr.readline, b""):
                log_write(line.decode("utf-8", "replace"))
        except Exception:  # noqa
            pass
        finally:
            try:
                proc.stderr.close()
            except Exception:  # noqa
                pass
    threading.Thread(target=run, daemon=True).start()


def normalize_server(s):
    s = s.strip()
    if not s:
        return s
    if not s.startswith(("http://", "https://")):
        s = "http://" + s
    p = urllib.parse.urlsplit(s)
    return "%s://%s" % (p.scheme, p.netloc)


# ---------------------------------------------------------------- ffmpeg ----
COPY_VIDEO = ("h264", "hevc", "av1", "vp9")
COPY_AUDIO = ("aac",)
TEXT_SUBS = ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "ttml")
_STREAM_RE = re.compile(r"Stream #0:(\d+)(?:\[0x[0-9a-f]+\])?(?:\((\w+)\))?[^\n]*?: (Video|Audio|Subtitle): (\w+)([^\n]*)")


def is_local(target):
    return not target.startswith(("http://", "https://"))


_RETRY_RE = re.compile(r"5\d\d|Server returned|Invalid data found|Connection reset|End of file|prematurely|Connection timed out", re.I)


LIVE_PROBE = {"d": None}
LIVE_PROBE_LOCK = threading.Lock()


def _live_probe_path():
    return os.path.join(CONFIG_DIR, "live_probe.json")


def live_probe_get(sid):
    """Codec info of a live stream from an earlier start (kept on disk for 7 days). Skipping the separate probe saves a
    provider connection and several seconds per channel start; if the stream changed, play_api probes again."""
    with LIVE_PROBE_LOCK:
        if LIVE_PROBE["d"] is None:
            try:
                with open(_live_probe_path(), "r", encoding="utf-8") as f:
                    LIVE_PROBE["d"] = json.load(f)
            except (OSError, ValueError):
                LIVE_PROBE["d"] = {}
        e = LIVE_PROBE["d"].get(str(sid))
    if e and time.time() - e.get("t", 0) < 7 * 86400 and isinstance(e.get("info"), dict):
        return dict(e["info"])
    return None


def live_probe_put(sid, info):
    with LIVE_PROBE_LOCK:
        d = LIVE_PROBE["d"] if LIVE_PROBE["d"] is not None else {}
        if info is None:
            d.pop(str(sid), None)
        else:
            d[str(sid)] = {"t": int(time.time()), "info": {k: v for k, v in info.items() if not k.startswith("_")}}
        if len(d) > 800:
            for k in sorted(d, key=lambda k: d[k].get("t", 0))[:len(d) - 800]:
                d.pop(k, None)
        LIVE_PROBE["d"] = d
        try:
            tmp = _live_probe_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f)
            os.replace(tmp, _live_probe_path())
        except OSError:
            pass


def probe(target, cache=True):
    """Quick look at the codecs so we convert as little as possible (and find English subtitles).
    The provider sometimes answers 503 / garbage for a moment even with no connection in use, so retry briefly."""
    if cache and target in PROBE_CACHE:
        return PROBE_CACHE[target]
    for attempt in range(3):
        info = _probe_once(target)
        if not info["error"] or is_local(target) or attempt == 2 or not _RETRY_RE.search(info["error"]):
            break
        time.sleep(2.5)
    if not info["error"] and cache:
        PROBE_CACHE[target] = info
    return info


def _probe_once(target):
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-user_agent", UA, "-rw_timeout", "12000000", "-analyzeduration", "3000000",
           "-probesize", "6000000", "-i", target, "-t", "0.01", "-f", "null", "-"]
    if is_local(target):
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-i", target, "-t", "0.01", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        err = r.stderr
    except subprocess.TimeoutExpired as e:
        err = (e.stderr or b"").decode("utf-8", "replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    info = {"vcodec": None, "acodec": None, "duration": 0, "container": None, "error": None,
            "audio": [], "subs": [], "width": 0, "height": 0}
    err = err.split("Output #0")[0].split("Stream mapping:")[0]      # only the input side
    m = re.search(r"Input #0, ([^,\s]+)", err)
    if m:
        info["container"] = m.group(1)
    for m in _STREAM_RE.finditer(err):
        idx, lang, kind, codec, rest = int(m.group(1)), (m.group(2) or "").lower(), m.group(3), m.group(4).lower(), m.group(5)
        if kind == "Video" and info["vcodec"] is None:
            info["vcodec"] = codec
            mm = re.search(r"(\d{2,5})x(\d{2,5})", rest)
            if mm:
                info["width"], info["height"] = int(mm.group(1)), int(mm.group(2))
        elif kind == "Audio":
            if info["acodec"] is None:
                info["acodec"] = codec
            info["audio"].append({"i": len(info["audio"]), "codec": codec, "lang": lang})
        elif kind == "Subtitle":
            info["subs"].append({"i": len(info["subs"]), "codec": codec, "lang": lang, "text": codec in TEXT_SUBS})
    m = re.search(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)", err)
    if m:
        info["duration"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if not info["vcodec"] and not info["acodec"]:
        last = [l for l in err.strip().splitlines() if l.strip()]
        info["error"] = redact(last[-1][:200]) if last else "Stream not reachable"
    # Which English text subtitle (if any) and which audio track to use
    eng = [s for s in info["subs"] if s["text"] and s["lang"] in ("eng", "en", "english")]
    info["sub"] = eng[0]["i"] if eng else None
    info["sub_langs"] = sorted({s["lang"] for s in info["subs"] if s["text"]})
    aud = None
    if len(info["audio"]) > 1:
        non_nl = [a for a in info["audio"] if a["lang"] not in ("dut", "nld", "nl", "dutch")]
        aud = (non_nl or info["audio"])[0]["i"]
    info["aud"] = aud
    v_ok = info["vcodec"] in COPY_VIDEO or info["vcodec"] is None
    a_ok = info["acodec"] in COPY_AUDIO or info["acodec"] is None
    if info["container"] in ("mov", "mp4", "m4a") and v_ok and a_ok and info["duration"] > 0 and info["sub"] is None and aud is None:
        info["mode"] = "direct"      # the browser can play the file itself (with native seeking)
    elif v_ok:
        info["mode"] = "copy"        # only repackage (and convert audio if needed)
    else:
        info["mode"] = "transcode"   # re-encode video (mpeg2 etc.)
    return info


def ffmpeg_cmd(target, info, outdir, start=0.0, force=False, live=True, follow=False, subs=True):
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning"]
    local = is_local(target)
    if not local:
        cmd += ["-user_agent", UA, "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"]
    if info.get("_known") and live and not follow:     # codecs already known: open the stream faster
        cmd += ["-fflags", "+genpts+discardcorrupt", "-analyzeduration", "1500000", "-probesize", "3000000"]
    else:
        cmd += ["-fflags", "+genpts+discardcorrupt", "-analyzeduration", "3000000", "-probesize", "6000000"]
    if follow:
        cmd += ["-follow", "1"]
    if start > 0:
        cmd += ["-ss", "%.2f" % start]
    cmd += ["-i", target, "-map", "0:v:0?"]
    if info.get("aud") is not None:
        cmd += ["-map", "0:a:%d" % info["aud"]]
    else:
        cmd += ["-map", "0:a:0?"]
    use_sub = subs and info.get("sub") is not None and not live
    if use_sub:
        cmd += ["-map", "0:s:%d" % info["sub"], "-c:s", "webvtt"]
    else:
        cmd += ["-sn"]
    cmd += ["-dn"]
    vcopy = (info.get("vcodec") in COPY_VIDEO) and not force
    if vcopy:
        cmd += ["-c:v", "copy"]
        if info.get("vcodec") == "hevc":
            cmd += ["-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency", "-crf", "23",
                "-pix_fmt", "yuv420p", "-vf", "yadif=deint=interlaced,scale=-2:'min(1080,ih)'",
                "-profile:v", "high", "-force_key_frames", "expr:gte(t,n_forced*2)", "-sc_threshold", "0"]
    if info.get("acodec") in COPY_AUDIO and not force:
        cmd += ["-c:a", "copy", "-bsf:a", "aac_adtstoasc"]
    else:
        cmd += ["-c:a", "aac", "-ac", "2", "-b:a", "160k", "-af", "aresample=async=1"]
    cmd += ["-f", "hls", "-hls_time", "2", "-hls_segment_type", "fmp4",
            "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", os.path.join(outdir, "seg_%05d.m4s")]
    if live:
        # live buffer: keep the last N minutes as segments, so live TV can be paused and rewound (Settings → Live TV)
        cmd += ["-hls_list_size", str(max(30, live_buffer_min() * 30)), "-hls_delete_threshold", "15",
                "-hls_flags", "delete_segments+omit_endlist+independent_segments"]
    else:
        cmd += ["-hls_list_size", "0", "-hls_playlist_type", "event", "-hls_flags", "independent_segments"]
    cmd += [os.path.join(outdir, "index.m3u8")]
    return cmd


def live_buffer_min():
    """Minutes of live TV kept on disk while watching (pause and rewind). 0 = only a short safety buffer."""
    with LOCK:
        v = STATE["settings"].get("live_buffer", 30)
    try:
        return max(0, min(120, int(v)))
    except (TypeError, ValueError):
        return 30


def buffer_segments(sess):
    """Segments of a live session that are complete, oldest first: [(name, seconds)]."""
    try:
        txt = open(os.path.join(sess["dir"], "index.m3u8")).read()
    except OSError:
        return []
    out, dur = [], 2.0
    for line in txt.splitlines():
        if line.startswith("#EXTINF:"):
            try:
                dur = float(line[8:].split(",")[0])
            except ValueError:
                dur = 2.0
        elif line and not line.startswith("#"):
            out.append((line.strip(), dur))
    return out


def buffer_info(sess):
    segs = buffer_segments(sess)
    return {"length": round(sum(d for _, d in segs), 1), "max": live_buffer_min() * 60, "live": bool(sess.get("live"))}


def stop_session(sid):
    with SESS_LOCK:
        sess = SESSIONS.pop(sid, None)
    if not sess:
        return
    try:
        sess["proc"].kill()
        sess["proc"].wait(timeout=3)
    except Exception:  # noqa
        pass
    PROCS.discard(sess["proc"])
    shutil.rmtree(sess["dir"], ignore_errors=True)


def stop_all_sessions(remote_only=False, taken_by=None, owner=None):
    """Stop streams. taken_by=(id, name): a new stream of that device replaces them (one connection);
    owner: only stop the streams of that device."""
    for sid, sess in list(SESSIONS.items()):
        if remote_only and sess.get("local"):
            continue
        if owner is not None and sess.get("owner") != owner:
            continue
        if taken_by and sess.get("owner") and sess.get("owner") != taken_by[0]:
            TAKEN[sess["owner"]] = {"by": taken_by[1], "t": time.time()}
        stop_session(sid)
    if taken_by:
        for dev, gen in list(DIRECT.items()):
            if dev != taken_by[0] and gen.get("n"):
                gen["stop"] += 1
                TAKEN[dev] = {"by": taken_by[1], "t": time.time()}
                direct_close(gen)


def start_session(target, info, start, force, live, follow=False, owner=("mac", "Mac"), min_segs=1, ext_sub=None):
    """Start ffmpeg writing HLS segments; returns (sid, playlist, error) once the playlist is usable."""
    stop_all_sessions(taken_by=owner)   # one stream at a time (the subscription allows one connection)
    sid = "%d-%s" % (int(time.time() * 1000), secrets.token_hex(8))   # unguessable: the HLS files are served by session id
    outdir = os.path.join(HLS_DIR, sid)
    os.makedirs(outdir, exist_ok=True)
    cmd = ffmpeg_cmd(target, info, outdir, start, force, live, follow)
    log_write("\n--- %s %s\n%s\n" % (time.strftime("%H:%M:%S"), sid, " ".join(cmd[:-12])))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
    log_pipe(proc)
    PROCS.add(proc)
    with SESS_LOCK:
        SESSIONS[sid] = {"sid": sid, "proc": proc, "dir": outdir, "t": time.time(), "local": is_local(target), "owner": owner[0], "live": live}
    emb_sub = info.get("sub") is not None and not live
    ext_ok = bool(ext_sub) and not live and not emb_sub
    use_sub = emb_sub or ext_ok
    pl = os.path.join(outdir, "index.m3u8")
    if ext_ok:
        # downloaded English subtitles (OpenSubtitles): one WebVTT file as a single-segment subtitle playlist, shifted for -ss
        try:
            vtt = open(ext_sub, encoding="utf-8").read()
            if start > 0:
                vtt = vtt_shift(vtt, -start)
            with open(os.path.join(outdir, "ext.vtt"), "w", encoding="utf-8") as f:
                f.write(vtt)
            dur = int(max(60, (info.get("duration") or 0) + 5)) or 36000
            with open(os.path.join(outdir, "index_vtt.m3u8"), "w") as f:
                f.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:%d\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:%d,\next.vtt\n#EXT-X-ENDLIST\n" % (dur, dur))
        except OSError:
            ext_ok = use_sub = emb_sub
    if use_sub:
        # our own master playlist: video/audio + the English WebVTT track (ffmpeg's, or the downloaded one)
        with open(os.path.join(outdir, "master.m3u8"), "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:7\n"
                    "#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID=\"subs\",NAME=\"English\",LANGUAGE=\"en\",DEFAULT=YES,AUTOSELECT=YES,URI=\"index_vtt.m3u8\"\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=12000000,SUBTITLES=\"subs\"\nindex.m3u8\n")
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            txt = open(pl).read()
            if txt.count(".m4s") >= min_segs and (not use_sub or os.path.exists(os.path.join(outdir, "index_vtt.m3u8"))):
                return sid, ("master.m3u8" if use_sub else "index.m3u8"), None
        except OSError:
            pass
        if proc.poll() is not None:   # finished already (short file) or failed
            try:
                if open(pl).read().count(".m4s") >= 1:
                    return sid, ("master.m3u8" if use_sub and os.path.exists(os.path.join(outdir, "index_vtt.m3u8")) else "index.m3u8"), None
            except OSError:
                pass
            stop_session(sid)
            return None, None, "The stream could not be started (see ffmpeg.log)"
        time.sleep(0.25)
    stop_session(sid)
    return None, None, "The stream is not responding (timeout)"


SESSION_IDLE = float(os.environ.get("IPTV_SESSION_IDLE", "150"))     # seconds without a request or a ping from the player before a stream is cleaned up


def session_janitor():
    while True:
        time.sleep(10)
        now = time.time()
        for sid, sess in list(SESSIONS.items()):
            if now - sess["t"] > SESSION_IDLE and not sess.get("readers"):
                stop_session(sid)


def kill_all():
    for p in list(PROCS):
        try:
            p.kill()
        except Exception:  # noqa
            pass


# -------------------------------------------------------------- catch-up ----
DEFAULT_TZ = "Europe/Amsterdam"


def server_tz():
    """Time zone the provider uses in timeshift URLs. The provider reports "UTC" in server_info but actually counts
    in Dutch time (checked against real catch-up on 7 Nov 2026), so this is a setting (catchup_tz) with a Dutch default."""
    with LOCK:
        name = (STATE["settings"].get("catchup_tz") or "").strip() or DEFAULT_TZ
    import zoneinfo
    for cand in (name, DEFAULT_TZ):
        try:
            return zoneinfo.ZoneInfo(cand)
        except Exception:  # noqa
            continue
    return datetime.timezone.utc


def timeshift_url(sid, start_ts, minutes):
    with LOCK:
        server, user, pw = STATE["server"], STATE["username"], STATE["password"]
    dt = datetime.datetime.fromtimestamp(int(start_ts), tz=server_tz())
    return "%s/timeshift/%s/%s/%d/%s/%s.ts" % (server.rstrip("/"), user, pw, int(minutes), dt.strftime("%Y-%m-%d:%H-%M"), sid)


# ------------------------------------------------------------- recordings ----
if sys.platform == "darwin":
    REC_DIR = os.path.expanduser("~/Movies/Beeldbuis")
else:
    REC_DIR = os.path.join(CONFIG_DIR, "recordings")
REC_PATH = os.path.join(CONFIG_DIR, "recordings.json")
RECS = {"items": [], "procs": {}, "watch": {}}   # procs: rec id -> Popen; watch: rec id -> [size, t last growth, t spawned]
REC_LOCK = threading.Lock()
REC_WARN_SECONDS = 120
REC_STALL_SECONDS = int(os.environ.get("IPTV_REC_STALL", "45"))   # a recording file that has not grown this long: dead stream, restart
REC_BACKOFF = (2, 4, 8, 15, 30)   # seconds between restarts of a recording whose stream keeps failing (never give up while it is on)
REC_MIN_FREE = 2 * 1024 ** 3      # below this much free disk space a recording gets a warning


def rec_take_connection():
    """A recording needs the one provider connection: end every stream that uses it (HLS sessions of all devices and
    direct movie streams), except local playback of recordings, which needs no connection. Returns True when
    something was stopped, so the caller can give the provider a moment to notice before connecting."""
    stopped = False
    for sid, sess in list(SESSIONS.items()):
        if sess.get("local"):
            continue
        if sess.get("owner") and sess.get("owner") != "mac":
            TAKEN[sess["owner"]] = {"by": "a recording", "t": time.time()}
        stop_session(sid)
        stopped = True
    for dev, gen in list(DIRECT.items()):
        if gen.get("n"):
            gen["stop"] += 1
            stopped = True
            direct_close(gen)
    if stopped:
        time.sleep(1.0)
    return stopped


def direct_close(gen):
    """Close the provider side of a device's direct streams right away (the proxy loop may be blocked writing to a slow
    browser, and would otherwise keep the provider connection open)."""
    for resp in list(gen.get("resps") or ()):
        try:
            resp.close()
        except Exception:  # noqa
            pass


def rec_load():
    try:
        with open(REC_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with REC_LOCK:
            RECS["items"] = data.get("items", [])
            now = time.time()
            for r in RECS["items"]:
                if r.get("status") == "recording":    # we were restarted while recording
                    if now < rec_window(r)[1] - 30:
                        r["resume"] = True            # still on: the scheduler continues it in the same file
                        continue
                    r["status"] = "done" if r.get("file") and os.path.exists(r["file"]) else "failed"
                    r.setdefault("error", "Interrupted by a restart")
    except (OSError, ValueError):
        pass


def rec_save():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with REC_LOCK:
        data = {"items": RECS["items"]}
        tmp = REC_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, REC_PATH)


def rec_settings():
    with LOCK:
        st = dict(STATE["settings"])
    return int(st.get("rec_before", 2)) * 60, int(st.get("rec_after", 10)) * 60


def rec_sport_extra():
    """Extra minutes at the end of a live sport recording (overtime, extra time, a late finish): Settings → Recordings."""
    with LOCK:
        v = STATE["settings"].get("rec_after_sport", 30)
    try:
        return max(0, min(180, int(v))) * 60
    except (TypeError, ValueError):
        return 30 * 60


def rec_is_live_sport(title, channel=""):
    try:
        return is_sport_text("%s %s" % (title or "", channel or "")) and not _REPLAY_RE.search((title or "").lower())
    except Exception:  # noqa
        return False


def rec_window(r):
    return r["start"] - r.get("pad_before", 0), r["stop"] + r.get("pad_after", 0)


def rec_active():
    with REC_LOCK:
        return [r for r in RECS["items"] if r["status"] == "recording"]


def rec_upcoming(within):
    now = time.time()
    with REC_LOCK:
        up = [r for r in RECS["items"] if r["status"] == "scheduled" and 0 <= rec_window(r)[0] - now <= within]
    up.sort(key=lambda r: r["start"])
    return up[0] if up else None


def rec_blocking():
    """The recording that currently owns the provider connection, if any."""
    act = rec_active()
    return act[0] if act else None


def safe_name(s):
    s = re.sub(r"[\\/:*?\"<>|]+", " ", s or "").strip()
    return re.sub(r"\s+", " ", s)[:80] or "Recording"


def rec_schedule(body):
    sid = int(body.get("stream_id"))
    start, stop = int(body.get("start")), int(body.get("stop"))
    if stop <= start:
        raise RuntimeError("Invalid programme times")
    now = int(time.time())
    if stop < now:
        raise RuntimeError("This programme has already finished (use catch-up instead)")
    pb, pa = rec_settings()
    rec = {"id": "%d" % int(time.time() * 1000), "stream_id": sid, "group": body.get("group") or "", "channel": body.get("channel") or "",
           "title": body.get("title") or "Recording", "description": body.get("description") or "", "start": start, "stop": stop,
           "pad_before": pb if start - pb > now else max(0, start - now), "pad_after": pa, "status": "scheduled", "file": "",
           "size": 0, "duration": 0, "error": None, "created": now, "epg": body.get("epg") or "", "icon": body.get("icon") or "",
           "manual": bool(body.get("manual"))}
    notes = []
    if rec_is_live_sport(rec["title"], rec["channel"]) and rec_sport_extra() > pa:
        rec["pad_after"] = rec_sport_extra()      # matches run late: keep recording a while after the planned end
        rec["sport"] = True
        notes.append("Live sport: recording %d min longer than planned" % (rec["pad_after"] // 60))
    with REC_LOCK:
        for o in RECS["items"]:
            if o["status"] not in ("scheduled", "recording"):
                continue
            a1, b1 = rec_window(rec)
            a2, b2 = rec_window(o)
            if b1 <= a2 or b2 <= a1:
                continue
            # overlap: if it is only the padding, trim it (back-to-back programmes); otherwise refuse
            if rec["start"] >= o["stop"]:
                o["pad_after"] = min(o["pad_after"], max(0, rec["start"] - o["stop"]))
                rec["pad_before"] = min(rec["pad_before"], max(0, rec["start"] - o["stop"] - o["pad_after"]))
                notes.append("Padding trimmed because '%s' ends right before" % o["title"])
            elif rec["stop"] <= o["start"]:
                rec["pad_after"] = min(rec["pad_after"], max(0, o["start"] - rec["stop"]))
                o["pad_before"] = min(o["pad_before"], max(0, o["start"] - rec["stop"] - rec["pad_after"]))
                notes.append("Padding trimmed because '%s' starts right after" % o["title"])
            else:
                raise RuntimeError("Overlaps with the recording of '%s' (%s). The subscription allows one connection; cancel that one first."
                                   % (o["title"], time.strftime("%a %H:%M", time.localtime(o["start"]))))
        RECS["items"].append(rec)
    rec_save()
    rec["notes"] = notes
    return rec


def rec_extend(rid, minutes):
    """Record longer than planned (a match going into overtime): adds minutes at the end, up to the next recording."""
    minutes = max(1, min(240, int(minutes or 30)))
    with REC_LOCK:
        rec = next((r for r in RECS["items"] if r["id"] == rid), None)
        if not rec or rec["status"] not in ("scheduled", "recording"):
            raise RuntimeError("This recording is not running or scheduled")
        end = rec_window(rec)[1] + minutes * 60
        nxt = [o for o in RECS["items"] if o is not rec and o["status"] == "scheduled" and rec_window(o)[0] >= rec_window(rec)[1] - 1]
        note = ""
        if nxt:
            first = min(nxt, key=lambda o: rec_window(o)[0])
            if end > rec_window(first)[0]:
                end = max(rec_window(rec)[1], rec_window(first)[0])
                note = "Stops when '%s' starts recording" % first["title"]
        rec["pad_after"] = max(0, int(end - rec["stop"]))
    rec_save()
    out = dict(rec)
    out["note"] = note
    return out


def rec_title_from_guide(rec):
    """A recording made without guide info (a manual block) takes the programme title(s) once the guide has them."""
    if not rec.get("manual"):
        return False
    eid = rec.get("epg") or ""
    g = CATALOG["by_key"].get(rec.get("group") or "")
    if g and g.get("epg"):
        eid = g["epg"]
    if not eid:
        return False
    try:
        lst = epg_rows([eid], rec["start"], rec["stop"]).get(eid, [])
    except Exception:  # noqa
        return False
    span = []
    for p in lst:
        ov = min(p["stop"], rec["stop"]) - max(p["start"], rec["start"])
        if ov >= 10 * 60 or ov >= (rec["stop"] - rec["start"]) * 0.5:
            span.append((ov, p))
    if not span:
        return False
    best = max(span, key=lambda x: x[0])[1]
    titles = []
    for _, p in span:
        if p["title"] and p["title"] not in titles:
            titles.append(p["title"])
    with REC_LOCK:
        rec["title"] = " + ".join(titles[:2]) + (" + more" if len(titles) > 2 else "")
        rec["description"] = best.get("description") or rec.get("description") or ""
        rec["epg"] = eid
        rec["manual"] = False
        rec["from_guide"] = True
    return True


def rec_cancel(rid):
    with REC_LOCK:
        rec = next((r for r in RECS["items"] if r["id"] == rid), None)
    if not rec:
        raise RuntimeError("Recording not found")
    if rec["status"] == "recording":
        rec_stop(rec, cancelled=True)
    elif rec["status"] == "scheduled":
        rec["status"] = "cancelled"
    rec_save()
    return rec


def rec_delete(rid):
    with REC_LOCK:
        rec = next((r for r in RECS["items"] if r["id"] == rid), None)
    if not rec:
        raise RuntimeError("Recording not found")
    if rec["status"] == "recording":
        rec_stop(rec, cancelled=True)
    for fp in (rec.get("file"), (rec.get("file") or "")[:-3] + ".ts", (rec.get("file") or "")[:-4] + ".ts"):
        if fp and os.path.isfile(fp):
            try:
                os.remove(fp)
            except OSError:
                pass
    with REC_LOCK:
        RECS["items"] = [r for r in RECS["items"] if r["id"] != rid]
    with LOCK:
        STATE["positions"].pop("rec:" + rid, None)
    rec_save()
    save_config()
    return True


def rec_spawn(rec, append=False):
    url = stream_url("live", rec["stream_id"], "ts")
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-user_agent", UA, "-rw_timeout", "20000000",
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
           "-fflags", "+genpts+discardcorrupt", "-i", url, "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-ignore_unknown",
           "-c", "copy", "-f", "mpegts", "pipe:1"]
    log_write("\n--- %s REC%s %s -> %s\n" % (time.strftime("%H:%M:%S"), " restart" if append else "", rec["id"], rec["file"]))
    os.makedirs(os.path.dirname(rec["file"]) or REC_DIR, exist_ok=True)   # the folder or file may have been removed meanwhile
    out = open(rec["file"], "ab" if append else "wb")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    out.close()
    log_pipe(proc)
    PROCS.add(proc)
    now = time.time()
    with REC_LOCK:
        RECS["procs"][rec["id"]] = proc
        RECS["watch"][rec["id"]] = [rec_file_size(rec), now, now]
    return proc


def rec_file_size(rec):
    try:
        return os.path.getsize(rec["file"]) if rec.get("file") else -1
    except OSError:
        return -1


def rec_watchdog(rec, proc, now):
    """True when the running ffmpeg is stuck: the file stopped growing (dead connection) or is gone (moved to the Trash)."""
    w = RECS["watch"].get(rec["id"])
    if not w:
        return False
    size = rec_file_size(rec)
    if size < 0:
        log_write("\n--- %s REC %s: file is gone, starting a new one\n" % (time.strftime("%H:%M:%S"), rec["id"]))
        return True
    if size > w[0]:
        w[0], w[1] = size, now
        if rec.get("restarts") and now - w[2] > 600:     # ran fine for 10 minutes: earlier hiccups no longer count
            rec["restarts"] = 0
        return False
    if now - w[1] > REC_STALL_SECONDS:
        log_write("\n--- %s REC %s: no data for %d s, restarting\n" % (time.strftime("%H:%M:%S"), rec["id"], int(now - w[1])))
        return True
    return False


def rec_start(rec):
    os.makedirs(REC_DIR, exist_ok=True)
    rec_title_from_guide(rec)
    base = "%s %s - %s" % (time.strftime("%Y-%m-%d %H.%M", time.localtime(rec["start"])), safe_name(rec["channel"]), safe_name(rec["title"]))
    fp = os.path.join(REC_DIR, base + ".ts")
    n = 1
    while os.path.exists(fp):
        n += 1
        fp = os.path.join(REC_DIR, "%s (%d).ts" % (base, n))
    rec_take_connection()                    # the recording has priority over live TV, movies and other devices
    with REC_LOCK:
        rec["status"] = "recording"
        rec["file"] = fp
        rec["started"] = int(time.time())
        rec["restarts"] = 0
        rec["error"] = None
        if disk_free(REC_DIR) < REC_MIN_FREE:
            rec["warning"] = "Less than 2 GB free on the disk"
    rec_spawn(rec)
    rec_save()


def rec_stop(rec, cancelled=False):
    with REC_LOCK:
        proc = RECS["procs"].pop(rec["id"], None)
    if proc:
        try:
            proc.send_signal(signal.SIGINT)      # lets ffmpeg finish the file cleanly
        except Exception:  # noqa
            pass
        try:
            proc.wait(timeout=6)
        except Exception:  # noqa
            proc.kill()
        PROCS.discard(proc)
    with REC_LOCK:
        RECS["watch"].pop(rec["id"], None)
        rec.pop("resume", None)
    fp = rec.get("file")
    size = os.path.getsize(fp) if fp and os.path.exists(fp) else 0
    with REC_LOCK:
        rec["size"] = size
        if cancelled:
            rec["status"] = "cancelled" if size < 1000000 else "done"
            if size < 1000000 and fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
                rec["file"] = ""
        else:
            rec["status"] = "done" if size > 1000000 else "failed"
            if size <= 1000000:
                rec["error"] = "No data received from the stream (see ffmpeg.log)"
    rec_save()
    if rec["status"] == "done":
        threading.Thread(target=rec_finalize, args=(rec,), daemon=True).start()


def rec_finalize(rec):
    """Remux the .ts to .mp4 when possible, so the browser can play it with native seeking."""
    if rec_title_from_guide(rec):
        rec_save()
    fp = rec.get("file")
    if not fp or not os.path.exists(fp):
        return
    info = probe(fp, cache=False)
    rec["duration"] = int(info.get("duration") or 0)
    aac = [a["i"] for a in info.get("audio") or [] if a.get("codec") == "aac"]
    if info.get("vcodec") in ("h264", "hevc") and aac and fp.endswith(".ts"):
        # subtitle/teletext streams and non-AAC audio tracks are left out (browsers cannot use them), so channels with
        # DVB subtitles also become a seekable .mp4; the original audio first
        if info.get("aud") is not None and info["aud"] in aac:
            aac.remove(info["aud"])
            aac.insert(0, info["aud"])
        mp4 = fp[:-3] + ".mp4"
        amap = []
        for i in aac:
            amap += ["-map", "0:a:%d" % i]
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-fflags", "+genpts+discardcorrupt", "-i", fp, "-map", "0:v:0"] + amap + \
              ["-c", "copy", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", mp4]
        if info.get("vcodec") == "hevc":
            cmd[cmd.index("-c") + 2:cmd.index("-c") + 2] = ["-tag:v", "hvc1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=3600)
            if r.returncode == 0 and os.path.getsize(mp4) > 1000000:
                os.remove(fp)
                with REC_LOCK:
                    rec["file"] = mp4
                    rec["size"] = os.path.getsize(mp4)
                    d2 = probe(mp4, cache=False).get("duration") or 0
                    if d2 > 0:
                        rec["duration"] = int(d2)
            else:
                try:
                    os.remove(mp4)
                except OSError:
                    pass
        except Exception:  # noqa
            pass
    rec_save()
    if STATE["settings"].get("whisper_recordings", True) is not False and info.get("sub") is None and "whisper_enqueue" in globals():
        whisper_enqueue("rec", rec["id"], "", rec.get("title") or "Recording", "rec:%s" % rec["id"], only_en=True, src_file=rec.get("file") or fp)


AWAKE = {"proc": None, "last_tick": 0.0, "slept": []}   # caffeinate process; scheduler heartbeat; detected sleep gaps


def rec_keep_awake(now):
    """macOS puts an idle Mac to sleep, and a sleeping Mac records nothing. While a recording runs or one starts within
    12 hours, hold a "no idle sleep" assertion (caffeinate -i -s: the screen may still go dark; -s only on mains power)."""
    if sys.platform != "darwin" or not shutil.which("caffeinate"):
        return
    with REC_LOCK:
        need = any(r["status"] == "recording" or (r["status"] == "scheduled" and rec_window(r)[0] - now < 12 * 3600)
                   for r in RECS["items"])
    p = AWAKE["proc"]
    if need and (p is None or p.poll() is not None):
        try:
            AWAKE["proc"] = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())], stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            PROCS.add(AWAKE["proc"])
        except OSError:
            AWAKE["proc"] = None
    elif not need and p is not None:
        try:
            p.kill()
        except Exception:  # noqa
            pass
        PROCS.discard(p)
        AWAKE["proc"] = None


def rec_scheduler():
    """Starts and stops recordings on time."""
    while True:
        try:
            now = time.time()
            last = AWAKE["last_tick"]
            AWAKE["last_tick"] = now
            if last and now - last > 90:       # the loop did not run: the Mac was asleep (or the clock jumped)
                AWAKE["slept"] = (AWAKE["slept"] + [[int(last), int(now)]])[-20:]
                log_write("\n--- %s the Mac was asleep for %d min\n" % (time.strftime("%H:%M:%S"), int((now - last) / 60)))
            rec_keep_awake(now)
            with REC_LOCK:
                items = list(RECS["items"])
            for r in items:
              try:
                a, b = rec_window(r)
                if r["status"] == "scheduled":
                    if now >= b:
                        r["status"] = "failed"
                        asleep = any(s0 <= a and s1 >= a for s0, s1 in AWAKE["slept"])
                        r["error"] = ("Missed: the Mac was asleep" if asleep else "Missed (the player was not running)")
                        rec_save()
                    elif now >= a:
                        if rec_active():
                            continue        # another recording still holds the connection
                        try:
                            rec_start(r)
                            if "remote_push" in globals():      # tell the TV app: it should stop its own live stream (one connection)
                                remote_push({"action": "rec-start", "id": r["id"], "title": r["title"], "channel": r.get("channel", "")})
                        except Exception as e:  # noqa
                            r["status"] = "failed"
                            r["error"] = str(e)
                            rec_save()
                elif r["status"] == "recording":
                    proc = RECS["procs"].get(r["id"])
                    if now >= b:
                        rec_stop(r)
                    elif proc is None and (r.get("resume") or r.get("retry_at")):
                        # restarted (an update) while recording, or waiting for the next try after a failed restart:
                        # carry on in the same file
                        if r.get("retry_at") and now < r["retry_at"]:
                            continue
                        r.pop("resume", None)
                        r.pop("retry_at", None)
                        rec_take_connection()
                        try:
                            rec_spawn(r, append=True)
                        except Exception as e:  # noqa
                            r["error"] = "Restart failed: %s" % e
                            r["retry_at"] = now + REC_BACKOFF[-1]
                        rec_save()
                    elif proc is not None and proc.poll() is None and rec_watchdog(r, proc, now):
                        try:
                            proc.kill()             # the branch below restarts it on the next pass
                        except Exception:  # noqa
                            pass
                    elif proc is not None and proc.poll() is not None:
                        # ffmpeg quit early: continue in the same file while the programme is still on
                        PROCS.discard(proc)
                        with REC_LOCK:
                            RECS["procs"].pop(r["id"], None)
                        if now < b - 10:
                            # a provider hiccup must not end a long recording: keep trying until the programme is over,
                            # waiting a little longer each time (2, 4, 8, 15, then every 30 s)
                            n = r.get("restarts", 0)
                            r["restarts"] = n + 1
                            r["gaps"] = r.get("gaps", 0) + 1
                            r["retry_at"] = now + REC_BACKOFF[min(n, len(REC_BACKOFF) - 1)]
                            log_write("\n--- %s REC %s: stream ended, retry %d in %d s\n" % (time.strftime("%H:%M:%S"), r["id"], n + 1, int(r["retry_at"] - now)))
                            rec_save()
                        else:
                            rec_stop(r)
              except Exception as e:  # noqa  (one bad item must not stop the other recordings)
                log_write("\n--- %s REC scheduler error on %s: %s\n" % (time.strftime("%H:%M:%S"), r.get("id"), e))
        except Exception as e:  # noqa
            log_write("\n--- REC scheduler error: %s\n" % e)
        time.sleep(3)


def rec_status():
    now = int(time.time())
    act = rec_blocking()
    up = rec_upcoming(REC_WARN_SECONDS)
    out = {"active": None, "upcoming": None}
    if act:
        out["active"] = {"id": act["id"], "title": act["title"], "channel": act["channel"], "stop": act["stop"] + act.get("pad_after", 0),
                         "retrying": bool(act.get("retry_at")), "gaps": act.get("gaps", 0),
                         "start": act["start"], "started": act.get("started", 0), "file": act.get("file", ""), "size": os.path.getsize(act["file"]) if act.get("file") and os.path.exists(act["file"]) else 0}
    if up:
        out["upcoming"] = {"id": up["id"], "title": up["title"], "channel": up["channel"], "in": rec_window(up)[0] - now, "start": up["start"]}
    return out


def rec_list():
    with REC_LOCK:
        items = [dict(r) for r in RECS["items"]]
    for r in items:
        r.pop("notes", None)
        if r.get("file") and os.path.exists(r["file"]):
            try:
                r["size"] = os.path.getsize(r["file"])
            except OSError:
                pass
    items.sort(key=lambda r: r["start"], reverse=True)
    return {"items": items, "dir": REC_DIR, "free": disk_free(REC_DIR)}


def disk_free(path):
    try:
        st = os.statvfs(path if os.path.exists(path) else os.path.dirname(path))
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


# ---------------------------------------------------------------- catalog ----
# Local copy of the live channel list, with HD/FHD/4K/RAW variants merged into one channel.
CATALOG_PATH = os.path.join(CONFIG_DIR, "catalog.json")
CATALOG_TTL = 12 * 3600
CATALOG = {"t": 0, "cats": [], "groups": [], "by_stream": {}, "by_key": {}, "status": "idle", "error": None}
CATALOG_LOCK = threading.Lock()

_SUP = {"ᴬ": "A", "ᴮ": "B", "ᶜ": "C", "ᴰ": "D", "ᴱ": "E", "ᶠ": "F", "ᴳ": "G", "ᴴ": "H", "ᴵ": "I", "ᴶ": "J", "ᴷ": "K",
        "ᴸ": "L", "ᴹ": "M", "ᴺ": "N", "ᴼ": "O", "ᴾ": "P", "ᴿ": "R", "ˢ": "S", "ᵀ": "T", "ᵁ": "U", "ⱽ": "V", "ᵂ": "W",
        "ᵃ": "A", "ᵇ": "B", "ᵈ": "D", "ᵉ": "E", "ᵍ": "G", "ʰ": "H", "ᶦ": "I", "ⁱ": "I", "ʲ": "J", "ᵏ": "K", "ˡ": "L",
        "ᵐ": "M", "ⁿ": "N", "ᵒ": "O", "ᵖ": "P", "ʳ": "R", "ᵗ": "T", "ᵘ": "U", "ᵛ": "V", "ʷ": "W", "ˣ": "X", "ʸ": "Y",
        "ᶻ": "Z", "⁰": "0", "¹": "1", "²": "", "³": "3", "⁴": "4", "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9",
        "◉": " ", "•": " ", "\xa0": " ", "★": " ", "☆": " ", "►": " ", "▶": " "}
_SUP_TR = str.maketrans(_SUP)
_QUAL_RE = re.compile(r"(?<![A-Z0-9])(8K|4K|UHD|FHD|HD|SD|RAW|HEVC|H265|H\.265|LQ|HQ|VIP|BACKUP|BK|ULTRA|HDR|50FPS|60FPS|"
                      r"1080P|720P|2160P|3840P|1920P|SUPER|GOLD|SUPE)(?![A-Z0-9])")
_PREFIX_RE = re.compile(r"^\s*[A-Za-z0-9+\-]{1,7}\s*\|\s*")
_QUAL_RANK = {"8K": 5, "4K": 5, "RAW": 4, "FHD": 3, "HD": 2, "": 2, "SD": 1}


def clean_name(name):
    """'NL| NPO 1 ᴴᴰ' -> ('NPO 1', 'HD')"""
    s = (name or "").translate(_SUP_TR)
    if _PREFIX_RE.match(s):
        first, rest = s.split("|", 1)
        first = first.strip().upper()
        if "|" not in rest or (len(first) <= 5 and first not in ("NEXT", "LIVE", "NOW", "NO", "TODAY")):
            s = rest
    up = s.upper()
    toks = set(_QUAL_RE.findall(up))
    q = ""
    if "8K" in toks:
        q = "8K"
    elif toks & {"4K", "UHD", "2160P", "3840P"}:
        q = "4K"
    elif "RAW" in toks:
        q = "RAW"
    elif toks & {"FHD", "1080P", "1920P"}:
        q = "FHD"
    elif toks & {"HD", "720P", "HEVC", "H265", "H.265"}:
        q = "HD"
    elif toks & {"SD", "LQ"}:
        q = "SD"
    s = _QUAL_RE.sub(" ", up)
    s = re.sub(r"[\(\)\[\]/]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–|:.")
    # keep the provider's casing (channel names are mostly acronyms), only stripped of tokens
    return s, q


def category_country(cat_name):
    c = (cat_name or "").translate(_SUP_TR)
    if "|" in c:
        return c.split("|", 1)[0].strip().upper() or "OTHER"
    return "OTHER"


def category_quality(cat_name):
    toks = set(_QUAL_RE.findall((cat_name or "").translate(_SUP_TR).upper()))
    for q, keys in (("8K", {"8K"}), ("4K", {"4K", "UHD"}), ("RAW", {"RAW"}), ("FHD", {"FHD"}), ("HD", {"HD", "HEVC"}), ("SD", {"SD", "LQ"})):
        if toks & keys:
            return q
    return ""


def build_groups(cats, streams):
    catname = {str(c.get("category_id")): c.get("category_name") or "" for c in cats}
    catq = {k: category_quality(v) for k, v in catname.items()}
    catcountry = {k: category_country(v) for k, v in catname.items()}
    groups = {}
    order = []
    by_stream = {}
    for s in streams:
        name = s.get("name") or ""
        if not name.strip() or name.strip().startswith("#") or name.strip().startswith("-"):
            continue
        cid = str(s.get("category_id"))
        disp, q = clean_name(name)
        if not q:
            q = catq.get(cid, "")
        if not disp:
            disp = name.strip()
        country = catcountry.get(cid, "OTHER")
        key = country + ":" + re.sub(r"[^A-Z0-9]", "", disp.upper())
        sid = int(s.get("stream_id"))
        arch = int(s.get("tv_archive_duration") or 0) if str(s.get("tv_archive")) == "1" else 0
        var = {"id": sid, "q": q, "rank": _QUAL_RANK.get(q, 2), "cat": cid, "name": name.strip(), "archive": arch, "_i": len(by_stream)}
        g = groups.get(key)
        eid = (s.get("epg_channel_id") or "").strip()
        if not g:
            g = {"key": key, "name": disp, "icon": s.get("stream_icon") or "",
                 "epg": eid, "cats": [cid], "variants": [var], "country": country, "_names": [disp], "_epgc": {}}
            groups[key] = g
            order.append(key)
        else:
            g["_names"].append(disp)
            if cid not in g["cats"]:
                g["cats"].append(cid)
            if not g["epg"] and s.get("epg_channel_id"):
                g["epg"] = s.get("epg_channel_id")
            if not g["icon"] and s.get("stream_icon"):
                g["icon"] = s.get("stream_icon")
            g["variants"].append(var)
        if eid:
            g["_epgc"][eid] = g["_epgc"].get(eid, 0) + 1
            var["epg"] = eid
        by_stream[sid] = key
    out = []
    for key in order:
        g = groups[key]
        g["variants"].sort(key=lambda v: (-v["rank"], v["q"] == "8K", v["_i"]))
        for v in g["variants"]:
            v.pop("_i", None)
        epgc = g.pop("_epgc", {})
        # every guide id the provider uses for this channel, most used first (the guide picks the one with data)
        g["epgs"] = sorted(epgc, key=lambda e: (-epgc[e], e))
        g["epg0"] = g["epg"]
        names = g.pop("_names", [])
        if len(names) > 1:
            cnt = {}
            for n in names:
                cnt[n] = cnt.get(n, 0) + 1
            g["name"] = sorted(cnt, key=lambda n: (-cnt[n], -len(n)))[0]
        g["archive"] = max([v["archive"] for v in g["variants"]] + [0])
        g["best"] = g["variants"][0]["q"]
        out.append(g)
    return out, by_stream


def catalog_load():
    try:
        with open(CATALOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with CATALOG_LOCK:
            CATALOG["t"] = data.get("t", 0)
            CATALOG["cats"] = data.get("cats", [])
            CATALOG["groups"] = data.get("groups", [])
            CATALOG["by_stream"] = {int(k): v for k, v in data.get("by_stream", {}).items()}
            CATALOG["by_key"] = {g["key"]: g for g in CATALOG["groups"]}
    except (OSError, ValueError):
        pass


def catalog_refresh(force=False):
    with CATALOG_LOCK:
        if CATALOG["status"] == "busy":
            return False
        if not force and time.time() - CATALOG["t"] < CATALOG_TTL and CATALOG["groups"]:
            return False
        CATALOG["status"] = "busy"
        CATALOG["error"] = None
    try:
        cats = xtream("get_live_categories")
        streams = xtream("get_live_streams")
        if not isinstance(cats, list) or not isinstance(streams, list):
            raise RuntimeError("Unexpected answer from the provider")
        cats = [{"id": str(c.get("category_id")), "name": c.get("category_name") or "", "country": category_country(c.get("category_name"))} for c in cats]
        groups, by_stream = build_groups([{"category_id": c["id"], "category_name": c["name"]} for c in cats], streams)
        counts = {}
        for g in groups:
            for cid in g["cats"]:
                counts[cid] = counts.get(cid, 0) + 1
        for c in cats:
            c["n"] = counts.get(c["id"], 0)
        data = {"t": int(time.time()), "cats": cats, "groups": groups, "by_stream": by_stream}
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CATALOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, CATALOG_PATH)
        with CATALOG_LOCK:
            CATALOG.update({"t": data["t"], "cats": cats, "groups": groups, "by_stream": by_stream,
                            "by_key": {g["key"]: g for g in groups}, "status": "idle"})
        migrate_favorites()
        try:
            epg_resolve(save=True)
        except Exception:  # noqa
            pass
        tv_schedule("channel list")
        return True
    except Exception as e:  # noqa
        with CATALOG_LOCK:
            CATALOG["status"] = "idle"
            CATALOG["error"] = str(e)
        return False


def migrate_favorites():
    """Old favourites were 'live:<stream_id>'; new ones are 'ch:<group key>'."""
    changed = False
    with LOCK:
        favs = list(STATE["favorites"])
    out = []
    for k in favs:
        if k.startswith("live:"):
            try:
                key = CATALOG["by_stream"].get(int(k[5:]))
            except ValueError:
                key = None
            if key:
                k = "ch:" + key
                changed = True
        if k not in out:
            out.append(k)
    if changed:
        with LOCK:
            STATE["favorites"] = out
        save_config()


def stream_group(sid):
    key = CATALOG["by_stream"].get(int(sid))
    return CATALOG["by_key"].get(key) if key else None


# -------------------------------------------------------------------- epg ----
# TV guide from xmltv.php, cached in SQLite and refreshed about 3x per day.

EPG_PATH = os.path.join(CONFIG_DIR, "epg.sqlite")
EPG_TTL = 4 * 3600               # normal refresh interval
EPG_RETRY = 30 * 60              # retry sooner when the provider's file turns out to be stale
EPG_KEEP_DAYS = 8
EPG = {"t": 0, "status": "idle", "error": None, "count": 0, "first": 0, "last": 0, "stale": False, "cover": 0,
       "matched": 0, "extra": 0}
EPG_LOCK = threading.Lock()
EPG_SID = {}                     # guide id -> stream ids that carry it (for the provider's short EPG)
SHORT_EPG = {}                   # guide id -> time of the last short-EPG lookup
SHORT_LOCK = threading.Lock()


def epg_db():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(EPG_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def epg_init():
    conn = epg_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS prog (ch TEXT NOT NULL, start INTEGER NOT NULL, stop INTEGER NOT NULL, "
                     "title TEXT, descr TEXT, PRIMARY KEY (ch, start)) WITHOUT ROWID")
        conn.execute("CREATE INDEX IF NOT EXISTS prog_start ON prog (start)")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS chan (id TEXT PRIMARY KEY, names TEXT)")
        conn.commit()
        row = conn.execute("SELECT v FROM meta WHERE k='t'").fetchone()
        EPG["t"] = int(row[0]) if row else 0
        epg_stats(conn)
    finally:
        conn.close()


def epg_stats(conn):
    row = conn.execute("SELECT COUNT(*), MIN(start), MAX(stop) FROM prog").fetchone()
    EPG["count"], EPG["first"], EPG["last"] = (row[0] or 0), (row[1] or 0), (row[2] or 0)
    # How fresh is the guide really? Share of channels (with data in the last day) that have a programme on now
    # and still something in 3 hours. A provider file that ends early (seen 8 Nov 2026: it stopped at 07:00) is "stale".
    now = int(time.time())
    recent = conn.execute("SELECT COUNT(DISTINCT ch) FROM prog WHERE stop > ? AND start < ?", (now - 86400, now)).fetchone()[0] or 0
    ahead = conn.execute("SELECT COUNT(DISTINCT ch) FROM prog WHERE stop > ? AND start <= ?", (now + 3 * 3600, now + 3 * 3600)).fetchone()[0] or 0
    EPG["cover"] = round(100.0 * ahead / recent) if recent else 0
    EPG["stale"] = bool(recent) and ahead < recent * 0.5


def parse_xmltv_time(s):
    # "20261106202539 +0000"
    try:
        base, _, off = s.strip().partition(" ")
        dt = datetime.datetime.strptime(base[:14].ljust(14, "0"), "%Y%m%d%H%M%S")
        ts = int(dt.replace(tzinfo=datetime.timezone.utc).timestamp())
        if off and len(off) == 5 and off[0] in "+-":
            delta = int(off[1:3]) * 3600 + int(off[3:5]) * 60
            ts -= delta if off[0] == "+" else -delta
        return ts
    except (ValueError, TypeError):
        return None


def epg_extra_urls():
    """Optional extra XMLTV sources (Settings -> TV guide), one URL per line; .gz is fine."""
    raw = STATE["settings"].get("epg_extra") or ""
    if isinstance(raw, list):
        raw = "\n".join(raw)
    return [u.strip() for u in re.split(r"[\s,]+", raw) if u.strip().lower().startswith(("http://", "https://"))][:6]


def epg_load_file(conn, path, prefix=""):
    """Reads one XMLTV file into the database. Extra sources get a prefix on their ids ("x:") so they never mix
    with the provider's ids; they are only used through name matching."""
    cur = conn.cursor()
    rows, chans, n = [], [], 0
    with open(path, "rb") as fh:
        head = fh.read(2)
    fobj = gzip.open(path, "rb") if head == b"\x1f\x8b" else open(path, "rb")
    try:
        for ev, el in ET.iterparse(fobj, events=("end",)):
            if el.tag == "channel":
                cid = (el.get("id") or "").strip()
                names = [(d.text or "").strip() for d in el.findall("display-name") if (d.text or "").strip()]
                if cid:
                    chans.append((prefix + cid, json.dumps(names[:4], ensure_ascii=False)))
                el.clear()
                continue
            if el.tag != "programme":
                continue
            st, sp = parse_xmltv_time(el.get("start", "")), parse_xmltv_time(el.get("stop", ""))
            ch = (el.get("channel") or "").strip()
            if st and sp and ch and sp > st:
                t = el.find("title")
                d = el.find("desc")
                rows.append((prefix + ch, st, sp, (t.text or "").strip() if t is not None and t.text else "",
                             (d.text or "").strip() if d is not None and d.text else ""))
            el.clear()
            if len(rows) >= 5000:
                cur.executemany("INSERT OR REPLACE INTO prog (ch,start,stop,title,descr) VALUES (?,?,?,?,?)", rows)
                n += len(rows)
                rows = []
    finally:
        fobj.close()
    if rows:
        cur.executemany("INSERT OR REPLACE INTO prog (ch,start,stop,title,descr) VALUES (?,?,?,?,?)", rows)
        n += len(rows)
    if chans:
        cur.executemany("INSERT OR REPLACE INTO chan (id,names) VALUES (?,?)", chans)
    return n


def epg_due():
    age = time.time() - EPG["t"]
    return age >= EPG_TTL or (EPG["stale"] and age >= EPG_RETRY)


def epg_refresh(force=False):
    with EPG_LOCK:
        if EPG["status"] == "busy":
            return False
        if not force and not epg_due():
            return False
        EPG["status"] = "busy"
        EPG["error"] = None
    tmp = os.path.join(CONFIG_DIR, "xmltv.tmp")
    try:
        with LOCK:
            server, user, pw = STATE["server"], STATE["username"], STATE["password"]
        if not server:
            raise RuntimeError("Not logged in")
        url = server.rstrip("/") + "/xmltv.php?" + urllib.parse.urlencode({"username": user, "password": pw})
        with open_url(url, timeout=600) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f, 1024 * 256)
        conn = epg_db()
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS chan (id TEXT PRIMARY KEY, names TEXT)")
            conn.execute("BEGIN")
            epg_load_file(conn, tmp)
            conn.commit()
            extra_n, extra_err = 0, []
            for i, xu in enumerate(epg_extra_urls()):
                xt = os.path.join(CONFIG_DIR, "xmltv-extra.tmp")
                try:
                    with open_url(xu, timeout=300) as r, open(xt, "wb") as f:
                        shutil.copyfileobj(r, f, 1024 * 256)
                    conn.execute("BEGIN")
                    extra_n += epg_load_file(conn, xt, prefix="x%d:" % i)
                    conn.commit()
                except Exception as e:  # noqa
                    conn.rollback()
                    extra_err.append("%s: %s" % (urllib.parse.urlparse(xu).netloc, e))
                finally:
                    try:
                        os.remove(xt)
                    except OSError:
                        pass
            conn.execute("DELETE FROM prog WHERE stop < ?", (int(time.time()) - EPG_KEEP_DAYS * 86400,))
            now = int(time.time())
            conn.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('t',?)", (str(now),))
            conn.commit()
            EPG["t"] = now
            EPG["extra"] = extra_n
            epg_stats(conn)
        finally:
            conn.close()
        with EPG_LOCK:
            EPG["status"] = "idle"
            if extra_err:
                EPG["error"] = "Extra guide source failed (" + "; ".join(extra_err)[:300] + ")"
        try:
            epg_resolve(save=True)
        except Exception:  # noqa
            pass
        try:
            with REC_LOCK:
                manual = [r for r in RECS["items"] if r.get("manual")]
            if manual and any([rec_title_from_guide(r) for r in manual]):
                rec_save()
        except Exception:  # noqa
            pass
        tv_schedule("tv guide", delay=10)
        return True
    except Exception as e:  # noqa
        with EPG_LOCK:
            EPG["status"] = "idle"
            EPG["error"] = redact(str(e)) if "redact" in globals() else str(e)
            EPG["t"] = max(EPG["t"], int(time.time()) - EPG_TTL + EPG_RETRY)    # try again in half an hour, not every 5 min
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---- matching channels to the guide ----
# The provider gives every stream its own guide id, and the variants of one channel do not always agree
# (ESPN 1 4K says espn.nl, ESPN 1 HD says ESPN1.nl). Many streams have no id at all. So per channel:
#   1. of all ids its variants use, take the one with the most programmes in the coming day;
#   2. none has data -> match the channel name against the guide's channel names (provider + extra sources);
#   3. still nothing on now -> ask the provider's short EPG for that stream (see epg_short_fill).

def _epg_norm(name):
    d, _ = clean_name(name or "")
    return re.sub(r"[^A-Z0-9]", "", (d or "").upper())


def _epg_country(cid, name):
    m = re.match(r"^\s*([A-Za-z]{2,3})\s*[|:]", name or "")
    if m and m.group(1).upper() not in ("TV", "HD", "4K"):
        return m.group(1).upper()
    tail = cid.rsplit(".", 1)[-1] if "." in cid else ""
    tail = tail.upper()
    return {"GB": "UK"}.get(tail, tail) if 2 <= len(tail) <= 3 and tail.isalpha() else ""


def epg_resolve(save=False):
    with CATALOG_LOCK:
        groups = list(CATALOG["groups"])
    if not groups or not os.path.exists(EPG_PATH):
        return 0
    now = int(time.time())
    conn = epg_db()
    try:
        have = dict(conn.execute("SELECT ch, COUNT(*) FROM prog WHERE stop > ? AND start < ? GROUP BY ch", (now - 3600, now + 86400)))
        try:
            chans = conn.execute("SELECT id, names FROM chan").fetchall()
        except sqlite3.Error:
            chans = []
    finally:
        conn.close()
    index = {}
    for cid, names in chans:
        if not have.get(cid):
            continue
        try:
            names = json.loads(names or "[]")
        except ValueError:
            names = []
        base = cid.split(":", 1)[1] if cid.startswith("x") and ":" in cid[:4] else cid
        for n in names + [base.rsplit(".", 1)[0]]:
            k = _epg_norm(n)
            if len(k) < 2:
                continue
            for c in {_epg_country(base, n), _epg_country(base, "")} - {""}:
                index.setdefault(c + ":" + k, set()).add(cid)
            index.setdefault("*:" + k, set()).add(cid)
    sid_map = {}
    changed = matched = 0
    for g in groups:
        cands = [e for e in (g.get("epgs") or ([g["epg"]] if g.get("epg") else [])) if e]
        best = max(cands, key=lambda e: (have.get(e, 0), -cands.index(e))) if cands else ""
        if not have.get(best):
            hits = index.get(g["key"]) or (index.get("*:" + g["key"].split(":", 1)[-1]) if g.get("country") in ("OTHER", "") else None)
            if hits:
                best = max(hits, key=lambda e: (have.get(e, 0), not e.startswith("x")))
                matched += 1
            elif not best:
                best = g.get("epg0") or g.get("epg") or ""
        if best and best != g.get("epg"):
            g["epg"] = best
            changed += 1
        if best:
            sids = [v["id"] for v in g["variants"] if v.get("epg")] or [v["id"] for v in g["variants"]]
            sid_map.setdefault(best, sids[:3])
    global EPG_SID
    EPG_SID = sid_map
    EPG["matched"] = matched
    if changed and save:
        catalog_save()
    return changed


def catalog_save():
    with CATALOG_LOCK:
        data = {"t": CATALOG["t"], "cats": CATALOG["cats"], "groups": CATALOG["groups"], "by_stream": CATALOG["by_stream"]}
        tmp = CATALOG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, CATALOG_PATH)


def epg_short_fill(ids, wait=True, limit=8):
    """Fill gaps from the provider's per-stream short EPG (now and the next few programmes). Used when the big
    XMLTV file has nothing on now for a channel, e.g. because the provider's file ended early."""
    todo = []
    now = time.time()
    with SHORT_LOCK:
        for e in ids:
            if e and now - SHORT_EPG.get(e, 0) > 20 * 60 and EPG_SID.get(e):
                SHORT_EPG[e] = now
                todo.append(e)
            if len(todo) >= limit:
                break
    if not todo:
        return 0

    def work():
        rows = []
        for e in todo:
            for sid in EPG_SID.get(e, [])[:2]:
                try:
                    r = xtream("get_short_epg", stream_id=sid, limit=8)
                except Exception:  # noqa
                    continue
                lst = (r or {}).get("epg_listings") if isinstance(r, dict) else None
                got = 0
                for p in lst or []:
                    try:
                        st, sp = int(p.get("start_timestamp") or 0), int(p.get("stop_timestamp") or 0)
                    except (TypeError, ValueError):
                        continue
                    if st and sp > st:
                        rows.append((e, st, sp, _b64txt(p.get("title")), _b64txt(p.get("description"))))
                        got += 1
                if got:
                    break
        if rows:
            conn = epg_db()
            try:
                conn.executemany("INSERT OR IGNORE INTO prog (ch,start,stop,title,descr) VALUES (?,?,?,?,?)", rows)
                conn.commit()
            finally:
                conn.close()
        return len(rows)

    if wait:
        return work()
    threading.Thread(target=work, daemon=True).start()
    return 0


def _b64txt(s):
    try:
        return base64.b64decode(s or "").decode("utf-8", "replace").strip()
    except Exception:  # noqa
        return (s or "").strip()


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def epg_rows(ids, a, b):
    """All programmes for the given xmltv channel ids that overlap [a, b)."""
    ids = [i for i in ids if i]
    out = {}
    if not ids:
        return out
    conn = epg_db()
    try:
        for part in _chunks(ids, 400):
            q = "SELECT ch,start,stop,title,descr FROM prog WHERE ch IN (%s) AND stop > ? AND start < ? ORDER BY ch, start" % ",".join("?" * len(part))
            for ch, st, sp, title, descr in conn.execute(q, list(part) + [int(a), int(b)]):
                out.setdefault(ch, []).append({"start": st, "stop": sp, "title": title, "description": descr})
    finally:
        conn.close()
    for ch, lst in out.items():
        out[ch] = _epg_dedupe(lst)
    return out


def _epg_dedupe(lst):
    """Some guide files list a programme twice with starts a few minutes apart; keep one, without overlaps."""
    res = []
    for p in lst:
        if res and p["start"] < res[-1]["stop"] - 60:
            last = res[-1]
            if p["title"] == last["title"] or p["start"] - last["start"] < 5 * 60:
                if p["stop"] > last["stop"] and p["title"] == last["title"]:
                    last["stop"] = p["stop"]
                continue
        res.append(p)
    return res


def epg_now_next(ids, fill=True):
    now = int(time.time())
    rows = epg_rows(ids, now, now + 12 * 3600)
    missing = [i for i in ids if i and not any(p["start"] <= now < p["stop"] for p in rows.get(i, []))]
    if fill and missing and EPG_SID:
        # one channel (the player, the TV remote): look it up right away; a whole list: in the background
        if len(missing) <= 2 and epg_short_fill(missing, wait=True):
            rows.update(epg_rows(missing, now, now + 12 * 3600))
        elif len(missing) > 2:
            epg_short_fill(missing, wait=False, limit=12)
    out = {}
    for ch, lst in rows.items():
        cur = next((p for p in lst if p["start"] <= now < p["stop"]), None)
        nxt = next((p for p in lst if p["start"] >= (cur["stop"] if cur else now)), None)
        out[ch] = {"now": cur, "next": nxt}
    return out


def epg_search(q, limit=300):
    q = (q or "").strip()
    if len(q) < 2:
        return []
    conn = epg_db()
    try:
        like = "%" + q.replace("%", "").replace("_", "") + "%"
        since = int(time.time()) - 3 * 86400
        rows = conn.execute("SELECT ch,start,stop,title,descr FROM prog WHERE start > ? AND (title LIKE ? OR descr LIKE ?) "
                            "ORDER BY start LIMIT ?", (since, like, like, int(limit))).fetchall()
    finally:
        conn.close()
    return [{"ch": r[0], "start": r[1], "stop": r[2], "title": r[3], "description": r[4]} for r in rows]


def epg_status():
    nxt = 0
    if EPG["t"]:
        nxt = EPG["t"] + (EPG_RETRY if EPG["stale"] else EPG_TTL)
    return {"updated": EPG["t"], "status": EPG["status"], "error": EPG["error"], "count": EPG["count"],
            "first": EPG["first"], "last": EPG["last"], "next": nxt, "stale": EPG["stale"], "cover": EPG["cover"],
            "matched": EPG["matched"], "extra": EPG["extra"], "extra_urls": len(epg_extra_urls())}



# ------------------------------------------------------------- tv export ----
# Cleaned-up M3U playlist for the Samsung TV (SmartOne IPTV), written to a folder in Dropbox.
# Built from exactly the app's channel settings: hidden categories, favourites, merged variants.
TV = {"status": "idle", "error": None, "t": 0, "changed": 0, "n": 0, "favs": 0, "groups": 0,
      "path": "", "guide_path": "", "guide_n": 0, "reason": ""}
TV_LOCK = threading.Lock()
TV_TIMER = {"t": None}
TV_M3U_NAME = "Beeldbuis TV.m3u"
TV_GUIDE_NAME = "Beeldbuis TV guide.xml"
TV_QORDER = {"best": ["4K", "8K", "RAW", "FHD", "HD", "", "SD"],
             "FHD": ["FHD", "HD", "", "4K", "8K", "RAW", "SD"],
             "HD": ["HD", "", "FHD", "4K", "8K", "RAW", "SD"]}
_TV_BACKUP_RE = re.compile(r"(?<![A-Z0-9])(BACKUP|BK|TEST)(?![A-Z0-9])")
_TV_CAT_DROP_RE = re.compile(r"(?<![A-Z0-9])(8K|4K|UHD|FHD|HD|SD|RAW|HEVC|H265|LQ|HQ|VIP|BACKUP|BK|ULTRA|HDR|50FPS|60FPS|"
                             r"60 ?FPS|1080P|720P|2160P|GOLD|SUPE)(?![A-Z0-9])")
# short words that are ordinary words, not acronyms -> title case
_TV_WORDS = {"SKY", "NOW", "ONE", "TWO", "RED", "CUP", "BOX", "AND", "THE", "NEW", "BIG", "ALL", "OF", "TEAM", "LIVE",
             "TV", "PLUS", "PER", "HUB", "GO", "PRO", "PAY"}
_TV_ACRONYMS = {"PPV", "BBC", "BBCI", "UFC", "EPL", "NFL", "NBA", "NHL", "MLB", "F1", "GAA", "PDC", "DAZN", "ESPN", "HBO",
                "MAX", "TNT", "TT", "FA", "MOLA", "MXGP", "ITV", "NPO", "RTL", "SBS", "UEFA", "USA", "UK", "NL", "BE", "PSF",
                "TV", "24/7"}


def tv_settings():
    with LOCK:
        st = dict(STATE["settings"])
    return {"dir": (st.get("tv_dir") or "").strip(), "auto": st.get("tv_auto", True) is not False,
            "quality": st.get("tv_quality") or "FHD", "hidden": set(str(c) for c in st.get("hidden_cats") or []),
            "manual": dict(st.get("q_manual") or {})}


def dropbox_dirs():
    """Dropbox folders on this Mac (personal first), from Dropbox's own info.json, else the usual places."""
    out = []
    try:
        with open(os.path.expanduser("~/.dropbox/info.json"), "r", encoding="utf-8") as f:
            info = json.load(f)
        for k in ("personal", "business"):
            p = (info.get(k) or {}).get("path")
            if p and os.path.isdir(p):
                out.append(p)
    except (OSError, ValueError, AttributeError):
        pass
    cands = []
    cs = os.path.expanduser("~/Library/CloudStorage")
    try:
        cands += sorted(os.path.join(cs, d) for d in os.listdir(cs) if d.startswith("Dropbox"))
    except OSError:
        pass
    home = os.path.expanduser("~")
    try:
        cands += sorted(os.path.join(home, d) for d in os.listdir(home) if d.startswith("Dropbox"))
    except OSError:
        pass
    for p in cands:
        if os.path.isdir(p) and os.path.realpath(p) not in [os.path.realpath(x) for x in out]:
            out.append(p)
    return out


def tv_suggested_dir():
    d = dropbox_dirs()
    return os.path.join(d[0], "Beeldbuis") if d else ""


_TV_SPECIAL = {"BBCIPLAYER": "BBC iPlayer", "IPLAYER": "iPlayer", "TELE2": "Tele2", "ITVX": "ITVX", "MOTOGP": "MotoGP"}


def tv_word(w):
    if "/" in w and w != "24/7":
        return "/".join(tv_word(p) for p in w.split("/"))
    if w in _TV_SPECIAL:
        return _TV_SPECIAL[w]
    base, plus = (w[:-1], "+") if w.endswith("+") and len(w) > 1 else (w, "")
    if not base.isalpha() or base in _TV_ACRONYMS:
        return w
    if len(base) <= 3 and base not in _TV_WORDS:
        return w
    return base[:1] + base[1:].lower() + plus


def tv_group_title(cat):
    """'NL| SPORT' -> 'NL · Sport', 'UK| GENERAL HEVC' -> 'UK · General'."""
    if not cat:
        return "Other"
    name = (cat.get("name") or "").translate(_SUP_TR).replace("\xa0", " ")
    country = cat.get("country") or "OTHER"
    rest = name.split("|", 1)[1] if "|" in name else name
    up = _TV_CAT_DROP_RE.sub(" ", rest.upper())
    up = re.sub(r"\s+", " ", up).strip(" -–|:.")
    if not up:
        up = "CHANNELS"
    title = " ".join(tv_word(w) for w in up.split(" "))
    if country == "OTHER":
        return title
    return "%s · %s" % (country, title)


def tv_pick(g, hidden, order, manual):
    vars_ = [v for v in g["variants"] if str(v["cat"]) not in hidden]
    if not vars_:
        return None
    m = manual.get(g["key"])
    if m:
        v = next((v for v in vars_ if str(v["id"]) == str(m)), None)
        if v:
            return v
    rank = {q: i for i, q in enumerate(order)}
    def score(v):
        nm = (v.get("name") or "").translate(_SUP_TR).upper()
        return (rank.get(v["q"], len(order)), 1 if _TV_BACKUP_RE.search(nm) else 0)
    return min(vars_, key=score)   # min() is stable: provider order breaks ties


def tv_home_cat(g, v, hidden, cat_by_id, cat_pos):
    """The category a channel is listed under: the chosen stream's category, unless that is only a
    quality bucket ('NEDERLAND ULTRA 4K', 'GENERAL HEVC') and the channel also sits in a normal one."""
    own = cat_by_id.get(str(v["cat"]))
    if own and not category_quality(own["name"]):
        return own
    vis = sorted((c for c in g["cats"] if str(c) not in hidden and str(c) in cat_by_id), key=lambda c: cat_pos.get(str(c), 1 << 30))
    for c in vis:
        if not category_quality(cat_by_id[str(c)]["name"]):
            return cat_by_id[str(c)]
    return own


def _attr(s):
    return re.sub(r"[\r\n\"]+", " ", str(s or "")).strip()


def tv_build():
    """Returns (m3u text, [epg ids], stats)."""
    cfg = tv_settings()
    hidden, manual = cfg["hidden"], cfg["manual"]
    order = TV_QORDER.get(cfg["quality"], TV_QORDER["FHD"])
    with CATALOG_LOCK:
        groups = list(CATALOG["groups"])
        cats = list(CATALOG["cats"])
        by_key = CATALOG["by_key"]
    if not groups:
        raise RuntimeError("The channel list is not loaded yet; try again in a minute.")
    with LOCK:
        server, user, pw = STATE["server"], STATE["username"], STATE["password"]
        favs = [k[3:] for k in STATE["favorites"] if k.startswith("ch:")]
    cat_by_id = {c["id"]: c for c in cats}
    cat_pos = {c["id"]: i for i, c in enumerate(cats)}
    def country_rank(c):
        return (0 if c == "NL" else 1 if c == "UK" else 3 if c == "OTHER" else 2, c)

    rows = []          # (group title, sort key, group, variant)
    done = set()
    for key in favs:
        g = by_key.get(key)
        if not g or key in done:
            continue
        v = tv_pick(g, hidden, order, manual)
        if not v:
            continue
        done.add(key)
        rows.append(("★ Favourites", (0,), g, v))
    nfav = len(rows)
    buckets = {}
    for i, g in enumerate(groups):
        if g["key"] in done:
            continue
        v = tv_pick(g, hidden, order, manual)
        if not v:
            continue
        cat = tv_home_cat(g, v, hidden, cat_by_id, cat_pos)
        title = tv_group_title(cat)
        sk = (1, country_rank(cat.get("country") if cat else "OTHER"), cat_pos.get(cat["id"] if cat else "", 1 << 30))
        b = buckets.get(title)
        if b is None or sk < b:
            buckets[title] = sk
        rows.append((title, None, g, v, i))
    fav_rows = rows[:nfav]
    other = sorted(rows[nfav:], key=lambda r: (buckets[r[0]], r[4]))
    epg_url = "%s/xmltv.php?username=%s&password=%s" % (server.rstrip("/"), urllib.parse.quote(user), urllib.parse.quote(pw))
    out = ['#EXTM3U x-tvg-url="%s" url-tvg="%s"' % (epg_url, epg_url),
           "#PLAYLIST:Beeldbuis"]
    epg_ids = []
    n = 0
    for r in fav_rows + other:
        title, g, v = r[0], r[2], r[3]
        n += 1
        name = g["name"]
        if "|" in name:   # event channel ("NEXT | UFC ... | Sun 08 Nov 00:00 CET"): keep the provider's own text
            name = re.sub(r"\s+", " ", (v.get("name") or name).translate(_SUP_TR)).strip()
        attrs = ['tvg-id="%s"' % _attr(g.get("epg")), 'tvg-name="%s"' % _attr(name),
                 'tvg-logo="%s"' % _attr(g.get("icon")), 'tvg-chno="%d"' % n, 'group-title="%s"' % _attr(title)]
        out.append("#EXTINF:-1 %s,%s" % (" ".join(attrs), _attr(name).replace(",", " ")))
        out.append(stream_url("live", v["id"], "ts"))
        if g.get("epg"):
            epg_ids.append((g["epg"], g["name"], g.get("icon") or ""))
    text = "\n".join(out) + "\n"
    stats = {"n": n, "favs": nfav, "groups": len(buckets) + (1 if nfav else 0)}
    return text, epg_ids, stats


def tv_guide_xml(epg_ids):
    """Small XMLTV file with only the playlist's channels (the provider's full guide is ~50 MB)."""
    from xml.sax.saxutils import escape, quoteattr
    seen = {}
    for eid, name, icon in epg_ids:
        if eid not in seen:
            seen[eid] = (name, icon)
    now = int(time.time())
    rows = epg_rows(list(seen), now - 6 * 3600, now + 14 * 86400) if seen else {}
    fmt = lambda t: datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y%m%d%H%M%S +0000")  # noqa: E731
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<tv generator-info-name="Beeldbuis">']
    for eid, (name, icon) in seen.items():
        out.append('<channel id=%s><display-name>%s</display-name>%s</channel>' % (
            quoteattr(eid), escape(name), ('<icon src=%s/>' % quoteattr(icon)) if icon else ""))
    n = 0
    for eid in seen:
        for p in rows.get(eid, []):
            n += 1
            out.append('<programme start="%s" stop="%s" channel=%s><title>%s</title>%s</programme>' % (
                fmt(p["start"]), fmt(p["stop"]), quoteattr(eid), escape(p["title"] or ""),
                ("<desc>%s</desc>" % escape(p["description"])) if p.get("description") else ""))
    out.append("</tv>")
    return "\n".join(out) + "\n", n


def tv_write(path, text):
    """Write only when the content changed, in place (keeps the same Dropbox file and share link)."""
    data = text.encode("utf-8")
    try:
        with open(path, "rb") as f:
            if f.read() == data:
                return False
    except OSError:
        pass
    with open(path, "wb") as f:
        f.write(data)
    return True


def tv_export(reason="manual", folder=None):
    cfg = tv_settings()
    folder = (folder or cfg["dir"]).strip()
    if not folder:
        raise RuntimeError("Choose a folder for the TV playlist first (Settings → TV playlist).")
    folder = os.path.expanduser(folder)
    with TV_LOCK:
        if TV["status"] == "busy":
            raise RuntimeError("The TV playlist is being written right now; try again in a few seconds.")
        TV["status"] = "busy"
    try:
        text, epg_ids, stats = tv_build()
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, TV_M3U_NAME)
            changed = tv_write(path, text)
        except PermissionError:
            raise RuntimeError("macOS does not allow Beeldbuis to write in %s. Allow it in System Settings → "
                               "Privacy & Security → Files and Folders, or choose another folder." % folder)
        except OSError as e:
            raise RuntimeError("Could not write the TV playlist in %s: %s" % (folder, e.strerror or e))
        guide_path, guide_n = "", 0
        try:
            gx, guide_n = tv_guide_xml(epg_ids)
            guide_path = os.path.join(folder, TV_GUIDE_NAME)
            tv_write(guide_path, gx)
        except Exception as e:  # noqa  (guide is a bonus; the playlist matters)
            guide_path = ""
            print("TV guide not written: %s" % e)
        now = int(time.time())
        with TV_LOCK:
            TV.update({"status": "idle", "error": None, "t": now, "path": path, "guide_path": guide_path,
                       "guide_n": guide_n, "reason": reason, **stats})
            if changed:
                TV["changed"] = now
        return tv_status()
    except Exception as e:
        with TV_LOCK:
            TV["status"] = "idle"
            TV["error"] = str(e)
        raise


def tv_schedule(reason="settings", delay=3.0):
    """Re-write the playlist shortly after a change (debounced), when a folder is set and auto-update is on."""
    cfg = tv_settings()
    if not cfg["dir"] or not cfg["auto"]:
        return
    t = TV_TIMER.get("t")
    if t:
        t.cancel()
    def run():
        try:
            tv_export(reason)
        except Exception as e:  # noqa
            print("TV playlist not written: %s" % e)
    TV_TIMER["t"] = threading.Timer(delay, run)
    TV_TIMER["t"].daemon = True
    TV_TIMER["t"].start()


def tv_status():
    cfg = tv_settings()
    with TV_LOCK:
        st = dict(TV)
    st.update({"dir": cfg["dir"], "auto": cfg["auto"], "quality": cfg["quality"], "suggested": tv_suggested_dir(),
               "dropbox": dropbox_dirs(), "file": TV_M3U_NAME, "guide_file": TV_GUIDE_NAME})
    return st


def tv_choose_folder():
    """Native macOS folder picker (runs in the user's login session via the launchd agent)."""
    if sys.platform != "darwin":
        raise RuntimeError("Type the folder path instead.")
    start = tv_settings()["dir"] or (dropbox_dirs() or [os.path.expanduser("~")])[0]
    while start and not os.path.isdir(start):
        start = os.path.dirname(start)
    script = ('tell application "System Events" to activate\n'
              'set f to choose folder with prompt "Choose the Dropbox folder for the Beeldbuis TV playlist"'
              + (' default location (POSIX file %s)' % json.dumps(start) if start else "") +
              '\nreturn POSIX path of f')
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        raise RuntimeError("No folder chosen.")
    if r.returncode != 0:
        if "-128" in (r.stderr or ""):
            return {"cancelled": True}
        raise RuntimeError("The folder picker did not open (%s). Type the folder path instead." % (r.stderr or "").strip()[:160])
    path = r.stdout.strip().rstrip("/")
    return {"path": path}


def background_refresher():
    """Keeps the channel list and the guide fresh (guide about 3x per day)."""
    time.sleep(5)
    while True:
        with LOCK:
            logged_in = bool(STATE["server"])
        if logged_in:
            try:
                catalog_refresh()
            except Exception:  # noqa
                pass
            try:
                epg_refresh()
            except Exception:  # noqa
                pass
        time.sleep(300)


# ===================================================== fase 2/3: library ====
# Movies and series as one library: variants merged per title, metadata from TMDb/OMDb (cached),
# taste profiles, the "Tonight" home screen, search, Letterboxd import and OpenSubtitles.
import csv
import io
import unicodedata
import zipfile

KEYS_PATH = os.path.join(CONFIG_DIR, "sleutels.txt")        # copied from the work folder by the update script
TASTE_PATH = os.path.join(CONFIG_DIR, "smaak.json")         # personal taste profiles (never in the repo)
META_PATH = os.path.join(CONFIG_DIR, "meta.sqlite")
VOD_PATH = os.path.join(CONFIG_DIR, "vod.json.gz")
SUBS_DIR = os.path.join(CONFIG_DIR, "subs")
LB_ZIP = os.path.join(CONFIG_DIR, "letterboxd.zip")
VOD_TTL = 12 * 3600
TREND_TTL = 12 * 3600
HOME_TTL = 30 * 60
TMDB_BASE = os.environ.get("IPTV_TMDB_BASE", "https://api.themoviedb.org/3")
TMDB_IMG = os.environ.get("IPTV_TMDB_IMG", "https://image.tmdb.org/t/p")
OMDB_BASE = os.environ.get("IPTV_OMDB_BASE", "https://www.omdbapi.com/")
OSUB_BASE = os.environ.get("IPTV_OSUB_BASE", "https://api.opensubtitles.com/api/v1")
LB_BASE = os.environ.get("IPTV_LB_BASE", "https://letterboxd.com")

KEYS = {}
VOD = {"t": 0, "status": "idle", "error": None, "movies": [], "series": [], "cats": {}, "groups": {}, "by_norm": {}, "n": 0,
       "by_sid": {}, "movie_cats": [], "series_cats": []}
VOD_LOCK = threading.Lock()
META_LOCK = threading.Lock()
META_Q = []                      # keys waiting for TMDb/OMDb lookups
RATE_WANT = {}                   # titles on screen without IMDb/RT yet; newest last (rate_worker takes the newest first)
META_Q_LOCK = threading.Lock()
TREND = {"t": 0, "items": []}    # TMDb trending, matched against the library
HOME = {}                        # profile -> {"t", "rows", ...}
HOME_LOCK = threading.Lock()
LB = {"t": 0, "n": 0, "rss_t": 0, "error": None}
GENRES = {28: "Action", 12: "Adventure", 16: "Animation", 35: "Comedy", 80: "Crime", 99: "Documentary", 18: "Drama",
          10751: "Family", 14: "Fantasy", 36: "History", 27: "Horror", 10402: "Music", 9648: "Mystery", 10749: "Romance",
          878: "Science Fiction", 10770: "TV Movie", 53: "Thriller", 10752: "War", 37: "Western",
          10759: "Action & Adventure", 10762: "Kids", 10763: "News", 10764: "Reality", 10765: "Sci-Fi & Fantasy",
          10766: "Soap", 10767: "Talk", 10768: "War & Politics"}
GENRE_ALIAS = {"Sci-Fi & Fantasy": ("Science Fiction", "Fantasy"), "Action & Adventure": ("Action", "Adventure"),
               "War & Politics": ("War",)}
DEFAULT_VOD_LANGS = ["EN", "NF", "4K", "", "MULTI", "NL"]
_VOD_PREFIX_RE = re.compile(r"^\s*(\|?\[?[A-Z0-9+]{1,8}\]?\|?(?:-\[?[A-Z0-9+]{1,8}\]?)*|18\+)\s*[-|:]\s+|^\s*\[([A-Z]{2,5})\]\s+")
_LANG_TOKENS = {"EN", "NL", "NF", "MULTI", "DE", "SE", "SC", "IN", "DK", "IT", "ES", "PT", "FR", "TR", "PL", "AR", "TE", "TA", "KA", "PU", "TM", "GU", "MR", "MA", "NO", "FI", "RU", "GR", "HU", "RO", "CZ", "BG", "KR", "JP", "CN", "LAT", "BR", "AL", "EX", "YU", "AF", "WESTERN"}


def _prefix_lang(tok):
    """Language/source code from a provider prefix such as 'EN', '4K-DE', 'DE-BLURAY-DV' or '[SE]'; '4K' when only a quality tag."""
    parts = [t.strip("[]|") for t in tok.upper().split("-")]
    for t in parts:
        if t in _LANG_TOKENS:
            return t
    for t in parts:
        if t == "18+":
            return "18+"
    return "4K" if "4K" in parts else parts[0]
_VOD_YEAR_RE = re.compile(r"[\(\[]?((?:19|20)\d\d)[\)\]]?\s*$")
_VOD_TAG_RE = re.compile(r"\((?:[^()]*?SUB[^()]*|QUEBEC[^()]*|DUBBED|CAM|HDCAM|TS|SD|[^()]*DUB[^()]*|US|UK|AU|CA|NZ|IE)\)", re.I)
_VOD_QUAL_RE = re.compile(r"(?<![A-Za-z0-9])(4K|UHD|HDR|FHD|HD|SD|1080P|720P|2160P|HEVC|X265|H265|WEB-?DL|BLURAY|REMUX|IMAX|EXTENDED|UNRATED|DIRECTOR'?S CUT|REMASTERED)(?![A-Za-z0-9])", re.I)


# ------------------------------------------------------------------ keys ----
def keys_load():
    """API keys from sleutels.txt (KEY=value lines) merged with keys typed in Settings; never logged."""
    out = {}
    try:
        with open(KEYS_PATH, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip().lstrip("﻿")
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip().upper()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    with LOCK:
        for k, v in (STATE["settings"].get("keys") or {}).items():
            if v:
                out[k.upper()] = v
    KEYS.clear()
    KEYS.update(out)
    return out


def key(name):
    return KEYS.get(name.upper(), "")


def keys_status():
    return {"tmdb": bool(key("TMDB_API_KEY") or key("TMDB_READ_TOKEN")), "omdb": bool(key("OMDB_API_KEY")),
            "opensubtitles": bool(key("OPENSUBTITLES_API_KEY")), "letterboxd": key("LETTERBOXD_USER") or "",
            "file": os.path.exists(KEYS_PATH)}


def http_json(url, headers=None, timeout=25, data=None, method=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "Beeldbuis v%d" % VERSION, "Accept": "application/json"}, **(headers or {})),
                                 data=data, method=method)
    with urllib.request.urlopen(req, timeout=timeout, context=API_SSL) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ------------------------------------------------------------ title parse ----
def strip_accents(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm_title(s):
    s = strip_accents((s or "").lower()).replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"^(the|a|an) ", "", s)
    return re.sub(r"\s+", " ", s)


def vod_parse(name):
    """'EN - A Nightmare On Elm Street 4K (1984)' -> ('A Nightmare On Elm Street', 1984, 'EN', '4K')"""
    s = (name or "").strip()
    lang = ""
    m = _VOD_PREFIX_RE.match(s)
    pre4k = False
    if m:
        lang = _prefix_lang(m.group(1) or m.group(2) or "")
        pre4k = "4K" in (m.group(1) or "").upper()
        s = s[m.end():]
    s = _VOD_TAG_RE.sub(" ", s)
    year = 0
    m = re.search(r"[\(\[]((?:19|20)\d\d)[\)\]]", s) or _VOD_YEAR_RE.search(s)
    if m:
        year = int(m.group(1))
        s = s[:m.start()]
    q = "4K" if pre4k or re.search(r"(?<![A-Z0-9])(4K|UHD|2160P)(?![A-Z0-9])", s.upper()) else ""
    title = (name or "").strip()
    mm = _VOD_PREFIX_RE.match(title)
    if mm:
        title = title[mm.end():]
    title = _VOD_TAG_RE.sub(" ", title)
    my = re.search(r"[\(\[]((?:19|20)\d\d)[\)\]]", title) or _VOD_YEAR_RE.search(title)
    if my:
        title = title[:my.start()]
    title = _VOD_QUAL_RE.sub(" ", title)
    title = re.sub(r"\s*[-–:|]\s*$", "", re.sub(r"\s+", " ", title)).strip(" -–|:.")
    if title.isupper() and len(title) > 3:
        title = title.title()
    return title, year, lang, q


def vod_lang_visible(lang):
    with LOCK:
        langs = STATE["settings"].get("vod_langs")
    if not langs:                       # an empty choice would hide every title: use the defaults
        langs = DEFAULT_VOD_LANGS
    return lang in langs


def vod_adult(item, catname):
    return str(item.get("is_adult", "0")) in ("1", "true", "True") or (item.get("name") or "").startswith("18+") or \
        bool(re.search(r"\b(XXX|ADULT|PORN|18\+)\b", (catname or "").upper()))


# ------------------------------------------------------------- vod index ----
def vod_slim_movie(s, cats):
    return {"id": int(s["stream_id"]), "name": s.get("name") or "", "cat": str(s.get("category_id") or ""), "icon": s.get("stream_icon") or "",
            "added": int(s.get("added") or 0), "rating": float(s.get("rating") or 0) if str(s.get("rating") or "").replace(".", "", 1).isdigit() else 0.0,
            "ext": s.get("container_extension") or "mp4", "adult": 1 if vod_adult(s, cats.get(str(s.get("category_id") or ""), "")) else 0}


def vod_slim_series(s, cats):
    return {"id": int(s["series_id"]), "name": s.get("name") or "", "cat": str(s.get("category_id") or ""), "icon": s.get("cover") or "",
            "added": int(s.get("last_modified") or 0), "rating": float(s.get("rating") or 0) if str(s.get("rating") or "").replace(".", "", 1).isdigit() else 0.0,
            "genre": s.get("genre") or "", "date": s.get("releaseDate") or "", "adult": 1 if vod_adult(s, cats.get(str(s.get("category_id") or ""), "")) else 0}


def vod_refresh(force=False):
    with VOD_LOCK:
        if VOD["status"] == "busy":
            return False
        if not force and VOD["t"] and time.time() - VOD["t"] < VOD_TTL:
            return False
        VOD["status"] = "busy"
        VOD["error"] = None
    try:
        vc = xtream("get_vod_categories") or []
        sc = xtream("get_series_categories") or []
        cats = {str(c.get("category_id")): c.get("category_name") or "" for c in vc + sc}
        movies = [vod_slim_movie(s, cats) for s in (xtream("get_vod_streams") or []) if s.get("stream_id")]
        series = [vod_slim_series(s, cats) for s in (xtream("get_series") or []) if s.get("series_id")]
        data = {"t": int(time.time()), "cats": cats, "movies": movies, "series": series,
                "movie_cats": [{"id": str(c.get("category_id")), "name": c.get("category_name") or ""} for c in vc],
                "series_cats": [{"id": str(c.get("category_id")), "name": c.get("category_name") or ""} for c in sc]}
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = VOD_PATH + ".tmp"
        with gzip.open(tmp, "wt", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, VOD_PATH)
        vod_apply(data)
        return True
    except Exception as e:  # noqa
        with VOD_LOCK:
            VOD["status"] = "idle"
            VOD["error"] = str(e)
        return False


def vod_load():
    try:
        with gzip.open(VOD_PATH, "rt", encoding="utf-8") as f:
            vod_apply(json.load(f))
    except (OSError, ValueError):
        pass


def vod_apply(data):
    groups, by_norm = {}, {}
    for kind, items in (("movie", data["movies"]), ("series", data["series"])):
        for it in items:
            title, year, lang, q = vod_parse(it["name"])
            n = norm_title(title)
            if not n:
                continue
            gk = "%s:%s|%d" % (kind, n, year)
            g = groups.get(gk)
            var = {"id": it["id"], "lang": lang, "q": q, "cat": it["cat"], "name": it["name"], "adult": it["adult"],
                   "ext": it.get("ext", ""), "rating": it.get("rating", 0), "added": it.get("added", 0)}
            if not g:
                g = {"key": gk, "kind": kind, "title": title, "year": year, "norm": n, "icon": it["icon"], "variants": [var],
                     "added": it.get("added", 0), "rating": it.get("rating", 0), "adult": it["adult"], "genre": it.get("genre", "")}
                groups[gk] = g
                by_norm.setdefault(n, []).append(g)
            else:
                g["variants"].append(var)
                g["added"] = max(g["added"], it.get("added", 0))
                g["rating"] = max(g["rating"], it.get("rating", 0))
                g["adult"] = g["adult"] and it["adult"]
                if not g["icon"] and it["icon"]:
                    g["icon"] = it["icon"]
                if not g["genre"] and it.get("genre"):
                    g["genre"] = it["genre"]
    by_sid = {}
    for g in groups.values():
        g["variants"].sort(key=vod_variant_rank)
        for v in g["variants"]:
            by_sid["%s:%s" % (g["kind"], v["id"])] = g["key"]
    with VOD_LOCK:
        VOD.update({"t": data["t"], "status": "idle", "error": None, "cats": data["cats"], "groups": groups, "by_norm": by_norm, "by_sid": by_sid,
                    "movie_cats": data.get("movie_cats", []), "series_cats": data.get("series_cats", []),
                    "n": len(data["movies"]) + len(data["series"]), "movies": [], "series": []})
    home_invalidate()


_LANG_PREF = {"EN": 0, "NF": 1, "4K": 2, "": 3, "MULTI": 4, "NL": 6}


def vod_variant_rank(v):
    """Original audio first (EN/NF variants), 4K after FHD for stability, hidden languages last."""
    vis = 0 if vod_lang_visible(v["lang"]) else 50
    return (vis, _LANG_PREF.get(v["lang"], 5), 1 if v["q"] == "4K" else 0, -v.get("rating", 0))


def vod_group(gk):
    with VOD_LOCK:
        return VOD["groups"].get(gk)


def vod_visible(g, allow_dead=False):
    """allow_dead: also titles whose every edition the provider failed to serve (search, library, watchlist still show
    them, marked; recommendations leave them out)."""
    if g["adult"] and STATE["settings"].get("show_adult") is not True:
        return False
    vis = [v for v in g["variants"] if vod_lang_visible(v["lang"])]
    if not vis:
        return False
    return allow_dead or not vod_group_dead(g, vis)


def vod_find(kind, title, year=0, alt_title=""):
    """Match a TMDb/Letterboxd title against the library: exact normalized title, year within 1."""
    with VOD_LOCK:
        cands = list(VOD["by_norm"].get(norm_title(title), []))
        if alt_title and norm_title(alt_title) != norm_title(title):
            cands += VOD["by_norm"].get(norm_title(alt_title), [])
    cands = [g for g in cands if g["kind"] == kind and vod_visible(g)]
    if not cands:
        return None
    if year:
        exact = [g for g in cands if g["year"] == year]
        near = [g for g in cands if g["year"] and abs(g["year"] - year) <= 1]
        cands = exact or near or [g for g in cands if not g["year"]] or []
    return cands[0] if cands else None


def vod_tile(g, reason="", meta=None):
    m = meta if meta is not None else meta_cached(g["key"])
    t = {"key": g["key"], "kind": g["kind"], "title": g["title"], "year": g["year"], "icon": g["icon"], "reason": reason,
         "variants": len(g["variants"]), "langs": sorted({v["lang"] or "EN" for v in g["variants"] if vod_lang_visible(v["lang"])}),
         "has4k": any(v["q"] == "4K" for v in g["variants"]), "added": g["added"], "prating": g["rating"]}
    if vod_group_dead(g, [v for v in g["variants"] if vod_lang_visible(v["lang"])] or None):
        t["dead"] = True
    if m:
        t["meta"] = {k: m.get(k) for k in ("id", "poster", "backdrop", "vote", "votes", "genres", "overview", "imdb_rating", "rt", "mc", "runtime", "seasons", "imdb")}
    if ratings_wanted(m):
        rate_want(g["key"])
    return t


_MISSING_RE = re.compile(r"5\d\d|5XX|404|Not Found")
DEAD_TTL = 3 * 86400            # a missing file is retried after a few days: providers do fix or re-upload titles


def stream_error(kind, err, tried=0):
    """Error text for the player; a provider 5XX/404 on a film or episode means its server has no working file."""
    err = redact(err or "")
    if kind in ("movie", "series") and _MISSING_RE.search(err):
        also = (" Beeldbuis also tried %d other edition%s and file format%s; none worked." % (tried, "" if tried == 1 else "s", "" if tried == 1 else "s")) if tried else ""
        return ("Your provider lists this title, but its server has no working file for it (a provider-side error, not Beeldbuis)."
                + also + " It is marked as unavailable for now; Beeldbuis tries again in a few days. (%s)" % err)
    return "Stream not available: " + err


def vod_is_dead(kind, sid):
    with LOCK:
        t = STATE["dead_vod"].get("%s:%s" % (kind, sid))
    return bool(t) and time.time() - t < DEAD_TTL


def vod_group_dead(g, variants=None):
    """True when every (visible) edition of a title failed at the provider recently."""
    with LOCK:
        dead = STATE["dead_vod"]
        if not dead:
            return False
        now = time.time()
        vs = variants if variants is not None else g["variants"]
        return bool(vs) and all(now - dead.get("%s:%s" % (g["kind"], v["id"]), 0) < DEAD_TTL for v in vs)


def vod_mark_dead(kind, sid, dead=True):
    key = "%s:%s" % (kind, sid)
    with LOCK:
        d = STATE["dead_vod"]
        if dead:
            d[key] = int(time.time())
            for k in [k for k, t in d.items() if time.time() - t > DEAD_TTL]:
                del d[k]
        elif key in d:
            del d[key]
        else:
            return
    log_write("[vod] %s %s\n" % ("unavailable at provider:" if dead else "works again:", key))
    home_invalidate()
    save_config()


def vod_alternatives(kind, sid, ext):
    """Other ways to get the same film when the provider fails on this file: the extension get_vod_info reports,
    the other common container, then other editions of the same title (visible languages, original audio first)."""
    out, seen = [], {(str(sid), ext)}
    exts = []
    if kind == "movie":
        try:
            md = (xtream("get_vod_info", vod_id=sid, _timeout=15) or {}).get("movie_data") or {}
            if md.get("container_extension"):
                exts.append(md["container_extension"])
        except Exception:  # noqa
            pass
    # A scan of a real library (10 Nov 2026): a file that answers 5XX does so under every extension, so blindly trying
    # .mkv/.mp4 only costs time. Only the extension the provider itself reports is worth a try, then other editions.
    for e in exts:
        if (str(sid), e) not in seen:
            seen.add((str(sid), e))
            out.append({"id": sid, "ext": e, "label": "." + e})
    if kind == "series":
        out += episode_alternatives(sid, seen)
    if kind == "movie":
        with VOD_LOCK:
            gk = VOD["by_sid"].get("movie:%s" % sid)
        g = vod_group(gk) if gk else None
        if g:
            for v in g["variants"]:
                if str(v["id"]) == str(sid) or not vod_lang_visible(v["lang"]) or vod_is_dead("movie", v["id"]):
                    continue
                e = v.get("ext") or "mp4"
                if (str(v["id"]), e) not in seen:
                    seen.add((str(v["id"]), e))
                    out.append({"id": v["id"], "ext": e, "label": " · ".join(x for x in (v["lang"] or "EN", v["q"]) if x), "edition": True})
    return out[:5]


def episode_alternatives(sid, seen):
    """The same episode (season and number) in other editions of the series, for when the provider's file is broken."""
    rec = episodes_get(sid)
    g = vod_group(rec["gkey"]) if rec and rec.get("gkey") else None
    if not g or not rec.get("season") and not rec.get("episode"):
        return []
    out = []
    for v in g["variants"]:
        if len(out) >= 3:
            break
        if str(v["id"]) == str(rec.get("series_id") or "") or not vod_lang_visible(v["lang"]):
            continue
        try:
            info = xtream("get_series_info", series_id=v["id"], _timeout=15) or {}
        except Exception:  # noqa
            continue
        eps = info.get("episodes") or {}
        eps = eps.get(str(rec["season"])) if isinstance(eps, dict) else None
        for e in eps or []:
            try:
                same = int(e.get("episode_num") or 0) == rec["episode"]
            except (TypeError, ValueError):
                same = False
            ext = e.get("container_extension") or "mp4"
            if same and e.get("id") and (str(e["id"]), ext) not in seen and not vod_is_dead("ep", e["id"]):
                seen.add((str(e["id"]), ext))
                with EPISODES_LOCK:
                    EPISODES[str(e["id"])] = dict(rec, series_id=str(v["id"]))
                out.append({"id": e["id"], "ext": ext, "label": " · ".join(x for x in (v["lang"] or "EN", v["q"]) if x), "edition": True})
                break
    return out


GENRE_IDX = {"t": 0, "movie": {}, "series": {}}


def genre_index(kind):
    """Library key -> TMDb genre names, from the metadata cache; rebuilt every 5 minutes."""
    with META_Q_LOCK:
        fresh = time.time() - GENRE_IDX["t"] < 300
    if fresh:
        return GENRE_IDX[kind]
    idx = {"movie": {}, "series": {}}
    conn = meta_db()
    try:
        for k, kd, v in conn.execute("SELECT k, kind, v FROM meta"):
            try:
                genres = json.loads(v).get("genres") or []
            except ValueError:
                continue
            if genres and kd in idx:
                idx[kd][k] = genres
    finally:
        conn.close()
    with META_Q_LOCK:
        GENRE_IDX.update(idx)
        GENRE_IDX["t"] = time.time()
    return GENRE_IDX[kind]


def vod_list(kind, cat="", q="", limit=400, offset=0, genre=""):
    """Library listing for the Movies / Series tabs: one entry per title, variants merged.
    `genre` filters on TMDb genre names (only titles with cached metadata have genres); `genres` lists names with counts."""
    q = norm_title(q)
    genre = (genre or "").strip().lower()
    with VOD_LOCK:
        groups = [g for g in VOD["groups"].values() if g["kind"] == kind]
    gidx = genre_index(kind)
    out = []
    counts = {}
    for g in groups:
        if not vod_visible(g, allow_dead=True):
            continue
        if cat and not any(v["cat"] == cat for v in g["variants"]):
            continue
        if q and q not in g["norm"]:
            continue
        gs = gidx.get(g["key"]) or []
        for name in gs:
            counts[name] = counts.get(name, 0) + 1
        if genre and genre not in [x.lower() for x in gs]:
            continue
        out.append(g)
    out.sort(key=lambda g: (-g["added"], g["title"]))
    total = len(out)
    return {"total": total, "items": [vod_tile(g) for g in out[offset:offset + limit]],
            "loading": not VOD["t"] and not VOD["error"], "error": VOD["error"] if not VOD["t"] else None,
            "cats": [c for c in (VOD.get("movie_cats") if kind == "movie" else VOD.get("series_cats")) or [] if vod_cat_visible(c["name"])],
            "genres": [{"name": n, "n": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:40]],
            "genre": genre}


def vod_cat_visible(name):
    if re.search(r"\b(XXX|ADULT|PORN|18\+)\b", (name or "").upper()) and STATE["settings"].get("show_adult") is not True:
        return False
    m = re.match(r"^\s*\|?([A-Z0-9+]{1,6})\|", (name or "").upper())
    return vod_lang_visible(m.group(1) if m else "")


RATING_FIELDS = ("imdb_rating", "rt", "mc", "vote", "imdb", "poster")


def ratings_for(keys=(), ids=()):
    """IMDb / Rotten Tomatoes / Metacritic for library keys or provider stream ids ("movie:123", "series:45", as the TV
    app knows them). Titles without ratings yet are queued, so asking again a little later fills them in."""
    with VOD_LOCK:
        by_sid = VOD.get("by_sid") or {}
        pairs = [(k, k) for k in keys if k in VOD["groups"]] + [(i, by_sid[i]) for i in ids if i in by_sid]
    metas = meta_cached_many({gk for _, gk in pairs})
    out = {}
    pairs = pairs[:400]
    for ask, gk in reversed(pairs):           # first on screen ends up first in line
        if ratings_wanted(metas.get(gk)):
            rate_want(gk)
    for ask, gk in pairs:
        m = metas.get(gk)
        if m and not m.get("miss"):
            out[ask] = dict({f: m.get(f) for f in RATING_FIELDS if m.get(f) not in (None, "")}, key=gk)
    return out


def vod_item(gk):
    g = vod_group(gk)
    if not g:
        raise RuntimeError("Not in the library any more")
    out = dict(g)
    out["meta"] = meta_get(g, fetch=True)
    vis = [v for v in g["variants"] if vod_lang_visible(v["lang"])] or g["variants"]
    # editions the provider could not serve go last and are marked; the first one that works is the default
    out["variants"] = sorted((dict(v, dead=True) if vod_is_dead(g["kind"], v["id"]) else v for v in vis), key=lambda v: bool(v.get("dead")))
    out["dead"] = all(v.get("dead") for v in out["variants"])
    with LOCK:
        out["position"] = next((p for k, p in STATE["positions"].items() if p and p.get("gkey") == gk), None)
    out["feedback"] = feedback_for(gk)
    out["watchlist"] = watchlist_for(gk)
    return out


# ------------------------------------------------------------ metadata db ----
META_READY = {"path": None}
META_INIT_LOCK = threading.Lock()


def meta_db():
    conn = sqlite3.connect(META_PATH, timeout=30)
    if META_READY["path"] == META_PATH and os.path.exists(META_PATH):
        return conn
    with META_INIT_LOCK:
        try:
            _meta_schema(conn)
        except Exception:
            conn.close()
            raise
        META_READY["path"] = META_PATH
    return conn


def _meta_schema(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, kind TEXT, tmdb INTEGER, v TEXT, t INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, t INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS feedback (profile TEXT, k TEXT, verdict TEXT, t INTEGER, PRIMARY KEY(profile, k))")
    conn.execute("CREATE TABLE IF NOT EXISTS shown (profile TEXT, k TEXT, n INTEGER, t INTEGER, PRIMARY KEY(profile, k))")
    conn.execute("CREATE TABLE IF NOT EXISTS lb (profile TEXT, norm TEXT, year INTEGER, title TEXT, rating REAL, watched INTEGER, "
                 "watchlist INTEGER, tmdb INTEGER, t INTEGER, PRIMARY KEY(profile, norm, year))")
    if "liked" not in [r[1] for r in conn.execute("PRAGMA table_info(lb)")]:
        conn.execute("ALTER TABLE lb ADD COLUMN liked INTEGER DEFAULT 0")
    conn.execute("CREATE TABLE IF NOT EXISTS subs (k TEXT PRIMARY KEY, file TEXT, src TEXT, t INTEGER)")
    if not conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='watch'").fetchone():
        # the watchlist only holds what you tick yourself; an old "watchlist" verdict was a tick too, so it moves over once
        conn.execute("CREATE TABLE watch (profile TEXT, k TEXT, t INTEGER, PRIMARY KEY(profile, k))")
        conn.execute("INSERT OR IGNORE INTO watch (profile, k, t) SELECT profile, k, t FROM feedback WHERE verdict='watchlist'")
        conn.execute("DELETE FROM feedback WHERE verdict='watchlist'")
    conn.execute("CREATE TABLE IF NOT EXISTS history (profile TEXT, k TEXT, kind TEXT, title TEXT, secs REAL, dur REAL, pct REAL, "
                 "sessions INTEGER, done INTEGER, dropped INTEGER, last INTEGER, PRIMARY KEY(profile, k))")
    conn.commit()


def cache_get(k, ttl):
    conn = meta_db()
    try:
        r = conn.execute("SELECT v, t FROM cache WHERE k=?", (k,)).fetchone()
    finally:
        conn.close()
    if r and time.time() - r[1] < ttl:
        return json.loads(r[0])
    return None


def cache_put(k, v):
    conn = meta_db()
    try:
        conn.execute("INSERT OR REPLACE INTO cache (k, v, t) VALUES (?,?,?)", (k, json.dumps(v), int(time.time())))
        conn.commit()
    finally:
        conn.close()


def tmdb(path, ttl=7 * 86400, **params):
    """TMDb GET with an on-disk cache. Returns None without a key or on error."""
    k = key("TMDB_API_KEY")
    tok = key("TMDB_READ_TOKEN")
    if not (k or tok):
        return None
    params = {kk: vv for kk, vv in params.items() if vv not in (None, "")}
    ck = "tmdb:" + path + "?" + urllib.parse.urlencode(sorted(params.items()))
    hit = cache_get(ck, ttl)
    if hit is not None:
        return hit
    if k:
        params["api_key"] = k
    url = TMDB_BASE + path + "?" + urllib.parse.urlencode(params)
    headers = {"Authorization": "Bearer " + tok} if (tok and not k) else {}
    for attempt in range(3):
        try:
            data = http_json(url, headers)
            cache_put(ck, data)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if e.code == 404:
                cache_put(ck, {})
                return {}
            return None
        except Exception:  # noqa
            time.sleep(1)
    return None


OMDB_STATE = {"day": "", "n": -1, "blocked": ""}
OMDB_LOCK = threading.Lock()


def omdb_budget(reserve=0):
    """OMDb's free key allows about 1000 requests a day (UTC). Count them (kept in the cache across restarts) and stop
    for the day when OMDb says the limit is reached. Background enrichment keeps `reserve` requests free for titles
    someone opens or sees on screen. OMDB_DAILY_LIMIT in sleutels.txt overrides the limit (paid keys)."""
    today = time.strftime("%Y-%m-%d", time.gmtime())
    with OMDB_LOCK:
        if OMDB_STATE["day"] != today or OMDB_STATE["n"] < 0:
            saved = cache_get("omdb_budget", 2 * 86400) or {}
            OMDB_STATE.update(day=today, n=int(saved.get("n", 0)) if saved.get("day") == today else 0,
                              blocked=saved.get("blocked", "") if saved.get("day") == today else "")
        if OMDB_STATE["blocked"] == today:
            return False
        try:
            lim = int(key("OMDB_DAILY_LIMIT") or 950)
        except ValueError:
            lim = 950
        return OMDB_STATE["n"] + reserve < lim


def omdb_count(blocked=False):
    with OMDB_LOCK:
        OMDB_STATE["n"] += 1
        if blocked:
            OMDB_STATE["blocked"] = OMDB_STATE["day"]
        snap = dict(OMDB_STATE)
    cache_put("omdb_budget", snap)


def omdb(imdb_id, ttl=None, background=False):
    """OMDb record (IMDb rating, Rotten Tomatoes, Metacritic) for an IMDb id. None = not asked (no key, daily limit,
    network error; try again later), {} = OMDb has nothing for this id."""
    k = key("OMDB_API_KEY")
    if not (k and imdb_id):
        return None
    ck = "omdb:" + imdb_id
    hit = cache_get(ck, ttl or 30 * 86400)
    if hit is not None and (hit or cache_get(ck, 7 * 86400) is not None):
        return hit
    if not omdb_budget(250 if background else 0):
        return None
    try:
        data = http_json(OMDB_BASE + "?" + urllib.parse.urlencode({"i": imdb_id, "apikey": k, "tomatoes": "true"}))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa
            body = ""
        omdb_count(blocked=bool(re.search(r"limit", body, re.I)) or e.code == 401)
        return None
    except Exception:  # noqa
        return None
    if isinstance(data, dict) and re.search(r"limit", str(data.get("Error", "")), re.I):
        omdb_count(blocked=True)
        return None
    omdb_count()
    if not isinstance(data, dict) or data.get("Response") == "False":
        cache_put(ck, {})
        return {}
    cache_put(ck, data)
    return data


def omdb_apply(m, background=False):
    """Copy IMDb / RT / Metacritic from OMDb into a metadata record. True when OMDb answered (also with nothing)."""
    o = omdb(m.get("imdb"), background=background)
    if o is None:
        return False
    m["omdb_t"] = int(time.time())
    if not o:
        return True
    try:
        m["imdb_rating"] = float(o.get("imdbRating")) if o.get("imdbRating") not in (None, "N/A") else None
    except ValueError:
        m["imdb_rating"] = None
    try:
        m["imdb_votes"] = int(str(o.get("imdbVotes", "")).replace(",", ""))
    except ValueError:
        pass
    rt = next((x.get("Value") for x in o.get("Ratings", []) if x.get("Source") == "Rotten Tomatoes"), None)
    m["rt"] = int(rt.rstrip("%")) if rt and rt.rstrip("%").isdigit() else None
    m["mc"] = int(o["Metascore"]) if str(o.get("Metascore", "")).isdigit() else None
    if o.get("Rated") and o["Rated"] != "N/A":
        m["rated"] = o["Rated"]
    return True


def ratings_wanted(m):
    """True when a title still needs a TMDb detail lookup or an OMDb rating (used to queue titles that are on screen)."""
    if not m:
        return True
    if m.get("miss"):
        return False
    if not m.get("detail"):
        return True
    return bool(m.get("imdb")) and not m.get("omdb_t") and bool(key("OMDB_API_KEY")) and omdb_budget()


def meta_pack(kind, r):
    """Compact record from a TMDb list or detail payload."""
    date = r.get("release_date") if kind == "movie" else r.get("first_air_date")
    m = {"id": r.get("id"), "title": r.get("title") or r.get("name") or "", "orig": r.get("original_title") or r.get("original_name") or "",
         "year": int(date[:4]) if date and date[:4].isdigit() else 0, "overview": (r.get("overview") or "")[:600],
         "poster": r.get("poster_path") or "", "backdrop": r.get("backdrop_path") or "", "vote": round(float(r.get("vote_average") or 0), 1),
         "votes": int(r.get("vote_count") or 0), "pop": round(float(r.get("popularity") or 0), 1), "lang": r.get("original_language") or "",
         "genre_ids": [g["id"] for g in r.get("genres", [])] if r.get("genres") else list(r.get("genre_ids") or [])}
    m["genres"] = genre_names(m["genre_ids"])
    if r.get("runtime"):
        m["runtime"] = r["runtime"]
    if r.get("episode_run_time"):
        m["runtime"] = (r["episode_run_time"] or [0])[0]
    if r.get("number_of_seasons"):
        m["seasons"] = r["number_of_seasons"]
    cc = [c.get("iso_3166_1") for c in r.get("production_countries") or [] if c.get("iso_3166_1")] or list(r.get("origin_country") or [])
    if cc:
        m["countries"] = cc[:4]
    if r.get("status"):
        m["status"] = r["status"]
    if r.get("production_companies"):
        m["companies"] = [c.get("name") for c in r["production_companies"]][:6]
    if r.get("networks"):
        m["companies"] = (m.get("companies") or []) + [c.get("name") for c in r["networks"]][:4]
    ext = r.get("external_ids") or {}
    if ext.get("imdb_id") or r.get("imdb_id"):
        m["imdb"] = ext.get("imdb_id") or r.get("imdb_id")
    if r.get("next_episode_to_air"):
        m["next_ep"] = (r["next_episode_to_air"] or {}).get("air_date")
    if r.get("last_air_date"):
        m["last_air"] = r["last_air_date"]
    return m


def genre_names(ids):
    out = []
    for i in ids or []:
        n = GENRES.get(i)
        if not n:
            continue
        for a in GENRE_ALIAS.get(n, (n,)):
            if a not in out:
                out.append(a)
    return out


def meta_reset_misses():
    """Once (v21): titles marked 'not found' and empty OMDb answers date from when https failed; look them up again."""
    with LOCK:
        if STATE["settings"].get("meta_reset") == 21:
            return
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)       # a fresh install has no folder yet
        conn = meta_db()
        try:
            n = conn.execute("DELETE FROM meta WHERE tmdb=0 AND v LIKE '%\"miss\": true%'").rowcount
            n2 = conn.execute("DELETE FROM cache WHERE k LIKE 'omdb:%' AND v='{}'").rowcount
            conn.commit()
        finally:
            conn.close()
        print("meta: cleared %d misses and %d empty OMDb answers" % (n, n2))
    except Exception as e:  # noqa
        print("meta reset failed: %s" % e)
        return
    with LOCK:
        STATE["settings"]["meta_reset"] = 21
    save_config()


def meta_cached(gk):
    conn = meta_db()
    try:
        r = conn.execute("SELECT v FROM meta WHERE k=?", (gk,)).fetchone()
    finally:
        conn.close()
    return json.loads(r[0]) if r else None


def meta_cached_many(keys):
    if not keys:
        return {}
    conn = meta_db()
    try:
        out = {}
        ks = list(keys)
        for i in range(0, len(ks), 400):
            part = ks[i:i + 400]
            for k, v in conn.execute("SELECT k, v FROM meta WHERE k IN (%s)" % ",".join("?" * len(part)), part):
                out[k] = json.loads(v)
        return out
    finally:
        conn.close()


def meta_store(gk, kind, m):
    m["t"] = int(time.time())
    conn = meta_db()
    try:
        conn.execute("INSERT OR REPLACE INTO meta (k, kind, tmdb, v, t) VALUES (?,?,?,?,?)", (gk, kind, m.get("id") or 0, json.dumps(m), m["t"]))
        conn.commit()
    finally:
        conn.close()


def tmdb_search(kind, title, year=0):
    path = "/search/movie" if kind == "movie" else "/search/tv"
    params = {"query": title, "include_adult": "false"}
    if year:
        params["year" if kind == "movie" else "first_air_date_year"] = year
    data = tmdb(path, 30 * 86400, **params)
    if data is None:
        return "ERR"            # network / key problem: not the same as "TMDb doesn't know this title"
    results = data.get("results") or []
    if not results and year:
        data = tmdb(path, 30 * 86400, query=title, include_adult="false")
        if data is None:
            return "ERR"
        results = data.get("results") or []
    if not results:
        return None
    n = norm_title(title)
    def score(r):
        t = norm_title(r.get("title") or r.get("name") or "")
        o = norm_title(r.get("original_title") or r.get("original_name") or "")
        d = (r.get("release_date") or r.get("first_air_date") or "")[:4]
        y = int(d) if d.isdigit() else 0
        s = 0
        if t == n or o == n:
            s += 50
        elif n in t or t in n:
            s += 20
        if year and y:
            s += 30 if y == year else (15 if abs(y - year) <= 1 else -10)
        s += min(20, (r.get("vote_count") or 0) / 100)
        return s
    results.sort(key=score, reverse=True)
    best = results[0]
    if score(best) < 20:
        return None
    return best


def omdb_worth(m):
    """Background enrichment spends the small daily OMDb budget only on titles that could end up on a shelf or in a
    "best rated" / "hidden gems" list (decent TMDb score from a few dozen voters). Others get IMDb/RT when they are opened or shown."""
    return (m.get("vote") or 0) >= 6.4 and (m.get("votes") or 0) >= 25


def meta_get(g, fetch=True, details=True, background=False):
    """Metadata for a library group: cached TMDb record, fetched (search + details + OMDb) when allowed.
    A miss ("TMDb doesn't know it") is retried after 3 days; a network error is never stored as a miss."""
    gk = g["key"]
    m = meta_cached(gk)
    if m and not fetch:
        return m
    age = time.time() - (m or {}).get("t", 0)
    fresh = m and age < (3 * 86400 if m.get("miss") else 14 * 86400)
    if m and fresh and (m.get("miss") or not details or m.get("detail")):
        if details and m.get("imdb") and not m.get("omdb_t") and key("OMDB_API_KEY") and (not background or omdb_worth(m)):
            if omdb_apply(m, background):
                meta_store(gk, g["kind"], m)
        return m
    if not fetch or not (key("TMDB_API_KEY") or key("TMDB_READ_TOKEN")):
        return m
    kind = g["kind"]
    if m and m.get("id"):
        r = {"id": m["id"]}
    else:
        r = tmdb_search(kind, g["title"], g["year"])
        if r == "ERR":
            return m
    if not r:
        meta_store(gk, kind, dict(m or {}, id=0, miss=True))
        return meta_cached(gk)
    if details:
        d = tmdb("/%s/%d" % ("movie" if kind == "movie" else "tv", int(r["id"])), 14 * 86400, append_to_response="external_ids")
        if d:
            r = d
    m2 = meta_pack(kind, r) if r.get("title") or r.get("name") else dict(m or {})
    if m:
        for kk, vv in m.items():
            if kk not in ("miss",):
                m2.setdefault(kk, vv)
    m2.pop("miss", None)
    m2["detail"] = bool(details and r.get("genres") is not None) or bool((m or {}).get("detail"))
    if details and m2.get("imdb") and (not background or omdb_worth(m2)):
        omdb_apply(m2, background)
    meta_store(gk, kind, m2)
    return m2


def meta_enqueue(keys, front=False):
    with META_Q_LOCK:
        for k in keys:
            if k in META_Q:
                continue
            if front:
                META_Q.insert(0, k)
            else:
                META_Q.append(k)


BACKFILL = {"keys": [], "vt": 0, "t": 0}    # library titles still without a TMDb record, best first (meta_worker only)


def backfill_next():
    """Next library title without a TMDb record: recently added first, then the provider's best-rated. Rebuilt when the
    catalogue changes or once an hour, so the shelves and genre chips keep filling in the background."""
    if not BACKFILL["keys"] and (BACKFILL["vt"] != VOD.get("t") or time.time() - BACKFILL["t"] > 3600):
        BACKFILL["vt"], BACKFILL["t"] = VOD.get("t"), time.time()
        conn = meta_db()
        try:
            have = {k for (k,) in conn.execute("SELECT k FROM meta WHERE v LIKE '%\"detail\": true%' OR v LIKE '%\"miss\": true%'")}
        finally:
            conn.close()
        with VOD_LOCK:
            groups = [g for g in VOD["groups"].values() if g["key"] not in have and not g["adult"]]
        groups = [g for g in groups if vod_visible(g)]
        now = time.time()
        groups.sort(key=lambda g: (-(now - g["added"] < 90 * 86400), -g["rating"], -g["added"]))
        BACKFILL["keys"] = [g["key"] for g in groups]
    return BACKFILL["keys"].pop(0) if BACKFILL["keys"] else None


def meta_worker():
    """Fills in TMDb/OMDb records for queued titles, one at a time, gently; when nothing is queued it works through the
    library in the background: titles that were on screen without ratings first, then the rest of the library (TMDb details
    + OMDb while the daily OMDb budget leaves room; see omdb_budget)."""
    while True:
        gk = None
        with META_Q_LOCK:
            if META_Q:
                gk = META_Q.pop(0)
        if gk:
            g = vod_group(gk)
            if g:
                try:
                    meta_get(g, fetch=True, details=True)
                except Exception:  # noqa
                    pass
            time.sleep(0.25)
            continue
        if not VOD["n"] or not (key("TMDB_API_KEY") or key("TMDB_READ_TOKEN")):
            time.sleep(5)
            continue
        bg = True
        try:
            gk = backfill_next()
        except Exception:  # noqa
            gk = None
        if not gk:
            time.sleep(1.5)
            continue
        g = vod_group(gk)
        if g:
            try:
                meta_get(g, fetch=True, details=True, background=bg)
            except Exception:  # noqa
                pass
        time.sleep(0.5)


def rate_want(gk):
    """Remember a title that is on screen without ratings; asking again moves it to the front."""
    with META_Q_LOCK:
        RATE_WANT.pop(gk, None)
        RATE_WANT[gk] = 1
        while len(RATE_WANT) > 3000:
            RATE_WANT.pop(next(iter(RATE_WANT)))


def rate_worker():
    """Looks up IMDb / Rotten Tomatoes for titles that are on screen right now, newest request first, next to the
    slower background backfill; the screen asks /api/ratings again and fills the badges in."""
    while True:
        with META_Q_LOCK:
            gk = RATE_WANT.popitem()[0] if RATE_WANT else None
        if not gk:
            time.sleep(0.5)
            continue
        g = vod_group(gk)
        if g:
            try:
                meta_get(g, fetch=True, details=True)
            except Exception:  # noqa
                pass
        time.sleep(0.12)


def poster_url(path, size="w342"):
    return "%s/%s%s" % (TMDB_IMG, size, path) if path else ""


# --------------------------------------------------------------- trending ----
def trending_refresh(force=False):
    if not (key("TMDB_API_KEY") or key("TMDB_READ_TOKEN")):
        return
    if not force and time.time() - TREND["t"] < TREND_TTL:
        return
    items = []
    for kind, path in (("movie", "/trending/movie/day"), ("movie", "/trending/movie/week"), ("series", "/trending/tv/day"), ("series", "/trending/tv/week")):
        data = tmdb(path, 6 * 3600) or {}
        for rank, r in enumerate((data.get("results") or [])[:50], 1):
            m = meta_pack(kind, r)
            g = vod_find(kind, m["title"], m["year"], m["orig"])
            if not g:
                continue
            period = "day" if path.endswith("/day") else "week"
            cur = next((x for x in items if x["key"] == g["key"]), None)
            if cur:
                cur["rank"] = min(cur["rank"], rank)
                cur["periods"].append(period)
            else:
                items.append({"key": g["key"], "kind": kind, "rank": rank, "periods": [period], "meta": m})
            old = meta_cached(g["key"])
            if not old or not old.get("id"):
                meta_store(g["key"], kind, dict(m, detail=False))
    TREND["items"] = items
    TREND["t"] = time.time()
    meta_enqueue([x["key"] for x in items[:40]])
    home_invalidate()


# --------------------------------------------------------------- profiles ----
# Profiles are neutral in the code ("Profile 1", "Profile 2", "Together"); real names and taste come from the
# personal smaak.json that the updater copies next to the app (never part of the repository).
# Older installs may have stored data under personal profile ids; smaak.json "legacy_ids" maps those to p1/p2/both.
LEGACY_PROFILE_IDS = {}


def legacy_ids_load():
    try:
        with open(TASTE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        LEGACY_PROFILE_IDS.clear()
        LEGACY_PROFILE_IDS.update({str(k).lower(): v for k, v in (data.get("legacy_ids") or {}).items() if v in ("p1", "p2", "both")})
    except (OSError, ValueError):
        pass


def profile_id(pid):
    pid = (pid or "p1").lower()
    return LEGACY_PROFILE_IDS.get(pid, pid) if pid else "p1"


def migrate_profile_ids():
    """One-time rename of profile ids stored by update 8 (v9), driven by smaak.json "legacy_ids"."""
    legacy_ids_load()
    if not LEGACY_PROFILE_IDS:
        return
    conn = meta_db()
    try:
        for table in ("feedback", "shown", "lb", "history", "watch"):
            try:
                for old, new in LEGACY_PROFILE_IDS.items():
                    conn.execute("UPDATE OR IGNORE %s SET profile=? WHERE profile=?" % table, (new, old))
            except sqlite3.Error:
                pass
        conn.commit()
    finally:
        conn.close()
    changed = False
    with LOCK:
        for pos in STATE["positions"].values():
            if pos and pos.get("profile") in LEGACY_PROFILE_IDS:
                pos["profile"] = LEGACY_PROFILE_IDS[pos["profile"]]
                changed = True
        st = STATE["settings"]
        if st.get("profile") in LEGACY_PROFILE_IDS:
            st["profile"] = LEGACY_PROFILE_IDS[st["profile"]]
            changed = True
        if isinstance(st.get("profiles"), dict):
            for old, new in LEGACY_PROFILE_IDS.items():
                if old in st["profiles"]:
                    st["profiles"][new] = {**st["profiles"].pop(old), **st["profiles"].get(new, {})}
                    changed = True
    if changed:
        save_config()


DEFAULT_PROFILES = {
    "p1": {"name": "Profile 1", "anchors": [], "genres": [], "avoid_genres": [], "exclude": [], "min_imdb": 7.0, "min_rt": 80,
            "sports": [], "teams": [], "surprise": 2},
    "p2": {"name": "Profile 2", "anchors": [], "genres": [], "avoid_genres": [], "exclude": [], "min_imdb": 7.0, "min_rt": 80,
               "sports": [], "teams": [], "surprise": 1},
    "both": {"name": "Together", "anchors": [], "genres": [], "avoid_genres": [], "exclude": [], "min_imdb": 7.0, "min_rt": 80,
              "sports": [], "teams": [], "surprise": 1, "together": True},
}


def profiles():
    """Taste profiles: smaak.json (shipped with the package, personal) overlaid with edits saved in Settings."""
    out = json.loads(json.dumps(DEFAULT_PROFILES))
    try:
        with open(TASTE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for pid, p in (data.get("profiles") or {}).items():
            out.setdefault(profile_id(pid), {}).update(p)
        out["_lists"] = data.get("lists") or {}
    except (OSError, ValueError):
        out["_lists"] = {}
    with LOCK:
        edits = STATE["settings"].get("profiles") or {}
    for pid, p in edits.items():
        if pid in out and isinstance(p, dict):
            out[pid].update(p)
    return out


def profile(pid):
    ps = profiles()
    pid = profile_id(pid)
    p = dict(ps.get(pid) or ps["p1"])
    p["id"] = pid if pid in ps else "p1"
    p["_lists"] = ps.get("_lists", {})
    return p


def watchlist_set(pid, gk, on):
    """Tick or untick a title on a profile's watchlist (independent of 👍/👎/Seen)."""
    if not gk or (on and not vod_group(gk)):
        raise RuntimeError("This title is not in the library")
    conn = meta_db()
    try:
        if on:
            conn.execute("INSERT OR IGNORE INTO watch (profile, k, t) VALUES (?,?,?)", (pid, gk, int(time.time())))
        else:
            conn.execute("DELETE FROM watch WHERE profile=? AND k=?", (pid, gk))
        conn.commit()
    finally:
        conn.close()
    home_invalidate()
    return {"key": gk, "on": bool(on)}


def watchlist_rows(pid):
    """[(key, profile, t)] newest first; "both" (Together) sees the ticks of everyone."""
    conn = meta_db()
    try:
        if pid == "both":
            return conn.execute("SELECT k, profile, MAX(t) FROM watch GROUP BY k ORDER BY MAX(t) DESC").fetchall()
        return conn.execute("SELECT k, profile, t FROM watch WHERE profile=? ORDER BY t DESC", (pid,)).fetchall()
    finally:
        conn.close()


def watchlist_api(pid, kind=""):
    keys = [r[0] for r in watchlist_rows(pid)]
    out = {"keys": keys}
    if kind in ("movie", "series"):      # the TV's Watchlist list needs the tiles too
        out["items"] = [vod_tile(g) for g in (vod_group(k) for k in keys) if g and g["kind"] == kind and vod_visible(g)]
    return out


def watchlist_keys(pid):
    return {r[0] for r in watchlist_rows(profile_id(pid))}


def watchlist_for(gk):
    conn = meta_db()
    try:
        return {r[0]: True for r in conn.execute("SELECT profile FROM watch WHERE k=?", (gk,))}
    finally:
        conn.close()


def feedback_set(pid, gk, verdict):
    if verdict == "watchlist":       # older TV app: "Watchlist" was one of the verdicts
        return watchlist_set(pid, gk, True)
    conn = meta_db()
    try:
        if verdict in (None, "", "clear"):
            conn.execute("DELETE FROM feedback WHERE profile=? AND k=?", (pid, gk))
        else:
            conn.execute("INSERT OR REPLACE INTO feedback (profile, k, verdict, t) VALUES (?,?,?,?)", (pid, gk, verdict, int(time.time())))
        conn.commit()
    finally:
        conn.close()
    home_invalidate()


def feedback_all(pid):
    conn = meta_db()
    try:
        return {k: v for k, v in conn.execute("SELECT k, verdict FROM feedback WHERE profile=?", (pid,))}
    finally:
        conn.close()


def feedback_for(gk):
    conn = meta_db()
    try:
        return {p: v for p, v in conn.execute("SELECT profile, verdict FROM feedback WHERE k=?", (gk,))}
    finally:
        conn.close()


def shown_bump(pid, keys):
    if not keys:
        return
    conn = meta_db()
    try:
        now = int(time.time())
        for k in keys:
            conn.execute("INSERT INTO shown (profile, k, n, t) VALUES (?,?,1,?) ON CONFLICT(profile, k) DO UPDATE SET n=n+1, t=excluded.t", (pid, k, now))
        conn.commit()
    finally:
        conn.close()


def shown_tired(pid):
    """Wildcards ignored three times rest for 30 days."""
    conn = meta_db()
    try:
        since = int(time.time()) - 30 * 86400
        return {k for (k,) in conn.execute("SELECT k FROM shown WHERE profile=? AND n>=3 AND t>?", (pid, since))}
    finally:
        conn.close()


# -------------------------------------------------------------- letterboxd ----
def lb_import_zip(path=LB_ZIP, pid="p1"):
    """Letterboxd data export (Settings -> Import & Export): watched, ratings, watchlist, diary and liked films."""
    rows = {}
    def add(name, year, **kw):
        n = norm_title(name)
        if not n:
            return
        y = int(year) if str(year).isdigit() else 0
        r = rows.setdefault((n, y), {"title": name, "rating": None, "watched": 0, "watchlist": 0, "tmdb": 0, "liked": 0})
        r.update({k: v for k, v in kw.items() if v not in (None, "")})
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            base = os.path.basename(info.filename).lower()
            if info.filename.lower().startswith(("deleted/", "orphaned/")):
                continue
            if "likes/" in info.filename.lower().replace("\\", "/") and base == "films.csv":
                base = "likes.csv"
            if base not in ("watched.csv", "ratings.csv", "watchlist.csv", "diary.csv", "likes.csv"):
                continue
            text = z.read(info).decode("utf-8", "replace")
            for rec in csv.DictReader(io.StringIO(text)):
                name, year = rec.get("Name") or "", rec.get("Year") or ""
                if base == "ratings.csv":
                    try:
                        add(name, year, rating=float(rec.get("Rating") or 0), watched=1)
                    except ValueError:
                        add(name, year, watched=1)
                elif base == "watchlist.csv":
                    add(name, year, watchlist=1)
                elif base == "likes.csv":
                    add(name, year, liked=1, watched=1)
                else:
                    add(name, year, watched=1)
                    if rec.get("Rating"):
                        try:
                            add(name, year, rating=float(rec["Rating"]))
                        except ValueError:
                            pass
    conn = meta_db()
    try:
        now = int(time.time())
        for (n, y), r in rows.items():
            conn.execute("INSERT INTO lb (profile, norm, year, title, rating, watched, watchlist, tmdb, t, liked) VALUES (?,?,?,?,?,?,?,?,?,?) "
                         "ON CONFLICT(profile, norm, year) DO UPDATE SET title=excluded.title, rating=COALESCE(excluded.rating, lb.rating), "
                         "watched=MAX(lb.watched, excluded.watched), watchlist=excluded.watchlist, liked=excluded.liked, t=excluded.t",
                         (pid, n, y, r["title"], r["rating"], r["watched"], r["watchlist"], r["tmdb"], now, r["liked"]))
        conn.commit()
    finally:
        conn.close()
    LB["t"], LB["n"], LB["error"] = int(time.time()), len(rows), None
    home_invalidate()
    return len(rows)


def lb_rss_refresh(force=False):
    """Public Letterboxd feed: recent diary entries with rating and TMDb id."""
    user = key("LETTERBOXD_USER")
    if not user or (not force and time.time() - LB["rss_t"] < 6 * 3600):
        return
    url = "%s/%s/rss/" % (LB_BASE, urllib.parse.quote(user.strip("/ ")))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Beeldbuis v%d" % VERSION})
        with urllib.request.urlopen(req, timeout=25, context=API_SSL) as r:
            xml = r.read()
        root = ET.fromstring(xml)
    except Exception as e:  # noqa
        LB["error"] = "Letterboxd feed: %s" % e
        LB["rss_t"] = time.time()
        return
    ns = {"letterboxd": "https://letterboxd.com", "tmdb": "https://themoviedb.org"}
    conn = meta_db()
    try:
        now = int(time.time())
        n = 0
        for item in root.iter("item"):
            title = (item.findtext("letterboxd:filmTitle", default="", namespaces=ns) or "").strip()
            year = item.findtext("letterboxd:filmYear", default="", namespaces=ns) or ""
            rating = item.findtext("letterboxd:memberRating", default="", namespaces=ns) or ""
            tmdb_id = item.findtext("tmdb:movieId", default="", namespaces=ns) or "0"
            if not title:
                continue
            try:
                rv = float(rating) if rating else None
            except ValueError:
                rv = None
            conn.execute("INSERT INTO lb (profile, norm, year, title, rating, watched, watchlist, tmdb, t) VALUES ('p1',?,?,?,?,1,0,?,?) "
                         "ON CONFLICT(profile, norm, year) DO UPDATE SET rating=COALESCE(excluded.rating, lb.rating), watched=1, "
                         "tmdb=CASE WHEN excluded.tmdb>0 THEN excluded.tmdb ELSE lb.tmdb END, t=excluded.t",
                         (norm_title(title), int(year) if year.isdigit() else 0, title, rv, int(tmdb_id) if tmdb_id.isdigit() else 0, now))
            n += 1
        conn.commit()
    finally:
        conn.close()
    LB["rss_t"], LB["error"] = time.time(), None
    if n:
        home_invalidate()


def lb_data(pid):
    conn = meta_db()
    try:
        rows = conn.execute("SELECT norm, year, title, rating, watched, watchlist, tmdb, liked FROM lb WHERE profile=?", (pid,)).fetchall()
    finally:
        conn.close()
    return [{"norm": r[0], "year": r[1], "title": r[2], "rating": r[3], "watched": r[4], "watchlist": r[5], "tmdb": r[6], "liked": r[7] or 0} for r in rows]


def lb_status():
    conn = meta_db()
    try:
        n = conn.execute("SELECT COUNT(*), SUM(watchlist), SUM(CASE WHEN rating>=4.5 OR liked=1 THEN 1 ELSE 0 END) FROM lb WHERE profile='p1'").fetchone()
    finally:
        conn.close()
    return {"n": n[0] or 0, "watchlist": n[1] or 0, "loved": n[2] or 0, "zip": os.path.exists(LB_ZIP), "user": key("LETTERBOXD_USER"),
            "rss_t": LB["rss_t"], "error": LB["error"]}


# ----------------------------------------------------------- watch history ----
def history_note(pid, key_, item, removed=False):
    """Learn from what is watched: completed titles count as liked, titles dropped early count against. Local only."""
    gk = (item or {}).get("gkey")
    if not gk:
        return
    secs = float((item or {}).get("t") or 0)
    dur = float((item or {}).get("dur") or 0)
    pct = secs / dur if dur else 0
    conn = meta_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS history (profile TEXT, k TEXT, kind TEXT, title TEXT, secs REAL, dur REAL, pct REAL, "
                     "sessions INTEGER, done INTEGER, dropped INTEGER, last INTEGER, PRIMARY KEY(profile, k))")
        row = conn.execute("SELECT secs, sessions, pct FROM history WHERE profile=? AND k=?", (profile_id(pid), gk)).fetchone()
        done = 1 if pct >= 0.9 else 0
        if removed and not done:
            # taken off "continue watching" early: counts as dropped when little was watched
            dropped = 1 if (pct < 0.35 and secs > 120) else 0
        else:
            dropped = 0
        sessions = (row[1] if row else 0) + (0 if (row and secs - row[0] < 600 and not removed) else 1)
        conn.execute("INSERT INTO history (profile, k, kind, title, secs, dur, pct, sessions, done, dropped, last) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                     "ON CONFLICT(profile, k) DO UPDATE SET secs=MAX(history.secs, excluded.secs), dur=excluded.dur, pct=MAX(history.pct, excluded.pct), "
                     "sessions=excluded.sessions, done=MAX(history.done, excluded.done), dropped=CASE WHEN history.done=1 THEN 0 ELSE excluded.dropped END, last=excluded.last",
                     (profile_id(pid), gk, (item or {}).get("kind") or "", (item or {}).get("name") or "", secs, dur, pct, sessions, done, dropped, int(time.time())))
        conn.commit()
    finally:
        conn.close()
    if done or dropped:
        home_invalidate()


def history_profile(pid):
    """What the viewing history says: genres of finished titles (liked), genres of dropped titles (less), keys to skip."""
    conn = meta_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS history (profile TEXT, k TEXT, kind TEXT, title TEXT, secs REAL, dur REAL, pct REAL, "
                     "sessions INTEGER, done INTEGER, dropped INTEGER, last INTEGER, PRIMARY KEY(profile, k))")
        rows = conn.execute("SELECT k, done, dropped, secs, pct FROM history WHERE profile=?", (pid,)).fetchall()
    finally:
        conn.close()
    out = {"liked": {}, "disliked": {}, "done": set(), "dropped": set(), "n": len(rows), "hours": round(sum(r[3] for r in rows) / 3600, 1)}
    metas = meta_cached_many([r[0] for r in rows])
    for k, done, dropped, secs, pct in rows:
        gens = (metas.get(k) or {}).get("genres") or []
        if done:
            out["done"].add(k)
            for g in gens:
                out["liked"][g] = out["liked"].get(g, 0) + 1
        elif dropped:
            out["dropped"].add(k)
            for g in gens:
                out["disliked"][g] = out["disliked"].get(g, 0) + 1
        elif secs > 1800:
            for g in gens:
                out["liked"][g] = out["liked"].get(g, 0) + 0.5
    return out


# ------------------------------------------------------------ home screen ----
def home_invalidate():
    with HOME_LOCK:
        HOME.clear()


def _excluded(m, g, p, seen, fb):
    """Hard exclusions: 'not for me', seen, adult, excluded words (e.g. Marvel), avoided genres."""
    if g["key"] in fb and fb[g["key"]] in ("down", "seen"):
        return True
    if g["key"] in seen:
        return True
    text = " ".join([g["title"], (m or {}).get("overview", ""), " ".join((m or {}).get("companies") or [])]).lower()
    for w in p.get("exclude") or []:
        if w.lower() in text:
            return True
    if m and set(m.get("genres") or []) & set(p.get("avoid_genres") or []):
        return True
    return False


def _quality_ok(m, p, relaxed=False):
    if not m or (m.get("vote", 0) == 0 and m.get("imdb_rating") is None):
        return relaxed
    imdb = m.get("imdb_rating")
    rt = m.get("rt")
    thr = p.get("min_imdb", 7.0) - (0.7 if relaxed else 0)
    if imdb is not None and imdb >= thr:
        return True
    if rt is not None and rt >= p.get("min_rt", 80) - (10 if relaxed else 0):
        return True
    if imdb is None and rt is None and m.get("vote", 0) >= thr and m.get("votes", 0) >= (30 if relaxed else 100):
        return True
    return False


def _anchor_ids(p):
    """TMDb ids of the profile's anchor titles (and Letterboxd favourites), cached."""
    ids = []
    anchors = list(p.get("anchors") or [])
    if p["id"] in ("p1", "both"):
        # Letterboxd: 4.5+ stars and liked films; rotate through a long list so every day leans on different favourites
        loved = sorted([r for r in lb_data("p1") if (r["rating"] or 0) >= 4.5 or r.get("liked")], key=lambda r: (-(r["rating"] or 0), r["title"]))
        if len(loved) > 14:
            day = int(time.time() // 86400)
            loved = [loved[(day * 7 + i) % len(loved)] for i in range(14)]
        anchors += [{"title": r["title"], "year": r["year"], "kind": "movie", "tmdb": r["tmdb"], "lb": True} for r in loved]
    for a in anchors[:30]:
        kind = a.get("kind") or "series"
        if a.get("tmdb"):
            ids.append((kind, int(a["tmdb"])))
            continue
        r = tmdb_search(kind, a.get("title", ""), a.get("year") or 0)
        if not r and a.get("lb"):   # Letterboxd lists series (Chernobyl, Shogun) as films
            kind = "series"
            r = tmdb_search(kind, a.get("title", ""), a.get("year") or 0)
        if isinstance(r, dict) and r.get("id"):   # "ERR" = TMDb unreachable: skip, don't break Tonight
            ids.append((kind, int(r["id"])))
    return ids


def _candidates_for_you(p):
    """Titles TMDb recommends next to the anchors, plus well-rated titles in the profile's genres; only what the provider has."""
    out = {}
    def consider(kind, r, reason, pts):
        m = meta_pack(kind, r)
        g = vod_find(kind, m["title"], m["year"], m["orig"])
        if not g:
            return
        c = out.setdefault(g["key"], {"g": g, "m": m, "pts": 0, "reasons": []})
        c["pts"] += pts
        if reason not in c["reasons"]:
            c["reasons"].append(reason)
        old = meta_cached(g["key"])
        if not old or not old.get("id"):
            meta_store(g["key"], kind, dict(m, detail=False))
    for kind, tid in _anchor_ids(p):
        path = "/%s/%d" % ("movie" if kind == "movie" else "tv", tid)
        for sub, pts in (("/recommendations", 40), ("/similar", 20)):
            data = tmdb(path + sub, 7 * 86400) or {}
            for r in (data.get("results") or [])[:20]:
                consider(kind, r, "Because you like " + (anchor_title(kind, tid) or "your favourites"), pts)
    gids = [i for i, n in GENRES.items() if n in (p.get("genres") or []) or any(a in (p.get("genres") or []) for a in GENRE_ALIAS.get(n, ()))]
    for kind, path in (("movie", "/discover/movie"), ("series", "/discover/tv")):
        for gid in gids[:8]:
            data = tmdb(path, 7 * 86400, with_genres=str(gid), sort_by="vote_average.desc", **{"vote_count.gte": "800" if kind == "movie" else "300"}, page=1) or {}
            for r in (data.get("results") or [])[:20]:
                consider(kind, r, "Top-rated " + GENRES.get(gid, ""), 15)
            data = tmdb(path, 7 * 86400, with_genres=str(gid), sort_by="popularity.desc", **{"vote_count.gte": "300" if kind == "movie" else "100"}, page=1) or {}
            for r in (data.get("results") or [])[:20]:
                consider(kind, r, "Popular " + GENRES.get(gid, ""), 10)
    return out


_ANCHOR_TITLES = {}


def anchor_title(kind, tid):
    k = (kind, tid)
    if k not in _ANCHOR_TITLES:
        d = tmdb("/%s/%d" % ("movie" if kind == "movie" else "tv", tid), 30 * 86400) or {}
        _ANCHOR_TITLES[k] = d.get("title") or d.get("name") or ""
    return _ANCHOR_TITLES[k]


def hot_score(c, p, now):
    """0-100: how much is this title talked about right now (signals from Beeldbuis-smaak-en-content.md)."""
    s = 0
    tr = c.get("trend")
    if tr:
        s += 30 if tr["rank"] <= 20 else 15
        if "day" in tr["periods"] and "week" in tr["periods"]:
            s += 5
    m = c.get("m") or {}
    imdb, rt = m.get("imdb_rating"), m.get("rt")
    if (imdb and imdb >= 8) or (rt and rt >= 90):
        s += 20
    elif (imdb and imdb >= 7.3) or (not imdb and m.get("vote", 0) >= 7.5 and m.get("votes", 0) >= 200):
        s += 10
    if m.get("pop", 0) >= 200:
        s += 10
    g = c["g"]
    if g["added"] and now - g["added"] < 14 * 86400:
        s += 15
    if len({v["lang"] for v in g["variants"]}) >= 2:
        s += 10
    return min(100, s)


def home_build(pid):
    p = profile(pid)
    now = time.time()
    fb = feedback_all(pid)
    # seen: finished positions, Letterboxd watched, explicit "seen"
    seen = set()
    with LOCK:
        for k, pos in STATE["positions"].items():
            if pos and pos.get("gkey") and pos.get("dur") and pos.get("t", 0) > 0.96 * pos["dur"]:
                seen.add(pos["gkey"])
    hist = history_profile(pid if not p.get("together") else "p1")
    if p.get("together"):
        h2 = history_profile("p2")
        for k in ("liked", "disliked"):
            for g_, n_ in h2[k].items():
                hist[k][g_] = hist[k].get(g_, 0) + n_
        hist["dropped"] |= h2["dropped"]
    seen |= hist["done"]
    lb_rows = lb_data("p1") if pid in ("p1", "both") else []
    for r in lb_rows:
        if r["watched"]:
            g = vod_find("movie", r["title"], r["year"])
            if g:
                seen.add(g["key"])
    # Together: union of both exclusion lists, and nothing one of them is watching alone
    if p.get("together"):
        ps = profiles()
        for other in ("p1", "p2"):
            o = ps.get(other, {})
            p["exclude"] = list(set(p.get("exclude") or []) | set(o.get("exclude") or []))
            p["avoid_genres"] = list(set(p.get("avoid_genres") or []) | set(o.get("avoid_genres") or []))
            for k, v in feedback_all(other).items():
                if v == "down":
                    fb.setdefault(k, "down")
        with LOCK:
            for k, pos in STATE["positions"].items():
                if pos and pos.get("gkey") and pos.get("kind") == "series" and profile_id(pos.get("profile")) in ("p1", "p2"):
                    seen.add(pos["gkey"])
    rows = []
    pending = 0

    # --- Live now: added per request by home_with_live(), always fresh

    # --- Continue watching (shared across profiles, series someone else is watching alone stay out of Together)
    cw = []
    with LOCK:
        pos_items = sorted([(k, v) for k, v in STATE["positions"].items() if v], key=lambda kv: -(kv[1].get("updated") or 0))
    for k, v in pos_items[:12]:
        owner = profile_id(v.get("profile")) if v.get("profile") else None
        if p.get("together") and owner in ("p1", "p2"):
            continue
        if not p.get("together") and owner and owner not in (pid, "both"):
            continue       # what the other profile is watching alone stays out of this profile's row
        cw.append({"key": k, "kind": v.get("kind"), "title": v.get("name"), "icon": v.get("icon"), "t": v.get("t"), "dur": v.get("dur"),
                   "pos": v, "gkey": v.get("gkey"), "reason": "%d%% watched" % (round(100 * v["t"] / v["dur"]) if v.get("dur") else 0)})
        try:      # a landscape picture for the card: the film's or series' backdrop
            m_ = meta_cached(v["gkey"]) if v.get("gkey") and vod_group(v["gkey"]) else None
            if m_:
                cw[-1]["meta"] = {"backdrop": m_.get("backdrop"), "poster": m_.get("poster"), "title": m_.get("title")}
        except Exception:  # noqa
            pass
    if cw:
        rows.append({"id": "continue", "title": "Continue watching", "kind": "continue", "tiles": cw})

    # --- Watchlist: only what you ticked yourself (nothing is added for you, Letterboxd's watchlist neither)
    wl = []
    names = {k: v.get("name") for k, v in profiles().items() if not k.startswith("_")}
    for k, who, _t in watchlist_rows(pid):
        g = vod_group(k)
        if g and vod_visible(g, allow_dead=True):
            wl.append(vod_tile(g, "Added by " + (names.get(who) or who) if pid == "both" and who != "both" else "On your watchlist"))
    for k, v in fb.items():
        if v == "together" and k not in {t["key"] for t in wl}:
            g = vod_group(k)
            if g and vod_visible(g, allow_dead=True):
                wl.append(vod_tile(g, "Watch together"))
    for t in wl:
        t["wl"] = True
    rows.append({"id": "watchlist", "title": "Your watchlist", "kind": "tiles", "tiles": wl[:60],
                 "empty": "Nothing here yet. Tick ＋ on any film or series to add it."})

    has_taste = bool(p.get("anchors") or p.get("genres") or p.get("makers") or any(r for r in lb_rows if (r["rating"] or 0) >= 4.5) or len(hist["done"]) >= 3)
    if len(hist["done"]) >= 3 and not p.get("anchors"):
        # no written taste yet: the finished titles become the anchors
        p = dict(p, anchors=[{"title": (meta_cached(k) or {}).get("title") or vod_group(k)["title"], "year": vod_group(k)["year"], "kind": vod_group(k)["kind"], "tmdb": (meta_cached(k) or {}).get("id")} for k in list(hist["done"])[:10] if vod_group(k)])
    cands = _candidates_for_you(p) if has_taste else {}
    metas = meta_cached_many(list(cands.keys()) + [x["key"] for x in TREND["items"]])
    for c in cands.values():
        c["m"] = metas.get(c["g"]["key"]) or c["m"]

    # --- For you
    fy = []
    if has_taste:
        liked_genres = set()
        for k, v in fb.items():
            if v == "up":
                mm = metas.get(k) or meta_cached(k)
                liked_genres |= set((mm or {}).get("genres") or [])
        pg = set(p.get("genres") or []) | liked_genres | {g_ for g_, n_ in hist["liked"].items() if n_ >= 2}
        scored = []
        for c in cands.values():
            g, m = c["g"], c["m"]
            if g["key"] in hist["dropped"] or _excluded(m, g, p, seen, fb) or not _quality_ok(m, p, relaxed=not m.get("detail")):
                continue
            pts = c["pts"] + 8 * len(set(m.get("genres") or []) & pg) + (m.get("vote", 0) - 6) * 5
            for g_ in m.get("genres") or []:
                pts += 3 * min(4, hist["liked"].get(g_, 0)) - 4 * min(3, hist["disliked"].get(g_, 0))
            if g["key"] in fb and fb[g["key"]] == "up":
                pts += 25
            if m.get("next_ep"):
                pts += 20
                c["reasons"].insert(0, "New episodes")
            scored.append((pts, c))
        scored.sort(key=lambda x: -x[0])
        run, last = 0, None
        for pts, c in scored:
            gen = (c["m"].get("genres") or [""])[0]
            if gen == last:
                run += 1
                if run >= 3:
                    continue
            else:
                run, last = 0, gen
            fy.append(vod_tile(c["g"], c["reasons"][0], c["m"]))
            if len(fy) >= 24:
                break
        if any(not (metas.get(t["key"]) or {}).get("detail") for t in fy):
            pending += 1
            meta_enqueue([t["key"] for t in fy if not (metas.get(t["key"]) or {}).get("detail")], front=True)
    # taste routes (makers, their circle, labels, new seasons): ~60% / 25% of For you, TMDb picks fill the rest
    tmix = []
    try:
        tmix = [t for t in taste_mix(pid, 24) if not _excluded(metas.get(t["key"]) or t.get("meta"), vod_group(t["key"]) or {"key": t["key"], "title": t["title"]}, p, seen, fb)]
    except Exception as e:  # noqa
        print("taste mix failed: %s" % e)
    if tmix:
        core = [t for t in tmix if t.get("route") in ("makers", "seasons")]
        near = [t for t in tmix if t.get("route") not in ("makers", "seasons")]
        rest = [t for t in fy if t["key"] not in {x["key"] for x in tmix}]
        mixed, pattern = [], ["c", "c", "n", "t", "c", "c", "n", "t"]
        i = 0
        while len(mixed) < 24 and (core or near or rest):
            src = {"c": core, "n": near, "t": rest}[pattern[i % len(pattern)]] or core or near or rest
            mixed.append(src.pop(0))
            i += 1
        fy = mixed
    if fy:
        rows.append({"id": "foryou", "title": "For you", "kind": "tiles", "tiles": fy})
    placed = {t["key"] for t in fy}      # a title appears in one row only (For you > Hot > New)
    td = taste_get(pid) if has_taste or tmix else None
    if td and td.get("routes"):
        for rid, title, routes in (("makers", "From your directors", ("makers", "seasons")), ("circle", "From the people around them", ("circle", "labels"))):
            ts = sorted([t for r_ in routes for t in td["routes"].get(r_) or []], key=lambda t: -t["score"])
            tiles, per = [], {}
            mm = meta_cached_many([t["key"] for t in ts[:80]])
            for t in ts:
                g = vod_group(t["key"])
                by = t["reason"].split(",")[0].split(" · ")[0]
                if not g or t["key"] in placed or per.get(by, 0) >= 4 or _excluded(mm.get(t["key"]), g, p, seen, fb):
                    continue
                per[by] = per.get(by, 0) + 1
                tiles.append(vod_tile(g, t["reason"], mm.get(t["key"]) or {}))
                if len(tiles) >= 20:
                    break
            if len(tiles) >= 4:
                rows.append({"id": rid, "title": title, "kind": "tiles", "tiles": tiles})
                placed |= {t["key"] for t in tiles}

    # --- Hot right now + Wildcard
    hot, wild = [], []
    tired = shown_tired(pid)
    pg = set(p.get("genres") or [])
    for x in TREND["items"]:
        g = vod_group(x["key"])
        if not g or not vod_visible(g) or g["key"] in placed:
            continue
        m = metas.get(g["key"]) or x["meta"]
        c = {"g": g, "m": m, "trend": x}
        if _excluded(m, g, p, seen, fb):
            continue
        hs = hot_score(c, p, now)
        if hs < 60:
            continue
        reason = ("Trending worldwide" if x["rank"] <= 20 else "Trending this week") + (" · IMDb %.1f" % m["imdb_rating"] if m.get("imdb_rating") else (" · TMDb %.1f" % m["vote"] if m.get("vote") else ""))
        tile = vod_tile(g, reason, m)
        tile["hot"] = hs
        in_taste = bool(set(m.get("genres") or []) & pg) if pg else True
        if hs >= 80 and not in_taste and g["key"] not in tired and _quality_ok(m, p, relaxed=True):
            wild.append(tile)
        else:
            hot.append(tile)
    hot.sort(key=lambda t: -t["hot"])
    if hot:
        rows.append({"id": "hot", "title": "Hot right now", "kind": "tiles", "tiles": hot[:20]})
    placed |= {t["key"] for t in hot[:20]} | {t["key"] for t in wild[:2]}
    if wild and p.get("surprise", 1) > 0:
        for t in wild[:2]:
            t["reason"] = "Everyone's talking about this · " + t["reason"]
        rows.append({"id": "wild", "title": "Wildcard", "kind": "tiles", "tiles": wild[:2]})
        shown_bump(pid, [t["key"] for t in wild[:2]])

    # --- New this week (new at the provider, filtered on quality where known)
    new = []
    with VOD_LOCK:
        recent = [g for g in VOD["groups"].values() if g["added"] and now - g["added"] < 14 * 86400]
    recent = [g for g in recent if vod_visible(g) and not (g["key"] in fb and fb[g["key"]] in ("down", "seen")) and g["key"] not in seen and g["key"] not in placed]
    recent.sort(key=lambda g: -g["added"])
    recent = recent[:150]
    rmeta = meta_cached_many([g["key"] for g in recent])
    unknown = []
    for g in recent:
        m = rmeta.get(g["key"])
        if _excluded(m, g, p, seen, fb):
            continue
        if m is None:
            unknown.append(g["key"])
            if g["rating"] >= 6.5 and len(new) < 30:
                new.append(vod_tile(g, "New this week", None))
            continue
        if m.get("miss"):
            if g["rating"] >= 6.8:
                new.append(vod_tile(g, "New this week", None))
            continue
        if _quality_ok(m, p, relaxed=not m.get("detail")):
            new.append(vod_tile(g, "New this week" + (" · IMDb %.1f" % m["imdb_rating"] if m.get("imdb_rating") else (" · TMDb %.1f" % m["vote"] if m.get("vote") else "")), m))
    if unknown:
        pending += 1
        meta_enqueue(unknown[:60])
    if new:
        rows.append({"id": "new", "title": "New this week", "kind": "tiles", "tiles": new[:24]})

    # --- Recordings coming up
    with REC_LOCK:
        recs = [r for r in RECS["items"] if r["status"] in ("scheduled", "recording")][:8]
    if recs:
        rows.append({"id": "recs", "title": "Recording", "kind": "recordings", "tiles": [{"id": r["id"], "title": r["title"], "channel": r["channel"], "start": r["start"], "stop": r["stop"], "status": r["status"], "icon": r.get("icon", "")} for r in recs]})

    return {"t": int(time.time()), "profile": p["id"], "name": p.get("name"), "rows": rows, "pending": pending, "history": {"n": hist["n"], "hours": hist["hours"], "done": len(hist["done"])},
            "has_taste": has_taste, "keys": keys_status(), "trend_t": TREND["t"], "vod_t": VOD["t"], "vod_n": VOD["n"]}


def home_get(pid, force=False):
    with HOME_LOCK:
        h = HOME.get(pid)
    if h and not force and time.time() - h["t"] < HOME_TTL and not (h.get("pending") and time.time() - h["t"] > 25):
        return h
    h = home_build(pid)
    with HOME_LOCK:
        HOME[pid] = h
    return h


# ------------------------------------------------------- sport and events ----
_SPORT_CAT_RE = re.compile(r"SPORT|PPV|EVENT|NFL|UFC|FIGHT|BOX|F1|FORMULA|DARTS|GOLF|TENNIS|RACING|MOTOR|VOETBAL|FOOTBALL|ESPN|ZIGGO|DAZN|EUROSPORT|BT SPORT|TNT SPORT|VIAPLAY|NBA|NHL|MLB|CRICKET|RUGBY|CYCLING|WIELREN", re.I)
_FINAL_RE = re.compile(r"\b(final|finale|grand final|super bowl|championship|decider|game 7|title fight|world cup final)\b", re.I)
_SEMI_RE = re.compile(r"\b(semi[- ]?final|halve finale|play[- ]?offs?|main event|quarter[- ]?final|championship round)\b", re.I)
_RECORD_RE = re.compile(r"\b(record|history|debut|farewell|last race|retirement)\b", re.I)
_PROMO_RE = re.compile(r"\b(ontdek|discover|preview|review|highlights|samenvatting|magazine|watch-?along|countdown|studio|talk|show|podcast|replay|herhaling|journaal|news|classic|best of|documentary|documentaire)\b", re.I)
_FOREIGN_RE = re.compile(r"\b(arabic|arab|french|fran[cç]ais|german|deutsch|spanish|espa[nñ]ol|italian|italiano|portuguese|portugu[eê]s|turkish|t[uü]rk[cç]e|polish|polski|russian|hindi|urdu|farsi|persian)\b", re.I)
SPORT_PREFERRED_COUNTRIES = {"NL", "UK", "US", "USA", "EN", "BE", "CA", "AU", "IE"}


def channel_bias(g, text):
    """Penalty for feeds the viewer would not pick: a non-preferred country and a non-English commentary tag."""
    s = 0
    if (g.get("country") or "OTHER") not in SPORT_PREFERRED_COUNTRIES:
        s -= 20
    if _FOREIGN_RE.search(text):
        s -= 15
    return s
DUTCH_NAMES = [   # well-known Dutch athletes and teams as a default; smaak.json "lists" can add more
    "verstappen", "van gerwen", "van veen", "noppert", "verhoeven", "van der poel", "vollering", "kooij", "pogacar", "oranje", "netherlands", "nederland",
               "ajax", "psv", "feyenoord", "van de zandschulp", "griekspoor", "schouten", "nuis", "de jong", "gakpo", "depay", "hato", "bol", "hassan"]
DERBIES = ["ajax - feyenoord", "feyenoord - ajax", "klassieker", "el clasico", "real madrid - barcelona", "barcelona - real madrid", "manchester derby",
           "manchester united - manchester city", "manchester city - manchester united", "north london derby", "arsenal - tottenham", "tottenham - arsenal",
           "merseyside derby", "liverpool - everton", "milan - inter", "inter - milan", "der klassiker", "bayern - dortmund", "dortmund - bayern",
           "celtic - rangers", "rangers - celtic", "boca - river", "river - boca"]
TOURNAMENTS = ["world cup", "wk ", "ek ", "euro 20", "olympic", "olympische", "champions league", "wimbledon", "roland garros", "us open", "australian open",
               "tour de france", "giro", "vuelta", "ryder cup", "six nations", "world championship", "wk darts", "pdc world", "super bowl", "playoffs",
               "masters", "grand prix", "grand slam", "final four", "stanley cup", "nba finals", "world series"]


_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTHS.update({"mrt": 3, "mei": 5, "okt": 10})


def event_name_stale(name, now):
    """True when an event channel's name carries a date that is clearly in the past (providers leave old PPV names standing)."""
    low = name.lower()
    today = time.localtime(now)
    m = re.search(r"\b(20\d\d)-(\d\d)-(\d\d)\b", low) or re.search(r"\b(\d\d)[-/.](\d\d)[-/.](20\d\d)\b", low)
    if m:
        try:
            y, mo, d = (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m.group(1).startswith("20") else (int(m.group(3)), int(m.group(2)), int(m.group(1)))
            return time.mktime((y, mo, d, 23, 59, 0, 0, 0, -1)) < now - 86400
        except (ValueError, OverflowError):
            return False
    m = re.search(r"\b(\d{1,2})\s+(jan|feb|mar|mrt|apr|may|mei|jun|jul|aug|sep|oct|okt|nov|dec)[a-z]*\b", low)
    if m:
        try:
            t = time.mktime((today.tm_year, _MONTHS[m.group(2)], int(m.group(1)), 23, 59, 0, 0, 0, -1))
        except (ValueError, OverflowError):
            return False
        return now - 86400 > t > now - 200 * 86400     # older than a day but not "next year"
    return False


def team_hit(text, teams):
    for t in teams:
        if re.search(r"(?<![a-z])" + re.escape(t) + r"(?![a-z])", text):
            if t == "ajax" and re.search(r"ajax\s+(k[oø]benhavn|copenhagen|cape town)", text):
                continue
            return True
    return False


def sport_lists(p):
    """Built-in sport vocabulary extended with the personal lists from smaak.json (names, derbies, tournaments)."""
    lists = p.get("_lists") or {}
    def merged(base, key):
        extra = [str(x).lower() for x in (lists.get(key) or []) if x]
        return base + [x for x in extra if x not in base]
    return merged(DUTCH_NAMES, "names"), merged(DERBIES, "derbies"), merged(TOURNAMENTS, "tournaments")


def sport_events(p, now):
    """Programmes live now or starting soon on sport/event channels, scored for the Live & upcoming row (spoiler-free wording)."""
    names, derbies, tournaments = sport_lists(p)
    with CATALOG_LOCK:
        cats = {c["id"]: c for c in CATALOG["cats"]}
        groups = [g for g in CATALOG["groups"] if any(_SPORT_CAT_RE.search(cats.get(c, {}).get("name", "")) for c in g["cats"])]
    hidden = set(str(c) for c in STATE["settings"].get("hidden_cats") or [])
    groups = [g for g in groups if any(c not in hidden for c in g["cats"])]
    if not groups:
        return []
    teams = [t.lower() for t in (p.get("teams") or [])]
    sports = [s.lower() for s in (p.get("sports") or [])]
    big_only = p.get("together") or p["id"] == "p2"
    tiles = []
    # a) guide programmes on sport channels, from 3 h ago to 12 h ahead
    ids = list({g["epg"] for g in groups if g.get("epg")})
    by_epg = {}
    for g in groups:
        if g.get("epg"):
            by_epg.setdefault(g["epg"], []).append(g)
    progs = epg_rows(ids, int(now - 3 * 3600), int(now + 12 * 3600)) if ids else {}
    for epg_id, plist in progs.items():
        for pr in plist:
            if pr["stop"] < now - 600 or pr["start"] > now + 12 * 3600:
                continue
            title = pr["title"] or ""
            text = (title + " " + (pr.get("description") or "")).lower()
            s = 0
            if _FINAL_RE.search(text):
                s += 40
            elif _SEMI_RE.search(text):
                s += 25
            if any(n in text for n in names):
                s += 30
            if team_hit(text, teams):
                s += 35
            if any(t in text for t in tournaments):
                s += 20
            if any(d in text for d in derbies):
                s += 20
            if _RECORD_RE.search(text) and pr["start"] > now:
                s += 15
            if pr["start"] <= now <= pr["stop"]:
                s += 10
            if any(sp in text for sp in sports):
                s += 15
            if re.search(r"\b(live)\b", title.lower()):
                s += 5
            if _PROMO_RE.search(text):
                s -= 40           # a promo, preview or talk programme, not the match itself
            g = by_epg[epg_id][0]
            s += channel_bias(g, text + " " + g["name"].lower())
            if s < (45 if big_only else 25):
                continue
            key_t = norm_title(title)
            night = 1 <= time.localtime(pr["start"]).tm_hour < 7
            tiles.append({"kind": "event", "title": title, "channel": g["name"], "gkey": g["key"], "icon": g["icon"], "start": pr["start"], "stop": pr["stop"], "_k": key_t,
                          "score": s, "live": pr["start"] <= now <= pr["stop"], "suggest": "record" if night and pr["start"] > now else ("watch" if pr["start"] <= now else "remind"),
                          "catchup": pr["stop"] < now and g.get("archive", 0) > 0, "reason": "Live now" if pr["start"] <= now <= pr["stop"] else ("Starts " + time.strftime("%H:%M", time.localtime(pr["start"])) if pr["start"] > now else "Just finished · catch-up")})
    # b) event channels that carry the fixture in their name ("NEXT | UFC ... | Sun 08 Nov 00:00 CET")
    for g in groups:
        name = g["name"]
        if not re.search(r"\b(NEXT|LIVE|TODAY|PPV|EVENT)\b", name.upper()) or "|" not in name:
            continue
        if event_name_stale(name, now):
            continue
        text = name.lower()
        s = 15
        if _FINAL_RE.search(text):
            s += 40
        elif _SEMI_RE.search(text):
            s += 25
        if any(n in text for n in names):
            s += 30
        if team_hit(text, teams):
            s += 35
        if any(sp in text for sp in sports):
            s += 15
        if any(t in text for t in tournaments):
            s += 20
        if _PROMO_RE.search(text):
            s -= 40
        s += channel_bias(g, text)
        if s < (50 if big_only else 30):
            continue
        parts = [x.strip() for x in name.split("|") if x.strip()]
        title = " · ".join(parts[1:3]) if len(parts) > 2 else name
        tiles.append({"kind": "event", "title": title, "channel": parts[0] if parts else name, "gkey": g["key"], "icon": g["icon"], "start": 0, "stop": 0, "_k": norm_title(title),
                      "score": s, "live": "LIVE" in name.upper(), "suggest": "watch", "reason": parts[-1] if len(parts) > 3 else "Event channel"})
    # the same event on several feeds: keep the best-scored one (preferred country, English commentary)
    best = {}
    for t in tiles:
        k = t.pop("_k")
        if k not in best or t["score"] > best[k]["score"]:
            best[k] = t
    tiles = list(best.values())
    tiles.sort(key=lambda t: (-t["score"], t["start"] or 0))
    return tiles


def is_sport_text(text):
    return bool(re.search(r"\b(nfl|ufc|football|voetbal|soccer|darts|formula|grand prix|f1|tennis|golf|boxing|cycling|wielrennen|eredivisie|premier league|champions league|match|wedstrijd|race|fight|basketball|nba|hockey|cricket|rugby|olympi|tour de france|mma|glory|ppv)\b", (text or "").lower()))


# ------------------------------------------------- live sport, right now ----
# "Live now" on Tonight and the Sport overview show only sport that is being played at this moment:
# no replays, classics, magazines, previews, studio shows or things starting later.
_REPLAY_RE = re.compile(
    r"\b(gameday|classics?|stories|story of|rewind|flashback|archive|archief|repeat|re-?run|replay\w*|playback|herhaling|best|dossier|huddle|after|avant-match|apr[eè]s-match|encore|retro|legends?|throwback|greatest|"
    r"top \d+|best of|best goals|mooiste|in \d+|catch-?up|condensed|highlights?|samenvatting|terugblik|review|recap|preview|voorbeschouwing|"
    r"nabeschouwing|magazine|documentar\w*|studio|talk|show|podcast|news|nieuws|journaal|countdown|build-?up|pre-?game|post-?game|pre-?match|post-?match|"
    r"pre-?race|post-?race|analysis|analyse|weekly|this week|inside|behind the|access|vintage|memorable|season review|full match|mini match|extended|"
    r"delayed|uitgesteld|from earlier|earlier today|eerder vandaag|re-?air|re-?live|uitzending gemist|teleshop\w*|off air|close ?down|"
    r"to be announced|tba|no information|geen informatie|programma(?:informatie)?|coming up|next on|up next|trailer|promo)\b", re.I)
_DESC_REPLAY_RE = re.compile(r"\b(replay|herhaling|repeat|re-?run|from earlier|earlier today|eerder vandaag|uitgesteld|delayed|classic|highlights|samenvatting|"
                             r"terugblik|archive|looks back|kijkt terug|recap)\b", re.I)
_LIVE_WORD_RE = re.compile(r"(^|\W)(live|direct|en vivo|ao vivo)(\W|$)", re.I)
SPORT_KINDS = [  # id, label, emoji, regex (first hit wins)
    ("nfl", "American football", "🏈", r"\b(nfl|ncaaf|college football|super bowl|redzone|red zone|sunday night football|monday night football|thursday night football)\b"),
]
_NFL_TEAMS_RE = re.compile(r"\b(bengals|falcons|lions|dolphins|chiefs|bills|eagles|cowboys|packers|49ers|ravens|steelers|patriots|jets|giants|bears|vikings|"
                                     r"saints|buccaneers|panthers|rams|seahawks|cardinals|chargers|raiders|broncos|texans|colts|jaguars|titans|browns|commanders)\b", re.I)
SPORT_KINDS += [
    ("other", "More live sport", "🏟️", r"\b(curling|handball|handbal|volleyball|volleybal|horse racing|paardenrace|racing!|greyhound|lacrosse|padel|squash|badminton|table tennis|esports?)\b"),
    ("snooker", "Snooker", "🎱", r"\b(snooker|pool|9-ball)\b"),
    ("basketball", "Basketball", "🏀", r"\b(nba|wnba|euroleague|eurocup|basketball|basketbal|basket-ball|basket|ncaab|ko[sš]arka|koszyk[oó]wka|μπάσκετ|liga acb|aba liga|betclic elite)\b"),
    ("f1", "Motorsport", "🏎️", r"\b(f1|formula ?[12e]|formule ?1|grand prix|gp|motogp|moto ?[23]|nascar|indycar|wec|le mans|rally|wrc|dtm|superbike|wsbk|"
                                 r"qualifying|kwalificatie|sprint race|practice|vrije training)\b"),
    ("fight", "Fight night", "🥊", r"\b(ufc|mma|boxing|boksen|bellator|pfl|one championship|one fc|glory|kickboxing|k-1|fight ?night|cage|wrestling|wwe|aew)\b"),
    ("darts", "Darts", "🎯", r"\b(darts|pdc|bdo|wdf|premier league darts|grand slam of darts|world matchplay|players championship)\b"),
    ("tennis", "Tennis", "🎾", r"\b(tennis|atp|wta|davis cup|billie jean king|wimbledon|roland garros|us open tennis|australian open)\b"),
    ("cycling", "Cycling", "🚴", r"\b(cycling|wielrennen|tour de france|giro|vuelta|veldrijden|cyclocross|cyclo-cross|uci|track cycling|baanwielrennen)\b"),
    ("hockey", "Ice hockey", "🏒", r"\b(nhl|ice hockey|ijshockey|khl|shl)\b"),
    ("golf", "Golf", "⛳", r"\b(golf|pga|lpga|dp world tour|liv golf|ryder cup|wtgl)\b"),
    ("rugby", "Rugby", "🏉", r"\b(rugby|six nations|premiership rugby|top 14|super rugby|nrl)\b"),
    ("cricket", "Cricket", "🏏", r"\b(cricket|ipl|test match|odi|t20|the ashes|big bash)\b"),
    ("skating", "Winter sport", "⛸️", r"\b(schaatsen|speed ?skating|skating|ski|skiing|biathlon|snowboard|shorttrack|short track)\b"),
    ("soccer", "Football", "⚽", r"\b(football|voetbal|soccer|f[uú]tbol|fotbal|futebol|nogomet|pi[lł]ka no[zż]na|calcio|fu[sß]ball|liga w[lł]oska|fc|cf|afc|sc|eredivisie|keuken kampioen|knvb|premier league|championship|laliga|la liga|bundesliga|serie a|"
                                  r"ligue 1|champions league|europa league|conference league|nations league|world cup|wk|ek|euro|fa cup|carabao|copa|mls|liga|"
                                  r"pokal|coppa|jupiler|derby|united|city|athletic|sporting|olympique|ajax|psv|feyenoord|vs\.?|v)\b"),
]
_SPORT_KIND_RES = [(k, l, e, re.compile(r, re.I)) for k, l, e, r in SPORT_KINDS]
_ESPN_KIND = {"soccer": "soccer", "football": "nfl", "racing": "f1", "basketball": "basketball", "hockey": "hockey"}
SPORT_LIVE_CACHE = {"t": 0, "data": None}
SPORT_LIVE_LOCK = threading.Lock()


def sport_kind(text, channel=""):
    low = (text or "").lower()
    nfl = _NFL_TEAMS_RE.findall(low)
    if len(set(nfl)) >= 2 or (nfl and "@" in low):
        return "nfl"
    for k, l, e, r in _SPORT_KIND_RES[:-1]:
        if r.search(low):
            return k
    ch = (channel or "").lower() if "|" not in (channel or "") else ""
    for k, l, e, r in _SPORT_KIND_RES[:-1]:
        if k in ("f1", "fight", "darts", "tennis", "cycling", "golf", "nfl") and r.search(ch):
            return k
    return "soccer" if _SPORT_KIND_RES[-1][3].search(low) else "other"


def _best_sid(g):
    """Stream id of a channel's best source (Full HD, then HD), so the TV app can play a live card directly."""
    vs = g.get("variants") or []
    v = next((v for v in vs if v.get("q") == "FHD"), None) or next((v for v in vs if v.get("q") == "HD"), None) or (vs[0] if vs else None)
    return v.get("id") if v else None


def chan_label(name):
    """Short channel name: 'NL: ESPN 1' -> 'ESPN 1'; an event channel 'NEXT | ... | NL: DISNEY+' -> 'DISNEY+ (event)'."""
    if "|" in name:
        parts = [x.strip() for x in name.split("|") if x.strip()]
        last = re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", parts[-1]) if parts else name
        last = re.sub(r"\s*\bPPV\b\s*\d*", "", last, flags=re.I).strip() or "Event"
        return last + " event"
    return re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", name)


def sport_kind_info():
    return [{"id": k, "label": l, "emoji": e} for k, l, e, _ in SPORT_KINDS if k != "other"] + [{"id": "other", "label": "More live sport", "emoji": "🏟️"}]


def _is_replay(title, desc, now_year):
    if _REPLAY_RE.search(title or "") or re.search(r"\((r|h|herh\.?|rerun)\)", title or "", re.I):
        return True
    for m in re.finditer(r"\b(19[5-9]\d|20[0-4]\d)\b(\s*[/-]\s*\d{2,4})?", title or ""):
        if int(m.group(1)) < now_year and not (m.group(2) and int(m.group(1)) >= now_year - 1):
            return True                     # "Final 1995", "Ajax v Milan (2019)": an old match; "2025/26" season names are fine
    d = (desc or "")[:400]
    if _DESC_REPLAY_RE.search(d) and not _LIVE_WORD_RE.search(title or ""):
        return True
    return False


def _espn_index(now):
    """ESPN scoreboards flattened: [(kind, event dict, normalised team keys)], fetched in parallel (cached 25 s per board)."""
    boards = list(SPORT_BOARDS) + [b for b in SPORT_BOARDS_EXTRA if b not in SPORT_BOARDS]
    res = {}

    def one(b):
        res[b] = sport_get("/%s/%s/scoreboard" % (b[0], b[1]))
    ths = [threading.Thread(target=one, args=(b,), daemon=True) for b in boards]
    for t in ths:
        t.start()
    for t in ths:
        t.join(10)
    out, ok = [], set()
    for b in boards:
        d = res.get(b)
        if d is not None:
            ok.add(b[1])
        for e in (d or {}).get("events") or []:
            try:
                ev = sport_event(b[0], b[1], b[2], e)
            except Exception:  # noqa
                continue
            out.append((b[0], ev))
    return out, ok


def _espn_match(text, espn):
    """The ESPN event whose two teams both appear in the programme title (or the race when the title names F1)."""
    nt = _sport_norm(text)
    for sport, ev in espn:
        teams = ev.get("teams") or []
        if sport == "racing":
            if re.search(r"\b(f1|formula 1|formule 1|grand prix)\b", nt) and re.search(r"\b(race|qualifying|sprint|practice|grand prix|gp|kwalificatie|training)\b", nt):
                return ev
            continue
        if len(teams) == 2 and all(_team_in(nt, t["_t"]) for t in teams):
            return ev
    return None


_LEAGUE_BOARDS = [(r"(?<!keuken kampioen )\beredivisie\b", "ned.1"), (r"(?<!canadian )(?<!northern )(?<!indian )(?<!scottish )\bpremier league\b(?! darts)", "eng.1"),
                  (r"\bla ?liga\b", "esp.1"), (r"\bbundesliga\b(?! 2)", "ger.1"), (r"\bserie a\b", "ita.1"), (r"\bligue 1\b", "fra.1"),
                  (r"\bchampions league\b", "uefa.champions"), (r"\beuropa league\b", "uefa.europa"), (r"\bconference league\b", "uefa.europa.conf"),
                  (r"\b(nfl|redzone|red zone)\b", "nfl"), (r"\b(nba)\b(?! g league)", "nba"), (r"\bnhl\b", "nhl")]
_LEAGUE_BOARDS = [(re.compile(r, re.I), b) for r, b in _LEAGUE_BOARDS]


def _league_idle(text, ev_idx, boards_ok):
    """True when the text names a league whose ESPN board loaded and none of its games is being played now."""
    low = text.lower()
    for rx, board in _LEAGUE_BOARDS:
        if board in boards_ok and rx.search(low):
            if re.search(r"\b(women|vrouwen|u\d\d|youth|jeugd)\b", low):
                return False
            return not any(ev.get("league") == board and ev.get("state") == "in" for _, ev in ev_idx)
    return False


_ACRO = {"nfl", "nba", "nhl", "mlb", "ufc", "mma", "f1", "fc", "afc", "cf", "sc", "psv", "az", "wta", "atp", "pga", "lpga", "uefa", "fifa", "knvb", "nwsl", "mls", "ncaa",
         "ii", "iii", "ac", "us", "uk", "pdc", "wec", "wrc", "gp", "tsn", "espn", "bt", "tnt", "nac", "rkc", "vvv", "nec", "ado", "sbv", "ucla", "usc", "lsu", "tcu", "byu", "smu", "unlv", "uconn", "umbc"}


def tidy_title(t):
    """'LIVE: CHICAGO - PHILADELPHIA (Z)' -> 'Chicago - Philadelphia'; mixed-case titles keep their own spelling."""
    t = re.sub(r"^(\s*(live|direct|en vivo)\b\s*[:|\-]?\s*)+", "", t or "", flags=re.I)
    t = re.sub(r"\s*\((z|ž|live|už|en directo|direct)\)\s*$", "", t, flags=re.I).strip()
    if t.isupper():
        t = " ".join(w if (w.lower().strip(".,:#()") in _ACRO or (len(w.strip(".,:#()")) <= 3 and w.isalpha() and w.lower() not in ("the", "and", "van", "de", "der", "den", "het", "for", "of", "at", "in", "on", "to", "day", "men", "cup", "run", "big", "top", "one", "new", "old"))) else ("vs." if w.lower() in ("vs.", "vs") else w.capitalize()) for w in t.split())
        t = re.sub(r"\b([A-Za-z])'S\b", r"\1's", t)
    return t


def _iso_ts(s):
    try:
        return calendar.timegm(time.strptime(str(s).replace("Z", "")[:16], "%Y-%m-%dT%H:%M"))
    except (ValueError, TypeError):
        return 0


def _espn_public(ev):
    if not ev:
        return None
    out = {k: ev.get(k) for k in ("id", "sport", "league", "league_name", "name", "state", "detail", "clock", "period", "session", "start")}
    out["teams"] = [{k: t.get(k) for k in ("name", "short", "logo", "home", "score")} for t in ev.get("teams") or []]
    return out


def _event_window(name, now):
    """Start time from an event channel's name ("... | Sun 08 Nov 00:00 CET") as a timestamp, or None."""
    low = name.lower()
    m = re.search(r"\b(\d{1,2})\s+(jan|feb|mar|mrt|apr|may|mei|jun|jul|aug|sep|oct|okt|nov|dec)[a-z]*\.?\s+(\d{1,2})[:.h](\d\d)", low)
    if not m:
        return None
    y = time.localtime(now).tm_year
    try:
        t = time.mktime((y, _MONTHS[m.group(2)], int(m.group(1)), int(m.group(3)), int(m.group(4)), 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return None
    tz = re.search(r"\b(cet|cest|gmt|bst|uk|et|est|edt|pt|pst|utc)\b", low[m.end():m.end() + 12])
    off = {"cet": 3600, "cest": 7200, "gmt": 0, "utc": 0, "bst": 3600, "uk": 0, "et": -18000, "est": -18000, "edt": -14400, "pt": -28800, "pst": -28800}
    if tz:                                  # convert from the named zone to the Mac's local time
        t += -time.timezone if not time.localtime(t).tm_isdst else -time.altzone
        t -= off[tz.group(1)]
    return t


def sport_live_all(now=None, espn=True):
    """Every sport programme being played right now on the sport and event channels, one entry per event with all its feeds."""
    now = now or time.time()
    year = time.localtime(now).tm_year
    with CATALOG_LOCK:
        cats = {c["id"]: c for c in CATALOG["cats"]}
        groups = [g for g in CATALOG["groups"] if any(_SPORT_CAT_RE.search(cats.get(c, {}).get("name", "")) for c in g["cats"])]
    hidden = set(str(c) for c in STATE["settings"].get("hidden_cats") or [])
    groups = [g for g in groups if any(c not in hidden for c in g["cats"])]
    ev_idx, boards_ok = _espn_index(now) if espn else ([], set())
    items = []
    by_epg = {}
    for g in groups:
        if g.get("epg"):
            by_epg.setdefault(g["epg"], []).append(g)
    progs = epg_rows(list(by_epg), int(now), int(now) + 1) if by_epg else {}
    for epg_id, plist in progs.items():
        for pr in plist:
            if not (pr["start"] <= now < pr["stop"]) or pr["stop"] - pr["start"] > 7 * 3600 or pr["stop"] - pr["start"] < 10 * 60:
                continue
            title = (pr["title"] or "").strip()
            desc = pr.get("description") or ""
            if not title or _is_replay(title, desc, year):
                continue
            g = by_epg[epg_id][0]
            if not is_sport_text(title + " " + desc[:200]) and not _LIVE_WORD_RE.search(title) and sport_kind(title) == "other":
                continue                    # the channel's generic filler ("Sky Sports", "Eurosport 1"), not a match
            m = _espn_match(title, ev_idx) if ev_idx else None
            if m and m.get("state") != "in" and not (m.get("state") == "pre" and _iso_ts(m.get("start")) - now < 15 * 60):
                continue                    # ESPN says this match is not being played now: a replay or a pre-game show
            if not m and _league_idle(title + " " + desc[:160], ev_idx, boards_ok):
                continue                    # names a league ESPN follows, but none of its games is being played: a replay
            items.append({"title": title, "desc": desc[:300], "start": pr["start"], "stop": pr["stop"],
                          "groups": by_epg[epg_id], "espn": m, "confirmed": bool(m) or bool(_LIVE_WORD_RE.search(title))})
    # event channels whose name carries the start time: live from the start until ~4 h later
    for g in groups:
        name = g["name"]
        if "|" not in name or not re.search(r"\b(NEXT|LIVE|TODAY|PPV|EVENT)\b", name.upper()):
            continue
        t0 = _event_window(name, now)
        if not t0 or not (t0 - 300 <= now <= t0 + 3 * 3600):
            continue
        parts = [x.strip() for x in name.split("|") if x.strip()]
        fixture = [x for x in parts[1:] if not re.search(r"\b\d{1,2}[:.]\d\d\b", x) and not re.fullmatch(r"(NEXT|LIVE|TODAY|PPV|EVENT|EXCLUSIVE|WEEK \d+|\w\w)", x.upper())]
        title = fixture[0] if fixture else (parts[1] if len(parts) > 1 else name)
        title = re.sub(r"\s*[·|]\s*(mon|tue|wed|thu|fri|sat|sun)\b.*$", "", title, flags=re.I).strip()
        if _is_replay(title, "", year):
            continue
        m = _espn_match(title, ev_idx)
        if m and m.get("state") != "in":
            continue
        if not m and _league_idle(title, ev_idx, boards_ok):
            continue
        if not m and now > t0 + (3 if sport_kind(title) in ("nfl", "fight", "golf", "cricket") else 2.25) * 3600:
            continue                        # no live data: assume a match is over ~2 h after kick-off
        items.append({"title": title, "desc": "", "start": int(t0), "stop": int(t0 + 3 * 3600), "groups": [g],
                      "espn": m, "confirmed": True, "event_ch": True})
    # one entry per event: feeds of the same match merge (normalised title, or the same ESPN event)
    merged = {}
    for it in items:
        k = ("espn", it["espn"]["id"]) if it["espn"] else ("t", re.sub(r"\b(live|20\d\d(?: \d\d)?)\b", "", norm_title(it["title"])).strip())
        cur = merged.get(k)
        if not cur:
            merged[k] = dict(it, groups=list(it["groups"]))
        else:
            for g in it["groups"]:
                if g["key"] not in {x["key"] for x in cur["groups"]}:
                    cur["groups"].append(g)
            cur["confirmed"] = cur["confirmed"] or it["confirmed"]
            if len(it["title"]) > len(cur["title"]) and not it.get("event_ch"):
                cur["title"] = it["title"]
    # "WTA Finals" and "WTA Finals Indian Wells 2026", "NASCAR Cup" and "NASCAR Cup Series": one event
    keyed = sorted(merged.items(), key=lambda kv: len(kv[0][1]) if kv[0][0] == "t" else 0)
    merged = {}
    for k, it in keyed:
        if k[0] == "t" and len(k[1].split()) >= 2:
            host = next((h for h in merged if h[0] == "t" and len(h[1].split()) >= 2 and (k[1] + " ").startswith(h[1] + " ")), None)
            if host:
                for g in it["groups"]:
                    if g["key"] not in {x["key"] for x in merged[host]["groups"]}:
                        merged[host]["groups"].append(g)
                continue
        merged[k] = it
    out = []
    for it in merged.values():
        text = it["title"] + " " + it["desc"]
        feeds = sorted(it["groups"], key=lambda g: -channel_bias(g, (it["title"] + " " + g["name"]).lower()))
        kind = _ESPN_KIND.get((it["espn"] or {}).get("sport")) or sport_kind(text, feeds[0]["name"])
        mt = (it["espn"] or {}).get("teams") or []
        if len(mt) == 2 and all(t.get("name") for t in mt):   # the live data's own spelling beats a foreign guide title
            title = ("%s @ %s" % (mt[1]["name"], mt[0]["name"])) if kind in ("nfl", "basketball", "hockey") else ("%s – %s" % (mt[0]["name"], mt[1]["name"]))
        else:
            title = tidy_title(it["title"])
        out.append({"kind": "event", "sport": kind, "title": title, "desc": it["desc"], "start": it["start"], "stop": it["stop"], "live": True,
                    "channel": chan_label(feeds[0]["name"]), "gkey": feeds[0]["key"], "icon": feeds[0].get("icon") or "",
                    "feeds": [{"gkey": g["key"], "name": chan_label(g["name"]), "country": g.get("country") or "", "icon": g.get("icon") or "", "sid": _best_sid(g)} for g in feeds[:8]],
                    "sid": _best_sid(feeds[0]),
                    "match": _espn_public(it["espn"]), "confirmed": it["confirmed"], "event_ch": bool(it.get("event_ch")), "_bias": channel_bias(feeds[0], (text + " " + feeds[0]["name"]).lower())})
    # ESPN games being played that no channel shows in its guide: listed so you know, with a search shortcut
    shown = {o["match"]["id"] for o in out if o["match"]}
    elsewhere = [_espn_public(ev) for s, ev in ev_idx if ev.get("state") == "in" and ev.get("id") not in shown]
    return {"t": int(now), "items": out, "elsewhere": elsewhere}


def sport_live_cached(max_age=45):
    with SPORT_LIVE_LOCK:
        c = SPORT_LIVE_CACHE
        if c["data"] and time.time() - c["t"] < max_age:
            return c["data"]
    d = sport_live_all()
    with SPORT_LIVE_LOCK:
        SPORT_LIVE_CACHE.update(t=time.time(), data=d)
    return d


def sport_highlight(text, p):
    """Why a live event is worth a look (spoiler-free): a final, a Dutch interest, your team, a derby, a big tournament."""
    names, derbies, tournaments = sport_lists(p)
    low = text.lower()
    if re.search(r"\b(final|finale|finals|super bowl|title fight|decider|game 7|grand final)\b", low) and not re.search(r"\b(semi|quarter|halve|kwart)", low):
        return "Final", 40
    if team_hit(low, [t.lower() for t in (p.get("teams") or [])]):
        return "Your team", 35
    if any(re.search(r"(?<![a-z])" + re.escape(n) + r"(?![a-z])", low) for n in names):
        return "Dutch interest", 30
    if _SEMI_RE.search(low) and (any(t in low for t in tournaments) or any(sp in low for sp in (p.get("sports") or []))):
        return "Knock-out", 25
    if any(d in low for d in derbies):
        return "Derby", 20
    if any(t in low for t in tournaments):
        return "Big event", 20
    return "", 0


def sport_live_scored(p, data=None):
    """The live list ordered for this profile: your sports, teams and big moments first; foreign feeds last."""
    data = data or sport_live_cached()
    sports = [s.lower() for s in (p.get("sports") or [])]
    out = []
    for it in data["items"]:
        text = (it["title"] + " " + it["desc"] + " " + ((it.get("match") or {}).get("league_name") or "")).lower()
        tag, s = sport_highlight(text, p)
        if any(sp in text for sp in sports):
            s += 15
        if it.get("match"):
            s += 10 + (10 if (it["match"].get("league") or "") in ("ned.1", "eng.1", "esp.1", "ger.1", "ita.1", "uefa.champions", "uefa.europa", "nfl", "f1", "fifa.world", "uefa.euro") else 0)
        if it.get("confirmed"):
            s += 5
        if it.get("event_ch") and not it.get("match"):
            s -= 10                         # a stand-alone event channel without live data (often minor college games)
        s += it["_bias"]
        t = {k: v for k, v in it.items() if not k.startswith("_")}
        t["score"], t["tag"] = s, tag
        t["local"] = it["_bias"] == 0           # a feed from your countries, in English/Dutch
        t["reason"] = "Live now"
        out.append(t)
    out.sort(key=lambda t: (-t["score"], t["start"]))
    return out


def sport_overview(pid):
    p = profile(pid)
    data = sport_live_cached()
    items = sport_live_scored(p, data)
    groups = []
    for info in sport_kind_info():
        its = [t for t in items if t["sport"] == info["id"]]
        if its:
            groups.append(dict(info, items=its))
    groups.sort(key=lambda gr: -max(t["score"] for t in gr["items"]))
    return {"t": data["t"], "n": len(items), "n_local": sum(1 for t in items if t["local"]), "groups": groups, "elsewhere": data["elsewhere"], "spoiler_free": STATE["settings"].get("spoiler_free", True) is not False}


def home_live_row(pid):
    """The 'Live now' row for Tonight: only sport that is being played right now (fresh, not from the 30-minute home cache)."""
    try:
        items = [t for t in sport_live_scored(profile(pid)) if t["local"] and (t["score"] >= 10 or t["tag"])]
    except Exception:  # noqa
        items = []
    return {"id": "live", "title": "Live now", "kind": "events", "tiles": items[:12], "total": len(items)}


def home_with_live(pid, force=False):
    h = dict(home_get(pid, force=force))
    rows = [r for r in h.get("rows") or [] if r.get("id") != "live"]
    rows.insert(0, home_live_row(pid))
    h["rows"] = rows
    return h


# ------------------------------------------------------------------ search ----
def search_all(q):
    q = (q or "").strip()
    if len(q) < 2:
        return {"channels": [], "programmes": [], "movies": [], "series": []}
    n = norm_title(q)
    ql = q.lower()
    hidden = set(str(c) for c in STATE["settings"].get("hidden_cats") or [])
    with CATALOG_LOCK:
        chans = [g for g in CATALOG["groups"] if ql in g["name"].lower() and any(c not in hidden for c in g["cats"])][:12]
    progs = [p for p in epg_search(q, 60) if p["stop"] > time.time() - 3 * 86400][:15]
    with CATALOG_LOCK:
        by_epg = {}
        for g in CATALOG["groups"]:
            if g.get("epg") and any(c not in hidden for c in g["cats"]):
                by_epg.setdefault(g["epg"], g)
    for p in progs:
        g = by_epg.get(p["ch"])
        p["channel"] = g["name"] if g else ""
        p["gkey"] = g["key"] if g else ""
        p["icon"] = g["icon"] if g else ""
        p["catchup"] = bool(g and g.get("archive") and p["stop"] < time.time())
    progs = [p for p in progs if p["gkey"]]
    with VOD_LOCK:
        vods = [g for g in VOD["groups"].values() if n and n in g["norm"]]
    vods = [g for g in vods if vod_visible(g, allow_dead=True)]
    vods.sort(key=lambda g: (0 if g["norm"] == n else 1, -g["year"], g["title"]))
    def dedupe(tiles):
        """Same TMDb title under two provider names: show it once (the one with the most editions)."""
        seen, out = {}, []
        for t in tiles:
            tid = (t.get("meta") or {}).get("id")
            if tid and tid in seen:
                if t["variants"] > out[seen[tid]]["variants"]:
                    out[seen[tid]] = t
                continue
            if tid:
                seen[tid] = len(out)
            out.append(t)
        return out
    movies = dedupe([vod_tile(g) for g in vods if g["kind"] == "movie"][:30])[:20]
    series = dedupe([vod_tile(g) for g in vods if g["kind"] == "series"][:30])[:20]
    return {"channels": [{"key": g["key"], "name": g["name"], "icon": g["icon"], "best": g.get("best", ""), "archive": g.get("archive", 0)} for g in chans],
            "programmes": progs, "movies": movies, "series": series}


# ------------------------------------------------------------ opensubtitles ----
_OSUB_TOKEN = {"v": "", "t": 0}
OSUB_RETRY_NONE = 3 * 86400       # OpenSubtitles had no English subtitles: ask again after a few days
OSUB_RETRY_FAILED = 3600          # the download failed (daily limit, network): ask again within the hour
SUBS_WAIT = float(os.environ.get("IPTV_SUBS_WAIT", "9"))    # seconds a film may wait for the subtitle lookup before it starts


def osub_headers():
    h = {"Api-Key": key("OPENSUBTITLES_API_KEY"), "User-Agent": "Beeldbuis v%d" % VERSION, "Accept": "application/json", "Content-Type": "application/json"}
    user, pw = key("OPENSUBTITLES_USER"), key("OPENSUBTITLES_PASSWORD")
    if user and pw:
        if not _OSUB_TOKEN["v"] or time.time() - _OSUB_TOKEN["t"] > 20 * 3600:
            try:
                r = http_json(OSUB_BASE + "/login", h, data=json.dumps({"username": user, "password": pw}).encode(), method="POST")
                _OSUB_TOKEN["v"], _OSUB_TOKEN["t"] = r.get("token", ""), time.time()
            except Exception:  # noqa
                _OSUB_TOKEN["v"] = ""
        if _OSUB_TOKEN["v"]:
            h["Authorization"] = "Bearer " + _OSUB_TOKEN["v"]
    return h


def srt_to_vtt(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    out = ["WEBVTT", ""]
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [l for l in block.split("\n") if l.strip()]
        if not lines:
            continue
        if re.match(r"^\d+$", lines[0].strip()):
            lines = lines[1:]
        if not lines or "-->" not in lines[0]:
            continue
        tl = re.sub(r"(\d+):(\d+):(\d+),(\d+)", r"\1:\2:\3.\4", lines[0])
        tl = re.sub(r"\s+-->\s+", " --> ", tl.strip())
        tl = re.sub(r"(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3}).*", r"\1 --> \2", tl)
        body = "\n".join(re.sub(r"<(?!/?(i|b|u)>)[^>]+>", "", l) for l in lines[1:]).replace("{\\an8}", "")
        out += [tl, body, ""]
    return "\n".join(out)


def vtt_shift(text, seconds):
    """Shift all cue times (for streams started with -ss); cues ending before 0 are dropped."""
    def ts(s):
        h, m, rest = s.split(":")
        return int(h) * 3600 + int(m) * 60 + float(rest)
    def fmt(t):
        t = max(0.0, t)
        return "%02d:%02d:%06.3f" % (int(t // 3600), int(t % 3600 // 60), t % 60)
    out = []
    skip = False
    for line in text.split("\n"):
        m = re.match(r"^(\d{2}:\d{2}:\d{2}\.\d{3}) --> (\d{2}:\d{2}:\d{2}\.\d{3})(.*)$", line)
        if m:
            a, b = ts(m.group(1)) + seconds, ts(m.group(2)) + seconds
            skip = b <= 0
            if skip:
                continue
            out.append("%s --> %s%s" % (fmt(a), fmt(b), m.group(3)))
        elif skip and line.strip():
            continue
        else:
            skip = False
            out.append(line)
    return "\n".join(out)


def osub_find(kind, title, year=0, imdb_id="", season=0, episode=0, cache_key=""):
    """English subtitles from OpenSubtitles for a title; returns the path of a cached .vtt or None."""
    if not key("OPENSUBTITLES_API_KEY"):
        return None
    ck = cache_key or "%s:%s|%s|%s|%s" % (kind, norm_title(title), year, season, episode)
    conn = meta_db()
    try:
        r = conn.execute("SELECT file, src, t FROM subs WHERE k=?", (ck,)).fetchone()
    finally:
        conn.close()
    if r:
        if r[0] and os.path.exists(r[0]):
            return r[0]
        # nothing last time: look again after a while. A new release gets subtitles within days; a failed download
        # (the free daily limit, a network error) is worth another try within the hour
        if time.time() - (r[2] or 0) < (OSUB_RETRY_FAILED if r[1] == "failed" or r[0] else OSUB_RETRY_NONE):
            return None
    params = {"languages": "en", "order_by": "download_count", "order_direction": "desc"}
    if imdb_id and imdb_id.startswith("tt") and kind == "movie":
        params["imdb_id"] = imdb_id.lstrip("t").lstrip("0") or "0"
    else:
        params["query"] = title
        if year and kind == "movie":
            params["year"] = year
        params["type"] = "movie" if kind == "movie" else "episode"
    if kind != "movie" and season and episode:
        params["season_number"], params["episode_number"] = season, episode
        if imdb_id and imdb_id.startswith("tt"):
            params["parent_imdb_id"] = imdb_id.lstrip("t").lstrip("0") or "0"
            params.pop("query", None)
    hdr = osub_headers()
    try:
        data = http_json(OSUB_BASE + "/subtitles?" + urllib.parse.urlencode(params), hdr)
    except Exception as e:  # noqa
        log_write("[subs] search failed: %s\n" % e)
        return None
    items = [x.get("attributes", {}) for x in (data.get("data") or [])]
    items = [a for a in items if a.get("language") == "en" and a.get("files")]
    items.sort(key=lambda a: (a.get("hearing_impaired") is True, a.get("ai_translated") is True, a.get("machine_translated") is True,
                              -(a.get("download_count") or 0)))
    path, failed = None, False
    for a in items[:2]:
        fid = a["files"][0].get("file_id")
        try:
            dl = http_json(OSUB_BASE + "/download", hdr, data=json.dumps({"file_id": fid, "sub_format": "srt"}).encode(), method="POST")
            link = dl.get("link")
            if not link:
                log_write("[subs] no link: %s\n" % json.dumps(dl)[:200])
                failed = True
                break
            req = urllib.request.Request(link, headers={"User-Agent": "Beeldbuis v%d" % VERSION})
            with urllib.request.urlopen(req, timeout=30, context=API_SSL) as r:
                raw = r.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp1252", "replace")
            vtt = srt_to_vtt(text) if not text.lstrip().startswith("WEBVTT") else text
            if vtt.count("-->") < 5:
                continue
            os.makedirs(SUBS_DIR, exist_ok=True)
            path = os.path.join(SUBS_DIR, re.sub(r"[^a-z0-9]+", "_", ck.lower())[:80] + ".vtt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(vtt)
            break
        except urllib.error.HTTPError as e:
            log_write("[subs] download failed HTTP %s\n" % e.code)
            failed = True
            if e.code in (406, 429):
                break
        except Exception as e:  # noqa
            failed = True
            log_write("[subs] download failed: %s\n" % e)
    conn = meta_db()
    try:
        conn.execute("INSERT OR REPLACE INTO subs (k, file, src, t) VALUES (?,?,?,?)",
                     (ck, path or "", "opensubtitles" if path or not failed else "failed", int(time.time())))
        conn.commit()
    finally:
        conn.close()
    return path


SUBS_BUSY = {}        # (kind, sid) -> (thread looking for subtitles, its result box)
SUBS_LOCK = threading.Lock()


def external_subs_for(kind, sid, ext, wait=None):
    """English subtitles for a movie/episode that has no English text track: a .vtt path or None. The lookup talks to
    OpenSubtitles, which can be slow or down; the film waits at most `wait` seconds for it (SUBS_WAIT). A lookup that
    takes longer finishes in the background, so the subtitles are there the next time the title starts."""
    wait = SUBS_WAIT if wait is None else wait
    k = (kind, str(sid))
    with SUBS_LOCK:
        cur = SUBS_BUSY.get(k)
        if cur is None or not cur[0].is_alive():
            box = {}

            def run():
                try:
                    box["v"] = _external_subs_for(kind, sid, ext)
                except Exception as e:  # noqa
                    log_write("[subs] %s\n" % e)
                finally:
                    with SUBS_LOCK:
                        if SUBS_BUSY.get(k, (None,))[0] is threading.current_thread():
                            SUBS_BUSY.pop(k, None)
            cur = (threading.Thread(target=run, daemon=True), box)
            SUBS_BUSY[k] = cur
            cur[0].start()
    th, box = cur
    th.join(wait)
    if th.is_alive():
        log_write("[subs] lookup for %s %s takes long; starting without, it carries on in the background\n" % (kind, sid))
    return box.get("v")


def subs_busy(kind, sid):
    cur = SUBS_BUSY.get((kind, str(sid)))
    return bool(cur and cur[0].is_alive())


def _external_subs_for(kind, sid, ext):
    if STATE["settings"].get("subs") is False or STATE["settings"].get("opensubs") is False:
        return None
    if kind == "movie":
        with VOD_LOCK:
            gk = (VOD.get("by_sid") or {}).get("movie:%s" % sid)
            g = VOD["groups"].get(gk) if gk else None
        if not g:
            return None
        m = meta_cached(g["key"]) or {}
        res = osub_find("movie", g["title"], g["year"], m.get("imdb", ""), cache_key="movie:%s" % g["key"])
        if not res:
            whisper_enqueue("movie", sid, ext, "%s%s" % (g["title"], " (%s)" % g["year"] if g.get("year") else ""), "movie:%s" % g["key"])
        return res
    if kind == "series":
        ep = episodes_get(sid)
        if not ep:
            return None
        g = vod_group(ep["gkey"]) if ep.get("gkey") else None
        m = meta_cached(g["key"]) if g else {}
        res = osub_find("series", ep["series"], 0, (m or {}).get("imdb", ""), ep["season"], ep["episode"], cache_key="ep:%s" % sid)
        if not res:
            whisper_enqueue("series", sid, ext, "%s S%02dE%02d" % (ep["series"], int(ep["season"] or 0), int(ep["episode"] or 0)), "ep:%s" % sid)
        return res
    return None


EPISODES = {}   # episode stream id -> {"series", "season", "episode", "gkey"}: remembered when a series is opened
EPISODES_PATH = os.path.join(CONFIG_DIR, "episodes.json")
EPISODES_STATE = {"loaded": False}
EPISODES_LOCK = threading.Lock()


def episodes_get(sid):
    """Which series, season and episode a stream id is. Kept on disk too: after a restart (an update) "Continue
    watching" starts an episode without opening its series first, and it still needs its English subtitles."""
    with EPISODES_LOCK:
        if not EPISODES_STATE["loaded"]:
            EPISODES_STATE["loaded"] = True
            try:
                with open(EPISODES_PATH, "r", encoding="utf-8") as f:
                    for k, v in (json.load(f) or {}).items():
                        EPISODES.setdefault(k, v)
            except (OSError, ValueError, AttributeError):
                pass
        return EPISODES.get(str(sid))


def episodes_save():
    with EPISODES_LOCK:
        items = list(EPISODES.items())[-6000:]
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = EPISODES_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(items), f, separators=(",", ":"))
        os.replace(tmp, EPISODES_PATH)
    except OSError:
        pass


def remember_episodes(series_name, series_id, info):
    title, year, _, _ = vod_parse(series_name)
    gk = "series:%s|%d" % (norm_title(title), year)
    g = vod_group(gk)
    episodes_get(0)          # make sure what is on disk has been read before adding to it
    changed = False
    for season, eps in (info.get("episodes") or {}).items():
        for e in eps or []:
            try:
                rec = {"series": title, "season": int(season), "episode": int(e.get("episode_num") or 0), "gkey": g["key"] if g else "",
                           "series_id": str(series_id)}
            except (TypeError, ValueError):
                continue
            with EPISODES_LOCK:
                k = str(e.get("id"))
                if EPISODES.get(k) != rec:
                    EPISODES.pop(k, None)        # re-insert: recently opened series stay when the file is trimmed
                    EPISODES[k] = rec
                    changed = True
    if changed:
        episodes_save()


def library_refresher():
    """Keeps the movie/series index, trending list, Letterboxd feed and taste-driven rows fresh."""
    time.sleep(12)
    while True:
        with LOCK:
            logged_in = bool(STATE["server"])
        if logged_in:
            keys_load()
            for fn in (vod_refresh, trending_refresh, lb_rss_refresh):
                try:
                    fn()
                except Exception:  # noqa
                    pass
            if os.path.exists(LB_ZIP) and os.path.getmtime(LB_ZIP) > LB["t"]:
                try:
                    lb_import_zip()
                except Exception as e:  # noqa
                    LB["error"] = "Letterboxd zip: %s" % e
                    LB["t"] = time.time()
            try:
                for pid in ("p1", "p2", "both"):
                    with HOME_LOCK:
                        stale = pid not in HOME or time.time() - HOME[pid]["t"] > HOME_TTL
                    if stale and VOD["n"]:
                        home_get(pid, force=True)
            except Exception:  # noqa
                pass
        time.sleep(600)


# ---------------------------------------------------------------- browse ----
# Movies / Series as shelves instead of one endless list: new, highly rated, recent, 4K, short films, hidden gems,
# decades and genres (the profile's own genres first). GET /api/shelves?kind=movie&p=p1 gives the rows,
# GET /api/shelves?kind=movie&shelf=genre:Drama&offset=0&limit=60 gives one shelf in full ("See all").
# Built on the library groups + the metadata cache; the index is rebuilt at most every 10 minutes.
import math  # noqa: E402 (kept inside this section so the patch stays self-contained)

BROWSE = {"t": 0, "vt": 0, "idx": {}}
BROWSE_LOCK = threading.Lock()
BROWSE_TTL = 600
SHELF_N = 24


def browse_index(kind):
    """Library key -> compact facts used for shelves: rating, votes, runtime, genres, year, seasons."""
    with BROWSE_LOCK:
        if time.time() - BROWSE["t"] < BROWSE_TTL and BROWSE["vt"] == VOD.get("t") and kind in BROWSE["idx"]:
            return BROWSE["idx"][kind]
    with VOD_LOCK:
        groups = [g for g in VOD["groups"].values() if g["kind"] == kind]
    facts = {}
    conn = meta_db()
    try:
        for k, v in conn.execute("SELECT k, v FROM meta WHERE kind=?", (kind,)):
            try:
                m = json.loads(v)
            except ValueError:
                continue
            facts[k] = (m.get("imdb_rating"), m.get("vote"), m.get("votes"), m.get("runtime"), m.get("genres") or [], m.get("seasons"), m.get("poster"), m.get("id") or 0,
                        {"iv": m.get("imdb_votes"), "rt": m.get("rt"), "mc": m.get("mc"), "pop": m.get("pop"), "ol": m.get("lang") or "",
                         "cc": m.get("countries") or [], "ty": m.get("year") or 0})
    finally:
        conn.close()
    # The provider lists some titles twice under different names ("Dune" and "Dune Part One", both 2021). When TMDb
    # says they are the same title, keep the group with the most editions on the shelves.
    best_by_id = {}
    for g in groups:
        f = facts.get(g["key"])
        if f and f[7]:
            cur = best_by_id.get(f[7])
            if cur is None or len(g["variants"]) > len(cur["variants"]):
                best_by_id[f[7]] = g
    idx = {}
    for g in groups:
        f = facts.get(g["key"])
        if f and f[7] and best_by_id.get(f[7]) is not g:
            continue
        gens = list(f[4]) if f else []
        if not gens and g.get("genre"):     # series: the provider's own genre field when TMDb has nothing yet
            gens = [x.strip() for x in re.split(r"[,/&]", g["genre"]) if x.strip()][:3]
        idx[g["key"]] = {"g": g, "imdb": f[0] if f else None, "vote": f[1] if f else None, "votes": f[2] if f else None,
                         "runtime": f[3] if f else None, "genres": gens, "seasons": f[5] if f else None, "poster": bool(f and f[6]),
                         "langs": {v["lang"] for v in g["variants"]}, "has4k": any(v["q"] == "4K" for v in g["variants"])}
        idx[g["key"]].update(f[8] if f else {"iv": None, "rt": None, "mc": None, "pop": None, "ol": "", "cc": [], "ty": 0})
    with BROWSE_LOCK:
        BROWSE["idx"][kind] = idx
        BROWSE["t"] = time.time()
        BROWSE["vt"] = VOD.get("t")
    return idx


def browse_rating(x):
    """One quality number: IMDb when known, else TMDb vote (only with enough votes), else the provider's own rating
    (so the shelves are filled from day one, before the metadata backfill has reached a title)."""
    if x["imdb"]:
        return float(x["imdb"])
    if x["vote"] and (x["votes"] or 0) >= 200:
        return float(x["vote"])
    if x["vote"] is None and x["g"]["rating"]:
        r = float(x["g"]["rating"])
        r = r if r <= 10 else r / 10.0
        return r if 1.0 <= r <= 9.4 else 0.0      # a flat 10 from the provider usually means "unknown"
    return 0.0


def browse_score(x):
    r = browse_rating(x)
    return r + min(math.log10((x["votes"] or 0) + 1), 5.5) * 0.25 if r else 0.0


def browse_shelves_def(kind, p):
    """(id, title, hint, filter, sort) for every shelf, in display order."""
    year = time.localtime().tm_year
    now = time.time()
    good = lambda x: browse_rating(x) >= 7.0
    by_score = lambda x: -browse_score(x)
    by_added = lambda x: -x["g"]["added"]
    out = [
        ("new", "New at your provider", "Added in the last 30 days, best first",
         lambda x: now - x["g"]["added"] < 30 * 86400 and (x["poster"] or x["g"]["icon"]), lambda x: (-(browse_rating(x) >= 6.5), by_added(x))),
        ("top", "Highly rated", "IMDb 7.5 and up", lambda x: browse_rating(x) >= 7.5 and (x["votes"] is None or x["votes"] >= 1000), by_score),
        ("recent", "Recent releases", "From %d and %d" % (year - 1, year), lambda x: x["g"]["year"] >= year - 1 and browse_rating(x) >= 6.3, by_score),
        ("gems", "Hidden gems", "Great ratings, few people know them", lambda x: disc_gem(x) is not None, lambda x: -(disc_gem(x) or 0)),
        ("critics", "Critics' favourites", "Rotten Tomatoes 90%+ or Metacritic 80+",
         lambda x: ((x.get("rt") or 0) >= 90 or (x.get("mc") or 0) >= 80) and disc_rating(x) >= 6.8, by_score),
        ("world", "World gems", "Not in English, loved anyway",
         lambda x: x.get("ol") and x["ol"] != "en" and disc_gem(x) is not None, lambda x: -(disc_gem(x) or 0)),
    ]
    if kind == "movie":
        out.append(("short", "Under 100 minutes", "Good films for a short evening",
                    lambda x: x["runtime"] and 60 <= x["runtime"] <= 100 and good(x), by_score))
    else:
        out.append(("mini", "One season", "Miniseries and one-season shows", lambda x: x["seasons"] == 1 and good(x), by_score))
        out.append(("binge", "Long runners", "Five seasons or more", lambda x: (x["seasons"] or 0) >= 5 and good(x), by_score))
    out.append(("4k", "In 4K", "Best picture quality", lambda x: x["has4k"] and browse_rating(x) >= 6.5, by_score))
    if "NL" in (STATE["settings"].get("vod_langs") or DEFAULT_VOD_LANGS):
        out.append(("nl", "Dutch", "Dutch versions and Dutch-made", lambda x: "NL" in x["langs"] and browse_rating(x) >= 6, by_score))
    # genres: the profile's own first, avoided ones left out, the rest by size
    counts = {}
    for x in browse_index(kind).values():
        for gname in x["genres"]:
            counts[gname] = counts.get(gname, 0) + 1
    avoid = {a.lower() for a in (p.get("avoid_genres") or [])}
    mine = [gname for gname in (p.get("genres") or []) if counts.get(gname)]
    rest = [gname for gname, n in sorted(counts.items(), key=lambda kv: -kv[1]) if n >= 12 and gname not in mine]
    for gname in (mine + rest)[:14]:
        if gname.lower() in avoid:
            continue
        out.append(("genre:" + gname, gname, "Your genre" if gname in mine else "",
                    (lambda gn: lambda x: gn in x["genres"] and browse_rating(x) >= 6.0)(gname), by_score))
    for dec in ((1980, "The 80s"), (1990, "The 90s"), (2000, "The 2000s"), (2010, "The 2010s")):
        out.append(("decade:%d" % dec[0], dec[1], "Classics, IMDb 7 and up",
                    (lambda d: lambda x: d <= x["g"]["year"] < d + 10 and good(x))(dec[0]), by_score))
    return out


def browse_rows(kind, pid="", shelf="", offset=0, limit=60):
    kind = "series" if kind == "series" else "movie"
    p = profile(pid or STATE["settings"].get("profile") or "p1")
    idx = browse_index(kind)
    fb = feedback_all(p["id"])
    pool = [x for x in idx.values() if vod_visible(x["g"]) and fb.get(x["g"]["key"]) not in ("down", "seen")]
    avoid = {a.lower() for a in (p.get("avoid_genres") or [])}
    if avoid:
        pool = [x for x in pool if not any(gn.lower() in avoid for gn in x["genres"])]
    defs = browse_shelves_def(kind, p)
    if shelf:
        d = next((d for d in defs if d[0] == shelf), None)
        if not d:
            raise RuntimeError("Unknown shelf")
        hits = sorted((x for x in pool if d[3](x)), key=d[4])
        metas = meta_cached_many([x["g"]["key"] for x in hits[offset:offset + limit]])
        return {"id": d[0], "title": d[1], "hint": d[2], "total": len(hits),
                "items": [vod_tile(x["g"], meta=metas.get(x["g"]["key"]) or {}) for x in hits[offset:offset + limit]]}
    rows, used = [], {}
    for d in defs:
        hits = sorted((x for x in pool if d[3](x)), key=d[4])
        if len(hits) < 6:
            continue
        # the same title should not fill every shelf: prefer ones not shown yet, max 2 appearances
        pick = [x for x in hits if used.get(x["g"]["key"], 0) == 0][:SHELF_N]
        if len(pick) < SHELF_N:
            pick += [x for x in hits if used.get(x["g"]["key"], 0) == 1 and x not in pick][:SHELF_N - len(pick)]
        if len(pick) < 6:
            continue
        for x in pick:
            used[x["g"]["key"]] = used.get(x["g"]["key"], 0) + 1
        rows.append((d, len(hits), pick))
    metas = meta_cached_many([x["g"]["key"] for _, _, pick in rows for x in pick])
    return {"kind": kind, "profile": p["id"], "t": int(time.time()), "with_meta": sum(1 for x in idx.values() if x["vote"] is not None), "total": len(idx),
            "tmdb": bool(key("TMDB_API_KEY") or key("TMDB_READ_TOKEN")), "backlog": len(BACKFILL["keys"]),
            "rows": [{"id": d[0], "title": d[1], "hint": d[2], "n": n,
                      "items": [vod_tile(x["g"], meta=metas.get(x["g"]["key"]) or {}) for x in pick]} for d, n, pick in rows]}


# -------------------------------------------------------------- discover ----
# Sort & filter for Movies / Series, built to dig up hidden gems in a huge library.
# GET /api/discover?kind=movie&p=p1&sort=gem&genre=Drama&xgenre=Horror&min=7&rt=80&fame=hidden&y0=1990&y1=1999
#     &len=short|mid|long|epic (movies) / seasons=1|few|many and ep=short|mid|long (series)&ol=en|foreign|ja,ko
#     &added=30&k4=1&seen=1 (show seen titles too)&shelf=<shelf id>&q=<text>&offset=0&limit=60
# "fame" is the number of IMDb votes (or TMDb votes x 12 when IMDb is not known yet): it tells famous from unknown.
DISC_SORTS = ("best", "gem", "imdb", "rt", "mc", "popular", "votes", "new", "year", "old", "short", "long", "az", "random")
LANG_NAMES = {"en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian", "ja": "Japanese", "ko": "Korean",
              "zh": "Chinese", "cn": "Cantonese", "hi": "Hindi", "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian",
              "fi": "Finnish", "pt": "Portuguese", "ru": "Russian", "pl": "Polish", "tr": "Turkish", "fa": "Persian", "ar": "Arabic",
              "th": "Thai", "id": "Indonesian", "te": "Telugu", "ta": "Tamil", "ml": "Malayalam", "he": "Hebrew", "is": "Icelandic",
              "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "el": "Greek", "uk": "Ukrainian", "tl": "Filipino", "sr": "Serbian"}


def disc_fame(x):
    if x.get("iv"):
        return int(x["iv"])
    if x.get("votes"):
        return int(x["votes"]) * 12
    return None


def disc_rating(x):
    """IMDb when known; else TMDb, pulled towards 6.5 when few people voted (3 votes of 10/10 is not a masterpiece);
    else the provider's own rating, so filters work before the metadata backfill reaches a title."""
    if x.get("imdb"):
        return float(x["imdb"])
    v, n = x.get("vote"), x.get("votes") or 0
    if v and n >= 20:
        # brand-new titles get early fan votes on TMDb: pull them in harder until IMDb is known
        yr = x["g"]["year"] or x.get("ty") or 0
        m = 400 if yr >= time.localtime().tm_year - 1 else 120
        return round((v * n + 6.5 * m) / (n + m), 2)
    if v is None:
        return browse_rating(x)
    return 0.0


def disc_gem(x):
    """Hidden-gem score: high rating, recognised by critics, but few people know it. None = not a gem."""
    r = disc_rating(x)
    fame = disc_fame(x)
    lo, hi = (800, 60000) if x["g"]["kind"] == "movie" else (300, 40000)
    if r < 7.0 or not fame or not lo <= fame <= hi:
        return None
    if not x.get("imdb") and ((x.get("votes") or 0) < 80 or (x["g"]["year"] or x.get("ty") or 0) >= time.localtime().tm_year):
        return None                        # this year's titles only count once IMDb confirms them
    s = r - 0.45 * max(0.0, math.log10(fame) - 3.7)
    rt, mc = x.get("rt"), x.get("mc")
    if rt is not None:
        s += 0.35 if rt >= 90 else 0.15 if rt >= 80 else -0.3 if rt < 60 else 0
    if mc is not None:
        s += 0.2 if mc >= 75 else -0.2 if mc < 50 else 0
    if fame < 2000:
        s -= 0.2
    return round(s, 3)


def disc_best(x):
    r = disc_rating(x)
    if r and x.get("vote") is None and not x.get("imdb"):
        return r - 0.8                     # only the provider's own rating: below titles that TMDb/IMDb confirm
    return r + 0.2 * min(math.log10((disc_fame(x) or 0) + 1), 6.0) if r else 0.0


def disc_seen(pid, kind):
    """Library keys the profile has seen: marked Seen or 👎, finished (90 %+), or watched on Letterboxd."""
    pids = ["p1", "p2"] if pid == "both" else [pid]
    out = set()
    conn = meta_db()
    try:
        for p_ in pids:
            out.update(k for k, v in conn.execute("SELECT k, verdict FROM feedback WHERE profile=?", (p_,)) if v in ("seen", "down"))
            try:
                out.update(k for (k,) in conn.execute("SELECT k FROM history WHERE profile=? AND done=1", (p_,)))
            except sqlite3.Error:
                pass
            try:
                lbw = conn.execute("SELECT norm, year FROM lb WHERE profile=? AND watched=1", (p_,)).fetchall()
            except sqlite3.Error:
                lbw = []
            with VOD_LOCK:
                for n, y in lbw:
                    for g in VOD["by_norm"].get(n, []):
                        if g["kind"] == kind and (not y or not g["year"] or abs(g["year"] - y) <= 1):
                            out.add(g["key"])
    finally:
        conn.close()
    return out


def _disc_len(x, kind, want, ep=""):
    rt_ = x.get("runtime")
    if kind == "movie":
        if not want:
            return True
        if not rt_:
            return False
        return {"short": 40 <= rt_ < 90, "mid": 90 <= rt_ <= 120, "long": 120 < rt_ <= 150, "epic": rt_ > 150}.get(want, True)
    if want:
        se = x.get("seasons")
        if not se:
            return False
        if not {"1": se == 1, "few": 2 <= se <= 4, "many": se >= 5}.get(want, True):
            return False
    if ep:
        if not rt_:
            return False
        return {"short": rt_ <= 30, "mid": 30 < rt_ <= 60, "long": rt_ > 60}.get(ep, True)
    return True


def disc_reason(x, sort):
    """Short line under the poster that explains why it is here."""
    bits = []
    if x.get("imdb"):
        bits.append("IMDb %.1f" % x["imdb"])
    elif x.get("vote") and (x.get("votes") or 0) >= 20:
        bits.append("TMDb %.1f" % x["vote"])
    if not bits and x["g"].get("rating") and x.get("vote") is None:
        bits.append("★ %s at provider" % (("%.1f" % x["g"]["rating"]).rstrip("0").rstrip(".")))
    if x.get("rt") is not None and sort in ("rt", "gem", "best", "imdb"):
        bits.append("RT %d%%" % x["rt"])
    if sort == "mc" and x.get("mc") is not None:
        bits.append("MC %d" % x["mc"])
    fame = disc_fame(x)
    if sort in ("gem", "votes", "popular") and fame:
        bits.append(("" if x.get("iv") else "~") + ("%.1fk" % (fame / 1000.0) if fame < 100000 else "%dk" % (fame // 1000)) + " votes")
    if sort in ("short", "long") and x.get("runtime"):
        r = int(x["runtime"])
        bits.append("%dh %02dm" % (r // 60, r % 60) if r >= 60 else "%d min" % r)
    if sort == "new" and x["g"].get("added"):
        days = int((time.time() - x["g"]["added"]) / 86400)
        bits.append("added today" if days < 1 else "added %dd ago" % days)
    if x.get("ol") and x["ol"] != "en" and len(bits) < 3:
        bits.append(LANG_NAMES.get(x["ol"], x["ol"].upper()))
    return " · ".join(bits[:3])


DISC_FACETS = {}


def disc_facets(kind, idx_):
    """Genre and original-language counts for the filter menus (cached with the browse index)."""
    ck = (kind, BROWSE.get("t"))
    if ck in DISC_FACETS:
        return DISC_FACETS[ck]
    gc, lc = {}, {}
    for x in idx_.values():
        if not vod_visible(x["g"]):
            continue
        for gn in x["genres"]:
            gc[gn] = gc.get(gn, 0) + 1
        if x.get("ol"):
            lc[x["ol"]] = lc.get(x["ol"], 0) + 1
    out = {"genres": [{"name": n, "n": c} for n, c in sorted(gc.items(), key=lambda kv: -kv[1]) if c >= 5][:30],
           "langs": [{"code": c, "name": LANG_NAMES.get(c, c.upper()), "n": n} for c, n in sorted(lc.items(), key=lambda kv: -kv[1]) if n >= 5][:24]}
    DISC_FACETS.clear()
    DISC_FACETS[ck] = out
    return out


def discover(kind, pid="", f=None, offset=0, limit=60):
    f = f or {}
    kind = "series" if kind == "series" else "movie"
    p = profile(pid or STATE["settings"].get("profile") or "p1")
    idx_ = browse_index(kind)
    sort = f.get("sort") if f.get("sort") in DISC_SORTS else "best"
    num = lambda k, d=0.0: float(f.get(k)) if str(f.get(k) or "").replace(".", "", 1).isdigit() else d
    genres = [x.strip().lower() for x in (f.get("genre") or "").split(",") if x.strip()]
    xgenres = [x.strip().lower() for x in (f.get("xgenre") or "").split(",") if x.strip()]
    mn, rtmin, mcmin = num("min"), num("rt"), num("mc")
    y0, y1 = int(num("y0")), int(num("y1"))
    added = num("added")
    fame = f.get("fame") or ""
    ol = (f.get("ol") or "").lower()
    ols = [x for x in ol.split(",") if x and x != "foreign"]
    q = norm_title(f.get("q") or "")
    k4 = f.get("k4") in ("1", "true", 1, True)
    show_seen = f.get("seen") in ("1", "true", 1, True)
    shelf = f.get("shelf") or ""
    shelf_fn = None
    if shelf:
        d = next((d for d in browse_shelves_def(kind, p) if d[0] == shelf), None)
        if d:
            shelf_fn = d[3]
    seen = set() if show_seen else disc_seen(p["id"], kind)
    now = time.time()
    hits = []
    for x in idx_.values():
        g = x["g"]
        if g["key"] in seen or not vod_visible(g):
            continue
        if q and q not in g["norm"]:
            continue
        if shelf_fn and not shelf_fn(x):
            continue
        if genres or xgenres:
            gl = [gn.lower() for gn in x["genres"]]
            if genres and not all(gn in gl for gn in genres):
                continue
            if xgenres and any(gn in gl for gn in xgenres):
                continue
        r = disc_rating(x)
        if mn and r < mn:
            continue
        if rtmin and (x.get("rt") is None or x["rt"] < rtmin):
            continue
        if mcmin and (x.get("mc") is None or x["mc"] < mcmin):
            continue
        yr = g["year"] or x.get("ty") or 0
        if (y0 and yr < y0) or (y1 and (not yr or yr > y1)):
            continue
        if added and now - g["added"] > added * 86400:
            continue
        if k4 and not x["has4k"]:
            continue
        if ol:
            if not x.get("ol"):
                continue
            if "foreign" in ol.split(",") and x["ol"] == "en":
                continue
            if ols and x["ol"] not in ols:
                continue
        if not _disc_len(x, kind, f.get("len") if kind == "movie" else f.get("seasons"), f.get("ep") if kind == "series" else ""):
            continue
        fm = disc_fame(x)
        if fame == "hidden" and not (fm and fm <= (25000 if kind == "movie" else 12000)):
            continue
        if fame == "known" and not (fm and fm >= (150000 if kind == "movie" else 60000)):
            continue
        if fame == "middle" and not (fm and (25000 if kind == "movie" else 12000) < fm < (150000 if kind == "movie" else 60000)):
            continue
        gem = None
        if sort == "gem":
            gem = disc_gem(x)
            if gem is None:
                continue
        hits.append((x, gem))
    key_fn = {
        "best": lambda h: -disc_best(h[0]),
        "gem": lambda h: -h[1],
        "imdb": lambda h: (h[0].get("imdb") is None, -(h[0].get("imdb") or disc_rating(h[0]))),
        "rt": lambda h: (h[0].get("rt") is None, -(h[0].get("rt") or 0), -disc_rating(h[0])),
        "mc": lambda h: (h[0].get("mc") is None, -(h[0].get("mc") or 0), -disc_rating(h[0])),
        "popular": lambda h: -(h[0].get("pop") or 0),
        "votes": lambda h: -(disc_fame(h[0]) or 0),
        "new": lambda h: -h[0]["g"]["added"],
        "year": lambda h: (-(h[0]["g"]["year"] or h[0].get("ty") or 0), -disc_rating(h[0])),
        "old": lambda h: ((h[0]["g"]["year"] or h[0].get("ty") or 9999), -disc_rating(h[0])),
        "short": lambda h: (h[0].get("runtime") is None or (kind == "movie" and h[0]["runtime"] < 40), h[0].get("runtime") or 0, -disc_rating(h[0])),
        "long": lambda h: (h[0].get("runtime") is None, -(h[0].get("runtime") or 0), -disc_rating(h[0])),
        "az": lambda h: h[0]["g"]["norm"],
    }.get(sort)
    if sort == "random":
        # "Surprise me": a fresh shuffle every hour, but stable while scrolling; good titles only unless asked otherwise
        import random as _r
        hits = [h for h in hits if mn or disc_rating(h[0]) >= 6.8]
        rnd = _r.Random("%s|%d|%s" % (kind, int(now // 3600), sorted(f.items())))
        rnd.shuffle(hits)
    else:
        hits.sort(key=key_fn)
    page = hits[offset:offset + limit]
    metas = meta_cached_many([x["g"]["key"] for x, _ in page])
    items = []
    for x, _ in page:
        t = vod_tile(x["g"], disc_reason(x, sort), meta=metas.get(x["g"]["key"]) or {})
        items.append(t)
    vis = [x for x in idx_.values() if vod_visible(x["g"])]
    with_meta = sum(1 for x in vis if x["vote"] is not None)
    with_imdb = sum(1 for x in vis if x.get("imdb"))
    return {"kind": kind, "profile": p["id"], "sort": sort, "total": len(hits), "offset": offset, "items": items,
            "facets": disc_facets(kind, idx_), "with_meta": with_meta, "with_imdb": with_imdb, "all": len(vis),
            "hidden_seen": len(seen), "tmdb": bool(key("TMDB_API_KEY") or key("TMDB_READ_TOKEN"))}


# ------------------------------------------------------------------ taste ----
# Getting to know a profile's taste, and searching from it the way a record digger would: through people, not
# popularity. Evidence = anchors, Letterboxd likes/stars, finished titles, thumbs, and answers from the "Your taste"
# screen. From the evidence's TMDb credits come the profile's makers (directors / series creators: a maker scores
# the average of their 5 best titles, empty places count 25, plus 8 x log2(titles)), their circle (writers,
# cinematographers, composers who keep coming back) and labels (studios / distributors). Recommendations then come
# from separate routes (makers, circle, labels, new seasons) so one source never takes over; ratings only filter.
# Manual makers ("makers" in smaak.json or added on the screen) always count. Nothing here is personal: names come
# from the local smaak.json / settings and the profile's own history.
TASTE = {}
TASTE_LOCK = threading.Lock()
TASTE_BUSY = set()
TASTE_TTL = 6 * 3600
ANS_SCORE = {"love": 100, "good": 75, "meh": 40, "no": 5}
CORE_JOBS = {"movie": ("Director",), "series": ("Creator",)}
RING_JOBS = {"Screenplay": "Written by", "Writer": "Written by", "Director of Photography": "Shot by"}   # composers/editors work on too much to say much
MAJOR_STUDIOS = {"warner bros. pictures", "universal pictures", "columbia pictures", "paramount pictures", "walt disney pictures",
                 "20th century fox", "20th century studios", "metro-goldwyn-mayer", "new line cinema", "netflix", "amazon mgm studios",
                 "sony pictures", "lionsgate", "dreamworks pictures", "legendary pictures", "relativity media", "touchstone pictures",
                 "canal+", "ciné+", "studiocanal", "bbc film", "film4 productions", "ingenious media", "dune entertainment", "tsg entertainment",
                 "ratpac-dune entertainment", "village roadshow pictures", "heyday films", "syncopy", "amblin entertainment",
                 # Japanese production-committee members finance almost everything, so they say little about taste
                 "dentsu", "pony canyon", "hakuhodo", "hakuhodo dy media partners", "nippon television network", "mainichi broadcasting system",
                 "tokyo broadcasting system", "fuji television network", "kodansha", "shueisha", "shogakukan", "tohokushinsha film", "bandai visual",
                 "avex pictures", "tv asahi", "tv tokyo", "kadokawa", "toho", "sony music entertainment japan", "lawson entertainment", "dentsu inc."}


def taste_db():
    conn = meta_db()
    conn.execute("CREATE TABLE IF NOT EXISTS taste_ans (profile TEXT, k TEXT, ans TEXT, t INTEGER, PRIMARY KEY(profile, k))")
    conn.execute("CREATE TABLE IF NOT EXISTS taste_makers (profile TEXT, pid INTEGER, name TEXT, state TEXT, t INTEGER, PRIMARY KEY(profile, pid))")
    return conn


def taste_answers(pid):
    conn = taste_db()
    try:
        return {k: (a, t) for k, a, t in conn.execute("SELECT k, ans, t FROM taste_ans WHERE profile=?", (pid,))}
    finally:
        conn.close()


def taste_answer(pid, gk, ans):
    pid = profile_id(pid)
    conn = taste_db()
    try:
        if ans in ("", "clear", None):
            conn.execute("DELETE FROM taste_ans WHERE profile=? AND k=?", (pid, gk))
        elif ans in ("love", "good", "meh", "no", "unseen"):
            conn.execute("INSERT OR REPLACE INTO taste_ans (profile, k, ans, t) VALUES (?,?,?,?)", (pid, gk, ans, int(time.time())))
        else:
            raise RuntimeError("Unknown answer")
        conn.commit()
    finally:
        conn.close()
    taste_dirty(pid)
    return {"ok": True}


def taste_dirty(pid):
    with TASTE_LOCK:
        if pid in TASTE:
            TASTE[pid]["stale"] = True
    home_invalidate()


def _person_search(name):
    d = tmdb("/search/person", 90 * 86400, query=name, include_adult="false") or {}
    res = d.get("results") or []
    res.sort(key=lambda r: (r.get("known_for_department") not in ("Directing", "Writing"), -(r.get("popularity") or 0)))
    return res[0] if res else None


def taste_maker(pid, action, person_id=0, name=""):
    """add (by name or id) / off / on / remove a maker for a profile."""
    pid = profile_id(pid)
    if action == "add" and not person_id:
        r = _person_search(name or "")
        if not r:
            raise RuntimeError("TMDb does not know anyone called %s" % name)
        person_id, name = int(r["id"]), r.get("name") or name
    conn = taste_db()
    try:
        if action == "remove":
            conn.execute("DELETE FROM taste_makers WHERE profile=? AND pid=?", (pid, int(person_id)))
        else:
            state = {"add": "pin", "on": "pin", "off": "off"}.get(action)
            if not state:
                raise RuntimeError("Unknown action")
            conn.execute("INSERT OR REPLACE INTO taste_makers (profile, pid, name, state, t) VALUES (?,?,?,?,?)",
                         (pid, int(person_id), name or "", state, int(time.time())))
        conn.commit()
    finally:
        conn.close()
    taste_dirty(pid)
    return {"ok": True, "id": person_id, "name": name}


def _taste_manual(pid, p):
    """Makers named in smaak.json ("makers") plus the ones added or switched off on the taste screen."""
    pins, offs = {}, set()
    for nm in p.get("makers") or []:
        r = _person_search(nm)
        if r:
            pins[int(r["id"])] = r.get("name") or nm
    conn = taste_db()
    try:
        rows = conn.execute("SELECT pid, name, state FROM taste_makers WHERE profile=?", (pid,)).fetchall()
    finally:
        conn.close()
    for i, nm, st in rows:
        if st == "off":
            offs.add(int(i))
            pins.pop(int(i), None)
        else:
            pins[int(i)] = nm
    return pins, offs


def _evidence(pid, p):
    """[(kind, tmdb id, title, year, score, why, library key)]: what this profile has shown to like or not."""
    ev = {}
    prio = {}

    def add(kind, tid, title, year, score, why, gk, pr):
        if not tid:
            return
        k = (kind, int(tid))
        if k in prio and prio[k] > pr:
            return
        prio[k] = pr
        ev[k] = {"kind": kind, "tmdb": int(tid), "title": title, "year": year or 0, "score": score, "why": why, "key": gk}

    def tid_for(gk):
        m = meta_cached(gk) if gk else None
        return (m or {}).get("id") or 0

    pids = ("p1", "p2") if p.get("together") else (pid,)
    for a in p.get("anchors") or []:
        kind = a.get("kind") or "series"
        tid = a.get("tmdb")
        if not tid:
            r = tmdb_search(kind, a.get("title", ""), a.get("year") or 0)
            tid = r.get("id") if isinstance(r, dict) else 0
        g = vod_find(kind, a.get("title", ""), a.get("year") or 0)
        add(kind, tid, a.get("title", ""), a.get("year"), 90, "a favourite", g["key"] if g else None, 3)
    for pp in pids:
        if pp == "p1":
            for r in lb_data("p1"):
                rating = r["rating"] or 0
                if r.get("liked") or rating >= 3.5 or (rating and rating <= 2.5):
                    sc = 95 if rating >= 4.5 else 85 if r.get("liked") else 80 if rating >= 4 else 60 if rating >= 3.5 else 15
                    tid = r["tmdb"]
                    kind = "movie"
                    if not tid:
                        x = tmdb_search("movie", r["title"], r["year"] or 0)
                        if not x:
                            x = tmdb_search("series", r["title"], r["year"] or 0)
                            kind = "series" if x else "movie"
                        tid = x.get("id") if isinstance(x, dict) else 0
                    g = vod_find(kind, r["title"], r["year"] or 0)
                    why = ("%s★ on Letterboxd" % ("%g" % rating)) if rating else "liked on Letterboxd"
                    add(kind, tid, r["title"], r["year"], sc, why, g["key"] if g else None, 2)
        for k, v in feedback_all(pp).items():
            if v in ("up", "down"):
                g = vod_group(k)
                if g:
                    add(g["kind"], tid_for(k), g["title"], g["year"], 75 if v == "up" else 10, "👍" if v == "up" else "👎", k, 4)
        h = history_profile(pp)
        for k in h["done"] | h["dropped"]:
            g = vod_group(k)
            if g:
                add(g["kind"], tid_for(k), g["title"], g["year"], 70 if k in h["done"] else 15, "watched to the end" if k in h["done"] else "stopped early", k, 1)
    answer_pids = pids if p.get("together") else (pid,)
    for pp in answer_pids:
        for k, (a, _) in taste_answers(pp).items():
            if a in ANS_SCORE:
                g = vod_group(k)
                if g:
                    add(g["kind"], tid_for(k), g["title"], g["year"], ANS_SCORE[a], {"love": "you love it", "good": "you liked it", "meh": "meh", "no": "not for you"}[a], k, 5)
    return list(ev.values())


def _credits(kind, tid):
    """(core people, ring people, companies, genres, details) for one title, from cached TMDb calls."""
    path = "/%s/%d" % ("movie" if kind == "movie" else "tv", tid)
    d = tmdb(path, 30 * 86400, append_to_response="external_ids") or {}
    c = tmdb(path + "/credits", 30 * 86400) or {}
    core, ring = [], []
    if kind == "movie":
        for x in c.get("crew") or []:
            if x.get("job") == "Director":
                core.append((x["id"], x.get("name") or ""))
            elif x.get("job") in RING_JOBS:
                ring.append((x["id"], x.get("name") or "", x["job"]))
    else:
        for x in d.get("created_by") or []:
            core.append((x["id"], x.get("name") or ""))
        for x in c.get("crew") or []:
            if x.get("job") in ("Director of Photography",):
                ring.append((x["id"], x.get("name") or "", x["job"]))
    comps = [(x["id"], x.get("name") or "") for x in (d.get("production_companies") or []) if (x.get("name") or "").lower() not in MAJOR_STUDIOS]
    gens = genre_names([g["id"] for g in d.get("genres") or []])
    return core, ring, comps, gens, d


def _people_score(titles):
    """titles: [(score, title)] -> maker score: average of the best five (empty places count 25) + 8 x log2(number of good titles)."""
    top = sorted((s for s, _ in titles), reverse=True)[:5]
    top += [25] * (5 - len(top))
    good = sum(1 for s, _ in titles if s >= 60)
    return round(sum(top) / 5.0 + (8 * math.log2(good) if good else 0), 1)


def taste_build(pid):
    p = profile(pid)
    pid = p["id"]
    pins, offs = _taste_manual(pid, p)
    ev = _evidence(pid, p)
    makers, ring, labels, genres = {}, {}, {}, {}
    for e in ev:
        try:
            core, rg, comps, gens, d = _credits(e["kind"], e["tmdb"])
        except Exception:  # noqa
            continue
        e["poster"] = d.get("poster_path") or ""
        e["title"] = d.get("title") or d.get("name") or e["title"]
        w = (e["score"] - 50) / 50.0
        for gname in gens:
            genres[gname] = genres.get(gname, 0) + w
        for i, nm in core:
            mk = makers.setdefault(i, {"id": i, "name": nm, "titles": [], "kinds": set()})
            mk["titles"].append((e["score"], e["title"]))
            mk["kinds"].add(e["kind"])
        for i, nm, job in rg:
            r = ring.setdefault(i, {"id": i, "name": nm, "job": job, "titles": []})
            if e["title"] not in [t for _, t in r["titles"]]:
                r["titles"].append((e["score"], e["title"]))
        if e["score"] >= 60:
            for i, nm in comps:
                lb_ = labels.setdefault(i, {"id": i, "name": nm, "titles": []})
                lb_["titles"].append(e["title"])
    for i, nm in pins.items():
        makers.setdefault(i, {"id": i, "name": nm, "titles": [], "kinds": set()})
    out_makers = []
    for i, mk in makers.items():
        if i in offs:
            continue
        sc = _people_score(mk["titles"])
        pinned = i in pins
        if pinned:
            sc = max(sc, 80)
        good = [t for s, t in sorted(mk["titles"], reverse=True) if s >= 60]
        bad = [t for s, t in mk["titles"] if s < 35]
        out_makers.append({"id": i, "name": mk["name"], "score": sc, "pinned": pinned, "titles": good[:6], "against": bad[:3],
                           "core": pinned or (len(good) >= 2 and sc >= 55), "kinds": sorted(mk["kinds"])})
    out_makers.sort(key=lambda m: (-m["core"], -m["score"], m["name"]))
    core_ids = {m["id"] for m in out_makers if m["core"]}
    out_ring = []
    for i, r in ring.items():
        good = [t for s, t in r["titles"] if s >= 60]
        if len(good) >= 2 and i not in core_ids:
            out_ring.append({"id": i, "name": r["name"], "job": r["job"], "score": _people_score(r["titles"]), "titles": good[:4]})
    out_ring.sort(key=lambda r: -r["score"])
    out_labels = sorted(({"id": i, "name": l_["name"], "n": len(l_["titles"]), "titles": l_["titles"][:4]} for i, l_ in labels.items() if len(l_["titles"]) >= 3),
                        key=lambda l_: -l_["n"])
    gl = sorted(genres.items(), key=lambda kv: -kv[1])
    data = {"t": time.time(), "profile": pid, "name": p.get("name"), "makers": out_makers[:60], "ring": out_ring[:25], "labels": out_labels[:12],
            "genres_up": [{"name": n, "w": round(v, 1)} for n, v in gl if v >= 1][:10],
            "genres_down": [{"name": n, "w": round(v, 1)} for n, v in reversed(gl) if v <= -0.8][:6],
            "avoid": p.get("avoid_genres") or [], "exclude": (p.get("exclude") or [])[:8], "n_exclude": len(p.get("exclude") or []),
            "evidence": sorted(({"title": e["title"], "year": e["year"], "kind": e["kind"], "why": e["why"], "score": e["score"], "poster": e.get("poster", ""), "key": e["key"]} for e in ev),
                               key=lambda e: -e["score"]),
            "answers": len(taste_answers(pid))}
    data["routes"] = taste_routes(pid, p, data)
    with TASTE_LOCK:
        TASTE[pid] = data
        TASTE_BUSY.discard(pid)
    home_invalidate()
    return data


def taste_get(pid, wait=False):
    """Cached taste for a profile; rebuilt in the background when old or changed (wait=True builds now)."""
    pid = profile_id(pid)
    with TASTE_LOCK:
        d = TASTE.get(pid)
        need = not d or d.get("stale") or time.time() - d["t"] > TASTE_TTL
        busy = pid in TASTE_BUSY
        if need and not busy and not wait:
            TASTE_BUSY.add(pid)
    if need and wait and not busy:
        try:
            return taste_build(pid)
        finally:
            with TASTE_LOCK:
                TASTE_BUSY.discard(pid)
    if need and not busy:
        def run():
            try:
                taste_build(pid)
            except Exception as e:  # noqa
                print("taste build failed: %s" % e)
            finally:
                with TASTE_LOCK:
                    TASTE_BUSY.discard(pid)
        threading.Thread(target=run, daemon=True).start()
    return d


def _taste_seen(pid, p):
    pids = ("p1", "p2") if p.get("together") else (pid,)
    seen = set()
    for pp in pids:
        seen |= disc_seen(pp, "movie") | disc_seen(pp, "series")
        seen |= {k for k, (a, _) in taste_answers(pp).items() if a != "unseen"}
    with LOCK:
        for pos in STATE["positions"].values():
            if pos and pos.get("gkey") and pos.get("dur") and pos.get("t", 0) > 0.9 * pos["dur"]:
                seen.add(pos["gkey"])
    return seen


def _rating_of(m):
    if m.get("imdb_rating"):
        return float(m["imdb_rating"])
    v, n = m.get("vote") or 0, m.get("votes") or 0
    return (v * n + 6.5 * 120) / (n + 120) if v and n else 0.0


def _st(title):
    """Short form of a title for a reason line: 'The Lord of the Rings: The Return of the King' -> 'The Lord of the Rings'."""
    t = title or ""
    return t.split(":")[0].strip() if len(t) > 24 and ":" in t else t


def _short(name):
    parts = (name or "").split()
    return parts[-1] if len(parts) > 1 else name


def taste_routes(pid, p, data):
    """Tips per route: makers (incl. forgotten gems from the watchlist), circle, labels, new seasons. Each tip: library
    key, route, score, reason (max ~12 words, naming the evidence)."""
    seen = _taste_seen(pid, p)
    fb = feedback_all(pid)
    wk = globals().get("watchlist_keys")      # the profile's own watchlist (update 25); before that: in-app watchlist marks
    watch = set(wk(pid)) if wk else {k for k, v in fb.items() if v in ("watchlist", "together")}
    lbw = set()
    tips = {}
    ev_ids = {(e["kind"], e.get("tmdb")) for e in data["evidence"]} | {e.get("key") for e in data["evidence"]}
    ev_tmdb = {e.get("tmdb") for e in data["evidence"] if e.get("tmdb")}
    unknown = []

    def consider(route, kind, title, year, orig, pts, reason, need=6.3, strict=False):
        g = vod_find(kind, title, year or 0, orig or "")
        if not g or g["key"] in seen or g["key"] in ev_ids or not vod_visible(g):
            return
        m = meta_cached(g["key"]) or {}
        if m.get("id") and m["id"] in ev_tmdb:
            return
        if _excluded(m, g, p, seen, fb):
            return
        if kind == "movie" and m.get("runtime") and m["runtime"] < 40:      # shorts
            return
        r = _rating_of(m) if m else 0
        if not r:
            unknown.append(g["key"])
            if strict:
                return
        if m and r and r < need:
            return
        old = tips.get(g["key"])
        score = pts + (r - 6.5) * 6 if r else pts - 4
        if g["key"] in watch or g["key"] in lbw:
            reason = "On your watchlist · " + reason
            score += 8
        if not old or old["score"] < score:
            tips[g["key"]] = {"key": g["key"], "route": route, "score": round(score, 1), "reason": reason}

    for mk in [m for m in data["makers"] if m["core"]][:20]:
        cr = tmdb("/person/%d/combined_credits" % mk["id"], 14 * 86400) or {}
        ev = mk["titles"][0] if mk["titles"] else ""
        for x in cr.get("crew") or []:
            kind = "movie" if x.get("media_type") == "movie" else "series"
            if x.get("job") != ("Director" if kind == "movie" else "Creator"):
                continue
            d = x.get("release_date") or x.get("first_air_date") or ""
            why = ("By %s · you loved %s" % (mk["name"], ev)) if ev else ("By %s, one of your %s" % (mk["name"], "creators" if kind == "series" and "series" in mk["kinds"] else "directors"))
            consider("makers", kind, x.get("title") or x.get("name") or "", int(d[:4]) if d[:4].isdigit() else 0,
                     x.get("original_title") or x.get("original_name"), 40 + mk["score"] * 0.5, why.replace(ev, _st(ev)) if ev else why, need=6.0)
    for rp in data["ring"][:12]:
        cr = tmdb("/person/%d/combined_credits" % rp["id"], 14 * 86400) or {}
        verb = RING_JOBS.get(rp["job"], "With")
        for x in cr.get("crew") or []:
            if x.get("job") != rp["job"]:
                continue
            kind = "movie" if x.get("media_type") == "movie" else "series"
            d = x.get("release_date") or x.get("first_air_date") or ""
            consider("circle", kind, x.get("title") or x.get("name") or "", int(d[:4]) if d[:4].isdigit() else 0,
                     x.get("original_title") or x.get("original_name"), 30 + rp["score"] * 0.4,
                     "%s %s, as in %s" % (verb, rp["name"], _st(rp["titles"][0])), need=6.8, strict=True)
    for lb_ in data["labels"][:6]:
        for kind, path in (("movie", "/discover/movie"), ("series", "/discover/tv")):
            dd = tmdb(path, 7 * 86400, with_companies=str(lb_["id"]), sort_by="vote_average.desc", **{"vote_count.gte": "150"}) or {}
            for x in (dd.get("results") or [])[:20]:
                d = x.get("release_date") or x.get("first_air_date") or ""
                consider("labels", kind, x.get("title") or x.get("name") or "", int(d[:4]) if d[:4].isdigit() else 0,
                         x.get("original_title") or x.get("original_name"), 28 + 2 * lb_["n"],
                         "%s, like %s" % (lb_["name"], " and ".join(_st(t) for t in lb_["titles"][:2])), need=6.8, strict=True)
    # new seasons of series this profile finished or loved
    for e in data["evidence"]:
        if e["kind"] != "series" or e["score"] < 60 or not e.get("key"):
            continue
        m = meta_cached(e["key"]) or {}
        la = m.get("last_air") or ""
        recent = la and time.time() - time.mktime(time.strptime(la[:10], "%Y-%m-%d")) < 120 * 86400 if re.match(r"\d{4}-\d\d-\d\d", la or "") else False
        if recent or m.get("next_ep"):
            tips[e["key"]] = {"key": e["key"], "route": "seasons", "score": 95, "reason": "New season of %s · %s" % (e["title"], e["why"])}
    if unknown:
        meta_enqueue(unknown[:120])          # ratings for these arrive in the background; the next build can use them
    out = {}
    for t in sorted(tips.values(), key=lambda t: -t["score"]):
        out.setdefault(t["route"], []).append(t)
    return {k: v[:60] for k, v in out.items()}


def taste_mix(pid, n=24):
    """For you, the taste way: about 60% makers + new seasons, 25% circle + labels; the caller adds TMDb picks and
    surprises. Returns tiles or [] when the taste is not known yet."""
    d = taste_get(pid)
    if not d:
        return []
    r = d.get("routes") or {}
    core = (r.get("seasons") or []) + (r.get("makers") or [])
    near = sorted((r.get("circle") or []) + (r.get("labels") or []), key=lambda t: -t["score"])
    picks, used, seen_makers = [], set(), {}
    want = [("core", round(n * 0.6)), ("near", round(n * 0.25))]
    for name, k in want:
        src = core if name == "core" else near
        for t in src:
            if len([x for x in picks if x[0] == name]) >= k:
                break
            if t["key"] in used:
                continue
            by = t["reason"].split(" · ")[0]
            if seen_makers.get(by, 0) >= 3:           # max three titles per maker in one row
                continue
            seen_makers[by] = seen_makers.get(by, 0) + 1
            used.add(t["key"])
            picks.append((name, t))
    tiles = []
    metas = meta_cached_many([t["key"] for _, t in picks])
    for _, t in picks:
        g = vod_group(t["key"])
        if g:
            tile = vod_tile(g, t["reason"], metas.get(t["key"]) or {})
            tile["route"] = t["route"]
            tiles.append(tile)
    return tiles


def taste_cards(pid, n=12):
    """Titles to rate on the taste screen, picked to teach the most: other work of makers we're not sure about yet,
    well-known titles across genres (likely seen, so they tell a lot) and a few hidden gems."""
    pid = profile_id(pid)
    p = profile(pid)
    d = taste_get(pid) or {}
    asked = set(taste_answers(pid).keys())
    seen_ev = {e.get("key") for e in d.get("evidence") or [] if e.get("key")}
    skip = asked | seen_ev | {k for k, v in feedback_all(pid).items() if v in ("up", "down", "seen")}
    rnd = __import__("random").Random("%s|%d|%d" % (pid, int(time.time() // 3600), len(asked)))
    cards = []

    def push(g, why):
        if g and g["key"] not in skip and vod_visible(g) and g["key"] not in {c["key"] for c in cards}:
            m = meta_cached(g["key"]) or {}
            if m.get("poster") and not _excluded(m, g, p, set(), {}):
                t = vod_tile(g, why, m)
                cards.append(t)
                return True
        return False
    # 1. the best-known work of your directors (probably seen: tells whether the tip is still new) and of makers we're
    #    not sure about yet (one title so far)
    for t in ((d.get("routes") or {}).get("makers") or [])[:30]:
        if len(cards) >= n * 0.3:
            break
        m_ = meta_cached(t["key"]) or {}
        if (m_.get("votes") or 0) >= 2000 and rnd.random() < 0.6:
            push(vod_group(t["key"]), t["reason"].split(" · ")[0].split(", one of")[0] + " · seen it?")
    unsure = [m for m in d.get("makers") or [] if not m["core"] and m["titles"]][:15]
    rnd.shuffle(unsure)
    for mk in unsure:
        if len(cards) >= n * 0.6:
            break
        cr = tmdb("/person/%d/combined_credits" % mk["id"], 14 * 86400) or {}
        cs = sorted([x for x in cr.get("crew") or [] if x.get("job") == ("Director" if x.get("media_type") == "movie" else "Creator")], key=lambda x: -(x.get("vote_count") or 0))
        for x in cs[:4]:
            kind = "movie" if x.get("media_type") == "movie" else "series"
            dd = x.get("release_date") or x.get("first_air_date") or ""
            if push(vod_find(kind, x.get("title") or x.get("name") or "", int(dd[:4]) if dd[:4].isdigit() else 0, x.get("original_title") or x.get("original_name") or ""),
                    "By %s, who made %s" % (mk["name"], mk["titles"][0])):
                break
    # 2. well-known titles across genres
    pool = []
    for kind in ("movie", "series"):
        for x in browse_index(kind).values():
            if (x.get("votes") or 0) >= (3000 if kind == "movie" else 800) and disc_rating(x) >= 6.6 and x["g"]["key"] not in skip:
                pool.append(x)
    rnd.shuffle(pool)
    by_genre = {}
    for x in pool:
        gname = (x["genres"] or ["?"])[0]
        if by_genre.get(gname, 0) >= 2:
            continue
        if push(x["g"], "Well known · %s" % gname):
            by_genre[gname] = by_genre.get(gname, 0) + 1
        if len(cards) >= n - 2:
            break
    # 3. a couple of hidden gems
    gems = [x for x in browse_index("movie").values() if disc_gem(x) is not None and x["g"]["key"] not in skip]
    rnd.shuffle(gems)
    for x in gems[:6]:
        if len(cards) >= n:
            break
        push(x["g"], "A hidden gem: seen it?")
    rnd.shuffle(cards)
    return {"profile": pid, "cards": cards[:n], "answers": len(asked), "building": d == {}}


def taste_summary(pid):
    d = taste_get(pid, wait=False)
    if not d:
        return {"profile": profile_id(pid), "building": True}
    out = {k: v for k, v in d.items() if k not in ("routes",)}
    out["routes"] = {k: len(v) for k, v in (d.get("routes") or {}).items()}
    out["evidence"] = d["evidence"][:80]
    out["n_evidence"] = len(d["evidence"])
    out["makers"] = [m for m in d["makers"] if m["core"]] + [m for m in d["makers"] if not m["core"]][:16]
    out["building"] = profile_id(pid) in TASTE_BUSY
    return out


# --------------------------------------------------------------- handler ----
# ---------------------------------------------------------- phone access ----
TAKEN = {}            # device id -> {"by": name, "t": time} : its stream was replaced by another device's
DIRECT = {}           # device id -> {"n": open direct (mp4) streams, "stop": generation}
PAIR_FAILS = []       # times of wrong pairing codes (rate limit)
# ---------------------------------------------------------------------------
# TV remote: "Play on TV" from a phone or the Mac, plus TV buttons (power, volume, inputs, apps).
# The Beeldbuis TV app (paired like a phone) long-polls /api/remote/poll for commands. Buttons for the
# TV itself go straight to the Samsung TV's own local remote API (websocket on port 8001, no install needed).
REMOTE = {"seq": 0, "cmds": [], "tv_seen": 0, "tv_ip": "", "tv_dev": "", "tv_now": None}
REMOTE_COND = threading.Condition()
TV_APP_ID = "BeeldBuis0.Beeldbuis"
TV_APPS = {"beeldbuis": TV_APP_ID, "netflix": "11101200001", "youtube": "111299001912",
           "prime": "3201512006785", "disney": "3201901017640", "spotify": "3201606009684"}
TV_KEYS = {"volup": "KEY_VOLUP", "voldown": "KEY_VOLDOWN", "mute": "KEY_MUTE", "up": "KEY_UP", "down": "KEY_DOWN",
           "left": "KEY_LEFT", "right": "KEY_RIGHT", "ok": "KEY_ENTER", "back": "KEY_RETURN", "home": "KEY_HOME",
           "source": "KEY_SOURCE", "playpause": "KEY_PLAY", "pause": "KEY_PAUSE", "chup": "KEY_CHUP", "chdown": "KEY_CHDOWN"}


def remote_settings():
    with LOCK:
        s = STATE["settings"]
        ip = (s.get("tv_ip") or "").strip()
        inputs = s.get("tv_inputs")
        mac = s.get("tv_mac") or ""
        auto = s.get("tv_ip_auto") or ""
    if inputs is None:                       # personal defaults may come with smaak.json ("tv_inputs")
        try:
            with open(TASTE_PATH, "r", encoding="utf-8") as f:
                inputs = json.load(f).get("tv_inputs") or []
        except (OSError, ValueError):
            inputs = []
    return {"ip": ip or REMOTE["tv_ip"] or auto, "inputs": [i for i in inputs if isinstance(i, dict) and i.get("name")], "mac": mac}


def _ws_frame(text):
    data = text.encode("utf-8")
    head = bytearray([0x81])
    n = len(data)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126); head += n.to_bytes(2, "big")
    else:
        head.append(0x80 | 127); head += n.to_bytes(8, "big")
    mask = os.urandom(4)
    return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def _ws_read(sock):
    def need(n):
        buf = b""
        while len(buf) < n:
            part = sock.recv(n - len(buf))
            if not part:
                raise OSError("closed")
            buf += part
        return buf
    b1, b2 = need(2)
    n = b2 & 0x7F
    if n == 126:
        n = int.from_bytes(need(2), "big")
    elif n == 127:
        n = int.from_bytes(need(8), "big")
    if b2 & 0x80:
        need(4)
    return need(n).decode("utf-8", "replace")


def tv_ws(messages, gap=0.35):
    """Send messages (dicts, or ('sleep', s)) to the Samsung TV's remote channel."""
    ip = remote_settings()["ip"]
    if not ip:
        raise RuntimeError("Beeldbuis does not know your TV yet. Open the Beeldbuis app on the TV once, or fill in the TV address in Settings.")
    import base64
    try:
        sock = socket.create_connection((ip, 8001), timeout=4)
    except OSError as e:
        if getattr(e, "errno", None) in (65, 1, 13) and sys.platform == "darwin":   # EHOSTUNREACH/EPERM: macOS Local Network privacy
            raise RuntimeError("macOS does not let Beeldbuis reach the TV yet: System Settings → Privacy & Security → Local Network → switch on Python (or Beeldbuis).")
        raise RuntimeError("The TV does not answer. Is it on and connected to the network?")
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        path = "/api/v2/channels/samsung.remote.control?name=" + base64.b64encode(b"Beeldbuis").decode()
        sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:8001\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\nOrigin: http://localhost\r\n\r\n" % (path, ip, key)).encode())
        head = b""
        while b"\r\n\r\n" not in head:
            part = sock.recv(1024)
            if not part:
                break
            head += part
        if b" 101" not in head.split(b"\r\n", 1)[0]:
            raise RuntimeError("The TV refused the remote connection.")
        sock.settimeout(6)
        try:
            first = _ws_read(sock)
            if "unauthorized" in first:
                raise RuntimeError("The TV asks for permission: choose Allow on the TV screen, then try again.")
        except socket.timeout:
            pass
        for m in messages:
            if isinstance(m, tuple):
                time.sleep(m[1]); continue
            sock.sendall(_ws_frame(json.dumps(m)))
            time.sleep(gap)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def tv_key_msg(key):
    return {"method": "ms.remote.control", "params": {"Cmd": "Click", "DataOfCmd": key, "Option": "false", "TypeOfRemote": "SendRemoteKey"}}


def tv_info():
    ip = remote_settings()["ip"]
    if not ip:
        return None
    try:
        with urllib.request.urlopen("http://%s:8001/api/v2/" % ip, timeout=2) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa
        return None


def tv_is_on():
    info = tv_info()
    if not info:
        return False
    return ((info.get("device") or {}).get("PowerState") or "on").lower() == "on"


def tv_wake():
    """Wake-on-LAN for a TV in standby (needs 'Power on with mobile' on Samsung TVs)."""
    mac = remote_settings()["mac"]
    if not mac:
        raise RuntimeError("The TV is off and Beeldbuis does not know its network address yet. Switch it on once with the remote.")
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    pkt = b"\xff" * 6 + raw * 16
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ip = remote_settings()["ip"]
    for dst in ("255.255.255.255", ip.rsplit(".", 1)[0] + ".255" if ip else None):
        if dst:
            for port in (9, 7):
                try:
                    s.sendto(pkt, (dst, port))
                except OSError:
                    pass
    s.close()


def tv_remember_mac():
    info = tv_info()
    dev = (info or {}).get("device") or {}
    mac = dev.get("wifiMac") or ""
    if mac and re.match(r"^[0-9A-Fa-f:]{17}$", mac):
        with LOCK:
            if STATE["settings"].get("tv_mac") != mac:
                STATE["settings"]["tv_mac"] = mac
                changed = True
            else:
                changed = False
        if changed:
            save_config()
    return info


def tv_launch(app):
    app_id = TV_APPS.get(app, app)
    ip = remote_settings()["ip"]
    try:                                          # REST launch works on 2016-2019 Samsung TVs
        req = urllib.request.Request("http://%s:8001/api/v2/applications/%s" % (ip, app_id), data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=4) as r:
            if r.status < 300:
                return
    except Exception:  # noqa
        pass
    tv_ws([{"method": "ms.channel.emit", "params": {"event": "ed.apps.launch", "to": "host",
            "data": {"appId": app_id, "action_type": "DEEP_LINK"}}}])


def tv_app_online():
    return time.time() - REMOTE["tv_seen"] < 30


def tv_power_on(wait=25):
    """True when the TV answers (after waking it if needed)."""
    if tv_is_on():
        return True
    standby = tv_info() is not None
    if standby:                       # newer TVs keep the network on in standby: the power key wakes them
        tv_ws([tv_key_msg("KEY_POWER")])
    else:
        tv_wake()
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(2)
        if tv_is_on():
            time.sleep(3)
            return True
    return False


def remote_push(cmd):
    with REMOTE_COND:
        REMOTE["seq"] += 1
        cmd = dict(cmd, seq=REMOTE["seq"], at=time.time())
        REMOTE["cmds"] = [c for c in REMOTE["cmds"] if time.time() - c["at"] < 120][-20:] + [cmd]
        REMOTE_COND.notify_all()
    return cmd["seq"]


def remote_poll(since, dev, ip, wait=20.0):
    known = REMOTE["tv_ip"]
    ip = (ip or "").replace("::ffff:", "")
    REMOTE["tv_seen"], REMOTE["tv_dev"] = time.time(), dev
    if ip and ip != known and not is_loopback(ip):
        REMOTE["tv_ip"] = ip
        with LOCK:
            STATE["settings"]["tv_ip_auto"] = ip
        save_config()
        threading.Thread(target=tv_remember_mac, daemon=True).start()
    deadline = time.time() + wait
    with REMOTE_COND:
        if since < 0:                  # first poll after the app starts: pick up a command from the last 90 s
            since = max([c["seq"] for c in REMOTE["cmds"] if time.time() - c["at"] > 90] or [0])
        while True:
            out = [c for c in REMOTE["cmds"] if c["seq"] > since]
            if out or time.time() >= deadline:
                REMOTE["tv_seen"] = time.time()
                return {"cmds": out, "seq": REMOTE["seq"] if not out else out[-1]["seq"]}
            REMOTE_COND.wait(max(0.5, deadline - time.time()))


def remote_input(name):
    inp = next((i for i in remote_settings()["inputs"] if i["name"].lower() == str(name).lower()), None)
    if not inp:
        raise RuntimeError("Unknown TV input '%s' (add it in Settings → TV remote)." % name)
    if inp.get("key"):
        return tv_ws([tv_key_msg(inp["key"])])
    pos = int(inp.get("pos", 0))
    msgs = [tv_key_msg("KEY_SOURCE"), ("sleep", 1.8)] + [tv_key_msg("KEY_LEFT")] * 14 + [("sleep", 0.4)]
    msgs += [tv_key_msg("KEY_RIGHT")] * pos + [("sleep", 0.6), tv_key_msg("KEY_ENTER")]
    tv_ws(msgs, gap=0.25)


def remote_send(body, sender):
    a = (body or {}).get("action") or ""
    if a == "play":
        kind = body.get("kind")
        if kind not in ("live", "movie", "episode"):
            raise RuntimeError("This can't be sent to the TV")
        blocking = rec_blocking()
        if blocking:     # films and episodes also come straight from the provider on the TV: they would end the recording
            raise RuntimeError("Recording '%s' is using the connection right now (one connection)." % blocking["title"])
        cmd = {k: body.get(k) for k in ("kind", "id", "ext", "name", "icon", "t", "key", "group")}
        cmd["action"], cmd["from"] = "play", sender
        stop_all_sessions()                      # the TV takes over the one connection
        seq = remote_push(cmd)
        started = False
        if not tv_app_online():
            try:
                if tv_power_on():
                    tv_launch("beeldbuis"); started = True
            except Exception as e:  # noqa
                return {"seq": seq, "queued": True, "note": str(e)}
        return {"seq": seq, "tv_app": tv_app_online(), "launched": started}
    if a == "stop":
        return {"seq": remote_push({"action": "stop", "from": sender})}
    if a == "power":
        if tv_is_on():
            tv_ws([tv_key_msg("KEY_POWER")])
            return {"power": "off"}
        threading.Thread(target=tv_power_on, daemon=True).start()
        return {"power": "on"}
    if a == "key":
        k = TV_KEYS.get(body.get("key"))
        if not k:
            raise RuntimeError("Unknown key")
        tv_ws([tv_key_msg(k)] * max(1, min(10, int(body.get("times") or 1))), gap=0.2)
        return {}
    if a == "input":
        if not tv_power_on():
            raise RuntimeError("The TV does not answer.")
        remote_input(body.get("name"))
        return {}
    if a == "scene":
        return remote_scene(body.get("name"))
    if a == "scene-stop":
        SCENE_RUN["stop"] = True
        return {}
    if a == "app":
        if not tv_power_on():
            raise RuntimeError("The TV does not answer.")
        tv_launch(body.get("app") or "beeldbuis")
        return {}
    raise RuntimeError("Unknown remote action")


def remote_record(body):
    """Record from the TV: the programme that is on now (or next) on this stream, using the Mac's guide."""
    sid = int(body.get("stream_id"))
    gk = CATALOG["by_stream"].get(sid)
    g = CATALOG["by_key"].get(gk) if isinstance(gk, str) else (gk if isinstance(gk, dict) else None)
    name = (g or {}).get("name") or body.get("name") or "Recording"
    now = int(time.time())
    prog = None
    if body.get("start") and body.get("stop"):          # a programme picked in the TV guide
        prog = {"start": int(body["start"]), "stop": int(body["stop"]), "title": body.get("title") or name, "description": body.get("description") or ""}
    elif g and g.get("epg"):
        nn = epg_now_next([g["epg"]]).get(g["epg"]) or {}
        prog = nn.get("next") if body.get("next") else nn.get("now")
    if prog:
        start, stop, title, desc = int(prog["start"]), int(prog["stop"]), prog.get("title") or name, prog.get("description") or ""
    else:
        start, stop, title, desc = now, now + 3600, name + " (1 hour)", ""
    return rec_schedule({"stream_id": sid, "start": start, "stop": stop, "title": title, "description": desc, "manual": not prog,
                         "channel": name, "group": (g or {}).get("key", ""), "epg": (g or {}).get("epg", ""), "icon": (g or {}).get("icon", "")})


# ---------------------------------------------------------- tv app (v28) ----
# The TV app asks the Mac for what only the Mac has: the full guide (also matched by channel name), guide search,
# subtitles from OpenSubtitles/Whisper, and the channel settings (favourites, hidden categories) so both stay equal.

def tv_group_out(g):
    """A channel group as the TV app builds it itself (groups.js), so it can play it right away."""
    return {"key": g["key"], "name": g["name"], "icon": g.get("icon") or "", "epg": g.get("epg") or "", "cats": list(g.get("cats") or []),
            "country": g.get("country") or "", "archive": g.get("archive") or 0, "best": g.get("best") or "",
            "variants": [{"id": v["id"], "q": v.get("q") or "", "rank": v.get("rank", 2), "cat": v.get("cat") or "", "name": v.get("name") or "",
                          "archive": v.get("archive") or 0, "epg": g.get("epg") or ""} for v in g.get("variants") or []]}


def tv_hidden_cats():
    with LOCK:
        return set(str(c) for c in STATE["settings"].get("hidden_cats") or [])


def tv_guide(ids, a, b):
    """Programmes per stream id between a and b: {stream_id: [{start, stop, title, description}]}."""
    now = int(time.time())
    a = int(a or now - 3600)
    b = int(b or now + 6 * 3600)
    if b - a > 2 * 86400:
        b = a + 2 * 86400
    want = {}
    for sid in ids[:80]:
        try:
            g = stream_group(int(sid))
        except (TypeError, ValueError):
            continue
        if g and g.get("epg"):
            want.setdefault(g["epg"], []).append((str(sid), g))
    rows = epg_rows(list(want), a, b) if want else {}
    missing = [e for e in want if a <= now <= b and not any(p["start"] <= now < p["stop"] for p in rows.get(e, []))]
    for e in missing:
        if not EPG_SID.get(e):
            g = want[e][0][1]
            EPG_SID[e] = [v["id"] for v in g["variants"] if v.get("epg")][:2] or [v["id"] for v in g["variants"]][:1]
    if missing:
        try:
            epg_short_fill(missing, wait=False, limit=12)
        except Exception:  # noqa
            pass
    out = {}
    for e, lst in want.items():
        for sid, _g in lst:
            out[sid] = rows.get(e, [])
    return out


def tv_search(q, limit=80):
    """Guide search for the TV: programmes with the channel they are on, now and later first, then catch-up."""
    progs = epg_search(q, 600)
    if not progs:
        return []
    hidden = tv_hidden_cats()
    by_epg = {}
    with CATALOG_LOCK:
        groups = list(CATALOG["groups"])
    for g in groups:
        if g.get("epg") and not (g.get("cats") and all(str(c) in hidden for c in g["cats"])):
            by_epg.setdefault(g["epg"], g)
    now = int(time.time())
    out, seen = [], set()
    for p in progs:
        g = by_epg.get(p["ch"])
        if not g:
            continue
        if p["stop"] < now and not (g.get("archive") and now - p["start"] < int(g["archive"]) * 86400):
            continue
        k = (g["key"], p["start"])
        if k in seen:
            continue
        seen.add(k)
        out.append({"title": p["title"], "start": p["start"], "stop": p["stop"], "description": p.get("description") or "",
                    "past": p["stop"] < now, "group": tv_group_out(g)})
    out.sort(key=lambda x: (x["past"], x["start"] if not x["past"] else -x["start"]))
    return out[:int(limit)]


def tv_channels():
    """The Mac's favourite channels (as full groups) and hidden categories."""
    with LOCK:
        favs = [k[3:] for k in STATE["favorites"] if k.startswith("ch:")]
    groups = []
    for k in favs:
        g = CATALOG["by_key"].get(k)
        if g:
            groups.append(tv_group_out(g))
    return {"favs": groups, "fav_keys": favs, "hidden": sorted(tv_hidden_cats()), "ready": bool(CATALOG["groups"])}


def tv_fav(body):
    key = str((body or {}).get("key") or "")
    if not key:
        raise RuntimeError("No channel")
    on = bool(body.get("on"))
    with LOCK:
        favs = [k for k in STATE["favorites"] if k != "ch:" + key]
        if on:
            favs.append("ch:" + key)
        STATE["favorites"] = favs
    save_config()
    tv_schedule("favourites")
    return {"key": key, "on": on}


def tv_subs(kind, sid, ext, series="", season=0, episode=0):
    """English subtitles for the TV when the file has none: {"url": "/subs/x.vtt"} or {"pending": True} while Whisper works."""
    if kind not in ("movie", "series") or not sid:
        raise RuntimeError("Invalid title")
    if kind == "series" and not episodes_get(sid) and series:
        title, year, _, _ = vod_parse(series)
        g = vod_group("series:%s|%d" % (norm_title(title), year))
        if not g:
            with VOD_LOCK:
                g = next((x for x in VOD["groups"].values() if x["kind"] == "series" and x.get("norm") == norm_title(title)), None)
        with EPISODES_LOCK:
            EPISODES[str(sid)] = {"series": title, "season": int(season or 0), "episode": int(episode or 0), "gkey": g["key"] if g else ""}
        episodes_save()
    path = external_subs_for(kind, sid, ext or "mp4")
    if path:
        return {"url": "/subs/" + os.path.basename(path)}
    with WHISPER_LOCK:
        keys = [j["key"] for j in WHISPER["queue"]] + ([WHISPER["job"]["key"]] if WHISPER["job"] else [])
    want = "ep:%s" % sid
    if kind == "movie":
        with VOD_LOCK:
            g = next((x for x in VOD["groups"].values() if x["kind"] == "movie" and any(v["id"] == int(sid) for v in x["variants"])), None)
        want = "movie:%s" % (g["key"] if g else "")
    pending = want in keys or subs_busy(kind, sid)
    return {"url": "", "pending": bool(pending)}


DEFAULT_SCENES = [
    {"name": "Movie night", "steps": ["on", "app:beeldbuis"]},
    {"name": "Sleep 30 min", "steps": ["fade:30", "off"]},
]
SCENE_RUN = {"name": None, "stop": False}


def remote_scenes():
    with LOCK:
        sc = STATE["settings"].get("tv_scenes")
    if sc is None:
        try:
            with open(TASTE_PATH, "r", encoding="utf-8") as f:
                sc = json.load(f).get("tv_scenes")
        except (OSError, ValueError):
            sc = None
    return [x for x in (sc if isinstance(sc, list) else DEFAULT_SCENES) if isinstance(x, dict) and x.get("name")]


def _scene_worker(scene):
    try:
        for st in scene.get("steps") or []:
            if SCENE_RUN["stop"]:
                break
            kind, _, arg = str(st).partition(":")
            if kind == "on":
                if not tv_power_on():
                    raise RuntimeError("TV did not switch on")
            elif kind == "off":
                if tv_is_on():
                    tv_ws([tv_key_msg("KEY_POWER")])
            elif kind == "input":
                remote_input(arg)
            elif kind == "app":
                tv_launch(arg or "beeldbuis")
            elif kind == "key":
                k, _, n = arg.partition("x")
                remote_send({"action": "key", "key": k, "times": int(n or 1)}, "scene")
            elif kind == "wait":
                time.sleep(float(arg or 1))
            elif kind == "channel":
                time.sleep(4)
                remote_channel(arg)
            elif kind == "fade":            # turn the volume down step by step over N minutes
                mins = max(1, int(float(arg or 30)))
                steps = 10
                for _ in range(steps):
                    end = time.time() + mins * 60 / steps
                    while time.time() < end:
                        if SCENE_RUN["stop"]:
                            return
                        time.sleep(2)
                    try:
                        tv_ws([tv_key_msg("KEY_VOLDOWN")] * 2, gap=0.2)
                    except Exception:  # noqa
                        return            # TV already off
            time.sleep(1.5)
    except Exception as e:  # noqa
        sys.stderr.write("scene %s: %s\n" % (scene.get("name"), e))
    finally:
        SCENE_RUN["name"] = None


def remote_scene(name):
    sc = next((x for x in remote_scenes() if x["name"].lower() == str(name).lower()), None)
    if not sc:
        raise RuntimeError("Unknown scene '%s'" % name)
    SCENE_RUN["stop"] = True
    time.sleep(0.3)
    SCENE_RUN.update(name=sc["name"], stop=False)
    threading.Thread(target=_scene_worker, args=(sc,), daemon=True).start()
    return {"scene": sc["name"]}


def remote_status():
    s = remote_settings()
    return {"tv_ip": s["ip"], "tv_app": tv_app_online(), "tv_seen": REMOTE["tv_seen"], "tv_dev": REMOTE["tv_dev"],
            "inputs": [{"name": i["name"], "pos": i.get("pos", 0), "key": i.get("key", "")} for i in s["inputs"]], "apps": [a for a in TV_APPS if a != "beeldbuis"],
            "now": REMOTE["tv_now"] if tv_app_online() else None, "scenes": [x["name"] for x in remote_scenes()], "scene": SCENE_RUN["name"]}


def remote_quick(a):
    """One-word commands for Siri Shortcuts: tv-on, tv-off, ps5, volup, netflix, beeldbuis, …"""
    a = (a or "").strip().lower()
    if a in ("tv-on", "on"):
        return {"ok": tv_power_on()}
    if a in ("tv-off", "off"):
        if tv_is_on():
            tv_ws([tv_key_msg("KEY_POWER")])
        return {}
    if a in ("tv-power", "power"):
        return remote_send({"action": "power"}, "siri")
    if a in TV_KEYS:
        return remote_send({"action": "key", "key": a}, "siri")
    if a in TV_APPS:
        return remote_send({"action": "app", "app": a}, "siri")
    if a == "stop":
        return remote_send({"action": "stop"}, "siri")
    if any(x["name"].lower() == a for x in remote_scenes()):
        return remote_scene(a)
    if any(i["name"].lower() == a for i in remote_settings()["inputs"]):
        return remote_send({"action": "input", "name": a}, "siri")
    raise RuntimeError("Unknown command '%s'" % a)


def remote_channel(name):
    """Siri: 'put <channel> on TV' — best match among the channels (Dutch and British first, Full HD first)."""
    want = re.sub(r"[^A-Z0-9]", "", re.sub(r"^(?:THE\s+)", "", str(name or "").upper()))
    if not want:
        raise RuntimeError("Which channel?")
    order = {"NL": 0, "UK": 1}
    cands = []
    for g in CATALOG["groups"]:
        k = re.sub(r"[^A-Z0-9]", "", re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", g.get("name", "").upper()))
        if k == want or k.startswith(want):
            cands.append((k != want, order.get(g.get("country"), 2), len(k), g))
    if not cands:
        raise RuntimeError("No channel called '%s'" % name)
    g = sorted(cands, key=lambda c: c[:3])[0][3]
    vs = g.get("variants") or []
    v = next((v for v in vs if v.get("q") == "FHD"), None) or next((v for v in vs if v.get("q") == "HD"), None) or (vs[0] if vs else None)
    if not v:
        raise RuntimeError("That channel has no source")
    return remote_send({"action": "play", "kind": "live", "id": v["id"], "ext": "ts", "name": g.get("name"),
                        "icon": g.get("icon", ""), "key": g.get("key")}, "siri")


# ---------------------------------------------------------------------------
# Second screen: what is on (TV or this device), matched to live sport data (ESPN's public scoreboards).
# Spoiler-free: while a recording, catch-up or anything not live is playing, scores stay hidden.
SPORT_BOARDS = [("soccer", "ned.1", "Eredivisie"), ("soccer", "ned.2", "Keuken Kampioen Divisie"), ("soccer", "ned.cup", "KNVB Beker"),
                ("soccer", "eng.1", "Premier League"), ("soccer", "esp.1", "LaLiga"), ("soccer", "ger.1", "Bundesliga"),
                ("soccer", "ita.1", "Serie A"), ("soccer", "fra.1", "Ligue 1"), ("soccer", "uefa.champions", "Champions League"),
                ("soccer", "uefa.europa", "Europa League"), ("soccer", "uefa.europa.conf", "Conference League"),
                ("soccer", "uefa.nations", "Nations League"), ("soccer", "fifa.world", "World Cup"), ("soccer", "uefa.euro", "EURO"),
                ("soccer", "fifa.worldq.uefa", "WC qualifying"), ("football", "nfl", "NFL"), ("racing", "f1", "Formula 1")]
SPORT_BOARDS_EXTRA = [("basketball", "nba", "NBA"), ("hockey", "nhl", "NHL"), ("football", "college-football", "College football")]
SPORT_BASE = os.environ.get("IPTV_SPORT_BASE", "https://site.api.espn.com/apis/site/v2/sports")
SPORT_CACHE = {}


def sport_get(path, ttl=25):
    hit = SPORT_CACHE.get(path)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        req = urllib.request.Request(SPORT_BASE + path, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8, context=API_SSL) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa
        data = hit[1] if hit else None
    SPORT_CACHE[path] = (time.time(), data)
    return data


def _sport_norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", s).strip() + " "


_TEAM_STOP = {"fc", "cf", "sc", "afc", "ac", "the", "club", "de", "city", "united", "real", "sv", "vv", "and"}


def _team_keys(t):
    keys = set()
    for f in ("displayName", "shortDisplayName", "name", "location", "abbreviation"):
        v = _sport_norm(t.get(f)).strip()
        if v and (len(v) > 3 or f == "abbreviation" and len(v) >= 3):
            keys.add(v)
        for w in v.split():
            if len(w) > 4 and w not in _TEAM_STOP:
                keys.add(w)
    return keys


def _team_in(text, t):
    return any((" " + k + " ") in text for k in _team_keys(t))


def sport_event(sport, league, lname, e):
    st = e.get("status") or {}
    stype = st.get("type") or {}
    comp = (e.get("competitions") or [{}])[0]
    out = {"id": e.get("id"), "sport": sport, "league": league, "league_name": lname, "name": e.get("name"), "start": e.get("date"),
           "state": stype.get("state"), "detail": stype.get("shortDetail") or stype.get("detail") or "", "clock": st.get("displayClock"),
           "period": st.get("period")}
    if sport == "racing":
        live = next((c for c in e.get("competitions") or [] if (c.get("status") or {}).get("type", {}).get("state") == "in"), None)
        last = live or next((c for c in reversed(e.get("competitions") or []) if (c.get("status") or {}).get("type", {}).get("state") == "post"), None)
        nxt = next((c for c in e.get("competitions") or [] if (c.get("status") or {}).get("type", {}).get("state") == "pre"), None)
        out["session"] = ((last or {}).get("type") or {}).get("abbreviation") or ""
        out["next_session"] = {"name": ((nxt or {}).get("type") or {}).get("abbreviation"), "start": (nxt or {}).get("date")} if nxt else None
        out["state"] = "in" if live else out["state"]
        out["order"] = [{"pos": c.get("order"), "name": (c.get("athlete") or {}).get("displayName"),
                         "flag": ((c.get("athlete") or {}).get("flag") or {}).get("href")} for c in (last or {}).get("competitors") or []][:20]
        return out
    teams = []
    for c in comp.get("competitors") or []:
        t = c.get("team") or {}
        teams.append({"id": t.get("id"), "name": t.get("displayName"), "short": t.get("shortDisplayName") or t.get("abbreviation"),
                      "logo": t.get("logo"), "home": c.get("homeAway") == "home", "score": c.get("score"), "_t": t})
    teams.sort(key=lambda x: not x["home"])
    out["teams"] = teams
    if sport == "soccer":
        out["events"] = [{"min": (d.get("clock") or {}).get("displayValue"), "type": (d.get("type") or {}).get("text"),
                          "team": (d.get("team") or {}).get("id"), "who": ", ".join(a.get("shortName") or a.get("displayName") or ""
                                                                              for a in d.get("athletesInvolved") or []),
                          "goal": bool(d.get("scoringPlay")), "red": bool(d.get("redCard")), "yellow": bool(d.get("yellowCard"))}
                         for d in comp.get("details") or []]
    if sport == "football":
        sit = comp.get("situation") or {}
        out["situation"] = sit.get("downDistanceText") or sit.get("shortDownDistanceText") or ""
        out["possession"] = sit.get("possession")
        out["last_play"] = (sit.get("lastPlay") or {}).get("text")
    return out


def sport_live_now():
    """Everything live (or about to start) across the boards: for 'live elsewhere' and matching."""
    out = []
    for sport, league, lname in SPORT_BOARDS:
        d = sport_get("/%s/%s/scoreboard" % (sport, league))
        for e in (d or {}).get("events") or []:
            out.append((sport, league, lname, e))
    return out


def companion_context(sid, kind):
    """The channel and programme the given stream is showing, from the Mac's guide."""
    ctx = {"kind": kind, "channel": "", "title": "", "desc": "", "start": 0, "stop": 0}
    try:
        sid = int(sid)
    except (TypeError, ValueError):
        return ctx
    gk = CATALOG["by_stream"].get(sid)
    g = CATALOG["by_key"].get(gk) if gk else None
    if not g:
        return ctx
    ctx["channel"] = re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", g.get("name", ""))
    if g.get("epg"):
        cur = (epg_now_next([g["epg"]]).get(g["epg"]) or {}).get("now")
        if cur:
            ctx.update(title=cur.get("title") or "", desc=cur.get("description") or "", start=cur.get("start", 0), stop=cur.get("stop", 0))
    return ctx


def companion(sid=None, kind=None, name=None):
    src = "this device"
    if not sid and tv_app_online() and REMOTE.get("tv_now") and REMOTE["tv_now"].get("name"):
        now = REMOTE["tv_now"]
        sid, kind, name, src = now.get("id"), now.get("kind"), now.get("name"), "TV"
        if not sid and kind == "live":            # TV app 0.8.0 reports only the channel name
            want = re.sub(r"[^A-Z0-9]", "", re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", str(name).upper()))
            g = next((g for g in CATALOG["groups"] if re.sub(r"[^A-Z0-9]", "", re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", g.get("name", "").upper())) == want), None)
            sid = g["variants"][0]["id"] if g and g.get("variants") else None
    live = kind == "live"
    ctx = companion_context(sid, kind) if live else {"kind": kind, "channel": "", "title": name or "", "desc": ""}
    text = _sport_norm(" ".join([ctx.get("title", ""), ctx.get("desc", ""), name or ""]))
    match, others = None, []
    for sport, league, lname, e in sport_live_now():
        ev = sport_event(sport, league, lname, e)
        if sport == "racing":
            hit = live and any(w in text for w in (" f1 ", " formula 1 ", " formule 1 ", " grand prix ", " grande premio "))
        else:
            ts = [t["_t"] for t in ev.get("teams") or []]
            n = sum(1 for t in ts if _team_in(text, t))
            hit = live and (n >= 2 or (n == 1 and ev["state"] == "in"))
        if hit and (not match or ev["state"] == "in"):
            match = ev
        elif ev["state"] == "in":
            others.append(ev)
    if match and match.get("sport") == "soccer" and match.get("id"):
        s = sport_get("/soccer/%s/summary?event=%s" % (match["league"], match["id"]), ttl=60) or {}
        match["lineups"] = [{"team": (r.get("team") or {}).get("displayName"), "formation": r.get("formation"),
                             "players": [{"name": (p.get("athlete") or {}).get("displayName"), "no": p.get("jersey"),
                                          "pos": (p.get("position") or {}).get("abbreviation")} for p in r.get("roster") or [] if p.get("starter")]}
                            for r in s.get("rosters") or []]
    for ev in [match] + others:
        if ev:
            for t in ev.get("teams") or []:
                t.pop("_t", None)
    return {"source": src, "context": ctx, "spoiler_safe": not live, "match": match, "live_elsewhere": others[:12],
            "updated": int(time.time())}


# ---------------------------------------------------------------------------
# English subtitles made on this Mac (Whisper on Apple Silicon via mlx-whisper), for films, episodes and
# recordings without an English subtitle track. Films and episodes need the provider's one connection to fetch
# the audio, so those jobs only run while nothing is playing or recording; recordings are local files.
WHISPER_DIR = os.path.join(CONFIG_DIR, "whisper")
WHISPER_PY = os.path.join(WHISPER_DIR, "venv", "bin", "python3")
WHISPER_MODEL = os.environ.get("IPTV_WHISPER_MODEL", "mlx-community/whisper-large-v3-turbo")
WHISPER_TRANSLATE_MODEL = os.environ.get("IPTV_WHISPER_TRANSLATE_MODEL", "mlx-community/whisper-large-v3-mlx")
WHISPER = {"state": "unknown", "msg": "", "queue": [], "job": None, "proc": None, "done": []}
WHISPER_LOCK = threading.Lock()
WHISPER_SCRIPT = r"""
import json, sys
import mlx_whisper
audio, out, model, tmodel, only_en = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5] == "1"
r = mlx_whisper.transcribe(audio, path_or_hf_repo=model, condition_on_previous_text=False)
lang = r.get("language") or ""
if lang != "en":
    if only_en:
        json.dump({"language": lang, "segments": []}, open(out, "w")); sys.exit(0)
    r = mlx_whisper.transcribe(audio, path_or_hf_repo=tmodel, task="translate", condition_on_previous_text=False)
json.dump({"language": lang, "segments": [{"start": s["start"], "end": s["end"], "text": s["text"]} for s in r.get("segments", [])]}, open(out, "w"))
"""


def whisper_ready():
    return (sys.platform == "darwin" or os.environ.get("IPTV_WHISPER_ANY")) and os.path.exists(WHISPER_PY) and os.path.exists(os.path.join(WHISPER_DIR, "ok"))


def whisper_status():
    with WHISPER_LOCK:
        st = WHISPER["state"]
        if st in ("unknown", "ready", "missing"):
            st = "ready" if whisper_ready() else ("unsupported" if sys.platform != "darwin" else "missing")
            WHISPER["state"] = st
        job = WHISPER["job"]
        return {"state": st, "msg": WHISPER["msg"], "queue": [j["title"] for j in WHISPER["queue"]],
                "job": job and {"title": job["title"], "step": job.get("step", "")}, "done": WHISPER["done"][-8:],
                "auto_recordings": STATE["settings"].get("whisper_recordings", True) is not False}


def whisper_install():
    with WHISPER_LOCK:
        if WHISPER["state"] == "installing":
            return whisper_status()
        WHISPER["state"], WHISPER["msg"] = "installing", "Preparing…"

    def run():
        try:
            os.makedirs(WHISPER_DIR, exist_ok=True)
            venv = os.path.join(WHISPER_DIR, "venv")
            steps = [([sys.executable, "-m", "venv", venv], "Making a private Python folder…"),
                     ([WHISPER_PY, "-m", "pip", "install", "--upgrade", "pip"], "Updating pip…"),
                     ([WHISPER_PY, "-m", "pip", "install", "mlx-whisper"], "Downloading Whisper (a few minutes)…")]
            for cmd, msg in steps:
                WHISPER["msg"] = msg
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                if r.returncode != 0:
                    raise RuntimeError((r.stderr or r.stdout or "failed")[-400:])
            with open(os.path.join(WHISPER_DIR, "ok"), "w") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M"))
            WHISPER["state"], WHISPER["msg"] = "ready", "Ready. The speech model (about 1.6 GB) downloads with the first job."
        except Exception as e:  # noqa
            WHISPER["state"], WHISPER["msg"] = "error", str(e)
            log_write("[whisper] install failed: %s\n" % e)
    threading.Thread(target=run, daemon=True).start()
    return whisper_status()


def whisper_enqueue(kind, sid, ext, title, cache_key, only_en=False, src_file=""):
    if not whisper_ready():
        return False
    with WHISPER_LOCK:
        if any(j["key"] == cache_key for j in WHISPER["queue"]) or (WHISPER["job"] and WHISPER["job"]["key"] == cache_key):
            return True
        WHISPER["queue"].append({"kind": kind, "sid": sid, "ext": ext, "title": title, "key": cache_key, "only_en": only_en, "file": src_file})
    return True


def whisper_vtt(segs):
    def ts(x):
        x = max(0.0, float(x)); h, rem = divmod(x, 3600); m, s = divmod(rem, 60)
        return "%02d:%02d:%06.3f" % (h, m, s)
    out = ["WEBVTT", ""]
    for s in segs:
        t = (s.get("text") or "").strip()
        if t:
            out += ["%s --> %s" % (ts(s["start"]), ts(s["end"])), t, ""]
    return "\n".join(out)


def whisper_line_busy():
    """True while something uses the provider connection (watching on any device, the TV app, a recording)."""
    with LOCK:
        busy = bool(SESSIONS)
    return busy or bool(rec_blocking()) or (tv_app_online() and bool((REMOTE.get("tv_now") or {}).get("name")))


def whisper_run(job):
    os.makedirs(os.path.join(WHISPER_DIR, "work"), exist_ok=True)
    audio = os.path.join(WHISPER_DIR, "work", "audio.m4a")
    out = os.path.join(WHISPER_DIR, "work", "out.json")
    for f in (audio, out):
        if os.path.exists(f):
            os.remove(f)
    src = job["file"] or stream_url(job["kind"], job["sid"], job["ext"] or "mp4")
    job["step"] = "fetching audio"
    p = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
                          "-c:a", "aac", "-b:a", "48k", audio], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    WHISPER["proc"] = p
    while p.poll() is None:
        time.sleep(2)
        if not job["file"] and whisper_line_busy():
            p.kill()
            raise InterruptedError()
    if p.returncode != 0 or not os.path.exists(audio):
        raise RuntimeError("Could not fetch the audio")
    job["step"] = "listening"
    p = subprocess.Popen([WHISPER_PY, "-c", WHISPER_SCRIPT, audio, out, WHISPER_MODEL, WHISPER_TRANSLATE_MODEL, "1" if job["only_en"] else "0"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    WHISPER["proc"] = p
    _, err = p.communicate()
    if p.returncode != 0:
        raise RuntimeError((err or b"").decode("utf-8", "replace")[-300:])
    data = json.load(open(out))
    if not data.get("segments"):
        return None
    os.makedirs(SUBS_DIR, exist_ok=True)
    path = os.path.join(SUBS_DIR, "wh_" + re.sub(r"[^a-z0-9]+", "_", job["key"].lower())[:80] + ".vtt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(whisper_vtt(data["segments"]))
    if not job["key"].startswith("rec:"):
        conn = meta_db()
        try:
            conn.execute("INSERT OR REPLACE INTO subs (k, file, src, t) VALUES (?,?,?,?)", (job["key"], path, "whisper", int(time.time())))
            conn.commit()
        finally:
            conn.close()
    return path


def whisper_worker():
    while True:
        time.sleep(10)
        try:
            with WHISPER_LOCK:
                job = next((j for j in WHISPER["queue"] if j["file"] or not whisper_line_busy()), None)
                if not job:
                    continue
                WHISPER["queue"].remove(job)
                WHISPER["job"] = job
            try:
                path = whisper_run(job)
                WHISPER["done"].append({"title": job["title"], "ok": bool(path), "t": int(time.time())})
            except InterruptedError:
                with WHISPER_LOCK:
                    job["step"] = ""
                    WHISPER["queue"].insert(0, job)          # try again when the connection is free
            except Exception as e:  # noqa
                log_write("[whisper] %s: %s\n" % (job["title"], e))
                WHISPER["done"].append({"title": job["title"], "ok": False, "error": str(e)[:200], "t": int(time.time())})
        except Exception as e:  # noqa
            log_write("[whisper] worker: %s\n" % e)
        finally:
            WHISPER["job"], WHISPER["proc"] = None, None


# ---------------------------------------------------------------------------
# DLNA / UPnP media server: TVs and players on the home network (LG, Philips, Hisense, Panasonic, Sony, VLC, ...) see
# "Beeldbuis" as a source with Recordings and Favourite channels. Off by default (Settings → Share on the network).
import uuid as _uuid
from xml.sax.saxutils import escape as _xesc
DLNA = {"uuid": None, "started": False}
SSDP_ADDR, SSDP_PORT = "239.255.255.250", 1900


def dlna_enabled():
    return STATE["settings"].get("dlna") is True


def dlna_uuid():
    if not DLNA["uuid"]:
        with LOCK:
            u = STATE["settings"].get("dlna_uuid")
            if not u:
                u = str(_uuid.uuid4())
                STATE["settings"]["dlna_uuid"] = u
        save_config()
        DLNA["uuid"] = u
    return DLNA["uuid"]


def dlna_ip_for(peer):
    """Our own address as seen from the asking device."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((peer, 9))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "127.0.0.1"


def dlna_location(peer):
    return "http://%s:%d/dlna/desc.xml" % (dlna_ip_for(peer), PORT)


DLNA_TYPES = ["upnp:rootdevice", "urn:schemas-upnp-org:device:MediaServer:1",
              "urn:schemas-upnp-org:service:ContentDirectory:1", "urn:schemas-upnp-org:service:ConnectionManager:1"]


def _ssdp_reply(st, peer):
    usn = "uuid:%s" % dlna_uuid() + ("" if st.startswith("uuid:") else "::" + st)
    return ("HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=1800\r\nEXT:\r\nLOCATION: %s\r\nSERVER: macOS UPnP/1.0 Beeldbuis/%d\r\n"
            "ST: %s\r\nUSN: %s\r\n\r\n" % (dlna_location(peer), VERSION, st, usn)).encode()


def dlna_ssdp():
    """Answers M-SEARCH and announces itself every few minutes while sharing is on."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        sock.bind(("", SSDP_PORT))
        mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton("0.0.0.0")
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(5)
    except OSError as e:
        log_write("[dlna] SSDP not available: %s\n" % e)
        return
    last_notify = 0
    while True:
        try:
            if dlna_enabled() and time.time() - last_notify > 300:
                last_notify = time.time()
                for st in ["uuid:" + dlna_uuid()] + DLNA_TYPES:
                    msg = ("NOTIFY * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nCACHE-CONTROL: max-age=1800\r\nLOCATION: %s\r\n"
                           "NT: %s\r\nNTS: ssdp:alive\r\nSERVER: macOS UPnP/1.0 Beeldbuis/%d\r\nUSN: uuid:%s%s\r\n\r\n"
                           % (dlna_location(SSDP_ADDR), st, VERSION, dlna_uuid(), "" if st.startswith("uuid:") else "::" + st)).encode()
                    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            time.sleep(5)
            continue
        if not dlna_enabled() or not data.startswith(b"M-SEARCH"):
            continue
        head = data.decode("utf-8", "replace")
        st = next((l.split(":", 1)[1].strip() for l in head.split("\r\n") if l.upper().startswith("ST:")), "")
        sts = DLNA_TYPES + ["uuid:" + dlna_uuid()] if st == "ssdp:all" else [st] if st in DLNA_TYPES or st == "uuid:" + dlna_uuid() else []
        for s in sts:
            try:
                sock.sendto(_ssdp_reply(s, addr[0]), addr)
            except OSError:
                pass


def dlna_desc(peer):
    base = "http://%s:%d" % (dlna_ip_for(peer), PORT)
    return ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:dlna="urn:schemas-dlna-org:device-1-0">'
            '<specVersion><major>1</major><minor>0</minor></specVersion><URLBase>%s</URLBase><device>'
            '<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><dlna:X_DLNADOC>DMS-1.50</dlna:X_DLNADOC>'
            '<friendlyName>Beeldbuis</friendlyName><manufacturer>Beeldbuis</manufacturer><modelName>Beeldbuis</modelName>'
            '<modelNumber>%d</modelNumber><UDN>uuid:%s</UDN>'
            '<iconList><icon><mimetype>image/png</mimetype><width>192</width><height>192</height><depth>24</depth><url>/assets/icon-192.png</url></icon></iconList>'
            '<serviceList>'
            '<service><serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType><serviceId>urn:upnp-org:serviceId:ContentDirectory</serviceId>'
            '<SCPDURL>/dlna/cd.xml</SCPDURL><controlURL>/dlna/cd/control</controlURL><eventSubURL>/dlna/cd/event</eventSubURL></service>'
            '<service><serviceType>urn:schemas-upnp-org:service:ConnectionManager:1</serviceType><serviceId>urn:upnp-org:serviceId:ConnectionManager</serviceId>'
            '<SCPDURL>/dlna/cm.xml</SCPDURL><controlURL>/dlna/cm/control</controlURL><eventSubURL>/dlna/cm/event</eventSubURL></service>'
            '</serviceList></device></root>') % (base, VERSION, dlna_uuid())


def _scpd(actions):
    out = ['<?xml version="1.0"?><scpd xmlns="urn:schemas-upnp-org:service-1-0"><specVersion><major>1</major><minor>0</minor></specVersion><actionList>']
    vars_ = {}
    for name, args in actions:
        out.append("<action><name>%s</name><argumentList>" % name)
        for a, d, v, t in args:
            out.append("<argument><name>%s</name><direction>%s</direction><relatedStateVariable>%s</relatedStateVariable></argument>" % (a, d, v))
            vars_[v] = t
        out.append("</argumentList></action>")
    out.append("</actionList><serviceStateTable>")
    for v, t in vars_.items():
        out.append('<stateVariable sendEvents="no"><name>%s</name><dataType>%s</dataType></stateVariable>' % (v, t))
    out.append("</serviceStateTable></scpd>")
    return "".join(out)


DLNA_CD_XML = _scpd([
    ("Browse", [("ObjectID", "in", "A_ARG_TYPE_ObjectID", "string"), ("BrowseFlag", "in", "A_ARG_TYPE_BrowseFlag", "string"),
                ("Filter", "in", "A_ARG_TYPE_Filter", "string"), ("StartingIndex", "in", "A_ARG_TYPE_Index", "ui4"),
                ("RequestedCount", "in", "A_ARG_TYPE_Count", "ui4"), ("SortCriteria", "in", "A_ARG_TYPE_SortCriteria", "string"),
                ("Result", "out", "A_ARG_TYPE_Result", "string"), ("NumberReturned", "out", "A_ARG_TYPE_Count", "ui4"),
                ("TotalMatches", "out", "A_ARG_TYPE_Count", "ui4"), ("UpdateID", "out", "A_ARG_TYPE_UpdateID", "ui4")]),
    ("GetSystemUpdateID", [("Id", "out", "SystemUpdateID", "ui4")]),
    ("GetSearchCapabilities", [("SearchCaps", "out", "SearchCapabilities", "string")]),
    ("GetSortCapabilities", [("SortCaps", "out", "SortCapabilities", "string")])])
DLNA_CM_XML = _scpd([("GetProtocolInfo", [("Source", "out", "SourceProtocolInfo", "string"), ("Sink", "out", "SinkProtocolInfo", "string")])])


def _soap(action, service, body):
    return ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body><u:%sResponse xmlns:u="urn:schemas-upnp-org:service:%s:1">%s'
            '</u:%sResponse></s:Body></s:Envelope>') % (action, service, body, action)


def dlna_children(oid):
    """(containers, items) under an object id. Items: (id, title, url_path, mime, size, duration)."""
    if oid == "0":
        return [("rec", "Recordings"), ("fav", "Favourite channels")], []
    if oid == "rec":
        items = []
        for r in rec_list()["items"]:
            if r["status"] in ("done", "recording") and r.get("file") and os.path.exists(r["file"]):
                ext = r["file"].rsplit(".", 1)[-1]
                items.append(("rec/" + r["id"], "%s · %s" % (r["title"], time.strftime("%a %d %b %H:%M", time.localtime(r["start"]))),
                              "/dlna/r/%s.%s" % (r["id"], ext), "video/mp4" if ext == "mp4" else "video/mpeg", r.get("size") or 0, r.get("duration") or 0))
        return [], items
    if oid == "fav":
        with LOCK:
            favs = list(STATE["favorites"])
        items = []
        for k in favs:
            g = CATALOG["by_key"].get(k)
            if not g or not g.get("variants"):
                continue
            v = next((v for v in g["variants"] if v.get("q") == "FHD"), None) or g["variants"][0]
            items.append(("fav/" + g["key"], re.sub(r"^[A-Z]{2,3}\s*[:|]\s*", "", g["name"]), "/dlna/l/%s.ts" % v["id"], "video/mpeg", 0, 0))
        return [], items
    return [], []


def dlna_didl(peer, oid, flag, start, count):
    base = "http://%s:%d" % (dlna_ip_for(peer), PORT)
    out = ['<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
           'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" xmlns:dlna="urn:schemas-dlna-org:metadata-1-0/">']
    def cont(cid, parent, title, n):
        return ('<container id="%s" parentID="%s" restricted="1" childCount="%d"><dc:title>%s</dc:title>'
                '<upnp:class>object.container.storageFolder</upnp:class></container>') % (_xesc(cid), parent, n, _xesc(title))
    def item(i, parent):
        iid, title, path, mime, size, dur = i
        d = ""
        if dur:
            d = ' duration="%d:%02d:%02d.000"' % (dur // 3600, dur // 60 % 60, dur % 60)
        flags = "DLNA.ORG_OP=01;DLNA.ORG_FLAGS=01700000000000000000000000000000" if size else "DLNA.ORG_OP=00;DLNA.ORG_FLAGS=8D700000000000000000000000000000"
        return ('<item id="%s" parentID="%s" restricted="1"><dc:title>%s</dc:title><upnp:class>object.item.videoItem</upnp:class>'
                '<res protocolInfo="http-get:*:%s:%s"%s%s>%s%s</res></item>') % (
            _xesc(iid), _xesc(parent), _xesc(title), mime, flags, ' size="%d"' % size if size else "", d, base, _xesc(path))
    if flag == "BrowseMetadata":
        if oid == "0":
            out.append('<container id="0" parentID="-1" restricted="1" childCount="2"><dc:title>Beeldbuis</dc:title><upnp:class>object.container.storageFolder</upnp:class></container>')
        elif oid in ("rec", "fav"):
            out.append(cont(oid, "0", "Recordings" if oid == "rec" else "Favourite channels", len(dlna_children(oid)[1])))
        else:
            parent = oid.split("/", 1)[0]
            it = next((i for i in dlna_children(parent)[1] if i[0] == oid), None)
            if it:
                out.append(item(it, parent))
        out.append("</DIDL-Lite>")
        return "".join(out), 1, 1
    conts, items = dlna_children(oid)
    entries = [cont(c, oid, t, len(dlna_children(c)[1])) for c, t in conts] + [item(i, oid) for i in items]
    total = len(entries)
    sel = entries[start:start + count] if count else entries[start:]
    out += sel
    out.append("</DIDL-Lite>")
    return "".join(out), len(sel), total


def dlna_control(peer, path, body, soapaction):
    action = (soapaction or "").strip('"').rsplit("#", 1)[-1]
    def arg(n):
        m = re.search(r"<%s[^>]*>(.*?)</%s>" % (n, n), body, re.S)
        return (m.group(1) if m else "").strip()
    if path.endswith("/cm/control"):
        if action == "GetProtocolInfo":
            return _soap(action, "ConnectionManager", "<Source>http-get:*:video/mpeg:*,http-get:*:video/mp4:*</Source><Sink></Sink>")
        return None
    if action == "Browse":
        res, n, total = dlna_didl(peer, arg("ObjectID") or "0", arg("BrowseFlag") or "BrowseDirectChildren",
                                  int(arg("StartingIndex") or 0), int(arg("RequestedCount") or 0))
        return _soap(action, "ContentDirectory", "<Result>%s</Result><NumberReturned>%d</NumberReturned><TotalMatches>%d</TotalMatches><UpdateID>1</UpdateID>"
                     % (_xesc(res), n, total))
    if action == "GetSystemUpdateID":
        return _soap(action, "ContentDirectory", "<Id>1</Id>")
    if action == "GetSearchCapabilities":
        return _soap(action, "ContentDirectory", "<SearchCaps></SearchCaps>")
    if action == "GetSortCapabilities":
        return _soap(action, "ContentDirectory", "<SortCaps></SortCaps>")
    return None


PUBLIC_GET = ("/", "/manifest.json", "/favicon.ico", "/icon-192.png", "/icon-512.png")
MAC_ONLY = ("/api/whisper/install", "/api/login", "/api/logout", "/api/quit", "/api/open-window", "/api/tv/choose-folder", "/api/tv/export",
            "/api/phone", "/api/phone/new-code", "/api/phone/forget")
_HOSTNAME = {"v": None}


def is_loopback(addr):
    a = (addr or "").lower()
    if a.startswith("::ffff:"):
        a = a[7:]
    return a == "::1" or a.startswith("127.")


def phone_enabled():
    with LOCK:
        return STATE["settings"].get("phone", True) is not False


def pair_code(new=False):
    with LOCK:
        c = STATE.get("pair_code") or ""
    if new or not c:
        c = "%06d" % secrets.randbelow(1000000)
        with LOCK:
            STATE["pair_code"] = c
        save_config()
    return c


def local_hostname():
    """Bonjour name of the Mac (e.g. My-MacBook.local): stays the same when the IP address changes."""
    if _HOSTNAME["v"] is None:
        name = ""
        if sys.platform == "darwin":
            try:
                name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True, timeout=3).stdout.strip()
            except Exception:  # noqa
                name = ""
        if not name:
            name = socket.gethostname().split(".")[0]
        _HOSTNAME["v"] = name + ".local" if name else ""
    return _HOSTNAME["v"]


def lan_addresses():
    """IPv4 addresses of this Mac: the Wi-Fi/LAN one and (if installed) Tailscale's 100.x address."""
    out = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))       # no packet is sent; this only picks the outgoing interface
        out.append({"ip": s.getsockname()[0], "kind": "wifi"})
        s.close()
    except OSError:
        pass
    try:
        txt = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        for ip in re.findall(r"\binet (\d+\.\d+\.\d+\.\d+)", txt):
            a, b = int(ip.split(".")[0]), int(ip.split(".")[1])
            if ip.startswith("127.") or any(x["ip"] == ip for x in out):
                continue
            if a == 100 and 64 <= b <= 127:
                out.append({"ip": ip, "kind": "tailscale"})
            elif a == 10 or (a == 192 and b == 168) or (a == 172 and 16 <= b <= 31):
                out.append({"ip": ip, "kind": "lan"})
    except Exception:  # noqa
        pass
    return out


def phone_info():
    with LOCK:
        devs = [{"id": k[:8], "name": v.get("name", "Device"), "added": v.get("added", 0), "seen": v.get("seen", 0)}
                for k, v in STATE["devices"].items()]
    host = local_hostname()
    addrs = lan_addresses()
    urls = (["http://%s:%d" % (host, PORT)] if host else []) + ["http://%s:%d" % (a["ip"], PORT) for a in addrs if a["kind"] != "tailscale"]
    ts = ["http://%s:%d" % (a["ip"], PORT) for a in addrs if a["kind"] == "tailscale"]
    return {"enabled": phone_enabled(), "code": pair_code(), "urls": urls, "tailscale": ts,
            "devices": sorted(devs, key=lambda d: -d["seen"])}


def device_name(ua):
    ua = ua or ""
    for k in ("iPad", "iPhone", "Android", "Macintosh", "Windows"):
        if k in ua:
            return {"Macintosh": "Mac or iPad", "Windows": "PC"}.get(k, k)
    return "Device"


def pair(code, name):
    now = time.time()
    PAIR_FAILS[:] = [t for t in PAIR_FAILS if now - t < 300]
    if len(PAIR_FAILS) >= 8:
        err = RuntimeError("Too many wrong codes. Wait five minutes and try again.")
        err.code = 429
        raise err
    if not code or not secrets.compare_digest(str(code).strip(), pair_code()):
        PAIR_FAILS.append(now)
        err = RuntimeError("That code is not right. Look in Beeldbuis on your Mac: Settings → iPhone & iPad.")
        err.code = 403
        raise err
    token = secrets.token_urlsafe(24)
    with LOCK:
        names = {v.get("name") for v in STATE["devices"].values()}
        base, n = (name or "Device")[:40], 2
        name = base
        while name in names:
            name = "%s (%d)" % (base, n)
            n += 1
        STATE["devices"][token] = {"name": name, "added": int(now), "seen": int(now)}
    save_config()
    return token, name


def device_for(token):
    if not token:
        return None
    with LOCK:
        d = STATE["devices"].get(token)
        if not d:
            return None
        now = int(time.time())
        dirty = now - d.get("seen", 0) > 600
        if dirty:
            d["seen"] = now
    if dirty:
        save_config()
    return d.get("name", "Device")


def forget_device(short_id):
    with LOCK:
        for k in list(STATE["devices"]):
            if k[:8] == short_id or k == short_id:
                STATE["devices"].pop(k, None)
    save_config()
    return True


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    def send_response(self, code, message=None):
        self._answered = True
        BaseHTTPRequestHandler.send_response(self, code, message)

    def _guarded(self, fn):
        """A bug in one request must not look like "the Mac is off": without an answer the window would show its
        offline screen. Log it and answer with an error the window can show."""
        self._answered = False
        try:
            fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass                                             # the player went away in the middle of a stream
        except Exception as e:  # noqa
            import traceback
            log_write("[error] %s %s: %s\n%s" % (self.command, redact(self.path.split("?")[0]), redact(str(e)), redact(traceback.format_exc()[-1500:])))
            if not self._answered:
                try:
                    self.send_json({"ok": False, "error": "Something went wrong in Beeldbuis (%s). Try again." % type(e).__name__}, 500)
                except Exception:  # noqa
                    pass

    def do_GET(self):
        self._guarded(self._get)

    def do_POST(self):
        self._guarded(self._post)

    # -- helpers
    def send_json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if len(body) > 50000 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html, cookie=None):
        body = html.encode("utf-8")
        self.send_response(200)
        if cookie:
            self.send_header("Set-Cookie", "bb_dev=%s; Path=/; Max-Age=315360000; HttpOnly; SameSite=Lax" % cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            body = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except ValueError:
            raise RuntimeError("This request could not be read")
        return body if isinstance(body, dict) else {}

    def api(self, fn):
        try:
            self.send_json({"ok": True, "data": fn()})
        except urllib.error.HTTPError as e:
            self.send_json({"ok": False, "error": "The server answered with HTTP %s" % e.code}, 502)
        except urllib.error.URLError as e:
            self.send_json({"ok": False, "error": "Cannot reach the server: %s" % e.reason}, 502)
        except Exception as e:  # noqa
            self.send_json({"ok": False, "error": str(e), "rec": getattr(e, "rec", None)}, getattr(e, "code", 500))

    # -- who is asking: the Mac itself, or a paired iPhone/iPad on the network
    def cookie_key(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "bb_dev":
                return v
        return ""

    def gate(self, path, qs, method="GET"):
        """True when the request may go on. Sets self.dev = (id, name) and self.remote."""
        self.dev, self.remote, self.key = ("mac", "Mac"), False, ""
        if is_loopback(self.client_address[0]):
            if not self.local_ok(path):
                self.send_json({"ok": False, "error": "Refused: this request did not come from the Beeldbuis window."}, 403)
                return False
            return True
        self.remote = True
        if path.startswith("/dlna/"):
            if not dlna_enabled():
                self.send_error(404)
                return False
            self.dev = ("dlna", "TV (DLNA)")
            return True
        if not phone_enabled():
            self.send_json({"ok": False, "error": "Phone access is switched off in Beeldbuis on the Mac (Settings → iPhone & iPad)."}, 403)
            return False
        key = self.headers.get("X-Beeldbuis-Key") or (qs.get("k") or [""])[0] or self.cookie_key()
        name = device_for(key)
        if name:
            self.dev, self.key = (key[:8], name), key
        if path in MAC_ONLY:
            self.send_json({"ok": False, "error": "This can only be done on the Mac."}, 403)
            return False
        if name or (method == "GET" and (path in PUBLIC_GET or path.startswith(("/assets/", "/hls/", "/subs/")))) or path == "/api/pair":
            return True    # /hls/<session>/ is reachable by its unguessable session id (native iOS playback sends no headers)
        self.send_json({"ok": False, "pair": True, "error": "This device is not connected to Beeldbuis yet."}, 401)
        return False

    def local_ok(self, path):
        """Requests from this Mac need no device key, so a web page open in a browser on the same Mac could otherwise
        talk to Beeldbuis behind the viewer's back (change settings, delete recordings, read the keys). Refuse what
        does not come from Beeldbuis' own window: another site's page (Origin / Sec-Fetch-Site) or a made-up name that
        points at this Mac (DNS rebinding: the Host is then a website's name, never an address or .local name)."""
        host = (self.headers.get("Host") or "").strip().lower()
        name = host[1:].split("]")[0] if host.startswith("[") else (host.rsplit(":", 1)[0] if host.count(":") == 1 else host)
        if name and not (name == "localhost" or name.endswith(".local") or ":" in name or re.match(r"^\d+\.\d+\.\d+\.\d+$", name)
                         or name == local_hostname().lower().rsplit(".local", 1)[0]):
            return False
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and (origin == "null" or urllib.parse.urlsplit(origin).netloc != host):
            return False
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site" and path != "/" and not path.startswith("/assets/") \
                and path not in PUBLIC_GET:
            return False
        return True

    def manifest(self):
        start = "/?k=%s" % urllib.parse.quote(self.key) if self.key else "/"   # the home-screen app starts paired
        body = json.dumps({"name": "Beeldbuis", "short_name": "Beeldbuis", "id": "/", "start_url": start, "scope": "/",
                           "display": "standalone", "background_color": "#0e0f12", "theme_color": "#0e0f12",
                           "icons": [{"src": "/assets/icon-180.png", "sizes": "180x180", "type": "image/png"},
                                     {"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                     {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/manifest+json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def client(self):
        return {"id": self.dev[0], "name": self.dev[1], "remote": self.remote}

    # -- routing
    def send_xml(self, text, status=200):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def dlna_get(self, path, qs):
        peer = self.client_address[0].replace("::ffff:", "")
        if path == "/dlna/desc.xml":
            return self.send_xml(dlna_desc(peer))
        if path == "/dlna/cd.xml":
            return self.send_xml(DLNA_CD_XML)
        if path == "/dlna/cm.xml":
            return self.send_xml(DLNA_CM_XML)
        if path.startswith("/dlna/r/"):
            return self.rec_file(path)
        if path.startswith("/dlna/l/"):
            return self.stream("/direct/live/" + path.rsplit("/", 1)[-1], qs, direct=True)
        self.send_error(404)

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path.startswith("/dlna/") and dlna_enabled():
            if parsed.path.startswith("/dlna/l/"):
                self.send_response(200); self.send_header("Content-Type", "video/mpeg"); self.end_headers(); return
            if parsed.path.startswith("/dlna/r/"):
                return self.rec_file(parsed.path)
            return self.dlna_get(parsed.path, {})
        self.send_error(405)

    def do_SUBSCRIBE(self):
        self.send_response(200)
        self.send_header("SID", "uuid:" + str(_uuid.uuid4()))
        self.send_header("TIMEOUT", "Second-1800")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_UNSUBSCRIBE(self):
        self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    def _get(self):
        LAST_REQ["t"] = time.time()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        q = lambda k, d=None: qs.get(k, [d])[0]  # noqa: E731
        if not self.gate(path, qs):
            return
        if path.startswith("/dlna/"):
            return self.dlna_get(path, qs)

        if path == "/":
            return self.send_html(HTML, cookie=self.key if self.remote and self.key else None)
        if path == "/manifest.json":
            return self.manifest()
        if path.startswith("/assets/") or path in ("/icon-192.png", "/icon-512.png", "/favicon.ico"):
            return self.asset(path)
        if path == "/api/ping":
            LAST_PING["t"] = time.time()
            now = time.time()
            taken = {k: v for k, v in TAKEN.items() if now - v["t"] < 600}
            out = {"ok": True, "ffmpeg": bool(FFMPEG), "v": VERSION, "rec": rec_status(), "now": now,
                   "taken": taken.get(self.dev[0]), "me": self.client()}
            if q("s"):
                # the player that shows this stream is still open (paused, or its buffer is full): keep the stream.
                # hls.js stops asking for segments then, and the janitor would remove a film someone paused for a drink
                sess = SESSIONS.get(q("s"))
                if sess and sess.get("owner") == self.dev[0]:
                    sess["t"] = now
                out["alive"] = bool(sess)
            return self.send_json(out)
        if path == "/api/open-window":
            return self.send_json({"ok": True, "window": open_window(), "v": VERSION})
        if path == "/api/status":
            return self.api(self.status)
        if path == "/api/live/categories":
            return self.api(lambda: xtream("get_live_categories"))
        if path == "/api/live/streams":
            return self.api(lambda: xtream("get_live_streams", category_id=q("cat")))
        if path == "/api/vod/categories":
            return self.api(lambda: xtream("get_vod_categories"))
        if path == "/api/vod/streams":
            return self.api(lambda: xtream("get_vod_streams", category_id=q("cat")))
        if path == "/api/series/categories":
            return self.api(lambda: xtream("get_series_categories"))
        if path == "/api/series/list":
            return self.api(lambda: xtream("get_series", category_id=q("cat")))
        if path == "/api/series/info":
            def _series_info():
                info = xtream("get_series_info", series_id=q("id"))
                try:
                    remember_episodes(((info or {}).get("info") or {}).get("name") or q("name") or "", q("id"), info or {})
                except Exception:  # noqa
                    pass
                return info
            return self.api(_series_info)
        if path == "/api/home":
            return self.api(lambda: dict(home_with_live(q("profile", "p1"), force=q("force") == "1"), img_base=TMDB_IMG,
                                           names={k: v.get("name") or k for k, v in profiles().items() if not k.startswith("_")}))
        if path == "/api/sport":
            return self.api(lambda: sport_overview(q("profile", "p1")))
        if path == "/api/shelves":
            return self.api(lambda: browse_rows(q("kind", "movie"), q("p", ""), q("shelf", ""), int(q("offset", "0") or 0), min(int(q("limit", "60") or 60), 200)))
        if path == "/api/taste":
            return self.api(lambda: taste_summary(q("p", "") or "p1"))
        if path == "/api/taste/cards":
            return self.api(lambda: taste_cards(q("p", "") or "p1", min(int(q("n", "12") or 12), 30)))
        if path == "/api/discover":
            return self.api(lambda: discover(q("kind", "movie"), q("p", ""), {k: q(k, "") for k in ("sort", "genre", "xgenre", "min", "rt", "mc", "y0", "y1",
                            "added", "fame", "ol", "q", "k4", "seen", "shelf", "len", "seasons", "ep")},
                            int(q("offset", "0") or 0), min(int(q("limit", "60") or 60), 300)))
        if path == "/api/library":
            return self.api(lambda: vod_list(q("kind", "movie"), q("cat", ""), q("q", ""), int(q("limit", "400") or 400), int(q("offset", "0") or 0), q("genre", "")))
        if path == "/api/ratings":
            return self.api(lambda: ratings_for([x for x in (q("keys") or "").split(",") if x], [x for x in (q("ids") or "").split(",") if x]))
        if path == "/api/item":
            return self.api(lambda: vod_item(q("key")))
        if path == "/api/meta":
            return self.api(lambda: (meta_get(vod_group(q("key")), fetch=True) if vod_group(q("key")) else None))
        if path == "/api/search":
            return self.api(lambda: search_all(q("q")))
        if path == "/api/profiles":
            return self.api(lambda: {k: v for k, v in profiles().items() if not k.startswith("_")})
        if path == "/api/keys":
            return self.api(lambda: (keys_load() and None) or keys_status())
        if path == "/api/letterboxd":
            return self.api(lb_status)
        if path == "/api/watchlist":
            return self.api(lambda: watchlist_api(profile_id(q("profile")), q("kind", "")))
        if path.startswith("/subs/"):
            return self.sub_file(path)
        if path == "/api/epg":
            return self.api(lambda: self.epg(q("id")))
        if path == "/api/channels":
            return self.api(self.channels)
        if path == "/api/epg/status":
            return self.send_json({"ok": True, "data": epg_status()})
        if path == "/api/epg/now":
            return self.api(lambda: epg_now_next((q("ids") or "").split(",")))
        if path == "/api/epg/range":
            return self.api(lambda: epg_rows((q("ids") or "").split(","), int(q("from", "0") or 0), int(q("to", "0") or 0)))
        if path == "/api/tv/guide":
            return self.api(lambda: tv_guide((q("ids") or "").split(","), int(q("from", "0") or 0), int(q("to", "0") or 0)))
        if path == "/api/tv/search":
            return self.api(lambda: tv_search(q("q"), int(q("limit", "80") or 80)))
        if path == "/api/tv/channels":
            return self.api(tv_channels)
        if path == "/api/tv/subs":
            return self.api(lambda: tv_subs(q("kind"), q("id"), q("ext"), q("series"), int(q("season", "0") or 0), int(q("episode", "0") or 0)))
        if path == "/api/epg/search":
            return self.api(lambda: epg_search(q("q"), int(q("limit", "300") or 300)))
        if path == "/api/settings":
            with LOCK:
                st = dict(STATE["settings"])
            return self.send_json({"ok": True, "data": st})
        if path == "/api/probe":
            return self.api(lambda: self.probe_api(q("kind"), q("id"), q("ext")))
        if path == "/api/favorites":
            with LOCK:
                favs = list(STATE["favorites"])
            return self.send_json({"ok": True, "data": favs})
        if path == "/api/play":
            return self.api(lambda: self.play_api(q("kind"), q("id"), q("ext"), float(q("t", "0") or 0), q("force") == "1",
                                                  int(q("start", "0") or 0), int(q("dur", "0") or 0), q("native") == "1"))
        if path == "/api/buffer":                # how much live TV is buffered for a session (pause/rewind on the TV)
            sess = SESSIONS.get(q("sid"))
            if not sess:
                return self.send_json({"ok": False, "error": "This stream has stopped"}, 404)
            return self.send_json({"ok": True, "data": buffer_info(sess)})
        if path == "/api/stop":
            stop_all_sessions(owner=self.dev[0])     # only this device's own stream
            return self.send_json({"ok": True})
        if path == "/api/phone":
            return self.api(phone_info)
        if path == "/api/recordings":
            return self.api(rec_list)
        if path == "/api/tv":
            return self.api(tv_status)
        if path == "/api/remote/poll":
            return self.api(lambda: remote_poll(int(q("since", "-1") or -1), self.dev[1], self.client_address[0]))
        if path == "/api/companion":
            return self.api(lambda: companion(q("sid"), q("kind"), q("name")))
        if path == "/api/whisper":
            return self.api(whisper_status)
        if path == "/api/remote/status":
            return self.api(remote_status)
        if path == "/api/remote/do":            # Siri Shortcuts: GET /api/remote/do?k=<key>&a=ps5  or  &channel=NPO 1
            return self.api(lambda: remote_channel(q("channel")) if q("channel") else remote_quick(q("a")))
        if path == "/api/positions":
            with LOCK:
                pos = dict(STATE["positions"])
            return self.send_json({"ok": True, "data": pos})
        if path.startswith("/direct/rec/"):
            return self.rec_file(path)
        if path.startswith("/hls/"):
            return self.hls_file(path, qs)
        if path.startswith("/direct/"):
            return self.stream(path, qs, direct=True)
        self.send_error(404)

    def _post(self):
        LAST_REQ["t"] = time.time()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not self.gate(path, urllib.parse.parse_qs(parsed.query), "POST"):
            return
        if path.startswith("/dlna/"):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n).decode("utf-8", "replace") if n else ""
            out = dlna_control(self.client_address[0].replace("::ffff:", ""), path, body, self.headers.get("SOAPACTION"))
            return self.send_xml(out) if out else self.send_error(500)
        if path == "/api/pair":
            return self.pair_api()
        if path == "/api/stop":                  # the window or tab closed (sendBeacon): free the provider connection now
            stop_all_sessions(owner=self.dev[0])
            gen = DIRECT.get(self.dev[0])
            if gen and gen.get("n"):
                gen["stop"] += 1
                direct_close(gen)
            return self.send_json({"ok": True})
        if path == "/api/phone/new-code":
            return self.api(lambda: {"code": pair_code(new=True)})
        if path == "/api/phone/forget":
            return self.api(lambda: forget_device(self.read_json_body().get("id", "")))
        if path == "/api/phone/leave":
            if self.key:
                forget_device(self.key)
            return self.send_json({"ok": True}, extra=[("Set-Cookie", "bb_dev=; Path=/; Max-Age=0; SameSite=Lax")])
        if path == "/api/login":
            return self.api(self.login)
        if path == "/api/logout":
            stop_all_sessions()
            with LOCK:
                STATE["server"] = STATE["username"] = STATE["password"] = ""
                STATE["account"] = {}
            with ACCOUNT_LOCK:
                ACCOUNT.update(t=0, ui=None, err=None)
            save_config()
            return self.send_json({"ok": True})
        if path == "/api/settings":
            body = self.read_json_body() or {}
            if body.get("vod_langs") == []:            # never hide every title
                body["vod_langs"] = None
            if body.get("profile") == "":
                body.pop("profile")
            with LOCK:
                STATE["settings"].update(body)
            save_config()
            tv_schedule("settings")
            return self.send_json({"ok": True})
        if path == "/api/whisper/install":
            return self.api(whisper_install)
        if path == "/api/remote/send":
            return self.api(lambda: remote_send(self.read_json_body(), self.dev[1]))
        if path == "/api/remote/record":
            return self.api(lambda: remote_record(self.read_json_body()))
        if path == "/api/remote/now":           # the TV app reports what it is playing
            now = self.read_json_body()
            REMOTE["tv_now"] = dict(now if isinstance(now, dict) else {}, at=time.time())   # 'at' lets the Mac see a fresh report
            return self.send_json({"ok": True})
        if path == "/api/tv/fav":
            return self.api(lambda: tv_fav(self.read_json_body()))
        if path == "/api/record":
            return self.api(lambda: rec_schedule(self.read_json_body()))
        if path == "/api/record/extend":
            body = self.read_json_body()
            return self.api(lambda: rec_extend(body.get("id"), body.get("minutes", 30)))
        if path == "/api/record/cancel":
            return self.api(lambda: rec_cancel(self.read_json_body().get("id")))
        if path == "/api/record/delete":
            return self.api(lambda: rec_delete(self.read_json_body().get("id")))
        if path == "/api/progress":
            body = self.read_json_body()
            key = body.get("key")
            if key:
                with LOCK:
                    if body.get("remove"):
                        hist_item = STATE["positions"].pop(key, None)
                    else:
                        item = dict(body.get("item") or {})
                        item["updated"] = int(time.time())
                        STATE["positions"][key] = item
                        hist_item = item
                        if len(STATE["positions"]) > 200:   # keep the list short
                            for k in sorted(STATE["positions"], key=lambda k: STATE["positions"][k].get("updated", 0))[:-200]:
                                STATE["positions"].pop(k, None)
                save_config()
                try:
                    history_note(body.get("profile") or (hist_item or {}).get("profile") or "p1", key, hist_item, removed=bool(body.get("remove")))
                except Exception:  # noqa
                    pass
            return self.send_json({"ok": True})
        if path == "/api/taste/answer":
            body = self.read_json_body()
            return self.api(lambda: taste_answer(body.get("p") or "p1", body.get("key") or "", body.get("ans")))
        if path == "/api/taste/maker":
            body = self.read_json_body()
            return self.api(lambda: taste_maker(body.get("p") or "p1", body.get("action") or "", int(body.get("id") or 0), body.get("name") or ""))
        if path == "/api/watchlist":
            body = self.read_json_body()
            return self.api(lambda: watchlist_set(profile_id(body.get("profile")), body.get("key") or "", bool(body.get("on"))))
        if path == "/api/feedback":
            body = self.read_json_body()
            feedback_set(profile_id(body.get("profile")), body.get("key") or "", body.get("verdict"))
            return self.send_json({"ok": True})
        if path == "/api/keys":
            body = self.read_json_body()
            with LOCK:
                ks = dict(STATE["settings"].get("keys") or {})
                for k, v in (body or {}).items():
                    k = str(k).upper()
                    if v is None or v == "":
                        ks.pop(k, None)
                    elif v != "••••":
                        ks[k] = str(v).strip()
                STATE["settings"]["keys"] = ks
            save_config()
            keys_load()
            home_invalidate()
            threading.Thread(target=lambda: (trending_refresh(force=True), lb_rss_refresh(force=True)), daemon=True).start()
            return self.send_json({"ok": True, "data": keys_status()})
        if path == "/api/profiles":
            body = self.read_json_body()
            with LOCK:
                cur = dict(STATE["settings"].get("profiles") or {})
                for pid, p in (body or {}).items():
                    if pid in DEFAULT_PROFILES and isinstance(p, dict):
                        cur[pid] = dict(cur.get(pid) or {}, **p)
                STATE["settings"]["profiles"] = cur
            save_config()
            home_invalidate()
            return self.send_json({"ok": True})
        if path == "/api/home/refresh":
            body = self.read_json_body()
            def _hr():
                keys_load()
                vod_refresh(force=True)
                trending_refresh(force=True)
                lb_rss_refresh(force=True)
                if os.path.exists(LB_ZIP):
                    lb_import_zip()
                return home_get((body or {}).get("profile") or "p1", force=True)
            return self.api(_hr)
        if path == "/api/letterboxd/upload":     # the export zip picked in the setup tour or Settings
            def lb_upload():
                n = int(self.headers.get("Content-Length") or 0)
                if not 0 < n < 80 * 1024 * 1024:
                    raise RuntimeError("That file is empty or too big for a Letterboxd export")
                data = self.rfile.read(n)
                if data[:2] != b"PK":
                    raise RuntimeError("That is not a zip file. Use the zip from letterboxd.com (Settings → Import & Export → Export your data).")
                tmp = LB_ZIP + ".new"
                with open(tmp, "wb") as f:
                    f.write(data)
                try:
                    count = lb_import_zip(tmp)
                except Exception:
                    os.remove(tmp)
                    raise RuntimeError("Could not read that zip. Is it the Letterboxd data export?")
                os.replace(tmp, LB_ZIP)
                home_invalidate()
                return {"n": count}
            return self.api(lb_upload)
        if path == "/api/letterboxd/import":
            return self.api(lambda: {"n": lb_import_zip()} if os.path.exists(LB_ZIP) else (_ for _ in ()).throw(RuntimeError("No Letterboxd export found. Put the zip from letterboxd.com (Settings → Import & Export) in the Beeldbuis work folder and run the update again, or save it as letterboxd.zip in the app folder.")))
        if path == "/api/channels/refresh":
            threading.Thread(target=catalog_refresh, kwargs={"force": True}, daemon=True).start()
            return self.send_json({"ok": True})
        if path == "/api/epg/refresh":
            threading.Thread(target=epg_refresh, kwargs={"force": True}, daemon=True).start()
            return self.send_json({"ok": True})
        if path == "/api/favorites":
            body = self.read_json_body()
            if not isinstance(body.get("favorites"), list):      # a half-sent or empty request must never wipe the favourites
                return self.send_json({"ok": False, "error": "No favourites in this request"}, 400)
            with LOCK:
                STATE["favorites"] = [str(k) for k in body["favorites"]][:5000]
            save_config()
            tv_schedule("favourites")
            return self.send_json({"ok": True})
        if path == "/api/tv/export":
            return self.api(lambda: tv_export("manual"))
        if path == "/api/tv/choose-folder":
            return self.api(tv_choose_folder)
        if path == "/api/quit":
            self.send_json({"ok": True})
            threading.Timer(0.3, shutdown).start()
            return
        self.send_error(404)

    # -- api implementations
    def pair_api(self):
        try:
            body = self.read_json_body()
            token, name = pair(body.get("code"), (body.get("name") or "").strip() or device_name(self.headers.get("User-Agent")))
        except Exception as e:  # noqa
            return self.send_json({"ok": False, "error": str(e)}, getattr(e, "code", 400))
        cookie = "bb_dev=%s; Path=/; Max-Age=315360000; HttpOnly; SameSite=Lax" % token
        return self.send_json({"ok": True, "data": {"key": token, "name": name}}, extra=[("Set-Cookie", cookie)])

    def status(self):
        with LOCK:
            if not STATE["server"]:
                return {"logged_in": False, "ffmpeg": bool(FFMPEG), "client": self.client()}
            server, user = STATE["server"], STATE["username"]
        ui, err = account_info()
        out = {
            "logged_in": True, "ffmpeg": bool(FFMPEG),
            "server": server, "username": user,
            "status": ui.get("status"), "exp_date": ui.get("exp_date"),
            "max_connections": ui.get("max_connections"), "active_cons": ui.get("active_cons"),
            "catalog": {"t": CATALOG["t"], "status": CATALOG["status"], "n": len(CATALOG["groups"])},
            "epg": epg_status(), "version": VERSION, "client": self.client(),
        }
        if err:
            out["provider_error"] = err               # the app stays usable: recordings, guide and library are local
        elif ui.get("auth") in (0, "0", False):
            out["provider_error"] = "Your provider no longer accepts this login. Has the subscription ended or the password changed?"
            out["auth_failed"] = True
        elif str(ui.get("status") or "Active").lower() in ("expired", "banned", "disabled"):
            out["provider_error"] = "Your provider reports this subscription as %s." % str(ui.get("status")).lower()
        return out

    def login(self):
        body = self.read_json_body()
        server = normalize_server(body.get("server", ""))
        user = body.get("username", "").strip()
        pw = body.get("password", "").strip()
        if not (server and user and pw):
            raise RuntimeError("Please fill in server, username and password")
        with LOCK:
            STATE["server"], STATE["username"], STATE["password"] = server, user, pw
        info = xtream()
        ui = info.get("user_info", {}) if isinstance(info, dict) else {}
        if not ui or ui.get("auth") in (0, "0", False):
            with LOCK:
                STATE["server"] = STATE["username"] = STATE["password"] = ""
            raise RuntimeError("Login failed: wrong username or password")
        si = info.get("server_info", {}) if isinstance(info, dict) else {}
        account_set(info)
        with LOCK:
            STATE["server_tz"] = si.get("timezone") or DEFAULT_TZ
        with LOCK:
            if "tour_done" not in STATE["settings"]:     # first sign-in on this Mac: the UI opens the setup tour
                STATE["settings"]["tour_done"] = False
        save_config()
        PROBE_CACHE.clear()
        threading.Thread(target=lambda: (catalog_refresh(force=True), epg_refresh(force=True)), daemon=True).start()
        return self.status()

    def epg(self, sid):
        g = stream_group(sid)
        if not g or not g.get("epg"):
            return []
        now = int(time.time())
        rows = epg_rows([g["epg"]], now - 3600, now + 12 * 3600).get(g["epg"], [])
        if not any(p["start"] <= now < p["stop"] for p in rows):
            if not EPG_SID.get(g["epg"]):
                EPG_SID[g["epg"]] = [v["id"] for v in g["variants"] if v.get("epg")][:2] or [int(sid)]
            if epg_short_fill([g["epg"]], wait=True):
                rows = epg_rows([g["epg"]], now - 3600, now + 12 * 3600).get(g["epg"], [])
        return rows

    def channels(self):
        with CATALOG_LOCK:
            data = {"t": CATALOG["t"], "cats": CATALOG["cats"], "groups": CATALOG["groups"],
                    "status": CATALOG["status"], "error": CATALOG["error"]}
        if not data["groups"] and data["status"] != "busy":
            threading.Thread(target=catalog_refresh, kwargs={"force": True}, daemon=True).start()
            data["status"] = "busy"
        return data

    def probe_api(self, kind, sid, ext):
        if kind not in ("live", "movie", "series") or not sid:
            raise RuntimeError("Invalid stream")
        if not FFMPEG:
            return {"mode": "browser", "duration": 0}
        target = stream_url(kind, sid, ext or ("ts" if kind == "live" else "mp4"))
        if target not in PROBE_CACHE:
            if rec_blocking():
                raise RuntimeError("A recording is in progress; the subscription allows one connection at a time.")
            stop_all_sessions(taken_by=self.dev)
        info = probe(target)
        if info.get("error"):
            raise RuntimeError(stream_error(kind, info["error"]))
        return info

    def vod_fallback(self, kind, sid, ext, target, info):
        """A film or episode the provider answers with 5XX/404 (its file is missing or broken on their side; retries
        already happened in probe()): try the real extension and other editions one by one (never two connections),
        remember what failed so it moves down the editions list and out of recommendations."""
        dk = "ep" if kind == "series" else kind      # episode ids and series ids are separate number ranges: never mix them
        if not info.get("error"):
            if vod_is_dead(dk, sid):
                vod_mark_dead(dk, sid, False)
            return target, info, sid, ext, None
        if not _MISSING_RE.search(info["error"]):
            return target, info, sid, ext, None
        first_err, tried = info["error"], 0
        for alt in vod_alternatives(kind, sid, ext):
            tried += 1
            t2 = stream_url(kind, alt["id"], alt["ext"])
            i2 = PROBE_CACHE.get(t2) or _probe_once(t2)
            if not i2.get("error"):
                PROBE_CACHE[t2] = i2
                vod_mark_dead(dk, sid)
                if str(alt["id"]) != str(sid) and vod_is_dead(dk, alt["id"]):
                    vod_mark_dead(dk, alt["id"], False)
                log_write("[vod] %s:%s failed (%s); playing %s:%s.%s instead\n" % (kind, sid, redact(first_err)[-60:], kind, alt["id"], alt["ext"]))
                fb = {"from": int(sid) if str(sid).isdigit() else sid, "id": int(alt["id"]) if str(alt["id"]).isdigit() else alt["id"],
                      "ext": alt["ext"], "label": alt["label"], "edition": bool(alt.get("edition"))}
                return t2, i2, str(alt["id"]), alt["ext"], fb
            if alt.get("edition") and _MISSING_RE.search(i2["error"]):
                vod_mark_dead(dk, alt["id"])
        vod_mark_dead(dk, sid)
        raise RuntimeError(stream_error(kind, first_err, tried))

    # -- streaming
    def play_api(self, kind, sid, ext, start, force, cu_start=0, cu_dur=0, native=False):
        if kind not in ("live", "movie", "series", "catchup", "rec") or not sid or not FFMPEG:
            raise RuntimeError("Invalid stream")
        follow = False
        fallback = None
        if kind == "rec":
            with REC_LOCK:
                rec = next((r for r in RECS["items"] if r["id"] == sid), None)
            if not rec or not rec.get("file") or not os.path.exists(rec["file"]):
                raise RuntimeError("Recording file not found")
            target = rec["file"]
            follow = rec["status"] == "recording"
            info = probe(target, cache=not follow)
            if follow:
                deadline = time.time() + 15      # a recording that has just started holds too little data to probe yet
                while info.get("error") and time.time() < deadline:
                    time.sleep(1.5)
                    info = probe(target, cache=False)
            if follow:
                info["mode"] = "copy"
                info["duration"] = max(0, time.time() - rec.get("started", rec["start"]))
        else:
            blocking = rec_blocking()
            if blocking:
                err = RuntimeError("Recording '%s' is in progress; the subscription allows one connection at a time." % blocking["title"])
                err.code = 409
                err.rec = blocking["id"]
                raise err
            # one connection: end the current stream before probing the new one; a probe next to a running stream made
            # the provider drop one of them, and the old ffmpeg's reconnect then knocked out the new channel
            stop_all_sessions(taken_by=self.dev)
            if kind == "catchup":
                if not cu_start or not cu_dur:
                    raise RuntimeError("Missing catch-up times")
                skip_min = min(int(start // 60), max(0, cu_dur - 1))
                target = timeshift_url(sid, cu_start + skip_min * 60, max(1, cu_dur - skip_min))
                info = dict(probe(target))
                if info.get("error"):
                    raise RuntimeError("Catch-up not available: " + info["error"])
                info["mode"] = "copy" if info.get("vcodec") in COPY_VIDEO else "transcode"
                info["duration"] = cu_dur * 60
                sess, pl, err = start_session(target, info, 0, force, live=False, owner=self.dev)
                if err:
                    raise RuntimeError(err)
                # the timeshift stream arrives at roughly real-time speed, so the seconds within the minute are skipped by the player
                return {"playlist": "/hls/%s/%s" % (sess, pl), "info": info, "offset": skip_min * 60, "seek": max(0, start - skip_min * 60)}
            target = stream_url(kind, sid, ext or ("ts" if kind == "live" else "mp4"))
            info = PROBE_CACHE.get(target)
            if info is None and kind == "live":
                info = live_probe_get(sid)
                if info:
                    info["_known"] = True
            if info is None:
                info = probe(target)
                if kind == "live" and not info.get("error"):
                    live_probe_put(sid, info)
            if kind in ("movie", "series"):
                target, info, sid, ext, fallback = self.vod_fallback(kind, sid, ext, target, info)
        if info.get("error"):
            raise RuntimeError(stream_error(kind, info["error"]))
        ext_sub = None
        if kind == "rec":
            wf = os.path.join(SUBS_DIR, "wh_" + re.sub(r"[^a-z0-9]+", "_", ("rec:%s" % sid).lower())[:80] + ".vtt")
            if os.path.exists(wf) and info.get("sub") is None:
                ext_sub = wf
        if kind in ("movie", "series") and info.get("sub") is None:
            try:
                ext_sub = external_subs_for(kind, sid, ext)
            except Exception as e:  # noqa
                log_write("[subs] %s\n" % e)
        if info.get("mode") == "direct" and not force and kind != "live":
            out = {"playlist": None, "info": info, "direct": "/direct/%s/%s.%s" % (kind, sid, ext or "mp4"), "offset": 0}
            if fallback:
                out["fallback"] = fallback
            if ext_sub:
                out["subs"] = "/subs/" + os.path.basename(ext_sub)
                info["ext_sub"] = True
            return out
        # a recording that is still being made plays as a growing "event" playlist from the start (or the resume point),
        # never from its live end: that would show the result of a match the viewer recorded to watch later
        sess, pl, err = start_session(target, info, start, force, live=(kind == "live"), follow=follow, owner=self.dev,
                                      min_segs=3 if native and (kind == "live" or follow) else 1, ext_sub=ext_sub)
        if err and info.get("_known"):        # remembered codec info may be stale: probe properly and try once more
            live_probe_put(sid, None)
            info = probe(target, cache=False)
            if info.get("error"):
                raise RuntimeError(stream_error(kind, info["error"]))
            live_probe_put(sid, info)
            sess, pl, err = start_session(target, info, start, force, live=True, owner=self.dev,
                                          min_segs=3 if native else 1)
        if err:
            raise RuntimeError(err)
        if ext_sub:
            info["ext_sub"] = True
        out = {"playlist": "/hls/%s/%s" % (sess, pl), "info": info, "offset": start}
        if fallback:
            out["fallback"] = fallback
        return out

    def sub_file(self, path):
        name = os.path.basename(path)
        fp = os.path.join(SUBS_DIR, name)
        if not re.match(r"^[a-z0-9_]+\.vtt$", name) or not os.path.exists(fp):
            return self.send_error(404)
        body = open(fp, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", "text/vtt; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def rec_file(self, path):
        rid = path.rsplit("/", 1)[-1].split(".")[0]
        with REC_LOCK:
            rec = next((r for r in RECS["items"] if r["id"] == rid), None)
        if not rec or not rec.get("file") or not os.path.exists(rec["file"]):
            return self.send_error(404)
        fp = rec["file"]
        size = os.path.getsize(fp)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].split(",")[0].strip().partition("-")
            try:
                if a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
                elif b:
                    start = max(0, size - int(b))
            except ValueError:
                rng, start, end = None, 0, size - 1
        if start > end or start >= size:
            self.send_response(416); self.send_header("Content-Range", "bytes */%d" % size); self.end_headers(); return
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4" if fp.endswith(".mp4") else "video/mp2t")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        try:
            with open(fp, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    chunk = f.read(min(256 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:  # noqa
            pass

    def asset(self, path):
        name = os.path.basename(path)
        if name == "favicon.ico":
            name = "icon-64.png"
        if not re.match(r"^[A-Za-z0-9_.-]+$", name):
            return self.send_error(404)
        fp = os.path.join(ASSET_DIR, name)
        try:
            data = open(fp, "rb").read()
        except OSError:
            return self.send_error(404)
        ctype = ("image/png" if name.endswith(".png") else "image/svg+xml" if name.endswith(".svg") else
                 "application/javascript" if name.endswith(".js") else "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def buffer_stream(self, sess, qs):
        """The live buffer as one endless MPEG-TS stream, starting `behind` seconds before live (for the Samsung
        TV, whose player cannot seek in a live HLS playlist). It follows new segments as ffmpeg writes them, so the
        TV pauses by simply not reading (TCP holds it) and rewinds by asking again with a larger `behind`."""
        try:
            behind = max(0.0, float((qs.get("behind") or ["0"])[0] or 0))
        except ValueError:
            behind = 0.0
        segs = buffer_segments(sess)
        if not segs:
            return self.send_error(404)
        # start segment: walk back from the newest one; "live" (0) keeps ~6 s of margin like a normal live player
        want, i, acc = max(behind, 6.0), len(segs) - 1, 0.0
        while i > 0 and acc + segs[i][1] <= want:
            acc += segs[i][1]
            i -= 1
        names = [n for n, _ in segs]
        cur = names[i]
        init = os.path.join(sess["dir"], "init.mp4")
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        ff = subprocess.Popen([FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-fflags", "+genpts", "-f", "mp4", "-i", "pipe:0",
                               "-map", "0", "-c", "copy", "-f", "mpegts", "-mpegts_flags", "resend_headers", "pipe:1"],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        PROCS.add(ff)
        sess["readers"] = sess.get("readers", 0) + 1
        alive = {"ok": True}

        def feed():
            nonlocal cur
            try:
                ff.stdin.write(open(init, "rb").read())
                while alive["ok"] and sess.get("proc") and SESSIONS.get(sess.get("sid")) is sess:
                    sess["t"] = time.time()
                    try:
                        data = open(os.path.join(sess["dir"], cur), "rb").read()
                    except OSError:
                        data = None
                    if data is None:     # deleted while paused longer than the buffer: carry on from the oldest one
                        segs2 = [n for n, _ in buffer_segments(sess)]
                        if not segs2:
                            time.sleep(0.5)
                            continue
                        cur = segs2[0]
                        continue
                    ff.stdin.write(data)
                    ff.stdin.flush()
                    m_ = re.match(r"seg_(\d+)", cur)
                    nxt = "seg_%05d.m4s" % ((int(m_.group(1)) if m_ else 0) + 1)
                    while alive["ok"] and SESSIONS.get(sess.get("sid")) is sess:     # wait until ffmpeg has finished the next one
                        sess["t"] = time.time()
                        names2 = [n for n, _ in buffer_segments(sess)]
                        if nxt in names2:
                            break
                        if names2 and nxt < names2[0]:
                            break
                        if sess["proc"].poll() is not None:
                            alive["ok"] = False
                            break
                        time.sleep(0.4)
                    cur = nxt
            except (OSError, ValueError):
                pass
            finally:
                try:
                    ff.stdin.close()
                except OSError:
                    pass
        th = threading.Thread(target=feed, daemon=True)
        th.start()
        try:
            while True:
                chunk = ff.stdout.read1(64 * 1024) if hasattr(ff.stdout, "read1") else ff.stdout.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                sess["t"] = time.time()
        except Exception:  # noqa  (the TV closed the stream: channel change, rewind, back)
            pass
        finally:
            alive["ok"] = False
            sess["readers"] = max(0, sess.get("readers", 1) - 1)
            try:
                ff.kill()
                ff.wait(timeout=3)
            except Exception:  # noqa
                pass
            PROCS.discard(ff)

    def hls_file(self, path, qs=None):
        qs = qs or {}
        parts = path.split("/")
        if len(parts) != 4 or ".." in path:
            return self.send_error(404)
        sid, name = parts[2], parts[3]
        sess = SESSIONS.get(sid)
        if not sess:
            return self.send_error(404)
        sess["t"] = time.time()
        if name == "buffer.ts":
            return self.buffer_stream(sess, qs)
        fp = os.path.join(sess["dir"], name)
        try:
            data = open(fp, "rb").read()
        except OSError:
            return self.send_error(404)
        if name == "index.m3u8" and not sess.get("live") and b"#EXT-X-START" not in data:
            data = data.replace(b"#EXTM3U\n", b"#EXTM3U\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\n", 1)
        ctype = ("application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp4" if name.endswith(".mp4") else
                 "text/vtt" if name.endswith(".vtt") else "video/iso.segment")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def stream(self, path, qs, direct=False):
        parts = path.split("/")
        if len(parts) != 4 or parts[2] not in ("live", "movie", "series"):
            return self.send_error(404)
        kind = parts[2]
        name, _, ext = parts[3].rpartition(".")
        if not name or not ext:
            return self.send_error(404)
        blocking = rec_blocking()
        if blocking:      # a recording holds the one connection: opening another stream would end the recording
            return self.send_json({"ok": False, "error": "Recording '%s' is in progress; the subscription allows one connection at a time."
                                   % blocking["title"], "rec": blocking["id"]}, 409)
        if any(s.get("owner") != self.dev[0] for s in list(SESSIONS.values())):
            stop_all_sessions(taken_by=self.dev)      # one connection: this device takes it over
        gen = DIRECT.setdefault(self.dev[0], {"n": 0, "stop": 0})
        gen["n"] += 1
        try:
            return self.proxy(stream_url(kind, name, ext), gen)
        finally:
            gen["n"] -= 1

    def proxy(self, target, gen=None):
        stop_at = gen["stop"] if gen else None
        extra = {}
        rng = self.headers.get("Range")
        if rng:
            extra["Range"] = rng
        try:
            resp = open_url(target, extra, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        except Exception as e:  # noqa
            try:
                self.send_error(502, str(e))
            except Exception:  # noqa
                pass
            return
        status = getattr(resp, "status", None) or getattr(resp, "code", 502)
        if gen is not None:
            gen.setdefault("resps", set()).add(resp)
            if gen["stop"] != stop_at:           # taken over while connecting
                resp.close()
        try:
            self.send_response(status)
            for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                v = resp.headers.get(h)
                if v:
                    self.send_header(h, v)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk or (gen and gen["stop"] != stop_at):     # another device took the connection
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:  # noqa
            pass
        finally:
            if gen is not None:
                gen.get("resps", set()).discard(resp)
            try:
                resp.close()
            except Exception:  # noqa
                pass


# ------------------------------------------------------------------ html ----
HTML = r"""__UI_HTML__"""


# ------------------------------------------------------------------ main ----
HTTPD = None


class Server(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self):
        if self.address_family == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            except (OSError, AttributeError):
                pass
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "beeldbuis", self.server_address[1]


class Server4(Server):
    address_family = socket.AF_INET


def make_server():
    if BIND in ("127.0.0.1", "localhost"):
        return Server4(("127.0.0.1", PORT), Handler)
    try:
        return Server((BIND if ":" in BIND else "::", PORT), Handler) if ":" in BIND or BIND == "::" else Server4((BIND, PORT), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98):   # port in use: don't fall back, report it
            raise
        return Server4(("0.0.0.0", PORT), Handler)


def shutdown():
    kill_all()
    os._exit(0)


def open_window():
    """Open the Beeldbuis window: Chrome in app mode when Chrome is installed, else the default browser."""
    url = "http://%s:%d" % (HOST, PORT)
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    try:
        if sys.platform == "darwin" and os.path.exists(chrome):
            WINDOW["proc"] = subprocess.Popen([chrome, "--app=" + url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    except Exception:  # noqa
        pass
    webbrowser.open(url)
    return False


def watchdog():
    while True:
        time.sleep(30)
        if not AUTO_EXIT:
            continue
        p = WINDOW["proc"]
        window_open = p is not None and p.poll() is None
        idle = time.time() - LAST_REQ["t"]
        if not window_open and idle > 15 * 60 and not PROCS:
            shutdown()


def main():
    global HTTPD
    if "--daemon" in ARGS:
        try:
            os.setsid()  # eigen sessie: wordt niet meegestopt als de app-icoon stopt
        except Exception:  # noqa
            pass
    load_config()
    keys_load()
    migrate_profile_ids()
    meta_reset_misses()
    catalog_load()
    rec_load()
    threading.Thread(target=vod_load, daemon=True).start()
    try:
        epg_init()
        threading.Thread(target=lambda: epg_resolve(save=True), daemon=True).start()
    except Exception as e:  # noqa
        print("EPG database could not be opened: %s" % e)
    try:
        HTTPD = make_server()
    except OSError as e:
        print("Cannot use port %d (%s). Is Beeldbuis already running? Then open http://%s:%d" % (PORT, e, HOST, PORT))
        sys.exit(1)
    HTTPD.daemon_threads = True
    url = "http://%s:%d" % (HOST, PORT)
    print("Beeldbuis is running on %s  (stop: Ctrl+C)" % url)
    print("ffmpeg: %s" % (FFMPEG or "not found - limited codec support"))
    if "--open-window" in ARGS:
        threading.Timer(0.5, open_window).start()
    elif "--no-browser" not in ARGS:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    threading.Thread(target=watchdog, daemon=True).start()
    threading.Thread(target=session_janitor, daemon=True).start()
    threading.Thread(target=background_refresher, daemon=True).start()
    threading.Thread(target=rec_scheduler, daemon=True).start()
    threading.Thread(target=whisper_worker, daemon=True).start()
    threading.Thread(target=dlna_ssdp, daemon=True).start()
    threading.Thread(target=meta_worker, daemon=True).start()
    threading.Thread(target=rate_worker, daemon=True).start()
    threading.Thread(target=library_refresher, daemon=True).start()
    shutil.rmtree(HLS_DIR, ignore_errors=True)
    try:
        HTTPD.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        kill_all()


if __name__ == "__main__":
    main()
