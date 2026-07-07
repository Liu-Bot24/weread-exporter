#!/usr/bin/env python3
"""Export a WeRead EPUB through the reader content API.

This avoids the canvas/page-turning path. Each Markdown file is built from the
book's own chapter XHTML, so chapter boundaries come from chapterUid rather
than from whichever visual page happened to be visible.
"""
import asyncio
import base64
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from playwright.async_api import async_playwright

from export_precise import USER_DATA_DIR, prepare_user_data_dir


RUNTIME_DIR = Path(os.environ.get("WEREAD_RUNTIME_DIR", ".runtime"))
EXPORT_DIR = Path(os.environ.get("WEREAD_EXPORT_DIR", RUNTIME_DIR / "exports"))
ARCHIVE_DIR = Path(os.environ.get("WEREAD_ARCHIVE_DIR", RUNTIME_DIR / "archives"))

INVALID = re.compile(r'[<>:"/\\|?*\n\r\t]+')
SENTENCE_SPACE = re.compile(r"\s+")


def safe_name(value, max_len=80):
    value = INVALID.sub("_", value).strip(" .")
    return (value[:max_len].rstrip(" .") or "未命名")


def weread_encode(value):
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return value

    digest = hashlib.md5(value.encode()).hexdigest()
    out = digest[:3]
    if value.isdigit():
        typ = "3"
        parts = [
            format(int(value[idx:min(idx + 9, len(value))]), "x")
            for idx in range(0, len(value), 9)
        ]
    else:
        typ = "4"
        parts = ["".join(format(ord(ch), "x") for ch in value)]

    out += typ + "2" + digest[-2:]
    for idx, part in enumerate(parts):
        part_len = format(len(part), "x")
        out += (part_len if len(part_len) > 1 else "0" + part_len) + part
        if idx < len(parts) - 1:
            out += "g"

    if len(out) < 20:
        out += digest[:20 - len(out)]
    return out + hashlib.md5(out.encode()).hexdigest()[:3]


def format_query(params):
    return "&".join(
        key + "=" + quote(str(params[key]), safe="~()*!.'")
        for key in sorted(params)
    )


def request_hash(query):
    left = right = 0x15051505
    length = len(query)
    for idx in range(length - 1, 0, -2):
        left = 0x7FFFFFFF & (left ^ (ord(query[idx]) << ((length - idx) % 30)))
        right = 0x7FFFFFFF & (right ^ (ord(query[idx - 1]) << (idx % 30)))
    return format(left + right, "x").lower()


def make_chapter_params(book_num_id, chapter_uid, part_idx):
    now = int(time.time())
    params = {
        "b": weread_encode(book_num_id),
        "c": weread_encode(chapter_uid),
        "r": int(10000 * time.time() % 10000) ** 2 + part_idx,
        "st": 1 if part_idx == 2 else 0,
        "ct": now,
        "ps": weread_encode(now),
        "pc": weread_encode(now),
        "sc": 0,
    }
    params["s"] = request_hash(format_query(params))
    return params


def unshuffle_positions(value):
    length = len(value)
    if length < 4:
        return []
    if length < 11:
        return [0, 2]

    tail_len = min(4, math.ceil(length / 10))
    seed = ""
    for idx in range(length - 1, length - 1 - tail_len, -1):
        seed += str(int(bin(ord(value[idx]))[2:], 4))

    max_index = length - tail_len - 2
    digit_len = len(str(max_index))
    positions = []
    idx = 0
    while len(positions) < 10 and idx + digit_len < len(seed):
        positions.append(int(seed[idx:idx + digit_len]) % max_index)
        positions.append(int(seed[idx + 1:idx + 1 + digit_len]) % max_index)
        idx += digit_len
    return positions


def decode_segment_string(value):
    if not value or len(value) <= 1:
        return ""
    chars = list(value[1:])
    positions = unshuffle_positions("".join(chars))
    for idx in range(len(positions) - 1, 0, -2):
        for offset in (1, 0):
            left = positions[idx] + offset
            right = positions[idx - 1] + offset
            chars[left], chars[right] = chars[right], chars[left]
    encoded = "".join(chars)
    encoded += "=" * ((4 - len(encoded) % 4) % 4)
    return base64.b64decode(encoded).decode("utf-8")


