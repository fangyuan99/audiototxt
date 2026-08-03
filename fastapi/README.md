# AudioToTxt Web UI

Web UI 默认关闭，并与 Telegram 共用全局 Gemini/Vertex、模型、语言与 Prompt 配置。页面不再接收或持久化 API Key、Vertex JSON 或代理配置。

## 启用

在项目根目录 `.env` 中显式配置：

```dotenv
WEB_ENABLED=true
WEB_ACCESS_KEY=replace-with-a-strong-independent-key
WEB_DATA_DIR=./data/web
```

如果 `WEB_ENABLED` 未开启，服务只暴露健康检查；如果开启但 `WEB_ACCESS_KEY` 为空，服务会失败关闭，所有操作端点均不可用。

启动：

```bash
python fastapi/run.py
python fastapi/run.py --port 8333
```

浏览器打开 `http://127.0.0.1:8000/`，输入独立的 Web 访问密钥。登录成功后使用 HttpOnly、SameSite=Strict Cookie 保持会话。

## 功能与安全边界

- 支持音频上传、YouTube、视频/音频直链和抖音分享内容。
- `/api/transcribe`、任务状态/取消、WebSocket、文件列表、下载和清理接口均要求同一 Web 会话。
- 上传文件在请求结束前按块复制到服务端拥有的临时文件，并执行大小限制和文件名净化。
- 直链仅允许 HTTP/HTTPS 公网地址；每次重定向都会重新校验，并受字节、连接、读取与任务总超时限制。
- 任务结束后 WebSocket 主动关闭；终态任务和结果会定期清理。
- 浏览器 localStorage 只保存主题，不保存凭据或任务表单。

## Telegram 嵌入模式

推荐独立运行：

```bash
python telegram_bot.py
```

确需由本地 Web 进程同时启动 Telegram polling 时，必须同时设置：

```dotenv
WEB_ENABLED=true
TELEGRAM_EMBEDDED_ENABLED=true
```

Vercel 入口始终禁用嵌入式 Telegram polling。

## 主要端点

- `GET /health`：返回 `enabled`、`disabled` 或 `misconfigured` 状态
- `GET|POST /login`：Web 访问密钥登录
- `POST /api/transcribe`：创建任务
- `GET /api/jobs/{job_id}`：任务状态
- `POST /api/jobs/{job_id}/cancel`：取消任务
- `WS /ws/{job_id}`：状态与最终结果事件
- `GET /download/{filename}`：下载结果
- `GET /api/files`、`POST /api/cleanup`：受保护的结果管理

请遵守来源网站服务条款与当地法律。视频抽音需要系统安装 `ffmpeg`。
