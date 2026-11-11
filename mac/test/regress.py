#!/usr/bin/env python3
"""Beeldbuis regression tests (white label, offline by default).

    python3 build.py && python3 test/regress.py            # all suites
    python3 test/regress.py rec ui                         # some suites
    FAKE_CACHE=/path python3 test/regress.py library       # library suite on a cached real catalogue (see fake_provider.py)

Starts its own fake provider (test/fake_provider.py: one connection, fault injection) and a fresh Beeldbuis server
(pkg/iptv.py) in a temporary config folder. Every bug that reached a real install gets a test here.
"""
import collections, json, os, re, shutil, signal, socket, subprocess, sys, tempfile, time, urllib.error, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PKG = os.path.join(ROOT, "pkg", "iptv.py")
RESULTS = []


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def http(url, data=None, timeout=60, method=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw[:1] in (b"{", b"[") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw


def wait(cond, timeout, step=0.5, what="condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        try:
            last = cond()
            if last:
                return last
        except Exception as e:  # noqa
            last = e
        time.sleep(step)
    raise AssertionError("timed out waiting for %s (last: %r)" % (what, last))


class Fake:
    def __init__(self, cache=None):
        self.port = free_port()
        env = dict(os.environ, FAKE_CACHE=cache or tempfile.mkdtemp(prefix="bb-fakecache-"))
        if not cache:
            env.pop("BEELDBUIS_UPSTREAM", None)
        self.p = subprocess.Popen([sys.executable, os.path.join(HERE, "fake_provider.py"), str(self.port)], env=env,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.url = "http://127.0.0.1:%d" % self.port
        wait(lambda: http(self.url + "/__log")[0] == 200, 10, what="fake provider")

    def ctl(self, **kw):
        return http(self.url + "/__ctl?" + "&".join("%s=%s" % kv for kv in kw.items()))[1]

    def log(self):
        return http(self.url + "/__log")[1]

    def stop(self):
        os.killpg(self.p.pid, signal.SIGKILL)


class Server:
    def __init__(self, fake, cfg=None, env=None):
        self.cfg = cfg or tempfile.mkdtemp(prefix="bb-cfg-")
        self.port = free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.fake = fake
        self.env = dict(os.environ, IPTV_CONFIG_DIR=self.cfg, IPTV_PORT=str(self.port), IPTV_BIND="127.0.0.1", IPTV_REC_STALL="8",
                        IPTV_TMDB_BASE="http://127.0.0.1:9/3", IPTV_OMDB_BASE="http://127.0.0.1:9/omdb",
                        IPTV_OSUB_BASE="http://127.0.0.1:9/osub", IPTV_LB_BASE="http://127.0.0.1:9/lb",
                        IPTV_SPORT_BASE="http://127.0.0.1:9/sport")
        self.env.update(env or {})
        self.start()

    def start(self, login=True):
        self.log = open(os.path.join(self.cfg, "test-server.log"), "ab")
        self.p = subprocess.Popen([sys.executable, PKG, "--no-browser"], env=self.env, cwd=os.path.dirname(PKG),
                                  stdout=self.log, stderr=self.log, start_new_session=True)
        wait(lambda: self.get("/api/ping")[0] == 200, 20, what="server start")
        if login and not self.get("/api/status")[1]["data"]["logged_in"]:
            st, j = self.post("/api/login", {"server": self.fake.url, "username": "tester", "password": "secret123"})
            assert st == 200 and j.get("ok"), j
        if login:
            wait(lambda: self.get("/api/status")[1]["data"]["catalog"]["n"] > 0, 60, what="channel list")

    def stop(self, hard=False):
        try:
            os.killpg(self.p.pid, signal.SIGKILL if hard else signal.SIGTERM)
            self.p.wait(timeout=10)
        except Exception:  # noqa
            pass

    def get(self, path, timeout=60):
        return http(self.url + path, timeout=timeout)

    def post(self, path, data, timeout=60):
        return http(self.url + path, data=data, timeout=timeout)


def test(name):
    def deco(fn):
        fn.test_name = name
        return fn
    return deco


def run(fns):
    for fn in fns:
        t0 = time.time()
        try:
            fn()
            RESULTS.append((fn.test_name, True, ""))
            print("PASS  %-62s %5.1fs" % (fn.test_name, time.time() - t0), flush=True)
        except Exception as e:  # noqa
            RESULTS.append((fn.test_name, False, "%s: %s" % (type(e).__name__, e)))
            print("FAIL  %-62s %5.1fs  %s: %s" % (fn.test_name, time.time() - t0, type(e).__name__, e), flush=True)


# =========================================================================================== static checks ====
def suite_static():
    ui = open(os.path.join(ROOT, "src", "ui.html"), encoding="utf-8").read()
    js = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", ui, re.S))

    @test("static: no JavaScript function is defined twice (update 24: two loadTaste)")
    def t1():
        names = re.findall(r"(?m)(?:^|[;\s{}])(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", js)
        d = [k for k, v in collections.Counter(names).items() if v > 1]
        assert not d, "defined twice: %s" % d

    @test("static: the UI script parses (node --check)")
    def t2():
        fp = os.path.join(tempfile.mkdtemp(), "ui.js")
        open(fp, "w").write(js)
        r = subprocess.run(["node", "--check", fp], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-600:]

    @test("static: every $('#id') used by the UI exists in the page")
    def t3():
        ids = set(re.findall(r'id="([\w-]+)"', ui)) | set(re.findall(r"id=\\?'([\w-]+)", ui)) | set(re.findall(r"\.id\s*=\s*'([\w-]+)'", ui))
        ids |= set(re.findall(r'id=\\"([\w-]+)\\"', ui))
        used = set(re.findall(r"\$\('#([\w-]+)'\)", js))
        # ids built at runtime in template strings: id="x${...}" are skipped by the regex above, so only static ones count
        missing = sorted(u for u in used if u not in ids and not re.search(r"id=[\"']%s" % re.escape(u), ui))
        assert not missing, "used but never defined: %s" % missing[:20]

    @test("static: server compiles and the built package is current")
    def t4():
        srv = open(os.path.join(ROOT, "src", "server.py"), encoding="utf-8").read()
        assert "__UI_HTML__" in srv
        v = re.search(r"^VERSION = (\d+)", srv, re.M).group(1)
        pkg = open(PKG, encoding="utf-8").read()
        assert re.search(r"^VERSION = %s$" % v, pkg, re.M), "pkg/iptv.py is older than src: run build.py"
        assert ui[:2000] in pkg, "pkg/iptv.py does not contain the current ui.html: run build.py"

    @test("static: white label (no provider host, login or personal names in the code)")
    def t5():
        srv = open(os.path.join(ROOT, "src", "server.py"), encoding="utf-8").read()
        bad = []
        # home-network addresses always; your own words (provider host, names, computer name) go one regex per line in
        # test/private-words.txt, which stays on your computer (never in the repository)
        pats = [r"192\.168\.\d+\.\d+"]
        try:
            pats += [ln.strip() for ln in open(os.path.join(HERE, "private-words.txt"), encoding="utf-8") if ln.strip() and not ln.startswith("#")]
        except OSError:
            pass
        for pat in pats:
            for where, txt in (("server.py", srv), ("ui.html", ui)):
                if re.search(pat, txt, re.I):
                    bad.append("%s in %s" % (pat, where))
        assert not bad, bad

    run([t1, t2, t3, t4, t5])


# ============================================================================================ recordings ====
def rec_items(s):
    return {r["id"]: r for r in s.get("/api/recordings")[1]["data"]["items"]}


def schedule(s, sid=100, start=None, dur=40, title="Test recording", **kw):
    now = int(time.time())
    start = now if start is None else start
    st, j = s.post("/api/record", dict({"stream_id": sid, "start": start, "stop": start + dur, "title": title, "channel": "NL| NPO 1"}, **kw))
    assert st == 200 and j.get("ok"), j
    return j["data"]["id"]


def rec_clean(s):
    for r in rec_items(s).values():
        if r["status"] in ("scheduled", "recording"):
            s.post("/api/record/cancel", {"id": r["id"]})
    s.get("/api/stop")


def max_concurrent(log, since):
    return max([e.get("concurrent", 0) for e in log["log"] if e["ev"] == "open" and e["t"] >= since] or [0])


def suite_rec():
    fake = Fake()
    s = Server(fake)
    s.post("/api/settings", {"rec_before": 0, "rec_after": 0})

    @test("rec: a recording starts on time, grows and ends as a seekable .mp4")
    def r1():
        rec_clean(s)
        rid = schedule(s, dur=25)
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 10, what="recording status")
        wait(lambda: rec_items(s)[rid].get("size", 0) > 200000, 20, what="file growing")
        r = wait(lambda: (lambda x: x if x["status"] in ("done", "failed") else None)(rec_items(s)[rid]), 60, what="recording end")
        assert r["status"] == "done", r
        r = wait(lambda: (lambda x: x if x["file"].endswith(".mp4") else None)(rec_items(s)[rid]), 30, what="mp4 remux")
        assert 15 <= r["duration"] <= 40, r["duration"]
        st, body = http(s.url + "/direct/rec/%s.mp4" % rid, timeout=30)
        assert st == 200 and len(body) > 100000

    @test("rec: live TV during a recording answers 409 and opens no provider stream")
    def r2():
        rec_clean(s)
        rid = schedule(s, dur=30)
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 10)
        t0 = time.time()
        st, j = s.get("/api/play?kind=live&id=101&ext=ts")
        assert st == 409, (st, j)
        st, j = s.get("/api/play?kind=movie&id=901&ext=mp4")
        assert st == 409, (st, j)
        st, j = http(s.url + "/direct/movie/901.mp4", timeout=10)
        assert st == 409, ("direct stream during a recording", st)
        time.sleep(2)
        assert max_concurrent(fake.log(), t0 - 1) <= 1
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: a running live stream is stopped when the recording starts (never 2 connections)")
    def r3():
        rec_clean(s)
        st, j = s.get("/api/play?kind=live&id=102&ext=ts")
        assert st == 200, j
        t0 = time.time()
        rid = schedule(s, start=int(time.time()) + 4, dur=20)
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 15)
        wait(lambda: rec_items(s)[rid].get("size", 0) > 100000, 20)
        log = fake.log()
        assert max_concurrent(log, t0) <= 1, [e for e in log["log"] if e["t"] >= t0]
        kicked = [e for e in log["log"] if e["t"] >= t0 and e.get("why") == "kicked"]
        assert not kicked, "the provider had to kick a stream: %s" % kicked
        st, ping = s.get("/api/ping")
        assert ping["rec"]["active"]["id"] == rid
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: a direct movie stream (iPhone/Mac mp4) is ended when the recording starts")
    def r4():
        rec_clean(s)
        import threading
        got = {"n": 0, "done": False}

        def reader():
            try:
                with urllib.request.urlopen(s.url + "/direct/movie/901.mp4", timeout=60) as r:
                    while True:
                        b = r.read(8192)
                        if not b:
                            break
                        got["n"] += len(b)
                        time.sleep(0.05)       # a slow reader, like a browser that buffers ahead
            except Exception:  # noqa
                pass
            got["done"] = True
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        wait(lambda: got["n"] > 0, 10)
        t0 = time.time()
        rid = schedule(s, start=int(time.time()) + 3, dur=20)
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 15)
        time.sleep(3)
        log = [e for e in fake.log()["log"] if e["t"] >= t0]
        movie = [e for e in log if e["ev"] == "close" and e["kind"] == "movie"]
        assert movie and movie[0]["why"] == "client", ("the movie stream was not ended by Beeldbuis", log)
        assert not [e for e in log if e.get("why") == "kicked"], log
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: a dead connection (no data) is restarted by the watchdog and the file keeps growing")
    def r5():
        rec_clean(s)
        rid = schedule(s, dur=70)
        wait(lambda: rec_items(s)[rid].get("size", 0) > 100000, 20)
        fake.ctl(stall=20)
        time.sleep(22)
        size1 = rec_items(s)[rid]["size"]
        wait(lambda: rec_items(s)[rid]["size"] > size1 + 200000, 30, what="growth after the stall")
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: provider errors for a minute do not end the recording (keeps retrying)")
    def r6():
        rec_clean(s)
        rid = schedule(s, dur=110)
        wait(lambda: rec_items(s)[rid].get("size", 0) > 100000, 20)
        fake.ctl(fail=40)          # every reconnect gets a 503 for a while
        fake.ctl(drop=1)
        time.sleep(40)
        r = rec_items(s)[rid]
        assert r["status"] == "recording", r
        fake.ctl(fail=0)
        size1 = rec_items(s)[rid]["size"]
        wait(lambda: rec_items(s)[rid]["size"] > size1 + 200000, 45, what="growth after the errors")
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: an app restart (update) during a recording continues in the same file")
    def r7():
        rec_clean(s)
        rid = schedule(s, dur=60)
        wait(lambda: rec_items(s)[rid].get("size", 0) > 200000, 20)
        f1 = rec_items(s)[rid]["file"]
        s.stop()
        time.sleep(2)
        s.start()
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 10)
        size1 = rec_items(s)[rid]["size"]
        wait(lambda: rec_items(s)[rid]["size"] > size1 + 200000, 25, what="growth after restart")
        assert rec_items(s)[rid]["file"] == f1
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: overlapping programmes are refused, back-to-back ones trim padding")
    def r8():
        rec_clean(s)
        s.post("/api/settings", {"rec_before": 2, "rec_after": 10})
        t = int(time.time()) + 3600
        a = schedule(s, start=t, dur=1800, title="A")
        st, j = s.post("/api/record", {"stream_id": 101, "start": t + 600, "stop": t + 2400, "title": "B"})
        assert st != 200 or not j.get("ok"), "overlap was accepted"
        b = schedule(s, start=t + 1800, dur=1800, title="C")
        items = rec_items(s)
        assert items[a]["pad_after"] == 0 and items[b]["pad_before"] == 0, (items[a]["pad_after"], items[b]["pad_before"])
        for x in (a, b):
            s.post("/api/record/cancel", {"id": x})
        s.post("/api/settings", {"rec_before": 0, "rec_after": 0})

    @test("rec: watching a recording while it records needs no provider connection")
    def r9():
        rec_clean(s)
        rid = schedule(s, dur=50)
        wait(lambda: rec_items(s)[rid].get("size", 0) > 600000, 25)
        t0 = time.time()
        st, j = s.get("/api/play?kind=rec&id=%s" % rid)
        assert st == 200 and j["data"]["playlist"], j
        st, pl = http(s.url + j["data"]["playlist"])
        assert b"#EXT-X-START:TIME-OFFSET=0" in pl, "a recording in progress must play from its start (spoilers): %r" % pl[:200]
        time.sleep(3)
        opens = [e for e in fake.log()["log"] if e["t"] >= t0 and e["ev"] == "open"]
        assert not opens, opens
        assert rec_items(s)[rid]["status"] == "recording"
        s.get("/api/stop")
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: cancelling a short recording removes it; deleting removes the file")
    def r10():
        rec_clean(s)
        rid = schedule(s, dur=60)
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 10)
        time.sleep(1)
        s.post("/api/record/cancel", {"id": rid})
        r = rec_items(s)[rid]
        assert r["status"] in ("cancelled", "done"), r
        rid2 = schedule(s, dur=12)
        r2 = wait(lambda: (lambda x: x if x["status"] == "done" else None)(rec_items(s)[rid2]), 40)
        time.sleep(4)
        f = rec_items(s)[rid2]["file"]
        s.post("/api/record/delete", {"id": rid2})
        assert rid2 not in rec_items(s) and not os.path.exists(f)

    @test("rec: the 2-minute warning shows in /api/ping")
    def r11():
        rec_clean(s)
        rid = schedule(s, start=int(time.time()) + 90, dur=60)
        st, ping = s.get("/api/ping")
        assert ping["rec"]["upcoming"] and ping["rec"]["upcoming"]["id"] == rid, ping["rec"]
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: a recording on a channel without a guide (manual block) works")
    def r12():
        rec_clean(s)
        st, j = s.post("/api/remote/record", {"stream_id": 103})
        assert st == 200 and j.get("ok"), j
        rid = j["data"]["id"]
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 10)
        s.post("/api/record/cancel", {"id": rid})

    @test("rec: live sport records 30 min longer; +30 extends up to the next recording")
    def r13():
        rec_clean(s)
        t = int(time.time()) + 7200
        a = schedule(s, start=t, dur=3600, title="NFL: Packers @ Patriots")
        assert rec_items(s)[a]["pad_after"] == 1800, rec_items(s)[a]["pad_after"]
        b = schedule(s, start=t + 3600 + 2400, dur=600, title="Journaal")
        st, j = s.post("/api/record/extend", {"id": a, "minutes": 30})
        assert st == 200 and j["data"]["pad_after"] == 2400, j        # clamped at the next recording's start
        assert "Journaal" in j["data"]["note"]
        c = schedule(s, start=t + 20000, dur=600, title="Highlights NFL")
        assert rec_items(s)[c]["pad_after"] == 0, "highlights are not live sport"
        for x in (a, b, c):
            s.post("/api/record/cancel", {"id": x})

    @test("rec: 'Play on TV' of a film is refused while recording (the TV would fetch it from the provider)")
    def r14():
        rec_clean(s)
        rid = schedule(s, dur=30)
        wait(lambda: rec_items(s)[rid]["status"] == "recording", 10)
        st, j = s.post("/api/remote/send", {"action": "play", "kind": "movie", "id": 900, "ext": "mkv", "name": "Film"})
        assert st != 200 and "one connection" in json.dumps(j), (st, j)
        s.post("/api/record/cancel", {"id": rid})

    try:
        run([r1, r2, r3, r4, r5, r6, r7, r8, r9, r10, r11, r12, r13, r14])
    finally:
        s.stop(hard=True)
        fake.stop()


