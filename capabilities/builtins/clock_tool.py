"""clock_tool.py — return current date/time."""

from datetime import datetime, timezone

from capabilities.base import BaseTool
from capabilities.result import ToolResult


class ClockTool(BaseTool):
    """Return the current date and time."""

    name = "clock"
    description = (
        "获取当前日期时间，返回 ISO 8601 格式的 UTC 时间、本地时间、"
        "Unix 时间戳和时区偏移。不需要任何参数。"
    )

    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    risk: str = "low"
    requires_approval: bool = False

    async def execute(self) -> ToolResult:
        """Return the current UTC time, local time, timestamp, and offset."""
        now_utc = datetime.now(timezone.utc)
        now_local = datetime.now().astimezone()
        offset_seconds = int(now_local.utcoffset().total_seconds()) if now_local.utcoffset() else 0
        offset_hours = offset_seconds / 3600

        utc_str = now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        local_str = now_local.strftime("%Y-%m-%dT%H:%M:%S.%f")
        unix_ts = now_utc.timestamp()

        content = (
            f"UTC:        {utc_str}\n"
            f"本地时间:   {local_str}  (UTC{offset_hours:+.0f})"
        )

        return ToolResult(
            status="success",
            content=content,
            data={
                "utc": utc_str,
                "local": local_str,
                "unix_ts": unix_ts,
                "tz_offset_hours": offset_hours,
                "iso_weekday": now_utc.isoweekday(),  # 1=Mon .. 7=Sun
            },
        )
