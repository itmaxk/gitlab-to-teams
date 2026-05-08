function addEmail() {
  const list = document.getElementById('email-list');
  const row = document.createElement('div');
  row.className = 'flex gap-2 email-row';
  row.innerHTML = `
    <input type="text" name="emails" value=""
      class="flex-1 bg-slate-800 border border-slate-600 rounded-lg px-3 py-2 text-white text-sm focus:border-blue-500 focus:outline-none"
      placeholder="user@example.com">
    <button type="button" onclick="this.parentElement.remove()"
      class="text-red-400 hover:text-red-300 px-2 cursor-pointer">✕</button>
  `;
  list.appendChild(row);
}

function loadGlobalExcludes() {
  const textarea = document.getElementById('global-title-excludes');
  const saveBtn = document.getElementById('save-global-btn');
  const status = document.getElementById('global-excludes-status');
  if (!textarea) return;

  fetch('/api/rules/global-title-excludes')
    .then(r => r.json())
    .then(data => {
      textarea.value = (data.patterns || []).join('\n');
      textarea.dataset.original = textarea.value;
      saveBtn.disabled = true;
      saveBtn.classList.remove('bg-blue-600', 'hover:bg-blue-700');
      saveBtn.classList.add('bg-slate-600', 'cursor-not-allowed');
    })
    .catch(() => {
      status.className = 'text-xs text-red-400 mt-1';
      status.textContent = '✕ Ошибка загрузки';
    });

  textarea.addEventListener('input', () => {
    if (textarea.value !== textarea.dataset.original) {
      saveBtn.disabled = false;
      saveBtn.classList.remove('bg-slate-600', 'cursor-not-allowed');
      saveBtn.classList.add('bg-blue-600', 'hover:bg-blue-700');
    } else {
      saveBtn.disabled = true;
      saveBtn.classList.remove('bg-blue-600', 'hover:bg-blue-700');
      saveBtn.classList.add('bg-slate-600', 'cursor-not-allowed');
    }
  });
}

function saveGlobalExcludes() {
  const textarea = document.getElementById('global-title-excludes');
  const saveBtn = document.getElementById('save-global-btn');
  const status = document.getElementById('global-excludes-status');
  const patterns = textarea.value.split('\n').map(s => s.trim()).filter(Boolean);

  saveBtn.textContent = '...';
  saveBtn.disabled = true;
  fetch('/api/rules/global-title-excludes', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ patterns }),
  })
    .then(r => {
      if (!r.ok) throw new Error(r.statusText);
      return r.json();
    })
    .then(data => {
      textarea.value = (data.patterns || []).join('\n');
      textarea.dataset.original = textarea.value;
      saveBtn.textContent = 'Сохранить';
      saveBtn.disabled = true;
      saveBtn.classList.remove('bg-blue-600', 'hover:bg-blue-700');
      saveBtn.classList.add('bg-slate-600', 'cursor-not-allowed');
      status.className = 'text-xs text-green-400 mt-1';
      status.textContent = '✓ Сохранено';
      setTimeout(() => { status.textContent = ''; }, 3000);
    })
    .catch(() => {
      saveBtn.textContent = 'Сохранить';
      saveBtn.disabled = false;
      status.className = 'text-xs text-red-400 mt-1';
      status.textContent = '✕ Ошибка сохранения';
    });
}

document.addEventListener('DOMContentLoaded', loadGlobalExcludes);

async function toggleRule(id) {
  const resp = await fetch(`/api/rules/${id}/toggle`, { method: 'PATCH' });
  if (resp.ok) {
    const data = await resp.json();
    const btn = document.getElementById(`toggle-${id}`);
    btn.textContent = data.enabled ? 'Вкл' : 'Выкл';
    btn.className = btn.className.replace(
      /bg-\S+ text-\S+ border-\S+/g, ''
    ).trim();
    if (data.enabled) {
      btn.classList.add('bg-green-900/30', 'text-green-400', 'border-green-800');
    } else {
      btn.classList.add('bg-slate-900', 'text-slate-500', 'border-slate-600');
    }
  }
}

async function deleteRule(id) {
  if (!confirm('Удалить правило?')) return;
  const resp = await fetch(`/api/rules/${id}`, { method: 'DELETE' });
  if (resp.ok) {
    const el = document.getElementById(`rule-${id}`);
    if (el) el.remove();
  }
}

async function copyRule(id) {
  const resp = await fetch(`/api/rules/${id}/copy`, { method: 'POST' });
  if (resp.ok) {
    location.reload();
  } else {
    const data = await resp.json();
    alert(data.detail || 'Ошибка копирования');
  }
}

async function resendNotification(logId, btn) {
  if (!confirm('Повторно отправить уведомление?')) return;
  btn.textContent = '...';
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/rules/logs/${logId}/resend`, { method: 'POST' });
    if (resp.ok) {
      btn.textContent = 'Отправлено';
      setTimeout(() => location.reload(), 1500);
    } else {
      const data = await resp.json();
      btn.textContent = 'Ошибка';
      alert(data.detail || 'Ошибка отправки');
      setTimeout(() => { btn.textContent = 'Повторить'; btn.disabled = false; }, 2000);
    }
  } catch (e) {
    btn.textContent = 'Ошибка';
    setTimeout(() => { btn.textContent = 'Повторить'; btn.disabled = false; }, 2000);
  }
}

async function testRule(id) {
  const btn = event.target;
  btn.textContent = '...';
  btn.disabled = true;
  try {
    const resp = await fetch(`/api/rules/${id}/test`, { method: 'POST' });
    if (resp.ok) {
      const data = await resp.json();
      btn.textContent = data.status === 'checked' ? '✓ Проверено' : '✓ Отправлено';
      setTimeout(() => { btn.textContent = 'Тест'; btn.disabled = false; }, 2000);
    } else {
      const data = await resp.json();
      btn.textContent = 'Ошибка';
      alert(data.detail || 'Ошибка отправки');
      setTimeout(() => { btn.textContent = 'Тест'; btn.disabled = false; }, 2000);
    }
  } catch (e) {
    btn.textContent = 'Ошибка';
    setTimeout(() => { btn.textContent = 'Тест'; btn.disabled = false; }, 2000);
  }
}
