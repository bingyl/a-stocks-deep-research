"""向量库 / 父子文档公共类型（与具体后端解耦）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Sequence

from langchain_core.documents import Document

# 相似度度量；后端可映射到自身枚举（Chroma / Milvus COSINE/IP/L2）
DistanceMetric = Literal["cosine", "ip", "l2"]

# 子块 metadata 中指向父文档的标准键
META_PARENT_ID = "parent_id"


@dataclass(slots=True)
class VectorRecord:
    """一条可写入的稠密向量（通常对应 child 切片）。"""

    id: str
    vector: Sequence[float]
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class VectorTextRecord:
    """一条待嵌入写入的文本记录（通常对应 child 切片）。"""

    id: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class SearchHit:
    """检索命中。

    score：Chroma 余弦场景通常越大越好；Milvus 加权融合分数可能仅作排序参考。
    """

    document: Document
    score: float

    @property
    def id(self) -> str | None:
        return self.document.id

    @property
    def metadata(self) -> dict[str, Any]:
        return self.document.metadata or {}


@dataclass(slots=True)
class MetadataFilter:
    """可移植的轻量过滤条件。

    - equals: 字段等于标量
    - any_of: 字段属于给定集合（IN）

    复杂表达式可通过 ``extra`` 传给支持的后端（如 Milvus 原生 expr），
    不支持的后端应忽略或显式报错。
    """

    equals: dict[str, Any] = field(default_factory=dict)
    any_of: dict[str, Sequence[Any]] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.equals and not self.any_of and not self.extra


@dataclass(slots=True)
class ParentDocument:
    """父文档：完整上下文，存 DocStore，一般不直接建稠密索引。"""

    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChildDocument:
    """子文档：用于向量召回；必须带 parent_id。"""

    id: str
    parent_id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    vector: Sequence[float] = field(default_factory=list)

    def to_vector_record(self) -> VectorTextRecord:
        meta = dict(self.metadata or {})
        meta[META_PARENT_ID] = self.parent_id
        return VectorTextRecord(
            id=self.id,
            text=self.text,
            metadata=meta,
        )


def normalize_filter(
    filt: MetadataFilter | dict[str, Any] | None,
) -> MetadataFilter | None:
    """允许调用方传入 dict：标量=equals，list/tuple/set=any_of。"""
    if filt is None:
        return None
    if isinstance(filt, MetadataFilter):
        return filt if not filt.is_empty() else None
    equals: dict[str, Any] = {}
    any_of: dict[str, Sequence[Any]] = {}
    for key, val in filt.items():
        if isinstance(val, (list, tuple, set)):
            any_of[key] = list(val)
        else:
            equals[key] = val
    out = MetadataFilter(equals=equals, any_of=any_of)
    return None if out.is_empty() else out


def metadata_matches(meta: dict[str, Any] | None, filt: MetadataFilter | None) -> bool:
    """客户端过滤（无服务端 filter 时使用）。"""
    if filt is None or filt.is_empty():
        return True
    data = meta or {}
    for key, expected in filt.equals.items():
        if data.get(key) != expected:
            return False
    for key, allowed in filt.any_of.items():
        if data.get(key) not in allowed:
            return False
    return True
