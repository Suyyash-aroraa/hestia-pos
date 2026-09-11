/**
 * shared-sse.js — One SSE connection per URL, shared across ALL same-origin browser tabs.
 *
 * Problem solved: browsers allow max 6 HTTP/1.1 connections per origin (shared across tabs).
 * SSE holds connections permanently, so 5-6 open staff tabs exhaust the pool and any new
 * page navigation queues — causing the "stuck" freeze on navigation.
 *
 * Solution: BroadcastChannel + leader election.
 *   - One tab becomes the "leader" and opens the actual EventSource.
 *   - All other tabs subscribe and receive events via BroadcastChannel.
 *   - When the leader tab closes, another tab automatically takes over within ~3s.
 *   - N tabs = 1 SSE connection (not N).
 *
 * Usage:
 *   SharedSSE.on('/api/events/pos', function(data) { ... });
 *   // returns an unsubscribe function
 */
(function (global) {
  'use strict';

  var CHANNEL = 'hestia_sse_v1';
  var bc = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel(CHANNEL) : null;

  var _streams = {};   // url → { isLeader, es, heartbeatTimer }
  var _handlers = {};  // url → [fn, ...]

  function _lsKey(url) {
    return 'sseLdr__' + url.replace(/[^a-z0-9]/gi, '_');
  }

  function _dispatch(url, data) {
    var hs = _handlers[url] || [];
    for (var i = 0; i < hs.length; i++) {
      try { hs[i](data); } catch (e) {}
    }
  }

  function _startLeading(url) {
    var s = _streams[url] || (_streams[url] = {});
    s.isLeader = true;
    if (s.es) { try { s.es.close(); } catch (e) {} }
    s.es = new EventSource(url);

    s.es.onmessage = function (ev) {
      if (!ev.data || ev.data.charAt(0) === ':') return;
      try {
        var d = JSON.parse(ev.data);
        _dispatch(url, d);
        if (bc) bc.postMessage({ u: url, d: d });
      } catch (e) {}
    };

    s.es.onerror = function () {
      if (!s.isLeader) return;
      setTimeout(function () { if (s.isLeader) _startLeading(url); }, 3000);
    };

    clearInterval(s.heartbeatTimer);
    localStorage.setItem(_lsKey(url), Date.now().toString());
    s.heartbeatTimer = setInterval(function () {
      localStorage.setItem(_lsKey(url), Date.now().toString());
    }, 2000);
  }

  function _releaseLeadership(url) {
    var s = _streams[url];
    if (!s || !s.isLeader) return;
    s.isLeader = false;
    clearInterval(s.heartbeatTimer);
    if (s.es) { try { s.es.close(); } catch (e) {} s.es = null; }
    try { localStorage.removeItem(_lsKey(url)); } catch (e) {}
  }

  function _tryBecomeLeader(url) {
    var s = _streams[url] || (_streams[url] = {});
    if (s.isLeader) return;
    var last = 0;
    try { last = parseInt(localStorage.getItem(_lsKey(url)) || '0', 10); } catch (e) {}
    if (Date.now() - last > 5000) {
      _startLeading(url);
    }
  }

  // Receive relayed events from the leader tab
  if (bc) {
    bc.onmessage = function (ev) {
      if (ev.data && ev.data.u && ev.data.d !== undefined) {
        _dispatch(ev.data.u, ev.data.d);
      }
    };
  }

  // When localStorage leader key disappears, a new tab should take over
  window.addEventListener('storage', function (e) {
    if (!e.key || e.key.indexOf('sseLdr__') !== 0 || e.newValue) return;
    Object.keys(_handlers).forEach(function (url) {
      if (_lsKey(url) === e.key && (_handlers[url] || []).length > 0) {
        setTimeout(function () { _tryBecomeLeader(url); }, Math.floor(Math.random() * 400) + 50);
      }
    });
  });

  // Failover poll: check every 3s in case storage event wasn't received
  setInterval(function () {
    Object.keys(_handlers).forEach(function (url) {
      if ((_handlers[url] || []).length > 0) _tryBecomeLeader(url);
    });
  }, 3000);

  // Release leadership cleanly on page close
  function _releaseAll() {
    Object.keys(_streams).forEach(_releaseLeadership);
  }
  window.addEventListener('pagehide', _releaseAll);
  window.addEventListener('beforeunload', _releaseAll);

  global.SharedSSE = {
    on: function (url, handler) {
      if (!_handlers[url]) _handlers[url] = [];
      _handlers[url].push(handler);
      _tryBecomeLeader(url);
      return function () {
        _handlers[url] = (_handlers[url] || []).filter(function (h) { return h !== handler; });
      };
    }
  };

})(window);
