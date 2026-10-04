(function () {
  function initialiseNotificationRichEditor() {
    var form = document.querySelector('[data-notification-rich-editor]');
    var editor = document.getElementById('notificationRichEditor');
    var toolbar = document.getElementById('notificationRichToolbar');
    var bodyField = document.getElementById('notificationBody');
    if (!form || !editor || !toolbar || !bodyField) return;

    toolbar.hidden = false;
    editor.hidden = false;
    bodyField.classList.add('d-none');

    var savedRange = null;
    function saveSelection() {
      var selection = window.getSelection();
      if (!selection || !selection.rangeCount || !editor.contains(selection.anchorNode)) return;
      savedRange = selection.getRangeAt(0).cloneRange();
    }
    function restoreSelection() {
      if (!savedRange) return;
      var selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(savedRange);
    }
    function syncBody() {
      bodyField.value = editor.innerHTML;
    }
    function runCommand(command, value) {
      restoreSelection();
      editor.focus();
      document.execCommand(command, false, value || null);
      saveSelection();
      syncBody();
    }

    document.addEventListener('selectionchange', saveSelection);
    editor.addEventListener('input', syncBody);
    editor.addEventListener('keyup', saveSelection);
    editor.addEventListener('mouseup', saveSelection);

    toolbar.querySelectorAll('button[data-command]').forEach(function (button) {
      button.addEventListener('mousedown', function (event) { event.preventDefault(); });
      button.addEventListener('click', function () {
        runCommand(button.dataset.command, button.dataset.commandValue || null);
      });
    });
    toolbar.querySelector('[data-action="link"]').addEventListener('mousedown', function (event) { event.preventDefault(); });
    toolbar.querySelector('[data-action="link"]').addEventListener('click', function () {
      var href = (window.prompt('أدخل رابطًا يبدأ بـ https:// أو http:// أو /', 'https://') || '').trim();
      if (!href) return;
      if (!/^(https?:\/\/|mailto:|\/|#)/i.test(href)) {
        window.alert('الرابط غير صالح. استخدم رابطًا آمنًا أو رابطًا داخليًا يبدأ بـ /.');
        return;
      }
      runCommand('createLink', href);
    });
    document.getElementById('notificationTextColor').addEventListener('change', function () {
      runCommand('foreColor', this.value);
    });
    document.getElementById('notificationBackgroundColor').addEventListener('change', function () {
      runCommand('hiliteColor', this.value);
    });
    form.addEventListener('submit', function (event) {
      syncBody();
      if (!(editor.innerText || '').replace(/\u00a0/g, ' ').trim()) {
        event.preventDefault();
        editor.focus();
        window.alert('يرجى كتابة نص الرسالة التفصيلي.');
      }
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initialiseNotificationRichEditor);
  } else {
    initialiseNotificationRichEditor();
  }
}());
