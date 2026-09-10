from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = ("월", "화", "수", "목", "금", "토", "일")
GRACE = timedelta(minutes=2)


@dataclass(frozen=True)
class Schedule:
    weekday: int
    hour: int
    minute: int
    revision: int
    updated_at: datetime


@dataclass(frozen=True)
class Reminder:
    kind: str
    meeting_at: datetime
    due_at: datetime
    revision: int

    @property
    def key(self) -> str:
        return f"{self.revision}:{self.meeting_at.isoformat()}:{self.kind}"


def validate_time(weekday: int, hour: int, minute: int) -> None:
    if not 0 <= weekday <= 6 or not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("요일·시간 범위가 올바르지 않습니다.")


def next_meeting(schedule: Schedule, now: datetime, timezone: ZoneInfo) -> datetime:
    local = now.astimezone(timezone)
    day = local.date() + timedelta(days=(schedule.weekday - local.weekday()) % 7)
    meeting = datetime.combine(day, time(schedule.hour, schedule.minute), timezone)
    if meeting <= local:
        meeting += timedelta(days=7)
    return meeting


def due_reminders(schedule: Schedule, now: datetime, timezone: ZoneInfo) -> list[Reminder]:
    local = now.astimezone(timezone)
    reminders = []
    # Include tomorrow so a 00:05 meeting receives its reminder at 23:55 today.
    for offset in (0, 1):
        day = local.date() + timedelta(days=offset)
        if day.weekday() != schedule.weekday:
            continue
        meeting = datetime.combine(day, time(schedule.hour, schedule.minute), timezone)
        morning = datetime.combine(day, time(9), timezone)
        candidates = [("ten_minutes", meeting - timedelta(minutes=10))]
        if morning < meeting:
            candidates.append(("morning", morning))
        for kind, due in candidates:
            if schedule.updated_at <= due <= local < meeting and local - due <= GRACE:
                reminders.append(Reminder(kind, meeting, due, schedule.revision))
    return sorted(reminders, key=lambda reminder: reminder.due_at)
