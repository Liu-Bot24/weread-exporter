# weread-exporter

微信读书全本导出工具 — 通过 Playwright + Canvas Hook 提取完整书籍**图文**内容，导出为 Markdown。文字与插图按阅读顺序精确交错。

## 原理

微信读书网页版用 Canvas 渲染书籍文字（而非 DOM 文本节点），插图则是 DOM `<img>` 元素。本工具：

1. **Playwright 自动化** — 启动 Chromium，持久化登录会话（扫码一次，后续自动复用）
2. **Canvas fillText Hook** — 注入钩子拦截所有 `CanvasRenderingContext2D.fillText()` 调用，捕获每个字符的 (x, y) 坐标
3. **双页拆分** — 微信读书在同一 Canvas 同时渲染当前页与下一页，通过检测 y 坐标重置点分离两页
4. **视口图片捕获** — 每页只取当前视口内可见的 `img[class*="wr_readerImage"]`（用 `getBoundingClientRect` 过滤掉预加载的下一页/下一章图片），解决图片归属偏移
5. **图文交错** — 把文字行和图片按屏幕 y 坐标排序，图片精确落在对应段落之间、正确章节里
6. **格式清理** — 合并 Canvas 渲染断行，还原自然段落

## 安装

```bash
pip install playwright
playwright install chromium
```

## 使用

### 1. 导出书籍（文字 + 图片 URL）

```bash
# 传入 reader URL（推荐）
python export_precise.py https://weread.qq.com/web/reader/d31323b0813abaf26g0137c2

# 或直接传 book_id
python export_precise.py d31323b0813abaf26g0137c2
```

- 首次运行会弹出浏览器要求扫码登录，会话自动保存在 `cache/browser_profile/`，后续复用
- 自动跳到全书开头（原生点击目录首项），逐页翻到全书末尾自动停止
- **自动续传**：中途卡住会重开浏览器，从上次章节继续
- 图片此阶段只记录 URL，正文 md 用相对路径 `images/chXXXX_imgNN.jpg` 内嵌引用

### 2. 下载图片

```bash
python download_images.py d31323b0813abaf26g0137c2
```

- 从 `raw/*.json` 读取所有图片 URL，8 线程并发下载到 `images/`
- **强制 IPv4**：macOS 上 urllib 默认先试 IPv6，路由不通会每张图卡约 120 秒；强制 IPv4 后恢复秒级
- 已存在的图片自动跳过（可重复运行补齐失败项）

> 导出和下载分两步：翻页抓取时若同步下载大图会阻塞翻页，故先记录 URL、翻完后统一并发下载。

## 输出

```
output/
├── <book_id>/
│   ├── _catalog.json        # 目录章节标题列表（用于判定全书末尾）
│   ├── chapters/            # 每章独立 Markdown（图文交错）
│   │   ├── 0001.md
│   │   ├── 0002.md
│   │   └── ...
│   ├── images/              # 下载的插图 chXXXX_imgNN.jpg
│   └── raw/                 # 每章的图片 URL 记录 + 字数
│       ├── 0001.json
│       └── ...
└── 书名.md                  # 合并后的全本文件（图片用 images/ 相对路径）
```

用 Typora / Obsidian 等打开全本 `.md` 即可看到图文完整的书籍。

## 限制

- 需要有效的微信读书账号，且对目标书籍有阅读权限（无限卡会员或已购买）
- 部分出版社限制网页端阅读（显示"去 App 阅读"），此类书籍无法导出
- 纯图廊章节图片密集时，图注与图的配对偶尔差一位；正文章节里图片相对段落的位置准确
- 导出速度受翻页等待限制，约每页 1-2 秒

## 工作流程

```
export_precise.py:
  浏览器登录 → 原生点击目录首项跳到开头 → 键盘 ArrowRight 逐页翻
    → 每页: Canvas Hook 捕获文字 + 视口内图片 URL
    → 双页拆分 → 文字/图片按 y 坐标交错 → 按章节切分输出 md
    → 翻到目录最后一章自动停止

download_images.py:
  读 raw/*.json 图片 URL → 强制 IPv4 + 8 线程并发下载 → images/
```

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播，请尊重著作权。
