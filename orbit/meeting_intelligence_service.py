from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from orbit.meeting_intelligence_repository import MeetingIntelligenceRepository


def _is_valid_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (TypeError, ValueError):
        return False


def _to_iso_string(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


@dataclass
class InvalidMeetingIdError(Exception):
    code: str
    message: str


@dataclass
class MeetingNotFoundError(Exception):
    code: str
    message: str


class MeetingIntelligenceService:
    def __init__(self, repository: MeetingIntelligenceRepository):
        self.repository = repository

    async def get_intelligence(self, meeting_id: str):
        if not _is_valid_uuid(meeting_id):
            raise InvalidMeetingIdError(
                code="INVALID_MEETING_ID",
                message="Meeting id must be a valid UUID.",
            )

        meeting = await self.repository.get_meeting(meeting_id)
        if not meeting:
            raise MeetingNotFoundError(
                code="MEETING_NOT_FOUND",
                message="Meeting not found.",
            )

        status = meeting.get("status")
        status_message = self._status_message(status)
        is_processed = status == "processed"

        decision_count = 0
        action_item_count = 0
        memory_count = 0
        decisions = []
        action_items = []
        memories = []

        if is_processed:
            decisions = await self.repository.get_decisions(meeting_id)
            action_items = await self.repository.get_action_items(meeting_id)
            memories = await self.repository.get_memories(meeting_id)
            decision_count = len(decisions)
            action_item_count = len(action_items)
            memory_count = len(memories)

        meeting_payload = {
            "id": meeting.get("id"),
            "source_id": meeting.get("source_id"),
            "status": meeting.get("status"),
            "gmeet_url": meeting.get("gmeet_url"),
            "summary_short": meeting.get("summary_short"),
            "summary_long": meeting.get("summary_long"),
            "started_at": _to_iso_string(meeting.get("started_at")),
            "ended_at": _to_iso_string(meeting.get("ended_at")),
            "created_at": _to_iso_string(meeting.get("created_at")),
            "updated_at": _to_iso_string(meeting.get("updated_at")),
        }

        return {
            "meeting": meeting_payload,
            "decisions": [
                {
                    "id": row.get("id"),
                    "title": row.get("title"),
                    "decision_text": row.get("decision_text"),
                    "rationale": row.get("rationale"),
                    "owner_text": row.get("owner_text"),
                    "confidence": row.get("confidence"),
                    "created_at": _to_iso_string(row.get("created_at")),
                }
                for row in decisions
            ],
            "action_items": [
                {
                    "id": row.get("id"),
                    "task": row.get("task"),
                    "owner_text": row.get("owner_text"),
                    "due_date": row.get("due_date"),
                    "status": row.get("status"),
                    "confidence": row.get("confidence"),
                    "created_at": _to_iso_string(row.get("created_at")),
                }
                for row in action_items
            ],
            "memories": [
                {
                    "id": row.get("id"),
                    "memory_type": row.get("memory_type"),
                    "content": row.get("content"),
                    "importance": row.get("importance"),
                    "confidence": row.get("confidence"),
                    "created_at": _to_iso_string(row.get("created_at")),
                }
                for row in memories
            ],
            "meta": {
                "is_ready": is_processed,
                "has_summary": bool(
                    (meeting.get("summary_short") or "").strip()
                    or (meeting.get("summary_long") or "").strip()
                ),
                "decision_count": decision_count,
                "action_item_count": action_item_count,
                "memory_count": memory_count,
                "message": "Meeting intelligence is ready." if is_processed else status_message,
            },
        }

    @staticmethod
    def _status_message(status: str | None) -> str:
        normalized_status = (status or "").lower()
        return {
            "created": "Meeting capture has been created but has not started yet.",
            "joining": "Meeting bot is joining the meeting.",
            "live": "Meeting is currently live.",
            "processing": "Meeting is still processing.",
            "processed": "Meeting intelligence is ready.",
            "failed": "Meeting processing failed.",
        }.get(normalized_status, "Meeting status is unknown.")
