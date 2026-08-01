/** 人造石排板系统 — 前端交互 */

const chatMsgs = document.getElementById('chat-messages');
const msgInput = document.getElementById('msg-input');
const btnSend = document.getElementById('btn-send');
const btnNest = document.getElementById('btn-nest');
const btnUpload = document.getElementById('btn-upload');
const fileInput = document.getElementById('file-input');
const btnReset = document.getElementById('btn-reset');
const uploadHint = document.getElementById('upload-hint');

let waiting = false;

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

function addBubble(role, text) {
  const div = document.createElement('div');
  div.className = 'msg ' + role;
  const b = document.createElement('div');
  b.className = 'bubble';
  b.textContent = text;
  div.appendChild(b);
  chatMsgs.appendChild(div);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
}

function addTyping() {
  const div = document.createElement('div');
  div.className = 'msg assistant typing-msg';
  const b = document.createElement('div');
  b.className = 'bubble typing';
  b.textContent = ' ';
  div.appendChild(b);
  chatMsgs.appendChild(div);
  chatMsgs.scrollTop = chatMsgs.scrollHeight;
  return div;
}

function enableInput(v) {
  btnSend.disabled = !v;
  btnNest.disabled = !v;
  btnUpload.disabled = !v;
  msgInput.disabled = !v;
}

async function sendMsg(msg) {
  if (waiting) return;
  waiting = true;
  enableInput(false);
  addBubble('user', msg);
  const typing = addTyping();

  try {
    const r = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg }),
    });
    typing.remove();
    if (!r.ok) throw new Error(r.statusText);
    const data = await r.json();
    addBubble('assistant', data.reply || '(no response)');
  } catch (e) {
    typing.remove();
    addBubble('system', '错误: ' + e.message);
  }
  waiting = false;
  enableInput(true);
  msgInput.focus();
}

// ---------------------------------------------------------------------------
// events
// ---------------------------------------------------------------------------

msgInput.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendMsg(msgInput.value);
    msgInput.value = '';
  }
});

btnSend.addEventListener('click', () => {
  const v = msgInput.value.trim();
  if (v) { sendMsg(v); msgInput.value = ''; }
});

btnNest.addEventListener('click', () => {
  sendMsg('排板');
});

btnUpload.addEventListener('click', () => {
  fileInput.click();
});

fileInput.addEventListener('change', async () => {
  const file = fileInput.files[0];
  if (!file) return;
  uploadHint.textContent = '正在上传 ' + file.name + ' ...';
  if (waiting) return;
  waiting = true;
  enableInput(false);
  addBubble('user', '[上传了 ' + file.name + ']');
  const typing = addTyping();

  const fd = new FormData();
  fd.append('file', file);
  fd.append('message', '上传');

  try {
    const r = await fetch('/api/chat', { method: 'POST', body: fd });
    typing.remove();
    const data = await r.json();
    addBubble('assistant', data.reply || '(no response)');
    uploadHint.textContent = '';
  } catch (e) {
    typing.remove();
    addBubble('system', '上传失败: ' + e.message);
    uploadHint.textContent = '';
  }
  waiting = false;
  enableInput(true);
  fileInput.value = '';
});

btnReset.addEventListener('click', async () => {
  await fetch('/api/reset', { method: 'POST' });
  chatMsgs.innerHTML = `<div class="msg system"><div class="bubble">
    已重置。点击 <b>排板</b> 按钮开始。</div></div>`;
});
