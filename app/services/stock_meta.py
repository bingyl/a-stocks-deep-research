from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from pypinyin import Style, lazy_pinyin


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def clean_name(name: str) -> str:
    text = str(name or "").strip()
    text = re.sub(r"\s+", "", text)
    text = text.replace("Ａ", "A").replace("Ｂ", "B")
    return text


def to_pinyin(name: str) -> tuple[str, str]:
    letters = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", name)
    full = "".join(lazy_pinyin(letters, style=Style.NORMAL)).lower()
    initials = "".join(lazy_pinyin(letters, style=Style.FIRST_LETTER)).lower()
    return full, initials


def infer_market_board(code: str) -> tuple[str, str]:
    """由代码推断市场与板块（相对稳定）。"""
    code = (code or "").zfill(6)
    if code.startswith("68"):
        return "SH", "科创板"
    if code.startswith("6"):
        return "SH", "主板"
    if code.startswith(("4", "8")):
        return "BJ", "北交所"
    if code.startswith("3"):
        return "SZ", "创业板"
    if code.startswith(("0", "1", "2")):
        return "SZ", "主板"
    return "", "其他"


def normalize_code(raw: Any) -> Optional[str]:
    text = re.sub(r"\D", "", str(raw or ""))
    if len(text) != 6:
        return None
    return text
