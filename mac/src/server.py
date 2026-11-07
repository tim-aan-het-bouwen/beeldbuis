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
HOST = "127.0.0.1"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Beeldbuis/3.0"
AUTO_EXIT = "--auto-exit" in ARGS
VERSION = 4

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

STATE = {"server": "", "username": "", "password": "", "favorites": [], "recent": [], "settings": {}, "positions": {}, "server_tz": ""}
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


def probe(target, cache=True):
    """Quick look at the codecs so we convert as little as possible (and find English subtitles)."""
    if cache and target in PROBE_CACHE:
        return PROBE_CACHE[target]
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
        info["error"] = last[-1][:200] if last else "Stream not reachable"
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
    if not info["error"] and cache:
        PROBE_CACHE[target] = info
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


def stop_all_sessions(remote_only=False):
    for sid, sess in list(SESSIONS.items()):
        if remote_only and sess.get("local"):
            continue
        stop_session(sid)


def start_session(target, info, start, force, live, follow=False):
    """Start ffmpeg writing HLS segments; returns (sid, playlist, error) once the playlist is usable."""
    stop_all_sessions()                 # one stream at a time (the subscription allows one connection)
    sid = "%d" % int(time.time() * 1000)
    outdir = os.path.join(HLS_DIR, sid)
    os.makedirs(outdir, exist_ok=True)
    cmd = ffmpeg_cmd(target, info, outdir, start, force, live, follow)
    logf = open(LOG_PATH, "ab")
    logf.write(("\n--- %s %s\n%s\n" % (time.strftime("%H:%M:%S"), sid, " ".join(cmd[:-12]))).encode())
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=logf, stdin=subprocess.DEVNULL)
    PROCS.add(proc)
    with SESS_LOCK:
        SESSIONS[sid] = {"proc": proc, "dir": outdir, "t": time.time(), "local": is_local(target)}
    use_sub = info.get("sub") is not None and not live
    pl = os.path.join(outdir, "index.m3u8")
    if use_sub:
        # our own master playlist: video/audio + the English WebVTT track that ffmpeg writes next to it
        with open(os.path.join(outdir, "master.m3u8"), "w") as f:
            f.write("#EXTM3U\n#EXT-X-VERSION:7\n"
                    "#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID=\"subs\",NAME=\"English\",LANGUAGE=\"en\",DEFAULT=YES,AUTOSELECT=YES,URI=\"index_vtt.m3u8\"\n"
                    "#EXT-X-STREAM-INF:BANDWIDTH=12000000,SUBTITLES=\"subs\"\nindex.m3u8\n")
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            txt = open(pl).read()
            if txt.count(".m4s") >= 1 and (not use_sub or os.path.exists(os.path.join(outdir, "index_vtt.m3u8"))):
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
def server_tz():
    """Provider's server time zone (xtream server_info.timezone), default UTC."""
    with LOCK:
        name = STATE.get("server_tz") or "UTC"
    try:
        import zoneinfo
        return zoneinfo.ZoneInfo(name)
    except Exception:  # noqa
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
    logf = open(LOG_PATH, "ab")
    logf.write(("\n--- %s REC%s %s -> %s\n" % (time.strftime("%H:%M:%S"), " restart" if append else "", rec["id"], rec["file"])).encode())
    out = open(rec["file"], "ab" if append else "wb")
    proc = subprocess.Popen(cmd, stdout=out, stderr=logf, stdin=subprocess.PIPE)
    out.close()
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


