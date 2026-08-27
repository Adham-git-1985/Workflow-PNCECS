/*
  Searchable selects (no external libs)
  - Adds a small search input above large <select> elements to filter options.
  - Applies to:
      * <select data-searchable="1">
      * OR any single-select with >= 10 options (excluding placeholders)
  - Disable per select with: data-searchable="0" or data-no-search="1"
*/

(function () {
  "use strict";

  const DEFAULT_THRESHOLD = 10;

  function isRtl() {
    const dir = (document.documentElement.getAttribute("dir") || "").toLowerCase();
    return dir === "rtl";
  }

  function normalizeText(s) {
    return (s || "").toString().trim().toLowerCase();
  }

  function optionCount(selectEl) {
    // count non-empty options, ignoring placeholder-style empty values
    let n = 0;
    for (const opt of selectEl.options) {
      if (!opt) continue;
      if ((opt.value || "").toString().trim() === "") continue;
      n += 1;
    }
    return n;
  }

  function shouldEnhance(selectEl) {
    if (!selectEl) return false;
    if (selectEl.dataset && (selectEl.dataset.noSearch === "1" || selectEl.dataset.searchable === "0")) return false;
    // Some pages keep a native select only as a backing value for a richer
    // picker.  Enhancing that hidden select creates a visible search box while
    // the actual option list remains hidden, which looks like an empty picker.
    if (
      selectEl.hidden ||
      selectEl.classList.contains("d-none") ||
      selectEl.getAttribute("aria-hidden") === "true"
    ) return false;
    if (selectEl.multiple) return false;
    if (selectEl.disabled) return false;
    if (selectEl.classList.contains("ss-enhanced")) return false;
    if (selectEl.closest(".ss-wrapper")) return false;

    const forced = selectEl.dataset && selectEl.dataset.searchable === "1";
    if (forced) return true;

    const count = optionCount(selectEl);
    return count >= DEFAULT_THRESHOLD;
  }

  function filterOptions(selectEl, q) {
    const query = normalizeText(q);
    let firstVisible = null;
    for (const opt of selectEl.options) {
      // Always keep placeholder visible
      const isPlaceholder = (opt.value || "").toString().trim() === "";
      if (isPlaceholder) {
        opt.hidden = false;
        continue;
      }

      const text = normalizeText(opt.textContent || opt.innerText || opt.label);
      const show = !query || text.includes(query);
      opt.hidden = !show;
      if (show && !firstVisible) firstVisible = opt;
    }
    return firstVisible;
  }

  function enhanceBoundedSelect(selectEl) {
    const wrapper = document.createElement("div");
    wrapper.className = "ss-wrapper dropdown";
    wrapper.style.width = "100%";
    wrapper.style.maxWidth = "100%";
    wrapper.style.minWidth = "0";

    const parent = selectEl.parentNode;
    parent.insertBefore(wrapper, selectEl);
    wrapper.appendChild(selectEl);
    selectEl.classList.add("ss-enhanced", "d-none");

    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "form-select text-start ss-picker-toggle";
    toggle.dataset.bsToggle = "dropdown";
    toggle.dataset.bsAutoClose = "outside";
    toggle.dataset.bsDisplay = "static";
    toggle.setAttribute("aria-expanded", "false");
    toggle.setAttribute("aria-haspopup", "listbox");

    const label = document.createElement("span");
    label.className = "ss-picker-label";
    label.style.display = "block";
    label.style.minWidth = "0";
    label.style.overflow = "hidden";
    label.style.textOverflow = "ellipsis";
    label.style.whiteSpace = "nowrap";
    toggle.appendChild(label);

    const menu = document.createElement("div");
    menu.className = "dropdown-menu shadow p-2 ss-picker-menu";
    menu.style.width = "100%";
    menu.style.minWidth = "0";
    menu.style.maxWidth = "100%";
    menu.style.overflow = "hidden";

    const input = document.createElement("input");
    input.type = "search";
    input.autocomplete = "off";
    input.className = "form-control form-control-sm mb-2 ss-input";
    input.placeholder = isRtl() ? "اكتب اسم الجهة أو المستخدم..." : "Search...";
    input.setAttribute("aria-label", isRtl() ? "بحث داخل قائمة جهة الاختصاص" : "Search options");
    if (isRtl()) input.dir = "rtl";

    const results = document.createElement("div");
    results.className = "ss-picker-results";
    results.setAttribute("role", "listbox");
    results.style.maxHeight = "18rem";
    results.style.overflowX = "hidden";
    results.style.overflowY = "auto";

    const empty = document.createElement("div");
    empty.className = "text-muted text-center small py-3 d-none";
    empty.textContent = isRtl() ? "لا توجد نتائج مطابقة." : "No matching results.";

    menu.appendChild(input);
    menu.appendChild(results);
    menu.appendChild(empty);
    wrapper.appendChild(toggle);
    wrapper.appendChild(menu);

    let optionButtons = [];
    const rebuildOptions = function () {
      results.innerHTML = "";
      optionButtons = Array.from(selectEl.options).map(function (option) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "dropdown-item ss-picker-option";
        button.dataset.value = option.value || "";
        button.dataset.label = (option.textContent || option.label || "").trim();
        button.textContent = button.dataset.label;
        button.setAttribute("role", "option");
        button.style.display = "block";
        button.style.width = "100%";
        button.style.padding = ".55rem .7rem";
        button.style.textAlign = "start";
        button.style.whiteSpace = "normal";
        button.style.overflowWrap = "anywhere";
        button.style.lineHeight = "1.45";
        button.style.borderRadius = ".35rem";
        if (!option.value) button.classList.add("text-muted");
        button.addEventListener("click", function () {
          selectEl.value = button.dataset.value;
          selectEl.dispatchEvent(new Event("change", { bubbles: true }));
          input.value = "";
          filterButtons();
          if (window.bootstrap && window.bootstrap.Dropdown) {
            window.bootstrap.Dropdown.getOrCreateInstance(toggle).hide();
          }
        });
        results.appendChild(button);
        return button;
      });
    };

    const refreshSelection = function () {
      const selected = selectEl.options[selectEl.selectedIndex];
      const selectedLabel = selected ? (selected.textContent || selected.label || "").trim() : "";
      label.textContent = selectedLabel || (isRtl() ? "— اختر —" : "— Select —");
      toggle.title = selected && selected.value ? selectedLabel : "";
      if (selected && !optionButtons.some(function (button) { return button.dataset.value === selected.value; })) {
        rebuildOptions();
      }
      optionButtons.forEach(function (button) {
        const active = button.dataset.value === selectEl.value;
        button.classList.toggle("active", active);
        button.setAttribute("aria-selected", active ? "true" : "false");
      });
    };

    const filterButtons = function () {
      const query = normalizeText(input.value);
      let visibleCount = 0;
      optionButtons.forEach(function (button) {
        const visible = !query || normalizeText(button.dataset.label).includes(query);
        button.classList.toggle("d-none", !visible);
        if (visible) visibleCount += 1;
      });
      empty.classList.toggle("d-none", visibleCount !== 0);
    };

    rebuildOptions();
    refreshSelection();
    input.addEventListener("input", filterButtons);
    input.addEventListener("click", function (event) { event.stopPropagation(); });
    selectEl.addEventListener("change", refreshSelection);
    toggle.addEventListener("shown.bs.dropdown", function () { input.focus(); });
  }

  function enhanceSelect(selectEl) {
    if (!shouldEnhance(selectEl)) return;

    if (selectEl.dataset && selectEl.dataset.searchableMode === "bounded") {
      enhanceBoundedSelect(selectEl);
      return;
    }

    // wrapper
    const wrapper = document.createElement("div");
    wrapper.className = "ss-wrapper";
    wrapper.style.width = "100%";

    // Insert wrapper in DOM
    const parent = selectEl.parentNode;
    parent.insertBefore(wrapper, selectEl);

    // Search input
    const input = document.createElement("input");
    input.type = "text";
    input.autocomplete = "off";
    input.className = "form-control form-control-sm mb-1 ss-input";
    input.placeholder = isRtl() ? "اكتب للبحث داخل القائمة..." : "اكتب للبحث...";
    input.setAttribute("aria-label", isRtl() ? "بحث داخل القائمة" : "بحث within list");

    // Move select inside wrapper
    wrapper.appendChild(input);
    wrapper.appendChild(selectEl);

    // Mark enhanced
    selectEl.classList.add("ss-enhanced");

    // Keep direction consistent
    if (isRtl()) {
      input.dir = "rtl";
    }

    // Filtering
    let lastValue = "";
    const doFilter = () => {
      const v = input.value;
      if (v === lastValue) return;
      lastValue = v;
      filterOptions(selectEl, v);
    };

    input.addEventListener("input", doFilter);

    // Enter = select first visible option
    input.addEventListener("keydown", function (e) {
      if (e.key !== "Enter") return;
      const first = filterOptions(selectEl, input.value);
      if (first) {
        selectEl.value = first.value;
        selectEl.dispatchEvent(new Event("change", { bubbles: true }));
      }
      e.preventDefault();
    });

    // When user changes select, keep search box (optional) — do nothing.
    // But if current selection becomes hidden due to filter, clear filter.
    selectEl.addEventListener("change", function () {
      const selOpt = selectEl.options[selectEl.selectedIndex];
      if (selOpt && selOpt.hidden) {
        input.value = "";
        lastValue = "";
        filterOptions(selectEl, "");
      }
    });
  }

  function init(root) {
    const scope = root || document;
    const selects = scope.querySelectorAll("select");
    selects.forEach(enhanceSelect);
  }

  // Expose for dynamic content
  window.initSearchableSelects = init;

  document.addEventListener("DOMContentLoaded", function () {
    init(document);

    // Re-init inside Bootstrap modals when shown
    document.addEventListener("shown.bs.modal", function (evt) {
      if (evt && evt.target) init(evt.target);
    });
  });
})();
