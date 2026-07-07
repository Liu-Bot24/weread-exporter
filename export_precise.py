#!/usr/bin/env python3
"""
微信读书导出 — 精确图文版 v3

逐页捕获：每翻一页，抓当前视口内的 canvas 文字 + 视口内图片，
按屏幕 y 坐标把文字行和图片交错排序，图片精确落在对应段落之间。
双页拆分(左页→右页)，按章节切分，自动续传，卡住重开。
"""
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import urllib.request

from playwright.async_api import async_playwright

RUNTIME_DIR = os.environ.get("WEREAD_RUNTIME_DIR", ".runtime")
CACHE_DIR = os.environ.get("WEREAD_CACHE_DIR", os.path.join(RUNTIME_DIR, "cache"))
EXPORT_DIR = os.environ.get("WEREAD_EXPORT_DIR", os.path.join(RUNTIME_DIR, "exports"))
DEFAULT_USER_DATA_DIR = os.path.join(CACHE_DIR, "browser_profile")
LEGACY_USER_DATA_DIR = os.path.join("cache", "browser_profile")
USER_DATA_DIR = os.environ.get("WEREAD_USER_DATA_DIR", DEFAULT_USER_DATA_DIR)
LAYOUT_MODE = os.environ.get("WEREAD_LAYOUT_MODE", "single").strip().lower()
SINGLE_PAGE_MODE = LAYOUT_MODE != "spread"
CHROME_EXECUTABLE = os.environ.get("WEREAD_CHROME_EXECUTABLE")

CANVAS_HOOK = """
(function() {
    window.__wr_chars = [];
    var origFill = CanvasRenderingContext2D.prototype.fillText;
    CanvasRenderingContext2D.prototype.fillText = function(text, x, y) {
        if (text && text.trim()) {
            var r = this.canvas ? this.canvas.getBoundingClientRect() : null;
            window.__wr_chars.push({
                t: text,
                x: Math.round(x*10)/10,
                y: Math.round(y*10)/10,
                canvasLeft: r ? Math.round(r.left) : 0,
                canvasTop: r ? Math.round(r.top) : 0,
                canvasWidth: r ? Math.round(r.width) : 0,
                canvasHeight: r ? Math.round(r.height) : 0
            });
        }
        return origFill.apply(this, arguments);
    };
    window.__wr_reset = function() { window.__wr_chars = []; };
    window.__wr_count = function() { return window.__wr_chars.length; };
})();
"""


def prepare_user_data_dir():
    if (USER_DATA_DIR == DEFAULT_USER_DATA_DIR and
            not os.path.exists(USER_DATA_DIR) and
            os.path.isdir(LEGACY_USER_DATA_DIR)):
        os.makedirs(os.path.dirname(USER_DATA_DIR), exist_ok=True)
        shutil.move(LEGACY_USER_DATA_DIR, USER_DATA_DIR)
        try:
            os.rmdir(os.path.dirname(LEGACY_USER_DATA_DIR))
        except OSError:
            pass
        print(f"  ℹ️  已迁移浏览器缓存: {LEGACY_USER_DATA_DIR} -> {USER_DATA_DIR}")
    os.makedirs(USER_DATA_DIR, exist_ok=True)

# 当前视口内可见的书籍插图，带屏幕坐标
VIEWPORT_IMGS_JS = """
() => {
    const H = window.innerHeight, W = window.innerWidth, out = [];
    document.querySelectorAll('img[class*="wr_readerImage"]').forEach(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        if (!src.includes('res.weread.qq.com/wrepub')) return;
        const r = i.getBoundingClientRect();
        if (r.width > 40 && r.height > 40 && r.bottom > 0 && r.top < H &&
            r.right > 0 && r.left < W &&
            getComputedStyle(i).visibility !== 'hidden' &&
            getComputedStyle(i).display !== 'none') {
            out.push({src, top: Math.round(r.top), left: Math.round(r.left),
                      w: i.naturalWidth||i.width, h: i.naturalHeight||i.height});
        }
    });
    return out;
}
"""

