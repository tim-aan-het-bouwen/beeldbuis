#!/usr/bin/env python3
"""Update 35 / tv-app 0.9.5: typing on the TV from a phone, match data next to the picture, calm home, screensaver.

    python3 build.py && python3 test/tv_extras.py

Starts the fake provider, a Beeldbuis server, a made-up Samsung TV (the remote API on 127.0.0.2:8001: it only writes
down what it is told), made-up sport scores, weather and a folder with three photos; then runs test/tv_extras.js
(the TV app and the Mac page in a browser)."""
import base64, hashlib, http.server, json, os, socket, struct, subprocess, sys, tempfile, threading, time, urllib.parse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import regress as R

TVLOG = []
PHOTOS = ""


def scoreboard(sport, league):
    def team(i, name, short, score, home):
        return {"homeAway": "home" if home else "away", "score": str(score), "team": {"id": str(i), "displayName": name, "shortDisplayName": short,
                "name": name, "location": name, "abbreviation": short[:3].upper(), "logo": ""}}
    def ev(i, a, b, sa, sb, clock):
        return {"id": str(i), "name": "%s at %s" % (b, a), "date": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() - 3600)),
                "status": {"displayClock": clock, "period": 2, "type": {"state": "in", "shortDetail": clock}},
                "competitions": [{"competitors": [team(i * 10, a, a, sa, True), team(i * 10 + 1, b, b, sb, False)],
                                  "details": [{"clock": {"displayValue": "23'"}, "type": {"text": "Goal"}, "team": {"id": str(i * 10)}, "scoringPlay": True,
                                               "athletesInvolved": [{"shortName": "S. Triker"}]}]}]}
    if (sport, league) == ("soccer", "ned.1"):
        return {"events": [ev(1, "Redtown", "Bluevale", 2, 1, "67'")]}
    if (sport, league) == ("soccer", "eng.1"):
        return {"events": [ev(2, "Northham", "Southpool", 0, 3, "80'")]}
    return {"events": []}


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def out(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
        self.send_header("Access-Control-Allow-Origin", "*"); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path); p = u.path.strip("/").split("/")
        if p[0] == "sport" and p[-1] == "scoreboard":
            return self.out(scoreboard(p[1], p[2]))
        if p[0] == "sport" and p[-1] == "summary":
            return self.out({"rosters": [{"team": {"displayName": n}, "formation": "4-3-3", "roster": [{"starter": True, "jersey": str(k), "athlete": {"displayName": "Player %d" % k},
                                          "position": {"abbreviation": "M"}} for k in range(1, 12)]} for n in ("Redtown", "Bluevale")]})
        if p[0] == "geo":
            return self.out({"results": [{"name": "Testville", "latitude": 52.1, "longitude": 5.1}]})
        if p[0] == "wx":
            return self.out({"current": {"temperature_2m": 11.6, "weather_code": 61},
                             "daily": {"temperature_2m_max": [13.2], "temperature_2m_min": [7.4], "precipitation_probability_max": [80]}})
        if p[0] == "img":
            b = open(os.path.join(PHOTOS, "a.jpg"), "rb").read()
            self.send_response(200); self.send_header("Content-Type", "image/jpeg"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            return
        if p[0] == "__tvlog":
            return self.out(TVLOG)
        self.out({})


def fake_tv():
    """A Samsung TV's remote API as far as Beeldbuis uses it: websocket on :8001 plus GET /api/v2/."""
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.2", 8001)); srv.listen(8)

    def frame(text):
        d = text.encode()
        return bytes([0x81, len(d)]) + d if len(d) < 126 else bytes([0x81, 126]) + struct.pack(">H", len(d)) + d

    def client(c):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                part = c.recv(4096)
                if not part:
                    return
                head += part
            line = head.split(b"\r\n", 1)[0].decode()
            if "Upgrade: websocket" not in head.decode("latin1"):
                body = json.dumps({"device": {"PowerState": "on", "wifiMac": "aa:bb:cc:dd:ee:ff", "name": "Test TV"}}).encode()
                if line.startswith("POST"):
                    TVLOG.append({"http": line})
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
                return
            key = [l.split(b":", 1)[1].strip() for l in head.split(b"\r\n") if l.lower().startswith(b"sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1(key + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11").digest())
            c.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + acc + b"\r\n\r\n")
            c.sendall(frame(json.dumps({"event": "ms.channel.connect", "data": {}})))
            buf = b""
            while True:
                part = c.recv(4096)
                if not part:
                    return
                buf += part
                while len(buf) >= 6:
                    n = buf[1] & 0x7F; off = 2
                    if n == 126:
                        if len(buf) < 4:
                            break
                        n = struct.unpack(">H", buf[2:4])[0]; off = 4
                    if len(buf) < off + 4 + n:
                        break
                    mask = buf[off:off + 4]; data = bytes(b ^ mask[i % 4] for i, b in enumerate(buf[off + 4:off + 4 + n]))
                    buf = buf[off + 4 + n:]
                    try:
                        TVLOG.append(json.loads(data.decode()))
                    except ValueError:
                        pass
        except OSError:
            pass
        finally:
            c.close()

    def loop():
        while True:
            c, _ = srv.accept()
            threading.Thread(target=client, args=(c,), daemon=True).start()
    threading.Thread(target=loop, daemon=True).start()


def main():
    lan = socket.gethostbyname(socket.gethostname()) if not os.environ.get("BB_LAN") else os.environ["BB_LAN"]
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("192.0.2.1", 9)); lan = s.getsockname()[0]; s.close()
    except OSError:
        pass
    mock = http.server.ThreadingHTTPServer(("127.0.0.1", R.free_port()), Mock)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mbase = "http://127.0.0.1:%d" % mock.server_address[1]
    fake_tv()
    global PHOTOS
    photos = PHOTOS = tempfile.mkdtemp(prefix="bb-photos-")
    ff = os.environ.get("IPTV_FFMPEG") or "ffmpeg"
    for i, (size, name) in enumerate((("1280x720", "a.jpg"), ("720x1280", "b.jpg"), ("640x480", "c.png"))):
        subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=%s:rate=1" % size, "-frames:v", "1", os.path.join(photos, name)], check=True)
    open(os.path.join(photos, "notes.txt"), "w").write("not a photo")
    fake = R.Fake()
    srv = R.Server(fake, env={"IPTV_BIND": "0.0.0.0", "IPTV_SPORT_BASE": mbase + "/sport", "IPTV_WEATHER_BASE": mbase + "/wx", "IPTV_GEO_BASE": mbase + "/geo"})
    code = 1
    try:
        st, j = srv.post("/api/settings", {"tv_ip": "127.0.0.2", "weather_place": "Testville", "saver_photos": photos, "phone": True,
                                           "tv_inputs": [{"name": "Game console", "pos": 3}]})
        assert st == 200, j
        macurl = "http://%s:%d" % (lan, srv.port)
        code = subprocess.call(["node", os.path.join(HERE, "tv_extras.js"), macurl, fake.url, mbase, srv.url] + sys.argv[1:])
    finally:
        srv.stop(); fake.stop()
    sys.exit(code)


if __name__ == "__main__":
    main()
