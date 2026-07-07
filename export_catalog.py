#!/usr/bin/env python3
"""Export a WeRead book by clicking each catalog item.

This mode favors structural correctness over speed. It avoids the old
page-turning failure where a spread can contain the end of one chapter and the
start of the next chapter, causing chapter-boundary corruption.
"""
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from export_precise import (
    CANVAS_HOOK,
    DEFAULT_USER_DATA_DIR,
    EXPORT_DIR,
    MEASURE_RE,
    SENTENCE_END,
    USER_DATA_DIR,
    download_all_images,
    fetch_book_title,
    img_filename,
    prepare_user_data_dir,
)


CATALOG_IMAGES_JS = """
() => Array.from(document.querySelectorAll('img[class*="wr_readerImage"], img[src*="res.weread.qq.com/wrepub"]'))
    .map(i => {
        const src = i.src || i.getAttribute('data-src') || '';
        const r = i.getBoundingClientRect();
        return {src, top: Math.round(r.top), left: Math.round(r.left),
                w: i.naturalWidth || i.width, h: i.naturalHeight || i.height,
                display: getComputedStyle(i).display,
                visibility: getComputedStyle(i).visibility};
    })
    .filter(i => i.src.includes('res.weread.qq.com/wrepub') &&
                 i.w > 40 && i.h > 40 &&
                 i.display !== 'none' && i.visibility !== 'hidden')
"""


def safe_title(title):
    return re.sub(r'[<>:"/\\|?*\n\r\t]+', "_", title).strip() or "未命名"


def chars_to_lines(chars):
    real = [c for c in chars if len(c.get("t", "")) == 1 or not MEASURE_RE.match(c.get("t", ""))]
    rows = {}
    for c in real:
        y = c.get("canvasTop", 0) + c.get("y", 0)
        key = round(y / 3) * 3
        rows.setdefault(key, []).append(c)
    lines = []
    for key in sorted(rows):
        text = "".join(c.get("t", "") for c in sorted(rows[key], key=lambda c: c.get("canvasLeft", 0) + c.get("x", 0)))
        if text.strip():
            lines.append({"type": "text", "y": key, "text": text.strip()})
    return lines


def render_section(title, chars, images, index):
    lines = chars_to_lines(chars)
    items = lines + [{"type": "img", "y": im["top"], "image": im} for im in images]
    items.sort(key=lambda item: item["y"])

    out = [f"# {title}\n"]
    para = []
    img_records = []
    img_seq = 0
    seen_img_urls = set()

    def flush_para():
        nonlocal para
        if not para:
            return
        merged = []
        for line in para:
            if line == title:
                continue
            if merged and merged[-1] and merged[-1][-1] not in SENTENCE_END:
                merged[-1] += line
            else:
                merged.append(line)
        for paragraph in merged:
            if paragraph.strip():
                out.append(paragraph.strip())
        para = []

    for item in items:
        if item["type"] == "text":
            para.append(item["text"])
            continue
        image = item["image"]
        if image["src"] in seen_img_urls:
            continue
        flush_para()
        seen_img_urls.add(image["src"])
        img_seq += 1
        filename = img_filename(image["src"], index, img_seq)
        out.append(f"![图](images/{filename})")
        img_records.append({"url": image["src"], "file": filename})
    flush_para()

    text_len = sum(len(item["text"]) for item in lines)
    return "\n\n".join(out).rstrip() + "\n", img_records, text_len


async def wait_stable(page, timeout=12):
    last = -1
    stable = 0
    for _ in range(int(timeout / 0.5)):
        count = await page.evaluate("() => window.__wr_count ? window.__wr_count() : window.__wr_chars.length")
        if count == last and count > 0:
            stable += 1
            if stable >= 2:
                return count
        else:
            stable = 0
        last = count
        await asyncio.sleep(0.5)
    return last


async def open_catalog(page):
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.2)
    await page.locator("button.readerControls_item.catalog").first.click(timeout=5000, force=True)
    await asyncio.sleep(0.7)


