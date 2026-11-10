#!/usr/bin/env python3
"""Fake Xtream provider for Beeldbuis regression tests (white label: no host or login in here).

* player_api.php / xmltv.php: forwarded to your own Xtream server once and cached (env BEELDBUIS_UPSTREAM =
  "http://host" plus BEELDBUIS_UP_USER / BEELDBUIS_UP_PASS), or served from the cache directory (env FAKE_CACHE).
  Without upstream and cache a small synthetic catalogue is used, so the tests also run offline.
* Streams are generated locally with ffmpeg (live, timeshift, movie, series).
* Behaves like a 1-connection subscription: a new stream connection silently ends the oldest one (EOF) after ~1 s,
  like the real provider does. Every stream connection is logged, so tests can assert the app never needed two.
* Fault injection through GET /__ctl?...:
    fail=N        the next N stream opens answer 503
    stall=S       open live streams stop sending data for S seconds (connection stays open, like a dead server)
    drop=1        end every open stream now (EOF)
    reset=1       clear the connection log and faults
  GET /__log returns the connection log as JSON.
"""
import hashlib, json, os, re, subprocess, sys, threading, time, urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.environ.get("FAKE_CACHE") or os.path.join(HERE, "fakecache")
UP = os.environ.get("BEELDBUIS_UPSTREAM", "").rstrip("/")
UP_USER = os.environ.get("BEELDBUIS_UP_USER", "")
UP_PASS = os.environ.get("BEELDBUIS_UP_PASS", "")
MEDIA = os.environ.get("FAKE_MEDIA") or os.path.join(HERE, "media")
os.makedirs(CACHE, exist_ok=True)


def make_media():
    """Two 3-minute test films, made once: movie.mkv (h264/aac + English subtitle track) and movie.mp4 (no subtitles)."""
    os.makedirs(MEDIA, exist_ok=True)
    srt = os.path.join(MEDIA, "sub.srt")
    if not os.path.exists(srt):
        open(srt, "w").write("1\n00:00:01,000 --> 00:00:05,000\nHello from the test film.\n\n2\n00:00:30,000 --> 00:00:40,000\nThirty seconds in.\n")
    src = ["-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=frequency=330"]
    enc = ["-t", "180", "-c:v", "libx264", "-preset", "veryfast", "-crf", "30", "-g", "50", "-c:a", "aac"]
    mkv, mp4 = os.path.join(MEDIA, "movie.mkv"), os.path.join(MEDIA, "movie.mp4")
    if not os.path.exists(mkv):
        subprocess.run(["ffmpeg", "-v", "error", "-y"] + src + ["-i", srt, "-map", "0", "-map", "1", "-map", "2"] + enc +
                       ["-c:s", "srt", "-metadata:s:s:0", "language=eng", "-metadata:s:a:0", "language=eng", mkv], check=True)
    if not os.path.exists(mp4):
        subprocess.run(["ffmpeg", "-v", "error", "-y"] + src + enc + ["-movflags", "+faststart", mp4], check=True)

LIVE = ["ffmpeg", "-v", "error", "-nostdin", "-re", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25", "-f", "lavfi",
        "-i", "sine=frequency=440", "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "50", "-crf", "32",
        "-c:a", "aac", "-f", "mpegts", "pipe:1"]

# files the provider lists but cannot serve: 903 (American Psycho, its 4K edition 904 works) and 906 are gone (500),
# 905 is listed as .mp4 but only exists as .mkv (get_vod_info knows the real extension)
DEAD = {"903", "906", "7002"}
ONLY_EXT = {"905": "mkv"}
STATE = {"fail": 0, "stall_until": 0.0, "drop_gen": 0, "conns": [], "log": [], "limit": int(os.environ.get("FAKE_LIMIT", "1"))}
LK = threading.Lock()


