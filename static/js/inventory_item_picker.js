/* Replace large item <select> lists in vouchers with a small remote search. */
(function () {
  'use strict';
  const endpoint = '/portal/inventory/items/search.json';
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
    const categoryInput = categorySelector ? document.querySelector(categorySelector) : null;
    if (required) input.required = true;

    function clearSelection() {
      id.value = '';
      input.setCustomValidity('اختر الصنف من نتائج البحث.');
    }
    function hideResults() {
      results.classList.add('d-none');
      results.replaceChildren();
    }
    function choose(item) {
      id.value = item.id;
      input.value = item.label;
      input.setCustomValidity('');
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
          button.innerHTML = '<div>' + escapeHtml(item.label) + '</div><small class="text-muted">' + escapeHtml([item.code, item.category, item.unit].filter(Boolean).join(' — ')) + '</small>';
          button.addEventListener('click', function () { choose(item); });
          results.appendChild(button);
        });
      }
      results.classList.remove('d-none');
    }
    async function search() {
      const query = input.value.trim();
      try {
        const params = new URLSearchParams({q: query});
        if (categoryInput && categoryInput.value) params.set('category_id', categoryInput.value);
        const response = await fetch(endpoint + '?' + params.toString(), {credentials: 'same-origin'});
        if (!response.ok) throw new Error('lookup failed');
        show((await response.json()).items || []);
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