# reader 的两个 canvas 的屏幕位置
CANVAS_RECTS_JS = """
() => Array.from(document.querySelectorAll('canvas')).map(c => {
    const r = c.getBoundingClientRect();
    return {top: r.top, left: r.left, w: Math.round(r.width), h: Math.round(r.height)};
}).filter(r => r.h > 300)
"""

MEASURE_RE = re.compile(r'^[a-zA-Z0-9`~!@#$%^&*()\-_=+\[\]{}|;:\',<.>/?\\"\s]+$')
SENTENCE_END = set("。！？；：」）】》…—")


def split_spread(chars):
    """双页拆分：返回 [左页chars, 右页chars] 或 [单页chars]"""
    if len(chars) < 10:
        return [chars]
    singles = [(i, c) for i, c in enumerate(chars) if len(c["t"]) == 1]
    if len(singles) < 10:
        return [chars]

    # Newer captures include the rendering canvas geometry. If WeRead renders
    # a spread through two canvases, split by canvas instead of draw order.
    canvas_groups = {}
    for _, c in singles:
        width = c.get("canvasWidth", 0)
        height = c.get("canvasHeight", 0)
        if width > 100 and height > 300:
            key = (round(c.get("canvasLeft", 0) / 20) * 20,
                   round(c.get("canvasTop", 0) / 20) * 20,
                   round(width / 20) * 20,
                   round(height / 20) * 20)
            canvas_groups[key] = canvas_groups.get(key, 0) + 1
    prominent = [key for key, count in canvas_groups.items() if count >= 5]
    if len(prominent) >= 2:
        selected = sorted(prominent, key=lambda k: k[0])[:2]

        def char_canvas_key(c):
            return (round(c.get("canvasLeft", 0) / 20) * 20,
                    round(c.get("canvasTop", 0) / 20) * 20,
                    round(c.get("canvasWidth", 0) / 20) * 20,
                    round(c.get("canvasHeight", 0) / 20) * 20)

        pages = [[c for c in chars if char_canvas_key(c) == key] for key in selected]
        if all(len(page) >= 5 for page in pages):
            return pages

    # Some spreads render into one wide canvas and the fillText calls may be
    # interleaved row-by-row. Split by the largest horizontal gap so left and
    # right pages do not get merged into one line.
    xs = sorted(c["x"] for _, c in singles if isinstance(c.get("x"), (int, float)))
    if len(xs) >= 20:
        best_gap = 0
        split_x = None
        for left, right in zip(xs, xs[1:]):
            gap = right - left
            if gap > best_gap:
                best_gap = gap
                split_x = (left + right) / 2
        if split_x is not None and best_gap >= max(100, (xs[-1] - xs[0]) * 0.18):
            left_count = sum(1 for _, c in singles if c["x"] < split_x)
            right_count = len(singles) - left_count
            if left_count >= 10 and right_count >= 10:
                left_page = [c for c in chars if c.get("x", 0) < split_x]
                right_page = [c for c in chars if c.get("x", 0) >= split_x]
                return [left_page, right_page]

    for j in range(1, len(singles)):
        if singles[j - 1][1]["y"] > 400 and singles[j][1]["y"] < 200:
            return [chars[:singles[j][0]], chars[singles[j][0]:]]
    return [chars]


def chars_to_lines(chars):
    """把单页字符按 y 分行，返回 [{y, text}]（未合并段落）"""
    real = [c for c in chars if len(c["t"]) == 1 or not MEASURE_RE.match(c["t"])]
    if not real:
        return []
    rows = {}
    for c in real:
        y_key = round(c["y"] / 3) * 3
        rows.setdefault(y_key, []).append(c)
    lines = []
    for yk in sorted(rows):
        line = "".join(c["t"] for c in sorted(rows[yk], key=lambda c: c["x"]))
        if line.strip():
            lines.append({"y": yk, "text": line.strip()})
    return lines


