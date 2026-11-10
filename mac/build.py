#!/usr/bin/env python3
"""Inline src/ui.html (and assets/substyle.js) into src/server.py -> pkg/iptv.py"""
import glob, os, re, sys, py_compile
root = os.path.dirname(os.path.abspath(__file__))
srv = open(os.path.join(root, 'src/server.py'), encoding='utf-8').read()
ui = open(os.path.join(root, 'src/ui.html'), encoding='utf-8').read()
# the subtitle style model is shared with the TV app: assets/substyle.js is the one copy;
# the newest tv-*-src/js gets the same file
sub = open(os.path.join(root, 'assets/substyle.js'), encoding='utf-8').read()
ui = ui.replace('/*__SUBSTYLE_JS__*/', sub)
assert '"' * 3 not in ui, 'triple quotes in ui.html or substyle.js'
tvs = sorted(glob.glob(os.path.join(root, 'tv-*-src')), key=lambda d: [int(x) for x in re.findall(r'\d+', os.path.basename(d))])
if tvs and os.path.isdir(os.path.join(tvs[-1], 'js')):
    dst_tv = os.path.join(tvs[-1], 'js', 'substyle.js')
    if not os.path.exists(dst_tv) or open(dst_tv, encoding='utf-8').read() != sub:
        open(dst_tv, 'w', encoding='utf-8').write(sub)
        print('copied substyle.js to', dst_tv)
out = srv.replace('__UI_HTML__', ui)
os.makedirs(os.path.join(root, 'pkg'), exist_ok=True)
dst = os.path.join(root, 'pkg/iptv.py')
open(dst, 'w', encoding='utf-8').write(out)
py_compile.compile(dst, doraise=True)
print('built', dst, len(out), 'bytes')
