#!/usr/bin/env python3
"""Browser preview for the Beeldbuis TV app.

    python3 tv/tools/serve.py [--port 8080]

- serves tv/app/ at http://localhost:8080
- /proxy?u=URL   fetches a provider URL for the preview (Xtream servers send no CORS headers; the TV app
                 itself calls the provider directly). HLS playlists are rewritten so segments also go through it.
- /demo/...      a small fake Xtream provider: sign in with server "demo" to try the app without a subscription.
                 Test video is made with ffmpeg on first use (tv/test/media/, not committed).
"""
import json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
MEDIA = os.path.join(os.path.dirname(HERE), "test", "media")
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8080

DEMO_CATS = [("1", "NL| GENERAL"), ("2", "NL| SPORT"), ("3", "UK| SPORTS"), ("4", "US| NEWS"), ("5", "4K| UHD")]
DEMO_CHANNELS = [
    ("1", "NL| NPO 1 HD"), ("1", "NL| NPO 1 FHD"), ("1", "NL| NPO 2 HD"), ("1", "NL| RTL 4 ᴴᴰ"), ("1", "NL| SBS 6"),
    ("2", "NL| SPORT 1 FHD"), ("2", "NL| SPORT 2 HD"), ("2", "NL| SPORT EXTRA 4K"), ("2", "NL| SPORT NEWS"),
    ("3", "UK| SPORTS MAIN UHD"), ("3", "UK| SPORTS MAIN HD"), ("3", "UK| SPORTS ACTION"), ("3", "UK| SPORTS ARENA"),
    ("4", "US| CNN"), ("4", "US| ESPN"), ("5", "4K| NATURE UHD"),
]
DEMO_MOVIES = [("10", "EN - The Long Night (2024)", "7.9"), ("10", "EN - Harbour Lights (2023)", "6.8"), ("11", "4K - Northern Sky (2025)", "8.1"),
               ("10", "EN - Paper Moons (2022)", "7.2"), ("11", "4K - The Quiet Coast (2025)", "7.5")]
DEMO_SERIES = [("20", "Blue Fox Samurai", "8.6", "A swordsman hunts four men across Edo."), ("20", "Pluto Station", "8.3", "One crew, one station, nobody else.")]


def ensure_media():
    os.makedirs(MEDIA, exist_ok=True)
    mp4 = os.path.join(MEDIA, "movie.mp4")
    hls = os.path.join(MEDIA, "live", "index.m3u8")
    if not os.path.exists(mp4):
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi", "-i",
                        "sine=frequency=330", "-t", "120", "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-movflags", "+faststart", mp4], check=True)
    if not os.path.exists(hls):
        os.makedirs(os.path.dirname(hls), exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", mp4, "-c", "copy", "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                        "-hls_playlist_type", "vod", hls], check=True)
    return mp4, hls


def logo_svg(text):
    t = re.sub(r"[^A-Za-z0-9 ]", "", text)[:10]
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="220" height="110"><rect width="220" height="110" rx="14" fill="#1D9E99" stroke="#000" stroke-width="6"/>'
            '<text x="110" y="68" font-family="Arial" font-weight="bold" font-size="30" fill="#F4E8C8" text-anchor="middle">%s</text></svg>' % t)


def poster_svg(text):
    t = re.sub(r"[^A-Za-z0-9 ]", "", text)[:16]
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="300" height="440"><rect width="300" height="440" fill="#E07A2F"/>'
            '<circle cx="150" cy="170" r="90" fill="#F0B429" stroke="#000" stroke-width="8"/>'
            '<text x="150" y="360" font-family="Arial" font-weight="bold" font-size="30" fill="#0E0F12" text-anchor="middle">%s</text></svg>' % t)