# --------------------------------------------------------------- handler ----
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if len(body) > 50000 and "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 5)
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
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

    # -- routing
    def do_GET(self):
        LAST_REQ["t"] = time.time()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        qs = urllib.parse.parse_qs(parsed.query)
        q = lambda k, d=None: qs.get(k, [d])[0]  # noqa: E731

        if path == "/":
            return self.send_html(HTML)
        if path == "/manifest.json":
            body = json.dumps({"name": "Beeldbuis", "short_name": "Beeldbuis", "start_url": "/", "scope": "/",
                               "display": "standalone", "background_color": "#0e0f12", "theme_color": "#0e0f12",
                               "icons": [{"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
                                         {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/manifest+json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body); return
        if path.startswith("/assets/") or path in ("/icon-192.png", "/icon-512.png", "/favicon.ico"):
            return self.asset(path)
        if path == "/api/ping":
            LAST_PING["t"] = time.time()
            return self.send_json({"ok": True, "ffmpeg": bool(FFMPEG), "v": VERSION, "rec": rec_status()})
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
            return self.api(lambda: xtream("get_series_info", series_id=q("id")))
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
                                                  int(q("start", "0") or 0), int(q("dur", "0") or 0)))
        if path == "/api/stop":
            stop_all_sessions()
            return self.send_json({"ok": True})
        if path == "/api/recordings":
            return self.api(rec_list)
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
        path = urllib.parse.urlsplit(self.path).path
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
                        STATE["positions"].pop(key, None)
                    else:
                        item = dict(body.get("item") or {})
                        item["updated"] = int(time.time())
                        STATE["positions"][key] = item
                        if len(STATE["positions"]) > 200:   # keep the list short
                            for k in sorted(STATE["positions"], key=lambda k: STATE["positions"][k].get("updated", 0))[:-200]:
                                STATE["positions"].pop(k, None)
                save_config()
            return self.send_json({"ok": True})
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
            return self.send_json({"ok": True})
        if path == "/api/quit":
            self.send_json({"ok": True})
            threading.Timer(0.3, shutdown).start()
            return
        self.send_error(404)

    # -- api implementations
    def status(self):
        with LOCK:
            if not STATE["server"]:
                return {"logged_in": False, "ffmpeg": bool(FFMPEG)}
            server, user = STATE["server"], STATE["username"]
        info = xtream()
        ui = info.get("user_info", {}) if isinstance(info, dict) else {}
        return {
            "logged_in": True, "ffmpeg": bool(FFMPEG),
            "server": server, "username": user,
            "status": ui.get("status"), "exp_date": ui.get("exp_date"),
            "max_connections": ui.get("max_connections"), "active_cons": ui.get("active_cons"),
            "catalog": {"t": CATALOG["t"], "status": CATALOG["status"], "n": len(CATALOG["groups"])},
            "epg": epg_status(), "version": VERSION,
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
            STATE["server_tz"] = si.get("timezone") or "UTC"
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
    def play_api(self, kind, sid, ext, start, force, cu_start=0, cu_dur=0):
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
                skip_min = int(start // 60)
                target = timeshift_url(sid, cu_start + skip_min * 60, max(1, cu_dur - skip_min))
                info = dict(probe(target))
                if info.get("error"):
                    raise RuntimeError("Catch-up not available: " + info["error"])
                info["mode"] = "copy" if info.get("vcodec") in COPY_VIDEO else "transcode"
                info["duration"] = cu_dur * 60
                sess, pl, err = start_session(target, info, start - skip_min * 60, force, live=False)
                if err:
                    raise RuntimeError(err)
                return {"playlist": "/hls/%s/%s" % (sess, pl), "info": info, "offset": start}
            target = stream_url(kind, sid, ext or ("ts" if kind == "live" else "mp4"))
            info = PROBE_CACHE.get(target) or probe(target)
        if info.get("error"):
            raise RuntimeError("Stream not available: " + info["error"])
        if info.get("mode") == "direct" and not force and kind != "live":
            return {"playlist": None, "info": info, "direct": "/direct/%s/%s.%s" % (kind, sid, ext or "mp4"), "offset": 0}
        sess, pl, err = start_session(target, info, start, force, live=(kind == "live" or follow), follow=follow)
        if err:
            raise RuntimeError(err)
        return {"playlist": "/hls/%s/%s" % (sess, pl), "info": info, "offset": start}

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
        return self.proxy(stream_url(kind, name, ext))

    def proxy(self, target):
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
                if not chunk:
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


def shutdown():
    kill_all()
    os._exit(0)


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
    catalog_load()
    rec_load()
    try:
        epg_init()
    except Exception as e:  # noqa
        print("EPG database could not be opened: %s" % e)
    try:
        HTTPD = ThreadingHTTPServer((HOST, PORT), Handler)
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
    shutil.rmtree(HLS_DIR, ignore_errors=True)
    try:
        HTTPD.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        kill_all()


if __name__ == "__main__":
    main()
