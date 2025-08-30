const sourceSelect = document.getElementById('source_type');
const form = document.getElementById('task-form');
const statusArea = document.getElementById('status-area');
const outputPre = document.getElementById('output');
const startBtn = document.getElementById('start-btn');
const stopBtn = document.getElementById('stop-btn');
const downloadLink = document.getElementById('download-link');
const copyBtn = document.getElementById('copy-btn');

let ws = null;

function renderConditionals() {
  const all = document.querySelectorAll('.conditional');
  const selected = sourceSelect.value;
  all.forEach(el => {
    const forType = el.getAttribute('data-for');
    el.style.display = (forType === selected) ? 'flex' : 'none';
  });
}

sourceSelect.addEventListener('change', renderConditionals);
renderConditionals();

function appendStatus(text) {
  const div = document.createElement('div');
  div.className = 'status-line';
  div.textContent = text;
  statusArea.appendChild(div);
  statusArea.scrollTop = statusArea.scrollHeight;
}

function appendOutput(text) {
  outputPre.textContent += text;
  outputPre.scrollTop = outputPre.scrollHeight;
}

function setRunning(isRunning) {
  startBtn.disabled = isRunning;
  stopBtn.disabled = !isRunning;
}

form.addEventListener('submit', async (e) => {
  e.preventDefault();
  if (ws) {
    try { ws.close(); } catch (e) {}
    ws = null;
  }
  outputPre.textContent = '';
  statusArea.innerHTML = '';
  downloadLink.style.display = 'none';
  downloadLink.removeAttribute('href');

  const formData = new FormData(form);
  const sourceType = formData.get('source_type');

  // Save form values to localStorage (without file)
  persistFormToLocalStorage();

  // Basic validation
  if (sourceType === 'audio') {
    const f = formData.get('file');
    if (!f || !(f instanceof File) || f.size === 0) {
      appendStatus('请选择一个音频文件');
      return;
    }
  } else if (sourceType === 'youtube') {
    if (!formData.get('youtube_url')) {
      appendStatus('请输入 YouTube 链接');
      return;
    }
  } else if (sourceType === 'video_url') {
    if (!formData.get('video_url')) {
      appendStatus('请输入视频直链 URL');
      return;
    }
  } else if (sourceType === 'douyin') {
    if (!formData.get('douyin_text')) {
      appendStatus('请输入抖音分享口令或短链');
      return;
    }
  }

  setRunning(true);
  appendStatus('提交任务...');

  try {
    const resp = await fetch('/api/transcribe', {
      method: 'POST',
      body: formData
    });
    if (!resp.ok) {
      appendStatus('提交任务失败');
      setRunning(false);
      return;
    }
    const data = await resp.json();
    const jobId = data.job_id;
    appendStatus('任务已创建：' + jobId);

    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/' + jobId);
    ws.onopen = () => appendStatus('已连接至进度通道');
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === 'status') {
          appendStatus(m.data);
        } else if (m.type === 'chunk') {
          appendOutput(m.data);
        } else if (m.type === 'error') {
          appendStatus('错误：' + m.data);
          setRunning(false);
        } else if (m.type === 'done') {
          appendStatus('完成');
          setRunning(false);
          if (m.data && m.data.output_filename) {
            downloadLink.href = '/download/' + encodeURIComponent(m.data.output_filename);
            downloadLink.style.display = 'inline-block';
            downloadLink.textContent = '下载：' + m.data.output_filename;
          }
        }
      } catch (e) {
        console.error(e);
      }
    };
    ws.onclose = () => {
      appendStatus('通道已关闭');
      setRunning(false);
    };
    stopBtn.onclick = () => {
      try { ws && ws.close(); } catch (e) {}
      setRunning(false);
    };
  } catch (e) {
    appendStatus('网络或服务错误');
    setRunning(false);
  }
});

// Copy full transcript
copyBtn?.addEventListener('click', async () => {
  try {
    const text = outputPre.textContent || '';
    if (!text) return;
    await navigator.clipboard.writeText(text);
    appendStatus('已复制全文');
  } catch (e) {
    appendStatus('复制失败，请手动选择文本');
  }
});

// Persist & restore form values
const PERSIST_KEYS = ['source_type','api_key','model_name','language_hint','proxy','proxy_http','proxy_https','youtube_url','video_url','douyin_text'];

function persistFormToLocalStorage() {
  const data = {};
  for (const k of PERSIST_KEYS) {
    const el = document.getElementById(k);
    if (!el) continue;
    data[k] = el.value || '';
  }
  try { localStorage.setItem('audiototxt_form', JSON.stringify(data)); } catch (e) {}
}

function restoreFormFromLocalStorage() {
  try {
    const raw = localStorage.getItem('audiototxt_form');
    if (!raw) return;
    const data = JSON.parse(raw);
    for (const k of PERSIST_KEYS) {
      if (data[k] !== undefined) {
        const el = document.getElementById(k);
        if (el) el.value = data[k];
      }
    }
    renderConditionals();
  } catch (e) {}
}

restoreFormFromLocalStorage();


