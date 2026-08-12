from __future__ import annotations

import logging
import logging.config
import sys
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Iterator

from app.core.config import BASE_DIR, get_settings

LOG_DIR = BASE_DIR / "logs"
DEBUG_LOG_FILE = LOG_DIR / "app_debug.log"

_LOGGING_CONFIGURED = False

# 当前分析轮次：异步任务 / to_thread 会自动继承
_analysis_run_id: ContextVar[str | None] = ContextVar("analysis_run_id", default=None)


def get_log_run_id() -> str | None:
    rid = _analysis_run_id.get()
    return rid or None


def set_log_run_id(run_id: str | None) -> Token:
    return _analysis_run_id.set((run_id or "").strip() or None)


def reset_log_run_id(token: Token) -> None:
    _analysis_run_id.reset(token)


@contextmanager
def bind_log_run_id(run_id: str | None) -> Iterator[None]:
    """分析任务作用域内绑定 run_id，日志自动带 [Run_id:xxx]。"""
    token = set_log_run_id(run_id)
    try:
        yield
    finally:
        reset_log_run_id(token)


class AnalysisRunIdFilter(logging.Filter):
    """把 ContextVar 中的 analysis_run_id 写成 [Run_id:xxx] 前缀。"""

    _prefix_marker = "[Run_id:"

    def filter(self, record: logging.LogRecord) -> bool:
        rid = get_log_run_id()
        if not rid:
            return True
        msg = record.getMessage()
        if msg.startswith(self._prefix_marker):
            return True
        # 改 msg/% 参数，避免 Formatter 再用原 args
        record.msg = f"[Run_id:{rid}] {msg}"
        record.args = ()
        return True


def log_caught(
    log: logging.Logger,
    msg: str,
    *args: Any,
    exc: BaseException | None = None,
    level: int = logging.WARNING,
) -> None:
    """``except Exception`` 统一落日志：异常类型 + 消息 + 堆栈。

    用法::

        except Exception as exc:
            log_caught(logger, "拉取行情失败 code=%s", code, exc=exc)
    """
    err = exc if exc is not None else sys.exc_info()[1]
    if err is None:
        log.log(level, msg, *args)
        return
    # 把类型/消息拼进同一条，便于检索；exc_info 保留完整堆栈
    log.log(
        level,
        msg + " | %s: %s",
        *args,
        type(err).__name__,
        err,
        exc_info=err,
    )


def _install_run_id_filter() -> None:
    filt = AnalysisRunIdFilter()
    root = logging.getLogger()
    for handler in root.handlers:
        if not any(isinstance(f, AnalysisRunIdFilter) for f in handler.filters):
            handler.addFilter(filt)


def setup_logging(level: str = "INFO") -> None:
    """统一日志：控制台 + logs/app.log；DEBUG=true 时额外 logs/app_debug.log。"""
    global _LOGGING_CONFIGURED
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "app.log"
    settings = get_settings()
    debug_on = bool(settings.debug)

    handlers = ["console", "file"]
    handler_cfg: dict = {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "standard",
            "level": level,
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": str(log_file),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "level": level,
        },
    }
    if debug_on:
        handlers.append("debug_file")
        handler_cfg["debug_file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "formatter": "standard",
            "filename": str(DEBUG_LOG_FILE),
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "encoding": "utf-8",
            "level": "DEBUG",
        }

    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "standard": {
                    "format": "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
                    "datefmt": "%Y-%m-%d %H:%M:%S",
                }
            },
            "handlers": handler_cfg,
            "root": {
                "handlers": handlers,
                "level": "DEBUG" if debug_on else level,
            },
            "loggers": {
                "uvicorn": {"level": "INFO"},
                "uvicorn.error": {"level": "INFO"},
                "uvicorn.access": {"level": "INFO"},
                "httpx": {"level": "WARNING"},
                "httpcore": {"level": "WARNING"},
                "urllib3": {"level": "WARNING"},
                "openai": {"level": "WARNING"},
                # checkpointer 写入会带 MessagePack 二进制；DEBUG 会刷出 \xe5\x88… 乱码长行
                "aiosqlite": {"level": "WARNING"},
                "sqlite3": {"level": "WARNING"},
                "langgraph": {"level": "INFO"},
                "deepagents": {"level": "INFO"},
                # RAG / agent 细节进 debug 文件
                "app.rag": {"level": "DEBUG" if debug_on else "INFO", "propagate": True},
                "app.agent": {"level": "DEBUG" if debug_on else "INFO", "propagate": True},
                "app.integrations.embedding": {
                    "level": "DEBUG" if debug_on else "INFO",
                    "propagate": True,
                },
            },
        }
    )
    _install_run_id_filter()
    if _LOGGING_CONFIGURED:
        return
    _LOGGING_CONFIGURED = True
    logger = logging.getLogger(__name__)
    logger.info("logging initialized -> %s debug=%s", log_file, debug_on)
    if debug_on:
        logger.info("debug logging -> %s", DEBUG_LOG_FILE)
