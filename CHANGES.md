# 更新说明

## 1. 添加粘贴按钮功能

### 前端修改

#### HTML (`fastapi/templates/index.html`)
- 为YouTube链接、视频直链和抖音分享文本输入框添加了粘贴按钮
- 每个输入框右侧都有一个📋图标的粘贴按钮
- 使用flex布局确保输入框和按钮在同一行显示

#### CSS (`fastapi/static/css/style.css`)
- 添加了 `.paste-btn` 样式类
- 按钮使用主题色背景，hover时加深
- 支持浅色和深色主题

#### JavaScript (`fastapi/static/js/main.js`)
- 实现了粘贴按钮的点击事件处理
- 使用 `navigator.clipboard.readText()` API读取剪贴板内容
- 点击后自动填充到对应的输入框
- 在状态区显示粘贴成功/失败的提示

### 使用方法
1. 复制链接到剪贴板（YouTube链接、视频直链或抖音分享口令）
2. 选择对应的来源类型
3. 点击输入框右侧的📋按钮
4. 链接会自动填充到输入框中

## 2. 优化直链下载逻辑

### 后端修改 (`main.py`)

#### `download_video_and_extract_audio()` 函数优化
- 添加了音频格式检测逻辑
- 支持的音频格式：`.mp3`, `.m4a`, `.wav`, `.flac`, `.ogg`, `.aac`, `.opus`, `.wma`
- 根据URL扩展名判断文件类型

#### 智能处理流程
1. **音频文件**：
   - 直接下载到最终路径，保持原始扩展名
   - 跳过ffmpeg转换步骤
   - 显示"音频文件下载完成，跳过格式转换"提示
   
2. **视频文件**：
   - 下载到临时路径
   - 使用ffmpeg提取音频
   - 转换为指定格式（默认m4a）
   - 删除临时视频文件

#### 优势
- **节省时间**：音频文件无需转换，直接使用
- **节省资源**：避免不必要的格式转换，减少CPU使用
- **保持质量**：跳过转换避免音频质量损失
- **更快速度**：特别是处理大文件时效果明显

### 示例场景
- 输入 `https://example.com/audio.mp3` → 直接下载，跳过转换
- 输入 `https://example.com/video.mp4` → 下载后提取音频
- 输入 `https://example.com/music.flac` → 直接下载，跳过转换

## 技术细节

### 音频格式检测
```python
AUDIO_EXTENSIONS = {'.mp3', '.m4a', '.wav', '.flac', '.ogg', '.aac', '.opus', '.wma'}
is_audio_file = ext.lower() in AUDIO_EXTENSIONS
```

### 条件下载路径
```python
download_path = audio_path if is_audio_file else temp_video_path
```

### 智能清理
```python
# 只有视频文件才需要清理临时文件
if not is_audio_file:
    try:
        if os.path.exists(temp_video_path):
            os.remove(temp_video_path)
    except Exception:
        pass
```

## 测试建议

1. **测试粘贴功能**：
   - 复制YouTube链接并使用粘贴按钮
   - 复制视频直链并使用粘贴按钮
   - 复制抖音口令并使用粘贴按钮

2. **测试音频直链**：
   - 使用mp3格式的直链，确认跳过转换
   - 使用视频格式的直链，确认正常提取音频
   - 检查日志中的提示信息

3. **测试浏览器兼容性**：
   - Chrome/Edge（现代浏览器）
   - Firefox
   - Safari（注意剪贴板权限）

## 注意事项

1. **剪贴板权限**：某些浏览器可能需要用户授权才能读取剪贴板
2. **HTTPS要求**：某些浏览器在非HTTPS环境下可能限制剪贴板API
3. **音频格式**：确保直链URL包含正确的文件扩展名
4. **代理设置**：音频/视频下载会使用系统环境变量中的代理设置

## 兼容性

- **前端**：支持所有现代浏览器（Chrome 63+, Firefox 53+, Safari 13.1+）
- **后端**：Python 3.7+，无需额外依赖
- **已有功能**：完全向后兼容，不影响现有功能

