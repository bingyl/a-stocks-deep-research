"""父子文档切片：文本用 RecursiveCharacterTextSplitter，JSON 用 RecursiveJsonSplitter。"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter, RecursiveJsonSplitter

from app.core.config import get_settings
from app.core.logging import log_caught
from app.persistence.vectorstore.types import ChildDocument, ParentDocument

logger = logging.getLogger(__name__)

WEB_TOOLS = {
    "web_search",
    "search_company_news",
    "search_policy_impact",
    "search_macro_international",
}

# 父文档：递归切分，但不含空分隔符，避免硬切到句子中间
_PARENT_SEPARATORS = ["\n\n", "\n", "。", "；", "！", "？", ". ", ";", " "]
# 子文档：可更细（含字符级兜底）
_CHILD_SEPARATORS = ["\n\n", "\n", "。", "；", "！", "？", ".", ";", " ", ""]


def stable_id(*parts: str) -> str:
    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


# 兼容旧名
_stable_id = stable_id


def _short(text: str, n: int = 80) -> str:
    s = (text or "").replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def detect_source_type(tool: str, output: Any) -> str:
    if tool in WEB_TOOLS:
        return "web"
    if isinstance(output, (dict, list)):
        return "json"
    if isinstance(output, str):
        s = output.strip()
        if s.startswith("{") or s.startswith("["):
            try:
                json.loads(s)
                return "json"
            except json.JSONDecodeError:
                pass
        return "text"
    return "text"


def parse_tool_output(output: Any) -> Any:
    if isinstance(output, (dict, list)):
        return output
    if output is None:
        return None
    if hasattr(output, "content"):
        return parse_tool_output(getattr(output, "content"))
    text = str(output)
    s = text.strip()
    if s.startswith("{") or s.startswith("["):
        try:
            return json.loads(s)
        except json.JSONDecodeError:
            return text
    return text


def _char_splitter(
    chunk_size: int,
    overlap: int,
    *,
    separators: list[str],
) -> RecursiveCharacterTextSplitter:
    return RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=overlap,
        separators=separators,
    )


def _split_text_parent_child(
    text: str,
    *,
    parent_id: str,
    base_meta: dict[str, Any],
) -> tuple[ParentDocument, list[tuple[str, str]]]:
    """返回 parent + [(child_id, child_text), ...]。"""
    settings = get_settings()
    if str((base_meta or {}).get("source_type") or "") == "json":
        child_size = max(100, settings.rag_json_child_chunk_size)
        overlap = max(0, settings.rag_json_child_chunk_overlap)
    else:
        child_size = max(100, settings.rag_child_chunk_size)
        overlap = max(0, settings.rag_child_chunk_overlap)

    parent_text = (text or "").strip()
    parent = ParentDocument(id=parent_id, text=parent_text, metadata=dict(base_meta))
    if not parent_text:
        return parent, []

    chunks = (
        _char_splitter(child_size, overlap, separators=_CHILD_SEPARATORS).split_text(
            parent_text
        )
        or [parent_text]
    )
    children: list[tuple[str, str]] = []
    for i, chunk in enumerate(chunks):
        children.append((f"{parent_id}:c{i}", chunk))
    return parent, children


def _format_web_item(item: Any, idx: int) -> str:
    if not isinstance(item, dict):
        return str(item)
    title = item.get("title") or item.get("name") or ""
    url = item.get("url") or item.get("link") or ""
    snippet = (
        item.get("snippet")
        or item.get("summary")
        or item.get("content")
        or item.get("description")
        or ""
    )
    published = item.get("published") or item.get("datePublished") or ""
    site = item.get("site") or item.get("siteName") or ""
    parts = [f"[{idx}] {title}".strip(), url, site, published, snippet]
    return "\n".join(p for p in parts if p)


def _web_item_meta(item: Any, base: dict[str, Any], parent_key: str) -> dict[str, Any]:
    meta = {**base, "parent_key": parent_key}
    if isinstance(item, dict):
        published = item.get("published") or item.get("datePublished") or ""
        if published:
            meta["source_published_at"] = str(published)
        url = item.get("url") or item.get("link") or ""
        if url:
            meta["source_url"] = str(url)
    return meta


def _expand_oversized_parent(text: str, *, max_size: int) -> list[str]:
    """过长父文本用递归字符切分拆成多个 parent（无空分隔符硬切）。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_size:
        return [text]
    parts = _char_splitter(
        max_size, 0, separators=_PARENT_SEPARATORS
    ).split_text(text)
    return [p.strip() for p in (parts or [text]) if p.strip()] or [text]


