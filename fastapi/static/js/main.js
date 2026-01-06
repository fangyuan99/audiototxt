const sourceSelect = document.getElementById("source_type");
const form = document.getElementById("task-form");
const statusArea = document.getElementById("status-area");
const outputPre = document.getElementById("output");
const startBtn = document.getElementById("start-btn");
const stopBtn = document.getElementById("stop-btn");
const downloadLink = document.getElementById("download-link");
const copyBtn = document.getElementById("copy-btn");
const themeToggleBtn = document.getElementById("theme-toggle");

let ws = null;
let timerIntervalId = null;
let taskStartTimestampMs = null;
const originalStartBtnText = startBtn ? startBtn.textContent : "开始转写";

function formatElapsed(ms) {
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const two = (n) => n.toString().padStart(2, "0");
  return hours > 0
    ? `${two(hours)}:${two(minutes)}:${two(seconds)}`
    : `${two(minutes)}:${two(seconds)}`;
}

function renderConditionals() {
  const all = document.querySelectorAll(".conditional");
  const selected = sourceSelect.value;
  all.forEach((el) => {
    const forType = el.getAttribute("data-for");
    // 默认隐藏，匹配时显示
    el.style.display = forType === selected ? "" : "none";
  });
}

// 初始化时隐藏所有 conditional 元素
document.querySelectorAll(".conditional").forEach((el) => {
  el.style.display = "none";
});

sourceSelect.addEventListener("change", renderConditionals);
renderConditionals();

function getNowTimeString() {
  const d = new Date();
  const two = (n) => n.toString().padStart(2, "0");
  return `${two(d.getHours())}:${two(d.getMinutes())}:${two(d.getSeconds())}`;
}

function appendStatus(text) {
  const p = document.createElement("p");
  p.textContent = `[${getNowTimeString()}] ${text}`;
  p.style.margin = "0.25rem 0";
  statusArea.appendChild(p);
  statusArea.scrollTop = statusArea.scrollHeight;
}

function appendOutput(text) {
  // 段落空一行：将双换行规范化为两个换行；
  // 若是单换行则保持；在显示时文本区域已使用 pre-wrap
  // 若上一次末尾非空且当前片段以换行开头，保持原样
  // 仅在出现段落断句（\n\n 或 \r\n\r\n）时确保两个换行
  const normalized = text.replace(/\r\n/g, "\n").replace(/\n{3,}/g, "\n\n");
  outputPre.value += normalized;
  outputPre.scrollTop = outputPre.scrollHeight;
}

function setRunning(isRunning) {
  if (!startBtn || !stopBtn) return;
  startBtn.disabled = isRunning;
  stopBtn.disabled = !isRunning;

  if (isRunning) {
    taskStartTimestampMs = Date.now();
    if (timerIntervalId) {
      try {
        clearInterval(timerIntervalId);
      } catch (_) {}
    }
    startBtn.textContent = "进行中 00:00";
    timerIntervalId = setInterval(() => {
      const elapsed = Date.now() - taskStartTimestampMs;
      startBtn.textContent = "进行中 " + formatElapsed(elapsed);
    }, 1000);
  } else {
    if (timerIntervalId) {
      try {
        clearInterval(timerIntervalId);
      } catch (_) {}
      timerIntervalId = null;
    }
    taskStartTimestampMs = null;
    startBtn.textContent = originalStartBtnText;
  }
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (ws) {
    try {
      ws.close();
    } catch (e) {}
    ws = null;
  }
  outputPre.value = "";
  statusArea.innerHTML = "";
  downloadLink.style.display = "none";
  downloadLink.removeAttribute("href");

  const formData = new FormData(form);
  const sourceType = formData.get("source_type");

  // Save form values to localStorage (without file)
  persistFormToLocalStorage();

  // Basic validation
  if (sourceType === "audio") {
    const f = formData.get("file");
    if (!f || !(f instanceof File) || f.size === 0) {
      appendStatus("请选择一个音频文件");
      return;
    }
  } else if (sourceType === "youtube") {
    if (!formData.get("youtube_url")) {
      appendStatus("请输入 YouTube 链接");
      return;
    }
  } else if (sourceType === "video_url") {
    if (!formData.get("video_url")) {
      appendStatus("请输入视频直链 URL");
      return;
    }
  } else if (sourceType === "douyin") {
    if (!formData.get("douyin_text")) {
      appendStatus("请输入抖音分享口令或短链");
      return;
    }
  }

  setRunning(true);
  appendStatus("提交任务...");

  try {
    const resp = await fetch("/api/transcribe", {
      method: "POST",
      body: formData,
    });
    if (!resp.ok) {
      appendStatus("提交任务失败");
      setRunning(false);
      return;
    }
    const data = await resp.json();
    const jobId = data.job_id;
    appendStatus("任务已创建：" + jobId);

    ws = new WebSocket(
      (location.protocol === "https:" ? "wss://" : "ws://") +
        location.host +
        "/ws/" +
        jobId
    );
    ws.onopen = () => appendStatus("已连接至进度通道");
    ws.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        if (m.type === "status") {
          appendStatus(m.data);
        } else if (m.type === "chunk") {
          appendOutput(m.data);
        } else if (m.type === "error") {
          appendStatus("错误：" + m.data);
          setRunning(false);
        } else if (m.type === "done") {
          // 在清除计时器前计算总用时
          const elapsedMs = taskStartTimestampMs
            ? Date.now() - taskStartTimestampMs
            : 0;
          appendStatus("完成");
          if (elapsedMs > 0) {
            appendStatus("总用时：" + formatElapsed(elapsedMs));
          }
          setRunning(false);
          if (m.data && m.data.output_filename) {
            downloadLink.href =
              "/download/" + encodeURIComponent(m.data.output_filename);
            downloadLink.style.display = "inline-block";
            downloadLink.textContent = "下载：" + m.data.output_filename;
          }
        }
      } catch (e) {
        console.error(e);
      }
    };
    ws.onclose = () => {
      appendStatus("通道已关闭");
      setRunning(false);
    };
    stopBtn.onclick = () => {
      try {
        ws && ws.close();
      } catch (e) {}
      setRunning(false);
    };
  } catch (e) {
    appendStatus("网络或服务错误");
    setRunning(false);
  }
});

