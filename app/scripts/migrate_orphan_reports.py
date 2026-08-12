"""一次性迁移：把 user_id 为空的旧深研报告归到指定用户（通常是 admin）。

用法（项目根目录）::

    # 归到 .env 的 AUTH_BOOTSTRAP_USERNAME（默认 admin）；不存在则按引导密码创建
    python -m app.scripts.migrate_orphan_reports

    # 指定用户
    python -m app.scripts.migrate_orphan_reports --username admin

    # 只预览不写库
    python -m app.scripts.migrate_orphan_reports --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import selectors
import sys

from sqlalchemy import func, select, update

from app.core.config import get_settings
from app.core.logging import setup_logging
from app.persistence.db import init_db, reset_engine
from app.persistence.db.models import ResearchReport
from app.persistence.db.session import async_session_scope
from app.services import users as users_svc

logger = logging.getLogger(__name__)


async def _resolve_owner(username: str) -> dict:
    settings = get_settings()
    name = (username or "").strip()
    if not name:
        raise SystemExit("未指定目标用户名（可用 --username 或配置 AUTH_BOOTSTRAP_USERNAME）")

    existing = await users_svc.get_user_by_username(name)
    if existing is not None:
        return {
            "id": int(existing.id),
            "username": existing.username,
            "created": False,
        }

    pwd = (settings.auth_bootstrap_password or "").strip()
    if name == (settings.auth_bootstrap_username or "").strip() and pwd:
        created = await users_svc.create_user(name, pwd)
        logger.info("已创建引导用户 id=%s username=%s", created["id"], name)
        return {"id": int(created["id"]), "username": name, "created": True}

    raise SystemExit(
        f"用户「{name}」不存在。请先登录/注册，或配置 AUTH_BOOTSTRAP_USERNAME/PASSWORD 后重试。"
    )


async def migrate_orphan_reports(*, username: str, dry_run: bool) -> int:
    await reset_engine()
    await init_db(force=True)
    owner = await _resolve_owner(username)
    uid = int(owner["id"])

    async with async_session_scope() as session:
        orphan_count = int(
            await session.scalar(
                select(func.count())
                .select_from(ResearchReport)
                .where(ResearchReport.user_id.is_(None))
            )
            or 0
        )
        if orphan_count == 0:
            logger.info("没有 user_id 为空的报告，无需迁移")
            return 0

        if dry_run:
            logger.info(
                "[dry-run] 将把 %s 条报告归到用户 #%s（%s）",
                orphan_count,
                uid,
                owner["username"],
            )
            return orphan_count

        result = await session.execute(
            update(ResearchReport)
            .where(ResearchReport.user_id.is_(None))
            .values(user_id=uid)
        )
        updated = int(result.rowcount or 0)
        logger.info(
            "已将 %s 条无主报告归到用户 #%s（%s）",
            updated,
            uid,
            owner["username"],
        )
        return updated


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    settings = get_settings()
    default_user = (settings.auth_bootstrap_username or "admin").strip() or "admin"
    p = argparse.ArgumentParser(description="将无主深研报告归到指定用户")
    p.add_argument(
        "--username",
        default=default_user,
        help=f"目标用户名（默认 {default_user}）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计将迁移的条数，不写库",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    setup_logging()
    args = _parse_args(argv)

    async def _run() -> int:
        return await migrate_orphan_reports(
            username=args.username, dry_run=bool(args.dry_run)
        )

    if sys.platform == "win32":
        from app.persistence.base import dialect_name

        if dialect_name() == "postgresql":
            n = asyncio.run(
                _run(),
                loop_factory=lambda: asyncio.SelectorEventLoop(
                    selectors.SelectSelector()
                ),
            )
        else:
            n = asyncio.run(_run())
    else:
        n = asyncio.run(_run())

    print(f"done orphan_reports={n} dry_run={bool(args.dry_run)}")


if __name__ == "__main__":
    main()
