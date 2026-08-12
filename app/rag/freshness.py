"""知识库时效判断。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        # 常见网页日期：2026-07-30 / 2026-07-30T12:00:00+08:00
        if len(text) == 10 and text[4] == "-" and text[7] == "-":
            text = text + "T00:00:00+00:00"
        text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_created_at(meta: dict[str, Any] | None) -> datetime | None:
    if not meta:
        return None
    ts = meta.get("created_at_ts")
    if ts is not None:
        dt = _parse_dt(ts)
        if dt is not None:
            return dt
    return _parse_dt(meta.get("created_at"))


def _event_time(meta: dict[str, Any] | None) -> datetime | None:
    """时效锚点：网页优先发布日，否则入库时间。"""
    if not meta:
        return None
    source = str(meta.get("source_type") or "")
    tool = str(meta.get("tool") or "")
    is_web = source == "web" or tool in {
        "web_search",
        "search_company_news",
        "search_policy_impact",
        "search_macro_international",
    }
    if is_web:
        pub = _parse_dt(meta.get("source_published_at"))
        if pub is not None:
            return pub
    return _parse_created_at(meta)


def age_hours(meta: dict[str, Any] | None, *, now: datetime | None = None) -> float | None:
    """距事件时间（网页=发布日，其它=入库）过去多少小时。"""
    created = _event_time(meta)
    if created is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max(0.0, (now - created).total_seconds() / 3600.0)


def is_stale(
    meta: dict[str, Any] | None,
    *,
    stale_hours: int,
    now: datetime | None = None,
) -> bool:
    """无时间戳或超过阈值视为过期。"""
    if stale_hours <= 0:
        return False
    hours = age_hours(meta, now=now)
    if hours is None:
        return True
    return hours >= float(stale_hours)


def summarize_freshness(
    metas: list[dict[str, Any]],
    *,
    stale_hours: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """汇总一批命中的时效信息。"""
    now = now or datetime.now(timezone.utc)
    ages: list[float] = []
    stale_count = 0
    web_stale = 0
    for meta in metas:
        hours = age_hours(meta, now=now)
        item_stale = is_stale(meta, stale_hours=stale_hours, now=now)
        if item_stale:
            stale_count += 1
            if (meta or {}).get("source_type") == "web" or (meta or {}).get("tool") in {
                "web_search",
                "search_company_news",
                "search_policy_impact",
                "search_macro_international",
            }:
                web_stale += 1
        if hours is not None:
            ages.append(hours)

    empty = len(metas) == 0
    fresh_count = 0 if empty else (len(metas) - stale_count)
    requires_web_refresh = empty or fresh_count == 0
    any_stale = empty or stale_count > 0

    return {
        "stale_threshold_hours": stale_hours,
        "checked_at": now.isoformat(),
        "item_count": len(metas),
        "stale_count": stale_count,
        "fresh_count": fresh_count,
        "web_stale_count": web_stale,
        "max_age_hours": round(max(ages), 2) if ages else None,
        "min_age_hours": round(min(ages), 2) if ages else None,
        "stale": any_stale,
        "requires_web_refresh": requires_web_refresh,
    }
