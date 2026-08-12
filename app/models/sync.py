from __future__ import annotations

from pydantic import BaseModel, Field


class SyncResult(BaseModel):
    ok: bool
    kind: str
    added: int = 0
    updated: int = 0
    delisted: int = 0
    industry_filled: int = 0
    listed_count: int = 0
    database_url: str = ""
    db_dialect: str = ""
    # 兼容旧字段：历史上为 SQLite 文件路径
    db_path: str = Field(default="", description="兼容字段，等同于 database_url")
    started_at: str = ""
    finished_at: str = ""
    message: str = ""
