import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import discord
import pytest
from discord import app_commands

from meeting_bot.cog import MeetingCog
from meeting_bot.schedule import Schedule, due_reminders, next_meeting
from meeting_bot.storage import PURPOSE_ANNOUNCEMENT, PURPOSE_NEWSLETTER, Store

KST = ZoneInfo("Asia/Seoul")
GUILD = 42


def at(day, hour, minute=0, second=0):
    return datetime(2026, 9, day, hour, minute, second, tzinfo=KST)


def schedule(hour=20, minute=0, weekday=2):
    return Schedule(weekday, hour, minute, 1, at(1, 0))


@pytest.mark.parametrize(
    "now, kinds",
    [
        (at(9, 8, 59), []),
        (at(9, 9), ["morning"]),
        (at(9, 9, 1), ["morning"]),
        (at(9, 9, 2, 1), []),
        (at(9, 19, 50), ["ten_minutes"]),
        (at(9, 20), []),
        (at(10, 9), []),
    ],
)
def test_reminder_windows(now, kinds):
    assert [r.kind for r in due_reminders(schedule(), now, KST)] == kinds


def test_midnight_and_week_boundary():
    result = due_reminders(schedule(0, 5, weekday=0), at(13, 23, 55), KST)
    assert len(result) == 1
    assert result[0].meeting_at == at(14, 0, 5)


def test_no_morning_after_meeting():
    assert due_reminders(schedule(8), at(9, 9), KST) == []


def test_new_schedule_does_not_backfill_old_reminder():
    new = Schedule(2, 20, 0, 2, at(9, 19, 51))
    assert due_reminders(new, at(9, 19, 51), KST) == []


def test_next_meeting_rolls_to_next_week():
    assert next_meeting(schedule(), at(9, 20), KST) == at(16, 20)


def test_schedule_change_and_persistence(tmp_path):
    path = tmp_path / "meetings.db"
    store = Store(path)
    store.set(GUILD, 2, 20, 0, at(1, 0))
    store.set(GUILD, 2, 20, 0, at(2, 0))
    assert store.get(GUILD).revision == 1
    store.set(GUILD, 2, 21, 0, at(3, 0))
    assert not due_reminders(store.get(GUILD), at(9, 19, 50), KST)
    assert store.claim(GUILD, 99, "example")
    store.sent(GUILD, 99, "example", 123)
    store.close()
    store = Store(path)
    assert store.get(GUILD).hour == 21
    assert not store.claim(GUILD, 99, "example")
    assert store.pending_count(GUILD) == 0
    store.disable(GUILD)
    assert store.get(GUILD) is None
    store.set(GUILD, 2, 20, 0, at(4, 0))
    assert store.get(GUILD).revision == 3
    store.close()


def test_pending_send_is_not_repeated_after_restart(tmp_path):
    path = tmp_path / "meetings.db"
    store = Store(path)
    assert store.claim(GUILD, 99, "uncertain")
    store.close()
    store = Store(path)
    assert not store.claim(GUILD, 99, "uncertain")
    assert store.pending_count(GUILD) == 1
    store.release(GUILD, 99, "uncertain")
    assert store.claim(GUILD, 99, "uncertain")
    store.close()


def test_schedule_data_is_isolated_per_guild(tmp_path):
    path = tmp_path / "meetings.db"
    store = Store(path)
    store.set(1, 2, 20, 0, at(1, 0))
    assert store.get(2) is None
    assert store.claim(1, 10, "same-key") and store.claim(2, 10, "same-key")
    store.close()


def test_delivery_data_is_isolated_per_channel(tmp_path):
    path = tmp_path / "meetings.db"
    store = Store(path)
    assert store.claim(GUILD, 10, "same-key") and store.claim(GUILD, 20, "same-key")
    store.sent(GUILD, 10, "same-key", 111)
    assert store.pending_count(GUILD) == 1
    store.close()


