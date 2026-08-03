const form = document.getElementById("task-form");
const sourceType = document.getElementById("source_type");
const statusArea = document.getElementById("status-area");
const output = document.getElementById("output");
const startBtn = document.getElementById("start-btn");
const stopBtn = document.getElementById("stop-btn");
const copyBtn = document.getElementById("copy-btn");
const downloadLink = document.getElementById("download-link");
const themeToggle = document.getElementById("theme-toggle");

let socket = null;
let activeJobId = null;

function showStatus(text) {
  const line = document.createElement("p");
  line.textContent = text;
  statusArea.replaceChildren(line);
}

function setRunning(running) {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
}

function renderSource() {
  document.querySelectorAll(".conditional").forEach((element) => {
    element.style.display = element.dataset.for === sourceType.value ? "block" : "none";
  });
}

function requireLogin(response) {
  if (response.status === 401) {
    location.href = "/login";
    return true;
  }
  return false;
}

sourceType.addEventListener("change", renderSource);
renderSource();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (socket) socket.close();
  output.value = "";
  downloadLink.style.display = "none";
  setRunning(true);
  showStatus("正在提交任务……");

  try {
    const response = await fetch("/api/transcribe", {
      method: "POST",
      body: new FormData(form),
    });
    if (requireLogin(response)) return;
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "提交失败");
    }
    const payload = await response.json();
    activeJobId = payload.job_id;
    showStatus(`任务 ${activeJobId.slice(0, 8)} 已创建`);
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    socket = new WebSocket(`${protocol}//${location.host}/ws/${activeJobId}`);
    socket.onmessage = (message) => {
      const eventPayload = JSON.parse(message.data);
      if (eventPayload.type === "status") {
        showStatus(eventPayload.data);
      } else if (eventPayload.type === "chunk") {
        output.value = eventPayload.data || "";
      } else if (eventPayload.type === "done") {
        showStatus("转写完成");
        setRunning(false);
        const filename = eventPayload.data?.output_filename;
        if (filename) {
          downloadLink.href = `/download/${encodeURIComponent(filename)}`;
          downloadLink.style.display = "inline-block";
        }
      } else if (eventPayload.type === "error") {
        showStatus(eventPayload.data || "任务失败");
        setRunning(false);
      }
    };
    socket.onclose = () => setRunning(false);
  } catch (error) {
    showStatus(error.message || "网络错误");
    setRunning(false);
  }
});

stopBtn.addEventListener("click", async () => {
  if (!activeJobId) return;
  const response = await fetch(`/api/jobs/${activeJobId}/cancel`, { method: "POST" });
  if (requireLogin(response)) return;
  showStatus("正在取消任务……");
  if (socket) socket.close();
  setRunning(false);
});

copyBtn.addEventListener("click", async () => {
  if (!output.value) return;
  try {
    await navigator.clipboard.writeText(output.value);
    showStatus("已复制全文");
  } catch (_) {
    showStatus("复制失败，请手动选择文本");
  }
});

function bindPaste(buttonId, inputId) {
  document.getElementById(buttonId)?.addEventListener("click", async () => {
    try {
      document.getElementById(inputId).value = (await navigator.clipboard.readText()).trim();
    } catch (_) {
      showStatus("无法读取剪贴板，请手动粘贴");
    }
  });
}

bindPaste("paste-youtube", "youtube_url");
bindPaste("paste-video", "video_url");
bindPaste("paste-douyin", "douyin_text");

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  themeToggle.textContent = theme === "dark" ? "☀️" : "🌙";
}

const savedTheme = localStorage.getItem("audiototxt_theme") || "light";
applyTheme(savedTheme);
themeToggle.addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  localStorage.setItem("audiototxt_theme", next);
  applyTheme(next);
});
