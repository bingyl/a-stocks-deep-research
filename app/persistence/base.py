from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from app.core.config import BASE_DIR, DATA_DIR, get_settings

DEFAULT_SQLITE_PATH = DATA_DIR / "app_local_sqlite.db"


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def resolve_database_url(url: str | None = None) -> str:
    """规范化 DATABASE_URL 为异步 SQLAlchemy 可用形式。

    - sqlite:///... / sqlite+aiosqlite:///... → sqlite+aiosqlite:///{绝对路径}
    - postgres(ql)://... → postgresql+psycopg://...
    """
    raw = (url if url is not None else get_settings().database_url).strip()
    if not raw:
        raw = f"sqlite+aiosqlite:///{DEFAULT_SQLITE_PATH.as_posix()}"

    lowered = raw.lower()
    if lowered.startswith("mysql"):
        raise ValueError(
            "已不再支持 MySQL，请改用 sqlite+aiosqlite:///... 或 postgresql+psycopg://..."
        )

    if raw.startswith("sqlite"):
        return _normalize_sqlite_url(raw)

    if lowered.startswith("postgres"):
        return _normalize_postgres_url(raw)

    return raw


def _normalize_sqlite_url(raw: str) -> str:
    """统一到 sqlite+aiosqlite:///绝对路径。"""
    ensure_data_dir()
    # strip optional driver
    if raw.startswith("sqlite+aiosqlite:///"):
        path_part = raw[len("sqlite+aiosqlite:///") :]
    elif raw.startswith("sqlite+aiosqlite://"):
        # sqlite+aiosqlite:///:memory: etc.
        return raw
    elif raw.startswith("sqlite:////"):
        path_part = raw[len("sqlite:////") :]
        path = Path("/" + path_part).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite+aiosqlite:///{path.as_posix()}"
    elif raw.startswith("sqlite:///"):
        path_part = raw[len("sqlite:///") :]
    else:
        return raw.replace("sqlite://", "sqlite+aiosqlite://", 1)

    if path_part.startswith("./"):
        path_part = path_part[2:]
    path = Path(path_part)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    else:
        path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _normalize_postgres_url(raw: str) -> str:
    """统一到 postgresql+psycopg://（psycopg3 同步/异步同一驱动）。"""
    if raw.startswith("postgresql+psycopg://"):
        return raw
    if raw.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + raw[len("postgresql+asyncpg://") :]
    if raw.startswith("postgresql+"):
        rest = raw.split("://", 1)[1]
        return f"postgresql+psycopg://{rest}"
    if raw.startswith("postgresql://"):
        return "postgresql+psycopg://" + raw[len("postgresql://") :]
    if raw.startswith("postgres://"):
        return "postgresql+psycopg://" + raw[len("postgres://") :]
    return raw


def masked_database_url(url: str | None = None) -> str:
    """日志/元数据用：隐藏密码。"""
    resolved = resolve_database_url(url)
    try:
        parsed = urlparse(resolved)
        if not parsed.password:
            return resolved
        netloc = parsed.netloc
        if "@" in netloc and ":" in netloc.split("@", 1)[0]:
            userinfo, host_info = netloc.rsplit("@", 1)
            user = userinfo.split(":", 1)[0]
            netloc = f"{user}:***@{host_info}"
        return urlunparse(parsed._replace(netloc=netloc))
    except (ValueError, TypeError, AttributeError):
        return resolved.split("@")[-1] if "@" in resolved else resolved


def dialect_name(url: str | None = None) -> str:
    resolved = resolve_database_url(url)
    if resolved.startswith("sqlite"):
        return "sqlite"
    if resolved.startswith("postgresql") or resolved.startswith("postgres"):
        return "postgresql"
    raise ValueError(
        "当前仅支持 Sqlite 和 PostgreSQL，请改用 sqlite+aiosqlite:///... "
        "或 postgresql+psycopg://..."
    )


def uses_async_engine(url: str | None = None) -> bool:
    """业务库一律 AsyncEngine。"""
    _ = url
    return True


def postgres_psycopg_conninfo(url: str | None = None) -> str:
    """供 psycopg / PostgresStore / PostgresSaver：去掉 SQLAlchemy 的 +psycopg 前缀。"""
    resolved = resolve_database_url(url)
    if resolved.startswith("postgresql+psycopg://"):
        return "postgresql://" + resolved[len("postgresql+psycopg://") :]
    if resolved.startswith("postgresql+"):
        rest = resolved.split("://", 1)[1]
        return f"postgresql://{rest}"
    return resolved


def sqlite_path_from_database_url(url: str | None = None) -> Path:
    """从 DATABASE_URL 解析本地 sqlite 文件路径。"""
    resolved = resolve_database_url(url)
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if resolved.startswith(prefix):
            return Path(resolved[len(prefix) :])
    if resolved.startswith("sqlite+aiosqlite:////") or resolved.startswith("sqlite:////"):
        # absolute unix-style
        rest = resolved.split(":///", 1)[1]
        return Path("/" + rest)
    raise ValueError(f"无法从 DATABASE_URL 解析 sqlite 路径: {resolved!r}")


def sqlite_checkpointer_path(url: str | None = None) -> Path:
    """SQLite 下 checkpointer 旁路文件，避免与业务库写锁互抢。"""
    main = sqlite_path_from_database_url(url)
    return main.with_name(f"{main.stem}.checkpoints{main.suffix}")


def sqlite_docstore_path(url: str | None = None) -> Path:
    """SQLite 下 DocStore 旁路文件，与业务库分离写锁。"""
    main = sqlite_path_from_database_url(url)
    return main.with_name(f"{main.stem}.docstore{main.suffix}")


def apply_sqlite_concurrency_pragmas(conn: Any) -> None:
    """同步 sqlite3 / aiosqlite DBAPI 连接：WAL + busy_timeout。"""
    cursor = conn.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=60000")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


async def apply_sqlite_concurrency_pragmas_async(conn: Any) -> None:
    """aiosqlite 连接（checkpointer 等直连）：同上。"""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA busy_timeout=60000")
    await conn.execute("PRAGMA foreign_keys=ON")
    await conn.commit()
