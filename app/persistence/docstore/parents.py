"""父文档领域 API：基于 LangGraph BaseStore（key = parent_id）。"""

from __future__ import annotations

from typing import Sequence

from langgraph.store.base import BaseStore, GetOp, PutOp

from app.persistence.vectorstore.types import ParentDocument

_NS_ROOT = "parents"
_PAGE = 1_000


def parents_namespace(collection: str) -> tuple[str, ...]:
    return (_NS_ROOT, (collection or "").strip())


class ParentDocRepository:
    """把 ParentDocument 映射到 store.put/get/search。"""

    def __init__(self, store: BaseStore) -> None:
        self._store = store

    @property
    def store(self) -> BaseStore:
        return self._store

    def upsert(self, collection: str, docs: Sequence[ParentDocument]) -> int:
        if not docs:
            return 0
        ns = parents_namespace(collection)
        ops = [
            PutOp(
                namespace=ns,
                key=d.id,
                value={
                    "text": d.text or "",
                    "metadata": dict(d.metadata or {}),
                },
            )
            for d in docs
        ]
        self._store.batch(ops)
        return len(docs)

    def get(self, collection: str, ids: Sequence[str]) -> list[ParentDocument]:
        if not ids:
            return []
        ns = parents_namespace(collection)
        results = self._store.batch(
            [GetOp(namespace=ns, key=str(pid)) for pid in ids]
        )
        out: list[ParentDocument] = []
        for item in results:
            if item is None:
                continue
            val = item.value or {}
            meta = val.get("metadata") if isinstance(val.get("metadata"), dict) else {}
            out.append(
                ParentDocument(
                    id=str(item.key),
                    text=str(val.get("text") or ""),
                    metadata=dict(meta),
                )
            )
        return out

    def delete(self, collection: str, ids: Sequence[str]) -> int:
        if not ids:
            return 0
        ns = parents_namespace(collection)
        self._store.batch(
            [PutOp(namespace=ns, key=str(pid), value=None) for pid in ids]
        )
        return len(ids)

    def list_ids(
        self,
        collection: str,
        *,
        key_prefix: str | None = None,
        page_size: int = _PAGE,
    ) -> list[str]:
        """分页枚举 namespace 下全部 key（可按 key 前缀过滤）。"""
        ns = parents_namespace(collection)
        page_size = max(1, min(int(page_size or _PAGE), 5_000))
        out: list[str] = []
        offset = 0
        lister = getattr(self._store, "list_keys", None)
        while True:
            if callable(lister):
                chunk = lister(
                    ns, key_prefix=key_prefix, limit=page_size, offset=offset
                )
            else:
                items = self._store.search(ns, limit=page_size, offset=offset)
                chunk = [
                    str(it.key)
                    for it in items
                    if key_prefix is None or str(it.key).startswith(key_prefix)
                ]
                # 无 list_keys 时：若本页因前缀过滤变空但仍可能有后续页，继续翻
                if not items:
                    break
                out.extend(chunk)
                if len(items) < page_size:
                    break
                offset += page_size
                continue
            if not chunk:
                break
            out.extend(chunk)
            if len(chunk) < page_size:
                break
            offset += page_size
        return out

    def drop_namespace(self, collection: str) -> None:
        keys = self.list_ids(collection)
        if not keys:
            return
        ns = parents_namespace(collection)
        # 分批删除，避免单次 batch 过大
        for i in range(0, len(keys), _PAGE):
            chunk = keys[i : i + _PAGE]
            self._store.batch(
                [PutOp(namespace=ns, key=k, value=None) for k in chunk]
            )

    def close(self) -> None:
        return None
