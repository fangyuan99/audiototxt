# 部署到 Vercel 指南

本分支 `vercel` 已配置为可以直接部署到 Vercel 平台。

## 前置条件

1. **Vercel 账号**：在 [vercel.com](https://vercel.com) 上注册账号
2. **GitHub 账号**：项目需要推送到 GitHub
3. **Google Gemini API Key**：获取地址 [ai.google.dev](https://ai.google.dev)

## 快速开始

### 方式一：通过 Vercel Dashboard（推荐）

1. **登录 Vercel**
   - 访问 [vercel.com/login](https://vercel.com/login)
   - 使用 GitHub 账号登录

2. **导入项目**
   - 点击 "Add New..." → "Project"
   - 选择 GitHub 上的 `audiototxt` 仓库
   - 选择分支为 `vercel`

3. **配置环境变量**
   - 在项目设置中找到 "Environment Variables"
   - 添加以下环境变量：

   ```
   GOOGLE_API_KEY     你的 Gemini API Key
   GEMINI_API_KEY     （可选，同上）
   CLEANUP_HOURS      24（可选）
   ```

4. **部署**
   - 点击 "Deploy"
   - 等待部署完成

### 方式二：通过 Vercel CLI

1. **安装 Vercel CLI**
   ```bash
   npm i -g vercel
   ```

2. **登录 Vercel**
   ```bash
   vercel login
   ```

3. **部署项目**
   ```bash
   cd D:\github_project\audiototxt
   git checkout vercel
   vercel --prod
   ```

4. **按照提示配置环境变量**

## 项目结构

```
audiototxt/
├── api/
│   └── index.py                 # Vercel 无服务函数入口
├── fastapi/
│   ├── app.py                   # FastAPI 应用
│   ├── static/                  # 静态文件（CSS、JS）
│   └── templates/               # HTML 模板
├── main.py                      # 核心转写逻辑
├── requirements.txt             # Python 依赖
├── vercel.json                  # Vercel 配置
└── .vercelignore               # Vercel 忽略文件列表
```

## 配置说明

### vercel.json

```json
{
  "version": 2,
  "builds": [
    {
      "src": "api/index.py",
      "use": "@vercel/python"
    }
  ],
  "routes": [
    {
      "src": "/(.*)",
      "dest": "api/index.py"
    }
  ]
}
```

### 环境变量

| 变量名 | 必需 | 说明 |
|--------|------|------|
| GOOGLE_API_KEY | 是 | Google Gemini API Key |
| GEMINI_API_KEY | 否 | 备用 API Key（如设置则优先使用 GOOGLE_API_KEY） |
| CLEANUP_HOURS | 否 | 自动清理过期文件的间隔（小时），默认 24 |
| DATA_DIR | 否 | 数据存储目录，默认 `/tmp/audiototxt_data` |

## 使用限制

### Vercel 无服务环境限制

1. **文件存储**
   - 无服务函数运行时间限制：30 秒（Pro 计划为 60 秒）
   - `/tmp` 目录在函数执行完成后会清空
   - **建议**：对于生产环境，使用外部存储服务（如 AWS S3、Google Cloud Storage）

2. **大文件处理**
   - 请求体大小限制：6MB（Pro 计划为 12MB）
   - 如需处理更大文件，需要使用流式上传或外部存储

3. **并发执行**
   - 免费计划：受并发限制
   - Pro 计划及以上：更高并发

### 推荐改进方案

对于生产环境，建议做以下改进：

1. **使用外部存储**
   ```python
   # 使用 AWS S3 或 Google Cloud Storage
   # 而不是本地 /tmp 目录
   ```

2. **异步任务队列**
   ```python
   # 使用 Redis 或 Vercel KV 存储任务状态
   ```

3. **长时间处理**
   ```python
   # 使用 Vercel Cron Jobs 进行后台任务
   ```

## 部署后测试

部署完成后，Vercel 会为你的项目生成一个 URL，例如：
```
https://audiototxt-xxxxx.vercel.app
```

### 测试 API

1. **健康检查**
   ```bash
   curl https://your-vercel-url.vercel.app/health
   ```
   
   预期响应：
   ```json
   {"status":"ok","version":"vercel"}
   ```

2. **访问 Web UI**
   ```
   https://your-vercel-url.vercel.app/
   ```

3. **提交转写任务**
   ```bash
   curl -X POST https://your-vercel-url.vercel.app/api/transcribe \
     -F "source_type=youtube" \
     -F "youtube_url=https://www.youtube.com/watch?v=xxxxx" \
     -F "api_key=your_gemini_key"
   ```

## 常见问题

### Q: 部署后无法访问 Web UI？

**A:** 检查以下几点：
1. 环境变量 `GOOGLE_API_KEY` 是否已配置
2. 静态文件是否正确打包（check Vercel build log）
3. 查看 Vercel 函数日志进行调试

### Q: 文件上传不成功？

**A:** 可能原因：
1. 文件大小超过 Vercel 限制（6MB 或 12MB）
2. 网络超时（大文件处理超过 30 秒）
3. 磁盘空间不足

解决方案：
- 减小文件大小
- 使用流式上传或分片上传
- 升级到 Vercel Pro 计划获得更高限制

### Q: 如何查看日志？

**A:** 在 Vercel Dashboard 中：
1. 选择项目
2. 点击 "Logs" 标签
3. 查看实时日志或历史日志

### Q: 部署后如何更新代码？

**A:** 有两种方式：

1. **自动部署**（推荐）
   - 在 GitHub 上推送代码到 `vercel` 分支
   - Vercel 会自动检测并重新部署

2. **手动部署**
   - 使用 `vercel --prod` 命令重新部署

## 分支说明

- **master**：主分支，包含完整项目代码
- **vercel**：部署分支，优化用于 Vercel 平台，包含额外配置

## 获得帮助

- Vercel 文档：https://vercel.com/docs
- FastAPI 文档：https://fastapi.tiangolo.com
- 项目 Issues：https://github.com/your-repo/issues

---

**注意**：确保你的 Google Gemini API Key 有效且配额充足，否则转写将失败。

