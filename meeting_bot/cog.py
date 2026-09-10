import asyncio
import logging
import math
from collections import defaultdict
from datetime import UTC, datetime

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Config
from .messages import format_meeting_notice
from .schedule import WEEKDAYS, due_reminders, next_meeting
from .storage import PURPOSE_ANNOUNCEMENT, PURPOSE_NEWSLETTER, Store

log = logging.getLogger(__name__)

EVERYONE_MENTIONS = discord.AllowedMentions(everyone=True, users=False, roles=False)


class MeetingCog(commands.Cog):
    """One instance serves every guild the bot is installed in; guild_id scopes each operation."""

    def __init__(self, bot: commands.Bot, config: Config, store: Store):
        self.bot, self.config, self.store = bot, config, store
        self.locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def cog_load(self) -> None:
        self.reminders.start()

    async def cog_unload(self) -> None:
        self.reminders.cancel()
        task = self.reminders.get_task()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            message = "서버 관리자(Administrator)만 실행할 수 있습니다."
        elif isinstance(error, app_commands.CommandOnCooldown):
            message = f"잠시 후 다시 실행하세요. 약 {math.ceil(error.retry_after)}초 남았습니다."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "서버 안에서만 사용할 수 있습니다."
        elif isinstance(error, app_commands.CheckFailure):
            message = str(error)
        else:
            log.error("Command failed: %s", type(error).__name__)
            message = "처리하지 못했습니다. 봇 로그와 공지 채널 권한을 확인하세요."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self.locks[guild_id]

    async def _resolve_channel(
        self, guild_id: int, channel_id: int, purpose: str
    ) -> discord.TextChannel:
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.NotFound:
                self.store.remove_channel(guild_id, channel_id, purpose)
                raise ValueError(f"채널을 찾을 수 없어 등록에서 제거했습니다: {channel_id}") from None
        if not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
            raise ValueError("알림 채널은 등록된 서버의 텍스트 채널이어야 합니다.")
        member = channel.guild.me
        if member is None:
            raise ValueError("봇의 서버 가입 상태를 확인하세요.")
        permissions = channel.permissions_for(member)
        if not (permissions.view_channel and permissions.send_messages):
            raise ValueError("봇에 채널 보기·메시지 보내기 권한이 필요합니다.")
        if purpose == PURPOSE_ANNOUNCEMENT and not permissions.mention_everyone:
            raise ValueError("공지 채널에는 전체 멘션(@everyone) 권한도 필요합니다.")
        if purpose == PURPOSE_NEWSLETTER and not permissions.embed_links:
            raise ValueError("뉴스레터 채널에는 링크 임베드 권한도 필요합니다.")
        return channel

    async def _add_purpose_channel(
        self,
        interaction: discord.Interaction,
        채널: discord.TextChannel,
        purpose: str,
        label: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if 채널.guild.id != interaction.guild_id:
            await interaction.followup.send("이 서버의 채널만 등록할 수 있습니다.", ephemeral=True)
            return
        added = self.store.add_channel(interaction.guild_id, 채널.id, purpose, datetime.now(UTC))
        if not added:
            await interaction.followup.send(
                f"<#{채널.id}>은 이미 등록된 {label} 채널입니다.", ephemeral=True
            )
            return
        warning = ""
        try:
            await self._resolve_channel(interaction.guild_id, 채널.id, purpose)
        except ValueError as exc:
            warning = f"\n주의: {exc}"
        await interaction.followup.send(
            f"<#{채널.id}>을 {label} 채널로 등록했습니다.{warning}", ephemeral=True
        )

    async def _remove_purpose_channel(
        self,
        interaction: discord.Interaction,
        채널: discord.TextChannel,
        purpose: str,
        label: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        removed = self.store.remove_channel(interaction.guild_id, 채널.id, purpose)
        message = (
            f"<#{채널.id}>을 {label} 채널에서 제거했습니다."
            if removed
            else f"<#{채널.id}>은 등록되지 않은 {label} 채널입니다."
        )
        await interaction.followup.send(message, ephemeral=True)

    async def _list_purpose_channels(
        self, interaction: discord.Interaction, purpose: str, label: str, add_command: str
    ) -> None:
        channel_ids = self.store.list_channels(interaction.guild_id, purpose)
        if not channel_ids:
            message = f"등록된 {label} 채널이 없습니다. 관리자가 {add_command} 로 등록하세요."
        else:
            lines = "\n".join(f"• <#{channel_id}>" for channel_id in channel_ids)
            message = f"등록된 {label} 채널 ({len(channel_ids)}개):\n{lines}"
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="공지채널추가", description="이 서버의 회의 알림을 보낼 공지 채널을 추가합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def add_announcement_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        await self._add_purpose_channel(interaction, 채널, PURPOSE_ANNOUNCEMENT, "공지")

    @app_commands.command(name="공지채널제거", description="등록된 공지 채널을 목록에서 제거합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_announcement_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        await self._remove_purpose_channel(interaction, 채널, PURPOSE_ANNOUNCEMENT, "공지")

    @app_commands.command(name="공지채널목록", description="이 서버에 등록된 공지 채널 목록을 확인합니다.")
    @app_commands.guild_only()
    async def list_announcement_channels(self, interaction: discord.Interaction) -> None:
        await self._list_purpose_channels(
            interaction, PURPOSE_ANNOUNCEMENT, "공지", "/공지채널추가"
        )

    @app_commands.command(
        name="뉴스레터채널추가", description="이 서버의 뉴스레터 알림을 보낼 채널을 추가합니다."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def add_newsletter_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        await self._add_purpose_channel(interaction, 채널, PURPOSE_NEWSLETTER, "뉴스레터")

    @app_commands.command(name="뉴스레터채널제거", description="등록된 뉴스레터 채널을 목록에서 제거합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def remove_newsletter_channel(
        self, interaction: discord.Interaction, 채널: discord.TextChannel
    ) -> None:
        await self._remove_purpose_channel(interaction, 채널, PURPOSE_NEWSLETTER, "뉴스레터")

    @app_commands.command(
        name="뉴스레터채널목록", description="이 서버에 등록된 뉴스레터 채널 목록을 확인합니다."
    )
    @app_commands.guild_only()
    async def list_newsletter_channels(self, interaction: discord.Interaction) -> None:
        await self._list_purpose_channels(
            interaction, PURPOSE_NEWSLETTER, "뉴스레터", "/뉴스레터채널추가"
        )

    @app_commands.command(name="회의설정", description="매주 반복되는 회의 요일·시간을 설정합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.choices(
        요일=[app_commands.Choice(name=day + "요일", value=i) for i, day in enumerate(WEEKDAYS)]
    )
    async def set_meeting(
        self,
        interaction: discord.Interaction,
        요일: app_commands.Choice[int],
        시: app_commands.Range[int, 0, 23],
        분: app_commands.Range[int, 0, 59] = 0,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with self._lock(guild_id):
            self.store.set(guild_id, 요일.value, 시, 분, datetime.now(UTC))
        channel_ids = self.store.list_channels(guild_id, PURPOSE_ANNOUNCEMENT)
        channel_note = (
            "\n".join(f"<#{channel_id}>" for channel_id in channel_ids)
            if channel_ids
            else "등록된 공지 채널이 없습니다. /공지채널추가 로 등록하세요."
        )
        note = "\n09:00 이전 또는 정각 회의는 오전 알림을 생략합니다." if (시, 분) <= (9, 0) else ""
        await interaction.followup.send(
            f"매주 {요일.name} {시:02d}:{분:02d} ({self.config.timezone.key})로 저장했습니다.\n"
            f"알림 채널:\n{channel_note}\n"
            f"당일 09:00 및 10분 전 알림. 이미 지난 알림 시각은 소급 발송하지 않습니다.{note}",
            ephemeral=True,
        )

    @app_commands.command(name="회의확인", description="현재 회의 일정과 알림 상태를 확인합니다.")
    @app_commands.guild_only()
    async def show_meeting(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild_id
        schedule = self.store.get(guild_id)
        message = "등록된 회의가 없습니다. 관리자가 /회의설정 으로 등록하세요."
        if schedule:
            meeting = next_meeting(schedule, datetime.now(UTC), self.config.timezone)
            channel_ids = self.store.list_channels(guild_id, PURPOSE_ANNOUNCEMENT)
            channel_note = (
                "\n".join(f"<#{channel_id}>" for channel_id in channel_ids)
                if channel_ids
                else "등록된 공지 채널이 없습니다."
            )
            message = (
                f"매주 {WEEKDAYS[schedule.weekday]}요일 {schedule.hour:02d}:{schedule.minute:02d}\n"
                f"다음 회의: {meeting:%Y-%m-%d %H:%M} ({self.config.timezone.key})\n"
                f"알림 채널:\n{channel_note}"
            )
        if interaction.permissions.administrator:
            pending = self.store.pending_count(guild_id)
            if pending:
                message += f"\n발송 결과 확인이 필요한 기록: {pending}건. 운영 로그를 확인하세요."
        await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(name="회의해제", description="정기 회의 알림을 중지합니다.")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    async def disable_meeting(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with self._lock(guild_id):
            self.store.disable(guild_id)
        await interaction.followup.send("정기 회의 알림을 중지했습니다.", ephemeral=True)

    @app_commands.command(
        name="알림", description="공지 채널에 금일 회의 시간과 참석 안내를 즉시 보냅니다."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.cooldown(1, 30, key=lambda interaction: interaction.guild_id)
    @app_commands.checks.has_permissions(administrator=True)
    async def announce_today(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        async with self._lock(guild_id):
            schedule = self.store.get(guild_id)
            if schedule is None:
                await interaction.followup.send(
                    "등록된 회의가 없습니다. /회의설정으로 요일과 시간을 먼저 등록해 주세요.",
                    ephemeral=True,
                )
                return
            today = datetime.now(UTC).astimezone(self.config.timezone)
            if schedule.weekday != today.weekday():
                await interaction.followup.send(
                    f"오늘은 등록된 회의 요일이 아닙니다. 현재 일정은 매주 "
                    f"{WEEKDAYS[schedule.weekday]}요일 {schedule.hour:02d}:{schedule.minute:02d}입니다.\n"
                    "금일 회의로 변경하려면 /회의설정에서 일정을 수정해 주세요.",
                    ephemeral=True,
                )
                return
            channel_ids = self.store.list_channels(guild_id, PURPOSE_ANNOUNCEMENT)
            if not channel_ids:
                await interaction.followup.send(
                    "등록된 공지 채널이 없습니다. /공지채널추가로 먼저 등록해 주세요.", ephemeral=True
                )
                return
            content = "@everyone\n\n" + format_meeting_notice(
                today.replace(hour=schedule.hour, minute=schedule.minute, second=0, microsecond=0),
                self.config.timezone.key,
            )
            sent, failed = [], []
            for channel_id in channel_ids:
                try:
                    channel = await self._resolve_channel(guild_id, channel_id, PURPOSE_ANNOUNCEMENT)
                    await channel.send(content, allowed_mentions=EVERYONE_MENTIONS)
                except (ValueError, discord.HTTPException) as exc:
                    log.error(
                        "Manual announce failed guild=%s channel=%s: %s", guild_id, channel_id, exc
                    )
                    failed.append(channel_id)
                else:
                    sent.append(channel_id)
        if sent:
            result = f"{schedule.hour:02d}:{schedule.minute:02d} 회의 안내를 보냈습니다:\n" + "\n".join(
                f"<#{channel_id}>" for channel_id in sent
            )
        else:
            result = "전송에 성공한 채널이 없습니다."
        if failed:
            result += "\n전송 실패: " + ", ".join(f"<#{channel_id}>" for channel_id in failed)
        await interaction.followup.send(result, ephemeral=True)

    @app_commands.command(
        name="알림테스트", description="공지 채널에 멘션 없는 테스트 메시지를 보냅니다."
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.cooldown(
        1, 30, key=lambda interaction: (interaction.guild_id, interaction.user.id)
    )
    async def test_reminder(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        channel_ids = self.store.list_channels(guild_id, PURPOSE_ANNOUNCEMENT)
        if not channel_ids:
            await interaction.followup.send(
                "등록된 공지 채널이 없습니다. /공지채널추가로 먼저 등록해 주세요.", ephemeral=True
            )
            return
        sent, failed = [], []
        for channel_id in channel_ids:
            try:
                channel = await self._resolve_channel(guild_id, channel_id, PURPOSE_ANNOUNCEMENT)
                await channel.send(
                    "회의 알림 봇 연결 테스트입니다. ✅", allowed_mentions=discord.AllowedMentions.none()
                )
            except (ValueError, discord.HTTPException) as exc:
                log.error("Test message failed guild=%s channel=%s: %s", guild_id, channel_id, exc)
                failed.append(channel_id)
            else:
                sent.append(channel_id)
        if sent:
            result = "테스트 메시지를 보냈습니다:\n" + "\n".join(f"<#{c}>" for c in sent)
        else:
            result = "전송에 성공한 채널이 없습니다."
        if failed:
            result += "\n전송 실패: " + ", ".join(f"<#{c}>" for c in failed)
        await interaction.followup.send(result, ephemeral=True)

    @tasks.loop(seconds=20)
    async def reminders(self) -> None:
        guild_ids = self.store.scheduled_guild_ids()
        await asyncio.gather(*(self._process_guild_reminders(guild_id) for guild_id in guild_ids))

    async def _process_guild_reminders(self, guild_id: int) -> None:
        try:
            async with self._lock(guild_id):
                schedule = self.store.get(guild_id)
                if schedule is None:
                    return
                now = datetime.now(UTC)
                for reminder in due_reminders(schedule, now, self.config.timezone):
                    for channel_id in self.store.list_channels(guild_id, PURPOSE_ANNOUNCEMENT):
                        await self._send_reminder(guild_id, channel_id, schedule, reminder)
        except Exception:
            log.exception("Reminder tick failed guild=%s; next tick will continue", guild_id)

    async def _send_reminder(self, guild_id, channel_id, schedule, reminder) -> None:
        try:
            channel = await self._resolve_channel(guild_id, channel_id, PURPOSE_ANNOUNCEMENT)
        except ValueError as exc:
            log.error("Reminder channel unavailable guild=%s channel=%s: %s", guild_id, channel_id, exc)
            return
        # Channel lookup can await HTTP; recheck deadline before claiming/sending.
        now = datetime.now(UTC)
        if reminder not in due_reminders(schedule, now, self.config.timezone):
            return
        if not self.store.claim(guild_id, channel_id, reminder.key):
            return
        is_start_reminder = reminder.kind == "ten_minutes"
        remaining = max(1, math.ceil((reminder.meeting_at - now).total_seconds() / 60))
        content = "@everyone\n\n" + format_meeting_notice(
            reminder.meeting_at,
            self.config.timezone.key,
            remaining_minutes=remaining if is_start_reminder else None,
        )
        try:
            message = await channel.send(content, allowed_mentions=EVERYONE_MENTIONS)
        except discord.HTTPException as exc:
            if 400 <= exc.status < 500:
                self.store.release(guild_id, channel_id, reminder.key)
            log.error(
                "Reminder send failed guild=%s channel=%s key=%s HTTP=%s",
                guild_id,
                channel_id,
                reminder.key,
                exc.status,
            )
        except (TimeoutError, OSError):
            log.error(
                "Reminder send result unknown guild=%s channel=%s key=%s; automatic retry suppressed",
                guild_id,
                channel_id,
                reminder.key,
            )
        else:
            self.store.sent(guild_id, channel_id, reminder.key, message.id)
            log.info(
                "Reminder sent guild=%s channel=%s key=%s message_id=%s",
                guild_id,
                channel_id,
                reminder.key,
                message.id,
            )

    @reminders.before_loop
    async def before_reminders(self) -> None:
        await self.bot.wait_until_ready()
