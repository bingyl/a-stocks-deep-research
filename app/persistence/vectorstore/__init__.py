"""可插拔向量库类型与基类导出。

工厂请从子模块导入：

- ``app.persistence.vectorstore.factory``
- 父子索引：``app.persistence.docstore.parent_child``
- 父文档 Store：``app.persistence.docstore``
"""

from __future__ import annotations

from app.persistence.vectorstore.base import VectorStore
from app.persistence.vectorstore.registry import register_backend
from app.persistence.vectorstore.types import (
    META_PARENT_ID,
    ChildDocument,
    DistanceMetric,
    MetadataFilter,
    ParentDocument,
    SearchHit,
    VectorRecord,
    VectorTextRecord,
)

__all__ = [
    "META_PARENT_ID",
    "ChildDocument",
    "DistanceMetric",
    "MetadataFilter",
    "ParentDocument",
    "SearchHit",
    "VectorRecord",
    "VectorTextRecord",
    "VectorStore",
    "register_backend",
]