class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=APP, **k)

    def log_message(self, fmt, *args):
        sys.stderr.write("[serve] %s\n" % (fmt % args)[:160])

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/proxy":
            return self.proxy(urllib.parse.parse_qs(u.query).get("u", [""])[0])
        if u.path.startswith("/demo/"):
            return self.demo(u)
        return super().do_GET()

    # ---------- preview proxy ----------
    def proxy(self, url):
        if not re.match(r"^https?://", url):
            return self.send(400, "bad url", "text/plain")
        headers = {"User-Agent": "Mozilla/5.0 Beeldbuis-preview"}
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30)
        except urllib.error.HTTPError as e:
            return self.send(e.code, "upstream %d" % e.code, "text/plain")
        except Exception as e:
            return self.send(502, "upstream error: %s" % e, "text/plain")
        ctype = r.headers.get("Content-Type", "application/octet-stream")
        final = r.geturl()
        if final.split("?")[0].endswith(".m3u8") or "mpegurl" in ctype.lower():
            text = r.read().decode("utf-8", "replace")
            out = []
            for line in text.splitlines():
                if line and not line.startswith("#"):
                    line = "/proxy?u=" + urllib.parse.quote(urllib.parse.urljoin(final, line), safe="")
                elif 'URI="' in line:
                    line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="/proxy?u=%s"' % urllib.parse.quote(urllib.parse.urljoin(final, m.group(1)), safe=""), line)
                out.append(line)
            return self.send(200, "\n".join(out) + "\n", "application/vnd.apple.mpegurl")
        self.send_response(r.status)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if r.headers.get(h):
                self.send_header(h, r.headers[h])
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            shutil.copyfileobj(r, self.wfile, 64 * 1024)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---------- demo provider ----------
    def demo(self, u):
        p = u.path[len("/demo"):]
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        if p == "/player_api.php":
            if q.get("username") != "demo" or q.get("password") != "demo":
                return self.send(200, json.dumps({"user_info": {"auth": 0}}), "application/json")
            return self.send(200, json.dumps(self.demo_api(q.get("action"), q)), "application/json")
        if p.startswith("/logo/"):
            return self.send(200, logo_svg(urllib.parse.unquote(p[6:])), "image/svg+xml")
        if p.startswith("/poster/"):
            return self.send(200, poster_svg(urllib.parse.unquote(p[8:])), "image/svg+xml")
        mp4, hls = ensure_media()
        if re.match(r"^/live/demo/demo/\d+\.(m3u8|ts)$", p):
            if p.endswith(".ts"):
                return self.send(404, "demo serves HLS only", "text/plain")
            text = open(hls).read().replace("index", "/demo/seg/index")
            return self.send(200, text, "application/vnd.apple.mpegurl")
        m = re.match(r"^/seg/(index\d+\.ts)$", p)
        if m:
            return self.send(200, open(os.path.join(MEDIA, "live", m.group(1)), "rb").read(), "video/mp2t")
        if re.match(r"^/(movie|series)/demo/demo/\d+\.\w+$", p):
            self.path = "/movie.mp4"
            self.directory = MEDIA
            return SimpleHTTPRequestHandler.do_GET(self)
        return self.send(404, "not found", "text/plain")

    def translate_path(self, path):
        if getattr(self, "directory", None) == MEDIA:
            return os.path.join(MEDIA, os.path.basename(path))
        return super().translate_path(path)

    def demo_api(self, action, q):
        base = "http://%s/demo" % self.headers.get("Host", "localhost:%d" % PORT)
        if not action:
            return {"user_info": {"auth": 1, "status": "Active", "active_cons": "0", "max_connections": "1", "exp_date": str(int(time.time()) + 86400 * 300)},
                    "server_info": {"timezone": "UTC"}}
        if action == "get_live_categories":
            return [{"category_id": c, "category_name": n} for c, n in DEMO_CATS]
        if action == "get_live_streams":
            return [{"stream_id": i + 1, "name": n, "category_id": c, "stream_icon": base + "/logo/" + urllib.parse.quote(n.split("|")[-1].strip()),
                     "epg_channel_id": "", "tv_archive": 1 if c == "2" else 0, "tv_archive_duration": 2}
                    for i, (c, n) in enumerate(DEMO_CHANNELS) if c == q.get("category_id")]
        if action == "get_short_epg":
            now = int(time.time()) // 1800 * 1800
            import base64
            b = lambda s: base64.b64encode(s.encode()).decode()
            return {"epg_listings": [{"title": b("Demo programme"), "description": b(""), "start_timestamp": now, "stop_timestamp": now + 1800},
                                     {"title": b("Next up"), "description": b(""), "start_timestamp": now + 1800, "stop_timestamp": now + 3600}]}
        if action == "get_vod_categories":
            return [{"category_id": "10", "category_name": "EN| MOVIES"}, {"category_id": "11", "category_name": "4K| MOVIES"}]
        if action == "get_vod_streams":
            return [{"stream_id": 100 + i, "name": n, "category_id": c, "rating": r, "added": str(1700000000 + i), "container_extension": "mp4",
                     "stream_icon": base + "/poster/" + urllib.parse.quote(n[5:])} for i, (c, n, r) in enumerate(DEMO_MOVIES) if c == q.get("category_id")]
        if action == "get_vod_info":
            return {"info": {"plot": "A demo film to try the player.", "genre": "Drama", "duration": "02:00", "releasedate": "2024-01-01"},
                    "movie_data": {"container_extension": "mp4"}}
        if action == "get_series_categories":
            return [{"category_id": "20", "category_name": "EN| SERIES"}]
        if action == "get_series":
            return [{"series_id": 200 + i, "name": n, "rating": r, "plot": pl, "cover": base + "/poster/" + urllib.parse.quote(n), "releaseDate": "2024-05-01"}
                    for i, (c, n, r, pl) in enumerate(DEMO_SERIES) if c == q.get("category_id")]
        if action == "get_series_info":
            sid = int(q.get("series_id", 0))
            eps = {str(s): [{"id": str(sid * 100 + s * 10 + e), "episode_num": e, "title": "Episode %d" % e, "container_extension": "mp4"} for e in range(1, 5)] for s in (1, 2)}
            return {"info": {"plot": "Demo series.", "genre": "Drama"}, "episodes": eps}
        return []


if __name__ == "__main__":
    print("Beeldbuis TV preview on http://localhost:%d  (server 'demo' to try it)" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
