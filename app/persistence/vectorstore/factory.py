"""向量库 / 父子索引工厂。"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from app.core.config import get_settings
from app.core.logging import log_caught
from app.integrations.embedding import reset_embeddings
from app.persistence.docstore.factory import get_doc_store, reset_doc_store
from app.persistence.docstore.parent_child import DefaultParentChildIndex, ParentChildIndex
from app.persistence.docstore.parents import ParentDocRepository
from app.persistence.vectorstore.base import VectorStore
from app.persistence.vectorstore.registry import (
    get_backend_factory,
    registered_backend_names,
)

# 导入即注册（backends 只依赖 registry，无循环）
from app.persistence.vectorstore.backends import chroma_backend as _chroma_backend  # noqa: F401
from app.persistence.vectorstore.backends import milvus_backend as _milvus_backend  # noqa: F401

logger = logging.getLogger(__name__)


def list_backends() -> list[str]:
    return registered_backend_names()


def create_vector_store(
    backend: str | None = None,
    **overrides: Any,
) -> VectorStore:
    """创建向量库实例（不缓存）。``overrides`` 传给具体后端工厂。"""
    settings = get_settings()
    name = (backend or settings.vector_backend or "chroma").strip().lower()
    factory = get_backend_factory(name)
    if factory is None:
        known = ", ".join(list_backends()) or "(none)"
        raise ValueError(f"未知向量后端 {name!r}，已注册: {known}")
    return factory(settings=settings, **overrides)


@lru_cache
def get_vector_store() -> VectorStore:
    """进程内单例，配置来自 Settings。"""
    return create_vector_store()


def reset_vector_store() -> None:
    """关闭并丢弃缓存实例（测试/热更新配置）。"""
    try:
        store = get_vector_store()
        store.close()
    except Exception as exc:
        log_caught(
            logger,
            "close vector store during reset failed",
            exc=exc,
            level=logging.DEBUG,
        )
    get_vector_store.cache_clear()
    get_parent_child_index.cache_clear()
    reset_doc_store()
    reset_embeddings()
    try:
        from app.rag.ingest import reset_ingest_state

        reset_ingest_state()
    except Exception as exc:
        log_caught(
            logger,
            "reset ingest state during vector store reset failed",
            exc=exc,
            level=logging.DEBUG,
        )


def create_parent_child_index(
    *,
    backend: str | None = None,
    **vector_overrides: Any,
) -> ParentChildIndex:
    vectors = create_vector_store(backend=backend, **vector_overrides)
    docs = ParentDocRepository(get_doc_store())
    return DefaultParentChildIndex(vectors, docs)


@lru_cache
def get_parent_child_index() -> ParentChildIndex:
    return DefaultParentChildIndex(
        get_vector_store(),
        ParentDocRepository(get_doc_store()),
    )
