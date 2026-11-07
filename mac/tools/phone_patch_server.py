#!/usr/bin/env python3
"""Update 6: iPhone/iPad access over the home network (pairing code, device keys, owner-aware sessions).
Applies to src/server.py in place; every replacement must match exactly once."""
import os
import sys

P = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "server.py")
src = open(P, encoding="utf-8").read()
if "def phone_info" in src:
    sys.exit("already patched")


def rep(old, new, count=1):
    global src
    n = src.count(old)
    assert n == count, (old[:120], n)
    src = src.replace(old, new)


rep("import base64\n", "import base64\nimport secrets\nimport socket\nimport socketserver\n")
rep('HOST = "127.0.0.1"\n', 'HOST = "127.0.0.1"          # address the Mac itself uses\nBIND = os.environ.get("IPTV_BIND", "::")   # listen on all networks so an iPhone on the same Wi-Fi can connect\n')
rep('"positions": {}, "server_tz": ""}', '"positions": {}, "server_tz": "",\n         "devices": {}, "pair_code": ""}')

# ---- sessions know who started them, so another device can take over cleanly
rep('''def stop_all_sessions(remote_only=False):
    for sid, sess in list(SESSIONS.items()):
        if remote_only and sess.get("local"):
            continue
        stop_session(sid)
''', '''def stop_all_sessions(remote_only=False, taken_by=None, owner=None):
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
''')
rep('''def start_session(target, info, start, force, live, follow=False):
    """Start ffmpeg writing HLS segments; returns (sid, playlist, error) once the playlist is usable."""
    stop_all_sessions()                 # one stream at a time (the subscription allows one connection)
    sid = "%d" % int(time.time() * 1000)
''', '''def start_session(target, info, start, force, live, follow=False, owner=("mac", "Mac")):
    """Start ffmpeg writing HLS segments; returns (sid, playlist, error) once the playlist is usable."""
    stop_all_sessions(taken_by=owner)   # one stream at a time (the subscription allows one connection)
    sid = "%d-%s" % (int(time.time() * 1000), secrets.token_hex(8))   # unguessable: the HLS files are served by session id
''')
rep('''        SESSIONS[sid] = {"proc": proc, "dir": outdir, "t": time.time(), "local": is_local(target)}''',
    '''        SESSIONS[sid] = {"proc": proc, "dir": outdir, "t": time.time(), "local": is_local(target), "owner": owner[0], "live": live}''')

# ---- Safari's own HLS player wants a few live segments before it starts
rep('''def start_session(target, info, start, force, live, follow=False, owner=("mac", "Mac")):''',
    '''def start_session(target, info, start, force, live, follow=False, owner=("mac", "Mac"), min_segs=1):''')
rep('''            if txt.count(".m4s") >= 1 and (not use_sub''', '''            if txt.count(".m4s") >= min_segs and (not use_sub''')
rep('''                                                  int(q("start", "0") or 0), int(q("dur", "0") or 0)))''',
    '''                                                  int(q("start", "0") or 0), int(q("dur", "0") or 0), q("native") == "1"))''')
rep('''    def play_api(self, kind, sid, ext, start, force, cu_start=0, cu_dur=0):''',
    '''    def play_api(self, kind, sid, ext, start, force, cu_start=0, cu_dur=0, native=False):''')

# ---- phone access helpers (before the HTTP handler)
rep('\nclass Handler(BaseHTTPRequestHandler):', r'''
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


class Handler(BaseHTTPRequestHandler):''')

# ---- gate every request
rep('''    # -- routing
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
''', '''    # -- who is asking: the Mac itself, or a paired iPhone/iPad on the network
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
        if name or (method == "GET" and (path in PUBLIC_GET or path.startswith(("/assets/", "/hls/")))) or path == "/api/pair":
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
''')
rep('''            return self.send_json({"ok": True, "ffmpeg": bool(FFMPEG), "v": VERSION, "rec": rec_status()})''',
    '''            now = time.time()
            taken = {k: v for k, v in TAKEN.items() if now - v["t"] < 600}
            return self.send_json({"ok": True, "ffmpeg": bool(FFMPEG), "v": VERSION, "rec": rec_status(), "now": now,
                                   "taken": taken.get(self.dev[0]), "me": self.client()})''')
