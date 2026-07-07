#!/usr/bin/env python3
"""Organize a WeRead export into a reader-friendly folder.

Usage:
  python organize_export.py <book_id> <book_title> <author>
"""
import re
import shutil
import sys
import os
from pathlib import Path


INVALID = re.compile(r'[<>:"/\\|?*\n\r\t]+')
RUNTIME_DIR = Path(os.environ.get("WEREAD_RUNTIME_DIR", ".runtime"))
EXPORT_DIR = Path(os.environ.get("WEREAD_EXPORT_DIR", RUNTIME_DIR / "exports"))
FINAL_OUTPUT_DIR = Path(os.environ.get("WEREAD_FINAL_OUTPUT_DIR", "output"))
ARCHIVE_DIR = Path(os.environ.get("WEREAD_ARCHIVE_DIR", RUNTIME_DIR / "archives"))


def safe_name(value, max_len=90):
    value = re.sub(r"^#+\s*", "", value).strip()
    value = INVALID.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:max_len].rstrip(" .") or "未命名")


def export_base(book_id):
    candidates = [EXPORT_DIR / book_id, Path("output") / book_id]
    for candidate in candidates:
        if (candidate / "chapters").exists():
            return candidate
    return candidates[0]


def organize(book_id, book_title, author):
    base = export_base(book_id)
    chapters_dir = base / "chapters"
    images_src = base / "images"
    if not chapters_dir.exists():
        raise SystemExit(f"missing chapters directory: {chapters_dir}")

    out_root = FINAL_OUTPUT_DIR / f"{safe_name(book_title, 120)}_整理版"
    full_out = out_root / f"{safe_name(book_title, 140)}_全书.md"
    split_dir = out_root / "按目录拆分"
    images_out = out_root / "images"
    index_out = out_root / "目录索引.md"

    if out_root.exists():
        backup_root = ARCHIVE_DIR / "final_output_backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        backup = backup_root / (out_root.name + "_旧版备份")
        n = 1
        while backup.exists():
            backup = backup_root / (out_root.name + f"_旧版备份_{n}")
            n += 1
        shutil.move(str(out_root), str(backup))

    out_root.mkdir(parents=True, exist_ok=True)
    split_dir.mkdir(parents=True, exist_ok=True)
    if images_src.exists():
        shutil.copytree(images_src, images_out)
    else:
        images_out.mkdir(parents=True, exist_ok=True)

    chapter_files = sorted(chapters_dir.glob("*.md"))
    index_rows = []
    full_parts = [f"# {book_title}\n\n**{author}**\n\n---\n"]
    used_names = set()

    for idx, src in enumerate(chapter_files, 1):
        text = src.read_text(encoding="utf-8")
        first = next((line.strip() for line in text.splitlines() if line.strip()), f"# 第{idx:04d}章")
        title = re.sub(r"^#+\s*", "", first).strip() or f"第{idx:04d}章"
        stem = f"{idx:04d}_{safe_name(title)}"
        filename = stem + ".md"
        dedupe = 2
        while filename in used_names:
            filename = f"{stem}_{dedupe}.md"
            dedupe += 1
        used_names.add(filename)

        split_text = text.replace("](images/", "](../images/")
        (split_dir / filename).write_text(split_text, encoding="utf-8")
        full_parts.append(text.rstrip() + "\n\n---\n")
        index_rows.append((idx, title, filename))

    full_out.write_text("\n".join(full_parts).rstrip() + "\n", encoding="utf-8")
    with index_out.open("w", encoding="utf-8") as handle:
        handle.write(f"# {book_title} 目录索引\n\n")
        handle.write(f"- 作者：{author}\n")
        handle.write(f"- 章节数：{len(index_rows)}\n")
        handle.write(f"- 图片数：{len(list(images_out.glob('*')))}\n")
        handle.write(f"- 全书 Markdown：[{full_out.name}]({full_out.name})\n")
        handle.write("- 拆分章节目录：`按目录拆分/`\n\n")
        for idx, title, filename in index_rows:
            handle.write(f"{idx}. [{title}](按目录拆分/{filename})\n")

    missing = []
    refs = 0
    for md_file in [full_out] + sorted(split_dir.glob("*.md")):
        text = md_file.read_text(encoding="utf-8")
        for ref in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text):
            refs += 1
            if not (md_file.parent / ref).resolve().exists():
                missing.append((str(md_file), ref))

    return {
        "out_root": out_root.resolve(),
        "full": full_out.resolve(),
        "index": index_out.resolve(),
        "chapters": len(index_rows),
        "images": len(list(images_out.glob("*"))),
        "image_refs": refs,
        "missing_refs": len(missing),
        "missing_examples": missing[:20],
    }


def main(argv):
    if len(argv) != 4:
        raise SystemExit(__doc__.strip())
    result = organize(argv[1], argv[2], argv[3])
    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main(sys.argv)
