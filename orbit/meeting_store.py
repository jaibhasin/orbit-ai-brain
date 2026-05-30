from __future__ import annotations

import json
from dataclasses import dataclass


MEETING_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS people (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT,
    phone TEXT,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,
    url TEXT,
    title TEXT,
    raw_text TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    gmeet_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    requested_by_person_id UUID REFERENCES people(id),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    summary_short TEXT,
    summary_long TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS source_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    speaker_label TEXT,
    speaker_person_id UUID REFERENCES people(id),
    start_ms INTEGER,
    end_ms INTEGER,
    text TEXT NOT NULL,
    metadata JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_source_chunks_source_id
    ON source_chunks(source_id, chunk_index);

CREATE TABLE IF NOT EXISTS extraction_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    meeting_id UUID REFERENCES meetings(id) ON DELETE CASCADE,
    run_type TEXT NOT NULL,
    model TEXT,
    prompt_version TEXT,
    output_json JSONB,
    status TEXT DEFAULT 'success',
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class DisabledMeetingStore:
    async def find_or_create_person_by_phone(self, phone: str, name: str | None = None) -> str | None:
        return None

    async def create_source(
        self,
        source_type: str,
        *,
        url: str | None = None,
        title: str | None = None,
        raw_text: str | None = None,
        raw_payload: str | None = None,
    ) -> str | None:
        return None

    async def create_meeting(
        self,
        gmeet_url: str,
        *,
        source_id: str | None,
        status: str,
        requested_by_person_id: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> str | None:
        return None

    async def update_meeting_status(
        self,
        meeting_id: str,
        status: str,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
    ) -> None:
        return None

    async def save_transcript_chunks(self, source_id: str, chunks: list[dict]) -> int:
        return 0

    async def saveTranscriptChunks(self, payload: dict) -> int:
        source_id = payload.get("sourceId") or payload.get("source_id")
        chunks = payload.get("chunks") or []
        return await self.save_transcript_chunks(source_id, chunks)

    async def get_source_chunks_by_source_id(self, source_id: str):
        return []

    async def getSourceChunksBySourceId(self, source_id: str):
        return await self.get_source_chunks_by_source_id(source_id)

    async def create_extraction_run(
        self,
        *,
        source_id: str | None = None,
        meeting_id: str | None = None,
        run_type: str = "full_meeting_extraction",
        model: str | None = None,
        prompt_version: str | None = None,
        output_json: dict | list | None = None,
        status: str = "success",
        error: str | None = None,
    ):
        return None

    async def createExtractionRun(self, payload: dict):
        if not isinstance(payload, dict):
            return None

        return await self.create_extraction_run(
            source_id=payload.get("sourceId") or payload.get("source_id"),
            meeting_id=payload.get("meetingId") or payload.get("meeting_id"),
            run_type=payload.get("runType") or payload.get("run_type", "full_meeting_extraction"),
            model=payload.get("model"),
            prompt_version=payload.get("promptVersion") or payload.get("prompt_version"),
            output_json=payload.get("outputJson") if "outputJson" in payload else payload.get("output_json"),
            status=payload.get("status", "success"),
            error=payload.get("error"),
        )

    async def get_meeting_by_id(self, meeting_id: str):
        return None


@dataclass
class PostgresMeetingStore:
    database_url: str

    async def _connect(self):
        try:
            from psycopg import AsyncConnection
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "Postgres meeting persistence requires psycopg. Run `pip install -r requirements.txt`."
            ) from error

        return await AsyncConnection.connect(self.database_url, row_factory=dict_row)

    async def _ensure_ready(self):
        if getattr(self, "_ready", False):
            return

        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(MEETING_SCHEMA_SQL)
            await conn.commit()

        self._ready = True

    async def find_or_create_person_by_phone(self, phone: str, name: str | None = None) -> str | None:
        if not phone:
            return None

        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT id
                    FROM people
                    WHERE phone = %s
                    LIMIT 1
                    """,
                    (phone,),
                )
                existing = await cursor.fetchone()
                if existing:
                    return existing["id"]

                await cursor.execute(
                    """
                    INSERT INTO people (name, phone)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (name, phone),
                )
                created = await cursor.fetchone()
                await conn.commit()
                return created["id"] if created else None

    async def create_source(
        self,
        source_type: str,
        *,
        url: str | None = None,
        title: str | None = None,
        raw_text: str | None = None,
        raw_payload: str | None = None,
    ) -> str | None:
        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO sources (source_type, url, title, raw_text, raw_payload)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (source_type, url, title, raw_text, raw_payload),
                )
                row = await cursor.fetchone()
                await conn.commit()
                return row["id"] if row else None

    async def create_meeting(
        self,
        gmeet_url: str,
        *,
        source_id: str | None,
        status: str,
        requested_by_person_id: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> str | None:
        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO meetings (
                        source_id,
                        gmeet_url,
                        status,
                        requested_by_person_id,
                        summary_short,
                        summary_long,
                        started_at,
                        ended_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        source_id,
                        gmeet_url,
                        status,
                        requested_by_person_id,
                        summary_short,
                        summary_long,
                        started_at,
                        ended_at,
                    ),
                )
                row = await cursor.fetchone()
                await conn.commit()
                return row["id"] if row else None

    async def get_meeting_by_id(self, meeting_id: str):
        if not meeting_id:
            return None

        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT
                        id,
                        source_id,
                        gmeet_url,
                        status,
                        requested_by_person_id,
                        created_at,
                        updated_at,
                        started_at,
                        ended_at,
                        summary_short,
                        summary_long
                    FROM meetings
                    WHERE id = %s
                    LIMIT 1
                    """,
                    (meeting_id,),
                )
                return await cursor.fetchone()

    async def update_meeting_status(
        self,
        meeting_id: str,
        status: str,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
    ) -> None:
        if not meeting_id:
            return

        await self._ensure_ready()
        updates = ["status = %s", "updated_at = now()"]
        values = [status]

        if started_at:
            updates.append("started_at = COALESCE(started_at, %s)")
            values.append(started_at)
        if ended_at:
            updates.append("ended_at = COALESCE(ended_at, %s)")
            values.append(ended_at)
        if summary_short is not None:
            updates.append("summary_short = COALESCE(summary_short, %s)")
            values.append(summary_short)
        if summary_long is not None:
            updates.append("summary_long = COALESCE(summary_long, %s)")
            values.append(summary_long)

        values.append(meeting_id)
        sql = f"""
            UPDATE meetings
            SET {", ".join(updates)}
            WHERE id = %s
        """

        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, values)
                await conn.commit()

    async def save_transcript_chunks(self, source_id: str, chunks: list[dict]) -> int:
        if not source_id:
            return 0

        sanitized = [self._normalize_source_chunk(chunk) for chunk in chunks or []]
        sanitized = [chunk for chunk in sanitized if chunk is not None]
        if not sanitized:
            return 0

        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                for chunk_index, chunk in enumerate(sanitized):
                    await cursor.execute(
                        """
                        INSERT INTO source_chunks (
                            source_id,
                            chunk_index,
                            speaker_label,
                            start_ms,
                            end_ms,
                            text,
                            metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        (
                            source_id,
                            chunk_index,
                            chunk["speaker_label"],
                            chunk["start_ms"],
                            chunk["end_ms"],
                            chunk["text"],
                            json.dumps(chunk["metadata"]) if chunk["metadata"] is not None else None,
                        ),
                    )
                await conn.commit()
        return len(sanitized)

    async def saveTranscriptChunks(self, payload: dict) -> int:
        if not isinstance(payload, dict):
            return 0

        source_id = payload.get("sourceId") or payload.get("source_id")
        chunks = payload.get("chunks") or []
        return await self.save_transcript_chunks(source_id, chunks)

    async def get_source_chunks_by_source_id(self, source_id: str):
        if not source_id:
            return []

        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT chunk_index, speaker_label, start_ms, end_ms, text, metadata
                    FROM source_chunks
                    WHERE source_id = %s
                    ORDER BY chunk_index ASC
                    """,
                    (source_id,),
                )
                return (await cursor.fetchall()) or []

    async def getSourceChunksBySourceId(self, source_id: str):
        return await self.get_source_chunks_by_source_id(source_id)

    async def create_extraction_run(
        self,
        *,
        source_id: str | None = None,
        meeting_id: str | None = None,
        run_type: str = "full_meeting_extraction",
        model: str | None = None,
        prompt_version: str | None = None,
        output_json: dict | list | None = None,
        status: str = "success",
        error: str | None = None,
    ):
        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO extraction_runs (
                        source_id,
                        meeting_id,
                        run_type,
                        model,
                        prompt_version,
                        output_json,
                        status,
                        error
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    RETURNING id
                    """,
                    (
                        source_id,
                        meeting_id,
                        run_type,
                        model,
                        prompt_version,
                        json.dumps(output_json) if output_json is not None else None,
                        status,
                        error,
                    ),
                )
                row = await cursor.fetchone()
                await conn.commit()
                return row["id"] if row else None

    async def createExtractionRun(self, payload: dict):
        if not isinstance(payload, dict):
            return None

        return await self.create_extraction_run(
            source_id=payload.get("sourceId") or payload.get("source_id"),
            meeting_id=payload.get("meetingId") or payload.get("meeting_id"),
            run_type=payload.get("runType") or payload.get("run_type", "full_meeting_extraction"),
            model=payload.get("model"),
            prompt_version=payload.get("promptVersion") or payload.get("prompt_version"),
            output_json=payload.get("outputJson") if "outputJson" in payload else payload.get("output_json"),
            status=payload.get("status", "success"),
            error=payload.get("error"),
        )

    def _normalize_source_chunk(self, chunk: dict) -> dict | None:
        if not isinstance(chunk, dict):
            return None

        text = str(chunk.get("text") or "").strip()
        if not text:
            return None

        return {
            "speaker_label": chunk.get("speakerLabel") or chunk.get("speaker_label"),
            "start_ms": self._as_optional_int(chunk.get("startMs"), chunk.get("start_ms")),
            "end_ms": self._as_optional_int(chunk.get("endMs"), chunk.get("end_ms")),
            "text": text,
            "metadata": chunk.get("metadata"),
        }

    def _as_optional_int(self, *values) -> int | None:
        for value in values:
            if value is None:
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return None


def build_meeting_store(database_url: str | None):
    if not database_url:
        return DisabledMeetingStore()

    return PostgresMeetingStore(database_url=database_url)