rep('''        if path == "/api/stop":
            stop_all_sessions()
            return self.send_json({"ok": True})''', '''        if path == "/api/stop":
            stop_all_sessions(owner=self.dev[0])     # only this device's own stream
            return self.send_json({"ok": True})
        if path == "/api/phone":
            return self.api(phone_info)''')
rep('''    def do_POST(self):
        LAST_REQ["t"] = time.time()
        path = urllib.parse.urlsplit(self.path).path
''', '''    def do_POST(self):
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
''')
rep('''    def send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")''', '''    def send_json(self, obj, status=200, extra=None):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        for k, v in (extra or []):
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json; charset=utf-8")''')
rep('''    def send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")''', '''    def send_html(self, html, cookie=None):
        body = html.encode("utf-8")
        self.send_response(200)
        if cookie:
            self.send_header("Set-Cookie", "bb_dev=%s; Path=/; Max-Age=315360000; HttpOnly; SameSite=Lax" % cookie)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "text/html; charset=utf-8")''')
rep('''    # -- api implementations
    def status(self):
        with LOCK:
            if not STATE["server"]:
                return {"logged_in": False, "ffmpeg": bool(FFMPEG)}''', '''    # -- api implementations
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
                return {"logged_in": False, "ffmpeg": bool(FFMPEG), "client": self.client()}''')
rep('''            "epg": epg_status(), "version": VERSION,
        }''', '''            "epg": epg_status(), "version": VERSION, "client": self.client(),
        }''')
rep(''', force, live=False)\n''', ''', force, live=False, owner=self.dev)\n''')
rep('''        sess, pl, err = start_session(target, info, start, force, live=(kind == "live" or follow), follow=follow)''',
    '''        sess, pl, err = start_session(target, info, start, force, live=(kind == "live" or follow), follow=follow, owner=self.dev,
                                      min_segs=3 if native and (kind == "live" or follow) else 1)''')

# ---- HLS files: a replaced session answers 410; VOD playlists start at the beginning on iOS too
rep('''        sess = SESSIONS.get(sid)
        if not sess:
            return self.send_error(404)
        sess["t"] = time.time()
        fp = os.path.join(sess["dir"], name)
        try:
            data = open(fp, "rb").read()
        except OSError:
            return self.send_error(404)''', '''        sess = SESSIONS.get(sid)
        if not sess:
            return self.send_error(404)
        sess["t"] = time.time()
        fp = os.path.join(sess["dir"], name)
        try:
            data = open(fp, "rb").read()
        except OSError:
            return self.send_error(404)
        if name == "index.m3u8" and not sess.get("live") and b"#EXT-X-START" not in data:
            data = data.replace(b"#EXTM3U\\n", b"#EXTM3U\\n#EXT-X-START:TIME-OFFSET=0,PRECISE=YES\\n", 1)''')

# ---- direct (mp4) streams count as this device's connection
rep('''        name, _, ext = parts[3].rpartition(".")
        if not name or not ext:
            return self.send_error(404)
        return self.proxy(stream_url(kind, name, ext))''', '''        name, _, ext = parts[3].rpartition(".")
        if not name or not ext:
            return self.send_error(404)
        if any(s.get("owner") != self.dev[0] for s in list(SESSIONS.values())):
            stop_all_sessions(taken_by=self.dev)      # one connection: this device takes it over
        gen = DIRECT.setdefault(self.dev[0], {"n": 0, "stop": 0})
        gen["n"] += 1
        try:
            return self.proxy(stream_url(kind, name, ext), gen)
        finally:
            gen["n"] -= 1''')
rep('''    def proxy(self, target):''', '''    def proxy(self, target, gen=None):
        stop_at = gen["stop"] if gen else None''')
rep('''                chunk = resp.read(64 * 1024)
                if not chunk:
                    break''', '''                chunk = resp.read(64 * 1024)
                if not chunk or (gen and gen["stop"] != stop_at):     # another device took the connection
                    break''')

# ---- listen on the network as well (dual stack), without slow reverse-DNS lookups
rep('''HTTPD = None
''', '''HTTPD = None


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
''')
rep('''        HTTPD = ThreadingHTTPServer((HOST, PORT), Handler)''', '''        HTTPD = make_server()''')

open(P, "w", encoding="utf-8").write(src)
print("patched", P)