# =============================================================================== playback / live buffer ====
def suite_play():
    fake = Fake()
    s = Server(fake)

    @test("play: live starts, the buffer grows and buffer.ts is valid MPEG-TS")
    def p1():
        st, j = s.get("/api/play?kind=live&id=100&ext=ts")
        assert st == 200, j
        sid = j["data"]["playlist"].split("/")[2]
        wait(lambda: s.get("/api/buffer?sid=" + sid)[1]["data"]["length"] >= 8, 20, what="buffer")
        req = urllib.request.urlopen(s.url + "/hls/%s/buffer.ts?behind=6" % sid, timeout=20)
        data = req.read(188 * 200)
        req.close()
        assert data[0] == 0x47 and data[188] == 0x47, "not MPEG-TS"
        s.get("/api/stop")

    @test("play: switching channels never needs two provider connections")
    def p2():
        t0 = time.time()
        for sid in (100, 101, 102, 100):
            st, j = s.get("/api/play?kind=live&id=%d&ext=ts" % sid)
            assert st == 200, j
            time.sleep(1.5)
        time.sleep(2)
        log = fake.log()
        kicked = [e for e in log["log"] if e["t"] >= t0 and e.get("why") == "kicked"]
        assert not kicked, kicked
        s.get("/api/stop")

    @test("play: catch-up (timeshift) starts")
    def p3():
        now = int(time.time()) // 60 * 60
        st, j = s.get("/api/play?kind=catchup&id=100&ext=ts&start=%d&dur=30" % (now - 3600))
        assert st == 200 and j["data"]["playlist"], j
        s.get("/api/stop")

    @test("play: movie with English subtitle track gets a subtitle playlist")
    def p4():
        st, j = s.get("/api/play?kind=movie&id=900&ext=mkv")
        assert st == 200, j
        assert j["data"]["playlist"].endswith("master.m3u8"), j["data"]
        s.get("/api/stop")

    @test("play: a stream that fails twice with 503 still starts (probe retries)")
    def p5():
        fake.ctl(fail=2)
        PROBE = s.get("/api/play?kind=live&id=103&ext=ts", timeout=90)
        assert PROBE[0] == 200, PROBE
        s.get("/api/stop")

    @test("play: /api/stop ends the provider connection")
    def p6():
        s.get("/api/play?kind=live&id=100&ext=ts")
        time.sleep(1)
        s.get("/api/stop")
        wait(lambda: fake.log()["open"] == 0, 10, what="connection closed")

    def lib(q):
        wait(lambda: s.get("/api/library?kind=movie&q=" + q)[1]["data"]["items"], 60, what="library " + q)
        return s.get("/api/library?kind=movie&q=" + q)[1]["data"]["items"]

    @test("play: a film whose file is missing at the provider (5XX) plays another edition and is marked")
    def p7():
        items = lib("american")
        assert len(items) == 1 and items[0]["variants"] == 2, items
        key = items[0]["key"]
        st, j = s.get("/api/item?key=" + urllib.parse.quote(key))
        first = j["data"]["variants"][0]["id"]
        assert first == 903, j["data"]["variants"]           # original audio FHD first, before the 4K one
        st, j = s.get("/api/play?kind=movie&id=903&ext=mkv", timeout=120)
        assert st == 200, j
        assert j["data"].get("fallback", {}).get("id") == 904 and j["data"]["fallback"]["edition"], j["data"]
        st, j = s.get("/api/item?key=" + urllib.parse.quote(key))
        vs = j["data"]["variants"]
        assert vs[0]["id"] == 904 and vs[-1]["id"] == 903 and vs[-1].get("dead"), vs
        assert not j["data"]["dead"], j["data"]
        s.get("/api/stop")

    @test("play: a film listed with the wrong extension plays from the real file")
    def p8():
        st, j = s.get("/api/play?kind=movie&id=905&ext=mp4", timeout=120)
        assert st == 200, j
        fb = j["data"].get("fallback") or {}
        assert fb.get("id") == 905 and fb.get("ext") == "mkv" and not fb.get("edition"), j["data"]
        s.get("/api/stop")

    @test("play: a film with no working file says it is the provider and drops out of recommendations")
    def p9():
        t0 = time.time()
        st, j = s.get("/api/play?kind=movie&id=906&ext=mkv", timeout=120)
        assert st != 200 and "provider-side" in j.get("error", ""), j
        items = lib("gone")
        assert items and items[0].get("dead"), items        # still findable, marked
        assert max_concurrent(fake.log(), t0) <= 1, "two provider connections"

    @test("play: an episode whose file is broken at the provider plays from another edition of the series")
    def p10():
        wait(lambda: s.get("/api/library?kind=series&q=test")[1]["data"]["items"], 60, what="series library")
        it = s.get("/api/library?kind=series&q=test")[1]["data"]["items"]
        assert len(it) == 1 and it[0]["variants"] == 2, it
        st, j = s.get("/api/series/info?id=500&name=" + urllib.parse.quote("EN - Test Show"))
        assert st == 200, j
        t0 = time.time()
        st, j = s.get("/api/play?kind=series&id=7002&ext=mkv", timeout=120)
        assert st == 200, j
        fb = j["data"].get("fallback") or {}
        assert fb.get("id") == 7102 and fb.get("edition"), j["data"]
        assert max_concurrent(fake.log(), t0) <= 1, "two provider connections"
        s.get("/api/stop")
        st, j = s.get("/api/library?kind=series&q=test")
        assert not j["data"]["items"][0].get("dead"), "one broken episode must not mark the whole series as unavailable"

    try:
        run([p1, p2, p3, p4, p5, p6, p7, p8, p9, p10])
    finally:
        s.stop(hard=True)
        fake.stop()


