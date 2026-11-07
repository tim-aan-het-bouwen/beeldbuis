#!/usr/bin/env python3
"""Inline src/ui.html into src/server.py -> pkg/iptv.py"""
import os, sys, py_compile
root = os.path.dirname(os.path.abspath(__file__))
srv = open(os.path.join(root, 'src/server.py'), encoding='utf-8').read()
ui = open(os.path.join(root, 'src/ui.html'), encoding='utf-8').read()
assert '"""' not in ui, 'triple quotes in ui.html'
out = srv.replace('__UI_HTML__', ui)
os.makedirs(os.path.join(root, 'pkg'), exist_ok=True)
dst = os.path.join(root, 'pkg/iptv.py')
open(dst, 'w', encoding='utf-8').write(out)
py_compile.compile(dst, doraise=True)
print('built', dst, len(out), 'bytes')