def synthetic(action, q):
    now = int(time.time())
    if action == "":
        return {"user_info": {"auth": 1, "status": "Active", "max_connections": "1", "active_cons": "0", "exp_date": str(now + 86400 * 300),
                              "username": "test"}, "server_info": {"timezone": "UTC", "url": "localhost"}}
    if action == "get_live_categories":
        return [{"category_id": "1", "category_name": "NL| GENERAL"}, {"category_id": "2", "category_name": "UK| SPORT"}]
    if action == "get_live_streams":
        return [{"stream_id": 100 + i, "name": n, "category_id": c, "epg_channel_id": e, "tv_archive": 1, "tv_archive_duration": 3,
                 "stream_icon": ""} for i, (n, c, e) in enumerate([("NL| NPO 1 FHD", "1", "npo1.nl"), ("NL| NPO 1 HD", "1", "npo1.nl"),
                                                                   ("NL| NPO 2 FHD", "1", "npo2.nl"), ("UK| SKY SPORTS F1 FHD", "2", "skyf1.uk")])]
    if action in ("get_vod_categories", "get_series_categories"):
        return [{"category_id": "10", "category_name": "EN - MOVIES"}]
    if action == "get_vod_streams":
        return [{"stream_id": 900 + i, "name": "EN - %s (%d)" % (t, y), "category_id": "10", "container_extension": ext, "rating": "7.5",
                 "added": str(now - 86400 * i), "stream_icon": ""} for i, (t, y, ext) in
                enumerate([("Interstellar", 2014, "mkv"), ("The Grand Budapest Hotel", 2014, "mp4"), ("Pulp Fiction", 1994, "mkv"),
                           ("American Psycho", 2000, "mkv"), ("American Psycho 4K", 2000, "mp4"), ("Fargo", 1996, "mp4"),
                           ("Gone Film", 1999, "mkv")])]
    if action == "get_series":
        return [{"series_id": 500 + i, "name": "EN - Test Show" + x, "category_id": "10", "rating": "8", "cover": "", "plot": "A show.", "genre": "Drama",
                 "releaseDate": "2020-01-01"} for i, x in enumerate(["", " 4K"])]
    if action == "get_series_info":
        # two editions of the same show; episode 2 of the first edition (7002) is broken at the provider, 7102 works
        base = 7000 if q.get("series_id", ["500"])[0] == "500" else 7100
        return {"info": {"name": "Test Show"}, "episodes": {"1": [{"id": str(base + n), "episode_num": n, "title": "Episode %d" % n, "container_extension": "mkv",
                                                                     "season": 1, "info": {"duration_secs": 180}} for n in (1, 2)]}}
    if action == "get_vod_info":
        vid = q.get("vod_id", [""])[0]
        return {"info": {"duration_secs": 180}, "movie_data": {"stream_id": vid, "container_extension": ONLY_EXT.get(vid, "")}}
    if action == "get_short_epg":
        return {"epg_listings": []}
    return []


