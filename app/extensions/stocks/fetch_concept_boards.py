#!/usr/bin/env python3
"""
拉取东方财富 A 股概念/行业板块及成分股。

用法：
  python -m app.extensions.stocks.fetch_concept_boards --help

输出（按类型，默认写在本包 data/）：
  data/concepts.csv / industries.csv
  data/concept_members.csv / industry_members.csv
  data/ashare_concepts.db
    boards / board_members / progress（含 board_type）

特性：curl_cffi 模拟浏览器、限速、失败重试、断点续跑。
数据来源：东方财富公开行情接口，非官方授权，仅供研究自用。
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
from curl_cffi import requests as cf_requests

ROOT = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = ROOT / "data"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"stdout/stderr reconfigure skipped: {type(exc).__name__}: {exc}", file=sys.stderr)

DB_NAME = "ashare_concepts.db"

# 东财板块类型：t:3=概念，t:2=行业
BOARD_KINDS: dict[str, dict[str, str]] = {
    "concept": {
        "label": "概念",
        "fs": "m:90 t:3 f:!50",
        "referer": "https://quote.eastmoney.com/center/boardlist.html#concept_board",
        "list_csv": "concepts.csv",
        "members_csv": "concept_members.csv",
    },
    "industry": {
        "label": "行业",
        "fs": "m:90 t:2 f:!50",
        "referer": "https://quote.eastmoney.com/center/boardlist.html#industry_board",
        "list_csv": "industries.csv",
        "members_csv": "industry_members.csv",
    },
}

CLIST_URLS = [
    "https://push2delay.eastmoney.com/api/qt/clist/get",
    "https://push2.eastmoney.com/api/qt/clist/get",
    "https://79.push2.eastmoney.com/api/qt/clist/get",
    "https://17.push2.eastmoney.com/api/qt/clist/get",
    "https://29.push2.eastmoney.com/api/qt/clist/get",
]

UT = "bd1d9ddb04089700cf9c27f6f7426281"
IMPERSONATE = "chrome131"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sleep_jitter(base: float) -> None:
    time.sleep(max(0.0, base + random.uniform(0.05, 0.35)))


def http_get_json(
    params: dict[str, Any],
    *,
    retries: int,
    retry_delay: float,
    label: str,
    referer: str,
) -> dict[str, Any]:
    last_err: Exception | None = None
    urls = CLIST_URLS[:]
    req_params = dict(params)
    headers = {
        "Referer": referer,
        "Accept": "*/*",
    }
    for attempt in range(1, retries + 1):
        for url in urls:
            try:
                req_params["_"] = str(int(time.time() * 1000))
                resp = cf_requests.get(
                    url,
                    params=req_params,
                    impersonate=IMPERSONATE,
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("data") is None and data.get("rc") not in (0, None):
                    raise RuntimeError(f"eastmoney rc={data.get('rc')}")
                return data
            except Exception as exc:  # noqa: BLE001
                last_err = exc
        wait = retry_delay * attempt + random.uniform(0.2, 0.8)
        print(
            f"[retry {attempt}/{retries}] {label}: "
            f"{type(last_err).__name__}: {last_err}；{wait:.1f}s 后重试",
            file=sys.stderr,
        )
        time.sleep(wait)
    raise RuntimeError(f"{label} 失败（已重试 {retries} 次）: {last_err}") from last_err


def fetch_paginated(
    base_params: dict[str, Any],
    *,
    page_size: int,
    retries: int,
    retry_delay: float,
    label: str,
    referer: str,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page = 1
    total: int | None = None
    while True:
        params = dict(base_params)
        params.update({"pn": str(page), "pz": str(page_size), "np": "1", "po": "1"})
        data = http_get_json(
            params,
            retries=retries,
            retry_delay=retry_delay,
            label=f"{label} p{page}",
            referer=referer,
        )
        payload = data.get("data") or {}
        if total is None:
            total = int(payload.get("total") or 0)
        diff = payload.get("diff") or []
        if isinstance(diff, dict):
            diff = list(diff.values())
        if not diff:
            break
        rows.extend(diff)
        if total is not None and len(rows) >= total:
            break
        if len(diff) < page_size:
            break
        page += 1
        sleep_jitter(0.15)
    return rows


def fetch_board_list(
    kind: str, *, retries: int, retry_delay: float
) -> pd.DataFrame:
    meta = BOARD_KINDS[kind]
    params = {
        "ut": UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": meta["fs"],
        "fields": "f12,f14,f2,f3,f4,f8,f20,f104,f105,f128,f136",
    }
    items = fetch_paginated(
        params,
        page_size=100,
        retries=retries,
        retry_delay=retry_delay,
        label=f"{meta['label']}列表",
        referer=meta["referer"],
    )
    records = []
    for i, item in enumerate(items, start=1):
        records.append(
            {
                "排名": i,
                "板块代码": str(item.get("f12", "")).strip(),
                "板块名称": str(item.get("f14", "")).strip(),
                "最新价": item.get("f2"),
                "涨跌幅": item.get("f3"),
                "涨跌额": item.get("f4"),
                "换手率": item.get("f8"),
                "总市值": item.get("f20"),
                "上涨家数": item.get("f104"),
                "下跌家数": item.get("f105"),
                "领涨股票": item.get("f128"),
                "领涨股票-涨跌幅": item.get("f136"),
            }
        )
    df = pd.DataFrame(records)
    if df.empty:
        raise RuntimeError(f"{meta['label']}列表为空，请检查网络或东财接口是否变更")
    df = df[df["板块代码"].astype(str).str.startswith("BK")]
    df = df[df["板块名称"] != ""]
    df = df.drop_duplicates(subset=["板块代码"], keep="first").reset_index(drop=True)
    if df.empty:
        raise RuntimeError(f"{meta['label']}列表为空，请检查网络或东财接口是否变更")
    return df


def fetch_board_members(
    board_code: str,
    *,
    kind: str,
    retries: int,
    retry_delay: float,
) -> pd.DataFrame:
    meta = BOARD_KINDS[kind]
    params = {
        "ut": UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f12",
        "fs": f"b:{board_code} f:!50",
        "fields": "f12,f14,f2,f3,f4,f5,f6,f7,f8,f9,f15,f16,f17,f18,f20,f23",
    }
    items = fetch_paginated(
        params,
        page_size=100,
        retries=retries,
        retry_delay=retry_delay,
        label=f"{meta['label']}成分股[{board_code}]",
        referer=meta["referer"],
    )
    records = []
    for i, item in enumerate(items, start=1):
        code = str(item.get("f12", "")).strip()
        name = str(item.get("f14", "")).strip()
        if not code:
            continue
        records.append(
            {
                "序号": i,
                "代码": code,
                "名称": name,
                "最新价": item.get("f2"),
                "涨跌幅": item.get("f3"),
                "涨跌额": item.get("f4"),
                "成交量": item.get("f5"),
                "成交额": item.get("f6"),
                "振幅": item.get("f7"),
                "换手率": item.get("f8"),
                "市盈率-动态": item.get("f9"),
                "最高": item.get("f15"),
                "最低": item.get("f16"),
                "今开": item.get("f17"),
                "昨收": item.get("f18"),
                "总市值": item.get("f20"),
                "市净率": item.get("f23"),
            }
        )
    return pd.DataFrame(records)


# 兼容旧调用名
def fetch_concept_list(*, retries: int, retry_delay: float) -> pd.DataFrame:
    return fetch_board_list("concept", retries=retries, retry_delay=retry_delay)


def fetch_concept_members(
    board_code: str, *, retries: int, retry_delay: float
) -> pd.DataFrame:
    return fetch_board_members(
        board_code, kind="concept", retries=retries, retry_delay=retry_delay
    )


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def connect_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS boards (
            board_type TEXT NOT NULL,
            board_code TEXT NOT NULL,
            board_name TEXT NOT NULL,
            raw_json TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (board_type, board_code)
        );

        CREATE TABLE IF NOT EXISTS board_members (
            board_type TEXT NOT NULL,
            board_code TEXT NOT NULL,
            board_name TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            raw_json TEXT,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (board_type, board_code, stock_code)
        );

        CREATE TABLE IF NOT EXISTS progress (
            board_type TEXT NOT NULL,
            board_code TEXT NOT NULL,
            board_name TEXT NOT NULL,
            status TEXT NOT NULL,
            member_count INTEGER DEFAULT 0,
            last_error TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (board_type, board_code)
        );

        CREATE INDEX IF NOT EXISTS idx_board_members_stock
            ON board_members(stock_code);
        CREATE INDEX IF NOT EXISTS idx_progress_status
            ON progress(board_type, status);
        """
    )
    _migrate_legacy(conn)
    return conn


