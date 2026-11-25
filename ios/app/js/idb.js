/* Tiny key-value store on IndexedDB for big provider lists (localStorage holds only ~5 MB).
   Falls back to an in-memory map when IndexedDB is unavailable. */
(function (root) {
  'use strict';
  var DB = 'beeldbuis', STORE = 'kv', dbp = null, mem = {};

  function open() {
    if (dbp) return dbp;
    dbp = new Promise(function (resolve) {
      try {
        var req = root.indexedDB.open(DB, 1);
        req.onupgradeneeded = function () { req.result.createObjectStore(STORE); };
        req.onsuccess = function () { resolve(req.result); };
        req.onerror = function () { resolve(null); };
      } catch (e) { resolve(null); }
    });
    return dbp;
  }
  function tx(mode, fn) {
    return open().then(function (db) {
      if (!db) return fn(null);
      return new Promise(function (resolve) {
        var t, r;
        try { t = db.transaction(STORE, mode); r = fn(t.objectStore(STORE)); }
        catch (e) { resolve(undefined); return; }
        t.oncomplete = function () { resolve(r && 'result' in r ? r.result : undefined); };
        t.onerror = t.onabort = function () { resolve(undefined); };
      });
    });
  }
  function get(key) {
    return tx('readonly', function (s) { return s ? s.get(key) : null; }).then(function (v) { return v === undefined ? mem[key] : v; });
  }
  function set(key, value) {
    mem[key] = value;
    return tx('readwrite', function (s) { return s ? s.put(value, key) : null; });
  }
  function clear() {
    mem = {};
    return tx('readwrite', function (s) { return s ? s.clear() : null; });
  }
  root.BBIdb = { get: get, set: set, clear: clear };
})(this);
