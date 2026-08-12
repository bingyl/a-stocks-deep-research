"""在同步线程（如 APScheduler）中跑异步协程。"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def run_coro(coro: Coroutine[Any, Any, T]) -> T:
    """从无事件循环的同步上下文执行 coroutine。

    Windows + PostgreSQL（psycopg）：必须用 SelectorEventLoop，Proactor 会报 InterfaceError。
    """
    if sys.platform == "win32":
        from app.persistence.base import dialect_name

        if dialect_name() == "postgresql":
            # 与 main.py 一致：走 policy，避免手搓 SelectorEventLoop 绕过调试器补丁
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            return asyncio.run(coro, loop_factory=asyncio.new_event_loop)
    return asyncio.run(coro)
