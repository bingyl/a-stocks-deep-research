"""重置用户密码。

用法::

    python -m app.scripts.reset_user_password --username admin --password '新密码'
    # 或使用 .env 的 AUTH_BOOTSTRAP_PASSWORD：
    python -m app.scripts.reset_user_password --username admin --from-env
"""

from __future__ import annotations

import argparse
import asyncio
import selectors
import sys

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.persistence.db import init_db, reset_engine
from app.services import users as users_svc


async def _run(*, username: str, password: str) -> None:
    await reset_engine()
    await init_db(force=True)
    updated = await users_svc.set_user_password(username, password)
    print(f"password updated user_id={updated['id']} username={updated['username']}")


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    settings = get_settings()
    p = argparse.ArgumentParser(description="重置用户密码")
    p.add_argument("--username", default=settings.auth_bootstrap_username or "admin")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--password", help="新密码")
    g.add_argument(
        "--from-env",
        action="store_true",
        help="使用 AUTH_BOOTSTRAP_PASSWORD",
    )
    args = p.parse_args(argv)
    password = (
        settings.auth_bootstrap_password if args.from_env else (args.password or "")
    )
    if not password:
        raise SystemExit("密码为空")

    async def runner() -> None:
        await _run(username=args.username, password=password)

    if sys.platform == "win32":
        from app.persistence.base import dialect_name

        if dialect_name() == "postgresql":
            asyncio.run(
                runner(),
                loop_factory=lambda: asyncio.SelectorEventLoop(
                    selectors.SelectSelector()
                ),
            )
            return
    asyncio.run(runner())


if __name__ == "__main__":
    main()
