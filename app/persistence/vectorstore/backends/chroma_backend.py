"""Chroma 本地稠密向量后端（langchain_chroma）。

本地 PersistentClient 无 BM25；检索为稠密近邻。
依赖：pip install langchain-chroma
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import typing
from pathlib import Path
from typing import Any, Sequence

from langchain_chroma import Chroma
from langchain_core.embeddings import Embeddings

from app.core.config import BASE_DIR, Settings
from app.core.logging import log_caught
from app.persistence.vectorstore.base import VectorStore
from app.persistence.vectorstore.registry import register_backend
from app.persistence.vectorstore.types import (
    MetadataFilter,
    SearchHit,
    VectorRecord,
    VectorTextRecord,
    normalize_filter,
)

logger = logging.getLogger(__name__)


def _resolve_dir(raw: str) -> Path:
    path = Path(raw.strip() or "./data/vector_chroma")
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _flatten_metadata(meta: dict[str, Any] | None) -> dict[str, Any]:
    """Chroma 仅接受 str/int/float/bool 元数据。"""
    out: dict[str, Any] = {}
    for key, val in (meta or {}).items():
        if val is None:
            continue
        if isinstance(val, bool):
            out[str(key)] = val
        elif isinstance(val, (int, float)):
            out[str(key)] = val
        elif isinstance(val, str):
            out[str(key)] = val
        else:
            out[str(key)] = json.dumps(val, ensure_ascii=False, default=str)
    return out


def _to_chroma_where(filt: MetadataFilter | None) -> dict[str, Any] | None:
    if filt is None or filt.is_empty():
        return None
    clauses: list[dict[str, Any]] = []
    for key, val in filt.equals.items():
        clauses.append({str(key): {"$eq": val}})
    for key, vals in filt.any_of.items():
        clauses.append({str(key): {"$in": list(vals)}})
    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


class ChromaVectorStore(VectorStore):
    backend_name = "chroma"

    def __init__(self, persist_directory: str | Path, **kwargs) -> None:
        self._root = _resolve_dir(str(persist_directory))
        self._lock = threading.RLock()
        self.vectorstore: typing.Optional[Chroma] = None
        super().__init__(**kwargs)

    def initialize(
        self,
        collection: str,
        embeddings: Embeddings,
        **kwargs: Any,
    ) -> None:
        space = kwargs["metric"] if "metric" in kwargs else self.metric
        self.metric = space
        self.collection = collection
        self.vectorstore = Chroma(
            collection_name=self.collection,
            embedding_function=embeddings,
            persist_directory=str(self._root),
            create_collection_if_not_exists=True,
            collection_metadata={"hnsw:space": space},
        )

    def _require_store(self) -> Chroma:
        if self.vectorstore is None:
            raise RuntimeError("ChromaVectorStore 未 initialize")
        return self.vectorstore

    def _distance_to_score(self, distance: float) -> float:
        """Chroma similarity_search_with_score 返回 distance；统一成越大越相关。"""
        d = float(distance)
        if self.metric == "cosine":
            # cosine space：distance ≈ 1 - cos_sim
            return 1.0 - d
        if self.metric == "ip":
            return d
        return -d

    def add_texts(self, records: Sequence[VectorTextRecord]) -> int:
        if not records:
            return 0
        store = self._require_store()
        with self._lock:
            ids = [r.id for r in records]
            try:
                store.delete(ids=ids)
            except Exception as exc:
                log_caught(
                    logger,
                    "chroma add texts 预删除跳过 collection=%s n=%s",
                    self.collection,
                    len(ids),
                    exc=exc,
                    level=logging.DEBUG,
                )
            texts = [r.text or "" for r in records]
            metadatas = [_flatten_metadata(r.metadata) for r in records]
            for i, meta in enumerate(metadatas):
                if not meta:
                    metadatas[i] = {"_ok": True}
            added = store.add_texts(
                ids=ids,
                texts=texts,
                metadatas=metadatas,
            )
            return len(added)

    async def aadd_texts(self, records: Sequence[VectorTextRecord]) -> int:
        return await asyncio.to_thread(self.add_texts, records)

    def delete(
        self,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        if ids is None and where is None:
            return 0
        filt = normalize_filter(where)
        chroma_where = _to_chroma_where(filt)
        ids_list = list(ids) if ids is not None else None
        if ids_list is not None and not ids_list and chroma_where is None:
            return 0
        if ids_list is None and chroma_where is None:
            return 0

        store = self._require_store()
        with self._lock:
            try:
                kwargs: dict[str, Any] = {}
                if ids_list is not None:
                    kwargs["ids"] = ids_list
                if chroma_where is not None:
                    kwargs["where"] = chroma_where
                result = store._collection.delete(**kwargs)
                if isinstance(result, dict):
                    return int(result.get("deleted", 0) or 0)
                return len(ids_list) if ids_list is not None else 0
            except Exception as exc:
                log_caught(
                    logger,
                    "chroma delete failed collection=%s",
                    self.collection,
                    exc=exc,
                    level=logging.ERROR,
                )
                return 0

    async def adelete(
        self,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        return await asyncio.to_thread(self.delete, ids, where)

    def get(self, ids: Sequence[str]) -> list[VectorRecord]:
        if not ids:
            return []
        store = self._require_store()
        with self._lock:
            got = store.get(ids=list(ids), include=["documents", "metadatas"])
            out: list[VectorRecord] = []
            got_ids = got.get("ids") or []
            docs = got.get("documents") or []
            metas = got.get("metadatas") or []
            for i, vid in enumerate(got_ids):
                meta = dict(metas[i] or {}) if i < len(metas) else {}
                meta.pop("_ok", None)
                out.append(
                    VectorRecord(
                        id=str(vid),
                        vector=[],
                        text=(docs[i] if i < len(docs) else "") or "",
                        metadata=meta,
                    )
                )
            return out

    async def aget(self, ids: Sequence[str]) -> list[VectorRecord]:
        return await asyncio.to_thread(self.get, ids)

    def search(
        self,
        query: str,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            return []
        filt = normalize_filter(where)
        chroma_where = _to_chroma_where(filt)
        store = self._require_store()
        with self._lock:
            try:
                kwargs: dict[str, Any] = {"query": query, "k": top_k}
                if chroma_where is not None:
                    kwargs["filter"] = chroma_where
                raw = store.similarity_search_with_score(**kwargs)
            except Exception as exc:
                log_caught(
                    logger,
                    "chroma search failed collection=%s",
                    self.collection,
                    exc=exc,
                    level=logging.ERROR,
                )
                return []

            return [
                SearchHit(document=document, score=self._distance_to_score(score))
                for document, score in raw
            ]

    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        return await asyncio.to_thread(
            self.search,
            query,
            top_k=top_k,
            where=where,
        )

    def close(self) -> None:
        with self._lock:
            self.vectorstore = None


@register_backend("chroma")
def build_chroma_store(settings: Settings, **overrides: Any) -> VectorStore:
    root = overrides.get("persist_directory") or overrides.get("root_dir")
    root = root or getattr(settings, "vector_chroma_dir", "./data/vector_chroma")
    return ChromaVectorStore(persist_directory=root)
