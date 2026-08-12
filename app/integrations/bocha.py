from __future__ import annotations

from typing import Any

import httpx

from app.core.config import get_settings


async def bocha_web_search(
    query: str,
    *,
    count: int = 8,
    freshness: str = "oneYear",
    summary: bool = True,
) -> dict[str, Any]:
    """调用博查 Web Search，返回适合给 LLM 阅读的结构化结果。"""
    settings = get_settings()
    settings.require_bocha()

    url = f"{settings.bocha_base_url}/v1/web-search"
    headers = {
        "Authorization": f"Bearer {settings.bocha_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "count": max(1, min(int(count), 20)),
        "freshness": freshness or "noLimit",
        "summary": bool(summary),
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        data = resp.json()

    root = data.get("data") if isinstance(data.get("data"), dict) else data
    pages = (root or {}).get("webPages") or {}
    values = pages.get("value") or []

    results: list[dict[str, Any]] = []
    for item in values[: payload["count"]]:
        results.append(
            {
                "title": item.get("name") or item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": item.get("snippet") or "",
                "summary": item.get("summary") or item.get("snippet") or "",
                "site": item.get("siteName") or "",
                "published": item.get("datePublished") or item.get("dateLastCrawled") or "",
            }
        )

    return {
        "query": query,
        "count": len(results),
        "results": results,
        "raw_message": data.get("message") or data.get("msg") or "",
    }
