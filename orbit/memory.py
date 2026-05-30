from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

from openai import AsyncOpenAI

from orbit.core import env_float, env_int, log
from orbit.meet_types import ChatMessage, MeetingState
from orbit.transcript import TranscriptSegment


@dataclass
class MemorySource:
    label: str
    source_type: str
    meeting_code: str | None = None
    author: str | None = None
    timestamp_text: str | None = None
    speaker_name: str | None = None
    speaker_label: str | None = None
    start_ms: int | None = None
    end_ms: int | None = None


@dataclass
class MemorySearchResult:
    text: str
    score: float
    source: MemorySource


@dataclass
class MemoryAnswer:
    answer: str
    sources: list[MemorySource] = field(default_factory=list)
    mode: str = "memory_answer"


class MemoryService(Protocol):
    async def record_meeting_chat(self, state: MeetingState, message: ChatMessage) -> None:
        ...

    async def record_transcript_segments(
        self,
        state: MeetingState,
        segments: list[TranscriptSegment],
    ) -> None:
        ...

    async def finalize_meeting(self, state: MeetingState) -> None:
        ...

    async def search_memory(self, query: str) -> list[MemorySearchResult]:
        ...

    async def answer_from_memory(self, question: str) -> MemoryAnswer:
        ...


class DisabledMemoryService:
    async def record_meeting_chat(self, state: MeetingState, message: ChatMessage) -> None:
        return None

    async def record_transcript_segments(
        self,
        state: MeetingState,
        segments: list[TranscriptSegment],
    ) -> None:
        return None

    async def finalize_meeting(self, state: MeetingState) -> None:
        return None

    async def search_memory(self, query: str) -> list[MemorySearchResult]:
        return []

    async def answer_from_memory(self, question: str) -> MemoryAnswer:
        return MemoryAnswer(
            "Persistent company memory is not configured yet. Set DATABASE_URL to enable memory-backed answers.",
            mode="insufficient_memory",
        )


def build_memory_service(openai_client: AsyncOpenAI, answer_model: str) -> MemoryService:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        log("DATABASE_URL is not set. Persistent memory is disabled.", level="important")
        return DisabledMemoryService()

    from orbit.postgres_memory import PostgresMemoryService

    return PostgresMemoryService(
        database_url=database_url,
        openai_client=openai_client,
        answer_model=answer_model,
        embedding_model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        organization_id=os.environ.get("ORBIT_ORGANIZATION_ID", "default"),
        search_limit=env_int("ORBIT_MEMORY_SEARCH_LIMIT", 6),
        similarity_threshold=env_float("ORBIT_MEMORY_SIMILARITY_THRESHOLD", 0.35),
    )