async def catalog_titles(page):
    await open_catalog(page)
    titles = await page.evaluate("""() => Array.from(
        document.querySelectorAll('.readerCatalog_list_item'))
        .map(el => el.textContent.replace(/当前读到.*$/, '').trim())
        .filter(Boolean)""")
    await page.keyboard.press("Escape")
    await asyncio.sleep(0.5)
    return titles


async def click_catalog_item(page, title):
    if title == "版权信息":
        return False
    await open_catalog(page)
    box = await page.evaluate("""(title) => {
        const items = Array.from(document.querySelectorAll('.readerCatalog_list_item'));
        const item = items.find(el => el.textContent.replace(/当前读到.*$/, '').trim() === title);
        const scroller = document.querySelector('.readerCatalog_list_scroll_area, [class*="readerCatalog_list_scroll"]');
        if (!item || !scroller) return null;
        if (item && scroller) {
            for (let i = 0; i < 8; i++) {
                const r = item.getBoundingClientRect();
                const sr = scroller.getBoundingClientRect();
                const delta = r.top - (sr.top + sr.height / 2);
                if (Math.abs(delta) < 80) break;
                scroller.scrollTop += delta;
                scroller.dispatchEvent(new Event('scroll', {bubbles: true}));
            }
            item.scrollIntoView({block: 'center'});
        }
        const r = item.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return null;
        return {x: r.left + r.width / 2, y: r.top + r.height / 2, top: r.top, bottom: r.bottom};
    }""", title)
    await asyncio.sleep(0.5)
    if not box:
        return False
    try:
        await page.mouse.click(box["x"], box["y"])
        await asyncio.sleep(0.8)
        actual = await page.evaluate("""() => (
            document.querySelector('.renderTargetPageInfo_header_chapterTitle')?.textContent?.trim()
            || document.querySelector('.readerTopBar_title_chapter')?.textContent?.trim()
            || ''
        ).replace(/当前读到.*$/, '').trim()""")
        if actual != title:
            print(f"      ⚠️  点击后标题不匹配: want={title!r}, actual={actual!r}")
            return False
    except Exception as exc:
        print(f"      ⚠️  点击目录项失败: {title!r}: {exc}")
        return False
    return True


async def click_catalog_item_fresh(page, title):
    if title == "版权信息":
        return False
    await open_catalog(page)
    locator = page.locator(".readerCatalog_list_item", has_text=title).first
    try:
        await locator.scroll_into_view_if_needed(timeout=10000)
        await asyncio.sleep(0.2)
        await locator.click(timeout=10000, force=True)
        await asyncio.sleep(0.8)
        actual = await page.evaluate("""() => (
            document.querySelector('.renderTargetPageInfo_header_chapterTitle')?.textContent?.trim()
            || document.querySelector('.readerTopBar_title_chapter')?.textContent?.trim()
            || ''
        ).replace(/当前读到.*$/, '').trim()""")
        if actual != title:
            print(f"      ⚠️  点击后标题不匹配: want={title!r}, actual={actual!r}")
            return False
        return True
    except Exception as exc:
        print(f"      ⚠️  点击目录项失败: {title!r}: {exc}")
        return False


async def force_redraw_with_font_size(page):
    visible = await page.evaluate("""() => {
        const panel = document.querySelector('.font-panel-content');
        if (!panel) return false;
        const r = panel.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
    }""")
    if not visible:
        await page.locator("button.fontSizeButton").click(timeout=5000, force=True)
        await asyncio.sleep(0.4)
    dots = await page.evaluate("""() => Array.from(
        document.querySelectorAll('.reader_font_control_slider_track_level_dot'))
        .map(el => {
            const r = el.getBoundingClientRect();
            return {x: r.left + r.width / 2, y: r.top + r.height / 2,
                    w: r.width, h: r.height, selected: el.classList.contains('show')};
        }).filter(r => r.w > 0 && r.h > 0)""")
    if len(dots) < 3:
        raise RuntimeError("找不到字号滑杆，无法触发重绘")
    selected = next((idx for idx, dot in enumerate(dots) if dot.get("selected")), 1)
    target_idx = 2 if selected != 2 else 1
    target = dots[target_idx]
    await page.evaluate("() => window.__wr_reset()")
    await page.mouse.click(target["x"], target["y"])
    count = await wait_stable(page)
    if count == 0:
        fallback = dots[1 if target_idx != 1 else 2]
        await page.mouse.click(fallback["x"], fallback["y"])
        await wait_stable(page)
    await asyncio.sleep(0.4)


