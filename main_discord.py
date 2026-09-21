#!/usr/bin/env python3
"""The Discord process (§71).

The human interface. It holds no broker and no data provider, so it cannot trade even by
mistake: it writes ``trade_requests`` for a human to confirm, ``control_requests`` for the
executor to perform, ``settings.trading_enabled``, and audit rows. Everything else it does
is reading.

Requires ``AUREON_DISCORD_TOKEN``, ``AUREON_DISCORD_GUILD_ID`` and
``AUREON_AUTHORIZED_USER_IDS``. It refuses to start without the token or the allowlist:
an empty allowlist authorises nobody, so starting would produce a bot that answers no one
while looking healthy -- worse than a clear failure.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aureon.config import AureonConfig
from aureon.services.heartbeat_service import HeartbeatService
from aureon.storage import paths

log = logging.getLogger("aureon.discord")


class MissingConfiguration(RuntimeError):
    """Required Discord configuration is absent."""


def check_configuration(config: AureonConfig) -> None:
    """Fail fast and specifically, rather than starting a bot that cannot work."""
    problems: list[str] = []
    if not config.discord_token:
        problems.append("AUREON_DISCORD_TOKEN is not set")
    if not config.authorized_user_ids:
        problems.append(
            "AUREON_AUTHORIZED_USER_IDS is empty — an empty allowlist authorises nobody, "
            "so the bot would answer no one while appearing healthy"
        )
    if not config.discord_guild_id:
        # A warning, not a refusal: the bot works, but its commands are not synced, and a
        # global trading command would appear in every server the app joins.
        log.warning(
            "AUREON_DISCORD_GUILD_ID is not set; commands will not be synced to a guild"
        )
    if problems:
        raise MissingConfiguration("; ".join(problems))


async def run(config: AureonConfig) -> None:
    from aureon.discord.bot import AureonBot
    from aureon.discord.context import build_context
    from aureon.storage.firebase_service import get_client
    from aureon.storage.system_state_repository import HeartbeatRepository

    client = get_client(
        project_id=config.firebase_project_id,
        emulator_host=config.firestore_emulator_host,
    )
    context = build_context(config, client)
    bot = AureonBot(context)

    heartbeat = HeartbeatService(
        HeartbeatRepository(client),
        paths.SERVICE_DISCORD,
        detail_provider=lambda: {"guild": config.discord_guild_id},
    )
    heartbeat.start()
    # 11B: handed to the bot so its market-follower task can slow it down at the close.
    # The bot does not build it -- one owner for the thread that has to be stopped.
    bot.heartbeat = heartbeat
    try:
        await bot.start(config.discord_token or "")
    finally:
        heartbeat.stop()
        await bot.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = AureonConfig.from_env()
    try:
        check_configuration(config)
    except MissingConfiguration as exc:
        log.error("cannot start: %s", exc)
        return 2

    try:
        asyncio.run(run(config))
    except KeyboardInterrupt:  # pragma: no cover - interactive
        log.info("interrupted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
