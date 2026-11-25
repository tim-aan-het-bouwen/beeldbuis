#!/usr/bin/env python3
"""Self-test of the iPhone web app in Safari on a Mac (Safari = the iPhone's video engine).

    python3 ios/test/selftest_server.py [--config PATH] [--port 8795]

Serves ios/app/ on http://localhost:PORT, answers /c with the login from a Beeldbuis config.json (default:
the Mac app's own) plus test ids picked from the provider, collects results on /log, opens Safari, waits for DONE,
prints the results with the login masked and closes the tab. Plays one thing at a time (one connection)."""
import json, os, re, subprocess, sys, threading, time, urllib.parse, http.server, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "app")
PORT = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 8795
CFG = sys.argv[sys.argv.index("--config") + 1] if "--config" in sys.argv else None
if not CFG:
    for d in ("IPTVPlayer", "Beeldbuis"):
        p = os.path.expanduser("~/Library/Application Support/%s/config.json" % d)
        if os.path.exists(p):
            CFG = p; break
cfg = json.load(open(CFG))
S, U, P = cfg["server"].rstrip("/"), cfg["username"], cfg["password"]
HOST = urllib.parse.urlparse(S).hostname
BASE = "https://" + HOST if "--http" not in sys.argv else "http://" + HOST


def mask(s):
    return str(s).replace(U, "U").replace(P, "P").replace(urllib.parse.quote(U), "U").replace(urllib.parse.quote(P), "P")


def api(**q):
    q.update(username=U, password=P)
    out = subprocess.run(["curl", "-sS", "-m", "180", BASE + "/player_api.php?" + urllib.parse.urlencode(q)], capture_output=True).stdout
    return json.loads(out or b"null")


def norm(n):
    n = re.sub(r"\((19|20)\d\d\)|\b(4k|uhd|fhd|hd|sd|hevc|h265|x265|multi|sub|subs|nl|en|vost\w*|dubbed|remastered)\b|\[[^\]]*\]", " ", n or "", flags=re.I)
    n = re.sub(r"^\s*[a-z0-9]{2,3}\s*[-|:]\s+", "", n.lower())
    return re.sub(r"[^a-z0-9]+", " ", n).strip()


live = api(action="get_live_streams")
vods = api(action="get_vod_streams")
npo = next((x for x in live if re.search(r"NPO ?1\b", x.get("name", "")) and re.search(r"\b(FHD|HD)\b", x.get("name", ""))), None) or \
    next((x for x in live if re.search(r"NPO ?1\b", x.get("name", ""))), live[0])
hevc = next((x for x in live if "Main Event" in x.get("name", "") and "FHD" in x.get("name", "")), None)
arch = next((x for x in live if str(x.get("tv_archive")) == "1" and re.search(r"NPO ?1\b", x.get("name", ""))), None) or \
    next((x for x in live if str(x.get("tv_archive")) == "1"), None)
mp4s = [v for v in vods if v.get("container_extension") == "mp4"]
by = {}
for v in vods:
    by.setdefault(norm(v["name"]), []).append(v)
twin = next((v for v in vods if v.get("container_extension") == "mkv" and len(norm(v["name"])) > 6 and
             any(w.get("container_extension") == "mp4" for w in by[norm(v["name"])])), None)
lone = next((v for v in vods if v.get("container_extension") == "mkv" and all(w.get("container_extension") != "mp4" for w in by[norm(v["name"])])), None)
mkvshare = sum(1 for v in vods if v.get("container_extension") == "mkv")
mkvtwin = sum(1 for v in vods if v.get("container_extension") == "mkv" and any(w.get("container_extension") in ("mp4", "m4v") for w in by[norm(v["name"])]))
print("films: %d, mkv: %d, mkv with an mp4 copy (same name): %d" % (len(vods), mkvshare, mkvtwin))

tests = [{"kind": "live", "id": npo["stream_id"], "name": "live " + npo["name"], "secs": 40}]
if hevc:
    tests.append({"kind": "live", "id": hevc["stream_id"], "name": "live hevc " + hevc["name"], "secs": 35})
m = mp4s[len(mp4s) // 2]
tests.append({"kind": "movie", "id": m["stream_id"], "ext": "mp4", "name": "movie mp4", "title": m["name"], "cat": m.get("category_id"), "secs": 30})
if twin:
    tests.append({"kind": "movie", "id": twin["stream_id"], "ext": "mkv", "name": "movie mkv with twin", "title": twin["name"], "cat": twin.get("category_id"), "secs": 90})
if lone:
    tests.append({"kind": "movie", "id": lone["stream_id"], "ext": "mkv", "name": "movie mkv alone", "title": lone["name"], "cat": lone.get("category_id"), "secs": 20})
if arch:
    st = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1) - datetime.timedelta(hours=2)).strftime("%Y-%m-%d:%H-%M")
    for ext in ("ts", "m3u8"):
        tests.append({"kind": "url", "name": "catch-up " + ext, "secs": 30, "url": "{S}/timeshift/{U}/{P}/60/%s/%s.%s" % (st, arch["stream_id"], ext)})
    tests.append({"kind": "url", "name": "catch-up php m3u8", "secs": 30,
                  "url": "{S}/streaming/timeshift.php?username={U}&password={P}&stream=%s&start=%s&duration=60&extension=m3u8" % (arch["stream_id"], st)})

C = json.dumps({"s": BASE, "u": U, "p": P, "tests": tests}).encode()
LOG = []


class H(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=APP, **k)

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/c":
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(C)))
            self.end_headers(); self.wfile.write(C); return
        return super().do_GET()

    def do_POST(self):
        d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        LOG.append(d); print(mask(json.dumps(d, ensure_ascii=False)), flush=True)
        self.send_response(204); self.end_headers()


srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
url = "http://localhost:%d/?selftest" % PORT
if "--no-open" not in sys.argv:
    subprocess.run(["open", "-a", "Safari", url]); time.sleep(2)
    subprocess.run(["osascript", "-e", 'tell application "Safari" to activate'])
t0 = time.time()
while time.time() - t0 < 600 and not any(l.get("m") == "DONE" for l in LOG):
    time.sleep(1)
if "--no-open" not in sys.argv:
    subprocess.run(["osascript", "-e", 'tell application "Safari" to close (every tab of every window whose URL contains "localhost:%d")' % PORT])
