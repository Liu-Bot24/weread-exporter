# weread-exporter

微信读书个人书籍导出工具，支持将可阅读书籍导出为 Markdown，并保存正文插图。

## 特性

- 按章节导出 Markdown
- 自动下载正文插图
- 支持全书合并版和按章拆分版
- 复用 Chromium 登录状态
- 运行缓存与最终成品分目录保存

## 安装

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## 使用

### 导出书籍

```bash
.venv/bin/python export_api.py "https://weread.qq.com/web/reader/<reader_id>"
```

也可以直接传入 reader id：

```bash
.venv/bin/python export_api.py "<reader_id>"
```

默认使用无头浏览器。如果需要扫码登录，先用可视模式运行一次：

```bash
WEREAD_HEADLESS=0 .venv/bin/python export_api.py "https://weread.qq.com/web/reader/<reader_id>"
```

### 整理成品

```bash
.venv/bin/python organize_export.py "<reader_id>" "书名" "作者"
```

### 检查结果

```bash
.venv/bin/python audit_export_quality.py "output/书名_整理版"
```

更严格的启发式检查：

```bash
.venv/bin/python audit_export_quality.py "output/书名_整理版" --strict
```

## 输出目录

中间产物：

```text
.runtime/exports/<reader_id>/
├── _catalog.json
├── chapters/
├── images/
└── raw/
```

最终成品：

```text
output/书名_整理版/
├── 书名_全书.md
├── 目录索引.md
├── 按目录拆分/
└── images/
```

## 配置

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WEREAD_RUNTIME_DIR` | `.runtime` | 运行时目录 |
| `WEREAD_CACHE_DIR` | `.runtime/cache` | 浏览器缓存目录 |
| `WEREAD_EXPORT_DIR` | `.runtime/exports` | 中间导出目录 |
| `WEREAD_ARCHIVE_DIR` | `.runtime/archives` | 归档目录 |
| `WEREAD_FINAL_OUTPUT_DIR` | `output` | 最终成品目录 |
| `WEREAD_USER_DATA_DIR` | `.runtime/cache/browser_profile` | Chromium 登录状态目录 |
| `WEREAD_HEADLESS` | `1` | 是否无头运行，扫码登录时设为 `0` |

## 备用导出方式

如果 `export_api.py` 不可用，可以尝试 Canvas 导出：

```bash
.venv/bin/python export_precise.py "https://weread.qq.com/web/reader/<reader_id>"
.venv/bin/python download_images.py "<reader_id>"
```

## 限制

- 需要微信读书账号拥有目标书籍的阅读权限
- 不支持网页端无法阅读的书籍
- 导出结果应结合 `audit_export_quality.py` 和人工抽查确认

## 声明

仅供个人学习研究使用。请勿用于商业用途或大规模传播，请尊重著作权和平台规则。