def build_parent_units(
    tool: str,
    arguments: dict[str, Any] | None,
    output: Any,
    *,
    report_id: str,
    code: str,
    stage: str = "",
) -> list[tuple[str, str, dict[str, Any]]]:
    """拆成若干 parent 单元：(parent_id, text, metadata)。"""
    parsed = parse_tool_output(output)
    source_type = detect_source_type(tool, parsed)
    args = arguments or {}
    arg_key = _stable_id(json.dumps(args, ensure_ascii=False, sort_keys=True, default=str))
    now = datetime.now(timezone.utc)
    settings = get_settings()
    # 文本/网页用通用尺寸；JSON 用单独放宽的尺寸
    parent_size = max(200, settings.rag_parent_chunk_size)
    json_parent_size = max(200, settings.rag_json_parent_chunk_size)
    base = {
        "report_id": report_id,
        "code": code,
        "tool": tool,
        "source_type": source_type,
        "stage": stage or "",
        "created_at": now.isoformat(),
        "created_at_ts": int(now.timestamp()),
    }

    units: list[tuple[str, str, dict[str, Any]]] = []

    if source_type == "web" and isinstance(parsed, dict):
        items = None
        for path in (
            ("results",),
            ("data", "webPages", "value"),
            ("data", "results"),
            ("webPages", "value"),
            ("items",),
        ):
            cur: Any = parsed
            ok = True
            for key in path:
                if isinstance(cur, dict) and key in cur:
                    cur = cur[key]
                else:
                    ok = False
                    break
            if ok and isinstance(cur, list) and cur:
                items = cur
                break
        if items:
            for i, item in enumerate(items):
                text = _format_web_item(item, i)
                if len(text.strip()) < 8:
                    continue
                parts = _expand_oversized_parent(text, max_size=parent_size)
                for j, part in enumerate(parts):
                    suffix = "" if len(parts) == 1 else f"p{j}"
                    pid = f"{report_id}:{tool}:{arg_key}:w{i}{suffix}"
                    units.append(
                        (pid, part, _web_item_meta(item, base, f"w{i}{suffix}"))
                    )
            if units:
                return units

    if source_type == "json" and isinstance(parsed, (dict, list)):
        try:
            splitter = RecursiveJsonSplitter(max_chunk_size=json_parent_size)
            docs = splitter.split_text(
                json_data=parsed if isinstance(parsed, dict) else {"items": parsed},
                convert_lists=True,
                ensure_ascii=False,
            )
            for i, chunk in enumerate(docs or []):
                text = (
                    chunk
                    if isinstance(chunk, str)
                    else json.dumps(chunk, ensure_ascii=False)
                )
                if len(text.strip()) < 8:
                    continue
                pid = f"{report_id}:{tool}:{arg_key}:j{i}"
                units.append((pid, text, {**base, "parent_key": f"j{i}"}))
            if units:
                return units
        except Exception as exc:
            log_caught(
                logger,
                "JSON 切分失败，回退为文本切分",
                exc=exc,
                level=logging.DEBUG,
            )

    # 文本 / 兜底
    if isinstance(parsed, (dict, list)):
        text = json.dumps(parsed, ensure_ascii=False, default=str)
    else:
        text = str(parsed or "")
    if len(text) > settings.rag_max_output_chars:
        logger.debug(
            "工具输出过长已截断：%s，%s → %s 字符",
            tool,
            len(text),
            settings.rag_max_output_chars,
        )
        text = text[: settings.rag_max_output_chars]
    if len(text.strip()) < 8:
        return []

    parts = _expand_oversized_parent(text, max_size=parent_size)
    for i, part in enumerate(parts):
        pid = f"{report_id}:{tool}:{arg_key}:t{i}"
        units.append((pid, part, {**base, "parent_key": f"t{i}"}))
    return units


def split_to_parent_child(
    units: list[tuple[str, str, dict[str, Any]]],
) -> tuple[list[ParentDocument], list[ChildDocument]]:
    """units -> parents + children（children 尚无 vector，vector 稍后填）。"""
    parents: list[ParentDocument] = []
    children: list[ChildDocument] = []
    for parent_id, text, meta in units:
        parent, child_pairs = _split_text_parent_child(
            text, parent_id=parent_id, base_meta=meta
        )
        parents.append(parent)
        for cid, ctext in child_pairs:
            children.append(
                ChildDocument(
                    id=cid,
                    parent_id=parent_id,
                    text=ctext,
                    vector=[],
                    metadata={**meta, "preview": _short(ctext)},
                )
            )
    return parents, children
