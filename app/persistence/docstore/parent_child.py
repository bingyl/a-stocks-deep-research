"""父子文档索引门面：父文 LangGraph Store + 子块 VectorStore。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Sequence

from app.integrations.embedding import get_embeddings
from app.persistence.docstore.parents import ParentDocRepository
from app.persistence.vectorstore.base import VectorStore
from app.persistence.vectorstore.types import (
    META_PARENT_ID,
    ChildDocument,
    MetadataFilter,
    ParentDocument,
    SearchHit,
)


class ParentChildIndex(ABC):
    """父子切片统一接口（业务侧只依赖本抽象，不直接碰后端细节）。"""

    @abstractmethod
    def ensure(self, collection: str) -> None:
        """准备向量集合 + 父文档 namespace。"""

    @abstractmethod
    def drop(self, collection: str) -> None:
        """删除子块索引与对应父文档 namespace。"""

    @abstractmethod
    def upsert(
        self,
        collection: str,
        *,
        parents: Sequence[ParentDocument] | None = None,
        children: Sequence[ChildDocument] | None = None,
    ) -> dict[str, int]:
        """写入/覆盖父文档与子块。返回 ``{"parents": n, "children": m}``。"""

    @abstractmethod
    def delete_children(
        self,
        collection: str,
        *,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        ...

    @abstractmethod
    def delete_parents(self, collection: str, ids: Sequence[str]) -> int:
        ...

    @abstractmethod
    def search(
        self,
        collection: str,
        query: str,
        *,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """在子块上做向量召回。"""

    @abstractmethod
    def resolve_parents(
        self,
        collection: str,
        hits: Sequence[SearchHit],
        *,
        dedupe: bool = True,
    ) -> list[ParentDocument]:
        """按命中子块回表父文档（供塞进 LLM）。"""

    def close(self) -> None:
        return None


class DefaultParentChildIndex(ParentChildIndex):
    """默认实现：VectorStore（子） + ParentDocRepository（父，底层 BaseStore）。"""

    def __init__(self, vectors: VectorStore, docs: ParentDocRepository) -> None:
        self._vectors = vectors
        self._docs = docs

    @property
    def vector_store(self) -> VectorStore:
        return self._vectors

    @property
    def doc_store(self) -> ParentDocRepository:
        return self._docs

    def ensure(self, collection: str) -> None:
        name = (collection or "").strip()
        if not name:
            raise ValueError("collection name 不能为空")
        if self._vectors.vectorstore is not None and self._vectors.collection == name:
            return
        self._vectors.initialize(name, get_embeddings())

    def drop(self, collection: str) -> None:
        name = (collection or "").strip()
        self._docs.drop_namespace(name)
        if self._vectors.collection == name and self._vectors.vectorstore is not None:
            vs = self._vectors.vectorstore
            if hasattr(vs, "delete_collection"):
                try:
                    vs.delete_collection()
                finally:
                    self._vectors.vectorstore = None
                    self._vectors.collection = ""

    def upsert(
        self,
        collection: str,
        *,
        parents: Sequence[ParentDocument] | None = None,
        children: Sequence[ChildDocument] | None = None,
    ) -> dict[str, int]:
        self.ensure(collection)
        n_parents = self._docs.upsert(collection, parents or [])
        records = [c.to_vector_record() for c in (children or [])]
        n_children = self._vectors.add_texts(records) if records else 0
        return {"parents": n_parents, "children": int(n_children or 0)}

    def delete_children(
        self,
        collection: str,
        *,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        self.ensure(collection)
        return self._vectors.delete(ids=ids, where=where)

    def delete_parents(self, collection: str, ids: Sequence[str]) -> int:
        return self._docs.delete(collection, ids)

    def search(
        self,
        collection: str,
        query: str,
        *,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        self.ensure(collection)
        return self._vectors.search(query, top_k=top_k, where=where)

    def resolve_parents(
        self,
        collection: str,
        hits: Sequence[SearchHit],
        *,
        dedupe: bool = True,
    ) -> list[ParentDocument]:
        parent_ids: list[str] = []
        for h in hits:
            pid = (h.metadata or {}).get(META_PARENT_ID)
            if not pid:
                continue
            parent_ids.append(str(pid))
        if not parent_ids:
            return []
        if dedupe:
            seen: set[str] = set()
            ordered: list[str] = []
            for pid in parent_ids:
                if pid in seen:
                    continue
                seen.add(pid)
                ordered.append(pid)
            parent_ids = ordered
        return self._docs.get(collection, parent_ids)

    def close(self) -> None:
        self._vectors.close()
        self._docs.close()
