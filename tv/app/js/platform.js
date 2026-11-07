/* Which device are we on, and what do its remote keys send? */
(function (root) {
  'use strict';
  var isTizen = typeof root.tizen !== 'undefined' && typeof root.webapis !== 'undefined';
  var isAndroid = typeof root.BeeldbuisAndroid !== 'undefined';
  var name = isTizen ? 'tizen' : isAndroid ? 'android' : 'browser';

  // Tizen key codes; the browser preview maps keyboard keys onto the same actions
  var KEYS = {
    37: 'left', 38: 'up', 39: 'right', 40: 'down', 13: 'ok',
    10009: 'back', 8: 'back', 27: 'back',
    427: 'chup', 428: 'chdown', 33: 'chup', 34: 'chdown',
    415: 'play', 19: 'pause', 10252: 'playpause', 413: 'stop', 417: 'ff', 412: 'rw', 32: 'playpause',
    403: 'red', 404: 'green', 405: 'yellow', 406: 'blue',
    457: 'info', 73: 'info', 10073: 'guide',
    48: 0, 49: 1, 50: 2, 51: 3, 52: 4, 53: 5, 54: 6, 55: 7, 56: 8, 57: 9
  };

  function registerKeys() {
    if (!isTizen) return;
    var want = ['MediaPlayPause', 'MediaPlay', 'MediaPause', 'MediaStop', 'MediaFastForward', 'MediaRewind',
      'ChannelUp', 'ChannelDown', 'ColorF0Red', 'ColorF1Green', 'ColorF2Yellow', 'ColorF3Blue', 'Info',
      '0', '1', '2', '3', '4', '5', '6', '7', '8', '9'];
    want.forEach(function (k) { try { root.tizen.tvinputdevice.registerKey(k); } catch (e) { /* key not on this model */ } });
  }

  function exit() {
    if (isTizen) { try { root.tizen.application.getCurrentApplication().exit(); } catch (e) {} }
    else if (isAndroid) { try { root.BeeldbuisAndroid.exit(); } catch (e) {} }
  }

  // Xtream servers rarely send CORS headers. The TV and Android apps are allowed to call any origin;
  // the browser preview goes through the preview server's /proxy.
  function xurl(url) {
    if (name !== 'browser' || url.indexOf(location.origin) === 0) return url;
    return location.origin + '/proxy?u=' + encodeURIComponent(url);
  }

  root.BBPlatform = { name: name, isTizen: isTizen, isAndroid: isAndroid, KEYS: KEYS, registerKeys: registerKeys, exit: exit, xurl: xurl };
})(this);
