/* Remote-control navigation: arrows move focus to the nearest focusable element (.f) in that direction.
   The screen has zones: the tab bar (#top), a sidebar (.side) and the main area. Arrows move within a zone first;
   only at its edge does focus cross to the next zone, and then it lands where you were last in that zone
   (the tab bar always lands on the active tab). data-left="#sel" etc. names where to go at the edge. */
(function (root) {
  'use strict';
  var current = null;
  var last = {};          // zone -> element focused there most recently

  function zoneOf(el) {
    if (!el || !el.closest) return 'main';
    if (el.closest('.modal')) return 'modal';
    if (el.closest('#top')) return 'top';
    var side = el.closest('.side');
    if (side) return 'side:' + (side.id || '');
    return 'main';
  }

  function visible(el) {
    if (!el || el.disabled) return false;
    var r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    for (var p = el; p && p !== document.body; p = p.parentElement) if (p.classList.contains('hidden')) return false;
    return true;
  }
  function candidates() {
    var scope = document.querySelector('.modal:not(.hidden)') || document.getElementById('stage');
    return Array.prototype.filter.call(scope.querySelectorAll('.f'), visible);
  }

  function focus(el, opts) {
    if (!el) return;
    if (current) current.classList.remove('focus');
    current = el;
    last[zoneOf(el)] = el;
    el.classList.add('focus');
    try { el.focus({ preventScroll: true }); } catch (e) { el.focus(); }
    if (!(opts && opts.noScroll)) scrollIntoView(el);
    if (el.onfocusin2) el.onfocusin2();
  }

  // keep the focused element inside its scrolling row/column
  function scrollIntoView(el) {
    for (var p = el.parentElement; p && p !== document.body; p = p.parentElement) {
      var cs = root.getComputedStyle(p);
      var r = el.getBoundingClientRect(), pr = p.getBoundingClientRect();
      if (/(auto|scroll|hidden)/.test(cs.overflowX) && p.scrollWidth > p.clientWidth) {
        if (r.left < pr.left + 40) p.scrollLeft -= (pr.left + 40 - r.left);
        else if (r.right > pr.right - 40) p.scrollLeft += (r.right - pr.right + 40);
      }
      if (/(auto|scroll|hidden)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight) {
        r = el.getBoundingClientRect();
        if (r.top < pr.top + 40) p.scrollTop -= (pr.top + 40 - r.top);
        else if (r.bottom > pr.bottom - 40) p.scrollTop += (r.bottom - pr.bottom + 40);
      }
    }
  }

  function gapY(a, b) { return Math.max(0, Math.max(a.top, b.top) - Math.min(a.bottom, b.bottom)); }
  function gapX(a, b) { return Math.max(0, Math.max(a.left, b.left) - Math.min(a.right, b.right)); }

  function nearest(dir, list, loose) {
    var a = current.getBoundingClientRect(), ax = a.left + a.width / 2, ay = a.top + a.height / 2;
    var best = null, bestScore = Infinity;
    list.forEach(function (el) {
      if (el === current) return;
      var b = el.getBoundingClientRect(), bx = b.left + b.width / 2, by = b.top + b.height / 2;
      // main: gap between the facing edges; cross: how far apart the two are sideways (0 when they overlap)
      var main, cross;
      if (dir === 'left') { if (b.right > a.left + 1) return; main = a.left - b.right; cross = gapY(a, b); }
      else if (dir === 'right') { if (b.left < a.right - 1) return; main = b.left - a.right; cross = gapY(a, b); }
      else if (dir === 'up') { if (b.bottom > a.top + 1) return; main = a.top - b.bottom; cross = gapX(a, b); }
      else { if (b.top < a.bottom - 1) return; main = b.top - a.bottom; cross = gapX(a, b); }
      // left/right stay within the same row: never jump up into the header
      if ((dir === 'left' || dir === 'right') && cross > 0 && !loose) return;
      var centre = (dir === 'left' || dir === 'right') ? Math.abs(by - ay) : Math.abs(bx - ax);
      var score = Math.max(0, main) + cross * 6 + centre * 0.1;
      if (score < bestScore) { bestScore = score; best = el; }
    });
    return best;
  }

  // where focus lands when it enters another zone
  function entry(zone, fallback) {
    if (zone === 'top') { var t = document.querySelector('#tabs .tab.active'); if (t && visible(t)) return t; }
    var el = last[zone];
    if (el && el !== current && document.body.contains(el) && visible(el) && zoneOf(el) === zone) return el;
    return fallback;
  }

  function move(dir) {
    if (!current || !visible(current)) { first(); return true; }
    var all = candidates(), zone = zoneOf(current);
    // 1. the nearest element in the same zone (the next tile in this row, the item above in this list)
    var here = all.filter(function (el) { return zoneOf(el) === zone; });
    var best = nearest(dir, here);
    // up/down into a row of choices (days, filters, subtitle options): land on the chosen one
    if (best && (dir === 'up' || dir === 'down') && best.classList.contains('chip') && best.parentElement !== current.parentElement) {
      var on = best.parentElement.querySelector('.f.chip.on');
      if (on && visible(on)) best = on;
    }
    if (best) { focus(best); return true; }
    // 2. at the edge of the zone: an explicit exit (e.g. the active category in the sidebar)
    var forced = current.getAttribute('data-' + dir);
    if (forced) { var t = document.querySelector(forced); if (t && visible(t)) { focus(t); return true; } }
    // 3. the nearest element in another zone, but land where you were last in that zone
    best = nearest(dir, all);
    // sideways into a pane whose items sit higher or lower (sidebar -> shelves): still go, but never into the tab bar
    if (!best && (dir === 'left' || dir === 'right')) best = nearest(dir, all.filter(function (el) { var z = zoneOf(el); return z !== 'top' && z !== zone; }), true);
    if (best) { focus(entry(zoneOf(best), best)); return true; }
    return false;
  }

  // Back: step out of the main area to the sidebar, then to the tab bar; false when already there
  function climb() {
    var zone = zoneOf(current);
    if (zone === 'modal') return false;
    if (zone === 'main') {
      var side = document.querySelector('#stage .side');
      if (side && visible(side)) {
        var el = side.querySelector('.f.active') || entry('side:' + (side.id || ''), null) || side.querySelector('.f');
        if (el && visible(el)) { focus(el); return true; }
      }
    }
    if (zone !== 'top') {
      var tab = entry('top', null);
      if (tab) { focus(tab); return true; }
    }
    return false;
  }

  function first(scope) {
    var list = scope ? Array.prototype.filter.call(scope.querySelectorAll('.f'), visible) : candidates();
    var pref = list.filter(function (e) { return e.classList.contains('autofocus'); })[0];
    focus(pref || list[0]);
  }

  root.BBNav = { focus: focus, move: move, first: first, climb: climb, zone: function () { return zoneOf(current); }, current: function () { return current; } };
})(this);
