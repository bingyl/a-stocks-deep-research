"""Milvus 稠密向量后端（可选 BM25 sparse 双路）。

连接语义与 ``langchain_milvus.Milvus(connection_args={...})`` 对齐：
- ``uri=./xxx.db`` → Milvus Lite（本地文件，仅稠密 / FLAT）
- ``uri=http://host:19530`` → Standalone/Distributed（可开 BM25）

依赖（可选）：pymilvus（建议 >= 2.5）、langchain-milvus
"""

from __future__ import annotations

import asyncio
import logging
import typing
from pathlib import Path
from typing import Any, Sequence

from langchain_core.embeddings import Embeddings
from langchain_milvus import BM25BuiltInFunction, Milvus

from app.core.config import BASE_DIR, Settings
from app.core.logging import log_caught
from app.persistence.vectorstore.base import VectorStore
from app.persistence.vectorstore.registry import register_backend
from app.persistence.vectorstore.types import (
    DistanceMetric,
    MetadataFilter,
    SearchHit,
    VectorRecord,
    VectorTextRecord,
    normalize_filter,
)

logger = logging.getLogger(__name__)

_TEXT_MAX = 65535
_ID_MAX = 128
_DENSE_FIELD = "dense"
_SPARSE_FIELD = "sparse"


def resolve_milvus_uri(uri: str) -> str:
    """解析 URI：http(s) 原样；本地路径转绝对路径（Milvus Lite）。"""
    u = (uri or "").strip()
    if not u:
        return "http://127.0.0.1:19530"
    low = u.lower()
    if low.startswith(("http://", "https://", "tcp://", "unix:")):
        return u
    path = Path(u)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def is_milvus_lite_uri(uri: str) -> bool:
    low = (uri or "").strip().lower()
    return not low.startswith(("http://", "https://", "tcp://", "unix:"))


def _metric_to_milvus(metric: DistanceMetric) -> str:
    if metric == "cosine":
        return "COSINE"
    if metric == "ip":
        return "IP"
    if metric == "l2":
        return "L2"
    raise ValueError(f"不支持的度量: {metric}")