@pytest.mark.parametrize("weekday,hour,minute", [(7, 20, 0), (0, 24, 0), (0, 20, 60)])
def test_invalid_schedule_rejected(tmp_path, weekday, hour, minute):
    store = Store(tmp_path / "meetings.db")
    with pytest.raises(ValueError):
        store.set(GUILD, weekday, hour, minute, at(1, 0))
    assert store.get(GUILD) is None
    store.close()


def test_channel_registration_add_remove_list(tmp_path):
    store = Store(tmp_path / "meetings.db")
    assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == []
    assert store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
    assert not store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
    assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == [99]
    assert store.list_channels(43, PURPOSE_ANNOUNCEMENT) == []
    assert store.remove_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT)
    assert not store.remove_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT)
    assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == []
    store.close()


def test_announcement_and_newsletter_channels_are_kept_separate(tmp_path):
    store = Store(tmp_path / "meetings.db")
    store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
    store.add_channel(GUILD, 100, PURPOSE_NEWSLETTER, at(1, 0))
    # The same channel can even be registered for both purposes independently.
    store.add_channel(GUILD, 99, PURPOSE_NEWSLETTER, at(1, 0))
    assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == [99]
    assert sorted(store.list_channels(GUILD, PURPOSE_NEWSLETTER)) == [99, 100]
    assert store.remove_channel(GUILD, 99, PURPOSE_NEWSLETTER)
    assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == [99]
    assert store.list_channels(GUILD, PURPOSE_NEWSLETTER) == [100]
    store.close()


def test_send_deduplication_and_mentions(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 0, at(1, 0))
        store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        cog._resolve_channel = AsyncMock(return_value=channel)
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 19, 50)
            await cog.reminders()
            await cog.reminders()
        channel.send.assert_awaited_once()
        assert "@everyone" in channel.send.call_args.args[0]
        assert "회의 시작 안내" in channel.send.call_args.args[0]
        assert "약 **10분**" in channel.send.call_args.args[0]
        assert "20:00 (Asia/Seoul)" in channel.send.call_args.args[0]
        assert channel.send.call_args.kwargs["allowed_mentions"].everyone is True
        assert store.pending_count(GUILD) == 0
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize("uncertain", [False, True])
def test_known_failure_retries_but_uncertain_send_does_not(tmp_path, uncertain):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 0, at(1, 0))
        store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        response = SimpleNamespace(status=403, reason="Forbidden")
        error = TimeoutError() if uncertain else discord.Forbidden(response, "missing access")
        channel = SimpleNamespace(send=AsyncMock(side_effect=error))
        cog._resolve_channel = AsyncMock(return_value=channel)
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 19, 50)
            await cog.reminders()
            await cog.reminders()
        assert channel.send.await_count == (1 if uncertain else 2)
        assert store.pending_count(GUILD) == (1 if uncertain else 0)
        store.close()

    asyncio.run(run())


def test_morning_send_mentions_everyone(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 0, at(1, 0))
        store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=123)))
        cog._resolve_channel = AsyncMock(return_value=channel)
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await cog.reminders()
        assert "@everyone" in channel.send.call_args.args[0]
        assert "금일 회의 안내" in channel.send.call_args.args[0]
        assert "2026년 09월 09일 (수)" in channel.send.call_args.args[0]
        assert "20:00 (Asia/Seoul)" in channel.send.call_args.args[0]
        assert channel.send.call_args.kwargs["allowed_mentions"].everyone is True
        store.close()

    asyncio.run(run())


def test_reminders_never_reach_a_newsletter_only_channel(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 0, at(1, 0))
        store.add_channel(GUILD, 200, PURPOSE_NEWSLETTER, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        cog._resolve_channel = AsyncMock()
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await cog.reminders()
        cog._resolve_channel.assert_not_awaited()
        store.close()

    asyncio.run(run())


def test_one_channel_failure_does_not_block_another_in_the_same_guild(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 0, at(1, 0))
        store.add_channel(GUILD, 10, PURPOSE_ANNOUNCEMENT, at(1, 0))
        store.add_channel(GUILD, 20, PURPOSE_ANNOUNCEMENT, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        broken = SimpleNamespace(
            send=AsyncMock(
                side_effect=discord.Forbidden(
                    SimpleNamespace(status=403, reason="Forbidden"), "no access"
                )
            )
        )
        working = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=1)))
        destinations = {10: broken, 20: working}
        cog._resolve_channel = AsyncMock(
            side_effect=lambda guild_id, channel_id, purpose: destinations[channel_id]
        )
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await cog.reminders()
        broken.send.assert_awaited_once()
        working.send.assert_awaited_once()
        assert store.pending_count(GUILD) == 0  # 403 released the broken channel's claim
        store.close()

    asyncio.run(run())


