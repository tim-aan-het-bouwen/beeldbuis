#!/usr/bin/env python3
"""Fake Xtream provider for local end-to-end tests.
Metadata (player_api.php) is forwarded to the real provider and cached; streams are generated locally with ffmpeg."""
import hashlib, json, os, re, subprocess, sys, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
REAL = os.environ.get("BEELDBUIS_UPSTREAM", "").rstrip("/")  # real Xtream server for metadata, e.g. https://example.org
CACHE = os.path.join(HERE, "mockcache")
MEDIA = os.path.join(HERE, "media", "movie.mkv")
XMLTV = os.path.join(HERE, "xmltv.raw")
LIVE_CMD = ["ffmpeg", "-v", "error", "-nostdin", "-re", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", "-g", "50", "-c:a", "aac", "-f", "mpegts", "pipe:1"]
LOG = []


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock] %s %s\n" % (self.command, self.path[:120]))

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        if u.path == "/player_api.php":
            return self.player_api(u.query)
        if u.path == "/xmltv.php":
            return self.send_file(XMLTV, "application/xml")
        m = re.match(r"^/live/[^/]+/[^/]+/(\d+)\.ts$", u.path)
        if m:
            return self.stream_ffmpeg(LIVE_CMD)
        m = re.match(r"^/timeshift/[^/]+/[^/]+/(\d+)/([\d:-]+)/(\d+)\.ts$", u.path)
        if m:
            secs = min(int(m.group(1)) * 60, 90)
            cmd = [c for c in LIVE_CMD if c != "-re"]
            cmd[cmd.index("-f"):cmd.index("-f")] = ["-t", str(secs)]
            sys.stderr.write("[mock] timeshift start=%s dur=%s\n" % (m.group(2), m.group(1)))
            return self.stream_ffmpeg(cmd)
        if re.match(r"^/(movie|series)/[^/]+/[^/]+/\d+\.\w+$", u.path):
            return self.send_file(MEDIA, "video/x-matroska")
        self.send_error(404)

    def player_api(self, query):
        q = urllib.parse.parse_qs(query)
        action = q.get("action", [""])[0]
        key = hashlib.md5(query.encode()).hexdigest()
        fp = os.path.join(CACHE, key + ".json")
        if os.path.exists(fp):
            data = open(fp, "rb").read()
        else:
            req = urllib.request.Request(REAL + "/player_api.php?" + query, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=120).read()
            if action in ("get_vod_streams", "get_series") and not q.get("category_id"):
                try:
                    items = json.loads(data)
                    data = json.dumps(items[:300]).encode()
                except ValueError:
                    pass
            open(fp, "wb").write(data)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, fp, ctype):
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
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if rng:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.end_headers()
        with open(fp, "rb") as f:
            f.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(256 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except Exception:
                    return
                left -= len(chunk)

    def stream_ffmpeg(self, cmd):
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                chunk = p.stdout.read(32 * 1024)
                if not chunk:
                    break
                self.wfile.write(b"%x\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
        except Exception:
            pass
        finally:
            p.kill()


if __name__ == "__main__":
    os.makedirs(CACHE, exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 8899), H)
    srv.daemon_threads = True
    print("mock provider on http://127.0.0.1:8899")
    srv.serve_forever()
