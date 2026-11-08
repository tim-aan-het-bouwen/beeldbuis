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
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Beeldbuis/9.0"
AUTO_EXIT = "--auto-exit" in ARGS
VERSION = 9

if sys.platform == "darwin":
    CONFIG_DIR = os.path.expanduser("~/Library/Application Support/IPTVPlayer")
else:
    CONFIG_DIR = os.path.expanduser("~/.config/iptvplayer")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "ffmpeg.log")
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

FFMPEG = arg("--ffmpeg") or os.environ.get("IPTV_FFMPEG") or shutil.which("ffmpeg")
if FFMPEG and not os.path.isfile(FFMPEG):
    FFMPEG = None

STATE = {"server": "", "username": "", "password": "", "favorites": [], "recent": [], "settings": {}, "positions": {}, "server_tz": "",
         "devices": {}, "pair_code": ""}
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


# ---------------------------------------------------------------- config ----
def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with LOCK:
            for k in STATE:
                if k in data:
                    STATE[k] = data[k]
    except (OSError, ValueError):
        pass


def save_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with LOCK:
        data = dict(STATE)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_PATH)


# ---------------------------------------------------------------- xtream ----
def open_url(url, extra_headers=None, timeout=30):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX)


def xtream(action=None, **params):
    with LOCK:
        server, user, pw = STATE["server"], STATE["username"], STATE["password"]
    if not server:
        raise RuntimeError("Not logged in")
    q = {"username": user, "password": pw}
    if action:
        q["action"] = action
    q.update({k: v for k, v in params.items() if v not in (None, "")})
    url = server.rstrip("/") + "/player_api.php?" + urllib.parse.urlencode(q)
    with open_url(url, timeout=90) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except ValueError:
        raise RuntimeError("The server did not return a valid answer (is this an Xtream Codes server?)")


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
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-user_agent", UA, "-analyzeduration", "3000000",
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
        cmd += ["-hls_list_size", "30", "-hls_delete_threshold", "15",
                "-hls_flags", "delete_segments+omit_endlist+independent_segments"]
    else:
        cmd += ["-hls_list_size", "0", "-hls_playlist_type", "event", "-hls_flags", "independent_segments"]
    cmd += [os.path.join(outdir, "index.m3u8")]
    return cmd


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
        SESSIONS[sid] = {"proc": proc, "dir": outdir, "t": time.time(), "local": is_local(target), "owner": owner[0], "live": live}
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


def session_janitor():
    while True:
        time.sleep(10)
        now = time.time()
        for sid, sess in list(SESSIONS.items()):
            if now - sess["t"] > 90:
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
RECS = {"items": [], "procs": {}}        # procs: rec id -> Popen
REC_LOCK = threading.Lock()
REC_WARN_SECONDS = 120


def rec_load():
    try:
        with open(REC_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        with REC_LOCK:
            RECS["items"] = data.get("items", [])
            for r in RECS["items"]:
                if r.get("status") == "recording":    # we were restarted while recording
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
           "size": 0, "duration": 0, "error": None, "created": now, "epg": body.get("epg") or "", "icon": body.get("icon") or ""}
    notes = []
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
    cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "warning", "-user_agent", UA,
           "-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "10",
           "-fflags", "+genpts+discardcorrupt", "-i", url, "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-ignore_unknown",
           "-c", "copy", "-f", "mpegts", "pipe:1"]
    log_write("\n--- %s REC%s %s -> %s\n" % (time.strftime("%H:%M:%S"), " restart" if append else "", rec["id"], rec["file"]))
    out = open(rec["file"], "ab" if append else "wb")
    proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    out.close()
    log_pipe(proc)
    PROCS.add(proc)
    with REC_LOCK:
        RECS["procs"][rec["id"]] = proc
    return proc


def rec_start(rec):
    os.makedirs(REC_DIR, exist_ok=True)
    base = "%s %s - %s" % (time.strftime("%Y-%m-%d %H.%M", time.localtime(rec["start"])), safe_name(rec["channel"]), safe_name(rec["title"]))
    fp = os.path.join(REC_DIR, base + ".ts")
    n = 1
    while os.path.exists(fp):
        n += 1
        fp = os.path.join(REC_DIR, "%s (%d).ts" % (base, n))
    stop_all_sessions(remote_only=True)      # the recording has priority over live TV
    with REC_LOCK:
        rec["status"] = "recording"
        rec["file"] = fp
        rec["started"] = int(time.time())
        rec["restarts"] = 0
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
    fp = rec.get("file")
    if not fp or not os.path.exists(fp):
        return
    info = probe(fp, cache=False)
    rec["duration"] = int(info.get("duration") or 0)
    if info.get("vcodec") in ("h264", "hevc") and info.get("acodec") in ("aac",) and not info.get("subs"):
        mp4 = fp[:-3] + ".mp4"
        cmd = [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", fp, "-map", "0:v:0", "-map", "0:a", "-c", "copy",
               "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", mp4]
        if info.get("vcodec") == "hevc":
            cmd[cmd.index("-c") + 2:cmd.index("-c") + 2] = ["-tag:v", "hvc1"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=3600)
            if r.returncode == 0 and os.path.getsize(mp4) > 1000000:
                os.remove(fp)
                with REC_LOCK:
                    rec["file"] = mp4
                    rec["size"] = os.path.getsize(mp4)
            else:
                try:
                    os.remove(mp4)
                except OSError:
                    pass
        except Exception:  # noqa
            pass
    rec_save()


