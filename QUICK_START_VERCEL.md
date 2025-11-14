# 🚀 Vercel 一键部署快速指南

## 前置条件（3 个要点）

✅ GitHub 账号  
✅ Vercel 账号（免费，使用 GitHub 登录）  
✅ Google Gemini API Key（免费获取：[ai.google.dev](https://ai.google.dev)）

## 部署步骤（5 分钟）

### 1️⃣ 访问 Vercel 导入项目

打开 [Vercel 控制面板](https://vercel.com/dashboard)

### 2️⃣ 导入仓库

- 点击 **"Add New..."** → **"Project"**
- 选择 **GitHub** 账户授权
- 搜索并选择 **`audiototxt`** 仓库

### 3️⃣ 选择部署分支

- **Branch**: 选择 **`vercel`**（不是 master）
- 项目名称可保持默认或自定义

### 4️⃣ 配置环境变量

在"Environment Variables"部分添加：

```
Name: GOOGLE_API_KEY
Value: 你在 ai.google.dev 获取的 API Key
Environments: 选择 Production
```

可选添加：

```
Name: CLEANUP_HOURS
Value: 24
（设置多少小时后自动删除过期文件）
```

### 5️⃣ 部署

点击 **"Deploy"** 按钮，等待部署完成（通常 1-2 分钟）

## 部署完成 ✨

部署成功后，你将看到：

```
✓ Deployment Successful
https://audiototxt-xxxxx.vercel.app
```

## 立即测试

1. **访问 Web UI**
   ```
   https://audiototxt-xxxxx.vercel.app
   ```

2. **测试 API**
   ```bash
   curl https://audiototxt-xxxxx.vercel.app/health
   ```

   预期返回：
   ```json
   {"status":"ok","version":"vercel"}
   ```

## 使用方式

### 上传本地音频
1. 访问 Web 页面
2. 选择"本地音频文件"
3. 上传音频文件
4. 点击开始转写
5. 实时查看转写进度

### 转写 YouTube 视频
1. 选择"YouTube 链接"
2. 粘贴 YouTube 链接
3. 点击开始转写

### 支持的格式
- ✅ 本地音频：MP3、M4A、WAV 等
- ✅ YouTube、Bilibili 等视频
- ✅ 视频直链 URL
- ✅ 抖音分享口令/短链

## 常见问题

### ❓ 如何更新代码？

提交代码到 `vercel` 分支后，Vercel 会自动部署：

```bash
git add .
git commit -m "更新描述"
git push origin vercel
# Vercel 自动部署
```

### ❓ 如何查看日志？

在 Vercel Dashboard 中：
- 选择项目
- 点击 **"Deployments"** 标签
- 查看构建日志或运行日志

### ❓ 上传失败？

可能原因：
- 文件超过 6MB（Pro 计划为 12MB）
- 处理超过 30 秒（Pro 计划为 60 秒）
- API Key 配额已用完

**解决方案**：
- 减小文件大小
- 升级到 Vercel Pro 计划
- 增加 Gemini API 配额

### ❓ 如何更换 API Key？

在 Vercel Dashboard 中：
1. 选择项目 → **Settings**
2. 点击 **Environment Variables**
3. 修改 `GOOGLE_API_KEY` 的值
4. 系统自动重新部署

### ❓ 免费计划限制？

| 功能 | 免费 | Pro |
|------|------|-----|
| 部署 | ✅ | ✅ |
| 执行时间 | 30 秒 | 60 秒 |
| 内存 | 1024MB | 3008MB |
| 并发 | 受限 | 更高 |

## 相关文档

| 文档 | 说明 |
|------|------|
| [DEPLOY_TO_VERCEL.md](DEPLOY_TO_VERCEL.md) | 详细部署指南 |
| [BUILD_AND_DEPLOY.md](BUILD_AND_DEPLOY.md) | 本地开发指南 |
| [VERCEL_SETUP_SUMMARY.md](VERCEL_SETUP_SUMMARY.md) | 配置总结 |

## 需要本地测试？

```bash
# 切换到 vercel 分支
git checkout vercel

# 安装依赖
pip install -r requirements.txt

# 本地运行
python fastapi/run.py

# 访问 http://localhost:8000
```

## 获取 API Key

1. 访问 https://ai.google.dev
2. 点击 "Get API Key"
3. 创建或选择现有项目
4. 复制生成的 API Key
5. 在 Vercel 中粘贴

## 💡 提示

- 💰 免费计划足够个人使用
- ⚡ 代码更新会自动部署
- 🔒 API Key 存储在 Vercel 的加密环境变量中
- 📊 可在 Vercel Analytics 中查看使用情况
- 🆘 遇到问题查看 Vercel Logs 或 GitHub Issues

---

**准备好了？** [立即部署到 Vercel →](https://vercel.com/import/git?s=https://github.com/fangyuan99/audiototxt&branch=vercel)

或者手动访问 [Vercel Dashboard](https://vercel.com/dashboard)

