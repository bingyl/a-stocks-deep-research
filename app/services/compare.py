from __future__ import annotations

import re
from typing import Any

SECTION_TITLES = [
    "1. 一句话结论",
    "2. 股性判定与估值框架",
    "3. 财务质量与成长（重点）",
    "4. 综合估值与同业对比（重点）",
    "5. 业务与护城河（简要）",
    "6. 公司动态与舆情（重点）",
    "7. 政策与宏观外溢（简要）",
    "8. 技术面（辅助，非主结论）",
    "9. 主要风险、催化与跟踪指标",
]


def _normalize_heading(text: str) -> str:
    s = re.sub(r"^#+\s*", "", (text or "").strip())
    s = re.sub(r"\s+", " ", s)
    return s


def split_report_sections(analysis: str) -> list[dict[str, str]]:
    """按 ## 标题切分报告；尽量对齐标准大纲顺序。"""
    raw = (analysis or "").replace("\r\n", "\n")
    # 去掉子智能体套话前缀
    raw = re.sub(
        r"^[\s\S]*?(?=#{1,3}\s*\d+\.\s*一句话结论)",
        "",
        raw,
        count=1,
    ).strip()
    if not raw.strip():
        return [{"title": "全文", "body": (analysis or "").strip()}]

    parts = re.split(r"(?m)^(#{1,3}\s+.+)$", raw)
    # parts: [preamble, h1, body1, h2, body2, ...]
    sections: list[dict[str, str]] = []
    if parts and parts[0].strip():
        sections.append({"title": "前言", "body": parts[0].strip()})

    i = 1
    while i + 1 < len(parts):
        title = _normalize_heading(parts[i])
        body = (parts[i + 1] or "").strip()
        sections.append({"title": title, "body": body})
        i += 2

    if not sections:
        return [{"title": "全文", "body": raw.strip()}]
    return sections


def _section_key(title: str) -> str:
    t = _normalize_heading(title)
    m = re.match(r"(\d+)\.", t)
    if m:
        return m.group(1)
    for idx, std in enumerate(SECTION_TITLES, start=1):
        if std in t or t in std:
            return str(idx)
    return t


def build_compare_payload(reports: list[dict[str, Any]]) -> dict[str, Any]:
    parsed = []
    for r in reports:
        secs = split_report_sections(str(r.get("analysis") or ""))
        by_key = {_section_key(s["title"]): s for s in secs}
        parsed.append(
            {
                "id": r.get("id"),
                "code": r.get("code") or "",
                "name": r.get("name") or "",
                "created_at": r.get("created_at") or "",
                "model": r.get("model") or "",
                "preview": "",
                "sections": secs,
                "by_key": by_key,
            }
        )

    # 统一章节顺序：标准大纲 + 双方出现过的其它标题
    keys: list[str] = [str(i) for i in range(1, 10)]
    extra: list[str] = []
    for p in parsed:
        for k, sec in p["by_key"].items():
            if k not in keys and k not in extra and k not in {"前言"}:
                extra.append(k)
    if any("前言" in p["by_key"] for p in parsed):
        keys = ["前言", *keys]
    keys.extend(extra)

    rows: list[dict[str, Any]] = []
    for key in keys:
        title = None
        cells = []
        for p in parsed:
            sec = p["by_key"].get(key)
            if sec and not title:
                title = sec["title"]
            cells.append(
                {
                    "report_id": p["id"],
                    "title": (sec or {}).get("title") or "",
                    "body": (sec or {}).get("body") or "",
                }
            )
        if not any(c["body"] for c in cells):
            continue
        # 标准标题
        if key.isdigit():
            idx = int(key)
            if 1 <= idx <= len(SECTION_TITLES):
                title = SECTION_TITLES[idx - 1]
        rows.append({"key": key, "title": title or key, "cells": cells})

    left, right = parsed[0], parsed[1] if len(parsed) > 1 else None
    same_stock = bool(right and left["code"] and left["code"] == right["code"])
    return {
        "same_stock": same_stock,
        "reports": [
            {
                "id": p["id"],
                "code": p["code"],
                "name": p["name"],
                "created_at": p["created_at"],
                "model": p["model"],
            }
            for p in parsed
        ],
        "sections": rows,
    }
