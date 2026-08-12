"""LangGraph DocStore（跨线程 KV）：SQLite 自研 + 官方 PostgresStore。

- Sqlite：``SqliteDocStore``（``BaseStore.batch`` / ``abatch``，对外 ``put/get/search``）
- Postgres：``langgraph.store.postgres.PostgresStore``（不重复造轮子）

按 ``DATABASE_URL`` 方言自动选择（Postgres 同库；Sqlite 旁路 ``*.docstore.db``）。
"""

from __future__ import annotations

from langgraph.store.base import BaseStore

from app.persistence.docstore.factory import create_doc_store, get_doc_store, reset_doc_store
from app.persistence.docstore.parent_child import DefaultParentChildIndex, ParentChildIndex
from app.persistence.docstore.parents import ParentDocRepository, parents_namespace
from app.persistence.docstore.sqlite_store import SqliteDocStore

__all__ = [
    "BaseStore",
    "DefaultParentChildIndex",
    "ParentChildIndex",
    "ParentDocRepository",
    "SqliteDocStore",
    "create_doc_store",
    "get_doc_store",
    "parents_namespace",
    "reset_doc_store",
]
