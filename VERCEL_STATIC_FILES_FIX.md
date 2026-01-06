# Vercel 静态文件 404 问题修复

## 问题描述

部署后出现 404 错误：
```
请求网址: https://audiototxt-five.vercel.app/static/js/main.js
状态代码: 404 Not Found
```

## 根本原因

`vercel.json` 中的静态文件路由配置有问题。Vercel 的无服务函数不能直接从文件系统服务静态文件，所有请求都需要通过 FastAPI 应用处理。

**错误配置**：
```json
"routes": [
  {
    "src": "/static/(.*)",
    "dest": "/fastapi/static/$1"
  },
  {
    "src": "/(.*)",
    "dest": "api/index.py"
  }
]
```

## 解决方案

删除静态文件的直接路由，让所有请求都通过 FastAPI 应用处理。FastAPI 已经通过 `app.mount("/static", StaticFiles(...))` 正确配置了静态文件服务。

**正确配置**：
```json
"routes": [
  {
    "src": "/(.*)",
    "dest": "api/index.py"
  }
]
```

## 已应用的修复

✅ 已更新 `vercel.json`（commit: 6b7dcc8）
✅ 已推送到 GitHub `vercel` 分支
✅ FastAPI 应用已正确配置 StaticFiles 挂载

## 重新部署

修复已推送到 `vercel` 分支，现在需要重新部署：

### 方式一：自动重新部署（推荐）
- Vercel 会检测到代码变更
- 自动拉取最新代码并重新构建
- 通常在 1-2 分钟内完成

### 方式二：手动重新部署
1. 访问 [Vercel Dashboard](https://vercel.com/dashboard)
2. 选择你的 `audiototxt` 项目
3. 点击 "Deployments" 标签
4. 找到最新的部署
5. 点击三个点菜单 "..." → "Redeploy"

### 方式三：清理缓存重新部署
1. 访问项目设置 → Settings
2. 点击 "Deployments" 标签
3. 点击 "Clear Caches"
4. 返回项目主页，点击 "Redeploy"

## 验证修复

部署完成后，测试以下 URL：

```bash
# 测试 main.js
curl https://audiototxt-five.vercel.app/static/js/main.js

# 测试 style.css
curl https://audiototxt-five.vercel.app/static/css/style.css

# 测试主页
curl https://audiototxt-five.vercel.app/

# 测试 health endpoint
curl https://audiototxt-five.vercel.app/health
```

## 技术说明

### FastAPI StaticFiles 配置

在 `api/index.py` 中已正确配置：

```python
static_dir = os.path.join(ROOT_DIR, "fastapi", "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
```

这个配置会自动处理所有 `/static/*` 的请求。

### Vercel 路由处理流程

```
请求来临 → Vercel 路由 (vercel.json)
         ↓
    所有请求转向 api/index.py
         ↓
    FastAPI 应用处理
         ↓
    FastAPI 路由匹配 → 静态文件中间件 → 返回文件
                    ↓
                  其他路由 → 处理 API 请求
```

## 常见问题

### Q: 我看不到 CSS 样式或 JS 功能？

**A:** 可能部署还未完成，等待 5-10 分钟后重新访问。

### Q: 重新部署后还是 404？

**A:** 尝试以下步骤：
1. 清除浏览器缓存（Ctrl+Shift+Del）
2. 访问 https://audiototxt-five.vercel.app/?t=新时间戳
3. 检查 Vercel 构建日志是否有错误

### Q: 如何查看构建日志？

**A:** 在 Vercel Dashboard 中：
1. 选择项目 → Deployments
2. 点击最新的部署
3. 查看 "Build Logs" 标签

### Q: 本地测试是否正常？

**A:** 本地测试应该工作正常：
```bash
# 切换到 vercel 分支
git checkout vercel

# 安装依赖
pip install -r requirements.txt

# 运行本地服务器
python fastapi/run.py

# 访问
http://localhost:8000/static/js/main.js
```

## 相关文件

- `vercel.json` - Vercel 配置文件（已修复）
- `api/index.py` - FastAPI 应用入口（StaticFiles 配置正确）
- `fastapi/static/` - 静态文件目录

## 后续注意事项

- ✅ 所有请求现在由 FastAPI 应用统一处理
- ✅ 静态文件通过 FastAPI 的 StaticFiles 中间件服务
- ✅ CORS 已配置，支持跨域请求
- ✅ 模板文件也由 FastAPI 正确处理

---

**修复完成！** 静态文件 404 错误应该已解决。如果问题仍然存在，请检查：
1. 部署是否已完成
2. 浏览器缓存是否已清除
3. Vercel 构建日志中是否有错误


