"""分析轮次 id 生成（独立模块，避免 reports ↔ analysis_jobs 循环依赖）。"""

from __future__ import annotations

import uuid


def new_run_id() -> str:
    return uuid.uuid4().hex
