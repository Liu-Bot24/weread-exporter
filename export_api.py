#!/usr/bin/env python3
"""Export a WeRead EPUB through the reader content API.

This avoids the canvas/page-turning path. Each Markdown file is built from the
book's own chapter XHTML, so chapter boundaries come from chapterUid rather
than from whichever visual page happened to be visible.
"""
import asyncio
import base64
import hashlib
import html
import html.entities
import io
import json
import math
import os
import re
import shutil
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree as ET

from playwright.async_api import async_playwright

from export_precise import USER_DATA_DIR, prepare_user_data_dir


RUNTIME_DIR = Path(os.environ.get("WEREAD_RUNTIME_DIR", ".runtime"))
EXPORT_DIR = Path(os.environ.get("WEREAD_EXPORT_DIR", RUNTIME_DIR / "exports"))
ARCHIVE_DIR = Path(os.environ.get("WEREAD_ARCHIVE_DIR", RUNTIME_DIR / "archives"))
CHROME_EXECUTABLE = os.environ.get("WEREAD_CHROME_EXECUTABLE")

INVALID = re.compile(r'[<>:"/\\|?*\n\r\t]+')
SENTENCE_SPACE = re.compile(r"\s+")
SPECIAL_CHAPTER_UID_RANGES = {
    # WeRead's chapterInfos lists only part dividers for this book. The hidden
    # chapter UIDs after 30 are not monotonic: 36 belongs to part 6, while
    # 31-35 belong to part 7. UID 29 is an invalid placeholder.
    "dd332b80813ab9b89g012936": {
        27: [27, 28, 36],
        30: [30, 31, 32, 33, 34, 35],
    },
}
TAIL_SCAN_MAX_UIDS = int(os.environ.get("WEREAD_TAIL_SCAN_MAX_UIDS", "80"))
TAIL_SCAN_INVALID_STREAK = int(os.environ.get("WEREAD_TAIL_SCAN_INVALID_STREAK", "6"))


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


def split_xhtml_documents(xhtml):
    parts = [
        part for part in re.split(r"(?=<\?xml\b)", xhtml)
        if part.strip() and "<html" in part and "</html>" in part
    ]
    if parts:
        return parts
    if xhtml.strip() and "<html" in xhtml and "</html>" in xhtml:
        return [xhtml]
    return []


def normalize_xhtml_entities(xhtml):
    xml_entities = {"amp", "lt", "gt", "quot", "apos"}

    def replace(match):
        name = match.group(1)
        if name in xml_entities:
            return match.group(0)
        value = html.entities.html5.get(name + ";")
        if value is None:
            return match.group(0)
        return value

    return re.sub(r"&([A-Za-z][A-Za-z0-9]+);", replace, xhtml)


def dedupe_xhtml_attributes(xhtml):
    attr_re = re.compile(
        r"\s+([:\w.-]+)(\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s\"'=<>`]+))?"
    )

    def clean_tag(match):
        tag = match.group(0)
        if tag.startswith(("<!--", "<?", "<!")) or tag.startswith("</"):
            return tag

        close = "/>" if tag.endswith("/>") else ">"
        inner = tag[1:-len(close)].strip()
        if not inner:
            return tag

        name_match = re.match(r"([^\s/>]+)", inner)
        if not name_match:
            return tag

        name = name_match.group(1)
        rest = inner[name_match.end():]
        seen = set()

        def replace_attr(attr_match):
            attr_name = attr_match.group(1)
            key = attr_name.lower()
            if key in seen:
                return ""
            seen.add(key)
            return attr_match.group(0)

        return "<" + name + attr_re.sub(replace_attr, rest) + close

    return re.sub(r"<[^<>]+>", clean_tag, xhtml)


def append_xhtml_blocks(xhtml, title, chapter_index, book_num_id, lines, images, image_seen):
    documents = split_xhtml_documents(xhtml)
    if not documents:
        return
    if len(documents) > 1:
        for document in documents:
            append_xhtml_blocks(document, title, chapter_index, book_num_id, lines, images, image_seen)
        return

    xhtml = dedupe_xhtml_attributes(normalize_xhtml_entities(xhtml))
    root = ET.fromstring(xhtml.encode("utf-8"))
    body = next((node for node in root.iter() if local_name(node.tag) == "body"), root)

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
            for child in node.iter():
                if child is not node and local_name(child.tag) == "img":
                    emit_image(child)
            heading = normalize_text(node.attrib.get("title")) or inline_text(node)
            if heading and heading != title:
                level = min(max(int(tag[1]), 2), 6)
                lines.append("#" * level + " " + heading)
                lines.append("")
            return
        if tag == "p":
            for child in node.iter():
                if child is not node and local_name(child.tag) == "img":
                    emit_image(child)
            text = inline_text(node)
            if text:
                lines.append(text)
                lines.append("")
            return
        for child in node:
            walk(child)

    for child in body:
        walk(child)