# ================================================================================== settings & library ====
def suite_settings():
    fake = Fake()
    s = Server(fake)
    wait(lambda: s.get("/api/library?kind=movie&limit=5")[1]["data"]["total"] > 0, 60, what="library loaded")

    @test("settings: saving an empty language list never hides every title (update 24 bug)")
    def s1():
        s.post("/api/settings", {"vod_langs": [], "profile": ""})
        st, j = s.get("/api/settings")
        langs = j["data"].get("vod_langs")
        assert langs is None or len(langs) > 0, langs
        st, j = s.get("/api/library?kind=movie")
        assert st == 200
        data = j["data"]
        n = data["total"]
        assert n > 0, "no movies visible after saving settings: %r" % str(data)[:300]

    @test("settings: a partial save keeps the other settings")
    def s2():
        s.post("/api/settings", {"live_buffer": 15, "rec_before": 3})
        s.post("/api/settings", {"rec_after": 7})
        st, j = s.get("/api/settings")
        d = j["data"]
        assert d.get("live_buffer") == 15 and d.get("rec_before") == 3 and d.get("rec_after") == 7, d

    @test("settings: a config.json with vod_langs [] is repaired at startup")
    def s3():
        s.stop()
        fp = os.path.join(s.cfg, "config.json")
        c = json.load(open(fp))
        c["settings"]["vod_langs"] = []
        json.dump(c, open(fp, "w"))
        s.start()
        st, j = s.get("/api/settings")
        assert j["data"].get("vod_langs") != [], j["data"].get("vod_langs")

    @test("library: watchlist add/remove per profile")
    def s4():
        st, j = s.get("/api/library?kind=movie")
        items = j["data"]["items"] if isinstance(j["data"], dict) else j["data"]
        gk = items[0]["key"]
        assert s.post("/api/watchlist", {"profile": "p1", "key": gk, "on": True})[0] == 200
        st, j = s.get("/api/watchlist?profile=p1")
        assert gk in json.dumps(j), j
        s.post("/api/watchlist", {"profile": "p1", "key": gk, "on": False})
        st, j = s.get("/api/watchlist?profile=p1")
        assert gk not in json.dumps(j.get("data", {}).get("keys", [])), j

    @test("library: endpoints answer without metadata keys (no TMDb/OMDb reachable)")
    def s5():
        for path in ("/api/home?profile=p1", "/api/shelves?kind=movie", "/api/discover?kind=movie&sort=best", "/api/search?q=inter",
                     "/api/sport", "/api/taste?profile=p1", "/api/ratings?keys=x", "/api/epg/now", "/api/recordings", "/api/profiles"):
            st, j = s.get(path, timeout=90)
            assert st == 200 and isinstance(j, dict) and j.get("ok") is not False, (path, st, str(j)[:200])

    try:
        run([s1, s2, s3, s4, s5])
    finally:
        s.stop(hard=True)
        fake.stop()