def strip_response_hash(raw):
    if len(raw) <= 32:
        return raw
    prefix, body = raw[:32], raw[32:]
    if re.fullmatch(r"[0-9A-Fa-f]{32}", prefix):
        expected = hashlib.sha256(body.encode()).hexdigest().upper()
        # WeRead sometimes hashes the full post-noise body. Keep the content
        # even if the check fails; XML parsing below is the real gate.
        return body if expected == prefix else body
    return raw


def normalize_text(text):
    return SENTENCE_SPACE.sub(" ", text or "").strip()


def local_name(tag):
    return tag.rsplit("}", 1)[-1].lower()


def is_footnote_image(element):
    if local_name(element.tag) != "img":
        return False
    classes = set((element.attrib.get("class") or "").split())
    src = element.attrib.get("src") or ""
    return "qqreader-footnote" in classes or src.endswith("/note.png")


def inline_text(element):
    parts = [element.text or ""]
    for child in element:
        if is_footnote_image(child):
            note = normalize_text(child.attrib.get("alt"))
            if note:
                parts.append(f"（注：{note}）")
        else:
            parts.append(inline_text(child))
        parts.append(child.tail or "")
    return normalize_text("".join(parts))


def image_url(src, book_num_id):
    if not src:
        return ""
    if src.startswith("http://") or src.startswith("https://"):
        return src
    name = src.split("/")[-1]
    return f"https://res.weread.qq.com/wrepub/web/{book_num_id}/{name}"


def image_ext(url):
    match = re.search(r"\.(jpg|jpeg|png|gif|webp)(?:[?#]|$)", url, re.I)
    if not match:
        return "jpg"
    ext = match.group(1).lower()
    return "jpg" if ext == "jpeg" else ext


def xhtml_to_markdown(xhtml, title, chapter_index, book_num_id):
    root = ET.fromstring(xhtml.encode("utf-8"))
    body = next((node for node in root.iter() if local_name(node.tag) == "body"), root)
    lines = [f"# {title}", ""]
    images = []
    image_seen = {}

    def emit_image(node):
        if is_footnote_image(node):
            return
        src = image_url(node.attrib.get("src", ""), book_num_id)
        if not src:
            return
        if src not in image_seen:
            seq = len(image_seen) + 1
            filename = f"ch{chapter_index:04d}_img{seq:02d}.{image_ext(src)}"
            image_seen[src] = filename
            images.append({"url": src, "file": filename})
        lines.append(f"![图](images/{image_seen[src]})")
        lines.append("")

    def walk(node):
        tag = local_name(node.tag)
        if tag == "img":
            emit_image(node)
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = normalize_text(node.attrib.get("title")) or inline_text(node)
            if heading and heading != title:
                level = min(max(int(tag[1]), 2), 6)
                lines.append("#" * level + " " + heading)
                lines.append("")
            return
        if tag == "p":
            text = inline_text(node)
            if text:
                lines.append(text)
                lines.append("")
            return
        for child in node:
            walk(child)

    for child in body:
        walk(child)

    while lines and lines[-1] == "":
        lines.pop()
    return "\n\n".join(lines) + "\n", images


async def request_text(page, url, payload):
    response = await page.request.post(
        url,
        data=json.dumps(payload, ensure_ascii=False),
        headers={"content-type": "application/json"},
        timeout=30000,
    )
    text = await response.text()
    if response.status != 200:
        raise RuntimeError(f"{url} status={response.status}: {text[:200]}")
    return text


async def fetch_chapter_xhtml(page, book_num_id, chapter_uid):
    pieces = []
    for part_idx in (0, 1, 3):
        payload = make_chapter_params(book_num_id, chapter_uid, part_idx)
        raw = await request_text(
            page,
            f"https://weread.qq.com/web/book/chapter/e_{part_idx}",
            payload,
        )
        pieces.append(strip_response_hash(raw))
    xhtml = decode_segment_string("".join(pieces))
    if "<html" not in xhtml or "</html>" not in xhtml:
        raise RuntimeError(f"chapterUid={chapter_uid} did not decode to XHTML")
    return xhtml


async def fetch_chapter_infos(page, book_num_id):
    response = await page.request.post(
        "https://weread.qq.com/web/book/chapterInfos",
        data=json.dumps({"bookIds": [str(book_num_id)]}),
        headers={"content-type": "application/json"},
        timeout=30000,
    )
    data = await response.json()
    books = data.get("data") or []
    if not books or not books[0].get("updated"):
        raise RuntimeError(f"chapterInfos missing for {book_num_id}: {data}")
    return books[0]["updated"]


