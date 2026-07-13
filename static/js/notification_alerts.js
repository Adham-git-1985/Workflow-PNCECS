(function () {
  "use strict";

  const STORAGE_KEY = "masar.notification.sound.enabled.v1";
  const ALERT_TITLE = "🔔 تنبيه جديد - مسار";
  const toggleButton = document.getElementById("notification-sound-toggle");

  if (!toggleButton) return;

  const notificationsUrl = toggleButton.dataset.notificationsUrl || "/workflow/notifications";
  const originalTitle = document.title;
  let audioContext = null;
  let flashTimer = null;
  let flashState = false;
  let soundEnabled = readPreference();

  function readPreference() {
    try {
      return window.localStorage.getItem(STORAGE_KEY) === "1";
    } catch (_) {
      return false;
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

  function addTone(context, frequency, startsAfter, duration) {
    const oscillator = context.createOscillator();
    const gain = context.createGain();
    const start = context.currentTime + startsAfter;
    const end = start + duration;

    oscillator.type = "sine";
    oscillator.frequency.setValueAtTime(frequency, start);

    gain.gain.setValueAtTime(0.0001, start);
    gain.gain.exponentialRampToValueAtTime(0.16, start + 0.025);
    gain.gain.exponentialRampToValueAtTime(0.0001, end);

    oscillator.connect(gain);
    gain.connect(context.destination);
    oscillator.start(start);
    oscillator.stop(end + 0.02);
  }

  async function playAlertSound() {
    if (!soundEnabled || !(await prepareAudio())) return;

    const context = getAudioContext();
    addTone(context, 880, 0, 0.16);
    addTone(context, 1175, 0.20, 0.22);
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

  function showToast(message, isConfirmation) {
    if (!window.bootstrap || !window.bootstrap.Toast) return;

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
    title.textContent = "مسار";

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
    messageElement.textContent = message || "وصل تنبيه جديد إلى نظام مسار";
    body.appendChild(messageElement);

    if (!isConfirmation) {
      const link = document.createElement("a");
      link.href = notificationsUrl;
      link.className = "btn btn-sm btn-primary mt-2";
      link.textContent = "عرض التنبيهات";
      body.appendChild(link);
    }

    toastElement.appendChild(header);
    toastElement.appendChild(body);
    ensureToastContainer().appendChild(toastElement);

    toastElement.addEventListener("hidden.bs.toast", function () {
      toastElement.remove();
    });

    const toast = new window.bootstrap.Toast(toastElement, {
      autohide: isConfirmation || !document.hidden,
      delay: isConfirmation ? 3000 : 8000,
    });
    toast.show();
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

  toggleButton.addEventListener("click", async function () {
    soundEnabled = !soundEnabled;
    savePreference(soundEnabled);
    updateToggleButton();

    if (soundEnabled) {
      await playAlertSound();
      showToast("تم تفعيل صوت التنبيهات", true);
    } else {
      showToast("تم إيقاف صوت التنبيهات", true);
    }
  });

  window.addEventListener("pointerdown", function () {
    if (soundEnabled) prepareAudio();
  }, { passive: true });

  window.addEventListener("keydown", function () {
    if (soundEnabled) prepareAudio();
  });

  window.addEventListener("masar:notification", function (event) {
    const detail = event.detail || {};
    playAlertSound();
    startTitleFlash();
    showToast(detail.message, false);
  });

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) stopTitleFlash();
  });

  window.addEventListener("focus", stopTitleFlash);
  updateToggleButton();
})();
