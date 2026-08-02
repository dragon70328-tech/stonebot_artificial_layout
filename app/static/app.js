// StoneBot Chat Frontend
const chatContainer = document.getElementById('chatContainer');
const msgInput = document.getElementById('msgInput');
const fileInput = document.getElementById('fileInput');
let isWaiting = false;

// ── Helpers ──
function addMessage(role, html) {
  const div = document.createElement('div');
  div.className = `message ${role}`;
  div.innerHTML = `
    <div class="avatar">${role === 'user' ? '👤' : '🤖'}</div>
    <div class="bubble">${html}</div>`;
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
  return div;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'message assistant typing';
  div.id = 'typingIndicator';
  div.innerHTML = '<div class="avatar">🤖</div><div class="bubble"></div>';
  chatContainer.appendChild(div);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function removeTyping() {
  const t = document.getElementById('typingIndicator');
  if (t) t.remove();
}

function setInputEnabled(enabled) {
  msgInput.disabled = !enabled;
  document.querySelector('.btn-send').disabled = !enabled;
  isWaiting = !enabled;
}

function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatReply(text) {
  let html = escapeHtml(text);
  html = html.replace(/\n/g, '<br>');
  // Convert file paths to download links
  html = html.replace(/([^\s]+\.(dxf|json))/gi, (match) => {
    const fname = match.replace(/^.*[\\/]/, '');
    return `<a class="download-link" href="/api/download/${encodeURIComponent(fname)}" download>${match}</a>`;
  });
  return html;
}

// ── Send Message ──
async function sendMessage() {
  const msg = msgInput.value.trim();
  if (!msg || isWaiting) return;

  addMessage('user', `<p>${escapeHtml(msg)}</p>`);
  msgInput.value = '';
  setInputEnabled(false);
  addTyping();

  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg })
    });
    const data = await resp.json();
    removeTyping();

    if (data.error) {
      addMessage('assistant', `<p>❌ ${escapeHtml(data.error)}</p>`);
    } else {
      addMessage('assistant', `<p>${formatReply(data.reply)}</p>`);
      updateDownloadLinks();
    }
  } catch (e) {
    removeTyping();
    addMessage('assistant', `<p>❌ 网络错误，请重试。</p>`);
  }
  setInputEnabled(true);
  msgInput.focus();
}

// ── Upload File ──
async function uploadFile() {
  const file = fileInput.files[0];
  if (!file) return;

  addMessage('user', `<p>📎 上传了 ${escapeHtml(file.name)}</p>`);
  setInputEnabled(false);
  addTyping();

  const formData = new FormData();
  formData.append('file', file);
  formData.append('message', '上传DXF文件');

  try {
    const resp = await fetch('/api/chat', { method: 'POST', body: formData });
    const data = await resp.json();
    removeTyping();

    if (data.error) {
      addMessage('assistant', `<p>❌ ${escapeHtml(data.error)}</p>`);
    } else {
      addMessage('assistant', `<p>${formatReply(data.reply)}</p>`);
      updateDownloadLinks();
    }
  } catch (e) {
    removeTyping();
    addMessage('assistant', `<p>❌ 上传失败，请重试。</p>`);
  }
  setInputEnabled(true);
  fileInput.value = '';
  msgInput.focus();
}

// ── Download Links ──
function updateDownloadLinks() {
  document.querySelectorAll('.download-link').forEach(link => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      const href = link.getAttribute('href');
      window.open(href, '_blank');
    });
  });
}

// ── Reset ──
async function resetChat() {
  if (!confirm('确定要重新开始？对话记录将被清除。')) return;
  await fetch('/api/reset', { method: 'POST' });
  chatContainer.innerHTML = '';
  addMessage('assistant', `<p>对话已重置。欢迎使用人造石排板系统！</p><p>请输入大板尺寸开始，例如：<code>3200 1800 18</code></p>`);
  msgInput.focus();
}

// ── Init ──
msgInput.focus();
updateDownloadLinks();
