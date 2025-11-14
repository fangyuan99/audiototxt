# 本地测试和部署指南

## 在 Vercel 分支中工作

### 1. 切换到 Vercel 分支

```bash
git checkout vercel
```

### 2. 本地开发和测试

#### 方式一：使用 FastAPI 的开发服务器

```bash
# 安装依赖
pip install -r requirements.txt

# 运行 FastAPI 服务
python fastapi/run.py
```

访问：`http://localhost:8000`

#### 方式二：使用 Vercel Functions 本地测试

```bash
# 安装 Vercel CLI
npm i -g vercel

# 在项目目录中
cd D:\github_project\audiototxt

# 本地运行（模拟 Vercel 环境）
vercel dev
```

访问：`http://localhost:3000`

## 提交变更

### 1. 检查变更

```bash
git status
```

### 2. 添加文件

```bash
# 添加所有文件
git add .

# 或仅添加特定文件
git add api/index.py vercel.json
```

### 3. 提交

```bash
git commit -m "chore: update Vercel deployment configuration"
```

### 4. 推送到远程

```bash
# 推送 vercel 分支
git push origin vercel
```

## 在 Vercel 上部署

### 方式一：自动部署（推荐）

1. 推送代码到 `vercel` 分支
2. Vercel 检测到代码变更后自动部署
3. 在 Vercel Dashboard 中查看部署状态

### 方式二：手动部署

```bash
# 使用 Vercel CLI
vercel --prod

# 按照提示完成部署
```

## 环境变量配置

### 本地开发

创建 `.env.local` 文件（不要提交到 Git）：

```bash
GOOGLE_API_KEY=your_api_key_here
CLEANUP_HOURS=24
```

### Vercel 平台

在 Vercel Dashboard 中设置：

1. 选择项目 → Settings → Environment Variables
2. 添加环境变量：
   - Name: `GOOGLE_API_KEY`
   - Value: 你的 API Key
   - Environments: 选择需要的环境（Production, Preview, Development）
3. 点击 Save

## 分支管理

### 主要分支

```
┌─ master (主分支，稳定版本)
└─ vercel (Vercel 部署分支)
```

### 工作流程

```
1. 在本地 vercel 分支开发
   git checkout vercel
   git pull origin vercel

2. 进行修改和测试
   # 修改代码
   # 本地测试

3. 提交变更
   git add .
   git commit -m "描述"

4. 推送到远程
   git push origin vercel

5. Vercel 自动部署
   # 在 Dashboard 中监控部署
```

## 常见命令

### Git 命令

```bash
# 查看当前分支
git branch

# 列出所有分支
git branch -a

# 切换分支
git checkout <branch-name>

# 创建新分支
git checkout -b <branch-name>

# 查看提交历史
git log --oneline

# 查看未提交的变更
git status

# 查看具体变更
git diff

# 撤销未提交的变更
git checkout .

# 删除本地分支
git branch -d <branch-name>
```

### Vercel CLI 命令

```bash
# 登录 Vercel
vercel login

# 本地开发
vercel dev

# 生产部署
vercel --prod

# 查看部署日志
vercel logs

# 拉取环境变量
vercel env pull

# 查看项目信息
vercel project inspect
```

## 故障排除

### 部署失败

1. 检查 Vercel Dashboard 中的构建日志
2. 确认环境变量已正确设置
3. 检查 `api/index.py` 和 `vercel.json` 是否有语法错误
4. 查看 `requirements.txt` 中的依赖是否正确

### 运行时错误

1. 查看 Vercel Functions 日志
2. 检查依赖版本兼容性
3. 检查环境变量是否在运行时正确加载

### WebSocket 连接失败

- Vercel 的无服务函数对 WebSocket 支持有限
- 如需 WebSocket 支持，考虑：
  - 使用 Vercel Edge Functions
  - 使用定时轮询代替 WebSocket
  - 使用专门的实时通信服务（如 Pusher、Socket.io）

## 推荐做法

1. **定期同步 master 分支**
   ```bash
   git checkout master
   git pull origin master
   git checkout vercel
   git merge master
   ```

2. **使用标签标记版本**
   ```bash
   git tag -a v1.0.0 -m "Version 1.0.0"
   git push origin v1.0.0
   ```

3. **定期清理旧分支**
   ```bash
   git branch -d <old-branch>
   git push origin --delete <old-branch>
   ```

4. **使用 `.gitignore` 管理文件**
   - `.env.local` 不提交
   - `__pycache__/` 不提交
   - `data/` 目录不提交

---

需要帮助？查看 [DEPLOY_TO_VERCEL.md](DEPLOY_TO_VERCEL.md) 了解更多部署信息。

