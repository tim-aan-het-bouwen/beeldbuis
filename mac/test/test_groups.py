import json, sys, time, collections
src = open('src/server.py', encoding='utf-8').read().replace('__UI_HTML__','')
code = src[:src.index('# --------------------------------------------------------------- handler ----')]
ns = {'__file__': '/home/user/beeldbuis/src/server.py'}
sys.argv=['x']
exec(compile(code, 'server_part', 'exec'), ns)
cats=json.load(open('test/live_cats.json')); streams=json.load(open('test/live_streams.json'))
t=time.time()
groups, by_stream = ns['build_groups'](cats, streams)
print(len(groups), 'groups from', len(streams), 'streams in %.2fs' % (time.time()-t))
for n in ['NL| NPO 1 HD','NL| ESPN 1 HD','UK| SKY SPORTS F1 HD','NL| ZIGGO SPORT HD ²','NEXT | UFC Fight Night | Sun 08 Nov 00:00 CET (NL) | X','NL-CW| NPO 1 ᴿᴬᵂ','NL| VIAPLAY PPV 3','UK|SKY SPORTS ACTION HD','NL| PRIME ᴿᴬᵂ ⁶⁰ᶠᵖˢ','UK| SKY SPORTS MAIN EVENTS HD(BK)','4K| SKY SPORTS F1 UHD']:
    print(repr(n), '->', ns['clean_name'](n))
bk = {g['key']: g for g in groups}
for k in ['NL:NPO1','NL:ESPN1','UK:SKYSPORTSF1','NL:ZIGGOSPORT','NL:ESPN','NL:RTL4']:
    g = bk.get(k); print(k, g and (g['name'], g['epg'], g['archive'], g['best'], [(v['q'], v['name']) for v in g['variants']]))
print('variant count dist', collections.Counter(min(len(g['variants']),6) for g in groups))
print(len(json.dumps({"groups":groups}))/1e6, 'MB json')