def json_quote(value: str) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def json_literal(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return json_quote(str(value))


def _build_expr(filt: MetadataFilter | None, ids: Sequence[str] | None = None) -> str:
    parts: list[str] = []
    if ids is not None:
        if not ids:
            return "pk in []"
        quoted = ", ".join(json_quote(i) for i in ids)
        parts.append(f"pk in [{quoted}]")
    if filt is not None:
        for key, val in filt.equals.items():
            parts.append(f"{key} == {json_literal(val)}")
        for key, vals in filt.any_of.items():
            inner = ", ".join(json_literal(v) for v in vals)
            parts.append(f"{key} in [{inner}]")
        raw = filt.extra.get("expr")
        if isinstance(raw, str) and raw.strip():
            parts.append(f"({raw.strip()})")
    return " and ".join(parts) if parts else ""


class MilvusVectorStore(VectorStore):
    backend_name = "milvus"

    def __init__(
        self,
        *,
        uri: str,
        token: str = "",
        db_name: str = "default",
        alias: str = "default",
    ) -> None:
        self._uri = resolve_milvus_uri(uri)
        self._token = token.strip()
        self._db_name = (db_name or "default").strip()
        self._alias = alias
        self._lite = is_milvus_lite_uri(self._uri)
        self.vectorstore: typing.Optional[Milvus] = None
        super().__init__()

    @property
    def connection_args(self) -> dict[str, Any]:
        """与 langchain_milvus.Milvus(connection_args=...) 同形。"""
        args: dict[str, Any] = {"uri": self._uri}
        if self._token:
            args["token"] = self._token
        if self._db_name and self._db_name != "default":
            args["db_name"] = self._db_name
        return args

    def _require_store(self) -> Milvus:
        if self.vectorstore is None:
            raise RuntimeError("MilvusVectorStore 未 initialize")
        return self.vectorstore

    def initialize(self, collection: str, embeddings: Embeddings, **kwargs: Any) -> None:
        space = kwargs["metric"] if "metric" in kwargs else self.metric
        self.metric = space
        self.collection = collection
        metric = _metric_to_milvus(self.metric)
        conn = self.connection_args

        if self._lite:
            # Lite：仅稠密；BM25 / jieba analyzer 不可靠
            self.vectorstore = Milvus(
                embedding_function=embeddings,
                collection_name=collection,
                index_params={"index_type": "FLAT", "metric_type": metric},
                connection_args=conn,
                consistency_level="Bounded",
                drop_old=False,
            )
            return

        self.vectorstore = Milvus(
            embedding_function=embeddings,
            collection_name=collection,
            index_params=[
                {"index_type": "AUTOINDEX", "metric_type": metric},
                {"index_type": "SPARSE_INVERTED_INDEX", "metric_type": "BM25"},
            ],
            builtin_function=BM25BuiltInFunction(
                output_field_names=_SPARSE_FIELD,
                analyzer_params={
                    "type": "jieba",
                    "dict": ["_default_"],
                    "mode": "search",
                    "hmm": True,
                },
            ),
            vector_field=[_DENSE_FIELD, _SPARSE_FIELD],
            connection_args=conn,
            consistency_level="Bounded",
            drop_old=False,
        )

    def add_texts(self, records: Sequence[VectorTextRecord]) -> int:
        if not records:
            return 0
        store = self._require_store()
        ids = [r.id[:_ID_MAX] for r in records]
        store.delete(ids=ids)
        texts = [(r.text or "")[:_TEXT_MAX] for r in records]
        metadatas = [dict(r.metadata or {}) for r in records]
        added = store.add_texts(texts=texts, metadatas=metadatas, ids=ids)
        return len(added)

    async def aadd_texts(self, records: Sequence[VectorTextRecord]) -> int:
        if not records:
            return 0
        store = self._require_store()
        ids = [r.id[:_ID_MAX] for r in records]
        await store.adelete(ids=ids)
        texts = [(r.text or "")[:_TEXT_MAX] for r in records]
        metadatas = [dict(r.metadata or {}) for r in records]
        added = await store.aadd_texts(texts=texts, metadatas=metadatas, ids=ids)
        return len(added)

    def delete(
        self,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        if ids is None and where is None:
            return 0
        filt = normalize_filter(where)
        expr = _build_expr(filt, ids=ids)
        if not expr:
            return 0
        store = self._require_store()
        try:
            if ids:
                ok = store.delete(ids=list(ids))
                return len(ids) if ok else 0
            ok = store.delete(expr=expr)
            return 1 if ok else 0
        except Exception as exc:
            log_caught(
                logger,
                "milvus delete failed collection=%s",
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
        if ids is None and where is None:
            return 0
        filt = normalize_filter(where)
        expr = _build_expr(filt, ids=ids)
        if not expr:
            return 0
        store = self._require_store()
        try:
            if ids:
                ok = await store.adelete(ids=list(ids))
                return len(ids) if ok else 0
            ok = await store.adelete(expr=expr)
            return 1 if ok else 0
        except Exception as exc:
            log_caught(
                logger,
                "milvus adelete failed collection=%s",
                self.collection,
                exc=exc,
                level=logging.ERROR,
            )
            return 0

    def get(self, ids: Sequence[str]) -> list[VectorRecord]:
        if not ids:
            return []
        store = self._require_store()
        expr = _build_expr(None, ids=ids)
        try:
            rows = store.client.query(
                collection_name=self.collection,
                filter=expr,
                output_fields=["*"],
            )
        except Exception as exc:
            log_caught(
                logger,
                "milvus get failed collection=%s",
                self.collection,
                exc=exc,
                level=logging.ERROR,
            )
            return []

        pk = getattr(store, "_primary_field", "pk")
        text_field = getattr(store, "_text_field", "text")
        vector_fields = set(store._as_list(getattr(store, "_vector_field", "vector")))
        skip = {pk, text_field, *vector_fields, _SPARSE_FIELD}

        by_id: dict[str, VectorRecord] = {}
        for row in rows or []:
            vid = str(row.get(pk, ""))
            if not vid:
                continue
            meta = {
                str(k): v
                for k, v in dict(row).items()
                if k not in skip and v is not None
            }
            dense = row.get(_DENSE_FIELD)
            if dense is None:
                dense = row.get("vector")
            vec = list(dense) if isinstance(dense, (list, tuple)) else []
            by_id[vid] = VectorRecord(
                id=vid,
                vector=vec,
                text=str(row.get(text_field) or ""),
                metadata=meta,
            )
        return [by_id[i] for i in ids if i in by_id]

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
        store = self._require_store()
        filt = normalize_filter(where)
        expr = _build_expr(filt) or None
        kwargs: dict[str, Any] = {"k": top_k, "expr": expr}
        if not self._lite:
            kwargs["ranker_type"] = "weighted"
            kwargs["ranker_params"] = {"weights": [0.6, 0.4]}
        raw = store.similarity_search_with_score(query, **kwargs)
        return [SearchHit(document=document, score=float(score)) for document, score in raw]

    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        if top_k <= 0:
            return []
        store = self._require_store()
        filt = normalize_filter(where)
        expr = _build_expr(filt) or None
        kwargs: dict[str, Any] = {"k": top_k, "expr": expr}
        if not self._lite:
            kwargs["ranker_type"] = "weighted"
            kwargs["ranker_params"] = {"weights": [0.6, 0.4]}
        raw = await store.asimilarity_search_with_score(query, **kwargs)
        return [SearchHit(document=document, score=float(score)) for document, score in raw]

    def close(self) -> None:
        if self.vectorstore is not None:
            try:
                self.vectorstore.client.close()
            except Exception as exc:
                log_caught(
                    logger,
                    "milvus close failed",
                    exc=exc,
                    level=logging.DEBUG,
                )
            self.vectorstore = None


@register_backend("milvus")
def build_milvus_store(settings: Settings, **overrides: Any) -> VectorStore:
    uri = overrides.get("uri") or settings.vector_milvus_uri
    return MilvusVectorStore(
        uri=uri,
        token=overrides.get("token") or settings.vector_milvus_token,
        db_name=overrides.get("db_name") or settings.vector_milvus_db,
        alias=overrides.get("alias") or "default",
    )
