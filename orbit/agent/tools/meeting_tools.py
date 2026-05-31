from __future__ import annotations

from orbit.agent.tools._shared import (
    ConfigurationError,
    NotFoundError,
    _normalize_limit,
    _query_row,
    _query_rows,
    _require_database_url,
    _require_google_meet_url,
    _require_uuid,
    _run_sync,
    _to_iso_string,
)
from orbit.meeting_intelligence_repository import build_meeting_intelligence_repository
from orbit.meeting_intelligence_service import (
    MeetingIntelligenceService,
    MeetingNotFoundError,
)
from orbit.meeting_store import DisabledMeetingStore, build_meeting_store


def get_meeting_intelligence(meeting_id: str) -> dict:
    _require_uuid(
        meeting_id,
        field_name="meeting_id",
        error_code="INVALID_MEETING_ID",
        required_message="Meeting id must be a valid UUID.",
    )

    database_url = _require_database_url()
    repository = build_meeting_intelligence_repository(database_url)
    service = MeetingIntelligenceService(repository=repository)
    return _run_sync(service.get_intelligence(meeting_id))


def get_meeting_capture_status(meeting_id: str) -> dict:
    meeting_id = _require_uuid(
        meeting_id,
        field_name="meeting_id",
        error_code="INVALID_MEETING_ID",
        required_message="Meeting id must be a valid UUID.",
    )

    async def _handler():
        database_url = _require_database_url()
        store = build_meeting_store(database_url)
        meeting = await store.get_meeting_by_id(meeting_id)
        if not meeting:
            raise MeetingNotFoundError(
                code="MEETING_NOT_FOUND",
                message="Meeting not found.",
            )
        return {
            "meeting_id": meeting["id"],
            "status": meeting["status"],
            "started_at": _to_iso_string(meeting.get("started_at")),
            "ended_at": _to_iso_string(meeting.get("ended_at")),
            "error": None,
        }

    return _run_sync(_handler())


def request_meeting_capture(gmeet_url: str, requested_by_person_id: str) -> dict:
    gmeet_url = _require_google_meet_url(gmeet_url)
    requested_by_person_id = _require_uuid(
        requested_by_person_id,
        field_name="requested_by_person_id",
        error_code="INVALID_PERSON_ID",
        required_message="requested_by_person_id must be a valid UUID.",
    )

    async def _fetch_person(database_url: str, person_id: str):
        return await _query_row(
            database_url,
            """
            SELECT id
            FROM people
            WHERE id = %s
            LIMIT 1
            """,
            (person_id,),
        )

    async def _handler():
        database_url = _require_database_url()
        person = await _fetch_person(database_url, requested_by_person_id)
        if not person:
            raise NotFoundError(
                code="PERSON_NOT_FOUND",
                message="Requested person was not found.",
            )

        store = build_meeting_store(database_url)
        if isinstance(store, DisabledMeetingStore):
            raise ConfigurationError(
                code="MEETING_STORE_UNAVAILABLE",
                message="Meeting persistence store is unavailable.",
            )

        source_id = await store.create_source("gmeet", url=gmeet_url)
        if not source_id:
            raise ConfigurationError(
                code="MEETING_CAPTURE_CREATE_FAILED",
                message="Failed to create source record for the request.",
            )

        meeting_id = await store.create_meeting(
            gmeet_url=gmeet_url,
            source_id=source_id,
            status="created",
            requested_by_person_id=requested_by_person_id,
        )
        if not meeting_id:
            raise ConfigurationError(
                code="MEETING_CAPTURE_CREATE_FAILED",
                message="Failed to create meeting record for the request.",
            )

        # TODO: enqueue a capture job using the project's queue/worker abstraction when added.
        return {
            "meeting_id": meeting_id,
            "status": "created",
            "message": "Meeting capture created.",
        }

    return _run_sync(_handler())


def get_recent_meetings(limit: int = 5, status: str | None = None) -> list[dict]:
    safe_limit = _normalize_limit(limit, default=5, maximum=20)

    normalized_status = (status or "").strip()

    async def _handler():
        database_url = _require_database_url()
        conditions = []
        params: list[str | int] = []

        if normalized_status:
            conditions.append("status = %s")
            params.append(normalized_status.lower())

        where_clause = "WHERE " + " AND ".join(conditions) if conditions else ""
        params.append(safe_limit)

        rows = await _query_rows(
            database_url,
            f"""
            SELECT
                id,
                status,
                started_at,
                ended_at,
                summary_short,
                created_at
            FROM meetings
            {where_clause}
            ORDER BY created_at DESC
            LIMIT %s
            """,
            tuple(params),
        )

        return [
            {
                "meeting_id": row["id"],
                "title": None,
                "status": row["status"],
                "started_at": _to_iso_string(row.get("started_at")),
                "ended_at": _to_iso_string(row.get("ended_at")),
                "summary_short": row.get("summary_short"),
            }
            for row in rows
        ]

    return _run_sync(_handler())
