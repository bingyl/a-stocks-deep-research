"""LangGraph Checkpointer：短时线程记忆。

- Postgres：与业务库同库（``AsyncPostgresSaver``）
- Sqlite：旁路 ``*.checkpoints.db``（``AsyncSqliteSaver``），避免写锁堵业务更新
"""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver

from app.persistence.checkpointer.factory import (
    analysis_thread_prefix,
    delete_checkpoints_for_report,
    get_checkpointer,
    reset_checkpointer,
    setup_checkpointer,
)

__all__ = [
    "BaseCheckpointSaver",
    "analysis_thread_prefix",
    "delete_checkpoints_for_report",
    "get_checkpointer",
    "reset_checkpointer",
    "setup_checkpointer",
]
