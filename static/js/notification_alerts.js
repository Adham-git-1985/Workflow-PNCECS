(function () {
  "use strict";

  const STORAGE_KEY = "masar.notification.sound.enabled.v1";
  const ALERT_TITLE = "🔔 تنبيه جديد - مسار";
  const toggleButton = document.getElementById("notification-sound-toggle");

  if (!toggleButton) return;

  const streamUrl = toggleButton.dataset.streamUrl || "/workflow/notifications/stream";
  const pollUrl = toggleButton.dataset.pollUrl || "/workflow/notifications/poll";
  const workflowNotificationsUrl =
    toggleButton.dataset.workflowNotificationsUrl ||
    toggleButton.dataset.notificationsUrl ||
    "/workflow/notifications";
  const portalNotificationsUrl =
    toggleButton.dataset.portalNotificationsUrl || "/portal/notifications";
  const badgeId = toggleButton.dataset.badgeId || "notif-badge";
  const badgeScope = (toggleButton.dataset.badgeScope || "workflow").toLowerCase();
  const userId = toggleButton.dataset.userId || "anonymous";
  const lastEventStorageKey = `masar.notification.last-event.v1.${userId}`;
  const originalTitle = document.title;

  let audioContext = null;
  let flashTimer = null;
  let flashState = false;
  let pendingTone = null;
  let pollCursor = null;
  let pollingTimer = null;
  let pollingInFlight = false;
  let soundEnabled = readPreference();

  function readPreference() {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      // Sound is on by default. Browsers still require one user gesture before
      // audio can start; the first click/key press unlocks it automatically.
      return stored === null ? true : stored === "1";
    } catch (_) {
      return true;
    }
  }

  function savePreference(value) {
    try {
      window.localStorage.setItem(STORAGE_KEY, value ? "1" : "0");
    } catch (_) {}
  }

  function getAudioContext() {
    if (audioContext) return audioContext;

    const AudioContextClass = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass) return null;

    audioContext = new AudioContextClass();
    return audioContext;
  }

  async function prepareAudio() {
    const context = getAudioContext();
    if (!context) return false;

    if (context.state === "suspended") {
      try {
        await context.resume();
      } catch (_) {
        return false;
      }
    }

    return context.state === "running";
  }

  function addTone(context, frequency, startsAfter, duration, volume) {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const start = context.currentTime + startsAfter;
    const end = start + duration;

    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, start);

    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(volume || 0.16, start + 0.025);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);

    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(start);
    oscillator.stop(end + 0.02);
  }

  async function playAlertSound(queueWhenBlocked) {
    if (!soundEnabled) return false;
    if (!(await prepareAudio())) {
      if (queueWhenBlocked !== false) pendingTone = "alert";
      return false;
    }

    pendingTone = null;
    const context = getAudioContext();
    addTone(context, 880, 0, 0.16, 0.16);
    addTone(context, 1175, 0.20, 0.22, 0.16);
    return true;
  }

  async function playConfirmationSound(queueWhenBlocked) {
    if (!soundEnabled) return false;
    if (!(await prepareAudio())) {
      if (queueWhenBlocked !== false && pendingTone !== "alert") {
        pendingTone = "confirmation";
      }
      return false;
    }

    pendingTone = null;
    const context = getAudioContext();
    addTone(context, 660, 0, 0.12, 0.10);
    addTone(context, 880, 0.13, 0.16, 0.12);
    return true;
  }

  async function unlockAndPlayPendingTone() {
    if (!soundEnabled || !(await prepareAudio())) return;
    const tone = pendingTone;
    pendingTone = null;
    if (tone === "alert") {
      playAlertSound(false);
    } else if (tone === "confirmation") {
      playConfirmationSound(false);
    }
  }

  function updateToggleButton() {
    const icon = toggleButton.querySelector("i");
    const label = soundEnabled ? "إيقاف صوت التنبيهات" : "تفعيل صوت التنبيهات";

    toggleButton.title = label;
    toggleButton.setAttribute("aria-label", label);
    toggleButton.setAttribute("aria-pressed", soundEnabled ? "true" : "false");
    toggleButton.classList.toggle("btn-light", soundEnabled);
    toggleButton.classList.toggle("text-primary", soundEnabled);
    toggleButton.classList.toggle("btn-outline-light", !soundEnabled);

    if (icon) {
      icon.className = soundEnabled ? "bi bi-volume-up-fill" : "bi bi-volume-mute";
    }
  }

  function ensureToastContainer() {
    let container = document.getElementById("masar-notification-toast-container");
    if (container) return container;

    container = document.createElement("div");
    container.id = "masar-notification-toast-container";
    container.className = "toast-container position-fixed top-0 start-0 p-3";
    container.style.marginTop = "72px";
    container.style.zIndex = "1090";
    document.body.appendChild(container);
    return container;
  }

  function notificationUrl(detail) {
    if (detail && detail.link_url) return detail.link_url;
    return (detail && detail.source === "portal")
      ? portalNotificationsUrl
      : workflowNotificationsUrl;
  }

  function showToast(detail, isConfirmation) {
    const data = typeof detail === "string" ? { message: detail } : (detail || {});
    const toastElement = document.createElement("div");
    toastElement.className = "toast border-0 shadow";
    toastElement.setAttribute("role", "alert");
    toastElement.setAttribute("aria-live", "assertive");
    toastElement.setAttribute("aria-atomic", "true");

    const header = document.createElement("div");
    header.className = "toast-header";

    const icon = document.createElement("i");
    icon.className = isConfirmation
      ? "bi bi-volume-up-fill text-success ms-2"
      : "bi bi-bell-fill text-primary ms-2";

    const title = document.createElement("strong");
    title.className = "me-auto";
    title.textContent = data.source === "portal" ? "البوابة الإدارية" : "مسار";

    const closeButton = document.createElement("button");
    closeButton.type = "button";
    closeButton.className = "btn-close";
    closeButton.dataset.bsDismiss = "toast";
    closeButton.setAttribute("aria-label", "إغلاق");

    header.appendChild(icon);
    header.appendChild(title);
    header.appendChild(closeButton);

    const body = document.createElement("div");
    body.className = "toast-body";

    const messageElement = document.createElement("div");
    messageElement.textContent = data.message || "وصل تنبيه جديد إلى نظام مسار";
    body.appendChild(messageElement);

    if (!isConfirmation) {
      const link = document.createElement("a");
      link.href = notificationUrl(data);
      link.className = "btn btn-sm btn-primary mt-2";
      link.textContent = data.link_url ? "فتح الطلب" : "عرض التنبيهات";
      body.appendChild(link);
    }

    toastElement.appendChild(header);
    toastElement.appendChild(body);
    ensureToastContainer().appendChild(toastElement);

    toastElement.addEventListener("hidden.bs.toast", function () {
      toastElement.remove();
    });

    if (window.bootstrap && window.bootstrap.Toast) {
      const toast = new window.bootstrap.Toast(toastElement, {
        autohide: isConfirmation || !document.hidden,
        delay: isConfirmation ? 3000 : 8000,
      });
      toast.show();
      return;
    }

    // The portal shell intentionally ships without Bootstrap's JavaScript.
    // Keep the notification visible there with the same DOM and timeout.
    toastElement.classList.add("show");
    toastElement.style.display = "block";
    toastElement.style.backgroundColor = "var(--bs-body-bg, #fff)";
    window.setTimeout(function () {
      toastElement.remove();
    }, isConfirmation ? 3000 : 8000);
  }

  function startTitleFlash() {
    if (!document.hidden || flashTimer) return;

    flashTimer = window.setInterval(function () {
      flashState = !flashState;
      document.title = flashState ? ALERT_TITLE : originalTitle;
    }, 900);
  }

  function stopTitleFlash() {
    if (flashTimer) {
      window.clearInterval(flashTimer);
      flashTimer = null;
    }
    flashState = false;
    document.title = originalTitle;
  }

  function updateBadge(data) {
    const badge = document.getElementById(badgeId);
    if (!badge) return;

    let unread = Number(data.unread || 0);
    if (badgeScope === "portal") {
      unread = Number(data.portal_unread || 0);
    } else if (badgeScope === "workflow") {
      unread = Number(data.workflow_unread || 0);
    }

    badge.textContent = unread > 0 ? String(unread) : "";
    badge.style.display = unread > 0 ? "inline-block" : "none";

    const portalUnread = Number(data.portal_unread || 0);
    document.querySelectorAll(".portal-unread-badge").forEach(function (portalBadge) {
      portalBadge.textContent = portalUnread > 0 ? String(portalUnread) : "";
      portalBadge.style.display = portalUnread > 0 ? "inline-block" : "none";
    });
  }

  function claimNotification(notificationId) {
    const id = Number(notificationId || 0);
    if (!id) return true;

    try {
      const previous = Number(window.localStorage.getItem(lastEventStorageKey) || 0);
      if (id <= previous) return false;
      window.localStorage.setItem(lastEventStorageKey, String(id));
    } catch (_) {}
    return true;
  }

  function handleNotificationData(data) {
    updateBadge(data);
    if (data.has_new && claimNotification(data.notification_id)) {
      window.dispatchEvent(new CustomEvent("masar:notification", {
        detail: {
          notificationId: data.notification_id,
          unread: Number(data.unread || 0),
          message: data.message || "وصل تنبيه جديد إلى نظام مسار",
          type: data.type || "INFO",
          source: data.source || "workflow",
          link_url: data.link_url || "",
        },
      }));
    }
  }

  async function pollNotifications() {
    if (pollingInFlight || !pollUrl) return;
    pollingInFlight = true;
    try {
      const url = new URL(pollUrl, window.location.origin);
      if (pollCursor !== null) {
        url.searchParams.set("after_id", String(pollCursor));
      }

      const response = await window.fetch(url.toString(), {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;

      const data = await response.json();
      updateBadge(data);

      if (pollCursor === null) {
        pollCursor = Number(data.latest_id || 0);
        return;
      }

      const notifications = Array.isArray(data.notifications) ? data.notifications : [];
      notifications.forEach(handleNotificationData);
      pollCursor = Math.max(pollCursor, Number(data.latest_id || 0));
    } catch (_) {
      // The next interval retries automatically.
    } finally {
      pollingInFlight = false;
    }
  }

  function startPolling() {
    if (pollingTimer || !window.fetch) return;
    pollNotifications();
    pollingTimer = window.setInterval(pollNotifications, 5000);
  }

  function connectEventStream() {
    if (window.__masarNotificationStream || pollingTimer) return;

    if (!window.EventSource || !streamUrl) {
      startPolling();
      return;
    }

    const eventSource = new EventSource(streamUrl);
    let streamHasDelivered = false;
    window.__masarNotificationStream = eventSource;

    eventSource.onmessage = function (event) {
      try {
        streamHasDelivered = true;
        const data = JSON.parse(event.data || "{}");
        handleNotificationData(data);
      } catch (_) {}
    };

    // Some corporate proxies expose EventSource but buffer it indefinitely.
    // Fall back to polling if no baseline message arrives promptly.
    window.setTimeout(function () {
      if (!streamHasDelivered) {
        try { eventSource.close(); } catch (_) {}
        window.__masarNotificationStream = null;
        startPolling();
      }
    }, 12000);

    window.addEventListener("beforeunload", function () {
      try { eventSource.close(); } catch (_) {}
    });
  }

  toggleButton.addEventListener("click", async function () {
    soundEnabled = !soundEnabled;
    savePreference(soundEnabled);
    updateToggleButton();

    if (soundEnabled) {
      await playAlertSound(false);
      showToast("تم تفعيل صوت التنبيهات", true);
    } else {
      pendingTone = null;
      showToast("تم إيقاف صوت التنبيهات", true);
    }
  });

  window.addEventListener("pointerdown", unlockAndPlayPendingTone, { passive: true });
  window.addEventListener("keydown", unlockAndPlayPendingTone);

  window.addEventListener("masar:notification", function (event) {
    const detail = event.detail || {};
    playAlertSound(true);
    startTitleFlash();
    showToast(detail, false);
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) stopTitleFlash();
  });

  window.addEventListener("focus", stopTitleFlash);

  updateToggleButton();
  connectEventStream();

  // A successful server-side action (sending mail, sharing, approving, etc.)
  // receives a short confirmation tone as well. The existing success message
  // remains the visual confirmation.
  if (document.querySelector(".alert.alert-success")) {
    window.setTimeout(function () {
      playConfirmationSound(true);
    }, 80);
  }
})();
