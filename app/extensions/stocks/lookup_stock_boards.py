#!/usr/bin/env python3
"""
根据股票代码或名称，查询所属行业板块与概念板块。

用法：
  python -m app.extensions.stocks.lookup_stock_boards 贵州茅台
  python -m app.extensions.stocks.lookup_stock_boards 600519
  python -m app.extensions.stocks.lookup_stock_boards 宁德时代 --json

数据来源：东方财富 F10 核心题材 / 公开行情接口，非官方授权。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from curl_cffi import requests as cf_requests

from app.extensions.stocks.fetch_concept_boards import (
    BOARD_KINDS,
    DEFAULT_DATA_DIR,
    fetch_board_list,
)

IMPERSONATE = "chrome131"
SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326A8"
CORE_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/CoreConception/PageAjax"
CACHE_TTL_SEC = 6 * 3600

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"stdout/stderr reconfigure skipped: {type(exc).__name__}: {exc}", file=sys.stderr)


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    referer: str,
    retries: int = 3,
) -> dict[str, Any]:
    last_err: Exception | None = None
    headers = {"Referer": referer, "Accept": "*/*"}
    for attempt in range(1, retries + 1):
        try:
            resp = cf_requests.get(
                url,
                params=params,
                impersonate=IMPERSONATE,
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            print(
                f"http_get_json retry: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )
            time.sleep(0.8 * attempt)
    raise RuntimeError(f"请求失败: {url} ({last_err})") from last_err


def normalize_board_code(code: Any) -> str:
    text = str(code).strip().upper()
    if text.startswith("BK"):
        return text
    if text.isdigit():
        return "BK" + text.zfill(4)
    return text


def to_f10_code(code: str, mkt_num: str | int | None = None, security_type_name: str = "") -> str:
    code = str(code).strip().upper()
    if code.startswith(("SH", "SZ", "BJ")) and len(code) >= 8:
        return code
    # 600519.SH / 000001.SZ
    if "." in code:
        num, mkt = code.split(".", 1)
        mkt = mkt.upper()
        if mkt in {"SH", "SS"}:
            return f"SH{num.zfill(6)}"
        if mkt in {"SZ"}:
            return f"SZ{num.zfill(6)}"
        if mkt in {"BJ"}:
            return f"BJ{num.zfill(6)}"
        code = num

    pure = "".join(ch for ch in code if ch.isdigit()).zfill(6)
    name = security_type_name or ""
    if "沪" in name or str(mkt_num) == "1":
        return f"SH{pure}"
    if "深" in name or str(mkt_num) in {"0", "2"}:
        return f"SZ{pure}"
    if "京" in name or str(mkt_num) in {"0", "90"} and pure.startswith(("8", "4", "9")):
        # 北交所常见 8/4 开头；部分接口 mkt 不统一，按代码兜底
        if pure.startswith(("8", "4")) or pure.startswith("92"):
            return f"BJ{pure}"

    # 代码规则兜底
    if pure.startswith(("5", "6", "9")) and not pure.startswith(("90", "92")):
        # 9 开头也可能是 B 股等，优先沪市 60/68
        if pure.startswith(("60", "68", "51", "50")):
            return f"SH{pure}"
    if pure.startswith(("60", "68")):
        return f"SH{pure}"
    if pure.startswith(("00", "30", "12", "15")):
        return f"SZ{pure}"
    if pure.startswith(("8", "4", "92")):
        return f"BJ{pure}"
    # 默认深市（兼容 000xxx）
    return f"SZ{pure}"


def search_stock(query: str) -> list[dict[str, str]]:
    data = http_get_json(
        SUGGEST_URL,
        params={
            "input": query,
            "type": "14",
            "token": SUGGEST_TOKEN,
            "count": "10",
        },
        referer="https://www.eastmoney.com/",
    )
    rows = ((data.get("QuotationCodeTable") or {}).get("Data")) or []
    results = []
    for row in rows:
        classify = str(row.get("Classify") or "")
        st_name = str(row.get("SecurityTypeName") or "")
        is_a = classify == "AStock" or st_name in {"沪A", "深A", "京A", "A股"}
        if not is_a:
            continue
        code = str(row.get("Code") or row.get("UnifiedCode") or "").strip()
        name = str(row.get("Name") or "").strip()
        if not code or not name:
            continue
        f10 = to_f10_code(
            code,
            mkt_num=row.get("MktNum"),
            security_type_name=st_name,
        )
        results.append(
            {
                "code": code.zfill(6) if code.isdigit() else code,
                "name": name,
                "f10_code": f10,
                "market": st_name,
            }
        )
    # 去重
    seen = set()
    uniq = []
    for item in results:
        key = item["f10_code"]
        if key in seen:
            continue
        seen.add(key)
        uniq.append(item)
    return uniq


def resolve_stock(query: str) -> dict[str, str]:
    q = query.strip()
    if not q:
        raise ValueError("请输入股票代码或名称")

    # 纯代码时也走搜索，顺便拿名称；失败再本地拼
    hits = search_stock(q)
    if not hits:
        # 直接当代码
        if any(ch.isdigit() for ch in q):
            f10 = to_f10_code(q)
            return {"code": "".join(c for c in q if c.isdigit()).zfill(6), "name": q, "f10_code": f10, "market": ""}
        raise RuntimeError(f"未找到股票: {q}")

    # 精确优先：代码全等 / 名称全等
    q_digits = "".join(c for c in q if c.isdigit())
    for item in hits:
        if q_digits and item["code"] == q_digits.zfill(6):
            return item
        if item["name"] == q:
            return item
    return hits[0]


def load_board_maps(data_dir: Path, *, refresh: bool = False) -> dict[str, dict[str, str]]:
    """返回 {industry: {BKxxxx: name}, concept: {...}}，带本地缓存。"""
    cache_path = data_dir / "board_maps_cache.json"
    data_dir.mkdir(parents=True, exist_ok=True)
    if cache_path.exists() and not refresh:
        age = time.time() - cache_path.stat().st_mtime
        if age < CACHE_TTL_SEC:
            return json.loads(cache_path.read_text(encoding="utf-8"))

    maps: dict[str, dict[str, str]] = {}
    for kind in ("industry", "concept"):
        df = fetch_board_list(kind, retries=3, retry_delay=1.5)
        maps[kind] = {
            normalize_board_code(r["板块代码"]): str(r["板块名称"])
            for _, r in df.iterrows()
        }
    cache_path.write_text(json.dumps(maps, ensure_ascii=False), encoding="utf-8")
    return maps


def fetch_stock_boards(f10_code: str) -> list[dict[str, Any]]:
    data = http_get_json(
        CORE_URL,
        params={"code": f10_code},
        referer="https://emweb.securities.eastmoney.com/",
    )
    rows = data.get("ssbk") or []
    out = []
    for row in rows:
        out.append(
            {
                "board_code": normalize_board_code(row.get("BOARD_CODE")),
                "board_name": str(row.get("BOARD_NAME") or "").strip(),
                "rank": row.get("BOARD_RANK"),
            }
        )
    return out


def classify_boards(
    boards: list[dict[str, Any]], maps: dict[str, dict[str, str]]
) -> dict[str, list[dict[str, str]]]:
    industry, concept, other = [], [], []
    for b in boards:
        code = b["board_code"]
        name = b["board_name"]
        item = {"board_code": code, "board_name": name}
        if code in maps.get("industry", {}):
            industry.append(item)
        elif code in maps.get("concept", {}):
            concept.append(item)
        else:
            other.append(item)
    return {"industry": industry, "concept": concept, "other": other}


def lookup(query: str, *, data_dir: Path, refresh_maps: bool = False) -> dict[str, Any]:
    stock = resolve_stock(query)
    boards = fetch_stock_boards(stock["f10_code"])
    maps = load_board_maps(data_dir, refresh=refresh_maps)
    classified = classify_boards(boards, maps)
    return {
        "query": query,
        "stock": stock,
        "industry": classified["industry"],
        "concept": classified["concept"],
        "other": classified["other"],
        "source": "eastmoney-f10-coreconception",
    }


def print_result(result: dict[str, Any]) -> None:
    stock = result["stock"]
    print(
        f"股票: {stock['name']} ({stock['code']})  "
        f"[{stock.get('market') or stock['f10_code']}]"
    )
    print(f"\n所属行业（{len(result['industry'])}）:")
    if result["industry"]:
        for b in result["industry"]:
            print(f"  - {b['board_name']} ({b['board_code']})")
    else:
        print("  （无）")

    print(f"\n所属概念（{len(result['concept'])}）:")
    if result["concept"]:
        for b in result["concept"]:
            print(f"  - {b['board_name']} ({b['board_code']})")
    else:
        print("  （无）")

    if result["other"]:
        print(f"\n其他板块（地域/风格等，{len(result['other'])}）:")
        for b in result["other"]:
            print(f"  - {b['board_name']} ({b['board_code']})")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="按股票代码/名称查询所属行业与概念")
    p.add_argument("query", help="股票代码或名称，如 600519 / 贵州茅台")
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument(
        "--refresh-maps",
        action="store_true",
        help="强制刷新行业/概念对照表缓存",
    )
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = lookup(
            args.query, data_dir=args.data_dir, refresh_maps=args.refresh_maps
        )
    except Exception as exc:  # noqa: BLE001
        print(f"查询失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
