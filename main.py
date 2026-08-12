"""本地启动入口。

PyCharm Debug（pydevd）会给 ``asyncio.run`` 打补丁，且不接受 ``loop_factory``。
uvicorn 在 import 时就把 ``asyncio.run`` 绑进 ``uvicorn._compat.asyncio_run``，
只改 ``asyncio.run`` 无效。Debug 下用自实现 runner 覆盖 uvicorn 的引用。

Windows + PostgreSQL（psycopg 异步）：uvicorn 默认 ProactorEventLoop，
psycopg 不支持。用 WindowsSelectorEventLoopPolicy + ``asyncio.new_event_loop``，
避免直接 ``SelectorEventLoop()`` 绕过 PyCharm nest_asyncio 的 loop 补丁
（否则会出现 ``TypeError: 'Task' object is not callable``）。
"""

from __future__ import annotations

import asyncio
import sys


def _under_pydevd() -> bool:
    return any(name.split(".", 1)[0] == "pydevd" for name in sys.modules)


def _install_uvicorn_asyncio_run_compat() -> None:
    """绕过 pydevd 残缺的 asyncio.run，供 uvicorn Server.run 使用。"""

    def asyncio_run(main, *, debug=None, loop_factory=None):  # noqa: ANN001
        # Python 3.11+ Runner 支持 loop_factory，且不经过被 pydevd 改过的 asyncio.run
        with asyncio.Runner(debug=debug, loop_factory=loop_factory) as runner:
            return runner.run(main)

    asyncio.run = asyncio_run  # type: ignore[misc]

    # uvicorn 可能已经 import：必须改模块内绑定，否则仍指向旧函数
    for mod_name in ("uvicorn._compat", "uvicorn.server", "uvicorn.workers"):
        mod = sys.modules.get(mod_name)
        if mod is not None and hasattr(mod, "asyncio_run"):
            setattr(mod, "asyncio_run", asyncio_run)


def _install_windows_selector_loop_for_psycopg() -> None:
    """Windows 下 uvicorn 默认 Proactor；psycopg async 只能用 Selector。"""
    if sys.platform != "win32":
        return
    from app.persistence.base import dialect_name

    if dialect_name() != "postgresql":
        return

    # 让 new_event_loop() 产出 Selector；勿直接返回 SelectorEventLoop 类，
    # 否则 PyCharm nest_asyncio 无法 _patch_loop，Debug 会 Task not callable。
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    import uvicorn.loops.asyncio as uv_asyncio

    def selector_loop_factory(use_subprocess: bool = False):  # noqa: ARG001
        return asyncio.new_event_loop

    uv_asyncio.asyncio_loop_factory = selector_loop_factory  # type: ignore[misc]


# 先打补丁再 import uvicorn，避免 _compat 捕获到残缺的 asyncio.run
if _under_pydevd():
    _install_uvicorn_asyncio_run_compat()


import uvicorn
from app import create_app

app = create_app()

if __name__ == "__main__":
    # 若 uvicorn 在补丁前已被其它路径导入，这里再盖一次
    if _under_pydevd():
        _install_uvicorn_asyncio_run_compat()
    _install_windows_selector_loop_for_psycopg()
    uvicorn.run(app, host="127.0.0.1", port=8000)