def test_permissions_enforced_at_runtime():
    async def run():
        interaction = SimpleNamespace(permissions=discord.Permissions.none())
        for command in (
            MeetingCog.set_meeting,
            MeetingCog.disable_meeting,
            MeetingCog.announce_today,
            MeetingCog.add_announcement_channel,
            MeetingCog.remove_announcement_channel,
            MeetingCog.add_newsletter_channel,
            MeetingCog.remove_newsletter_channel,
        ):
            with pytest.raises(app_commands.MissingPermissions):
                await command.checks[0](interaction)

    asyncio.run(run())


def test_announcement_channel_commands_add_list_remove(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        cog._resolve_channel = AsyncMock()
        channel = SimpleNamespace(id=99, guild=SimpleNamespace(id=GUILD))
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await MeetingCog.add_announcement_channel.callback(cog, interaction, channel)
        assert "공지 채널로 등록했습니다" in interaction.followup.send.call_args.args[0]
        assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == [99]

        await MeetingCog.add_announcement_channel.callback(cog, interaction, channel)
        assert "이미 등록된 공지 채널" in interaction.followup.send.call_args.args[0]

        await MeetingCog.list_announcement_channels.callback(cog, interaction)
        assert "<#99>" in interaction.response.send_message.call_args.args[0]

        await MeetingCog.remove_announcement_channel.callback(cog, interaction, channel)
        assert "공지 채널에서 제거했습니다" in interaction.followup.send.call_args.args[0]
        assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == []

        await MeetingCog.remove_announcement_channel.callback(cog, interaction, channel)
        assert "등록되지 않은 공지 채널" in interaction.followup.send.call_args.args[0]
        store.close()

    asyncio.run(run())


def test_newsletter_channel_commands_add_list_remove(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        cog._resolve_channel = AsyncMock()
        channel = SimpleNamespace(id=200, guild=SimpleNamespace(id=GUILD))
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await MeetingCog.add_newsletter_channel.callback(cog, interaction, channel)
        assert "뉴스레터 채널로 등록했습니다" in interaction.followup.send.call_args.args[0]
        assert store.list_channels(GUILD, PURPOSE_NEWSLETTER) == [200]
        # Registering as newsletter must not make it an announcement channel too.
        assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == []

        await MeetingCog.list_newsletter_channels.callback(cog, interaction)
        assert "<#200>" in interaction.response.send_message.call_args.args[0]

        await MeetingCog.remove_newsletter_channel.callback(cog, interaction, channel)
        assert "뉴스레터 채널에서 제거했습니다" in interaction.followup.send.call_args.args[0]
        assert store.list_channels(GUILD, PURPOSE_NEWSLETTER) == []
        store.close()

    asyncio.run(run())


def test_add_channel_warns_when_bot_lacks_permissions(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        cog._resolve_channel = AsyncMock(side_effect=ValueError("권한 부족"))
        channel = SimpleNamespace(id=99, guild=SimpleNamespace(id=GUILD))
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await MeetingCog.add_announcement_channel.callback(cog, interaction, channel)
        message = interaction.followup.send.call_args.args[0]
        assert "등록했습니다" in message
        assert "권한 부족" in message
        assert store.list_channels(GUILD, PURPOSE_ANNOUNCEMENT) == [99]
        store.close()

    asyncio.run(run())


def test_manual_announcement_sends_to_every_channel_without_changing_schedule(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 30, at(1, 0))
        store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
        store.add_channel(GUILD, 100, PURPOSE_ANNOUNCEMENT, at(1, 0))
        previous = store.get(GUILD)
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        destinations = {99: SimpleNamespace(send=AsyncMock()), 100: SimpleNamespace(send=AsyncMock())}
        cog._resolve_channel = AsyncMock(
            side_effect=lambda guild_id, channel_id, purpose: destinations[channel_id]
        )
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("meeting_bot.cog.datetime") as clock:
            # UTC is still September 8; the announcement date must follow Korea time.
            clock.now.return_value = datetime(2026, 9, 8, 16, tzinfo=UTC)
            await MeetingCog.announce_today.callback(cog, interaction)
        for destination in destinations.values():
            destination.send.assert_awaited_once()
            content = destination.send.call_args.args[0]
            assert "금일 회의 안내" in content
            assert "2026년 09월 09일 (수)" in content
            assert "20:30 (Asia/Seoul)" in content
            assert content.startswith("@everyone")
            assert destination.send.call_args.kwargs["allowed_mentions"].to_dict()["parse"] == [
                "everyone"
            ]
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        result = interaction.followup.send.call_args.args[0]
        assert "<#99>" in result and "<#100>" in result
        assert store.get(GUILD) == previous
        assert store.pending_count(GUILD) == 0
        store.close()

    asyncio.run(run())


def test_manual_announcement_isolates_per_channel_failure(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 30, at(1, 0))
        store.add_channel(GUILD, 99, PURPOSE_ANNOUNCEMENT, at(1, 0))
        store.add_channel(GUILD, 100, PURPOSE_ANNOUNCEMENT, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        error = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no access")
        broken = SimpleNamespace(send=AsyncMock(side_effect=error))
        working = SimpleNamespace(send=AsyncMock())
        destinations = {99: broken, 100: working}
        cog._resolve_channel = AsyncMock(
            side_effect=lambda guild_id, channel_id, purpose: destinations[channel_id]
        )
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await MeetingCog.announce_today.callback(cog, interaction)
        working.send.assert_awaited_once()
        broken.send.assert_awaited_once()
        result = interaction.followup.send.call_args.args[0]
        assert "<#100>" in result
        assert "전송 실패" in result and "<#99>" in result
        store.close()

    asyncio.run(run())


def test_manual_announcement_requires_a_registered_channel(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 30, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await MeetingCog.announce_today.callback(cog, interaction)
        assert "등록된 공지 채널이 없습니다" in interaction.followup.send.call_args.args[0]
        store.close()

    asyncio.run(run())


def test_manual_announcement_ignores_newsletter_only_channels(tmp_path):
    async def run():
        store = Store(tmp_path / "meetings.db")
        store.set(GUILD, 2, 20, 30, at(1, 0))
        store.add_channel(GUILD, 200, PURPOSE_NEWSLETTER, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        cog._resolve_channel = AsyncMock()
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await MeetingCog.announce_today.callback(cog, interaction)
        cog._resolve_channel.assert_not_awaited()
        assert "등록된 공지 채널이 없습니다" in interaction.followup.send.call_args.args[0]
        store.close()

    asyncio.run(run())


@pytest.mark.parametrize("weekday", [None, 4])
def test_manual_announcement_requires_a_meeting_today(tmp_path, weekday):
    async def run():
        store = Store(tmp_path / "meetings.db")
        if weekday is not None:
            store.set(GUILD, weekday, 20, 30, at(1, 0))
        cog = MeetingCog(SimpleNamespace(), SimpleNamespace(timezone=KST), store)
        cog._resolve_channel = AsyncMock()
        interaction = SimpleNamespace(
            guild_id=GUILD,
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        with patch("meeting_bot.cog.datetime") as clock:
            clock.now.return_value = at(9, 9)
            await MeetingCog.announce_today.callback(cog, interaction)
        cog._resolve_channel.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
        expected = (
            "등록된 회의가 없습니다" if weekday is None else "오늘은 등록된 회의 요일이 아닙니다"
        )
        assert expected in interaction.followup.send.call_args.args[0]
        store.close()

    asyncio.run(run())
