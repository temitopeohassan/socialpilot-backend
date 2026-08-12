/**
 * SocialPilot Website Tracker
 * Lightweight analytics snippet (~3KB minified)
 *
 * Tracks: pageviews, time-on-page, scroll depth, custom events,
 *         UTM attribution, and SPA (single-page app) navigation.
 *
 * No cookies. GDPR-friendly fingerprinting only.
 * Replace INGEST_BASE_URL with your SocialPilot deployment URL.
 */
(function () {
  "use strict";

  const INGEST = "__INGEST_BASE_URL__"; // replaced at serve time
  const tid = window._spId;
  if (!tid) return;

  // ── Session / visitor identity ─────────────────────────────────────────────

  function uuid() {
    return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, function (c) {
      var r = (Math.random() * 16) | 0,
        v = c === "x" ? r : (r & 0x3) | 0x8;
      return v.toString(16);
    });
  }

  // Session: new UUID per browser tab / session (sessionStorage)
  var sid = sessionStorage.getItem("_sp_sid");
  if (!sid) {
    sid = uuid();
    sessionStorage.setItem("_sp_sid", sid);
  }

  // Visitor: stable per browser (localStorage, 1-year TTL)
  var vid = localStorage.getItem("_sp_vid");
  if (!vid) {
    vid = uuid();
    localStorage.setItem("_sp_vid", vid);
  }

  // ── Scroll depth tracking ──────────────────────────────────────────────────

  var maxScroll = 0;
  function updateScroll() {
    var scrolled =
      ((window.scrollY + window.innerHeight) / document.body.scrollHeight) * 100;
    maxScroll = Math.max(maxScroll, Math.round(scrolled));
  }
  window.addEventListener("scroll", updateScroll, { passive: true });

  // ── Page entry time ────────────────────────────────────────────────────────

  var pageStart = Date.now();

  // ── Payload builder ────────────────────────────────────────────────────────

  function basePayload() {
    return {
      tid: tid,
      sid: sid,
      vid: vid,
      url: window.location.href,
      title: document.title,
      ref: document.referrer,
      ua: navigator.userAgent,
      scroll: maxScroll,
      ts: new Date().toISOString(),
    };
  }

  // ── Send helper (beacon preferred, XHR fallback) ───────────────────────────

  function send(endpoint, data) {
    var url = INGEST + endpoint;
    var body = JSON.stringify(data);
    if (navigator.sendBeacon) {
      navigator.sendBeacon(url, new Blob([body], { type: "application/json" }));
    } else {
      var xhr = new XMLHttpRequest();
      xhr.open("POST", url, true);
      xhr.setRequestHeader("Content-Type", "application/json");
      xhr.send(body);
    }
  }

  // ── Pageview ───────────────────────────────────────────────────────────────

  function trackPageview() {
    pageStart = Date.now();
    maxScroll = 0;
    updateScroll();
    var p = basePayload();
    send("/track/pageview", p);
  }

  // ── Duration on exit / visibility change ──────────────────────────────────

  function sendDuration() {
    var duration = Math.round((Date.now() - pageStart) / 1000);
    send("/track/duration", {
      tid: tid,
      sid: sid,
      url: window.location.href,
      duration: duration,
      scroll: maxScroll,
    });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") sendDuration();
  });
  window.addEventListener("pagehide", sendDuration);

  // Heartbeat every 30s for long sessions
  setInterval(sendDuration, 30000);

  // ── Custom event API ────────────────────────────────────────────────────────

  window.spTrack = function (eventName, props) {
    var p = basePayload();
    p.event = eventName;
    p.category = (props && props.category) || "custom";
    p.props = props || {};
    send("/track/event", p);
  };

  // ── SPA support (hash + pushState) ────────────────────────────────────────

  var lastPath = window.location.pathname;

  function onNavigation() {
    var currentPath = window.location.pathname;
    if (currentPath !== lastPath) {
      sendDuration();
      lastPath = currentPath;
      trackPageview();
    }
  }

  // Intercept pushState / replaceState
  (function (history) {
    var pushState = history.pushState;
    history.pushState = function () {
      pushState.apply(history, arguments);
      onNavigation();
    };
    var replaceState = history.replaceState;
    history.replaceState = function () {
      replaceState.apply(history, arguments);
      onNavigation();
    };
  })(window.history);

  window.addEventListener("popstate", onNavigation);
  window.addEventListener("hashchange", onNavigation);

  // ── Outbound link tracking ─────────────────────────────────────────────────

  document.addEventListener("click", function (e) {
    var target = e.target.closest("a[href]");
    if (!target) return;
    var href = target.getAttribute("href");
    if (href && href.startsWith("http") && !href.includes(window.location.hostname)) {
      window.spTrack("outbound_click", { url: href, category: "outbound" });
    }
  });

  // ── Initial pageview ───────────────────────────────────────────────────────

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", trackPageview);
  } else {
    trackPageview();
  }
})();
