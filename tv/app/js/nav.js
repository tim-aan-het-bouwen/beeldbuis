/* Remote-control navigation: arrows move focus to the nearest focusable element (.f) in that direction. */
(function (root) {
  'use strict';
  var current = null;

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

  function move(dir) {
    if (!current || !visible(current)) { first(); return true; }
    // an element can name its neighbour explicitly: data-left="#id" etc.
    var forced = current.getAttribute('data-' + dir);
    if (forced) { var t = document.querySelector(forced); if (t && visible(t)) { focus(t); return true; } }
    var a = current.getBoundingClientRect(), ax = a.left + a.width / 2, ay = a.top + a.height / 2;
    var best = null, bestScore = Infinity;
    candidates().forEach(function (el) {
      if (el === current) return;
      var b = el.getBoundingClientRect(), bx = b.left + b.width / 2, by = b.top + b.height / 2;
      // main: gap between the facing edges; cross: how far apart the two are sideways (0 when they overlap)
      var main, cross;
      if (dir === 'left') { if (b.right > a.left + 1) return; main = a.left - b.right; cross = gapY(a, b); }
      else if (dir === 'right') { if (b.left < a.right - 1) return; main = b.left - a.right; cross = gapY(a, b); }
      else if (dir === 'up') { if (b.bottom > a.top + 1) return; main = a.top - b.bottom; cross = gapX(a, b); }
      else { if (b.top < a.bottom - 1) return; main = b.top - a.bottom; cross = gapX(a, b); }
      // left/right stay within the same row: never jump up into the header
      if ((dir === 'left' || dir === 'right') && cross > 0) return;
      var centre = (dir === 'left' || dir === 'right') ? Math.abs(by - ay) : Math.abs(bx - ax);
      var score = Math.max(0, main) + cross * 6 + centre * 0.1;
      if (score < bestScore) { bestScore = score; best = el; }
    });
    if (best) { focus(best); return true; }
    return false;
  }

  function first(scope) {
    var list = scope ? Array.prototype.filter.call(scope.querySelectorAll('.f'), visible) : candidates();
    var pref = list.filter(function (e) { return e.classList.contains('autofocus'); })[0];
    focus(pref || list[0]);
  }

  root.BBNav = { focus: focus, move: move, first: first, current: function () { return current; } };
})(this);
