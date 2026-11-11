#!/usr/bin/env python3
"""Browser preview of the TV app for tests: serves a tv-x.y.z-src folder plus a made-up Xtream catalogue
(5 channel groups, 8 film categories x 30, 80 series, a guide) and the /proxy the browser build expects.
Usage: python3 test/tv_preview.py <tv source folder> <port>"""
import json, os, sys, time, urllib.parse, http.server, random
ROOT = sys.argv[1]; PORT = int(sys.argv[2])
random.seed(1)
now = int(time.time())
LC = [("1","NL| GENERAL"),("2","NL| SPORT"),("3","UK| SPORT"),("4","UK| ENTERTAINMENT"),("5","US| NEWS")]
LS = []
for c, n in LC:
    for i in range(14):
        sid = int(c)*100+i
        LS.append({"stream_id": sid, "name": "%s CH %d FHD" % (n.split('|')[0], sid), "category_id": c, "epg_channel_id": "c%d" % sid, "tv_archive": 1, "tv_archive_duration": 3, "stream_icon": ""})
VC = [(str(10+i), "EN - %s" % g) for i, g in enumerate(["ACTION","COMEDY","DRAMA","THRILLER","SCI-FI","KIDS","DOCS","4K MOVIES"])]
VS = []
for c, n in VC:
    for i in range(30):
        sid = int(c)*1000+i
        VS.append({"stream_id": sid, "name": "EN - Film %s %d (%d)" % (n[5:].title(), i, 1980+i), "category_id": c, "container_extension": "mp4", "rating": str(5+i%5), "added": str(now-86400*i), "stream_icon": ""})
SE = [{"series_id": 5000+i, "name": "EN - Show %d" % i, "category_id": VC[i%8][0], "rating": "8", "cover": "", "plot": "A show.", "genre": "Drama", "releaseDate": "2020-01-01"} for i in range(80)]
def xmltv():
    t0 = now // 1800 * 1800; f = lambda t: time.strftime("%Y%m%d%H%M%S +0000", time.gmtime(t))
    out = ['<?xml version="1.0" encoding="utf-8"?><tv>']
    for st in LS:
        out.append('<channel id="%s"><display-name>%s</display-name></channel>' % (st["epg_channel_id"], st["name"]))
        k = -48
        while k < 60:
            d = random.choice([1, 2, 3])
            out.append('<programme start="%s" stop="%s" channel="%s"><title>Prog %d on %d</title><desc>Test.</desc></programme>' % (f(t0 + k * 1800), f(t0 + (k + d) * 1800), st["epg_channel_id"], k, st["stream_id"]))
            k += d
    out.append("</tv>"); return "\n".join(out).encode()
def api(a, q):
    if a == "": return {"user_info": {"auth": 1, "status": "Active", "max_connections": "1", "active_cons": "0", "exp_date": str(now+86400*300), "username": "test"}, "server_info": {"timezone": "UTC", "url": "localhost"}}
    if a == "get_live_categories": return [{"category_id": c, "category_name": n} for c, n in LC]
    if a == "get_live_streams":
        c = q.get("category_id", [None])[0]; return [s for s in LS if not c or s["category_id"] == c]
    if a in ("get_vod_categories", "get_series_categories"): return [{"category_id": c, "category_name": n} for c, n in VC]
    if a == "get_vod_streams":
        c = q.get("category_id", [None])[0]; return [s for s in VS if not c or s["category_id"] == c]
    if a == "get_series":
        c = q.get("category_id", [None])[0]; return [s for s in SE if not c or s["category_id"] == c]
    if a == "get_series_info": return {"info": {"name": "Show"}, "episodes": {str(s): [{"id": str(7000+s*10+e), "episode_num": e, "title": "Ep %d" % e, "container_extension": "mp4", "season": s, "info": {"duration_secs": 1800}} for e in range(1, 9)] for s in (1, 2)}}
    if a == "get_vod_info": return {"info": {"duration_secs": 5400, "plot": "A film."}, "movie_data": {"stream_id": q.get("vod_id", [""])[0], "container_extension": "mp4"}}
    if a == "get_short_epg": return {"epg_listings": []}
    return []
class H(http.server.SimpleHTTPRequestHandler):
    def __init__(s, *a, **k): super().__init__(*a, directory=ROOT, **k)
    def log_message(s, *a): pass
    def send(s, b, ct="application/json"):
        s.send_response(200); s.send_header("Content-Type", ct); s.send_header("Content-Length", str(len(b))); s.end_headers(); s.wfile.write(b)
    def fwd(s, target, body=None):
        import urllib.request
        hdr = {"Content-Type": s.headers.get("Content-Type", "application/json")}
        if s.headers.get("Range"): hdr["Range"] = s.headers["Range"]          # byte ranges pass through (mp4 index at the end)
        req = urllib.request.Request(target, data=body, method=s.command, headers=hdr)
        extra = {}
        try:
            r = urllib.request.urlopen(req, timeout=60); b = r.read(); code = r.status; ct = r.headers.get("Content-Type", "")
            extra = {k: r.headers[k] for k in ("Content-Range", "Accept-Ranges") if r.headers.get(k)}
        except urllib.error.HTTPError as e:
            b = e.read(); code = e.code; ct = e.headers.get("Content-Type", "")
        s.send_response(code); s.send_header("Content-Type", ct); s.send_header("Content-Length", str(len(b)))
        for k, v in extra.items(): s.send_header(k, v)
        s.end_headers(); s.wfile.write(b)
    def own(s, t):
        q = urllib.parse.parse_qs(t.query)
        if t.path.endswith("player_api.php"): return s.send(json.dumps(api(q.get("action", [""])[0], q)).encode())
        if t.path.endswith("xmltv.php"): return s.send(xmltv(), "text/xml")
        s.send_response(404); s.send_header("Content-Length", "0"); s.end_headers()
    def do_POST(s):
        u = urllib.parse.urlsplit(s.path)
        body = s.rfile.read(int(s.headers.get("Content-Length", 0) or 0))
        return s.fwd(urllib.parse.parse_qs(u.query)["u"][0], body)
    def do_GET(s):
        u = urllib.parse.urlsplit(s.path)
        if u.path == "/proxy":
            target = urllib.parse.parse_qs(u.query)["u"][0]; t = urllib.parse.urlsplit(target)
            if t.netloc == "prov.test" or t.port == PORT: return s.own(t)
            return s.fwd(target)
        if u.path.endswith(("player_api.php", "xmltv.php")): return s.own(u)
        return super().do_GET()
http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
