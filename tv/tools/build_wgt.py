#!/usr/bin/env python3
"""Package tv/app/ as dist/Beeldbuis.wgt (unsigned).

A Samsung TV only installs a signed package. Signing needs a Samsung author certificate and a
distributor certificate that lists the TV's DUID (both made once in Tizen Studio's Certificate Manager);
see tv/README.md. `tizen package -t wgt -s <profile> -- dist/Beeldbuis.wgt` signs this file."""
import os, re, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")
DIST = os.path.join(os.path.dirname(ROOT), "dist")
SKIP = re.compile(r"(^|/)(\.|__pycache__|.*\.map$|hls\.min\.js$)")  # hls.js is only for the browser preview
os.makedirs(DIST, exist_ok=True)
out = os.path.join(DIST, "Beeldbuis.wgt")
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for d, _, files in os.walk(APP):
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(d, f), APP).replace(os.sep, "/")
            if not SKIP.search(rel):
                z.write(os.path.join(d, f), rel)
print("built", out, os.path.getsize(out), "bytes")