def rec_scheduler():
    """Starts and stops recordings on time."""
    while True:
        try:
            now = time.time()
            with REC_LOCK:
                items = list(RECS["items"])
            for r in items:
                a, b = rec_window(r)
                if r["status"] == "scheduled":
                    if now >= b:
                        r["status"] = "failed"
                        r["error"] = "Missed (the player was not running)"
                        rec_save()
                    elif now >= a:
                        if rec_active():
                            continue        # another recording still holds the connection
                        try:
                            rec_start(r)
                        except Exception as e:  # noqa
                            r["status"] = "failed"
                            r["error"] = str(e)
                            rec_save()
                elif r["status"] == "recording":
                    proc = RECS["procs"].get(r["id"])
                    if now >= b:
                        rec_stop(r)
                    elif proc is not None and proc.poll() is not None:
                        # ffmpeg quit early: continue in the same file while the programme is still on
                        PROCS.discard(proc)
                        with REC_LOCK:
                            RECS["procs"].pop(r["id"], None)
                        if now < b - 30 and r.get("restarts", 0) < 20:
                            r["restarts"] = r.get("restarts", 0) + 1
                            time.sleep(2)
                            try:
                                rec_spawn(r, append=True)
                            except Exception as e:  # noqa
                                r["error"] = "Restart failed: %s" % e
                                rec_stop(r)
                        else:
                            rec_stop(r)
        except Exception:  # noqa
            pass
        time.sleep(3)