def xhtml_to_markdown_parts(xhtml_parts, title, chapter_index, book_num_id):
    lines = [f"# {title}", ""]
    images = []
    image_seen = {}
    for xhtml in xhtml_parts:
        append_xhtml_blocks(xhtml, title, chapter_index, book_num_id, lines, images, image_seen)
    blocks = [line for line in lines if line.strip()]
    return "\n\n".join(blocks) + "\n", images


def xhtml_to_markdown(xhtml, title, chapter_index, book_num_id):
    return xhtml_to_markdown_parts([xhtml], title, chapter_index, book_num_id)


def image_ext_from_bytes(data):
    if data.startswith(b"\x89PNG"):
        return "png"
    if data.startswith(b"\xff\xd8"):
        return "jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith(b"GIF8"):
        return "gif"
    return "jpg"


def tar_member_sort_key(member):
    parts = [int(value) for value in re.findall(r"\d+", member.name)]
    return parts or [0, member.name]


async def tar_images_to_markdown(page, tar_url, title, chapter_index, book_dir):
    response = await page.request.get(tar_url, timeout=30000)
    body = await response.body()
    if response.status != 200:
        raise RuntimeError(f"{tar_url} status={response.status}: {body[:120]!r}")

    images_dir = book_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    records = []
    members_meta = []
    lines = [f"# {title}", ""]
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tar:
        members = []
        for member in tar.getmembers():
            if not member.isfile():
                continue
            extracted = tar.extractfile(member)
            if not extracted:
                continue
            data = extracted.read()
            ext = image_ext_from_bytes(data)
            if ext == "jpg" and not data.startswith(b"\xff\xd8"):
                continue
            members.append((member, data, ext))

        for seq, (member, data, ext) in enumerate(sorted(members, key=lambda item: tar_member_sort_key(item[0])), 1):
            filename = f"ch{chapter_index:04d}_img{seq:02d}.{ext}"
            (images_dir / filename).write_bytes(data)
            records.append({
                "url": f"{tar_url}#{member.name}",
                "file": filename,
                "source": "tar",
            })
            members_meta.append({
                "name": member.name,
                "size": member.size,
                "file": filename,
            })
            lines.append(f"![图](images/{filename})")
            lines.append("")

    if not records:
        raise RuntimeError(f"{tar_url} contained no supported images")
    return "\n\n".join(line for line in lines if line.strip()) + "\n", records, members_meta, len(body)


def chapter_uid_ranges(chapters, book_reader_id=None):
    special = SPECIAL_CHAPTER_UID_RANGES.get(book_reader_id or "")
    chapter_uids = [int(chapter["chapterUid"]) for chapter in chapters]
    if special:
        return [special.get(uid, [uid]) for uid in chapter_uids]

    if chapter_uids != sorted(chapter_uids):
        return [[uid] for uid in chapter_uids]

    ranges = []
    for idx, chapter in enumerate(chapters):
        start = chapter_uids[idx]
        if idx + 1 < len(chapters):
            stop = chapter_uids[idx + 1]
        else:
            stop = start + 1
        ranges.append(list(range(start, stop)))
    return ranges


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


def plaintext_chapter_to_xhtml(content):
    paragraphs = [
        paragraph.strip()
        for paragraph in re.split(r"(?:\r?\n\s*)+", content or "")
        if paragraph.strip()
    ]
    body = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in paragraphs)
    return f"<?xml version=\"1.0\" encoding=\"utf-8\"?><html><body>{body}</body></html>"


def json_chapter_to_xhtml(raw):
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    content = data.get("content") if isinstance(data, dict) else None
    if not content:
        return None
    return plaintext_chapter_to_xhtml(content)


async def fetch_chapter_xhtml(page, book_num_id, chapter_uid):
    pieces = []
    for part_idx in (0, 1, 3):
        payload = make_chapter_params(book_num_id, chapter_uid, part_idx)
        raw = await request_text(
            page,
            f"https://weread.qq.com/web/book/chapter/e_{part_idx}",
            payload,
        )
        body = strip_response_hash(raw)
        plaintext_xhtml = json_chapter_to_xhtml(body)
        if plaintext_xhtml:
            return plaintext_xhtml
        pieces.append(body)
    try:
        xhtml = decode_segment_string("".join(pieces))
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"chapterUid={chapter_uid} did not decode cleanly: {exc}") from exc
    if "<html" not in xhtml or "</html>" not in xhtml:
        raise RuntimeError(f"chapterUid={chapter_uid} did not decode to XHTML")
    return xhtml


