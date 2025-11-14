# Vercel 部署配置总结

## ✅ 完成的工作

已成功创建 `vercel` 分支，并添加了完整的 Vercel 部署配置。

### 新增文件

1. **`api/index.py`** - Vercel 无服务函数入口
   - FastAPI 应用适配
   - 完整的任务处理逻辑
   - WebSocket 支持
   - 文件上传和下载
   - 环境变量支持

2. **`vercel.json`** - Vercel 项目配置
   - Python 运行时
   - 最大执行时间：60 秒
   - 内存限制：3008 MB
   - 路由配置

3. **`.vercelignore`** - Vercel 忽略文件列表
   - 排除 `data/` 目录
   - 排除 `__pycache__/`
   - 排除不必要的文件

4. **`DEPLOY_TO_VERCEL.md`** - 部署指南
   - 快速开始步骤
   - 环境变量配置
   - 常见问题解答
   - API 使用说明

5. **`BUILD_AND_DEPLOY.md`** - 本地开发指南
   - 本地测试方法
   - Git 工作流程
   - 命令参考
   - 故障排除

## 🚀 快速开始

### 1. 推送到 Vercel（最简单）

```bash
# 前提：已登录 GitHub 并推送 vercel 分支

# 访问 Vercel
# https://vercel.com/dashboard

# 导入项目 → 选择 audiototxt 仓库
# 分支选择 → vercel
# 环境变量配置：
#   GOOGLE_API_KEY = 你的 API Key
# 点击 Deploy
```

### 2. 本地测试

```bash
# 切换到 vercel 分支
git checkout vercel

# 安装依赖
pip install -r requirements.txt

# 方式 A：使用 FastAPI 开发服务器
python fastapi/run.py
# 访问 http://localhost:8000

# 方式 B：使用 Vercel 本地环境
npm install -g vercel
vercel dev
# 访问 http://localhost:3000
```

## 📋 文件结构

```
audiototxt/
├── api/
│   └── index.py                      # ← Vercel 函数入口
├── fastapi/
│   ├── app.py                        # FastAPI 应用
│   ├── static/                       # CSS、JS 等静态文件
│   └── templates/                    # HTML 模板
├── main.py                           # 核心转写逻辑
├── requirements.txt                  # Python 依赖
│
├── vercel.json                       # ← Vercel 配置
├── .vercelignore                     # ← Vercel 忽略文件
├── DEPLOY_TO_VERCEL.md              # ← 部署指南
└── BUILD_AND_DEPLOY.md              # ← 开发指南
```

## 🔧 关键配置

### vercel.json

```json
{
  "version": 2,
  "name": "AudioToTxt",
  "builds": [{
    "src": "api/index.py",
    "use": "@vercel/python",
    "config": {
      "maxDuration": 60,      // 最大执行时间 60 秒
      "memory": 3008          // 内存 3008 MB
    }
  }],
  "routes": [{
    "src": "/(.*)",
    "dest": "api/index.py"    // 所有请求转到 Python 函数
  }]
}
```

### 环境变量

必需配置：

| 变量名 | 说明 | 获取方式 |
|--------|------|---------|
| `GOOGLE_API_KEY` | Gemini API Key | https://ai.google.dev |

可选配置：

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `CLEANUP_HOURS` | 自动清理过期文件间隔 | 24 |
| `DATA_DIR` | 数据存储目录 | `/tmp/audiototxt_data` |

## ⚠️ 重要提示

### Vercel 无服务环境限制

1. **执行时间限制**
   - 免费计划：30 秒
   - Pro 计划：60 秒（已配置）

2. **内存限制**
   - 默认：1024 MB
   - 已配置：3008 MB

3. **文件存储**
   - `/tmp` 目录在执行完成后清空
   - 仅用于临时文件存储
   - **生产环境建议**：使用外部存储（AWS S3、Google Cloud Storage）

4. **并发处理**
   - 免费计划：有并发限制
   - 建议监控 Vercel Analytics

### 性能优化建议

1. **流式处理**
   - 使用 WebSocket 实时推送进度
   - 避免等待完整结果后再返回

2. **缓存策略**
   - 使用 Redis 或 Vercel KV 存储中间结果
   - 减少重复计算

3. **任务队列**
   - 对于长时间处理的任务
   - 使用后台任务队列（如 Celery）

## 📊 部署后监控

在 Vercel Dashboard 中可以：

1. **查看实时日志**
   - Functions → Logs
   - 查看每个请求的执行情况

2. **监控性能指标**
   - Analytics 标签
   - 查看响应时间、错误率等

3. **管理环境变量**
   - Settings → Environment Variables
   - 随时更新或新增变量

4. **查看部署历史**
   - Deployments 标签
   - 回滚到之前的版本

## 🔄 工作流程

### 开发流程

```
1. 创建本地分支 (可选)
   git checkout -b feature/xxx

2. 进行开发和测试
   # 修改代码
   # 本地测试：python fastapi/run.py 或 vercel dev

3. 提交到 vercel 分支
   git add .
   git commit -m "描述"
   git checkout vercel
   git merge feature/xxx

4. 推送到远程
   git push origin vercel

5. Vercel 自动部署
   # 在 Dashboard 中监控部署
```

### 从 master 同步

```bash
# 定期同步 master 分支的更新
git checkout vercel
git pull origin master
# 解决冲突
git push origin vercel
```

## 🎯 下一步

1. **立即部署**
   ```
   访问 https://vercel.com/dashboard
   导入 audiototxt 仓库的 vercel 分支
   配置 GOOGLE_API_KEY 环境变量
   点击 Deploy
   ```

2. **测试 API**
   - 访问你的 Vercel URL
   - 测试各个转写功能
   - 监控日志检查性能

3. **优化和扩展**
   - 根据实际使用情况调整配置
   - 考虑集成外部存储服务
   - 实现更高级的缓存策略

## 📚 相关文档

- [DEPLOY_TO_VERCEL.md](DEPLOY_TO_VERCEL.md) - 详细部署指南
- [BUILD_AND_DEPLOY.md](BUILD_AND_DEPLOY.md) - 开发测试指南
- [Vercel 官方文档](https://vercel.com/docs)
- [FastAPI 官方文档](https://fastapi.tiangolo.com)

## 🆘 遇到问题？

1. **检查部署日志**
   - Vercel Dashboard → Deployments → 查看构建和运行时日志

2. **查看函数日志**
   - Vercel Dashboard → Functions → Logs

3. **测试 API 连接**
   ```bash
   curl https://your-vercel-url.vercel.app/health
   ```

4. **验证环境变量**
   - Vercel Dashboard → Settings → Environment Variables
   - 确认 GOOGLE_API_KEY 已设置

---

**分支信息**：`vercel` 分支已推送至 GitHub，可随时部署到 Vercel

