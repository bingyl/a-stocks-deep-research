"""向量库抽象接口。新增后端只需实现本 ABC 并在 factory 注册。"""

from __future__ import annotations

import typing
from abc import ABC, abstractmethod
from typing import Any, Sequence

from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores.base import VectorStore as BaseVectorStore
from app.persistence.vectorstore.types import (
    MetadataFilter,
    SearchHit,
    VectorRecord,
    VectorTextRecord,
)


class VectorStore(ABC):
    """稠密向量库统一门面。"""

    backend_name: str = "abstract"

    def __init__(self, **kwargs):
        self.collection = ""
        self.vectorstore: typing.Optional[BaseVectorStore] = None
        self.metric: typing.Literal["l2", "cosine", "ip"] = "cosine"

    @property
    def embeddings(self) -> Embeddings:
        return self.vectorstore.embeddings

    @abstractmethod
    def initialize(self, collection: str, embeddings: Embeddings, **kwargs) -> None:
        """初始化相关操作"""

    @abstractmethod
    def add_texts(self, records: Sequence[VectorTextRecord]) -> int:
        """写入/覆盖向量，返回成功条数。"""

    @abstractmethod
    async def aadd_texts(self, records: Sequence[VectorTextRecord]) -> int:
        """异步写入/覆盖向量，返回成功条数。"""

    @abstractmethod
    def delete(
        self,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        """按 id 和/或 metadata 删除。"""

    @abstractmethod
    async def adelete(
        self,
        ids: Sequence[str] | None = None,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> int:
        """异步按 id 和/或 metadata 删除。"""

    @abstractmethod
    def get(
        self,
        ids: Sequence[str]
    ) -> list[VectorRecord]:
        """按 id 批量读取；缺失的 id 跳过。"""

    @abstractmethod
    async def aget(
        self,
        ids: Sequence[str]
    ) -> list[VectorRecord]:
        """按 id 批量读取；缺失的 id 跳过。"""

    @abstractmethod
    def search(
        self,
        query: str,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None,
    ) -> list[SearchHit]:
        """稠密向量近邻检索。"""

    @abstractmethod
    async def asearch(
        self,
        query: str,
        top_k: int = 5,
        where: MetadataFilter | dict[str, Any] | None = None
    ) -> list[SearchHit]:
        """稠密向量近邻检索。"""

    def close(self) -> None:
        """释放连接/文件句柄；默认无操作。"""

    def __enter__(self) -> VectorStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _safe_name(name: str) -> str:
    safe = name.strip().replace("/", "_").replace("\\", "_")
    if not safe:
        raise ValueError("collection name 不能为空")
    return safe