def build_page_blocks(chars, images, canvas_rects, seen_imgs):
    """把一次渲染(可能双页)拆成有序块列表: [{type:'text'/'img', ...}]
       文字行和图片按屏幕 y 交错；左页整页在前，右页在后。"""
    blocks = []
    pages = split_spread(chars)

    # 判定左右 canvas
    rects = sorted(canvas_rects, key=lambda r: r["left"])
    left_rect = rects[0] if rects else {"top": 0, "left": 0}
    right_rect = rects[1] if len(rects) > 1 else left_rect
    if len(rects) > 1:
        mid_x = (left_rect["left"] + left_rect["w"] + right_rect["left"]) / 2
    elif len(pages) == 2 and rects:
        mid_x = left_rect["left"] + left_rect["w"] / 2
    else:
        mid_x = 99999

    # 图片按左右分组
    left_imgs = [im for im in images if im["left"] < mid_x]
    right_imgs = [im for im in images if im["left"] >= mid_x]

    def emit_page(page_chars, page_rect, page_imgs):
        lines = chars_to_lines(page_chars)
        items = []
        for ln in lines:
            items.append(("text", page_rect["top"] + ln["y"], ln["text"]))
        for im in page_imgs:
            if im["src"] in seen_imgs:
                continue
            items.append(("img", im["top"], im))
        items.sort(key=lambda t: t[1])
        for typ, _y, payload in items:
            if typ == "text":
                blocks.append({"type": "text", "text": payload})
            else:
                seen_imgs.add(payload["src"])
                blocks.append({"type": "img", "src": payload["src"],
                                "w": payload["w"], "h": payload["h"]})

    if len(pages) == 2:
        emit_page(pages[0], left_rect, left_imgs)
        emit_page(pages[1], right_rect, right_imgs)
    else:
        # 单页：图片全归这页，仍按 y 排
        emit_page(pages[0], left_rect, left_imgs + right_imgs)
    return blocks


def img_filename(url, ch_idx, seq):
    ext = "jpg"
    m = re.search(r'\.(jpg|jpeg|png|gif|webp)', url.lower())
    if m:
        ext = m.group(1).replace("jpeg", "jpg")
    return f"ch{ch_idx:04d}_img{seq:02d}.{ext}"


def render_chapter_md(ch_title, blocks, ch_idx):
    """把有序块渲染成 Markdown：文字行合并成段落，图片就地插入"""
    out = [f"# {ch_title}\n"]
    para = []
    img_records = []
    img_seq = 0

    def flush_para():
        nonlocal para
        if not para:
            return
        # 合并 canvas 断行为自然段：上一行不以句末标点结尾则接续
        merged = []
        for line in para:
            if line == ch_title:
                continue
            if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
                merged[-1] += line
            else:
                merged.append(line)
        for m in merged:
            if m.strip():
                out.append(m.strip())
        para = []

    for b in blocks:
        if b["type"] == "text":
            para.append(b["text"])
        else:
            flush_para()
            img_seq += 1
            fname = img_filename(b["src"], ch_idx, img_seq)
            out.append(f"![图](images/{fname})")
            img_records.append({"url": b["src"], "file": fname})
    flush_para()

    body = "\n\n".join(out) + "\n"
    return body, img_records


async def wait_stable(page, prev_count, timeout=8):
    """等页面渲染稳定，返回稳定后的字符数"""
    last = -1
    for _ in range(int(timeout / 0.5)):
        c = await page.evaluate("() => window.__wr_count()")
        if c == last:
            return c
        last = c
        await asyncio.sleep(0.5)
    return last


def get_last_chapter_title(md_dir):
    if not os.path.exists(md_dir):
        return None, 0
    files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    if not files:
        return None, 0
    idx = int(files[-1].replace(".md", ""))
    with open(os.path.join(md_dir, files[-1])) as f:
        title = f.readline().strip().replace("# ", "")
    return title, idx


def load_last_catalog_title(catalog_path):
    try:
        with open(catalog_path) as f:
            titles = json.load(f)
        return titles[-1] if titles else ""
    except Exception:
        return ""


async def _title(page):
    return await page.evaluate(
        """() => (
            document.querySelector('.renderTargetPageInfo_header_chapterTitle')?.textContent?.trim()
            || document.querySelector('.readerTopBar_title_chapter')?.textContent?.trim()
            || ''
        ).replace(/当前读到.*$/, '').trim()""")