def rec_status():
    now = int(time.time())
    act = rec_blocking()
    up = rec_upcoming(REC_WARN_SECONDS)
    out = {"active": None, "upcoming": None}
    if act:
        out["active"] = {"id": act["id"], "title": act["title"], "channel": act["channel"], "stop": act["stop"] + act.get("pad_after", 0),
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
        if not g:
            g = {"key": key, "name": disp, "icon": s.get("stream_icon") or "",
                 "epg": s.get("epg_channel_id") or "", "cats": [cid], "variants": [var], "country": country, "_names": [disp]}
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
        by_stream[sid] = key
    out = []
    for key in order:
        g = groups[key]
        g["variants"].sort(key=lambda v: (-v["rank"], v["q"] == "8K", v["_i"]))
        for v in g["variants"]:
            v.pop("_i", None)
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
EPG_TTL = 8 * 3600
EPG_KEEP_DAYS = 8
EPG = {"t": 0, "status": "idle", "error": None, "count": 0, "first": 0, "last": 0}
EPG_LOCK = threading.Lock()


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
        conn.commit()
        row = conn.execute("SELECT v FROM meta WHERE k='t'").fetchone()
        EPG["t"] = int(row[0]) if row else 0
        epg_stats(conn)
    finally:
        conn.close()


def epg_stats(conn):
    row = conn.execute("SELECT COUNT(*), MIN(start), MAX(stop) FROM prog").fetchone()
    EPG["count"], EPG["first"], EPG["last"] = (row[0] or 0), (row[1] or 0), (row[2] or 0)


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


def epg_refresh(force=False):
    with EPG_LOCK:
        if EPG["status"] == "busy":
            return False
        if not force and time.time() - EPG["t"] < EPG_TTL:
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
            rows = []
            n = 0
            cur = conn.cursor()
            cur.execute("BEGIN")
            for ev, el in ET.iterparse(tmp, events=("end",)):
                if el.tag != "programme":
                    if el.tag == "channel":
                        el.clear()
                    continue
                st, sp = parse_xmltv_time(el.get("start", "")), parse_xmltv_time(el.get("stop", ""))
                ch = el.get("channel") or ""
                if st and sp and ch and sp > st:
                    t = el.find("title")
                    d = el.find("desc")
                    rows.append((ch, st, sp, (t.text or "").strip() if t is not None else "", (d.text or "").strip() if d is not None else ""))
                el.clear()
                if len(rows) >= 5000:
                    cur.executemany("INSERT OR REPLACE INTO prog (ch,start,stop,title,descr) VALUES (?,?,?,?,?)", rows)
                    n += len(rows)
                    rows = []
            if rows:
                cur.executemany("INSERT OR REPLACE INTO prog (ch,start,stop,title,descr) VALUES (?,?,?,?,?)", rows)
                n += len(rows)
            cur.execute("DELETE FROM prog WHERE stop < ?", (int(time.time()) - EPG_KEEP_DAYS * 86400,))
            now = int(time.time())
            cur.execute("INSERT OR REPLACE INTO meta (k,v) VALUES ('t',?)", (str(now),))
            conn.commit()
            EPG["t"] = now
            epg_stats(conn)
        finally:
            conn.close()
        with EPG_LOCK:
            EPG["status"] = "idle"
        tv_schedule("tv guide", delay=10)
        return True
    except Exception as e:  # noqa
        with EPG_LOCK:
            EPG["status"] = "idle"
            EPG["error"] = str(e)
        return False
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


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
    return out


def epg_now_next(ids):
    now = int(time.time())
    rows = epg_rows(ids, now, now + 12 * 3600)
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
    return {"updated": EPG["t"], "status": EPG["status"], "error": EPG["error"], "count": EPG["count"],
            "first": EPG["first"], "last": EPG["last"], "next": EPG["t"] + EPG_TTL if EPG["t"] else 0}



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
    """Native macOS folder picker (runs in Tim's login session via the launchd agent)."""
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
VOD = {"t": 0, "status": "idle", "error": None, "movies": [], "series": [], "cats": {}, "groups": {}, "by_norm": {}, "n": 0}
VOD_LOCK = threading.Lock()
META_LOCK = threading.Lock()
META_Q = []                      # keys waiting for TMDb/OMDb lookups
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
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    if langs is None:
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
    for g in groups.values():
        g["variants"].sort(key=vod_variant_rank)
    with VOD_LOCK:
        VOD.update({"t": data["t"], "status": "idle", "error": None, "cats": data["cats"], "groups": groups, "by_norm": by_norm,
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


def vod_visible(g):
    if g["adult"] and STATE["settings"].get("show_adult") is not True:
        return False
    return any(vod_lang_visible(v["lang"]) for v in g["variants"])


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
    if m:
        t["meta"] = {k: m.get(k) for k in ("id", "poster", "backdrop", "vote", "votes", "genres", "overview", "imdb_rating", "rt", "runtime", "seasons", "imdb")}
    return t


def vod_list(kind, cat="", q="", limit=400, offset=0):
    """Library listing for the Movies / Series tabs: one entry per title, variants merged."""
    q = norm_title(q)
    with VOD_LOCK:
        groups = [g for g in VOD["groups"].values() if g["kind"] == kind]
    out = []
    for g in groups:
        if not vod_visible(g):
            continue
        if cat and not any(v["cat"] == cat for v in g["variants"]):
            continue
        if q and q not in g["norm"]:
            continue
        out.append(g)
    out.sort(key=lambda g: (-g["added"], g["title"]))
    total = len(out)
    return {"total": total, "items": [vod_tile(g) for g in out[offset:offset + limit]],
            "cats": [c for c in (VOD["movie_cats"] if kind == "movie" else VOD["series_cats"]) if vod_cat_visible(c["name"])]}


def vod_cat_visible(name):
    if re.search(r"\b(XXX|ADULT|PORN|18\+)\b", (name or "").upper()) and STATE["settings"].get("show_adult") is not True:
        return False
    m = re.match(r"^\s*\|?([A-Z0-9+]{1,6})\|", (name or "").upper())
    return vod_lang_visible(m.group(1) if m else "")


def vod_item(gk):
    g = vod_group(gk)
    if not g:
        raise RuntimeError("Not in the library any more")
    out = dict(g)
    out["meta"] = meta_get(g, fetch=True)
    out["variants"] = [v for v in g["variants"] if vod_lang_visible(v["lang"])] or g["variants"]
    with LOCK:
        out["position"] = next((p for k, p in STATE["positions"].items() if p and p.get("gkey") == gk), None)
    out["feedback"] = feedback_for(gk)
    return out


# ------------------------------------------------------------ metadata db ----
def meta_db():
    conn = sqlite3.connect(META_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, kind TEXT, tmdb INTEGER, v TEXT, t INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, t INTEGER)")
    conn.execute("CREATE TABLE IF NOT EXISTS feedback (profile TEXT, k TEXT, verdict TEXT, t INTEGER, PRIMARY KEY(profile, k))")
    conn.execute("CREATE TABLE IF NOT EXISTS shown (profile TEXT, k TEXT, n INTEGER, t INTEGER, PRIMARY KEY(profile, k))")
    conn.execute("CREATE TABLE IF NOT EXISTS lb (profile TEXT, norm TEXT, year INTEGER, title TEXT, rating REAL, watched INTEGER, "
                 "watchlist INTEGER, tmdb INTEGER, t INTEGER, PRIMARY KEY(profile, norm, year))")
    if "liked" not in [r[1] for r in conn.execute("PRAGMA table_info(lb)")]:
        conn.execute("ALTER TABLE lb ADD COLUMN liked INTEGER DEFAULT 0")
    conn.execute("CREATE TABLE IF NOT EXISTS subs (k TEXT PRIMARY KEY, file TEXT, src TEXT, t INTEGER)")
    return conn


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


def omdb(imdb_id, ttl=7 * 86400):
    k = key("OMDB_API_KEY")
    if not (k and imdb_id):
        return None
    ck = "omdb:" + imdb_id
    hit = cache_get(ck, ttl)
    if hit is not None:
        return hit
    try:
        data = http_json(OMDB_BASE + "?" + urllib.parse.urlencode({"i": imdb_id, "apikey": k, "tomatoes": "true"}))
    except Exception:  # noqa
        return None
    if not isinstance(data, dict) or data.get("Response") == "False":
        cache_put(ck, {})
        return {}
    cache_put(ck, data)
    return data


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
    results = (data or {}).get("results") or []
    if not results and year:
        data = tmdb(path, 30 * 86400, query=title, include_adult="false")
        results = (data or {}).get("results") or []
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


def meta_get(g, fetch=True, details=True):
    """Metadata for a library group: cached TMDb record, fetched (search + details + OMDb) when allowed."""
    gk = g["key"]
    m = meta_cached(gk)
    fresh = m and time.time() - m.get("t", 0) < 14 * 86400
    if m and (fresh or not fetch) and (not details or m.get("detail") or not fetch):
        if details and fetch and not m.get("detail"):
            pass
        else:
            return m
    if not fetch or not (key("TMDB_API_KEY") or key("TMDB_READ_TOKEN")):
        return m
    kind = g["kind"]
    r = None
    if m and m.get("id"):
        r = {"id": m["id"]}
    else:
        r = tmdb_search(kind, g["title"], g["year"])
    if not r:
        if not m:
            meta_store(gk, kind, {"id": 0, "miss": True})
        return m
    if details:
        d = tmdb("/%s/%d" % ("movie" if kind == "movie" else "tv", int(r["id"])), 14 * 86400, append_to_response="external_ids")
        if d:
            r = d
    m2 = meta_pack(kind, r) if r.get("title") or r.get("name") else dict(m or {})
    if m:
        for kk, vv in m.items():
            m2.setdefault(kk, vv)
    m2["detail"] = bool(details and r.get("genres") is not None)
    if details and m2.get("imdb"):
        o = omdb(m2["imdb"])
        if o:
            try:
                m2["imdb_rating"] = float(o.get("imdbRating")) if o.get("imdbRating") not in (None, "N/A") else None
            except ValueError:
                m2["imdb_rating"] = None
            rt = next((x.get("Value") for x in o.get("Ratings", []) if x.get("Source") == "Rotten Tomatoes"), None)
            m2["rt"] = int(rt.rstrip("%")) if rt and rt.rstrip("%").isdigit() else None
            m2["mc"] = int(o["Metascore"]) if str(o.get("Metascore", "")).isdigit() else None
            if o.get("Rated") and o["Rated"] != "N/A":
                m2["rated"] = o["Rated"]
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


def meta_worker():
    """Fills in TMDb/OMDb records for queued titles, one at a time, gently."""
    while True:
        gk = None
        with META_Q_LOCK:
            if META_Q:
                gk = META_Q.pop(0)
        if not gk:
            time.sleep(1.5)
            continue
        g = vod_group(gk)
        if not g:
            continue
        try:
            meta_get(g, fetch=True, details=True)
        except Exception:  # noqa
            pass
        time.sleep(0.35)


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
DEFAULT_PROFILES = {
    "tim": {"name": "Tim", "anchors": [], "genres": [], "avoid_genres": [], "exclude": [], "min_imdb": 7.0, "min_rt": 80,
            "sports": [], "teams": [], "surprise": 2},
    "joanna": {"name": "Joanna", "anchors": [], "genres": [], "avoid_genres": [], "exclude": [], "min_imdb": 7.0, "min_rt": 80,
               "sports": [], "teams": [], "surprise": 1},
    "samen": {"name": "Together", "anchors": [], "genres": [], "avoid_genres": [], "exclude": [], "min_imdb": 7.0, "min_rt": 80,
              "sports": [], "teams": [], "surprise": 1, "together": True},
}


def profiles():
    """Taste profiles: smaak.json (shipped with the package, personal) overlaid with edits saved in Settings."""
    out = json.loads(json.dumps(DEFAULT_PROFILES))
    try:
        with open(TASTE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for pid, p in (data.get("profiles") or {}).items():
            out.setdefault(pid, {}).update(p)
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
    p = ps.get(pid) or ps["tim"]
    p = dict(p)
    p["id"] = pid if pid in ps else "tim"
    p["_lists"] = ps.get("_lists", {})
    return p


def feedback_set(pid, gk, verdict):
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
def lb_import_zip(path=LB_ZIP, pid="tim"):
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
        with urllib.request.urlopen(req, timeout=25) as r:
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
            conn.execute("INSERT INTO lb (profile, norm, year, title, rating, watched, watchlist, tmdb, t) VALUES ('tim',?,?,?,?,1,0,?,?) "
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
        n = conn.execute("SELECT COUNT(*), SUM(watchlist), SUM(CASE WHEN rating>=4.5 OR liked=1 THEN 1 ELSE 0 END) FROM lb WHERE profile='tim'").fetchone()
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
        row = conn.execute("SELECT secs, sessions, pct FROM history WHERE profile=? AND k=?", (pid, gk)).fetchone()
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
                     (pid or "tim", gk, (item or {}).get("kind") or "", (item or {}).get("name") or "", secs, dur, pct, sessions, done, dropped, int(time.time())))
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
    if p["id"] in ("tim", "samen"):
        # Letterboxd: 4.5+ stars and liked films; rotate through a long list so every day leans on different favourites
        loved = sorted([r for r in lb_data("tim") if (r["rating"] or 0) >= 4.5 or r.get("liked")], key=lambda r: (-(r["rating"] or 0), r["title"]))
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
        if r:
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
    hist = history_profile(pid if not p.get("together") else "tim")
    if p.get("together"):
        h2 = history_profile("joanna")
        for k in ("liked", "disliked"):
            for g_, n_ in h2[k].items():
                hist[k][g_] = hist[k].get(g_, 0) + n_
        hist["dropped"] |= h2["dropped"]
    seen |= hist["done"]
    lb_rows = lb_data("tim") if pid in ("tim", "samen") else []
    lb_watch = []
    for r in lb_rows:
        g = vod_find("movie", r["title"], r["year"])
        if g and r["watched"]:
            seen.add(g["key"])
        if r["watchlist"]:
            lb_watch.append((r, g))
    # Together: union of both exclusion lists, and nothing one of them is watching alone
    if p.get("together"):
        ps = profiles()
        for other in ("tim", "joanna"):
            o = ps.get(other, {})
            p["exclude"] = list(set(p.get("exclude") or []) | set(o.get("exclude") or []))
            p["avoid_genres"] = list(set(p.get("avoid_genres") or []) | set(o.get("avoid_genres") or []))
            for k, v in feedback_all(other).items():
                if v == "down":
                    fb.setdefault(k, "down")
        with LOCK:
            for k, pos in STATE["positions"].items():
                if pos and pos.get("gkey") and pos.get("kind") == "series" and pos.get("profile") in ("tim", "joanna"):
                    seen.add(pos["gkey"])
    rows = []
    pending = 0

    # --- Live & upcoming (sport and events)
    try:
        ev = sport_events(p, now)
    except Exception:  # noqa
        ev = []
    if ev:
        rows.append({"id": "live", "title": "Live & upcoming", "kind": "events", "tiles": ev[:10]})

    # --- Continue watching (shared across profiles, series someone else is watching alone stay out of Together)
    cw = []
    with LOCK:
        pos_items = sorted([(k, v) for k, v in STATE["positions"].items() if v], key=lambda kv: -(kv[1].get("updated") or 0))
    for k, v in pos_items[:12]:
        if p.get("together") and v.get("profile") in ("tim", "joanna"):
            continue
        cw.append({"key": k, "kind": v.get("kind"), "title": v.get("name"), "icon": v.get("icon"), "t": v.get("t"), "dur": v.get("dur"),
                   "pos": v, "gkey": v.get("gkey"), "reason": "%d%% watched" % (round(100 * v["t"] / v["dur"]) if v.get("dur") else 0)})
    if cw:
        rows.append({"id": "continue", "title": "Continue watching", "kind": "continue", "tiles": cw})

    # --- Watchlist (Letterboxd + in-app)
    wl = []
    for k, v in fb.items():
        if v in ("watchlist", "together"):
            g = vod_group(k)
            if g and vod_visible(g):
                wl.append(vod_tile(g, "On your watchlist" if v == "watchlist" else "Watch together"))
    for r, g in lb_watch:
        if g and g["key"] not in {t["key"] for t in wl}:
            wl.append(vod_tile(g, "Letterboxd watchlist"))
    if wl:
        rows.append({"id": "watchlist", "title": "Your watchlist", "kind": "tiles", "tiles": wl[:24]})

    has_taste = bool(p.get("anchors") or p.get("genres") or any(r for r in lb_rows if (r["rating"] or 0) >= 4.5) or len(hist["done"]) >= 3)
    if len(hist["done"]) >= 3 and not p.get("anchors"):
        # no written taste yet (Joanna): the finished titles become the anchors
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
        if fy:
            rows.append({"id": "foryou", "title": "For you", "kind": "tiles", "tiles": fy})

    # --- Hot right now + Wildcard
    hot, wild = [], []
    tired = shown_tired(pid)
    pg = set(p.get("genres") or [])
    for x in TREND["items"]:
        g = vod_group(x["key"])
        if not g or not vod_visible(g):
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
    if wild and p.get("surprise", 1) > 0:
        for t in wild[:2]:
            t["reason"] = "Everyone's talking about this · " + t["reason"]
        rows.append({"id": "wild", "title": "Wildcard", "kind": "tiles", "tiles": wild[:2]})
        shown_bump(pid, [t["key"] for t in wild[:2]])

    # --- New this week (new at the provider, filtered on quality where known)
    new = []
    with VOD_LOCK:
        recent = [g for g in VOD["groups"].values() if g["added"] and now - g["added"] < 14 * 86400]
    recent = [g for g in recent if vod_visible(g) and not (g["key"] in fb and fb[g["key"]] in ("down", "seen")) and g["key"] not in seen]
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
DUTCH_NAMES = ["verstappen", "van gerwen", "van veen", "noppert", "verhoeven", "van der poel", "vollering", "kooij", "pogacar", "oranje", "netherlands", "nederland",
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


def sport_events(p, now):
    """Programmes live now or starting soon on sport/event channels, scored for the Live & upcoming row (spoiler-free wording)."""
    with CATALOG_LOCK:
        cats = {c["id"]: c for c in CATALOG["cats"]}
        groups = [g for g in CATALOG["groups"] if any(_SPORT_CAT_RE.search(cats.get(c, {}).get("name", "")) for c in g["cats"])]
    hidden = set(str(c) for c in STATE["settings"].get("hidden_cats") or [])
    groups = [g for g in groups if any(c not in hidden for c in g["cats"])]
    if not groups:
        return []
    teams = [t.lower() for t in (p.get("teams") or [])]
    sports = [s.lower() for s in (p.get("sports") or [])]
    big_only = p.get("together") or p["id"] == "joanna"
    tiles = []
    seen_titles = set()
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
            if any(n in text for n in DUTCH_NAMES):
                s += 30
            if team_hit(text, teams):
                s += 35
            if any(t in text for t in TOURNAMENTS):
                s += 20
            if any(d in text for d in DERBIES):
                s += 20
            if _RECORD_RE.search(text) and pr["start"] > now:
                s += 15
            if pr["start"] <= now <= pr["stop"]:
                s += 10
            if any(sp in text for sp in sports):
                s += 15
            if re.search(r"\b(live)\b", title.lower()):
                s += 5
            if re.search(r"\b(highlights|samenvatting|replay|herhaling|magazine|preview|review|studio|talk|show|journaal|news)\b", text):
                s -= 25
            if s < (45 if big_only else 25):
                continue
            key_t = norm_title(title)
            if key_t in seen_titles:
                continue
            seen_titles.add(key_t)
            g = by_epg[epg_id][0]
            night = 1 <= time.localtime(pr["start"]).tm_hour < 7
            tiles.append({"kind": "event", "title": title, "channel": g["name"], "gkey": g["key"], "icon": g["icon"], "start": pr["start"], "stop": pr["stop"],
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
        if any(n in text for n in DUTCH_NAMES):
            s += 30
        if team_hit(text, teams):
            s += 35
        if any(sp in text for sp in sports):
            s += 15
        if any(t in text for t in TOURNAMENTS):
            s += 20
        if s < (50 if big_only else 30):
            continue
        parts = [x.strip() for x in name.split("|") if x.strip()]
        title = " · ".join(parts[1:3]) if len(parts) > 2 else name
        if norm_title(title) in seen_titles:
            continue
        seen_titles.add(norm_title(title))
        tiles.append({"kind": "event", "title": title, "channel": parts[0] if parts else name, "gkey": g["key"], "icon": g["icon"], "start": 0, "stop": 0,
                      "score": s, "live": "LIVE" in name.upper(), "suggest": "watch", "reason": parts[-1] if len(parts) > 3 else "Event channel"})
    tiles.sort(key=lambda t: (-t["score"], t["start"] or 0))
    return tiles


def is_sport_text(text):
    return bool(re.search(r"\b(nfl|ufc|football|voetbal|soccer|darts|formula|grand prix|f1|tennis|golf|boxing|cycling|wielrennen|eredivisie|premier league|champions league|match|wedstrijd|race|fight|basketball|nba|hockey|cricket|rugby|olympi|tour de france|mma|glory|ppv)\b", (text or "").lower()))


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
    vods = [g for g in vods if vod_visible(g)]
    vods.sort(key=lambda g: (0 if g["norm"] == n else 1, -g["year"], g["title"]))
    movies = [vod_tile(g) for g in vods if g["kind"] == "movie"][:20]
    series = [vod_tile(g) for g in vods if g["kind"] == "series"][:20]
    return {"channels": [{"key": g["key"], "name": g["name"], "icon": g["icon"], "best": g.get("best", ""), "archive": g.get("archive", 0)} for g in chans],
            "programmes": progs, "movies": movies, "series": series}


# ------------------------------------------------------------ opensubtitles ----
_OSUB_TOKEN = {"v": "", "t": 0}


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
        r = conn.execute("SELECT file FROM subs WHERE k=?", (ck,)).fetchone()
    finally:
        conn.close()
    if r:
        return r[0] if r[0] and os.path.exists(r[0]) else None
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
    path = None
    for a in items[:2]:
        fid = a["files"][0].get("file_id")
        try:
            dl = http_json(OSUB_BASE + "/download", hdr, data=json.dumps({"file_id": fid, "sub_format": "srt"}).encode(), method="POST")
            link = dl.get("link")
            if not link:
                log_write("[subs] no link: %s\n" % json.dumps(dl)[:200])
                break
            req = urllib.request.Request(link, headers={"User-Agent": "Beeldbuis v%d" % VERSION})
            with urllib.request.urlopen(req, timeout=30) as r:
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
            if e.code in (406, 429):
                break
        except Exception as e:  # noqa
            log_write("[subs] download failed: %s\n" % e)
    conn = meta_db()
    try:
        conn.execute("INSERT OR REPLACE INTO subs (k, file, src, t) VALUES (?,?,?,?)", (ck, path or "", "opensubtitles", int(time.time())))
        conn.commit()
    finally:
        conn.close()
    return path


def external_subs_for(kind, sid, ext):
    """Try to find English subtitles for a movie/episode that has no English text track. Returns a .vtt path or None."""
    if STATE["settings"].get("subs") is False or STATE["settings"].get("opensubs") is False:
        return None
    if kind == "movie":
        g = None
        with VOD_LOCK:
            for gg in VOD["groups"].values():
                if gg["kind"] == "movie" and any(v["id"] == int(sid) for v in gg["variants"]):
                    g = gg
                    break
        if not g:
            return None
        m = meta_cached(g["key"]) or {}
        return osub_find("movie", g["title"], g["year"], m.get("imdb", ""), cache_key="movie:%s" % g["key"])
    if kind == "series":
        ep = EPISODES.get(str(sid))
        if not ep:
            return None
        g = vod_group(ep["gkey"]) if ep.get("gkey") else None
        m = meta_cached(g["key"]) if g else {}
        return osub_find("series", ep["series"], 0, (m or {}).get("imdb", ""), ep["season"], ep["episode"], cache_key="ep:%s" % sid)
    return None


EPISODES = {}   # episode stream id -> {"series", "season", "episode", "gkey"}: remembered when a series is opened


def remember_episodes(series_name, series_id, info):
    title, year, _, _ = vod_parse(series_name)
    gk = "series:%s|%d" % (norm_title(title), year)
    g = vod_group(gk)
    for season, eps in (info.get("episodes") or {}).items():
        for e in eps or []:
            try:
                EPISODES[str(e.get("id"))] = {"series": title, "season": int(season), "episode": int(e.get("episode_num") or 0), "gkey": g["key"] if g else ""}
            except (TypeError, ValueError):
                continue


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
                for pid in ("tim", "joanna", "samen"):
                    with HOME_LOCK:
                        stale = pid not in HOME or time.time() - HOME[pid]["t"] > HOME_TTL
                    if stale and VOD["n"]:
                        home_get(pid, force=True)
            except Exception:  # noqa
                pass
        time.sleep(600)


# --------------------------------------------------------------- handler ----
# ---------------------------------------------------------- phone access ----
TAKEN = {}            # device id -> {"by": name, "t": time} : its stream was replaced by another device's
DIRECT = {}           # device id -> {"n": open direct (mp4) streams, "stop": generation}
PAIR_FAILS = []       # times of wrong pairing codes (rate limit)
PUBLIC_GET = ("/", "/manifest.json", "/favicon.ico", "/icon-192.png", "/icon-512.png")
MAC_ONLY = ("/api/login", "/api/logout", "/api/quit", "/api/open-window", "/api/tv/choose-folder", "/api/tv/export",
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
    """Bonjour name of the Mac (e.g. Tims-MacBook-Pro.local): stays the same when the IP address changes."""
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
        return json.loads(self.rfile.read(n).decode("utf-8"))

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
            return True
        self.remote = True
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
    def do_GET(self):
        LAST_REQ["t"] = time.time()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        q = lambda k, d=None: qs.get(k, [d])[0]  # noqa: E731
        if not self.gate(path, qs):
            return

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
            return self.send_json({"ok": True, "ffmpeg": bool(FFMPEG), "v": VERSION, "rec": rec_status(), "now": now,
                                   "taken": taken.get(self.dev[0]), "me": self.client()})
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
            return self.api(lambda: dict(home_get(q("profile", "tim"), force=q("force") == "1"), img_base=TMDB_IMG))
        if path == "/api/library":
            return self.api(lambda: vod_list(q("kind", "movie"), q("cat", ""), q("q", ""), int(q("limit", "400") or 400), int(q("offset", "0") or 0)))
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
        if path == "/api/stop":
            stop_all_sessions(owner=self.dev[0])     # only this device's own stream
            return self.send_json({"ok": True})
        if path == "/api/phone":
            return self.api(phone_info)
        if path == "/api/recordings":
            return self.api(rec_list)
        if path == "/api/tv":
            return self.api(tv_status)
        if path == "/api/positions":
            with LOCK:
                pos = dict(STATE["positions"])
            return self.send_json({"ok": True, "data": pos})
        if path.startswith("/direct/rec/"):
            return self.rec_file(path)
        if path.startswith("/hls/"):
            return self.hls_file(path)
        if path.startswith("/direct/"):
            return self.stream(path, qs, direct=True)
        self.send_error(404)

    def do_POST(self):
        LAST_REQ["t"] = time.time()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if not self.gate(path, urllib.parse.parse_qs(parsed.query), "POST"):
            return
        if path == "/api/pair":
            return self.pair_api()
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
            with LOCK:
                STATE["server"] = STATE["username"] = STATE["password"] = ""
            save_config()
            return self.send_json({"ok": True})
        if path == "/api/settings":
            body = self.read_json_body()
            with LOCK:
                STATE["settings"].update(body or {})
            save_config()
            tv_schedule("settings")
            return self.send_json({"ok": True})
        if path == "/api/record":
            return self.api(lambda: rec_schedule(self.read_json_body()))
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
                    history_note(body.get("profile") or (hist_item or {}).get("profile") or "tim", key, hist_item, removed=bool(body.get("remove")))
                except Exception:  # noqa
                    pass
            return self.send_json({"ok": True})
        if path == "/api/feedback":
            body = self.read_json_body()
            feedback_set(body.get("profile") or "tim", body.get("key") or "", body.get("verdict"))
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
                return home_get((body or {}).get("profile") or "tim", force=True)
            return self.api(_hr)
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
            with LOCK:
                STATE["favorites"] = body.get("favorites", [])
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
        info = xtream()
        ui = info.get("user_info", {}) if isinstance(info, dict) else {}
        return {
            "logged_in": True, "ffmpeg": bool(FFMPEG),
            "server": server, "username": user,
            "status": ui.get("status"), "exp_date": ui.get("exp_date"),
            "max_connections": ui.get("max_connections"), "active_cons": ui.get("active_cons"),
            "catalog": {"t": CATALOG["t"], "status": CATALOG["status"], "n": len(CATALOG["groups"])},
            "epg": epg_status(), "version": VERSION, "client": self.client(),
        }

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
        with LOCK:
            STATE["server_tz"] = si.get("timezone") or DEFAULT_TZ
        save_config()
        PROBE_CACHE.clear()
        threading.Thread(target=lambda: (catalog_refresh(force=True), epg_refresh(force=True)), daemon=True).start()
        return self.status()

    def epg(self, sid):
        g = stream_group(sid)
        if not g or not g.get("epg"):
            return []
        now = int(time.time())
        return epg_rows([g["epg"]], now - 3600, now + 12 * 3600).get(g["epg"], [])

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
        info = probe(target)
        if info.get("error"):
            raise RuntimeError("Stream not available: " + info["error"])
        return info

    # -- streaming
    def play_api(self, kind, sid, ext, start, force, cu_start=0, cu_dur=0, native=False):
        if kind not in ("live", "movie", "series", "catchup", "rec") or not sid or not FFMPEG:
            raise RuntimeError("Invalid stream")
        follow = False
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
            info = PROBE_CACHE.get(target) or probe(target)
        if info.get("error"):
            raise RuntimeError("Stream not available: " + info["error"])
        ext_sub = None
        if kind in ("movie", "series") and info.get("sub") is None:
            try:
                ext_sub = external_subs_for(kind, sid, ext)
            except Exception as e:  # noqa
                log_write("[subs] %s\n" % e)
        if info.get("mode") == "direct" and not force and kind != "live":
            out = {"playlist": None, "info": info, "direct": "/direct/%s/%s.%s" % (kind, sid, ext or "mp4"), "offset": 0}
            if ext_sub:
                out["subs"] = "/subs/" + os.path.basename(ext_sub)
                info["ext_sub"] = True
            return out
        sess, pl, err = start_session(target, info, start, force, live=(kind == "live" or follow), follow=follow, owner=self.dev,
                                      min_segs=3 if native and (kind == "live" or follow) else 1, ext_sub=ext_sub)
        if err:
            raise RuntimeError(err)
        if ext_sub:
            info["ext_sub"] = True
        return {"playlist": "/hls/%s/%s" % (sess, pl), "info": info, "offset": start}

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
            a, _, b = rng[6:].partition("-")
            if a:
                start = int(a)
                if b:
                    end = min(int(b), size - 1)
            elif b:
                start = max(0, size - int(b))
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

    def hls_file(self, path):
        parts = path.split("/")
        if len(parts) != 4 or ".." in path:
            return self.send_error(404)
        sid, name = parts[2], parts[3]
        sess = SESSIONS.get(sid)
        if not sess:
            return self.send_error(404)
        sess["t"] = time.time()
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
    catalog_load()
    rec_load()
    threading.Thread(target=vod_load, daemon=True).start()
    try:
        epg_init()
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
    threading.Thread(target=meta_worker, daemon=True).start()
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
