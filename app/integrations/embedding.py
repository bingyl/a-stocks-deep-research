"""DashScope Embedding（langchain_openai.OpenAIEmbeddings，兼容 OpenAI API）。"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Sequence

from langchain_openai import OpenAIEmbeddings

from app.core.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache
def get_embeddings() -> OpenAIEmbeddings:
    settings = get_settings()
    settings.require_embedding()
    # DashScope text-embedding-v4：单请求最多 10 条；关闭 ctx length 检查避免非 OpenAI tokenizer 误伤
    chunk = max(1, min(int(settings.embedding_batch_size), 10))
    emb = OpenAIEmbeddings(
        model=settings.embedding_model,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        dimensions=settings.embedding_dim,
        chunk_size=chunk,
        check_embedding_ctx_length=False,
    )
    logger.debug(
        "embeddings ready model=%s dim=%s chunk_size=%s",
        settings.embedding_model,
        settings.embedding_dim,
        chunk,
    )
    return emb


def reset_embeddings() -> None:
    get_embeddings.cache_clear()


def _clean_texts(texts: Sequence[str]) -> list[str]:
    return [(t or "").strip() or " " for t in texts]


def _chunk_size(batch_size: int | None) -> int:
    settings = get_settings()
    size = batch_size if batch_size is not None else settings.embedding_batch_size
    return max(1, min(int(size), 10))


async def aembed_texts(
    texts: Sequence[str], *, batch_size: int | None = None
) -> list[list[float]]:
    """异步批量嵌入（OpenAIEmbeddings.aembed_documents）。"""
    cleaned = _clean_texts(texts)
    if not cleaned:
        return []
    size = _chunk_size(batch_size)
    logger.debug("向量化文本 %s 条（批大小 %s）", len(cleaned), size)
    vectors = await get_embeddings().aembed_documents(cleaned, chunk_size=size)
    if len(vectors) != len(cleaned):
        raise RuntimeError(
            f"embedding 数量不匹配: expect={len(cleaned)} got={len(vectors)}"
        )
    return vectors


async def aembed_query(text: str) -> list[float]:
    """异步单条查询嵌入。"""
    q = (text or "").strip() or " "
    return await get_embeddings().aembed_query(q)


def embed_texts(
    texts: Sequence[str], *, batch_size: int | None = None
) -> list[list[float]]:
    """同步批量嵌入（无事件循环时的兜底）。"""
    cleaned = _clean_texts(texts)
    if not cleaned:
        return []
    size = _chunk_size(batch_size)
    logger.debug("向量化文本 %s 条（批大小 %s，同步）", len(cleaned), size)
    vectors = get_embeddings().embed_documents(cleaned, chunk_size=size)
    if len(vectors) != len(cleaned):
        raise RuntimeError(
            f"embedding 数量不匹配: expect={len(cleaned)} got={len(vectors)}"
        )
    return vectors


def embed_query(text: str) -> list[float]:
    q = (text or "").strip() or " "
    return get_embeddings().embed_query(q)