async def fetch_book_title(page):
    info = await page.evaluate("""() => {
        const title = document.querySelector('.readerCatalog_bookInfo_title_txt, .bookInfo_right_header_title')
            ?.textContent?.trim() || document.title.replace(/-.*$/, '').trim();
        const author = document.querySelector('.readerCatalog_bookInfo_author, .bookInfo_author a')
            ?.textContent?.trim() || '';
        return {title, author};
    }""")
    return info.get("title", "未知"), info.get("author", "")


async def goto_first_chapter(page, catalog_path=None):
    first_title = ""
    try:
        await page.locator("button.rbb_item.catalog, button.readerControls_item.catalog").first.click(
            timeout=5000, force=True)
        await asyncio.sleep(1.5)
        titles = await page.evaluate("""() => Array.from(
            document.querySelectorAll('.readerCatalog_list_item'))
            .map(el => el.textContent.replace(/当前读到.*$/, '').trim())
            .filter(Boolean)""")
        if titles and catalog_path:
            with open(catalog_path, "w") as f:
                json.dump(titles, f, ensure_ascii=False)
        await page.evaluate("""() => {
            const sc = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
            if (sc) {
                sc.scrollTop = 0;
                sc.dispatchEvent(new Event("scroll", {bubbles: true}));
            }
        }""")
        await asyncio.sleep(1)
        first_title = await page.evaluate("""() => {
            const item = document.querySelector('.readerCatalog_list_item');
            if (!item) return '';
            const title = item.textContent.replace(/当前读到.*$/, '').trim();
            item.click();
            return title;
        }""")
        await asyncio.sleep(3)
        try:
            await page.locator("button.rbb_item.catalog, button.readerControls_item.catalog").first.click(
                timeout=2000, force=True)
        except Exception:
            await page.keyboard.press("Escape")
        await asyncio.sleep(2)
    except Exception as e:
        print(f"  ⚠️  目录跳转异常: {e}")
    print(f"  ✅ 已跳到全书开头，当前:「{await _title(page)}」(点击首项「{first_title}」)")


def save_chapter(ch_title, blocks, ch_idx, md_dir, raw_dir):
    body, img_records = render_chapter_md(ch_title, blocks, ch_idx)
    text_len = sum(len(b["text"]) for b in blocks if b["type"] == "text")
    if text_len == 0 and not img_records:
        return 0, []
    with open(os.path.join(md_dir, f"{ch_idx:04d}.md"), "w") as f:
        f.write(body)
    with open(os.path.join(raw_dir, f"{ch_idx:04d}.json"), "w") as f:
        json.dump({"title": ch_title, "images": img_records, "text_len": text_len},
                  f, ensure_ascii=False)
    return text_len, img_records