async def resolve_chapter_uid_ranges(page, book_num_id, chapters, book_reader_id=None):
    ranges = chapter_uid_ranges(chapters, book_reader_id)
    if not ranges or SPECIAL_CHAPTER_UID_RANGES.get(book_reader_id or ""):
        return ranges, {}

    cache = {}
    last_range = list(ranges[-1])
    seen = set(last_range)
    invalid_streak = 0
    next_uid = max(last_range) + 1
    upper = next_uid + TAIL_SCAN_MAX_UIDS
    while next_uid < upper and invalid_streak < TAIL_SCAN_INVALID_STREAK:
        try:
            cache[next_uid] = await fetch_chapter_xhtml(page, book_num_id, next_uid)
            if next_uid not in seen:
                last_range.append(next_uid)
                seen.add(next_uid)
            invalid_streak = 0
        except RuntimeError:
            invalid_streak += 1
        next_uid += 1
    ranges[-1] = last_range
    return ranges, cache


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
    if os.environ.get("WEREAD_SKIP_INLINE_IMAGE_DOWNLOAD", "").lower() in {"1", "true", "yes", "on"}:
        return 0, 0
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
        launch_options = {
            "headless": headless,
            "viewport": {"width": 1400, "height": 1000},
        }
        if CHROME_EXECUTABLE:
            launch_options["executable_path"] = CHROME_EXECUTABLE
        ctx = await p.chromium.launch_persistent_context(
            USER_DATA_DIR,
            **launch_options,
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

        chapter_infos = await fetch_chapter_infos(page, book_num_id)
        has_nested_catalog = any((ch.get("level") or 0) > 1 for ch in chapter_infos)
        has_level_catalog = any("level" in ch for ch in chapter_infos)
        if has_nested_catalog:
            chapters = [
                ch for ch in chapter_infos
                if (ch.get("level") or 0) >= 1 and ch.get("title") != "封面"
            ]
        elif not has_level_catalog:
            chapters = [
                ch for ch in chapter_infos
                if ch.get("title") != "封面"
            ]
        else:
            chapters = [
                ch for ch in chapter_infos
                if ch.get("level") == 1 and ch.get("title") != "封面"
            ]
        (book_dir / "_catalog_full.json").write_text(
            json.dumps(chapter_infos, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (book_dir / "_catalog.json").write_text(
            json.dumps(chapters, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        all_images = []
        if has_nested_catalog:
            uid_ranges = [[int(chapter["chapterUid"])] for chapter in chapters]
            xhtml_cache = {}
        else:
            uid_ranges, xhtml_cache = await resolve_chapter_uid_ranges(
                page, book_num_id, chapters, book_reader_id
            )
        catalog_uids = {int(chapter["chapterUid"]) for chapter in chapters}
        for idx, (chapter, uids) in enumerate(zip(chapters, uid_ranges), 1):
            title = chapter["title"]
            print(f"[{idx:02d}/{len(chapters):02d}] {title} ({uids[0]}-{uids[-1]})")
            xhtml_parts = []
            fetched_uids = []
            tar_image_members = []
            tar_body_bytes = 0
            for uid in uids:
                try:
                    if uid in xhtml_cache:
                        xhtml_parts.append(xhtml_cache[uid])
                    else:
                        xhtml_parts.append(await fetch_chapter_xhtml(page, book_num_id, uid))
                    fetched_uids.append(uid)
                except RuntimeError as exc:
                    label = "catalog" if int(uid) in catalog_uids else "implicit"
                    if int(uid) == int(chapter["chapterUid"]) and chapter.get("tar"):
                        print(f"  fallback {label} chapterUid={uid} to tar images: {exc}")
                        md, images, tar_image_members, tar_body_bytes = await tar_images_to_markdown(
                            page, chapter["tar"], title, idx, book_dir
                        )
                        fetched_uids.append(uid)
                        break
                    else:
                        print(f"  skip {label} chapterUid={uid}: {exc}")
            if not xhtml_parts and not tar_image_members:
                raise RuntimeError(f"no XHTML fetched for chapterUid={chapter['chapterUid']} {title}")
            if xhtml_parts:
                md, images = xhtml_to_markdown_parts(xhtml_parts, title, idx, book_num_id)
            chapter_path = chapters_dir / f"{idx:04d}_{safe_name(title)}.md"
            chapter_path.write_text(md, encoding="utf-8")
            raw_payload = {
                "chapterUid": chapter["chapterUid"],
                "sectionUids": fetched_uids,
                "chapterIdx": chapter.get("chapterIdx"),
                "title": title,
                "wordCount": chapter.get("wordCount"),
                "xhtmlChars": sum(len(part) for part in xhtml_parts),
                "markdownChars": len(md),
                "images": images,
            }
            if tar_image_members:
                raw_payload["tar"] = chapter.get("tar")
                raw_payload["tarBodyBytes"] = tar_body_bytes
                raw_payload["tarImages"] = tar_image_members
            (raw_dir / f"{idx:04d}_{safe_name(title)}.json").write_text(
                json.dumps(raw_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (raw_dir / f"{idx:04d}_{safe_name(title)}.xhtml").write_text(
                "\n\n".join(xhtml_parts),
                encoding="utf-8",
            )
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
