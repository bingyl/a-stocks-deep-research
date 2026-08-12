"""SQLite-backed LangGraph ``BaseStore``（官方无 SqliteStore，按文档自研）。

实现契约（以本机 ``inspect.getsource(BaseStore)`` 为准，对齐 ``InMemoryStore``）：

- **必须实现**：``batch`` / ``abatch``
- **不必重写**：``put`` / ``get`` / ``delete`` / ``search`` / ``list_namespaces``
  以及对应 ``a*`` —— 均由 ``BaseStore`` 委托到 batch

文档「Build a custom store」中的 aput/aget 等是对外 API；在当前 langgraph
版本里它们已由基类提供。无向量能力时，``SearchOp.query`` 非空则抛
``NotImplementedError``（官方约定）。

表结构对齐官方 ``PostgresStore`` 的 ``store(prefix, key, value, ...)``。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Generator

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    MatchCondition,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

from app.persistence.base import apply_sqlite_concurrency_pragmas

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store (
    prefix TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (prefix, key)
);
CREATE INDEX IF NOT EXISTS store_prefix_idx ON store(prefix);
"""


def _namespace_to_text(namespace: tuple[str, ...]) -> str:
    """与 PostgresStore 一致：用 ``.`` 拼接 namespace → prefix。"""
    return ".".join(namespace)


def _text_to_namespace(prefix: str) -> tuple[str, ...]:
    if not prefix:
        return ()
    return tuple(prefix.split("."))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return _now()


def _escape_like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _apply_operator(value: Any, operator: str, op_value: Any) -> bool:
    """与 ``langgraph.store.memory._apply_operator`` 行为对齐。"""
    if operator == "$eq":
        return value == op_value
    if operator == "$gt":
        return float(value) > float(op_value)
    if operator == "$gte":
        return float(value) >= float(op_value)
    if operator == "$lt":
        return float(value) < float(op_value)
    if operator == "$lte":
        return float(value) <= float(op_value)
    if operator == "$ne":
        return value != op_value
    raise ValueError(f"Unsupported operator: {operator}")


def _compare_values(item_value: Any, filter_value: Any) -> bool:
    """与 ``langgraph.store.memory._compare_values`` 行为对齐。"""
    if isinstance(filter_value, dict):
        if any(k.startswith("$") for k in filter_value):
            return all(
                _apply_operator(item_value, op_key, op_value)
                for op_key, op_value in filter_value.items()
            )
        if not isinstance(item_value, dict):
            return False
        return all(
            _compare_values(item_value.get(k), v) for k, v in filter_value.items()
        )
    if isinstance(filter_value, (list, tuple)):
        return (
            isinstance(item_value, (list, tuple))
            and len(item_value) == len(filter_value)
            and all(
                _compare_values(iv, fv)
                for iv, fv in zip(item_value, filter_value, strict=False)
            )
        )
    return item_value == filter_value


def _does_match(match_condition: MatchCondition, key: tuple[str, ...]) -> bool:
    """与 ``langgraph.store.memory._does_match`` 行为对齐（含 ``*`` 通配）。"""
    match_type = match_condition.match_type
    path = match_condition.path
    if len(key) < len(path):
        return False
    if match_type == "prefix":
        for k_elem, p_elem in zip(key, path, strict=False):
            if p_elem == "*":
                continue
            if k_elem != p_elem:
                return False
        return True
    if match_type == "suffix":
        for k_elem, p_elem in zip(reversed(key), reversed(path), strict=False):
            if p_elem == "*":
                continue
            if k_elem != p_elem:
                return False
        return True
    raise ValueError(f"Unsupported match type: {match_type}")


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        namespace=_text_to_namespace(row["prefix"]),
        key=row["key"],
        value=json.loads(row["value"]),
        created_at=_parse_ts(row["created_at"]),
        updated_at=_parse_ts(row["updated_at"]),
    )


