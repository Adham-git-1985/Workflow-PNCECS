(function () {
  "use strict";

  var script = document.currentScript;
  if (!script) return;

  var endpoint = script.dataset.presenceUrl || "";
  var userId = script.dataset.userId || "";
  if (!endpoint || !userId) return;

  var intervalMs = 60000;
  var minimumGapMs = 45000;
  var storageKey = "wf-presence-heartbeat-" + userId;

  function recentlySent(now) {
    try {
      var lastSent = Number(window.localStorage.getItem(storageKey) || 0);
      return lastSent > 0 && (now - lastSent) < minimumGapMs;
    } catch (error) {
      return false;
    }
  }

  function rememberSent(now) {
    try {
      window.localStorage.setItem(storageKey, String(now));
    } catch (error) {
      // Heartbeats still work when storage is unavailable.
    }
  }

  function sendHeartbeat() {
    var now = Date.now();
    if (recentlySent(now)) return;
    rememberSent(now);

    window.fetch(endpoint, {
      method: "POST",
      credentials: "same-origin",
      keepalive: true,
      headers: {
        "Accept": "application/json",
        "Content-Type": "application/json"
      },
      body: JSON.stringify({ path: window.location.pathname })
    }).catch(function () {
      // Presence is best-effort and must never interrupt the user's work.
    });
  }

  sendHeartbeat();
  window.setInterval(sendHeartbeat, intervalMs);
  window.addEventListener("focus", sendHeartbeat);
  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "visible") sendHeartbeat();
  });
})();
