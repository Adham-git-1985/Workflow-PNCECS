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

  function enhanceSelect(selectEl) {
    if (!shouldEnhance(selectEl)) return;

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
    input.placeholder = isRtl() ? "اكتب للبحث داخل القائمة..." : "Type to search...";
    input.setAttribute("aria-label", isRtl() ? "بحث داخل القائمة" : "Search within list");

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
