import asyncio
import sqlite3
from unittest.mock import AsyncMock, PropertyMock, patch

import discord
import pytest

from meeting_bot.__main__ import MeetingBot
from meeting_bot.config import Config

COMMAND_NAMES = {
    "공지채널추가",
    "공지채널제거",
    "공지채널목록",
    "뉴스레터채널추가",
    "뉴스레터채널제거",
    "뉴스레터채널목록",
    "회의설정",
    "회의확인",
    "회의해제",
    "알림",
    "알림테스트",
}


@pytest.fixture
def configured_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "test-only-token")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "meetings.db"))
    monkeypatch.setenv("BOT_TIMEZONE", "Asia/Seoul")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")


def test_token_is_not_in_config_repr(configured_env):
    config = Config.from_env()
    assert "test-only-token" not in repr(config)


@pytest.mark.parametrize(
    "name,value",
    [
        ("DISCORD_BOT_TOKEN", ""),
        ("DISCORD_BOT_TOKEN", "replace_with_your_bot_token"),
        ("BOT_TIMEZONE", "Not/AZone"),
    ],
)
def test_invalid_config(configured_env, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError):
        Config.from_env()


def test_commands_register_globally_and_scheduler_stops_without_network(configured_env):
    async def run():
        config = Config.from_env()
        async with MeetingBot(config) as bot:
            await bot.setup_hook()
            assert {command.name for command in bot.tree.get_commands()} == COMMAND_NAMES
            assert bot.tree.get_command("알림").parameters == []
            cog = bot.meeting_cog
            task = cog.reminders.get_task()
            assert task is not None
        assert task.done()

    asyncio.run(run())


def test_on_ready_syncs_globally_once_and_per_already_joined_guild(configured_env):
    async def run():
        config = Config.from_env()
        async with MeetingBot(config) as bot:
            await bot.setup_hook()
            guild_a, guild_b = discord.Object(id=100), discord.Object(id=200)
            with (
                patch.object(bot.tree, "sync", new_callable=AsyncMock) as sync,
                patch.object(bot.tree, "copy_global_to") as copy_global,
                patch.object(type(bot), "guilds", new_callable=PropertyMock, return_value=[guild_a, guild_b]),
            ):
                await bot.on_ready()
                # One global sync plus one per already-joined guild.
                assert sync.await_count == 3
                assert copy_global.call_count == 2
                await bot.on_ready()
                # A reconnect must not repeat the (rate-limited) global sync.
                assert sync.await_count == 3

    asyncio.run(run())


def test_new_guild_join_gets_commands_immediately(configured_env):
    async def run():
        config = Config.from_env()
        async with MeetingBot(config) as bot:
            await bot.setup_hook()
            guild = discord.Object(id=300)
            with (
                patch.object(bot.tree, "sync", new_callable=AsyncMock) as sync,
                patch.object(bot.tree, "copy_global_to") as copy_global,
            ):
                await bot.on_guild_join(guild)
                sync.assert_awaited_once_with(guild=guild)
                copy_global.assert_called_once_with(guild=guild)

    asyncio.run(run())


def test_guild_sync_failure_is_isolated(configured_env):
    async def run():
        config = Config.from_env()
        async with MeetingBot(config) as bot:
            await bot.setup_hook()
            failing, working = discord.Object(id=100), discord.Object(id=200)

            async def sync_guild(*, guild):
                if guild.id == failing.id:
                    raise discord.HTTPException(
                        type("Response", (), {"status": 403, "reason": "Forbidden"})(), "no access"
                    )
                return []

            with (
                patch.object(bot.tree, "sync", new_callable=AsyncMock, side_effect=sync_guild) as sync,
                patch.object(bot.tree, "copy_global_to"),
            ):
                await bot._sync_guild(failing)
                await bot._sync_guild(working)
                assert sync.await_count == 2

    asyncio.run(run())


def test_close_unloads_cog_and_closes_store(configured_env):
    async def run():
        config = Config.from_env()
        bot = MeetingBot(config)
        async with bot:
            await bot.setup_hook()
            cog = bot.meeting_cog
            task = cog.reminders.get_task()
        assert task.done()
        assert bot.meeting_cog is None
        with pytest.raises(sqlite3.ProgrammingError):
            bot.store.db.execute("SELECT 1")

    asyncio.run(run())