# ============================================================ night round 3 (update 34): robustness ====
def raw(url, method="GET", headers=None, body=None, timeout=30):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def suite_guard():
    fake = Fake()
    s = Server(fake, env={"IPTV_SESSION_IDLE": "12", "IPTV_ACCOUNT_AGE": "2"})

    @test("guard: the library answers while the film list is still loading (was a 500)")
    def g1():
        s2 = Server.__new__(Server)
        s2.cfg = tempfile.mkdtemp(prefix="bb-cfg-"); s2.port = free_port(); s2.url = "http://127.0.0.1:%d" % s2.port; s2.fake = fake
        s2.env = dict(s.env, IPTV_CONFIG_DIR=s2.cfg, IPTV_PORT=str(s2.port))
        s2.start(login=False)
        try:
            for path in ("/api/library?kind=movie", "/api/library?kind=series", "/api/status", "/api/recordings"):
                st, j = s2.get(path)
                assert st == 200, (path, st, str(j)[:200])
        finally:
            s2.stop(hard=True)

    @test("guard: a paused film is kept while its player pings, and cleaned up once the player is gone")
    def g2():
        st, j = s.get("/api/play?kind=movie&id=900&ext=mkv&force=1", timeout=120)
        assert st == 200 and j["data"].get("playlist"), j
        sid = re.search(r"/hls/([^/]+)/", j["data"]["playlist"]).group(1)
        end = time.time() + 30                               # well past the 12 s idle limit, no segment requests at all
        while time.time() < end:
            st, p = s.get("/api/ping?s=" + sid)
            assert p.get("alive") is True, "the session disappeared while the player was still there: %r" % p
            time.sleep(3)
        time.sleep(26)                                       # the player is gone (no pings): the janitor removes the stream
        st, p2 = s.get("/api/ping?s=" + sid)
        assert p2.get("alive") is False, p2
        wait(lambda: fake.log()["open"] == 0, 10, what="provider connection closed")

    @test("guard: POST /api/stop without a body (closing the window) frees the provider connection")
    def g3():
        s.get("/api/play?kind=live&id=100&ext=ts")
        time.sleep(1)
        st, body = raw(s.url + "/api/stop", "POST", body=b"")
        assert st == 200, (st, body)
        wait(lambda: fake.log()["open"] == 0, 10, what="connection closed")

    @test("guard: another website cannot use Beeldbuis on this Mac (Origin, Sec-Fetch-Site, DNS rebinding)")
    def g4():
        host = "127.0.0.1:%d" % s.port
        st, _ = raw(s.url + "/api/settings", "POST", {"Origin": "http://evil.example", "Content-Type": "text/plain"}, b'{"rec_after": 99}')
        assert st == 403, st
        st, _ = raw(s.url + "/api/settings", "GET", {"Host": "evil.example:%d" % s.port})
        assert st == 403, st
        st, _ = raw(s.url + "/api/keys", "GET", {"Sec-Fetch-Site": "cross-site"})
        assert st == 403, st
        st, _ = raw(s.url + "/api/settings", "GET", {"Origin": "null"})
        assert st == 403, st
        assert s.get("/api/settings")[1]["data"].get("rec_after") != 99
        # Beeldbuis' own window and the tools on this Mac keep working
        st, _ = raw(s.url + "/api/settings", "POST", {"Origin": "http://" + host, "Sec-Fetch-Site": "same-origin", "Content-Type": "application/json"}, b'{"rec_after": 6}')
        assert st == 200, st
        st, _ = raw(s.url + "/", "GET", {"Sec-Fetch-Site": "cross-site"})          # a link to Beeldbuis may still open it
        assert st == 200, st
        st, _ = raw("http://localhost:%d/api/ping" % s.port, "GET")
        assert st == 200, st

    @test("guard: a broken Range header on a recording does not crash the request")
    def g5():
        st, _ = raw(s.url + "/direct/rec/nope.mp4", "GET", {"Range": "bytes=abc-"})
        assert st in (400, 404, 416), st

    @test("guard: with the provider unreachable the app stays usable (no login screen, recordings and guide answer)")
    def g6():
        fake.stop()
        time.sleep(3)
        t0 = time.time()
        st, j = s.get("/api/status", timeout=40)
        assert st == 200 and j["data"]["logged_in"] and j["data"].get("provider_error"), (st, str(j)[:300])
        assert time.time() - t0 < 20, "status took %.0f s with the provider down" % (time.time() - t0)
        t0 = time.time()
        st, j = s.get("/api/status", timeout=40)
        assert st == 200 and time.time() - t0 < 2, "the second status call was not answered from the cache"
        for path in ("/api/recordings", "/api/library?kind=movie&limit=5", "/api/epg/now", "/api/settings"):
            st, j = s.get(path, timeout=40)
            assert st == 200, (path, st, str(j)[:200])

    try:
        run([g1, g2, g3, g4, g5, g6])
    finally:
        s.stop(hard=True)
        try:
            fake.stop()
        except Exception:  # noqa
            pass


SUITES = collections.OrderedDict([("static", suite_static), ("rec", suite_rec), ("play", suite_play), ("settings", suite_settings),
                                  ("guard", suite_guard)])

if __name__ == "__main__":
    want = sys.argv[1:] or list(SUITES)
    for name in want:
        print("== %s" % name, flush=True)
        SUITES[name]()
    bad = [r for r in RESULTS if not r[1]]
    print("\n%d passed, %d failed" % (len(RESULTS) - len(bad), len(bad)))
    for n, _, e in bad:
        print("  FAIL %s\n       %s" % (n, e))
    sys.exit(1 if bad else 0)
