import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

from meeting_bot.__main__ import MeetingBot
from meeting_bot.cog import MeetingCog
from meeting_bot.config import Config
from meeting_bot.storage import PURPOSE_ANNOUNCEMENT, PURPOSE_NEWSLETTER

KST = ZoneInfo("Asia/Seoul")
EARLIER = datetime(2026, 9, 1, tzinfo=KST)
MORNING = datetime(2026, 9, 9, 9, tzinfo=KST)

CLUB_GUILD, CLUB_CHANNEL = 100, 101
TEST_GUILD, TEST_CHANNEL = 200, 201


def channel(guild_id):
    result = Mock(spec=discord.TextChannel)
    result.guild = SimpleNamespace(id=guild_id, me=object())
    result.permissions_for.return_value = discord.Permissions(
        view_channel=True, send_messages=True, mention_everyone=True
    )
    result.send = AsyncMock(return_value=SimpleNamespace(id=guild_id * 100))
    return result


def interaction(guild_id):
    return SimpleNamespace(
        guild_id=guild_id,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        permissions=discord.Permissions(administrator=True),
    )


def config(tmp_path):
    return Config("test-only-token", KST, tmp_path / "meetings.db")


def test_commands_use_only_their_own_guild(tmp_path):
    """Same bot, one process: a club-server command must never touch the test server's data."""

    async def run():
        async with MeetingBot(config(tmp_path)) as bot:
            await bot.setup_hook()
            cog = bot.meeting_cog
            cog.store.add_channel(CLUB_GUILD, CLUB_CHANNEL, PURPOSE_ANNOUNCEMENT, EARLIER)
            cog.store.add_channel(TEST_GUILD, TEST_CHANNEL, PURPOSE_ANNOUNCEMENT, EARLIER)
            channels = {CLUB_CHANNEL: channel(CLUB_GUILD), TEST_CHANNEL: channel(TEST_GUILD)}
            with patch.object(bot, "get_channel", side_effect=channels.get):
                with patch("meeting_bot.cog.datetime") as clock:
                    clock.now.return_value = EARLIER
                    for guild_id, hour in ((CLUB_GUILD, 20), (TEST_GUILD, 21)):
                        await MeetingCog.set_meeting.callback(
                            cog,
                            interaction(guild_id),
                            app_commands.Choice(name="수요일", value=2),
                            hour,
                            30,
                        )
                    assert cog.store.get(CLUB_GUILD).hour == 20
                    assert cog.store.get(TEST_GUILD).hour == 21

                    clock.now.return_value = MORNING
                    await MeetingCog.announce_today.callback(cog, interaction(TEST_GUILD))
                    channels[CLUB_CHANNEL].send.assert_not_awaited()
                    channels[TEST_CHANNEL].send.assert_awaited_once()
                    assert "21:30" in channels[TEST_CHANNEL].send.call_args.args[0]
                    assert (
                        channels[TEST_CHANNEL].send.call_args.kwargs["allowed_mentions"].everyone
                        is True
                    )

                    await MeetingCog.announce_today.callback(cog, interaction(CLUB_GUILD))
                    assert "20:30" in channels[CLUB_CHANNEL].send.call_args.args[0]

                await MeetingCog.disable_meeting.callback(cog, interaction(TEST_GUILD))
                assert cog.store.get(TEST_GUILD) is None
                assert cog.store.get(CLUB_GUILD).hour == 20

                await MeetingCog.test_reminder.callback(cog, interaction(TEST_GUILD))
                assert "@everyone" not in channels[TEST_CHANNEL].send.call_args.args[0]
                assert (
                    channels[TEST_CHANNEL].send.call_args.kwargs["allowed_mentions"].to_dict()[
                        "parse"
                    ]
                    == []
                )
                channels[CLUB_CHANNEL].send.assert_awaited_once()

    asyncio.run(run())


