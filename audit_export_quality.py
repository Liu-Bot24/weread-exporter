#!/usr/bin/env python3
"""Audit exported Markdown for structural corruption.

This is a guardrail for generated exports. It does not prove the book is
perfect, but it catches the failure modes that make a source unsafe for a
knowledge base: chapter starts cut in half, previous chapters swallowing the
next chapter's opening, inline chapter/postscript headings, and obvious
sentence fragments across chapter boundaries.
"""
import argparse
import re
import sys
from pathlib import Path


SENTENCE_END = tuple("。！？；：」）】》…—.!?")
INLINE_HEADING = re.compile(
    r"(.{4,})(第[一二三四五六七八九十百零0-9]+[章节篇]|前言|后记|结语)"
    r"[\u4e00-\u9fffA-Za-z0-9“”《》]+"
)


def iter_chapter_files(path):
    path = Path(path)
    if path.is_dir() and (path / "按目录拆分").exists():
        path = path / "按目录拆分"
    if not path.is_dir():
        raise SystemExit(f"not a directory: {path}")
    return sorted(path.glob("*.md"))


def visible_text_lines(text):
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("!["):
            continue
        lines.append(line)
    return lines


def audit_file(path, text, strict=False):
    issues = []
    lines = visible_text_lines(text)
    if not lines:
        issues.append(("empty", 1, "章节没有正文"))
        return issues

    head = lines[0]
    if len(head) <= 8 and head.endswith(SENTENCE_END):
        issues.append(("cut_head", 1, f"章节开头疑似断头: {head}"))

    if not strict:
        return issues

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = INLINE_HEADING.search(line)
        if match:
            issues.append(("inline_heading", lineno, f"标题疑似粘入正文: {line[:140]}"))
        if len(line) > 120 and re.search(r"[\u4e00-\u9fff][A-Za-z][\u4e00-\u9fff]", line):
            issues.append(("mixed_chars", lineno, f"中英文字符疑似交叉: {line[:140]}"))
    return issues


def audit_boundaries(chapters):
    issues = []
    parsed = []
    for path in chapters:
        text = path.read_text(encoding="utf-8")
        lines = visible_text_lines(text)
        parsed.append((path, text, lines))

    for (prev_path, _prev_text, prev_lines), (next_path, _next_text, next_lines) in zip(parsed, parsed[1:]):
        if not prev_lines or not next_lines:
            continue
        tail = prev_lines[-1]
        head = next_lines[0]
        if "版权信息" not in prev_path.stem and not tail.endswith(SENTENCE_END):
            issues.append((
                "open_tail",
                prev_path,
                0,
                f"章节末尾缺少收束标点，可能续到下一章: {tail[-100:]} / 下一章开头: {head[:80]}",
            ))
        if len(head) <= 8 and head.endswith(SENTENCE_END):
            issues.append((
                "cut_head_boundary",
                next_path,
                0,
                f"下一章开头是短残句: {head} / 上一章末尾: {tail[-100:]}",
            ))
    return issues


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="final export directory or 按目录拆分 directory")
    parser.add_argument("--max-examples", type=int, default=80)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="enable noisy heuristic checks for inline chapter references and mixed CJK/Latin text",
    )
    args = parser.parse_args(argv)

    chapters = iter_chapter_files(args.path)
    all_issues = []
    for path in chapters:
        text = path.read_text(encoding="utf-8")
        for kind, line, detail in audit_file(path, text, strict=args.strict):
            all_issues.append((kind, path, line, detail))
    all_issues.extend(audit_boundaries(chapters))

    print(f"chapters={len(chapters)} issues={len(all_issues)}")
    for kind, path, line, detail in all_issues[: args.max_examples]:
        loc = f"{path}:{line}" if line else str(path)
        print(f"{kind}\t{loc}\t{detail}")

    return 1 if all_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
