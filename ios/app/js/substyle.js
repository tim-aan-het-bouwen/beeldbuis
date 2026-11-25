/* Subtitle style: one model for the TV app and the Mac/iPhone app (the Mac's ui.html carries the same code).
   A style is a flat object of the keys in GROUPS below. css(style, frame) turns it into inline CSS for the
   subtitle box (position, width, alignment) and for the text (font, colours, background, outline). */
(function (root) {
  'use strict';

  var COLORS = {
    white: ['White', '255,255,255'], yellow: ['Yellow', '255,225,77'], creme: ['Cream', '244,232,200'], oker: ['Ochre', '240,180,41'],
    cyan: ['Cyan', '111,231,255'], green: ['Green', '125,255,138'], pink: ['Pink', '255,150,200'],
    black: ['Black', '0,0,0'], navy: ['Navy', '14,15,18'], grey: ['Dark grey', '45,48,56'], teal: ['Teal', '29,158,153']
  };
  function pick(keys) { return keys.map(function (k) { return [k, COLORS[k][0]]; }); }
  var FONTS = {
    sans: ['Sans', '"Helvetica Neue", Helvetica, Arial, "SamsungOne", "Tizen Sans", sans-serif'],
    serif: ['Serif', 'Georgia, "Times New Roman", "DejaVu Serif", serif'],
    mono: ['Typewriter', '"Courier New", Menlo, "DejaVu Sans Mono", monospace'],
    round: ['Rounded', '"Arial Rounded MT Bold", "Varela Round", "Trebuchet MS", sans-serif'],
    narrow: ['Condensed', '"Arial Narrow", "Helvetica Neue Condensed", "Roboto Condensed", sans-serif']
  };

  // [key, label, kind, choices | [min, max, step, unit]]
  var GROUPS = [
    ['text', 'Text', [
      ['size', 'Size', 'range', [50, 220, 10, '%']],
      ['color', 'Colour', 'choice', pick(['white', 'yellow', 'creme', 'oker', 'cyan', 'green', 'pink'])],
      ['opacity', 'Text opacity', 'range', [40, 100, 10, '%']],
      ['font', 'Font', 'choice', Object.keys(FONTS).map(function (k) { return [k, FONTS[k][0]]; })],
      ['weight', 'Weight', 'choice', [['400', 'Regular'], ['600', 'Medium'], ['800', 'Bold']]],
      ['spacing', 'Letter spacing', 'range', [0, 6, 1, '']],
      ['lineh', 'Line spacing', 'range', [100, 170, 10, '%']],
      ['caps', 'Capitals', 'choice', [['off', 'As written'], ['on', 'ALL CAPS']]]
    ]],
    ['bg', 'Background', [
      ['bg', 'Colour', 'choice', [['none', 'None']].concat(pick(['black', 'navy', 'grey', 'teal', 'creme', 'white']))],
      ['bgop', 'Opacity', 'range', [10, 100, 10, '%']],
      ['shape', 'Shape', 'choice', [['line', 'Strip per line'], ['block', 'One block'], ['bar', 'Full-width bar']]],
      ['radius', 'Corners', 'range', [0, 24, 4, 'px']],
      ['pad', 'Padding', 'range', [0, 30, 5, 'px']]
    ]],
    ['edge', 'Outline', [
      ['edge', 'Outline width', 'range', [0, 6, 1, 'px']],
      ['edgecol', 'Outline colour', 'choice', pick(['black', 'navy', 'grey', 'white', 'oker'])],
      ['shadow', 'Shadow', 'choice', [['none', 'None'], ['soft', 'Soft'], ['hard', 'Hard'], ['glow', 'Glow']]]
    ]],
    ['pos', 'Position', [
      ['lift', 'Height', 'range', [2, 86, 4, '%']],
      ['align', 'Alignment', 'choice', [['left', 'Left'], ['center', 'Centre'], ['right', 'Right']]],
      ['width', 'Max width', 'range', [40, 96, 4, '%']],
      ['avoid', 'Move up for the player bar', 'choice', [['on', 'Yes'], ['off', 'No']]]
    ]]
  ];

  var DEFAULT = { size: 100, color: 'white', opacity: 100, font: 'sans', weight: '600', spacing: 0, lineh: 120, caps: 'off',
    bg: 'none', bgop: 60, shape: 'line', radius: 4, pad: 10, edge: 2, edgecol: 'black', shadow: 'soft',
    lift: 6, align: 'center', width: 84, avoid: 'on' };

  function ext(base, over) { var o = {}, k; for (k in base) o[k] = base[k]; for (k in over) o[k] = over[k]; return o; }
  var PRESETS = [
    ['classic', 'Classic', 'White with a thin black outline', ext(DEFAULT, {})],
    ['stream', 'Streaming', 'Like the big streaming services: clean white, soft shadow', ext(DEFAULT, { weight: '600', edge: 1, shadow: 'soft', size: 95 })],
    ['cinema', 'Cinema', 'Yellow, bold, hard shadow', ext(DEFAULT, { color: 'yellow', weight: '800', edge: 3, shadow: 'hard' })],
    ['big', 'Big and clear', 'Large text on a dark block', ext(DEFAULT, { size: 150, weight: '800', bg: 'black', bgop: 80, shape: 'block', edge: 0, shadow: 'none', radius: 8, pad: 15 })],
    ['minimal', 'Minimal', 'Smaller cream text, no outline', ext(DEFAULT, { size: 85, color: 'creme', opacity: 90, weight: '400', edge: 0, shadow: 'soft' })],
    ['bar', 'TV news bar', 'White on a full-width bar', ext(DEFAULT, { bg: 'navy', bgop: 80, shape: 'bar', edge: 0, shadow: 'none', lift: 4, radius: 0 })],
    ['beeldbuis', 'Beeldbuis', 'Cream on navy strips with round corners', ext(DEFAULT, { color: 'creme', bg: 'navy', bgop: 85, shape: 'line', radius: 12, edge: 0, shadow: 'none', weight: '800' })]
  ];

  function spec(k) {
    for (var g = 0; g < GROUPS.length; g++) for (var i = 0; i < GROUPS[g][2].length; i++) if (GROUPS[g][2][i][0] === k) return GROUPS[g][2][i];
    return null;
  }
  // older stored styles (update 32 test builds: size s/m/l/xl, edge outline/box/...) are mapped onto the new keys
  function migrate(st) {
    if (!st || typeof st !== 'object') return {};
    var o = ext({}, st);
    if (typeof o.size === 'string') o.size = { s: 80, m: 100, l: 125, xl: 155 }[o.size] || 100;
    if (o.font === 'bold') { o.font = 'sans'; o.weight = '800'; }
    if (o.pos) { o.lift = { bottom: 8, raised: 22, top: 84 }[o.pos] || 8; delete o.pos; }
    if (o.edge === 'outline') o.edge = 2; else if (o.edge === 'shadow') { o.edge = 0; o.shadow = 'hard'; }
    else if (o.edge === 'box' || o.edge === 'soft') { o.bg = 'black'; o.bgop = o.edge === 'box' ? 85 : 50; o.edge = 0; o.shadow = 'none'; }
    else if (o.edge === 'none') o.edge = 0;
    return o;
  }
  function normalize(st) {
    st = migrate(st);
    var out = {};
    Object.keys(DEFAULT).forEach(function (k) {
      var s = spec(k), v = st[k];
      if (s && s[2] === 'range') {
        v = Number(v);
        out[k] = isFinite(v) ? Math.max(s[3][0], Math.min(s[3][1], Math.round(v / s[3][2]) * s[3][2])) : DEFAULT[k];
      } else out[k] = s && s[3].some(function (c) { return c[0] === String(v); }) ? String(v) : DEFAULT[k];
    });
    return out;
  }
  // one step left (-1) or right (+1) for a setting; ranges stop at their ends, choices go round
  function step(st, k, dir) {
    var s = spec(k), o = ext(st, {});
    if (!s) return o;
    if (s[2] === 'range') o[k] = Math.max(s[3][0], Math.min(s[3][1], Number(st[k]) + dir * s[3][2]));
    else { var c = s[3], i = 0; for (var j = 0; j < c.length; j++) if (c[j][0] === String(st[k])) i = j; o[k] = c[(i + dir + c.length) % c.length][0]; }
    return o;
  }
  function label(st, k) {
    var s = spec(k); if (!s) return '';
    if (s[2] === 'range') return (k === 'spacing' ? (st[k] ? '+' + st[k] : 'Normal') : st[k] + s[3][3]);
    for (var i = 0; i < s[3].length; i++) if (s[3][i][0] === String(st[k])) return s[3][i][1];
    return '';
  }
  function rgba(name, a) { var c = COLORS[name] || COLORS.white; return 'rgba(' + c[1] + ',' + (a == null ? 1 : a) + ')'; }

  // frame: {h: picture height in px, osd: true when the player bar is on screen, osdPx: its height}
  function css(st, frame) {
    st = normalize(st);
    var h = (frame && frame.h) || 1080;
    var fs = Math.round(h * 0.042 * st.size / 100);
    var lift = st.lift + (frame && frame.osd && st.avoid === 'on' && st.lift < 30 ? (frame.osdPct || 16) : 0);
    var side = (100 - st.width) / 2;
    var box = 'position:absolute;bottom:' + lift + '%;text-align:' + st.align + ';pointer-events:none;';
    if (st.align === 'center') box += 'left:' + side + '%;right:' + side + '%;';
    else if (st.align === 'left') box += 'left:' + side + '%;right:auto;max-width:' + st.width + '%;';
    else box += 'right:' + side + '%;left:auto;max-width:' + st.width + '%;';
    var bgc = st.bg === 'none' ? 'transparent' : rgba(st.bg, st.bgop / 100);
    var pad = Math.round(st.pad * h / 1080), padV = Math.round(pad * 0.35);
    var w = Math.round(st.edge * h / 1080 * 10) / 10, ec = rgba(st.edgecol, 1), sh = [];
    if (w > 0) for (var a = 0; a < 16; a++) {
      var r = a * Math.PI / 8;
      sh.push((Math.cos(r) * w).toFixed(1) + 'px ' + (Math.sin(r) * w).toFixed(1) + 'px 0 ' + ec);
    }
    var u = h / 1080;
    if (st.shadow === 'soft') sh.push((2 * u).toFixed(1) + 'px ' + (2 * u).toFixed(1) + 'px ' + (6 * u).toFixed(1) + 'px rgba(0,0,0,.9)');
    else if (st.shadow === 'hard') sh.push((4 * u).toFixed(1) + 'px ' + (4 * u).toFixed(1) + 'px 0 rgba(0,0,0,.95)');
    else if (st.shadow === 'glow') sh.push('0 0 ' + (12 * u).toFixed(1) + 'px ' + ec + ', 0 0 ' + (4 * u).toFixed(1) + 'px ' + ec);
    var text = 'font-size:' + fs + 'px;line-height:' + (st.lineh / 100) + ';font-family:' + FONTS[st.font][1] + ';font-weight:' + st.weight + ';' +
      'color:' + rgba(st.color, st.opacity / 100) + ';letter-spacing:' + (st.spacing * 0.02) + 'em;text-transform:' + (st.caps === 'on' ? 'uppercase' : 'none') + ';' +
      'text-shadow:' + (sh.length ? sh.join(',') : 'none') + ';white-space:pre-line;';
    if (st.shape === 'bar') {
      box = box.replace(/left:[^;]*;|right:[^;]*;|max-width:[^;]*;/g, '') + 'left:0;right:0;padding:' + padV + 'px ' + side + '%;background:' + bgc + ';';
    } else {
      text += 'background:' + bgc + ';border-radius:' + (st.bg === 'none' ? 0 : Math.round(st.radius * u)) + 'px;padding:' + (st.bg === 'none' ? 0 : padV + 'px ' + pad + 'px') + ';';
      if (st.shape === 'block') text += 'display:inline-block;';
      else text += '-webkit-box-decoration-break:clone;box-decoration-break:clone;';
    }
    return { box: box, text: text };
  }
  function summary(st) {
    st = normalize(st);
    for (var i = 0; i < PRESETS.length; i++) {
      var p = normalize(PRESETS[i][3]), same = Object.keys(p).every(function (k) { return String(p[k]) === String(st[k]); });
      if (same) return PRESETS[i][1];
    }
    return 'Custom · ' + label(st, 'size') + ' · ' + label(st, 'color') + (st.bg !== 'none' ? ' on ' + label(st, 'bg').toLowerCase() : '');
  }

  root.BBSubStyle = { GROUPS: GROUPS, PRESETS: PRESETS, DEFAULT: DEFAULT, normalize: normalize, step: step, label: label, css: css, summary: summary, spec: spec };
})(this);