async def download_images(page, book_dir, image_records):
    images_dir = book_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    ok = fail = 0
    for record in image_records:
        target = images_dir / record["file"]
        if target.exists() and target.stat().st_size > 500:
            ok += 1
            continue
        try:
            response = await page.request.get(record["url"], timeout=30000)
            body = await response.body()
            if response.status == 200 and len(body) > 100:
                target.write_bytes(body)
                ok += 1
            else:
                fail += 1
        except Exception:
            fail += 1
    return ok, fail


async def export_book(book_url_or_id, book_title=None, author=None):
    book_reader_id = book_url_or_id.rstrip("/").split("/")[-1]
    if not re.fullmatch(r"[0-9a-fA-F]+g?[0-9a-fA-F]*|[0-9A-Za-z_]+", book_reader_id):
        raise RuntimeError(f"invalid reader id: {book_reader_id}")

    prepare_user_data_dir()
    book_dir = EXPORT_DIR / book_reader_id
    if book_dir.exists():
        archive = ARCHIVE_DIR / f"api_export_{book_reader_id}_{time.strftime('%Y%m%d_%H%M%S')}"
        archive.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(book_dir), str(archive))
    chapters_dir = book_dir / "chapters"
    raw_dir = book_dir / "raw"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        headless = os.environ.get("WEREAD_HEADLESS", "1").lower() not in {"0", "false", "no", "off"}
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=headless,
            viewport={"width": 1400, "height": 1000},
        )
        page = await ctx.new_page()
        await page.goto(f"https://weread.qq.com/web/reader/{book_reader_id}", wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(1500)
        book_meta = await page.evaluate(
            """() => {
                const node = document.querySelector('script[type="application/ld+json"]');
                if (!node) return null;
                try { return JSON.parse(node.textContent); } catch (err) { return null; }
            }"""
        )
        if not book_meta or not book_meta.get("@Id"):
            raise RuntimeError("could not resolve numeric bookId from page metadata")
        book_num_id = str(book_meta["@Id"])
        detected_title = book_meta.get("name") or ""
        detected_author = (book_meta.get("author") or {}).get("name") or ""
        book_title = book_title or detected_title
        author = author or detected_author

        chapters = [
            ch for ch in await fetch_chapter_infos(page, book_num_id)
            if ch.get("level") == 1 and ch.get("title") != "封面"
        ]
        (book_dir / "_catalog.json").write_text(
            json.dumps(chapters, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        all_images = []
        for idx, chapter in enumerate(chapters, 1):
            title = chapter["title"]
            uid = chapter["chapterUid"]
            print(f"[{idx:02d}/{len(chapters):02d}] {title}")
            xhtml = await fetch_chapter_xhtml(page, book_num_id, uid)
            md, images = xhtml_to_markdown(xhtml, title, idx, book_num_id)
            chapter_path = chapters_dir / f"{idx:04d}_{safe_name(title)}.md"
            chapter_path.write_text(md, encoding="utf-8")
            raw_payload = {
                "chapterUid": uid,
                "chapterIdx": chapter.get("chapterIdx"),
                "title": title,
                "wordCount": chapter.get("wordCount"),
                "xhtmlChars": len(xhtml),
                "markdownChars": len(md),
                "images": images,
            }
            (raw_dir / f"{idx:04d}_{safe_name(title)}.json").write_text(
                json.dumps(raw_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (raw_dir / f"{idx:04d}_{safe_name(title)}.xhtml").write_text(xhtml, encoding="utf-8")
            all_images.extend(images)

        ok, fail = await download_images(page, book_dir, all_images)
        await ctx.close()

    print(f"完成: {len(chapters)} 章, 图片 ok={ok} fail={fail}")
    print(f"book_id={book_reader_id}")
    print(f"title={book_title}")
    print(f"author={author}")
    return book_reader_id, book_title, author


def main(argv):
    if len(argv) < 2:
        raise SystemExit("Usage: python export_api.py <reader_url_or_id> [title] [author]")
    title = argv[2] if len(argv) >= 3 else None
    author = argv[3] if len(argv) >= 4 else None
    asyncio.run(export_book(argv[1], title, author))


if __name__ == "__main__":
    main(sys.argv)
