from __future__ import annotations

from os import getenv

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from orbit.meeting_intelligence_repository import build_meeting_intelligence_repository
from orbit.meeting_intelligence_service import (
    InvalidMeetingIdError,
    MeetingIntelligenceService,
    MeetingNotFoundError,
)

router = APIRouter()


@router.get("/meetings/{meeting_id}/intelligence")
async def get_meeting_intelligence(meeting_id: str):
    repository = build_meeting_intelligence_repository(getenv("DATABASE_URL"))
    service = MeetingIntelligenceService(repository=repository)
    try:
        payload = await service.get_intelligence(meeting_id)
    except InvalidMeetingIdError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"code": exc.code, "message": exc.message}},
        )
    except MeetingNotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return payload
