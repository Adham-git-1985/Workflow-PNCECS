(function () {
  // Global system search (command palette)
  // Works in both مسار layout and البوابة layout.

  function qs(sel, root) { return (root || document).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }

  const modalEl = document.getElementById('systemSearchModal');
  if (!modalEl) return;

  const input = qs('#systemSearchInput', modalEl);
  const list = qs('#systemSearchResults', modalEl);
  const hint = qs('#systemSearchHint', modalEl);
  const form = qs('#systemSearchForm', modalEl);

  let timer = null;
  let lastQ = '';
  let activeIdx = -1;

  function escapeHtml(s) {
    return String(s || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function setHint(text) {
    if (!hint) return;
    hint.textContent = text || '';
  }

  function clearResults() {
    if (!list) return;
    list.innerHTML = '';
    activeIdx = -1;
  }

  function renderResults(results) {
    clearResults();
    if (!list) return;
    if (!results || !results.length) {
      list.innerHTML = '<div class="text-muted small px-1 py-2">لا توجد نتائج.</div>';
      return;
    }
    const html = results.map(function (r, idx) {
      const title = escapeHtml(r.title);
      const desc = escapeHtml(r.desc || '');
      const cat = escapeHtml(r.category || '');
      const href = escapeHtml(r.href || '#');
      return (
        '<a class="list-group-item list-group-item-action d-flex justify-content-between align-items-start gap-2" '
        + 'href="' + href + '" data-idx="' + idx + '" '
        + 'style="cursor:pointer">'
        +   '<div>'
        +     '<div class="fw-bold">' + title + '</div>'
        +     (desc ? '<div class="text-muted small">' + desc + '</div>' : '')
        +   '</div>'
        +   '<span class="badge bg-light text-dark border">' + cat + '</span>'
        + '</a>'
      );
    }).join('');
    list.innerHTML = html;
    // activate first
    activeIdx = 0;
    highlightActive();
  }

  function highlightActive() {
    const items = qsa('[data-idx]', list);
    items.forEach(function (el) { el.classList.remove('active'); });
    if (activeIdx >= 0 && activeIdx < items.length) {
      items[activeIdx].classList.add('active');
      try { items[activeIdx].scrollIntoView({ block: 'nearest' }); } catch (e) {}
    }
  }

  async function fetchResults(q) {
    const url = '/users/api/search?q=' + encodeURIComponent(q || '');
    const res = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!res.ok) return [];
    const data = await res.json();
    return (data && data.results) ? data.results : [];
  }

  function doSearch(q) {
    const qq = (q || '').trim();
    lastQ = qq;
    setHint(qq ? 'نتائج البحث' : 'اقتراحات');
    fetchResults(qq).then(renderResults).catch(function () {
      clearResults();
      list.innerHTML = '<div class="text-danger small px-1 py-2">تعذر تحميل نتائج البحث.</div>';
    });
  }

  function schedule(q) {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(function () { doSearch(q); }, 180);
  }

  // Ctrl+K opens modal
  document.addEventListener('keydown', function (e) {
    const isMac = navigator.platform && /Mac/.test(navigator.platform);
    const k = (e.key || '').toLowerCase();
    const combo = (isMac ? e.metaKey : e.ctrlKey) && k === 'k';
    if (!combo) return;
    e.preventDefault();
    try {
      const m = bootstrap.Modal.getOrCreateInstance(modalEl);
      m.show();
    } catch (err) {}
  });

  // When modal shown
  modalEl.addEventListener('shown.bs.modal', function () {
    try { input.focus(); input.select(); } catch (e) {}
    doSearch(input.value || '');
  });

  // Input
  input.addEventListener('input', function () {
    schedule(input.value);
  });

  // Keyboard navigation
  input.addEventListener('keydown', function (e) {
    const items = qsa('[data-idx]', list);
    if (!items.length) return;

    if (e.key === 'ArrowDown') {
      e.preventDefault();
      activeIdx = Math.min(activeIdx + 1, items.length - 1);
      highlightActive();
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      highlightActive();
    } else if (e.key === 'Enter') {
      // open active
      if (activeIdx >= 0 && activeIdx < items.length) {
        e.preventDefault();
        const href = items[activeIdx].getAttribute('href');
        if (href) window.location.href = href;
      }
    }
  });

  // Click result closes modal
  modalEl.addEventListener('click', function (e) {
    const a = e.target && e.target.closest ? e.target.closest('a[data-idx]') : null;
    if (!a) return;
    try {
      const m = bootstrap.Modal.getInstance(modalEl);
      if (m) m.hide();
    } catch (err) {}
  });

  // Form submit -> full search page
  if (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      const q = (input.value || '').trim();
      window.location.href = '/users/search?q=' + encodeURIComponent(q);
    });
  }
})();