async def run_session(book_id, md_dir, raw_dir, start_idx, seen_imgs,
                      goto_first=False, catalog_path=None):
    reached_end = False
    last_cat_title = load_last_catalog_title(catalog_path) if catalog_path else ""
    async with async_playwright() as p:
        headless = os.environ.get("WEREAD_HEADLESS", "").lower() in {"1", "true", "yes", "on"}
        default_width = "760" if SINGLE_PAGE_MODE else "1200"
        default_height = "1100" if SINGLE_PAGE_MODE else "900"
        viewport = {
            "width": int(os.environ.get("WEREAD_VIEWPORT_WIDTH", default_width)),
            "height": int(os.environ.get("WEREAD_VIEWPORT_HEIGHT", default_height)),
        }
        print(f"  版式模式: {'单页质量模式' if SINGLE_PAGE_MODE else '双页/宽屏模式'} "
              f"({viewport['width']}x{viewport['height']})")
        launch_options = {
            "headless": headless,
            "viewport": viewport,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if CHROME_EXECUTABLE:
            launch_options["executable_path"] = CHROME_EXECUTABLE
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR, **launch_options)

        login_page = await ctx.new_page()
        await login_page.goto("https://weread.qq.com/web/shelf", timeout=30000)
        await asyncio.sleep(3)
        if "login" in login_page.url.lower():
            print("\n  ⚠️  请扫码登录微信读书")
            for _ in range(120):
                await asyncio.sleep(5)
                if "login" not in login_page.url.lower():
                    print("  ✅ 登录成功"); break
            else:
                await login_page.close(); await ctx.close()
                return "", "", 0, 0, start_idx, False
        else:
            print("  ✅ 已登录")
        await login_page.close()

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        print("\n  打开阅读器...")
        await page.goto(f"https://weread.qq.com/web/reader/{book_id}",
                        wait_until="networkidle", timeout=30000)
        await asyncio.sleep(5)

        book_title, book_author = await fetch_book_title(page)
        if goto_first:
            await goto_first_chapter(page, catalog_path)
            last_cat_title = load_last_catalog_title(catalog_path)

        await page.mouse.click(600, 450)
        await asyncio.sleep(0.5)

        current_chapter = await _title(page)
        if not current_chapter:
            raise RuntimeError("无法识别当前章节标题，停止导出以避免生成无边界文档。")
        print(f"  📖 {book_title} — {book_author}")
        print(f"  会话开始:「{current_chapter}」\n")

        ch_idx = start_idx
        ch_blocks = []
        total_chars = total_imgs = 0
        chapters_this_session = 0
        stale = 0
        page_num = 0

        async def capture_current_page():
            """抓当前页的有序块，累加到 ch_blocks；返回是否有新内容"""
            await asyncio.sleep(0.3)
            chars = await page.evaluate("() => window.__wr_chars")
            rects = await page.evaluate(CANVAS_RECTS_JS)
            imgs = await page.evaluate(VIEWPORT_IMGS_JS)
            if SINGLE_PAGE_MODE and len(split_spread(chars)) > 1:
                raise RuntimeError(
                    "单页质量模式下仍检测到双页内容。请调小 WEREAD_VIEWPORT_WIDTH，"
                    "或显式设置 WEREAD_LAYOUT_MODE=spread 后自行承担跨章双页风险。")
            before = len(ch_blocks)
            new_blocks = build_page_blocks(chars, imgs, rects, seen_imgs)
            # 文字去重：同一页可能重复捕获，按文本行内容去重
            for b in new_blocks:
                if b["type"] == "text":
                    if ch_blocks and ch_blocks[-1].get("type") == "text" and ch_blocks[-1]["text"] == b["text"]:
                        continue
                ch_blocks.append(b)
            return len(ch_blocks) > before

        # 首页
        await page.evaluate("() => window.__wr_reset()")
        await wait_stable(page, 0)
        await capture_current_page()

        while True:
            await page.evaluate("() => window.__wr_reset()")
            await page.mouse.click(600, 450)
            await page.keyboard.press("ArrowRight")
            await asyncio.sleep(1.0)
            await wait_stable(page, 0)

            new_chapter = await _title(page)
            if new_chapter and new_chapter != current_chapter:
                # 章节切换：保存上一章
                n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
                total_chars += n; total_imgs += len(imgs)
                note = f" +{len(imgs)}图" if imgs else ""
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 ({page_num}页){note}")
                is_last = bool(last_cat_title and current_chapter == last_cat_title)
                ch_idx += 1
                chapters_this_session += 1
                ch_blocks = []
                current_chapter = new_chapter
                page_num = 0
                stale = 0
                await capture_current_page()
                if is_last:
                    reached_end = True
                    # 再存这最后一章
                    break
                continue

            got_new = await capture_current_page()
            if not got_new:
                stale += 1
                if stale >= 10:
                    n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
                    total_chars += n; total_imgs += len(imgs)
                    note = f" +{len(imgs)}图" if imgs else ""
                    if last_cat_title and current_chapter == last_cat_title:
                        reached_end = True; note += " [全书末尾]"
                    print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 ({page_num}页){note}")
                    break
            else:
                stale = 0
            page_num += 1

        # reached_end 时把最后一章存下
        if reached_end and ch_blocks:
            n, imgs = save_chapter(current_chapter, ch_blocks, ch_idx, md_dir, raw_dir)
            if n or imgs:
                total_chars += n; total_imgs += len(imgs)
                print(f"  [{ch_idx:4d}] {current_chapter[:32]:32s} {n:6d}字 [末章]")
                chapters_this_session += 1

        await page.close(); await ctx.close()
        return book_title, book_author, chapters_this_session, total_chars, ch_idx, reached_end


