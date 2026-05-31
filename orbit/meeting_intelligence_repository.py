from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orbit.meeting_store import build_meeting_store


def _to_float(value):
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_value_created_at(value):
    if value is None:
        return ""
    return value


@dataclass
class MeetingIntelligenceRepository:
    meeting_store: Any

    async def get_meeting(self, meeting_id: str):
        return await self.meeting_store.get_meeting_by_id(meeting_id)

    async def get_decisions(self, meeting_id: str):
        decisions = await self.meeting_store.get_decisions_by_meeting_id(meeting_id)
        rows = list(decisions or [])
        rows.sort(
            key=lambda row: (
                row.get("confidence") is None,
                -(_to_float(row.get("confidence")) or 0.0),
                _sort_value_created_at(row.get("created_at")),
            )
        )
        return rows

    async def get_action_items(self, meeting_id: str):
        action_items = await self.meeting_store.get_action_items_by_meeting_id(meeting_id)
        rows = list(action_items or [])
        rows.sort(
            key=lambda row: (
                0 if (row.get("status") or "").strip().lower() == "open" else 1,
                (row.get("due_date") is None),
                row.get("due_date") or "",
                row.get("confidence") is None,
                -(_to_float(row.get("confidence")) or 0.0),
                _sort_value_created_at(row.get("created_at")),
            )
        )
        return rows

    async def get_memories(self, meeting_id: str):
        memories = await self.meeting_store.get_memories_by_meeting_id(meeting_id)
        rows = list(memories or [])
        rows.sort(
            key=lambda row: (
                {"high": 0, "medium": 1, "low": 2}.get((row.get("importance") or "").lower(), 3),
                row.get("confidence") is None,
                -(_to_float(row.get("confidence")) or 0.0),
                _sort_value_created_at(row.get("created_at")),
            )
        )
        return rows


def build_meeting_intelligence_repository(database_url: str | None):
    return MeetingIntelligenceRepository(meeting_store=build_meeting_store(database_url))
