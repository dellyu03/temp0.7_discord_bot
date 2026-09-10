import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    timezone: ZoneInfo
    database_path: Path
    openai_api_key: str | None = field(default=None, repr=False)
    openai_model: str = "gpt-5.6-luna"
    news_source_list_path: Path = Path(__file__).resolve().parent.parent / "docs/source-list.md"

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
        if not token or token == "replace_with_your_bot_token":
            raise ValueError(".env에 DISCORD_BOT_TOKEN을 설정하세요.")

        try:
            timezone = ZoneInfo(os.environ.get("BOT_TIMEZONE", "Asia/Seoul"))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("BOT_TIMEZONE이 올바른 시간대가 아닙니다.") from exc
        return cls(
            token=token,
            timezone=timezone,
            database_path=Path(os.environ.get("DATABASE_PATH", "data/meetings.sqlite3")),
            openai_api_key=os.environ.get("OPENAI_API_KEY", "").strip() or None,
            openai_model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna").strip()
            or "gpt-5.6-luna",
            news_source_list_path=Path(
                os.environ.get(
                    "NEWS_SOURCE_LIST_PATH",
                    Path(__file__).resolve().parent.parent / "docs/source-list.md",
                )
            ),
        )
