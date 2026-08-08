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
  const maxMessageChars = Number.parseInt(root.dataset.maxMessageChars || "2000", 10) || 2000;
  const internalKnowledgeEnabled = root.dataset.internalKnowledge === "1";
  const storageKey = `aref-assistant:v3:${root.dataset.userId || "user"}`;
  const defaultSuggestions = [
    "ما هي صلاحياتي؟",
    "ما الطلبات التي تخصني؟",
    "ما الإشعارات غير المقروءة؟",
    "ابحث عن وارد أو صادر",
  ];
  if (internalKnowledgeEnabled) {
    defaultSuggestions.push("اشرح هيكلية المشروع", "ما جداول قاعدة البيانات؟");
  }
  let history = loadHistory();
  let busy = false;

  function loadHistory() {
    try {
      const parsed = JSON.parse(sessionStorage.getItem(storageKey) || "[]");
      if (!Array.isArray(parsed)) return [];
      return parsed
        .filter((item) => item && ["user", "assistant"].includes(item.role) && typeof item.content === "string")
        .map((item) => ({
          role: item.role,
          content: item.content,
          links: Array.isArray(item.links) ? item.links.slice(0, 5) : [],
          sources: Array.isArray(item.sources) ? item.sources.slice(0, 10) : [],
        }))
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

  function appendMessage(role, content, links, extraClass, sources) {
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

    if (role === "assistant" && Array.isArray(sources) && sources.length) {
      const details = document.createElement("details");
      details.className = "masar-assistant__sources";
      const summary = document.createElement("summary");
      summary.textContent = `المصادر (${Math.min(sources.length, 10)})`;
      details.appendChild(summary);

      const sourceList = document.createElement("div");
      sourceList.className = "masar-assistant__source-list";
      sources.slice(0, 10).forEach((item) => {
        if (!item || typeof item.label !== "string") return;
        const source = document.createElement("code");
        source.className = "masar-assistant__source";
        source.textContent = item.label;
        sourceList.appendChild(source);
      });
      if (sourceList.childElementCount) {
        details.appendChild(sourceList);
        bubble.appendChild(details);
      }
    }

    messages.appendChild(bubble);
    scrollToBottom();
    return bubble;
  }

  function renderSuggestions(items) {
    suggestions.replaceChildren();
    (Array.isArray(items) && items.length ? items : defaultSuggestions).slice(0, 7).forEach((label) => {
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
        internalKnowledgeEnabled
          ? "أهلًا بك! أنا عارف. أعرف بيانات النظام وبنية المشروع والكود والملفات ومخطط قاعدة البيانات، وأجيبك مع المصادر ضمن نطاق حسابك."
          : "أهلًا بك! أنا عارف. أسألني عن بياناتك أو معاملاتك أو صلاحياتك، أو أخبرني بما تريد إنجازه وسأرشدك ضمن نطاق حسابك."
      );
    } else {
      history.forEach((item) => appendMessage(item.role, item.content, item.links, "", item.sources));
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
    if (message.length > maxMessageChars) {
      appendMessage("assistant", `اختصر السؤال إلى ${maxMessageChars} حرف أو أقل.`, [], "masar-assistant__message--error");
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
      appendMessage("assistant", reply, data.links || [], "", data.sources || []);
      history.push({
        role: "assistant",
        content: reply,
        links: data.links || [],
        sources: data.sources || [],
      });
      saveHistory();
      renderSuggestions(data.suggestions || defaultSuggestions);
      const accessLabel = String(data.access_label || "نطاق المستخدم وصلاحياته");
      const knowledgeLabel = data.index_stats ? " — معرفة المشروع" : "";
      modeLabel.textContent = data.mode === "ai"
        ? `عارف الذكي — ${accessLabel}${knowledgeLabel}`
        : `عارف — ${accessLabel}${knowledgeLabel}`;
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
    modeLabel.textContent = internalKnowledgeEnabled
      ? "عارف — بيانات ومعرفة المشروع"
      : "عارف — معلومات حسب صلاحياتك";
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
