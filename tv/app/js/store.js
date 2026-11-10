/* Settings kept on the device (localStorage). Never leaves the device. */
(function (root) {
  'use strict';
  var KEY = 'beeldbuis.v1';
  var DEFAULTS = {
    account: null,            // {server, username, password}
    countries: [],            // category prefixes to show, e.g. ["NL","UK"]; empty = ask on first run
    favorites: [],            // items {key, type: live|movie|series, name, icon, ...}; key "live:NL:NPO1", "movie:123"
    recent: [],               // last watched items, newest first
    positions: {},            // "movie:123" -> {t, d, at}
    lastChannel: null,
    englishSubs: true,
    showAdult: false,
    quality: 'fhd',           // 'fhd' = Full HD first, 'best' = 4K first
    profile: 'p1',            // who is watching; favorites/recent/positions above belong to this profile
    omdbKey: '',              // optional free OMDb key for IMDb/RT ratings, typed in on the device
    omdbBase: '',             // tests only: a fake OMDb
    startScreen: 'calm',      // 'calm' = clock, consoles and apps first; 'tonight' = straight to Tonight
    saverMin: 5,              // screensaver after this many quiet minutes; 0 = off
    mac: null,                // optional link with Beeldbuis on a Mac: {base, key, name}
    profiles: null            // {p1: {name, favorites, recent, positions}, ...}; names are set on the device
  };
  var state = null;

  function load() {
    var s = {};
    try { s = JSON.parse(root.localStorage.getItem(KEY) || '{}') || {}; } catch (e) { s = {}; }
    state = {};
    Object.keys(DEFAULTS).forEach(function (k) { state[k] = (k in s) ? s[k] : JSON.parse(JSON.stringify(DEFAULTS[k])); });
    return state;
  }
  var PROFILE_IDS = ['p1', 'p2', 'both'];
  var PROFILE_DEFAULT = { p1: 'Profile 1', p2: 'Profile 2', both: 'Together' };
  var PERSONAL = ['favorites', 'recent', 'positions'];
  function profiles() {
    var s = get();
    if (!s.profiles) {          // first run of a version with profiles: what was there belongs to profile 1
      s.profiles = {};
      PROFILE_IDS.forEach(function (id) { s.profiles[id] = { name: PROFILE_DEFAULT[id], favorites: [], recent: [], positions: {} }; });
    }
    return s.profiles;
  }
  function profileName(id) { var p = profiles()[id || get().profile]; return (p && p.name) || PROFILE_DEFAULT[id || get().profile]; }
  function setProfileName(id, name) { profiles()[id].name = String(name || '').trim().slice(0, 20) || PROFILE_DEFAULT[id]; save(); }
  function switchProfile(id) {
    var s = get(), ps = profiles();
    if (!ps[id] || id === s.profile) return;
    PERSONAL.forEach(function (k) { ps[s.profile][k] = s[k]; s[k] = ps[id][k] || (k === 'positions' ? {} : []); ps[id][k] = null; });
    s.profile = id;
    save();
  }
  function save() { try { root.localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) {} }
  function get() { return state || load(); }

  function isFav(key) { return get().favorites.some(function (f) { return f.key === key; }); }
  function toggleFav(item) {
    var s = get(), was = isFav(item.key);
    s.favorites = s.favorites.filter(function (f) { return f.key !== item.key; });
    if (!was) s.favorites.unshift(item);
    save();
    return !was;
  }
  function addRecent(item) {
    var s = get();
    s.recent = [item].concat(s.recent.filter(function (r) { return r.key !== item.key; })).slice(0, 20);
    save();
  }
  function setPosition(item, t, d) {
    var s = get(), key = item.key;
    if (d && t > d * 0.95) delete s.positions[key];        // finished: forget the position
    else if (t > 30) s.positions[key] = { item: item, t: Math.floor(t), d: Math.floor(d || 0), at: Date.now() };
    save();
  }

  // cache for big provider lists, with a time-to-live
  function cacheGet(name, ttlMs) {
    try {
      var raw = root.localStorage.getItem('beeldbuis.cache.' + name);
      if (!raw) return null;
      var c = JSON.parse(raw);
      return (Date.now() - c.t < ttlMs) ? c.v : null;
    } catch (e) { return null; }
  }
  function cacheSet(name, v) {
    try { root.localStorage.setItem('beeldbuis.cache.' + name, JSON.stringify({ t: Date.now(), v: v })); }
    catch (e) { /* storage full: just don't cache */ }
  }
  function cacheClear() {
    if (root.BBIdb) root.BBIdb.clear();
    for (var i = root.localStorage.length - 1; i >= 0; i--) {
      var k = root.localStorage.key(i);
      if (k && k.indexOf('beeldbuis.cache.') === 0) root.localStorage.removeItem(k);
    }
  }

  root.BBStore = { get: get, save: save, load: load, toggleFav: toggleFav, isFav: isFav, addRecent: addRecent, setPosition: setPosition,
    cacheGet: cacheGet, cacheSet: cacheSet, cacheClear: cacheClear,
    PROFILE_IDS: PROFILE_IDS, profiles: profiles, profileName: profileName, setProfileName: setProfileName, switchProfile: switchProfile };
})(this);
