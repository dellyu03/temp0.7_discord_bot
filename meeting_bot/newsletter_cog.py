import asyncio
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands

from .config import Config
from .newsletter import NewsletterError, NewsletterService, build_newsletter_embed
from .storage import PURPOSE_NEWSLETTER, Store

log = logging.getLogger(__name__)


class NewsletterCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        config: Config,
        store: Store,
        *,
        service: NewsletterService | None = None,
    ):
        self.bot = bot
        self.config = config
        self.store = store
        self.service = service
        if service is None and config.openai_api_key:
            self.service = NewsletterService(
                config.openai_api_key,
                config.openai_model,
                config.news_source_list_path,
            )
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "서버 관리자(Administrator)만 실행할 수 있습니다."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"잠시 후 다시 실행하세요. 약 {math.ceil(error.retry_after)}초 남았습니다."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "서버 안에서만 사용할 수 있습니다."
        else:
            log.error("Newsletter command failed: %s", type(error).__name__)
            message = "뉴스레터 명령을 처리하지 못했습니다. 봇 로그를 확인하세요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _resolve_channel(self, guild_id: int, channel_id: int) -> discord.TextChannel:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                self.store.remove_channel(guild_id, channel_id, PURPOSE_NEWSLETTER)
                raise ValueError(f"채널을 찾을 수 없어 등록에서 제거했습니다: {channel_id}") from None
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
            raise ValueError("뉴스레터 채널은 명령을 실행한 서버의 텍스트 채널이어야 합니다.")
        member = channel.guild.me
        if member is None:
            raise ValueError("봇의 서버 가입 상태를 확인하세요.")
        permissions = channel.permissions_for(member)
        if not (
            permissions.view_channel and permissions.send_messages and permissions.embed_links
        ):
            raise ValueError("봇에 채널 보기·메시지 보내기·링크 임베드 권한이 필요합니다.")
        return channel

    @app_commands.command(
        name="뉴스레터발송",
        description="원천 소스에서 분야별 최신 뉴스를 골라 뉴스레터 채널에 즉시 보냅니다.",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.cooldown(1, 300, key=lambda interaction: interaction.guild_id)
    async def send_newsletter(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.followup.send("서버 안에서만 사용할 수 있습니다.", ephemeral=True)
            return
        if self.service is None:
            await interaction.followup.send(
                "OPENAI_API_KEY가 설정되지 않았습니다. .env에 키를 추가하고 봇을 재시작하세요.",
                ephemeral=True,
            )
            return

        channel_ids = self.store.list_channels(guild_id, PURPOSE_NEWSLETTER)
        if not channel_ids:
            await interaction.followup.send(
                "등록된 뉴스레터 채널이 없습니다. /뉴스레터채널추가로 먼저 등록해 주세요.",
                ephemeral=True,
            )
            return

        lock = self.locks[guild_id]
        if lock.locked():
            await interaction.followup.send(
                "이 서버의 뉴스레터를 이미 생성하고 있습니다. 완료 후 다시 시도하세요.",
                ephemeral=True,
            )
            return

        async with lock:
            destinations: list[tuple[int, discord.TextChannel]] = []
            failed: list[int] = []
            for channel_id in channel_ids:
                try:
                    channel = await self._resolve_channel(guild_id, channel_id)
                except ValueError as exc:
                    log.error(
                        "Newsletter channel unavailable guild=%s channel=%s: %s",
                        guild_id,
                        channel_id,
                        exc,
                    )
                    failed.append(channel_id)
                else:
                    destinations.append((channel_id, channel))
            if not destinations:
                await interaction.followup.send(
                    "사용 가능한 뉴스레터 채널이 없습니다. 채널 권한을 확인해 주세요.",
                    ephemeral=True,
                )
                return

            generated_at = datetime.now(UTC).astimezone(self.config.timezone)
            try:
                edition = await self.service.generate(generated_at)
                embed = build_newsletter_embed(edition, generated_at, self.config.openai_model)
            except NewsletterError as exc:
                log.error("Newsletter generation failed guild=%s: %s", guild_id, exc)
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            sent: list[int] = []
            for channel_id, channel in destinations:
                try:
                    # One Discord message with one long embed containing all five categories.
                    await channel.send(
                        embed=embed,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException as exc:
                    log.error(
                        "Newsletter send failed guild=%s channel=%s HTTP=%s",
                        guild_id,
                        channel_id,
                        exc.status,
                    )
                    failed.append(channel_id)
                else:
                    sent.append(channel_id)

        result = (
            "뉴스레터 한 건을 전송했습니다:\n" + "\n".join(f"<#{channel_id}>" for channel_id in sent)
            if sent
            else "전송에 성공한 뉴스레터 채널이 없습니다."
        )
        if failed:
            result += "\n전송 실패: " + ", ".join(f"<#{channel_id}>" for channel_id in failed)
        await interaction.followup.send(result, ephemeral=True)
