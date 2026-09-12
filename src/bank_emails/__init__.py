"""bank-emails：交易确认单邮件处理 daemon。

当前阶段提供配置管理、数据库表结构与 REST API；邮件采集、LLM 流水线与 Odoo
推送在后续步骤中接入（见 docs/product-design.md §17 里程碑）。
"""

from __future__ import annotations

import argparse
import logging
import sys

from .api import VERSION, create_app, run
from .config import ConfigError, Settings, load_settings
from .db import init_db, open_database
from .store import Store

__version__ = VERSION

__all__ = [
    "ConfigError",
    "Settings",
    "Store",
    "__version__",
    "create_app",
    "init_db",
    "load_settings",
    "main",
    "open_database",
]

logger = logging.getLogger("bank_emails")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bank-emails", description="交易确认单邮件处理 daemon")
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="只校验 .env 配置与数据库可初始化，然后退出（供部署前自检）",
    )
    parser.add_argument("--env-file", default=".env", help="配置文件路径，默认 .env")
    return parser


def main(argv: list[str] | None = None) -> None:
    """程序入口：加载配置 → 初始化数据库 → 启动 REST API。"""
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        print(f"配置错误：\n{exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    logging.getLogger().setLevel(settings.log_level)
    settings.ensure_dirs()
    connection = open_database(settings.db_path)
    store = Store(connection)
    logger.info("数据库就绪：%s", settings.db_path)

    if args.check_config:
        logger.info("配置检查通过：认证方式=%s，库表=%d", settings.auth_mode, store.check_health()["tables"])
        connection.close()
        return

    try:
        run(settings, store)
    finally:
        connection.close()