class SqliteDocStore(BaseStore):
    """SQLite 持久化 Store。对外用法与官方文档一致：``put`` / ``get`` / ``search`` …

    >>> store = SqliteDocStore("./data/store.sqlite")
    >>> store.put(("user", "memories"), "m1", {"food_preference": "pizza"})
    >>> store.get(("user", "memories"), "m1")
    >>> store.search(("user", "memories"), limit=10)
    """

    supports_ttl: bool = False

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self._path),
            check_same_thread=False,
            timeout=60,
        )
        self._conn.row_factory = sqlite3.Row
        apply_sqlite_concurrency_pragmas(self._conn)
        self.setup()

    @classmethod
    @contextmanager
    def create(cls, path: str | Path) -> Generator[SqliteDocStore, None, None]:
        """文档测试示例风格：``with SqliteDocStore.create(path) as store:``。"""
        store = cls(path)
        try:
            yield store
        finally:
            store.close()

    def setup(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- BaseStore 抽象契约（与 InMemoryStore 相同） ---

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        """同步批量执行 Op。``put``/``get``/``search`` 等均经此入口。"""
        ops_list = list(ops)
        results: list[Result] = [None] * len(ops_list)
        with self._lock:
            try:
                for i, op in enumerate(ops_list):
                    if isinstance(op, GetOp):
                        results[i] = self._handle_get(op)
                    elif isinstance(op, PutOp):
                        self._handle_put(op)
                        results[i] = None
                    elif isinstance(op, SearchOp):
                        results[i] = self._handle_search(op)
                    elif isinstance(op, ListNamespacesOp):
                        results[i] = self._handle_list_namespaces(op)
                    else:
                        raise ValueError(f"Unknown operation type: {type(op)}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        """异步批量：sqlite3 无原生 async，与 InMemoryStore 一样直接跑同步 batch。"""
        return self.batch(ops)

    # --- Op handlers ---

    def _handle_get(self, op: GetOp) -> Item | None:
        cur = self._conn.execute(
            "SELECT prefix, key, value, created_at, updated_at FROM store "
            "WHERE prefix = ? AND key = ?",
            (_namespace_to_text(op.namespace), op.key),
        )
        row = cur.fetchone()
        return None if row is None else _row_to_item(row)

    def _handle_put(self, op: PutOp) -> None:
        prefix = _namespace_to_text(op.namespace)
        if op.value is None:
            self._conn.execute(
                "DELETE FROM store WHERE prefix = ? AND key = ?",
                (prefix, op.key),
            )
            return
        now = _now().isoformat()
        payload = json.dumps(op.value, ensure_ascii=False, default=str)
        self._conn.execute(
            """
            INSERT INTO store(prefix, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(prefix, key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (prefix, op.key, payload, now, now),
        )

    def _handle_search(self, op: SearchOp) -> list[SearchItem]:
        # 官方约定：无向量后端时 query 必须 NotImplementedError
        if op.query:
            raise NotImplementedError(
                "SqliteDocStore does not support semantic search (query=...). "
                "Use filter/list, or a store with index embeddings "
                "(e.g. PostgresStore with index config)."
            )

        path = _namespace_to_text(op.namespace_prefix)
        params: list[Any] = []
        if path:
            where = "WHERE prefix = ? OR prefix LIKE ? ESCAPE '\\'"
            params.extend([path, f"{_escape_like(path)}.%"])
        else:
            where = ""

        # 无 filter 时把 LIMIT/OFFSET 下推 SQL，保证分页枚举正确且省内存
        if not op.filter:
            sql = (
                "SELECT prefix, key, value, created_at, updated_at FROM store "
                f"{where} ORDER BY updated_at DESC, key ASC LIMIT ? OFFSET ?"
            )
            params.extend([int(op.limit), int(op.offset)])
            cur = self._conn.execute(sql, params)
            return [
                SearchItem(
                    namespace=_text_to_namespace(row["prefix"]),
                    key=row["key"],
                    value=json.loads(row["value"]),
                    created_at=_parse_ts(row["created_at"]),
                    updated_at=_parse_ts(row["updated_at"]),
                )
                for row in cur.fetchall()
            ]

        sql = (
            "SELECT prefix, key, value, created_at, updated_at FROM store "
            f"{where} ORDER BY updated_at DESC, key ASC"
        )
        cur = self._conn.execute(sql, params)
        items: list[SearchItem] = []
        for row in cur.fetchall():
            value = json.loads(row["value"])
            if not all(
                _compare_values(value.get(k), fv) for k, fv in (op.filter or {}).items()
            ):
                continue
            items.append(
                SearchItem(
                    namespace=_text_to_namespace(row["prefix"]),
                    key=row["key"],
                    value=value,
                    created_at=_parse_ts(row["created_at"]),
                    updated_at=_parse_ts(row["updated_at"]),
                )
            )
        return items[op.offset : op.offset + op.limit]

    def list_keys(
        self,
        namespace: tuple[str, ...],
        *,
        key_prefix: str | None = None,
        limit: int = 1_000,
        offset: int = 0,
    ) -> list[str]:
        """按 namespace（及可选 key 前缀）分页列 key，不读 value。"""
        prefix = _namespace_to_text(namespace)
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        if key_prefix:
            like = f"{_escape_like(key_prefix)}%"
            cur = self._conn.execute(
                "SELECT key FROM store WHERE prefix = ? AND key LIKE ? ESCAPE '\\' "
                "ORDER BY key ASC LIMIT ? OFFSET ?",
                (prefix, like, limit, offset),
            )
        else:
            cur = self._conn.execute(
                "SELECT key FROM store WHERE prefix = ? "
                "ORDER BY key ASC LIMIT ? OFFSET ?",
                (prefix, limit, offset),
            )
        return [str(r["key"]) for r in cur.fetchall()]

    def _handle_list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        cur = self._conn.execute("SELECT DISTINCT prefix FROM store ORDER BY prefix")
        namespaces = [_text_to_namespace(r["prefix"]) for r in cur.fetchall()]
        if op.match_conditions:
            namespaces = [
                ns
                for ns in namespaces
                if all(_does_match(cond, ns) for cond in op.match_conditions)
            ]
        if op.max_depth is not None:
            namespaces = sorted({ns[: op.max_depth] for ns in namespaces})
        else:
            namespaces = sorted(namespaces)
        return namespaces[op.offset : op.offset + op.limit]
