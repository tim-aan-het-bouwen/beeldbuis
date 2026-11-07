/* Channel grouping: merges quality variants ("NPO 1 HD", "NPO 1 FHD", "NL| NPO 1 4K") into one channel.
   Port of build_groups/clean_name in mac/src/server.py. Plain ES2017 so it runs on 2018 Samsung TVs. */
(function (root) {
  'use strict';
  var SUP = { 'ᴬ': 'A', 'ᴮ': 'B', 'ᶜ': 'C', 'ᴰ': 'D', 'ᴱ': 'E', 'ᶠ': 'F', 'ᴳ': 'G', 'ᴴ': 'H', 'ᴵ': 'I', 'ᴶ': 'J', 'ᴷ': 'K',
    'ᴸ': 'L', 'ᴹ': 'M', 'ᴺ': 'N', 'ᴼ': 'O', 'ᴾ': 'P', 'ᴿ': 'R', 'ˢ': 'S', 'ᵀ': 'T', 'ᵁ': 'U', 'ⱽ': 'V', 'ᵂ': 'W',
    'ᵃ': 'A', 'ᵇ': 'B', 'ᵈ': 'D', 'ᵉ': 'E', 'ᵍ': 'G', 'ʰ': 'H', 'ᶦ': 'I', 'ⁱ': 'I', 'ʲ': 'J', 'ᵏ': 'K', 'ˡ': 'L',
    'ᵐ': 'M', 'ⁿ': 'N', 'ᵒ': 'O', 'ᵖ': 'P', 'ʳ': 'R', 'ᵗ': 'T', 'ᵘ': 'U', 'ᵛ': 'V', 'ʷ': 'W', 'ˣ': 'X', 'ʸ': 'Y',
    'ᶻ': 'Z', '⁰': '0', '¹': '1', '²': '', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6', '⁷': '7', '⁸': '8', '⁹': '9',
    '◉': ' ', '•': ' ', ' ': ' ', '★': ' ', '☆': ' ', '►': ' ', '▶': ' ' };
  var QUAL = '8K|4K|UHD|FHD|HD|SD|RAW|HEVC|H265|H\\.265|LQ|HQ|VIP|BACKUP|BK|ULTRA|HDR|50FPS|60FPS|1080P|720P|2160P|3840P|1920P|SUPER|GOLD|SUPE';
  // no lookbehind on old engines: capture the leading boundary instead
  var QUAL_RE = new RegExp('(^|[^A-Z0-9])(' + QUAL + ')(?![A-Z0-9])', 'g');
  var PREFIX_RE = /^\s*[A-Za-z0-9+\-]{1,7}\s*\|\s*/;
  var RANK = { '8K': 5, '4K': 5, 'RAW': 4, 'FHD': 3, 'HD': 2, '': 2, 'SD': 1 };

  function tr(s) {
    var out = '';
    for (var i = 0; i < s.length; i++) { var c = s.charAt(i); out += (c in SUP) ? SUP[c] : c; }
    return out;
  }
  function tokens(up) {
    var t = {}, m;
    QUAL_RE.lastIndex = 0;
    while ((m = QUAL_RE.exec(up))) { t[m[2]] = 1; QUAL_RE.lastIndex = m.index + m[1].length + m[2].length; }
    return t;
  }
  function any(t, keys) { for (var i = 0; i < keys.length; i++) if (t[keys[i]]) return true; return false; }

  function cleanName(name) {
    var s = tr(name || '');
    if (PREFIX_RE.test(s)) {
      var i = s.indexOf('|'), first = s.slice(0, i).trim().toUpperCase(), rest = s.slice(i + 1);
      if (rest.indexOf('|') < 0 || (first.length <= 5 && ['NEXT', 'LIVE', 'NOW', 'NO', 'TODAY'].indexOf(first) < 0)) s = rest;
    }
    var up = s.toUpperCase(), t = tokens(up), q = '';
    if (t['8K']) q = '8K';
    else if (any(t, ['4K', 'UHD', '2160P', '3840P'])) q = '4K';
    else if (t.RAW) q = 'RAW';
    else if (any(t, ['FHD', '1080P', '1920P'])) q = 'FHD';
    else if (any(t, ['HD', '720P', 'HEVC', 'H265', 'H.265'])) q = 'HD';
    else if (any(t, ['SD', 'LQ'])) q = 'SD';
    s = up.replace(QUAL_RE, '$1 ').replace(QUAL_RE, '$1 ');
    s = s.replace(/[()\[\]\/]+/g, ' ').replace(/\s+/g, ' ').replace(/^[\s\-–|:.]+|[\s\-–|:.]+$/g, '');
    return [s, q];
  }
  function categoryCountry(n) { var c = tr(n || ''); return c.indexOf('|') >= 0 ? (c.split('|')[0].trim().toUpperCase() || 'OTHER') : 'OTHER'; }
  function categoryQuality(n) {
    var t = tokens(tr(n || '').toUpperCase());
    if (t['8K']) return '8K'; if (any(t, ['4K', 'UHD'])) return '4K'; if (t.RAW) return 'RAW';
    if (t.FHD) return 'FHD'; if (any(t, ['HD', 'HEVC'])) return 'HD'; if (any(t, ['SD', 'LQ'])) return 'SD';
    return '';
  }

  function buildGroups(cats, streams) {
    var catname = {}, catq = {}, catc = {};
    cats.forEach(function (c) { var id = String(c.category_id); catname[id] = c.category_name || ''; catq[id] = categoryQuality(catname[id]); catc[id] = categoryCountry(catname[id]); });
    var groups = {}, order = [], n = 0;
    streams.forEach(function (s) {
      var name = (s.name || '').trim();
      if (!name || name.charAt(0) === '#' || name.charAt(0) === '-') return;
      var cid = String(s.category_id), cn = cleanName(name), disp = cn[0] || name, q = cn[1] || catq[cid] || '';
      var country = catc[cid] || 'OTHER', key = country + ':' + disp.toUpperCase().replace(/[^A-Z0-9]/g, '');
      var arch = String(s.tv_archive) === '1' ? (parseInt(s.tv_archive_duration, 10) || 0) : 0;
      var v = { id: parseInt(s.stream_id, 10), q: q, rank: RANK[q] !== undefined ? RANK[q] : 2, cat: cid, name: name, archive: arch, _i: n++ };
      var g = groups[key];
      if (!g) {
        g = groups[key] = { key: key, name: disp, icon: s.stream_icon || '', epg: s.epg_channel_id || '', cats: [cid], variants: [v], country: country, _names: [disp] };
        order.push(key);
      } else {
        g._names.push(disp);
        if (g.cats.indexOf(cid) < 0) g.cats.push(cid);
        if (!g.epg && s.epg_channel_id) g.epg = s.epg_channel_id;
        if (!g.icon && s.stream_icon) g.icon = s.stream_icon;
        g.variants.push(v);
      }
    });
    return order.map(function (key) {
      var g = groups[key];
      g.variants.sort(function (a, b) { return (b.rank - a.rank) || ((a.q === '8K') - (b.q === '8K')) || (a._i - b._i); });
      g.variants.forEach(function (v) { delete v._i; });
      if (g._names.length > 1) {
        var cnt = {};
        g._names.forEach(function (x) { cnt[x] = (cnt[x] || 0) + 1; });
        g.name = Object.keys(cnt).sort(function (a, b) { return (cnt[b] - cnt[a]) || (b.length - a.length); })[0];
      }
      delete g._names;
      g.archive = Math.max.apply(null, g.variants.map(function (v) { return v.archive; }).concat([0]));
      g.best = g.variants[0].q;
      return g;
    });
  }

  var api = { cleanName: cleanName, categoryCountry: categoryCountry, categoryQuality: categoryQuality, buildGroups: buildGroups };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.BBGroups = api;
})(this);