def download_all_images(raw_dir, img_dir):
    os.makedirs(img_dir, exist_ok=True)
    tasks = []
    for jf in sorted(os.listdir(raw_dir)):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                tasks.append((img["url"], img["file"]))
    if not tasks:
        print("  (无图片)"); return 0
    print(f"\n  下载 {len(tasks)} 张图片...")
    ok = 0
    for url, fname in tasks:
        fp = os.path.join(img_dir, fname)
        if os.path.exists(fp) and os.path.getsize(fp) > 1000:
            ok += 1; continue
        try:
            req = urllib.request.Request(url, headers={
                "Referer": "https://weread.qq.com/", "User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=20).read()
            if len(raw) > 500:
                open(fp, "wb").write(raw); ok += 1
                if ok % 20 == 0:
                    print(f"    {ok}/{len(tasks)}...")
        except Exception as e:
            print(f"    ⚠️  {fname} 失败: {e}")
    print(f"  ✅ 图片下载完成 {ok}/{len(tasks)}")
    return ok


async def main(book_id):
    print("=" * 60)
    print("  weread-exporter — 精确图文导出 v3")
    print("=" * 60)
    prepare_user_data_dir()
    book_dir = os.path.join(EXPORT_DIR, book_id)
    md_dir = os.path.join(book_dir, "chapters")
    raw_dir = os.path.join(book_dir, "raw")
    img_dir = os.path.join(book_dir, "images")
    for d in (md_dir, raw_dir, img_dir):
        os.makedirs(d, exist_ok=True)

    seen_imgs = set()
    for jf in os.listdir(raw_dir):
        if jf.endswith(".json"):
            for img in json.load(open(os.path.join(raw_dir, jf))).get("images", []):
                seen_imgs.add(img["url"])

    catalog_path = os.path.join(book_dir, "_catalog.json")
    book_title = book_author = ""
    session = 0
    while True:
        session += 1
        last_title, last_idx = get_last_chapter_title(md_dir)
        start_idx = last_idx + 1 if last_idx > 0 else 1
        print(f"\n--- 会话 {session} ---")
        print(f"  上次: {last_title or '(无)'}, 编号: {last_idx}")
        goto_first = (session == 1 and last_idx == 0)
        title, author, added, chars_added, end_idx, reached_end = await run_session(
            book_id, md_dir, raw_dir, start_idx, seen_imgs,
            goto_first=goto_first, catalog_path=catalog_path)
        if title: book_title = title
        if author: book_author = author
        print(f"\n  本次: +{added} 章, +{chars_added:,} 字")
        if reached_end:
            print("\n  ✅ 已到全书最后一章，导出完成。"); break
        if added == 0:
            print("\n  无新章节，导出完成。"); break
        print("  3 秒后自动重开继续..."); await asyncio.sleep(3)

    download_all_images(raw_dir, img_dir)

    total_files = sorted(f for f in os.listdir(md_dir) if f.endswith(".md"))
    img_count = len([f for f in os.listdir(img_dir) if not f.startswith(".")])
    if not book_title: book_title = book_id
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)
    merged_dir = os.path.join(EXPORT_DIR, "merged")
    os.makedirs(merged_dir, exist_ok=True)
    merged = os.path.join(merged_dir, f"{safe}.md")
    with open(merged, "w") as out:
        out.write(f"# {book_title}\n\n**{book_author}**\n\n---\n\n")
        for fn in total_files:
            out.write(open(os.path.join(md_dir, fn)).read())
            out.write("\n\n---\n\n")
    print(f"\n{'=' * 60}")
    print(f"  ✅ 全书导出完成!  📖 {book_title} — {book_author}")
    print(f"  📄 {len(total_files)} 章, {os.path.getsize(merged):,} bytes,  🖼 {img_count} 张图")
    print(f"  📦 {merged}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python export_precise.py <book_url_or_id>"); sys.exit(1)
    raw = sys.argv[1].strip().rstrip("/")
    book_id = raw.split("/")[-1] if "weread.qq.com" in raw else raw
    print(f"  Book ID: {book_id}")
    asyncio.run(main(book_id))