def synthetic_xmltv():
    now = int(time.time()) // 600 * 600
    out = ['<?xml version="1.0" encoding="utf-8"?><tv>']
    for cid, name in (("npo1.nl", "NPO 1"), ("npo2.nl", "NPO 2"), ("skyf1.uk", "Sky Sports F1")):
        out.append('<channel id="%s"><display-name>%s</display-name></channel>' % (cid, name))
        for k in range(-36, 36):
            a, b = now + k * 1200, now + (k + 1) * 1200
            f = lambda t: time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))
            out.append('<programme start="%s" stop="%s" channel="%s"><title>%s show %d</title><desc>Test programme.</desc></programme>'
                       % (f(a), f(b), cid, name, k))
    out.append("</tv>")
    return "\n".join(out).encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/__ctl":
            return self.ctl(q)
        if u.path == "/__log":
            with LK:
                return self.send_bytes(json.dumps({"log": STATE["log"], "open": len(STATE["conns"])}).encode(), "application/json")
        if u.path == "/player_api.php":
            return self.player_api(q)
        if u.path == "/xmltv.php":
            return self.xmltv()
        m = re.match(r"^/live/[^/]+/[^/]+/(\d+)\.(ts|m3u8)$", u.path)
        if m:
            return self.stream("live", m.group(1), LIVE)
        m = re.match(r"^/timeshift/[^/]+/[^/]+/(\d+)/([\d:-]+)/(\d+)\.ts$", u.path)
        if m:
            secs = min(int(m.group(1)) * 60, int(os.environ.get("FAKE_TS_MAX", "240")))
            cmd = [c for c in LIVE if c != "-re"]
            cmd[cmd.index("-f"):cmd.index("-f")] = ["-re", "-t", str(secs)] if os.environ.get("FAKE_TS_RT") else ["-t", str(secs)]
            return self.stream("timeshift", m.group(3), cmd)
        m = re.match(r"^/(movie|series)/[^/]+/[^/]+/(\d+)\.(\w+)$", u.path)
        if m:
            if m.group(2) in DEAD:
                with LK:
                    STATE["log"].append({"t": time.time(), "ev": "500", "kind": m.group(1), "sid": m.group(2)})
                return self.send_error(500)
            if m.group(2) in ONLY_EXT and m.group(3) != ONLY_EXT[m.group(2)]:
                return self.send_error(404)
            fp = os.path.join(MEDIA, "movie.mp4" if m.group(3) == "mp4" else "movie.mkv")
            return self.send_file(m.group(1), m.group(2), fp)
        self.send_error(404)

    # ---- control
    def ctl(self, q):
        with LK:
            if "fail" in q:
                STATE["fail"] = int(q["fail"][0])
            if "stall" in q:
                STATE["stall_until"] = time.time() + float(q["stall"][0])
            if "drop" in q:
                STATE["drop_gen"] += 1
            if "limit" in q:
                STATE["limit"] = int(q["limit"][0])
            if "reset" in q:
                STATE.update(fail=0, stall_until=0.0, log=[])
        return self.send_bytes(b'{"ok":true}', "application/json")

    # ---- metadata
    def player_api(self, q):
        action = q.get("action", [""])[0]
        params = {k: v[0] for k, v in q.items() if k not in ("username", "password")}
        key = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
        fp = os.path.join(CACHE, "api_%s_%s.json" % (action or "info", key))
        data = None
        if action == "":
            data = json.dumps(synthetic("", q)).encode()     # account info: never cached from upstream (it holds the login)
        elif os.path.exists(fp):
            data = open(fp, "rb").read()
        elif UP:
            qq = dict(params, username=UP_USER, password=UP_PASS)
            try:
                req = urllib.request.Request(UP + "/player_api.php?" + urllib.parse.urlencode(qq), headers={"User-Agent": "Mozilla/5.0"})
                data = urllib.request.urlopen(req, timeout=180).read()
                json.loads(data)
                open(fp, "wb").write(data)
            except Exception as e:  # noqa
                sys.stderr.write("[fake] upstream %s failed: %s\n" % (action, e))
                data = None
        if data is None:
            data = json.dumps(synthetic(action, q)).encode()
        self.send_bytes(data, "application/json")

    def xmltv(self):
        fp = os.path.join(CACHE, "xmltv.xml")
        if not os.path.exists(fp) and UP:
            try:
                qq = urllib.parse.urlencode({"username": UP_USER, "password": UP_PASS})
                req = urllib.request.Request(UP + "/xmltv.php?" + qq, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=300) as r, open(fp + ".tmp", "wb") as f:
                    while True:
                        b = r.read(1 << 20)
                        if not b:
                            break
                        f.write(b)
                os.replace(fp + ".tmp", fp)
            except Exception as e:  # noqa
                sys.stderr.write("[fake] xmltv failed: %s\n" % e)
        if os.path.exists(fp):
            return self.send_path(fp, "application/xml")
        return self.send_bytes(synthetic_xmltv(), "application/xml")

    def send_bytes(self, data, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_path(self, fp, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(os.path.getsize(fp)))
        self.end_headers()
        with open(fp, "rb") as f:
            while True:
                b = f.read(1 << 20)
                if not b:
                    break
                self.wfile.write(b)

    # ---- streams (1 connection)
    def open_conn(self, kind, sid):
        with LK:
            if STATE["fail"] > 0:
                STATE["fail"] -= 1
                STATE["log"].append({"t": time.time(), "ev": "503", "kind": kind, "sid": sid})
                return None
            c = {"id": len(STATE["log"]) + 1, "kind": kind, "sid": sid, "t": time.time(), "kick": False, "drop": STATE["drop_gen"]}
            STATE["conns"].append(c)
            over = len(STATE["conns"]) - STATE["limit"]
            STATE["log"].append({"t": c["t"], "ev": "open", "kind": kind, "sid": sid, "id": c["id"], "concurrent": len(STATE["conns"])})
            if over > 0:
                for old in STATE["conns"][:over]:
                    old["kick_at"] = time.time() + 1.0       # the provider ends the oldest stream a moment later
        return c

    def close_conn(self, c, why):
        with LK:
            if c in STATE["conns"]:
                STATE["conns"].remove(c)
            STATE["log"].append({"t": time.time(), "ev": "close", "why": why, "kind": c["kind"], "sid": c["sid"], "id": c["id"]})

    def should_end(self, c):
        if c.get("kick_at") and time.time() >= c["kick_at"]:
            return "kicked"
        if c["drop"] != STATE["drop_gen"]:
            return "dropped"
        return None

    def stream(self, kind, sid, cmd):
        c = self.open_conn(kind, sid)
        if c is None:
            return self.send_error(503)
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        why = "client"
        try:
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                w = self.should_end(c)
                if w:
                    why = w
                    break
                if time.time() < STATE["stall_until"] and kind == "live":
                    time.sleep(0.2)
                    continue
                b = p.stdout.read1(32768)
                if not b:
                    why = "eof"
                    break
                self.wfile.write(b)
        except Exception:  # noqa
            pass
        finally:
            p.kill()
            p.wait()
            self.close_conn(c, why)

    def send_file(self, kind, sid, fp):
        c = self.open_conn(kind, sid)
        if c is None:
            return self.send_error(503)
        why = "client"
        try:
            size = os.path.getsize(fp)
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                if a:
                    start = int(a)
                    if b:
                        end = min(int(b), size - 1)
            self.send_response(206 if rng else 200)
            self.send_header("Content-Type", "video/mp4" if fp.endswith(".mp4") else "video/x-matroska")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if rng:
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
            self.end_headers()
            with open(fp, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0:
                    w = self.should_end(c)
                    if w:
                        why = w
                        break
                    b = f.read(min(65536, left))
                    if not b:
                        break
                    self.wfile.write(b)
                    left -= len(b)
                else:
                    why = "eof"
        except Exception:  # noqa
            pass
        finally:
            self.close_conn(c, why)


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9911
    make_media()
    ThreadingHTTPServer.daemon_threads = True
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    sys.stderr.write("fake provider on %d (upstream: %s)\n" % (port, "yes" if UP else "no"))
    srv.serve_forever()
