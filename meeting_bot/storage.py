import sqlite3
from datetime import datetime
from pathlib import Path

from .schedule import Schedule, validate_time

PURPOSE_ANNOUNCEMENT = "announcement"
PURPOSE_NEWSLETTER = "newsletter"


class Store:
    """All guilds share one SQLite file; short operations run on the bot event loop."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        delivery_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(deliveries)")}
        if delivery_columns and "channel_id" not in delivery_columns:
            # Pre-multi-channel schema: pending/sent markers are safe to discard on upgrade.
            self.db.execute("DROP TABLE deliveries")
        channel_columns = {row["name"] for row in self.db.execute("PRAGMA table_info(channels)")}
        migrate_channels = bool(channel_columns) and "purpose" not in channel_columns
        if migrate_channels:
            self.db.execute("ALTER TABLE channels RENAME TO channels_old")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS schedules (
                guild_id INTEGER PRIMARY KEY,
                weekday INTEGER NOT NULL,
                hour INTEGER NOT NULL,
                minute INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL,
                enabled INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS channels (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                purpose TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, channel_id, purpose)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id TEXT,
                PRIMARY KEY (guild_id, channel_id, event_key)
            );
        """)
        if migrate_channels:
            # Channels registered before purposes existed were all meeting-announcement channels.
            self.db.execute(
                """
                INSERT OR IGNORE INTO channels (guild_id, channel_id, purpose, added_at)
                SELECT guild_id, channel_id, ?, added_at FROM channels_old
                """,
                (PURPOSE_ANNOUNCEMENT,),
            )
            self.db.execute("DROP TABLE channels_old")
            self.db.commit()

    def get(self, guild_id: int) -> Schedule | None:
        row = self.db.execute(
            "SELECT * FROM schedules WHERE guild_id=? AND enabled=1", (guild_id,)
        ).fetchone()
        if row is None:
            return None
        return Schedule(
            row["weekday"],
            row["hour"],
            row["minute"],
            row["revision"],
            datetime.fromisoformat(row["updated_at"]),
        )

    def set(self, guild_id: int, weekday: int, hour: int, minute: int, now: datetime) -> None:
        validate_time(weekday, hour, minute)
        current = self.get(guild_id)
        if current and (current.weekday, current.hour, current.minute) == (weekday, hour, minute):
            return
        with self.db:
            self.db.execute(
                """
                INSERT INTO schedules VALUES (?, ?, ?, ?, 1, ?, 1)
                ON CONFLICT(guild_id) DO UPDATE SET
                    weekday=excluded.weekday, hour=excluded.hour, minute=excluded.minute,
                    revision=schedules.revision+1, updated_at=excluded.updated_at, enabled=1
            """,
                (guild_id, weekday, hour, minute, now.isoformat()),
            )

    def disable(self, guild_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE schedules SET enabled=0 WHERE guild_id=?", (guild_id,))

    def scheduled_guild_ids(self) -> list[int]:
        return [
            row["guild_id"]
            for row in self.db.execute("SELECT guild_id FROM schedules WHERE enabled=1")
        ]

    def add_channel(self, guild_id: int, channel_id: int, purpose: str, now: datetime) -> bool:
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO channels VALUES (?, ?, ?, ?)",
                (guild_id, channel_id, purpose, now.isoformat()),
            )
            return cursor.rowcount == 1

    def remove_channel(self, guild_id: int, channel_id: int, purpose: str) -> bool:
        with self.db:
            cursor = self.db.execute(
                "DELETE FROM channels WHERE guild_id=? AND channel_id=? AND purpose=?",
                (guild_id, channel_id, purpose),
            )
            return cursor.rowcount == 1

    def list_channels(self, guild_id: int, purpose: str) -> list[int]:
        return [
            row["channel_id"]
            for row in self.db.execute(
                "SELECT channel_id FROM channels WHERE guild_id=? AND purpose=? ORDER BY added_at",
                (guild_id, purpose),
            )
        ]

    def claim(self, guild_id: int, channel_id: int, key: str) -> bool:
        # Commit before sending: uncertain sends are not automatically repeated after a crash.
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?, 'pending', NULL)",
                (guild_id, channel_id, key),
            )
            return cursor.rowcount == 1

    def sent(self, guild_id: int, channel_id: int, key: str, message_id: int) -> None:
        with self.db:
            self.db.execute(
                """
                UPDATE deliveries SET status='sent', message_id=?
                WHERE guild_id=? AND channel_id=? AND event_key=?
                """,
                (str(message_id), guild_id, channel_id, key),
            )

    def release(self, guild_id: int, channel_id: int, key: str) -> None:
        with self.db:
            self.db.execute(
                """
                DELETE FROM deliveries WHERE guild_id=? AND channel_id=?
                AND event_key=? AND status='pending'
                """,
                (guild_id, channel_id, key),
            )

    def pending_count(self, guild_id: int) -> int:
        return self.db.execute(
            "SELECT count(*) FROM deliveries WHERE guild_id=? AND status='pending'",
            (guild_id,),
        ).fetchone()[0]

    def close(self) -> None:
        self.db.close()