def _migrate_legacy(conn: sqlite3.Connection) -> None:
    """把旧版 concepts / concept_members / progress 迁到新表。"""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "concepts" in tables:
        conn.execute(
            """
            INSERT OR IGNORE INTO boards(
                board_type, board_code, board_name, raw_json, fetched_at
            )
            SELECT 'concept', board_code, board_name, raw_json, fetched_at
            FROM concepts
            """
        )
    if "concept_members" in tables:
        conn.execute(
            """
            INSERT OR IGNORE INTO board_members(
                board_type, board_code, board_name,
                stock_code, stock_name, raw_json, fetched_at
            )
            SELECT 'concept', board_code, board_name,
                   stock_code, stock_name, raw_json, fetched_at
            FROM concept_members
            """
        )
    if "progress" in tables:
        cols = _table_columns(conn, "progress")
        if "board_type" not in cols:
            # 旧 progress：无 board_type，整表迁为 concept 后重建
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS progress_new (
                    board_type TEXT NOT NULL,
                    board_code TEXT NOT NULL,
                    board_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    member_count INTEGER DEFAULT 0,
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (board_type, board_code)
                )
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO progress_new(
                    board_type, board_code, board_name, status,
                    member_count, last_error, updated_at
                )
                SELECT 'concept', board_code, board_name, status,
                       member_count, last_error, updated_at
                FROM progress
                """
            )
            conn.execute("DROP TABLE progress")
            conn.execute("ALTER TABLE progress_new RENAME TO progress")
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_progress_status
                    ON progress(board_type, status)
                """
            )
    conn.commit()


