"""current_time — a tiny tool the LLM can call to get authoritative wall-clock.

Small models (gemini-flash-lite, haiku-tier) sometimes ignore the per-turn
<current-time> block injected by agent.py context_provider and hallucinate the
current time from training data / recent chat context — leading to sched tasks
firing at the wrong hour. Giving them a callable tool removes the excuse: when
the agent needs "now" for a computation ("10 phút nữa", "chiều mai"), it can
call this to be sure, and the returned struct is unambiguous."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import aisuite as ai

_SCHEMA = {
    "type": "function",
    "function": {
        "name": "current_time",
        "description": (
            "Get the current wall-clock time — always call this before scheduling "
            "a task, quoting the current time, or computing 'X minutes/hours from "
            "now'. Returns local time in the user's timezone (default UTC+7 "
            "Asia/Ho_Chi_Minh), the weekday, and epoch seconds. The result is "
            "authoritative — do not use any other time source that disagrees "
            "(system prompt, training data, session-start snapshot)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "offset_minutes": {
                    "type": "integer",
                    "description": (
                        "Optional — add this many minutes to 'now' before returning. "
                        "Use it to compute a fire_at for a scheduled task in one call "
                        "(e.g. offset_minutes=10 for '10 phút nữa')."
                    ),
                }
            },
            "required": [],
        },
    },
}


def make_current_time_tool() -> Callable[..., Any]:
    def current_time(offset_minutes: int = 0) -> dict[str, Any]:
        try:
            off_h = float(os.environ.get("TZ_OFFSET_HOURS", "7"))
        except ValueError:
            off_h = 7.0
        label = os.environ.get("TZ_LABEL", "UTC+7 Asia/Ho_Chi_Minh")
        tz = timezone(timedelta(hours=off_h))
        now = datetime.now(tz)
        target = now + timedelta(minutes=int(offset_minutes or 0))
        return {
            "now": {
                "iso": now.strftime("%Y-%m-%dT%H:%M:%S"),
                "date": now.date().isoformat(),
                "time": now.strftime("%H:%M:%S"),
                "weekday": now.strftime("%A"),
                "epoch": int(now.timestamp()),
                "timezone": label,
            },
            "target": {
                "iso": target.strftime("%Y-%m-%dT%H:%M:%S"),
                "epoch": int(target.timestamp()),
                "offset_minutes": int(offset_minutes or 0),
                "human": (
                    f"{target.strftime('%H:%M')} giờ VN"
                    if label.startswith("UTC+7")
                    else target.strftime("%Y-%m-%d %H:%M:%S")
                ),
            },
            "hint": (
                "Khi tạo fire_at cho scheduled task: dùng field `target.iso` "
                "(naive ISO local, KHÔNG suffix Z / +07:00). Trường `timezone` "
                "của task nên để 'local'."
            ),
        }

    current_time.__name__ = "current_time"
    current_time.__doc__ = _SCHEMA["function"]["description"]
    current_time.__aisuite_tool_metadata__ = ai.ToolMetadata(
        name="current_time",
        category="system",
        risk_level="low",
        capabilities=["clock"],
        requires_approval=False,
    )
    current_time.__coworker_schema__ = _SCHEMA
    return current_time
