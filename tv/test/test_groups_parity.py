"""Check that tv/app/js/groups.js gives the same channel grouping as mac/src/server.py."""
import json, os, random, subprocess, sys, importlib.util, types
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
src = open(os.path.join(ROOT, "mac/src/server.py"), encoding="utf-8").read()
start = src.index("_SUP = {"); end = src.index("def catalog_load")
mod = types.ModuleType("g"); exec("import re\n" + src[start:end], mod.__dict__)
names = ["NL| NPO 1 ᴴᴰ", "NL| NPO 1 FHD", "NL| NPO 1 4K", "NL| NPO 1", "UK| SPORTS MAIN UHD", "UK| Sports Main HD",
         "NEXT | Big Fight Night | Sun 08 Nov 00:00 CET (NL) | x", "US| ESPN (BACKUP)", "◉ NL| SPORT 2 ᶠᴴᴰ", "SPORT PPV 1 RAW",
         "DE| DAS ERSTE HEVC", "4K| NATURE [2160P]", "NL| RTL 4 HD/FHD", "## NL ##", "- sep -", "NL| SBS 6 50FPS", "FR| TF1 HD HD"]
cats = [{"category_id": i, "category_name": n} for i, n in enumerate(["NL| SPORT", "UK| SPORTS HD", "US| ENTERTAINMENT 4K", "4K| UHD", "OTHER"])]
random.seed(1)
streams = [{"stream_id": i + 1, "name": n, "category_id": random.randrange(5), "tv_archive": random.choice([0, 1]),
            "tv_archive_duration": random.choice([1, 3]), "stream_icon": "", "epg_channel_id": "e%d" % i} for i, n in enumerate(names * 3)]
py_groups, _ = mod.build_groups(cats, streams)
py_clean = [list(mod.clean_name(n)) for n in names]
js = subprocess.run(["node", "-e", """
const G=require(process.argv[1]); const d=JSON.parse(require('fs').readFileSync(0,'utf8'));
console.log(JSON.stringify({clean:d.names.map(G.cleanName), groups:G.buildGroups(d.cats,d.streams)}));""",
                     os.path.join(ROOT, "tv/app/js/groups.js")], input=json.dumps({"names": names, "cats": cats, "streams": streams}),
                    capture_output=True, text=True, check=True)
out = json.loads(js.stdout)
bad = [(n, p, j) for n, p, j in zip(names, py_clean, out["clean"]) if p != j]
for b in bad: print("clean_name differs:", b)
if json.loads(json.dumps(py_groups)) != out["groups"]:
    print("groups differ"); print(json.dumps(py_groups)[:800]); print(json.dumps(out["groups"])[:800]); sys.exit(1)
sys.exit(1 if bad else print("groups.js matches server.py:", len(names), "names,", len(out["groups"]), "groups") or 0)