// Copy full transcript
copyBtn?.addEventListener("click", async () => {
  try {
    const text = outputPre.value || "";
    if (!text) return;
    await navigator.clipboard.writeText(text);
    appendStatus("已复制全文");
  } catch (e) {
    appendStatus("复制失败，请手动选择文本");
  }
});

// Persist & restore form values
const PERSIST_KEYS = [
  "source_type",
  "api_key",
  "model_name",
  "language_hint",
  "proxy",
  "proxy_http",
  "proxy_https",
  "youtube_url",
  "video_url",
  "douyin_text",
];

function persistFormToLocalStorage() {
  const data = {};
  for (const k of PERSIST_KEYS) {
    const el = document.getElementById(k);
    if (!el) continue;
    data[k] = el.value || "";
  }
  try {
    localStorage.setItem("audiototxt_form", JSON.stringify(data));
  } catch (e) {}
}

function restoreFormFromLocalStorage() {
  try {
    const raw = localStorage.getItem("audiototxt_form");
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

// Paste button functionality
const pasteYoutubeBtn = document.getElementById("paste-youtube");
const pasteVideoBtn = document.getElementById("paste-video");
const pasteDouyinBtn = document.getElementById("paste-douyin");

const setupPasteButton = (btn, inputId) => {
  if (!btn) return;
  btn.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      const input = document.getElementById(inputId);
      if (input && text) {
        input.value = text.trim();
        appendStatus(`已粘贴链接到${input.placeholder || "输入框"}`);
      }
    } catch (e) {
      appendStatus("粘贴失败，请手动输入或检查浏览器权限");
    }
  });
};

setupPasteButton(pasteYoutubeBtn, "youtube_url");
setupPasteButton(pasteVideoBtn, "video_url");
setupPasteButton(pasteDouyinBtn, "douyin_text");

// 主题切换与持久化
const THEME_KEY = "audiototxt_theme";
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light") {
    root.setAttribute("data-theme", "light");
    themeToggleBtn && (themeToggleBtn.textContent = "🌙 深色主题");
  } else {
    root.setAttribute("data-theme", "dark");
    themeToggleBtn && (themeToggleBtn.textContent = "☀️ 浅色主题");
  }
}
function initTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    // Default to light theme for better readability
    applyTheme(saved === "dark" ? "dark" : "light");
  } catch (_) {
    applyTheme("light");
  }
}
themeToggleBtn?.addEventListener("click", () => {
  const isLight =
    document.documentElement.getAttribute("data-theme") === "light";
  const next = isLight ? "dark" : "light";
  applyTheme(next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (_) {}
});
initTheme();