async def capture_catalog_item(ctx, book_id, title, index):
    page = await ctx.new_page()
    await page.add_init_script(CANVAS_HOOK)
    try:
        await page.goto(f"https://weread.qq.com/web/reader/{book_id}", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(4)
        clicked = await click_catalog_item_fresh(page, title)
        if not clicked:
            return None
        await force_redraw_with_font_size(page)
        chars = await page.evaluate("() => window.__wr_chars")
        images = await page.evaluate(CATALOG_IMAGES_JS)
        body, img_records, text_len = render_section(title, chars, images, index)
        return body, img_records, text_len
    finally:
        await page.close()


async def run(book_id):
    prepare_user_data_dir()
    book_dir = Path(EXPORT_DIR) / book_id
    chapters_dir = book_dir / "chapters"
    raw_dir = book_dir / "raw"
    img_dir = book_dir / "images"
    for path in (chapters_dir, raw_dir, img_dir):
        path.mkdir(parents=True, exist_ok=True)

    headless = os.environ.get("WEREAD_HEADLESS", "").lower() in {"1", "true", "yes", "on"}
    viewport = {
        "width": int(os.environ.get("WEREAD_VIEWPORT_WIDTH", "1200")),
        "height": int(os.environ.get("WEREAD_VIEWPORT_HEIGHT", "900")),
    }
    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR or DEFAULT_USER_DATA_DIR,
            headless=headless,
            viewport=viewport,
            args=["--disable-blink-features=AutomationControlled"],
        )
        login_page = await ctx.new_page()
        await login_page.goto("https://weread.qq.com/web/shelf", timeout=30000)
        await asyncio.sleep(3)
        if "login" in login_page.url.lower():
            print("请先扫码登录微信读书。")
            await ctx.close()
            return 1
        await login_page.close()

        page = await ctx.new_page()
        await page.add_init_script(CANVAS_HOOK)
        await page.goto(f"https://weread.qq.com/web/reader/{book_id}", wait_until="networkidle", timeout=30000)
        await asyncio.sleep(5)
        book_title, book_author = await fetch_book_title(page)
        titles = await catalog_titles(page)
        (book_dir / "_catalog.json").write_text(json.dumps(titles, ensure_ascii=False, indent=2), encoding="utf-8")
        await page.close()
        print(f"📖 {book_title} — {book_author}")
        print(f"目录项: {len(titles)}")

        total_chars = 0
        total_images = 0
        for idx, title in enumerate(titles, 1):
            captured = await capture_catalog_item(ctx, book_id, title, idx)
            if captured is None:
                print(f"[{idx:04d}] {title[:36]:36s} 目录项不可点击，跳过")
                continue
            body, img_records, text_len = captured
            if text_len == 0 and not img_records:
                print(f"[{idx:04d}] {title[:36]:36s} 空，跳过")
                continue
            (chapters_dir / f"{idx:04d}.md").write_text(body, encoding="utf-8")
            (raw_dir / f"{idx:04d}.json").write_text(
                json.dumps({"title": title, "images": img_records, "text_len": text_len}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            total_chars += text_len
            total_images += len(img_records)
            print(f"[{idx:04d}] {title[:36]:36s} {text_len:5d}字 +{len(img_records)}图")

        await ctx.close()

    downloaded = download_all_images(str(raw_dir), str(img_dir))
    print(f"完成: {len(list(chapters_dir.glob('*.md')))} 项, {total_chars:,} 字, {total_images} 图, 下载 {downloaded} 图")
    print(f"title={book_title}")
    print(f"author={book_author}")
    return 0


def main(argv):
    if len(argv) != 2:
        raise SystemExit("Usage: python export_catalog.py <book_id_or_url>")
    raw = argv[1].strip().rstrip("/")
    book_id = raw.split("/")[-1] if "weread.qq.com" in raw else raw
    return asyncio.run(run(book_id))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
