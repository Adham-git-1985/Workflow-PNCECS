/* Replace large item <select> lists in vouchers with a small remote search. */
(function () {
  'use strict';
  const defaultEndpoint = '/portal/inventory/items/search.json';
  const minimumSearchLength = 2;
  const resultCache = new Map();
  let timer;

  function escapeHtml(value) {
    const node = document.createElement('span');
    node.textContent = value || '';
    return node.innerHTML;
  }

  function enhance(select) {
    if (!select || select.dataset.itemPickerReady === '1') return;
    select.dataset.itemPickerReady = '1';
    const name = select.name;
    const selected = select.options[select.selectedIndex];
    const selectedId = selected && selected.value ? selected.value : '';
    const selectedText = selectedId ? selected.textContent.trim() : '';
    const required = select.required;
    const wrapper = document.createElement('div');
    wrapper.className = 'position-relative inventory-item-picker';
    wrapper.innerHTML = [
      '<input type="hidden" name="' + escapeHtml(name) + '" value="' + escapeHtml(selectedId) + '">',
      '<input type="search" class="form-control inventory-item-query" autocomplete="off" placeholder="اكتب كود الصنف أو اسمه (حرفان على الأقل)" value="' + escapeHtml(selectedText) + '">',
      '<div class="list-group position-absolute w-100 shadow-sm d-none inventory-item-results" style="z-index:1050;max-height:240px;overflow:auto"></div>'
    ].join('');
    select.replaceWith(wrapper);

    const id = wrapper.querySelector('input[type="hidden"]');
    const input = wrapper.querySelector('.inventory-item-query');
    const results = wrapper.querySelector('.inventory-item-results');
    const categorySelector = select.dataset.itemPickerCategory || '';
    const endpoint = select.dataset.itemPickerEndpoint || defaultEndpoint;
    const categoryInput = categorySelector ? document.querySelector(categorySelector) : null;
    if (required) input.required = true;

    function clearSelection() {
      id.value = '';
      input.setCustomValidity('اختر الصنف من نتائج البحث.');
      const row = wrapper.closest('tr');
      const previous = row && row.querySelector('.previous-request');
      if (previous) {
        previous.className = 'previous-request text-muted';
        previous.textContent = 'اختر صنفًا لعرض آخر طلب';
      }
    }
    function hideResults() {
      results.classList.add('d-none');
      results.replaceChildren();
    }
    function choose(item) {
      id.value = item.id;
      input.value = item.label;
      input.setCustomValidity('');
      const row = wrapper.closest('tr');
      const previous = row && row.querySelector('.previous-request');
      if (previous) {
        previous.replaceChildren();
        if (item.last_request) {
          previous.className = 'previous-request';
          const badge = document.createElement('span');
          badge.className = 'badge ' + (item.last_request.within_month ? 'text-bg-warning' : 'text-bg-info');
          badge.textContent = item.last_request.label;
          const detail = document.createElement('div');
          detail.className = 'small text-muted mt-1';
          detail.textContent = 'طلب #' + item.last_request.request_id + ' · ' + item.last_request.status_label;
          previous.append(badge, detail);
        } else {
          previous.className = 'previous-request text-muted';
          previous.textContent = 'لم يسبق طلب هذا الصنف';
        }
      }
      hideResults();
    }
    function show(items) {
      results.replaceChildren();
      if (!items.length) {
        results.innerHTML = '<span class="list-group-item text-muted small">لا توجد أصناف مطابقة.</span>';
      } else {
        items.forEach(function (item) {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'list-group-item list-group-item-action text-start';
          const history = item.last_request
            ? '<div class="small mt-1 ' + (item.last_request.within_month ? 'text-warning' : 'text-info') + '">' + escapeHtml(item.last_request.label + ' · ' + item.last_request.status_label) + '</div>'
            : '<div class="small mt-1 text-success">لم يسبق طلب هذا الصنف</div>';
          button.innerHTML = '<div>' + escapeHtml(item.label) + '</div><small class="text-muted">' + escapeHtml([item.code, item.category, item.unit].filter(Boolean).join(' — ')) + '</small>' + history;
          button.addEventListener('click', function () { choose(item); });
          results.appendChild(button);
        });
      }
      results.classList.remove('d-none');
    }
    async function search() {
      const query = input.value.trim();
      // A blank/one-character query used to fetch the first 60 catalogue
      // items every time a voucher line was focused.  On a large catalogue
      // that creates unnecessary database reads while the user is entering a
      // multi-line stock voucher.  The placeholder already instructs users
      // to search with at least two characters.
      if (query.length < minimumSearchLength) {
        hideResults();
        return;
      }
      try {
        const params = new URLSearchParams({q: query});
        if (categoryInput && categoryInput.value) params.set('category_id', categoryInput.value);
        const url = endpoint + '?' + params.toString();
        if (resultCache.has(url)) {
          show(resultCache.get(url));
          return;
        }
        const response = await fetch(url, {credentials: 'same-origin'});
        if (!response.ok) throw new Error('lookup failed');
        const items = (await response.json()).items || [];
        // Keep the cache intentionally small; it avoids repeated lookups for
        // the same material without turning a long data-entry session into a
        // client-side copy of the catalogue.
        if (resultCache.size >= 100) resultCache.clear();
        resultCache.set(url, items);
        show(items);
      } catch (_) {
        results.innerHTML = '<span class="list-group-item text-danger small">تعذر البحث عن الأصناف.</span>';
        results.classList.remove('d-none');
      }
    }
    input.addEventListener('input', function () {
      clearSelection();
      window.clearTimeout(timer);
      timer = window.setTimeout(search, 220);
    });
    input.addEventListener('focus', search);
    input.addEventListener('blur', function () { window.setTimeout(hideResults, 180); });
    if (categoryInput) {
      categoryInput.addEventListener('change', function () {
        clearSelection();
        search();
      });
    }
    if (selectedId) input.setCustomValidity('');
    else if (required) clearSelection();
  }

  function scan(root) {
    (root || document).querySelectorAll('select[name="item_id[]"], select[name="item_id"]').forEach(enhance);
  }
  document.addEventListener('DOMContentLoaded', function () {
    scan(document);
    new MutationObserver(function (records) {
      records.forEach(function (record) {
        record.addedNodes.forEach(function (node) {
          if (node.nodeType === 1) {
            if (node.matches && node.matches('select[name="item_id[]"], select[name="item_id"]')) enhance(node);
            scan(node);
          }
        });
      });
    }).observe(document.body, {childList: true, subtree: true});
  });
}());
