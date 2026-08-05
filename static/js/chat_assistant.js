(function () {
  "use strict";

  const root = document.getElementById("masarAssistant");
  if (!root) return;

  const panel = root.querySelector("#masarAssistantPanel");
  const toggle = root.querySelector("[data-assistant-toggle]");
  const closeButton = root.querySelector("[data-assistant-close]");
  const clearButton = root.querySelector("[data-assistant-clear]");
  const form = root.querySelector("[data-assistant-form]");
  const input = root.querySelector("[data-assistant-input]");
  const sendButton = root.querySelector("[data-assistant-send]");
  const messages = root.querySelector("[data-assistant-messages]");
  const suggestions = root.querySelector("[data-assistant-suggestions]");
  const modeLabel = root.querySelector("[data-assistant-mode]");
  const chatUrl = root.dataset.chatUrl;
  const csrfToken = root.dataset.csrfToken;
  const storageKey = `aref-assistant:v2:${root.dataset.userId || "user"}`;
  const defaultSuggestions = [
    "ما هي صلاحياتي؟",
    "ما الطلبات التي تخصني؟",
    "ما الإشعارات غير المقروءة؟",
    "ابحث عن وارد أو صادر",
  ];
  let history = loadHistory();
  let busy = false;

  function loadHistory() {
    try {
      const parsed = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter((item) => item && ["user", "assistant"].includes(item.role) && typeof item.content === "string")
        .slice(-12);
    } catch (_error) {
      return [];
    }
  }

  function saveHistory() {
    try {
      sessionStorage.setItem(storageKey, JSON.stringify(history.slice(-12)));
    } catch (_error) {}
  }

  function scrollToBottom() {
    messages.scrollTop = messages.scrollHeight;
  }

  function safeHref(value) {
    return typeof value === "string" && value.startsWith("/") && !value.startsWith("//") ? value : null;
  }

  function appendMessage(role, content, links, extraClass) {
    const bubble = document.createElement("div");
    bubble.className = `masar-assistant__message masar-assistant__message--${role}`;
    if (extraClass) bubble.classList.add(extraClass);

    const text = document.createElement("div");
    text.textContent = content;
    bubble.appendChild(text);

    if (role === "assistant" && Array.isArray(links) && links.length) {
      const list = document.createElement("div");
      list.className = "masar-assistant__links";
      links.slice(0, 5).forEach((item) => {
        const href = safeHref(item && item.href);
        if (!href) return;
        const link = document.createElement("a");
        link.className = "masar-assistant__link";
        link.href = href;

        const title = document.createElement("strong");
        title.textContent = item.title || "فتح الشاشة";
        link.appendChild(title);

        if (item.desc) {
          const desc = document.createElement("small");
          desc.textContent = item.desc;
          link.appendChild(desc);
        }
        list.appendChild(link);
      });
      if (list.childElementCount) bubble.appendChild(list);
    }

    messages.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function renderSuggestions(items) {
    suggestions.replaceChildren();
    (Array.isArray(items) && items.length ? items : defaultSuggestions).slice(0, 5).forEach((label) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "masar-assistant__suggestion";
      button.textContent = label;
      button.addEventListener("click", () => sendMessage(label));
      suggestions.appendChild(button);
    });
  }

  function renderConversation() {
    messages.replaceChildren();
    if (!history.length) {
      appendMessage(
        "assistant",
        "أهلًا بك! أنا عارف. أسألني عن بياناتك أو معاملاتك أو صلاحياتك، أو أخبرني بما تريد إنجازه وسأرشدك ضمن نطاق حسابك."
      );
    } else {
      history.forEach((item) => appendMessage(item.role, item.content));
    }
    renderSuggestions(defaultSuggestions);
  }

  function setOpen(open) {
    panel.hidden = !open;
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      window.setTimeout(() => input.focus(), 60);
      scrollToBottom();
    }
  }

  function setBusy(value) {
    busy = value;
    input.disabled = value;
    sendButton.disabled = value;
  }

  async function sendMessage(rawMessage) {
    const message = String(rawMessage || "").trim();
    if (!message || busy) return;
    if (message.length > 1200) {
      appendMessage("assistant", "اختصر السؤال إلى 1200 حرف أو أقل.", [], "masar-assistant__message--error");
      return;
    }

    const previousHistory = history.slice(-8);
    appendMessage("user", message);
    history.push({ role: "user", content: message });
    saveHistory();
    input.value = "";
    input.style.height = "auto";
    setBusy(true);
    const typing = appendMessage("assistant", "يبحث عارف في المعلومات المسموح بها…", [], "masar-assistant__message--typing");

    try {
      const response = await fetch(chatUrl, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
          "X-CSRFToken": csrfToken,
        },
        body: JSON.stringify({
          message,
          history: previousHistory,
          context: {
            path: root.dataset.contextPath || window.location.pathname,
            title: document.title || "",
          },
        }),
      });

      const contentType = response.headers.get("content-type") || "";
      if (!contentType.includes("application/json")) {
        throw new Error("انتهت الجلسة أو تعذر قراءة رد الخادم. حدّث الصفحة وحاول مجددًا.");
      }
      const data = await response.json();
      if (!response.ok) throw new Error(data.message || "تعذر تشغيل عارف الآن.");

      typing.remove();
      const reply = String(data.reply || "لم يصل رد من عارف.");
      appendMessage("assistant", reply, data.links || []);
      history.push({ role: "assistant", content: reply });
      saveHistory();
      renderSuggestions(data.suggestions || defaultSuggestions);
      const accessLabel = String(data.access_label || "نطاق المستخدم وصلاحياته");
      modeLabel.textContent = data.mode === "ai" ? `عارف الذكي — ${accessLabel}` : `عارف — ${accessLabel}`;
    } catch (error) {
      typing.remove();
      appendMessage(
        "assistant",
        error && error.message ? error.message : "تعذر الاتصال بعارف. حاول مجددًا.",
        [],
        "masar-assistant__message--error"
      );
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  toggle.addEventListener("click", () => setOpen(panel.hidden));
  closeButton.addEventListener("click", () => setOpen(false));
  clearButton.addEventListener("click", () => {
    history = [];
    saveHistory();
    modeLabel.textContent = "عارف — معلومات حسب صلاحياتك";
    renderConversation();
    input.focus();
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage(input.value);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage(input.value);
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 110)}px`;
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) setOpen(false);
  });

  renderConversation();
})();
