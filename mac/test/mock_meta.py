#!/usr/bin/env python3
"""Mock TMDb + OMDb + OpenSubtitles + Letterboxd on 127.0.0.1:8898 for offline tests.
Point the server at it with:
  IPTV_TMDB_BASE=http://127.0.0.1:8898/3 IPTV_TMDB_IMG=http://127.0.0.1:8898/t/p
  IPTV_OMDB_BASE=http://127.0.0.1:8898/omdb IPTV_OSUB_BASE=http://127.0.0.1:8898/api/v1
  IPTV_LB_BASE=http://127.0.0.1:8898/lb
Titles are answered deterministically (id = hash of the title) so the server's matching logic is exercised,
and trending lists are built from real provider names in test/vod so they match the library."""
import hashlib, json, os, re, sys, urllib.parse, zlib
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
VOD = os.path.join(HERE, "vod")
GENRE_POOL = [[878, 12], [18], [9648, 53], [16, 10759], [14, 12], [35], [28, 80], [10765, 18], [99], [10749, 35]]
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xcf\xc0\xf0\x1f\x00\x05\x00\x01\xff\xa5\xf2\x0f\x00\x00\x00\x00IEND\xaeB`\x82")
SRT = "\n".join("%d\n00:%02d:%02d,000 --> 00:%02d:%02d,500\nMock subtitle line %d" % (i, i // 60, i % 60, i // 60, i % 60, i) for i in range(1, 40))
RSS = """<?xml version="1.0" encoding="UTF-8"?><rss xmlns:letterboxd="https://letterboxd.com" xmlns:tmdb="https://themoviedb.org" version="2.0"><channel><title>Letterboxd - mock</title>
<item><title>Dune: Part Two, 2024 - ★★★★★</title><letterboxd:filmTitle>Dune: Part Two</letterboxd:filmTitle><letterboxd:filmYear>2024</letterboxd:filmYear><letterboxd:memberRating>5.0</letterboxd:memberRating><tmdb:movieId>693134</tmdb:movieId></item>
<item><title>Oppenheimer, 2023 - ★★★★</title><letterboxd:filmTitle>Oppenheimer</letterboxd:filmTitle><letterboxd:filmYear>2023</letterboxd:filmYear><letterboxd:memberRating>4.0</letterboxd:memberRating><tmdb:movieId>872585</tmdb:movieId></item>
<item><title>Madame Web, 2024 - ★</title><letterboxd:filmTitle>Madame Web</letterboxd:filmTitle><letterboxd:filmYear>2024</letterboxd:filmYear><letterboxd:memberRating>1.0</letterboxd:memberRating><tmdb:movieId>634492</tmdb:movieId></item>
</channel></rss>"""

def tid(title, year=0):
    return int(hashlib.md5(("%s|%s" % (title.lower(), year or "")).encode()).hexdigest()[:6], 16) + 1000

def clean(name):
    name = re.sub(r"^[A-Z0-9+]{1,6}\s*[-|:]\s*", "", name)
    m = re.search(r"\((19|20)\d\d\)", name)
    year = int(m.group(0)[1:5]) if m else 0
    title = name[:m.start()].strip() if m else re.sub(r"\s+S\d\d.*$", "", name).strip()
    return title, year

def load_titles(fn, n):
    try:
        items = json.load(open(os.path.join(VOD, fn)))
    except OSError:
        return []
    out, seen = [], set()
    for it in items[:4000]:
        t, y = clean(it.get("name", ""))
        if len(t) < 3 or t.lower() in seen or "18+" in it.get("name", ""):
            continue
        seen.add(t.lower())
        out.append((t, y))
        if len(out) >= n:
            break
    return out

MOVIES = load_titles("get_vod_streams.json", 60)
SERIES = load_titles("get_series.json", 60)

def record(kind, title, year, rank=0):
    h = zlib.crc32(title.lower().encode())
    i = tid(title, year)
    date = "%04d-%02d-01" % (year or 2000 + h % 26, 1 + h % 12)
    r = {"id": i, "overview": "Mock overview for %s." % title, "poster_path": "/p%d.jpg" % i, "backdrop_path": "/b%d.jpg" % i,
         "vote_average": 5.5 + (h % 40) / 10.0, "vote_count": 300 + h % 5000, "popularity": 20 + (h % 900), "original_language": "en",
         "genre_ids": GENRE_POOL[h % len(GENRE_POOL)]}
    if kind == "movie":
        r.update(title=title, original_title=title, release_date=date)
    else:
        r.update(name=title, original_name=title, first_air_date=date)
    return r

def detail(kind, i):
    title, year = BY_ID.get(i, ("Unknown %d" % i, 0))
    r = record(kind, title, year)
    r["genres"] = [{"id": g, "name": str(g)} for g in r.pop("genre_ids")]
    r["external_ids"] = {"imdb_id": "tt%07d" % (i % 10000000)}
    r["imdb_id"] = r["external_ids"]["imdb_id"]
    if kind == "movie":
        r["runtime"] = 95 + i % 60
        r["production_companies"] = [{"name": "Mock Studios"}]
    else:
        r["episode_run_time"] = [45]
        r["number_of_seasons"] = 1 + i % 4
        r["status"] = "Returning Series"
        r["networks"] = [{"name": "Mock TV"}]
        r["last_air_date"] = "2026-10-01"
    return r

BY_ID = {}
for t, y in MOVIES + SERIES:
    BY_ID[tid(t, y)] = (t, y)

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.command, self.path.split("?")[0]))

    def send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        p = self.path.split("?")[0]
        if p.endswith("/login"):
            return self.send(200, {"token": "mock-token", "user": {"allowed_downloads": 20}})
        if p.endswith("/download"):
            return self.send(200, {"link": "http://127.0.0.1:8898/files/mock.srt", "file_name": "mock.srt", "remaining": 19})
        self.send(404, {"error": "no"})

    def do_GET(self):
        u = urllib.parse.urlsplit(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        p = u.path
        if p.startswith("/t/p/"):
            return self.send(200, PNG, "image/png")
        if p == "/files/mock.srt":
            return self.send(200, SRT, "text/plain")
        if p.startswith("/lb/"):
            return self.send(200, RSS, "application/rss+xml")
        if p.startswith("/omdb"):
            i = q.get("i", "")
            n = int(re.sub(r"\D", "", i) or 0)
            return self.send(200, {"Title": "Mock", "imdbRating": "%.1f" % (6.0 + n % 35 / 10.0), "Metascore": str(50 + n % 50), "Rated": "TV-MA" if n % 2 else "PG-13",
                                   "Ratings": [{"Source": "Internet Movie Database", "Value": "7.5/10"}, {"Source": "Rotten Tomatoes", "Value": "%d%%" % (60 + n % 40)}],
                                   "Response": "True"})
        if p.startswith("/api/v1/subtitles"):
            attrs = {"language": "en", "download_count": 1000, "hearing_impaired": False, "ai_translated": False, "machine_translated": False,
                     "files": [{"file_id": 12345, "file_name": "mock.srt"}]}
            return self.send(200, {"total_count": 1, "data": [{"id": "1", "type": "subtitle", "attributes": attrs}]})
        if p.startswith("/3/"):
            return self.tmdb(p[2:], q)
        self.send(404, {"error": "no"})

    def tmdb(self, p, q):
        m = re.match(r"^/search/(movie|tv)$", p)
        if m:
            kind = "movie" if m.group(1) == "movie" else "series"
            title = q.get("query", "")
            yr = int(q.get("year") or q.get("first_air_date_year") or 0)
            BY_ID.setdefault(tid(title, yr), (title, yr))
            res = [record(kind, title, yr)]
            if len(title) > 4:  # a decoy result with a different title so the matcher has to choose
                res.append(record(kind, title + " Reloaded", 1999))
            return self.send(200, {"page": 1, "results": res, "total_results": len(res)})
        m = re.match(r"^/trending/(movie|tv)/(day|week)$", p)
        if m:
            kind = "movie" if m.group(1) == "movie" else "series"
            pool = MOVIES if kind == "movie" else SERIES
            off = 0 if m.group(2) == "day" else 10
            return self.send(200, {"results": [record(kind, t, y, i) for i, (t, y) in enumerate(pool[off:off + 40])]})
        m = re.match(r"^/discover/(movie|tv)$", p)
        if m:
            kind = "movie" if m.group(1) == "movie" else "series"
            pool = MOVIES if kind == "movie" else SERIES
            gid = int(q.get("with_genres") or 0)
            res = [record(kind, t, y) for t, y in pool]
            res = [r for r in res if not gid or gid in r["genre_ids"]] or res[:10]
            return self.send(200, {"results": res[:20]})
        m = re.match(r"^/(movie|tv)/(\d+)(/recommendations|/similar)?$", p)
        if m:
            kind = "movie" if m.group(1) == "movie" else "series"
            i = int(m.group(2))
            if m.group(3):
                pool = MOVIES if kind == "movie" else SERIES
                return self.send(200, {"results": [record(kind, t, y) for t, y in pool[i % 20:i % 20 + 12]]})
            if i not in BY_ID:
                return self.send(404, {"status_code": 34, "status_message": "not found"})
            return self.send(200, detail(kind, i))
        self.send(404, {"status_code": 34, "status_message": "not found: " + p})

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8898
    print("mock meta on http://127.0.0.1:%d (%d movies, %d series known)" % (port, len(MOVIES), len(SERIES)))
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
