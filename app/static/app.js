(() => {
  const one = (selector, root = document) => root.querySelector(selector);
  const all = (selector, root = document) => [...root.querySelectorAll(selector)];

  all('[data-dismiss]').forEach((button) => {
    button.addEventListener('click', () => button.closest('.flash')?.remove());
  });
  const flash = one('.flash');
  if (flash) window.setTimeout(() => flash.remove(), 6500);

  const sidebar = one('#sidebar');
  one('[data-sidebar-toggle]')?.addEventListener('click', () => sidebar?.classList.toggle('open'));
  document.addEventListener('click', (event) => {
    if (window.innerWidth <= 820 && sidebar?.classList.contains('open') && !sidebar.contains(event.target) && !event.target.closest('[data-sidebar-toggle]')) {
      sidebar.classList.remove('open');
    }
  });

  all('[data-dialog-open]').forEach((button) => {
    button.addEventListener('click', () => document.getElementById(button.dataset.dialogOpen)?.showModal());
  });
  all('[data-dialog-close]').forEach((button) => {
    button.addEventListener('click', () => button.closest('dialog')?.close());
  });
  const mailboxPreset = one('[data-mailbox-preset]');
  if (mailboxPreset) {
    const presets = {
      qq: { name: 'QQ 邮箱', host: 'imap.qq.com', port: '993', ssl: true },
      netease: { name: '网易邮箱', host: 'imap.163.com', port: '993', ssl: true },
      chency: { name: '承希邮箱', host: 'imap.263.net', port: '993', ssl: true },
    };
    const form = mailboxPreset.closest('form');
    const field = (name) => one(`input[name="${name}"]`, form);
    mailboxPreset.addEventListener('change', () => {
      const preset = presets[mailboxPreset.value];
      if (!preset) return;
      field('host').value = preset.host;
      field('port').value = preset.port;
      const ssl = field('use_ssl');
      if (ssl) ssl.checked = preset.ssl;
      const name = field('name');
      if (name) {
        const current = name.value.trim();
        const presetNames = Object.values(presets).map((item) => item.name);
        if (!current || presetNames.includes(current)) name.value = preset.name;
      }
    });
  }
  all('dialog').forEach((dialog) => {
    dialog.addEventListener('click', (event) => {
      const bounds = dialog.getBoundingClientRect();
      if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
    });
  });

  all('[data-settings-toggle]').forEach((toggle) => {
    const target = document.getElementById(toggle.dataset.settingsToggle);
    if (!target) return;
    const sync = () => { target.hidden = !toggle.checked; };
    toggle.addEventListener('change', sync);
    sync();
  });

  all('form[data-confirm]').forEach((form) => {
    form.addEventListener('submit', (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const uploadForm = one('[data-upload-form]');
  if (uploadForm) {
    const input = one('[data-file-input]', uploadForm);
    const zone = one('[data-drop-zone]', uploadForm);
    const list = one('[data-selected-files]', uploadForm);
    const button = one('[data-upload-button]', uploadForm);
    const formatSize = (bytes) => bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(0)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    const renderFiles = () => {
      list.replaceChildren();
      [...input.files].forEach((file) => {
        const row = document.createElement('div');
        const name = document.createElement('strong');
        const size = document.createElement('small');
        name.textContent = file.name;
        size.textContent = formatSize(file.size);
        row.append(name, size);
        list.append(row);
      });
      list.hidden = input.files.length === 0;
      button.disabled = input.files.length === 0;
    };
    input.addEventListener('change', renderFiles);
    ['dragenter', 'dragover'].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.add('dragging'); }));
    ['dragleave', 'drop'].forEach((name) => zone.addEventListener(name, (event) => { event.preventDefault(); zone.classList.remove('dragging'); }));
    zone.addEventListener('drop', (event) => {
      const transfer = new DataTransfer();
      [...event.dataTransfer.files].forEach((file) => transfer.items.add(file));
      input.files = transfer.files;
      renderFiles();
    });
    uploadForm.addEventListener('submit', () => {
      button.disabled = true;
      button.textContent = '正在接收文件…';
    });
  }

  const invoiceChecks = all('[data-invoice-select]');
  if (invoiceChecks.length) {
    const selectAll = one('[data-select-all]');
    const count = one('[data-selected-count]');
    const update = () => {
      const checked = invoiceChecks.filter((item) => item.checked);
      count.textContent = checked.length;
      selectAll.checked = checked.length === invoiceChecks.length && invoiceChecks.length > 0;
      selectAll.indeterminate = checked.length > 0 && checked.length < invoiceChecks.length;
    };
    invoiceChecks.forEach((item) => item.addEventListener('change', update));
    selectAll?.addEventListener('change', () => { invoiceChecks.forEach((item) => { item.checked = selectAll.checked; }); update(); });
    const selectedIds = () => invoiceChecks.filter((item) => item.checked).map((item) => item.value);
    const guardSelection = () => {
      const ids = selectedIds();
      if (!ids.length) {
        window.alert('请先勾选发票');
        return null;
      }
      return ids;
    };
    one('[data-print-selected]')?.addEventListener('click', () => {
      const ids = guardSelection();
      if (!ids) return;
      window.location.href = `/print?ids=${encodeURIComponent(ids.join(','))}`;
    });
    one('[data-export-selected]')?.addEventListener('click', () => {
      const ids = guardSelection();
      if (!ids) return;
      window.location.href = `/export/files?ids=${encodeURIComponent(ids.join(','))}`;
    });
    one('[data-delete-selected]')?.addEventListener('click', () => {
      const ids = guardSelection();
      if (!ids) return;
      if (!window.confirm(`确定删除选中的 ${ids.length} 张发票及其原始文件吗？此操作无法恢复。`)) return;
      const form = document.createElement('form');
      form.method = 'POST';
      form.action = '/invoices/batch-delete';
      form.hidden = true;
      const csrf = document.createElement('input');
      csrf.type = 'hidden';
      csrf.name = 'csrf_token';
      csrf.value = one('[data-batch-csrf]')?.value ?? '';
      form.append(csrf);
      ids.forEach((id) => {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'invoice_ids';
        input.value = id;
        form.append(input);
      });
      document.body.append(form);
      form.submit();
    });
  }

  const printForm = one('[data-print-form]');
  if (printForm) {
    const checks = all('[data-print-item]', printForm);
    const selectAll = one('[data-print-select-all]', printForm);
    const count = one('[data-print-count]', printForm);
    const pageCount = one('[data-page-count]', printForm);
    const generate = one('[data-generate-print]', printForm);
    const update = () => {
      const selected = checks.filter((item) => item.checked).length;
      const perPage = Number(one('input[name="per_page"]:checked', printForm)?.value || 2);
      count.textContent = selected;
      pageCount.textContent = selected ? Math.ceil(selected / perPage) : 0;
      generate.disabled = selected === 0;
      selectAll.checked = selected === checks.length && checks.length > 0;
      selectAll.indeterminate = selected > 0 && selected < checks.length;
    };
    checks.forEach((item) => item.addEventListener('change', update));
    all('input[name="per_page"]', printForm).forEach((item) => item.addEventListener('change', update));
    selectAll?.addEventListener('change', () => { checks.forEach((item) => { item.checked = selectAll.checked; }); update(); });
    printForm.addEventListener('submit', () => {
      generate.disabled = true;
      generate.textContent = '正在生成 PDF…';
      window.setTimeout(() => { generate.disabled = false; generate.textContent = '生成排版 PDF ↓'; }, 4000);
    });
    update();
  }
})();
