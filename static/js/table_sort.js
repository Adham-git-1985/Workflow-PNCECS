// Simple client-side table sorting (click header to sort)
// Usage: add data-sortable="true" on <table> and optionally data-nosort on <th>.
(function () {
  function normalizeText(t) {
    return (t || "").toString().trim().replace(/\s+/g, " ");
  }

  function isNumericText(t) {
    const s = normalizeText(t).replace(/,/g, "");
    return /^-?\d+(\.\d+)?$/.test(s);
  }

  function getCellValue(tr, idx) {
    const td = tr.children[idx];
    if (!td) return "";
    // Prefer explicit sort key if provided
    const key = td.getAttribute("data-sort") || td.getAttribute("data-sort-key");
    if (key != null) return normalizeText(key);
    return normalizeText(td.innerText);
  }

  function sortTable(table, colIdx, asc) {
    const tbody = table.tBodies[0];
    if (!tbody) return;
    const rows = Array.from(tbody.querySelectorAll("tr"));
    rows.sort((a, b) => {
      const av = getCellValue(a, colIdx);
      const bv = getCellValue(b, colIdx);
      const aNum = isNumericText(av);
      const bNum = isNumericText(bv);
      if (aNum && bNum) {
        const an = parseFloat(av.replace(/,/g, ""));
        const bn = parseFloat(bv.replace(/,/g, ""));
        return asc ? (an - bn) : (bn - an);
      }
      // String compare (Arabic + English)
      return asc ? av.localeCompare(bv, undefined, { numeric: true, sensitivity: "base" })
                : bv.localeCompare(av, undefined, { numeric: true, sensitivity: "base" });
    });
    rows.forEach(r => tbody.appendChild(r));
  }

  function init() {
    const tables = document.querySelectorAll("table[data-sortable='true']");
    tables.forEach(table => {
      const thead = table.tHead;
      if (!thead || !thead.rows.length) return;
      const headerRow = thead.rows[0];
      Array.from(headerRow.cells).forEach((th, idx) => {
        if (th.hasAttribute("data-nosort")) return;
        th.style.cursor = "pointer";
        th.title = "اضغط للترتيب";

        th.addEventListener("click", () => {
          // Toggle sort direction per column
          const current = table.getAttribute("data-sort-col");
          const currentDir = table.getAttribute("data-sort-dir") || "asc";
          let asc = true;
          if (current === String(idx)) {
            asc = currentDir !== "asc";
          }
          table.setAttribute("data-sort-col", String(idx));
          table.setAttribute("data-sort-dir", asc ? "asc" : "desc");

          // Visual indicator
          Array.from(headerRow.cells).forEach(h => {
            h.classList.remove("sorted-asc", "sorted-desc");
          });
          th.classList.add(asc ? "sorted-asc" : "sorted-desc");

          sortTable(table, idx, asc);
        });
      });
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
