/**
 * Masar / Portal - Smart Print Helpers
 * - wfPrintSmart(selector): prints target area if selector exists, otherwise prints full page.
 * - wfGuideOpenAll(containerSel): open all <details> inside container.
 * - wfGuideCloseAll(containerSel): close all <details> inside container.
 * Notes:
 *   - For target-only printing we add a body class and mark the target with .wf-print-target
 */
(function () {
  function _q(sel) { try { return document.querySelector(sel); } catch (_) { return null; } }
  function _qa(sel) { try { return Array.from(document.querySelectorAll(sel)); } catch (_) { return []; } }

  function _openAllDetails(root) {
    (root ? root.querySelectorAll('details') : document.querySelectorAll('details')).forEach(function (d) { d.open = true; });
  }
  function _closeAllDetails(root) {
    (root ? root.querySelectorAll('details') : document.querySelectorAll('details')).forEach(function (d) { d.open = false; });
  }

  function _expandBootstrapCollapses() {
    // Open Bootstrap collapse elements if present
    _qa('.collapse').forEach(function (el) {
      if (el.classList.contains('show')) return;
      // if it is within print target we open it; otherwise leave it
      try { el.classList.add('show'); } catch (_) {}
    });
  }

  function _markTargetOnly(targetEl) {
    document.body.classList.add('wf-print-only-target');
    targetEl.classList.add('wf-print-target');
  }

  function _unmarkTargetOnly(targetEl) {
    document.body.classList.remove('wf-print-only-target');
    if (targetEl) targetEl.classList.remove('wf-print-target');
  }

  window.wfGuideOpenAll = function (containerSel) {
    var root = containerSel ? _q(containerSel) : null;
    _openAllDetails(root);
  };

  window.wfGuideCloseAll = function (containerSel) {
    var root = containerSel ? _q(containerSel) : null;
    _closeAllDetails(root);
  };

  window.wfPrintSmart = function (selector) {
    // 1) Try print target area only
    var target = selector ? _q(selector) : null;

    // remember current open states of <details>
    var details = _qa('details');
    var prev = details.map(function (d) { return !!d.open; });

    try {
      _openAllDetails(target || document);
      _expandBootstrapCollapses();

      if (target) {
        _markTargetOnly(target);
      }

      // Let layout settle before print
      setTimeout(function () {
        window.print();
        setTimeout(function () {
          // restore
          details.forEach(function (d, i) { d.open = prev[i]; });
          _unmarkTargetOnly(target);
        }, 300);
      }, 120);
    } catch (e) {
      // fallback
      try { window.print(); } catch (_) {}
    }
  };

  // Convenience: print central area if present, else full page.
  // Uses the same Smart Print mechanism.
  window.wfPrintAuto = function () {
    if (_q('#wfPrintArea')) return window.wfPrintSmart('#wfPrintArea');
    if (_q('#app-main')) return window.wfPrintSmart('#app-main');
    if (_q('main')) return window.wfPrintSmart('main');
    try { window.print(); } catch (_) {}
  };
})();
