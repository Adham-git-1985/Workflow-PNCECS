(function () {
  "use strict";

  const root = document.getElementById("masarAssistant");
  if (!root) return;

  const panel = root.querySelector("#masarAssistantPanel");
  const toggle = root.querySelector("[data-assistant-toggle]");
  const closeButton = root.querySelector("[data-assistant-close]");
  const homeButton = root.querySelector("[data-assistant-home]");
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
  const aiReady = root.dataset.aiReady === "1";
  const storageKey = `aref-assistant:v4:${root.dataset.userId || "user"}`;
  const defaultInputPlaceholder = "اكتب كما تتكلم، مثل: مرحبًا أو أريد مساعدتك...";
  const defaultSuggestions = [
    "اشرح هذه الصفحة",
    "ما الطلبات التي تخصني؟",
    "ما هي صلاحياتي؟",
    "لا أعرف من أين أبدأ",
  ];
  if (internalKnowledgeEnabled) {
    defaultSuggestions.push("ملخص النظام");
  }

  const helpMenus = {
    home: {
      title: "كيف تريد أن يساعدك عارف؟",
      hint: "اختر النوع الأقرب لما تحتاجه؛ لا يلزم أن تعرف كيف تصوغ السؤال.",
      items: [
        {
          icon: "bi-window",
          title: "اشرح هذه الصفحة",
          desc: "ما فائدتها وماذا أستطيع أن أفعل هنا؟",
          prompt: "اشرح لي هذه الصفحة وما الذي أستطيع فعله فيها خطوة بخطوة.",
        },
        {
          icon: "bi-list-check",
          title: "أنجز مهمة",
          desc: "خطوات جاهزة للطلبات والإجازات وغيرها.",
          menu: "tasks",
        },
        {
          icon: "bi-compass",
          title: "خذني إلى شاشة",
          desc: "الوصول السريع إلى المكان الصحيح.",
          menu: "navigation",
        },
        {
          icon: "bi-search",
          title: "ابحث واعرض بياناتي",
          desc: "طلباتي وإشعاراتي ومراسلاتي وصلاحياتي.",
          menu: "data",
        },
        {
          icon: "bi-tools",
          title: "حل مشكلة",
          desc: "زر مفقود، صفحة لا تفتح، طلب متوقف أو مرفق.",
          menu: "problems",
        },
        {
          icon: "bi-book",
          title: "الأدلة الكاملة",
          desc: "افتح مركز أدلة النظام المتاحة لك.",
          prompt: "أريد فتح مركز الأدلة والدليل الشامل للنظام.",
        },
      ],
    },
    tasks: {
      title: "ما المهمة التي تريد إنجازها؟",
      items: [
        { title: "إنشاء طلب جديد", desc: "من البداية حتى المتابعة.", prompt: "كيف أنشئ طلبًا جديدًا خطوة بخطوة؟" },
        { title: "معالجة مهمة أو اعتماد", desc: "التعامل مع معاملة وصلت إليك.", prompt: "كيف أعالج مهمة وصلتني أو أعتمد طلبًا خطوة بخطوة؟" },
        { title: "تقديم طلب إجازة", desc: "الطلب والمرفقات والمتابعة.", prompt: "كيف أقدم طلب إجازة خطوة بخطوة؟" },
        { title: "تسجيل وارد أو صادر", desc: "إنشاء المراسلة وتشغيل مسارها.", prompt: "كيف أسجل واردًا أو صادرًا وأبدأ مساره؟" },
        { title: "رفع ملف إلى الأرشيف", desc: "الرفع والتصنيف والصلاحيات.", prompt: "كيف أرفع ملفًا إلى الأرشيف خطوة بخطوة؟" },
        { title: "طلب صلاحية جديدة", desc: "التقديم ومتابعة حالة الطلب.", prompt: "كيف أطلب صلاحية جديدة وأتابعها؟" },
        { title: "مهمة أخرى", desc: "صف ما تريد بكلماتك.", compose: "اكتب المهمة التي تريد إنجازها، وسأرتبها لك خطوة بخطوة..." },
      ],
    },
    navigation: {
      title: "إلى أين تريد الذهاب؟",
      items: [
        { title: "مهامي", desc: "المعاملات التي تنتظر إجراءك.", prompt: "خذني إلى مهامي وصندوق الوارد." },
        { title: "طلباتي", desc: "متابعة ما أنشأته من طلبات.", prompt: "خذني إلى طلباتي لمتابعة حالتها." },
        { title: "الإشعارات", desc: "عرض التنبيهات الجديدة.", prompt: "خذني إلى الإشعارات." },
        { title: "المراسلات", desc: "الوارد والصادر والرسائل.", prompt: "خذني إلى المراسلات والوارد والصادر." },
        { title: "البوابة الإدارية", desc: "الموارد البشرية والخدمات الإدارية.", prompt: "افتح لي البوابة الإدارية." },
        { title: "ملفي الشخصي", desc: "بيانات الحساب والصورة.", prompt: "خذني إلى ملفي الشخصي." },
        { title: "شاشة أخرى", desc: "اكتب اسم الشاشة أو الخدمة.", compose: "اكتب اسم الشاشة أو الخدمة التي تريد الوصول إليها..." },
      ],
    },
    data: {
      title: "ما المعلومات التي تريدها؟",
      items: [
        { title: "صلاحياتي وحسابي", desc: "الدور ونطاق الوصول والشاشات المتاحة.", prompt: "ما هي صلاحياتي وماذا أستطيع أن أفعل في النظام؟" },
        { title: "طلباتي ومعاملاتي", desc: "الحالات وآخر المعاملات المتاحة لك.", prompt: "اعرض ملخص الطلبات والمعاملات التي تخصني." },
        { title: "إشعاراتي", desc: "عدد غير المقروء وآخر التنبيهات.", prompt: "ما الإشعارات غير المقروءة لدي؟" },
        { title: "بحث في الوارد والصادر", desc: "ابحث بالرقم أو الموضوع أو الجهة.", compose: "اكتب رقم المراسلة أو موضوعها أو الجهة للبحث في الوارد والصادر..." },
        { title: "دليل الموظفين", desc: "البحث ضمن نطاقك الإداري المسموح.", compose: "اكتب اسم الموظف الذي تبحث عنه..." },
        { title: "ملخص شامل", desc: "حسابك وطلباتك وإشعاراتك في إجابة واحدة.", prompt: "أعطني ملخصًا شاملًا لحسابي وما لدي اليوم." },
      ],
    },
    problems: {
      title: "ما المشكلة التي تواجهها؟",
      items: [
        { title: "صفحة لا تفتح", desc: "منع وصول أو صفحة لا تستجيب.", prompt: "لا أستطيع فتح الصفحة أو تظهر رسالة غير مصرح. ساعدني." },
        { title: "زر أو خيار لا يظهر", desc: "زر حفظ أو إجراء أو اعتماد مفقود.", prompt: "لا يظهر لي الزر أو الخيار الذي أحتاجه. ما الأسباب والحل؟" },
        { title: "طلب متوقف", desc: "المعاملة لا تنتقل للخطوة التالية.", prompt: "الطلب متوقف ولا ينتقل للخطوة التالية. كيف أتحقق من السبب؟" },
        { title: "مشكلة في المرفق", desc: "فشل رفع ملف أو فتحه.", prompt: "فشل رفع الملف أو المرفق. كيف أحل المشكلة؟" },
        { title: "البيانات لا تظهر", desc: "قائمة فارغة أو نتيجة مفقودة.", prompt: "القائمة فارغة أو البيانات لا تظهر. كيف أتحقق من السبب؟" },
        { title: "انتهت الجلسة", desc: "تم تسجيل الخروج أو تعذر الحفظ.", prompt: "انتهت الجلسة أو تم تسجيل خروجي. ماذا أفعل؟" },
        { title: "مشكلة أخرى", desc: "صف الشاشة والخطوة ورسالة الخطأ.", compose: "صف المشكلة، واذكر اسم الشاشة وما فعلته ورسالة الخطأ كما ظهرت..." },
      ],
    },
    admin: {
      title: "مساعدة الإدارة والتقنية",
      items: [
        { title: "ملخص النظام", desc: "أعداد المستخدمين والمعاملات والسجلات.", prompt: "أعطني ملخص النظام وإحصاءاته الحالية." },
        { title: "هيكلية المشروع", desc: "المجلدات والمكونات ومسارات Flask.", prompt: "اشرح هيكلية المشروع ومكوناته الرئيسية مع المصادر." },
        { title: "قاعدة البيانات", desc: "الجداول والعلاقات والنماذج.", prompt: "ما جداول قاعدة البيانات وعلاقاتها؟" },
        { title: "نظام الصلاحيات", desc: "الأدوار وفحص الوصول داخل الكود.", prompt: "كيف يعمل نظام الصلاحيات في الكود؟" },
      ],
    },
  };
  if (internalKnowledgeEnabled) {
    helpMenus.home.items.push({
      icon: "bi-code-square",
      title: "مساعدة الإدارة والتقنية",
      desc: "الكود وقاعدة البيانات وهيكلية النظام.",
      menu: "admin",
    });
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
    suggestions.className = "masar-assistant__suggestions";
    (Array.isArray(items) && items.length ? items : defaultSuggestions).slice(0, 7).forEach((label) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "masar-assistant__suggestion";
      button.textContent = label;
      button.addEventListener("click", () => sendMessage(label));
      suggestions.appendChild(button);
    });
  }

  function focusComposer(placeholder) {
    suggestions.replaceChildren();
    suggestions.className = "masar-assistant__suggestions";
    renderSuggestions(defaultSuggestions);
    input.value = "";
    input.placeholder = placeholder || defaultInputPlaceholder;
    input.focus();
  }

  function renderHelpMenu(menuName) {
    const menu = helpMenus[menuName] || helpMenus.home;
    suggestions.replaceChildren();
    suggestions.className = "masar-assistant__suggestions masar-assistant__suggestions--menu";
    suggestions.dataset.menu = menuName;

    const heading = document.createElement("div");
    heading.className = "masar-assistant__help-heading";
    if (menuName !== "home") {
      const back = document.createElement("button");
      back.type = "button";
      back.className = "masar-assistant__help-back";
      back.setAttribute("aria-label", "العودة إلى أنواع المساعدة");
      back.innerHTML = '<i class="bi bi-arrow-right" aria-hidden="true"></i>';
      back.addEventListener("click", () => renderHelpMenu("home"));
      heading.appendChild(back);
    }
    const headingText = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = menu.title;
    headingText.appendChild(title);
    if (menu.hint) {
      const hint = document.createElement("small");
      hint.textContent = menu.hint;
      headingText.appendChild(hint);
    }
    heading.appendChild(headingText);
    suggestions.appendChild(heading);

    const grid = document.createElement("div");
    grid.className = `masar-assistant__help-grid${menuName === "home" ? "" : " masar-assistant__help-grid--list"}`;
    menu.items.forEach((item) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "masar-assistant__help-card";
      if (item.icon) {
        const icon = document.createElement("i");
        icon.className = `bi ${item.icon}`;
        icon.setAttribute("aria-hidden", "true");
        button.appendChild(icon);
      }
      const copy = document.createElement("span");
      const itemTitle = document.createElement("strong");
      itemTitle.textContent = item.title;
      copy.appendChild(itemTitle);
      if (item.desc) {
        const desc = document.createElement("small");
        desc.textContent = item.desc;
        copy.appendChild(desc);
      }
      button.appendChild(copy);
      button.addEventListener("click", () => {
        if (item.menu) {
          renderHelpMenu(item.menu);
        } else if (item.compose) {
          focusComposer(item.compose);
        } else if (item.prompt) {
          input.placeholder = defaultInputPlaceholder;
          sendMessage(item.prompt);
        }
      });
      grid.appendChild(button);
    });
    suggestions.appendChild(grid);
  }

  function renderConversation() {
    messages.replaceChildren();
    if (!history.length) {
      appendMessage(
        "assistant",
        aiReady
          ? "أهلًا بك! الذكاء الخارجي متاح للأسئلة العامة النظيفة فقط، وتبقى بيانات النظام والعمل الحكومي داخل الخادم المحلي."
          : internalKnowledgeEnabled
            ? "أهلًا بك! يمكنك التحدث معي بكلام عادي أو اختيار مساعدة جاهزة. أشرح الشاشات والخطوات والبيانات والمشكلات، بينما تحتاج المحادثة العامة المفتوحة إلى تفعيل الوضع الذكي."
            : "أهلًا بك! تكلّم معي بكلام عادي أو اختر نوع المساعدة من القائمة. أستطيع الحوار اليومي البسيط ومساعدتك داخل النظام، بينما تحتاج المحادثة العامة المفتوحة إلى تفعيل الوضع الذكي من الإدارة."
      );
      renderHelpMenu("home");
    } else {
      history.forEach((item) => appendMessage(item.role, item.content, item.links, "", item.sources));
      renderSuggestions(defaultSuggestions);
    }
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
        ? `محادثة ذكية — ${accessLabel}${knowledgeLabel}`
        : `محادثة محلية — ${accessLabel}${knowledgeLabel}`;
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
  homeButton.addEventListener("click", () => {
    input.placeholder = defaultInputPlaceholder;
    renderHelpMenu("home");
  });
  clearButton.addEventListener("click", () => {
    history = [];
    saveHistory();
    input.placeholder = defaultInputPlaceholder;
    modeLabel.textContent = aiReady
      ? "محادثة ذكية تتذكر سياق كلامك"
      : "محادثة محلية ومساعدة داخل النظام";
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
