from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = "Asia/Kolkata"


def timezone(name: str | None = None) -> ZoneInfo:
    return ZoneInfo(name or DEFAULT_TIMEZONE)


def local_datetime(day: date, clock: time, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, clock, tzinfo=tz)


def to_utc(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value.astimezone(UTC)


def to_timezone(value: datetime, tz: ZoneInfo) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(tz)


def normalize_range(
    start: datetime, end: datetime, tz: ZoneInfo
) -> tuple[datetime, datetime]:
    normalized = to_utc(start, tz), to_utc(end, tz)
    if normalized[1] <= normalized[0]:
        raise ValueError("scheduled_end must be after scheduled_start")
    return normalized


def reinterpret_utc_wall_time(value: datetime, tz: ZoneInfo) -> datetime:
    """Repair a UTC value that was originally persisted from a naive local wall time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(tzinfo=None).replace(tzinfo=tz).astimezone(UTC)