def upsert_boards(
    conn: sqlite3.Connection, kind: str, boards: pd.DataFrame
) -> None:
    now = utc_now()
    rows = []
    for _, r in boards.iterrows():
        raw = json.dumps(r.to_dict(), ensure_ascii=False, default=str)
        rows.append((kind, str(r["板块代码"]), str(r["板块名称"]), raw, now))
    conn.executemany(
        """
        INSERT INTO boards(board_type, board_code, board_name, raw_json, fetched_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(board_type, board_code) DO UPDATE SET
            board_name=excluded.board_name,
            raw_json=excluded.raw_json,
            fetched_at=excluded.fetched_at
        """,
        rows,
    )
    conn.executemany(
        """
        INSERT INTO progress(
            board_type, board_code, board_name, status,
            member_count, last_error, updated_at
        ) VALUES (?, ?, ?, 'pending', 0, NULL, ?)
        ON CONFLICT(board_type, board_code) DO UPDATE SET
            board_name=excluded.board_name,
            updated_at=excluded.updated_at
        """,
        [(k, c, n, now) for k, c, n, _, _ in rows],
    )
    conn.commit()


def mark_progress(
    conn: sqlite3.Connection,
    kind: str,
    board_code: str,
    board_name: str,
    status: str,
    member_count: int = 0,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO progress(
            board_type, board_code, board_name, status,
            member_count, last_error, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(board_type, board_code) DO UPDATE SET
            board_name=excluded.board_name,
            status=excluded.status,
            member_count=excluded.member_count,
            last_error=excluded.last_error,
            updated_at=excluded.updated_at
        """,
        (kind, board_code, board_name, status, member_count, error, utc_now()),
    )
    conn.commit()


def save_members(
    conn: sqlite3.Connection,
    kind: str,
    board_code: str,
    board_name: str,
    members: pd.DataFrame,
) -> int:
    conn.execute(
        "DELETE FROM board_members WHERE board_type=? AND board_code=?",
        (kind, board_code),
    )
    if members is None or members.empty:
        conn.commit()
        return 0

    now = utc_now()
    rows = []
    for _, r in members.iterrows():
        code = str(r.get("代码", "")).strip()
        name = str(r.get("名称", "")).strip()
        if not code:
            continue
        raw = json.dumps(r.to_dict(), ensure_ascii=False, default=str)
        rows.append((kind, board_code, board_name, code, name, raw, now))

    dedup: dict[str, tuple] = {row[3]: row for row in rows}
    rows = list(dedup.values())
    conn.executemany(
        """
        INSERT INTO board_members(
            board_type, board_code, board_name,
            stock_code, stock_name, raw_json, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return len(rows)


def pending_boards(
    conn: sqlite3.Connection, kind: str, *, retry_failed: bool
) -> list[tuple[str, str]]:
    if retry_failed:
        sql = """
            SELECT board_code, board_name FROM progress
            WHERE board_type=? AND status IN ('pending', 'failed')
            ORDER BY board_name
        """
    else:
        sql = """
            SELECT board_code, board_name FROM progress
            WHERE board_type=? AND status='pending'
            ORDER BY board_name
        """
    return list(conn.execute(sql, (kind,)))


def export_csv(conn: sqlite3.Connection, data_dir: Path, kinds: list[str]) -> None:
    for kind in kinds:
        meta = BOARD_KINDS[kind]
        boards = pd.read_sql_query(
            """
            SELECT board_code, board_name, fetched_at
            FROM boards WHERE board_type=?
            ORDER BY board_name
            """,
            conn,
            params=(kind,),
        )
        members = pd.read_sql_query(
            """
            SELECT board_code, board_name, stock_code, stock_name, fetched_at
            FROM board_members WHERE board_type=?
            ORDER BY board_name, stock_code
            """,
            conn,
            params=(kind,),
        )
        boards.to_csv(data_dir / meta["list_csv"], index=False, encoding="utf-8-sig")
        members.to_csv(
            data_dir / meta["members_csv"], index=False, encoding="utf-8-sig"
        )


def print_summary(conn: sqlite3.Connection, kind: str) -> None:
    label = BOARD_KINDS[kind]["label"]
    total = conn.execute(
        "SELECT COUNT(*) FROM progress WHERE board_type=?", (kind,)
    ).fetchone()[0]
    done = conn.execute(
        "SELECT COUNT(*) FROM progress WHERE board_type=? AND status='done'",
        (kind,),
    ).fetchone()[0]
    failed = conn.execute(
        "SELECT COUNT(*) FROM progress WHERE board_type=? AND status='failed'",
        (kind,),
    ).fetchone()[0]
    pending = conn.execute(
        "SELECT COUNT(*) FROM progress WHERE board_type=? AND status='pending'",
        (kind,),
    ).fetchone()[0]
    members = conn.execute(
        "SELECT COUNT(*) FROM board_members WHERE board_type=?", (kind,)
    ).fetchone()[0]
    print(
        f"[{label}] 进度: total={total} done={done} failed={failed} "
        f"pending={pending} members={members}"
    )


def resolve_kinds(type_arg: str) -> list[str]:
    if type_arg == "all":
        return ["concept", "industry"]
    if type_arg not in BOARD_KINDS:
        raise ValueError(f"未知类型: {type_arg}")
    return [type_arg]


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="拉取东财概念/行业板块及成分股")
    p.add_argument(
        "--type",
        choices=["concept", "industry", "all"],
        default="all",
        help="板块类型：concept=概念，industry=行业，all=两者（默认 all）",
    )
    p.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    p.add_argument("--delay", type=float, default=0.35, help="每个板块间隔秒数")
    p.add_argument("--retries", type=int, default=3, help="请求失败重试次数")
    p.add_argument("--retry-delay", type=float, default=1.5, help="重试基础等待秒数")
    p.add_argument("--limit", type=int, default=0, help="每类仅处理前 N 个，0=全部")
    p.add_argument(
        "--refresh-list",
        action="store_true",
        help="强制重新拉取板块列表",
    )
    p.add_argument(
        "--retry-failed",
        action="store_true",
        help="把上次 failed 的板块一并重试",
    )
    p.add_argument(
        "--export-only",
        action="store_true",
        help="不拉网络，仅从 SQLite 导出 CSV",
    )
    return p.parse_args(argv)


def run_kind(conn: sqlite3.Connection, kind: str, args: argparse.Namespace) -> None:
    meta = BOARD_KINDS[kind]
    label = meta["label"]
    data_dir: Path = args.data_dir

    existing = conn.execute(
        "SELECT COUNT(*) FROM boards WHERE board_type=?", (kind,)
    ).fetchone()[0]
    if args.refresh_list or existing == 0:
        print(f"正在拉取{label}板块列表…")
        boards = fetch_board_list(
            kind, retries=args.retries, retry_delay=args.retry_delay
        )
        upsert_boards(conn, kind, boards)
        boards.to_csv(data_dir / meta["list_csv"], index=False, encoding="utf-8-sig")
        print(f"{label}板块: {len(boards)} 个 -> {data_dir / meta['list_csv']}")
    else:
        print(
            f"使用库中已有{label}列表（{existing} 个）。需要刷新请加 --refresh-list"
        )

    pending = pending_boards(conn, kind, retry_failed=args.retry_failed)
    if args.limit and args.limit > 0:
        pending = pending[: args.limit]
    print(f"待拉取{label}成分股: {len(pending)} 个板块")

    for i, (board_code, board_name) in enumerate(pending, start=1):
        print(f"[{label} {i}/{len(pending)}] {board_name} ({board_code})")
        try:
            members = fetch_board_members(
                board_code,
                kind=kind,
                retries=args.retries,
                retry_delay=args.retry_delay,
            )
            count = save_members(conn, kind, board_code, board_name, members)
            mark_progress(
                conn, kind, board_code, board_name, "done", member_count=count
            )
            print(f"  -> {count} 只成分股")
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
            mark_progress(
                conn, kind, board_code, board_name, "failed", error=err
            )
            print(f"  -> 失败: {err}", file=sys.stderr)
        sleep_jitter(args.delay)

    print_summary(conn, kind)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    kinds = resolve_kinds(args.type)
    data_dir: Path = args.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / DB_NAME

    conn = connect_db(db_path)
    try:
        if args.export_only:
            export_csv(conn, data_dir, kinds)
            for kind in kinds:
                print_summary(conn, kind)
                meta = BOARD_KINDS[kind]
                print(f"已导出: {data_dir / meta['list_csv']}")
                print(f"已导出: {data_dir / meta['members_csv']}")
            return 0

        for kind in kinds:
            run_kind(conn, kind, args)

        export_csv(conn, data_dir, kinds)
        print(f"SQLite: {db_path}")
        for kind in kinds:
            meta = BOARD_KINDS[kind]
            print(
                f"CSV[{BOARD_KINDS[kind]['label']}]: "
                f"{data_dir / meta['list_csv']}, {data_dir / meta['members_csv']}"
            )
        print("说明: 数据来源于东方财富公开接口，非官方授权。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
