import argparse
import logging
import os
from datetime import UTC, datetime

import discord
from discord.ext import commands

from .cog import MeetingCog
from .config import Config
from .newsletter_cog import NewsletterCog
from .storage import PURPOSE_ANNOUNCEMENT, Store

log = logging.getLogger(__name__)


class MeetingBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.none()
        intents.guilds = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
        self.config = config
        self.store = Store(config.database_path)
        self.meeting_cog: MeetingCog | None = None
        self.newsletter_cog: NewsletterCog | None = None
        self._commands_synced = False

    async def setup_hook(self) -> None:
        self.meeting_cog = MeetingCog(self, self.config, self.store)
        await self.add_cog(self.meeting_cog)
        self.newsletter_cog = NewsletterCog(self, self.config, self.store)
        await self.add_cog(self.newsletter_cog)

    async def _sync_guild(self, guild: discord.Guild) -> None:
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except discord.HTTPException as exc:
            log.error(
                "Command registration failed guild=%s HTTP=%s; check bot installation and retry",
                guild.id,
                exc.status,
            )
        else:
            log.info("Slash commands registered for guild %s", guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self._sync_guild(guild)

    async def on_ready(self) -> None:
        log.info("Bot connected: %s (%d guilds)", self.user, len(self.guilds))
        if self._commands_synced:
            return
        self._commands_synced = True
        try:
            await self.tree.sync()
        except discord.HTTPException as exc:
            log.error("Global command registration failed HTTP=%s", exc.status)
        else:
            log.info("Slash commands registered globally (propagation may take up to an hour)")
        # Also register per already-joined guild so commands appear immediately there.
        for guild in self.guilds:
            await self._sync_guild(guild)

    async def close(self) -> None:
        if self.newsletter_cog is not None:
            await self.remove_cog(self.newsletter_cog.qualified_name)
            self.newsletter_cog = None
        if self.meeting_cog is not None:
            await self.remove_cog(self.meeting_cog.qualified_name)
            self.meeting_cog = None
        self.store.close()
        await super().close()


def _migrate_legacy_channels(store: Store) -> None:
    """One-time convenience: earlier versions configured one guild/channel pair via .env."""
    now = datetime.now(UTC)
    migrated = False
    for guild_var, channel_var in (
        ("DISCORD_GUILD_ID", "DISCORD_ANNOUNCEMENT_CHANNEL_ID"),
        ("DISCORD_TEST_GUILD_ID", "DISCORD_TEST_ANNOUNCEMENT_CHANNEL_ID"),
    ):
        guild_id = os.environ.get(guild_var, "").strip()
        channel_id = os.environ.get(channel_var, "").strip()
        if guild_id.isdigit() and channel_id.isdigit():
            migrated |= store.add_channel(
                int(guild_id), int(channel_id), PURPOSE_ANNOUNCEMENT, now
            )
    if migrated:
        log.warning(
            "Legacy %s/%s env vars migrated into the channel list; remove them from .env and "
            "manage channels with /공지채널추가, /공지채널제거, /공지채널목록.",
            "DISCORD_GUILD_ID",
            "DISCORD_ANNOUNCEMENT_CHANNEL_ID",
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check-config", action="store_true", help="Validate .env without connecting"
    )
    args = parser.parse_args()
    try:
        config = Config.from_env()
    except ValueError as exc:
        parser.exit(2, f"설정 오류: {exc}\n")
    if args.check_config:
        print("환경변수 형식 확인 완료. 토큰 유효성·채널 권한은 실제 연결 시 확인됩니다.")
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    bot = MeetingBot(config)
    _migrate_legacy_channels(bot.store)
    bot.run(config.token, log_handler=None)


if __name__ == "__main__":
    main()
