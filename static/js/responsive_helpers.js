// Responsive helpers for Masar (Workflow_PNCECS)
(function () {
  function setHeaderOffset() {
    var header = document.querySelector('.main-header');
    if (!header) return;
    var h = Math.ceil(header.getBoundingClientRect().height || 0);
    if (h < 50) h = 70;
    document.documentElement.style.setProperty('--wf-header-h', h + 'px');
  }

  function wrapTablesForMobile() {
    // Wrap tables in a horizontal scroll container on small screens,
    // without requiring template changes.
    if (window.innerWidth >= 992) return;

    var tables = document.querySelectorAll('table');
    tables.forEach(function (tbl) {
      if (!tbl || !tbl.parentElement) return;

      // Skip if already wrapped or already inside bootstrap responsive wrapper
      if (tbl.closest('.wf-table-scroll') || tbl.closest('.table-responsive')) return;

      // Skip tiny tables (1-2 columns) to avoid unnecessary wrappers
      var ths = tbl.querySelectorAll('thead th');
      if (ths && ths.length > 0 && ths.length <= 2) return;

      // Wrap
      var wrap = document.createElement('div');
      wrap.className = 'wf-table-scroll';
      tbl.parentElement.insertBefore(wrap, tbl);
      wrap.appendChild(tbl);
    });
  }

  function enableOffcanvasAutoClose() {
    var offcanvasEl = document.getElementById('sidebarOffcanvas');
    if (!offcanvasEl) return;

    document.addEventListener('click', function (e) {
      var a = e.target && e.target.closest ? e.target.closest('#sidebarOffcanvas a') : null;
      if (!a) return;
      try {
        // bootstrap is available via bootstrap.bundle
        var inst = window.bootstrap && window.bootstrap.Offcanvas
          ? window.bootstrap.Offcanvas.getInstance(offcanvasEl) || new window.bootstrap.Offcanvas(offcanvasEl)
          : null;
        if (inst) inst.hide();
      } catch (_) {}
    });
  }

  function debounce(fn, wait) {
    var t;
    return function () {
      clearTimeout(t);
      t = setTimeout(fn, wait);
    };
  }

  document.addEventListener('DOMContentLoaded', function () {
    setHeaderOffset();
    wrapTablesForMobile();
    enableOffcanvasAutoClose();

    // If images in header load after DOMContentLoaded, recalc.
    setTimeout(setHeaderOffset, 250);
    setTimeout(setHeaderOffset, 800);
  });

  window.addEventListener('resize', debounce(function () {
    setHeaderOffset();
    wrapTablesForMobile();
  }, 150));
})();
