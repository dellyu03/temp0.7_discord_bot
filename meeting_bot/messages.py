from datetime import datetime

from .schedule import WEEKDAYS


def format_meeting_notice(
    meeting_at: datetime, timezone_name: str, *, remaining_minutes: int | None = None
) -> str:
    if remaining_minutes is None:
        title = "📢 **금일 회의 안내**"
        introduction = "금일 회의 일정을 아래와 같이 안내드립니다."
    else:
        title = "⏰ **회의 시작 안내**"
        introduction = f"회의 시작까지 약 **{remaining_minutes}분** 남아 안내드립니다."
    return (
        f"{title}\n\n"
        "안녕하십니까, Temp0.7 부원 여러분.\n"
        f"{introduction}\n\n"
        f"• **일자:** {meeting_at:%Y년 %m월 %d일} ({WEEKDAYS[meeting_at.weekday()]})\n"
        f"• **시간:** {meeting_at:%H:%M} ({timezone_name})\n\n"
        "원활한 회의 진행을 위해 시작 시간에 맞추어 참석해 주시면 감사하겠습니다.\n"
        "감사합니다."
    )
