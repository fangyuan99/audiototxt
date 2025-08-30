## AudioToTxt（FastAPI 可视化分支）

此分支为项目提供基于 FastAPI 的可视化网页，支持上传音频或通过各类链接获取音频，并在网页上实时显示处理进度与转写内容。

### 功能
- 可视化页面操作（表单选择来源，一键开始）
- 实时进度同步（WebSocket 推送：下载进度/处理状态/转写中）
- 流式转写展示（边生成边追加到页面）
- 一键复制全文（转写区域“复制全文”按钮）
- 结果下载（转写完成后提供 `.txt` 下载链接）
- 表单配置持久化（localStorage 自动保存与恢复）
- 支持来源：本地文件、YouTube 链接、视频直链、抖音分享口令/短链（Tiksave）

### 运行
1) 安装依赖（项目根目录）：
```bash
python -m pip install -r requirements.txt
```

2) 启动服务（项目根目录）：
```bash
python fastapi/run.py
```

3) 打开浏览器访问：`http://127.0.0.1:8000/`

### API Key 与代理
- API Key：
  - 页面表单可直接填写；或使用环境变量 `GOOGLE_API_KEY`（或 `GEMINI_API_KEY`）。
- 代理：
  - 默认读取系统环境变量 `HTTP_PROXY/HTTPS_PROXY`；
  - 页面表单支持覆盖：`代理（统一）/HTTP 代理/HTTPS 代理`。

### 端点
- `GET /`：主页（可视化页面）
- `POST /api/transcribe`：提交转写任务（表单）
- `WS /ws/{job_id}`：任务进度与分片文本实时推送（WebSocket）
- `GET /download/{filename}`：下载转写结果（仅限 `./data` 目录内文件）
- `GET /health`：健康检查

### 依赖说明
- 必需：`fastapi`、`uvicorn`、`python-multipart`、`jinja2`
- 已与 `pydantic`、`starlette` 版本匹配（见根目录 `requirements.txt`）
- 建议安装：`ffmpeg`（用于视频直链抽取音频）
- 抖音路径依赖第三方 `tiksave.io`，其可用性受外部网络与站点变化影响

### 使用提示
- 本地音频：选择“本地音频文件”并上传；
- YouTube：粘贴链接；
- 视频直链：粘贴视频 URL（需 `ffmpeg` 抽音）；
- 抖音：粘贴分享口令或短链，系统会解析 MP3 直链后下载；
- 语言提示：填写 `zh/en/ja` 等，辅助模型更稳地识别语种；
- 模型：默认 `gemini-2.5-flash`，可在页面调整。

### 常见问题
- 端口占用：修改 `fastapi/run.py` 中端口，或关闭占用进程；
- 无法导入依赖：再次执行 `python -m pip install -r requirements.txt`；
- `ffmpeg` 未安装：视频直链将无法抽音，请安装并加入 PATH；
- 无进度/无分片：确认已重启服务，且网络/代理可访问外部站点与 Gemini；
- 抖音失败：可能为 Tiksave 接口变化或网络受限，请更换网络或来源方式。

### 安全与边界
- 下载接口仅允许 `./data` 目录下的文件；
- 请遵守各网站服务条款与当地法律，合理合法使用本工具。