@pytest.mark.parametrize("failure", [None, "permissions", "send"])
def test_schedulers_and_deliveries_are_isolated_per_guild(tmp_path, failure):
    async def run():
        async with MeetingBot(config(tmp_path)) as bot:
            await bot.setup_hook()
            cog = bot.meeting_cog
            for guild_id in (CLUB_GUILD, TEST_GUILD):
                cog.store.set(guild_id, 2, 20, 0, EARLIER)
            cog.store.add_channel(CLUB_GUILD, CLUB_CHANNEL, PURPOSE_ANNOUNCEMENT, EARLIER)
            cog.store.add_channel(TEST_GUILD, TEST_CHANNEL, PURPOSE_ANNOUNCEMENT, EARLIER)
            channels = {CLUB_CHANNEL: channel(CLUB_GUILD), TEST_CHANNEL: channel(TEST_GUILD)}
            if failure == "permissions":
                channels[CLUB_CHANNEL].permissions_for.return_value = discord.Permissions.none()
            elif failure == "send":
                channels[CLUB_CHANNEL].send.side_effect = discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden"), "no access"
                )
            with (
                patch.object(bot, "get_channel", side_effect=channels.get),
                patch("meeting_bot.cog.datetime") as clock,
            ):
                clock.now.return_value = MORNING
                for _ in range(2):
                    await cog.reminders()
            channels[TEST_CHANNEL].send.assert_awaited_once()
            if failure is None:
                channels[CLUB_CHANNEL].send.assert_awaited_once()
            for guild_id, channel_id in ((CLUB_GUILD, CLUB_CHANNEL), (TEST_GUILD, TEST_CHANNEL)):
                count = cog.store.db.execute(
                    "SELECT count(*) FROM deliveries WHERE guild_id=? AND channel_id=? AND status='sent'",
                    (guild_id, channel_id),
                ).fetchone()[0]
                assert count == (0 if guild_id == CLUB_GUILD and failure else 1)

    asyncio.run(run())


def test_multiple_channels_in_the_same_guild_fail_independently(tmp_path):
    async def run():
        async with MeetingBot(config(tmp_path)) as bot:
            await bot.setup_hook()
            cog = bot.meeting_cog
            broken_channel, working_channel = 301, 302
            cog.store.set(CLUB_GUILD, 2, 20, 0, EARLIER)
            cog.store.add_channel(CLUB_GUILD, broken_channel, PURPOSE_ANNOUNCEMENT, EARLIER)
            cog.store.add_channel(CLUB_GUILD, working_channel, PURPOSE_ANNOUNCEMENT, EARLIER)
            broken = channel(CLUB_GUILD)
            broken.send.side_effect = discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"), "no access"
            )
            working = channel(CLUB_GUILD)
            channels = {broken_channel: broken, working_channel: working}
            with (
                patch.object(bot, "get_channel", side_effect=channels.get),
                patch("meeting_bot.cog.datetime") as clock,
            ):
                clock.now.return_value = MORNING
                await cog.reminders()
            broken.send.assert_awaited_once()
            working.send.assert_awaited_once()
            assert (
                cog.store.db.execute(
                    "SELECT status FROM deliveries WHERE guild_id=? AND channel_id=?",
                    (CLUB_GUILD, working_channel),
                ).fetchone()["status"]
                == "sent"
            )
            # A 403 releases the claim on the broken channel so it can retry later.
            assert (
                cog.store.db.execute(
                    "SELECT count(*) FROM deliveries WHERE guild_id=? AND channel_id=?",
                    (CLUB_GUILD, broken_channel),
                ).fetchone()[0]
                == 0
            )

    asyncio.run(run())


def test_announcement_and_newsletter_channels_in_the_same_guild_stay_separate(tmp_path):
    """One server, two channels: 공지 gets meeting content only, 뉴스레터 gets none (yet)."""

    async def run():
        async with MeetingBot(config(tmp_path)) as bot:
            await bot.setup_hook()
            cog = bot.meeting_cog
            announcement_channel, newsletter_channel = 401, 402
            cog.store.set(CLUB_GUILD, 2, 20, 0, EARLIER)
            cog.store.add_channel(CLUB_GUILD, announcement_channel, PURPOSE_ANNOUNCEMENT, EARLIER)
            cog.store.add_channel(CLUB_GUILD, newsletter_channel, PURPOSE_NEWSLETTER, EARLIER)
            channels = {
                announcement_channel: channel(CLUB_GUILD),
                newsletter_channel: channel(CLUB_GUILD),
            }
            with (
                patch.object(bot, "get_channel", side_effect=channels.get),
                patch("meeting_bot.cog.datetime") as clock,
            ):
                clock.now.return_value = MORNING
                await cog.reminders()
                await MeetingCog.announce_today.callback(cog, interaction(CLUB_GUILD))
                await MeetingCog.test_reminder.callback(cog, interaction(CLUB_GUILD))
            # Morning reminder + /알림 + /알림테스트, all to the announcement channel only.
            assert channels[announcement_channel].send.await_count == 3
            channels[newsletter_channel].send.assert_not_awaited()
            assert cog.store.list_channels(CLUB_GUILD, PURPOSE_ANNOUNCEMENT) == [
                announcement_channel
            ]
            assert cog.store.list_channels(CLUB_GUILD, PURPOSE_NEWSLETTER) == [newsletter_channel]

    asyncio.run(run())
