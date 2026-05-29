from __future__ import annotations

import json
from dataclasses import dataclass

from openai import AsyncOpenAI

from orbit.core import log
from orbit.meet_types import ChatMessage, MeetingState
from orbit.memory import MemoryAnswer, MemorySearchResult, MemorySource
from orbit.transcript import TranscriptSegment, format_timestamp_ms


EMBEDDING_DIMENSIONS = 1536


def vector_literal(values):
    return "[" + ",".join(str(float(value)) for value in values) + "]"


def coerce_float(value, default=0.0):
    if value is None:
        return default
    return float(value)


def format_source(source):
    parts = []
    if source.meeting_code:
        parts.append(f"Meet {source.meeting_code}")
    if source.author:
        parts.append(source.author)
    elif source.speaker_label:
        parts.append(source.speaker_label)
    elif source.start_ms is not None:
        start_text = format_timestamp_ms(source.start_ms)
        end_text = format_timestamp_ms(source.end_ms)
        if start_text and end_text:
            parts.append(f"{start_text}-{end_text}")
        elif start_text:
            parts.append(start_text)
    if source.timestamp_text:
        parts.append(source.timestamp_text)
    return " / ".join(parts) or source.label


@dataclass
class PostgresMemoryService:
    database_url: str
    openai_client: AsyncOpenAI
    answer_model: str
    embedding_model: str
    search_limit: int = 6

    def __post_init__(self):
        self._ready = False

    async def _connect(self):
        try:
            from psycopg import AsyncConnection
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "Postgres memory requires psycopg. Run `pip install -r requirements.txt`."
            ) from error

        return await AsyncConnection.connect(self.database_url, row_factory=dict_row)

    async def ensure_ready(self):
        if self._ready:
            return

        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orbit_meet_sessions (
                        session_id TEXT PRIMARY KEY,
                        meet_url TEXT NOT NULL,
                        meeting_code TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        status TEXT NOT NULL,
                        joined_at TEXT,
                        finished_at TEXT,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                    )
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orbit_chat_messages (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES orbit_meet_sessions(session_id) ON DELETE CASCADE,
                        meeting_code TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        author TEXT,
                        timestamp_text TEXT,
                        raw_text TEXT NOT NULL,
                        normalized_text TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        UNIQUE (session_id, fingerprint)
                    )
                    """
                )
                await cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS orbit_transcript_segments (
                        id BIGSERIAL PRIMARY KEY,
                        session_id TEXT NOT NULL REFERENCES orbit_meet_sessions(session_id) ON DELETE CASCADE,
                        meeting_code TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        speaker_label TEXT,
                        speaker_confidence TEXT NOT NULL,
                        detected_language TEXT,
                        start_ms INTEGER,
                        end_ms INTEGER,
                        raw_text TEXT NOT NULL,
                        clean_text TEXT NOT NULL,
                        memory_text TEXT NOT NULL,
                        confidence REAL,
                        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        UNIQUE (session_id, source_id)
                    )
                    """
                )
                await cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS orbit_memory_chunks (
                        id BIGSERIAL PRIMARY KEY,
                        source_type TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        meeting_code TEXT,
                        session_id TEXT,
                        text TEXT NOT NULL,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                        UNIQUE (source_type, source_id)
                    )
                    """
                )
                await cur.execute(
                    """
                    CREATE INDEX IF NOT EXISTS orbit_memory_chunks_embedding_idx
                    ON orbit_memory_chunks
                    USING ivfflat (embedding vector_cosine_ops)
                    WITH (lists = 100)
                    """
                )
                await conn.commit()

        self._ready = True

    async def record_meeting_chat(self, state: MeetingState, message: ChatMessage) -> None:
        await self.ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await self._upsert_session(cur, state)
                await cur.execute(
                    """
                    INSERT INTO orbit_chat_messages (
                        session_id,
                        meeting_code,
                        fingerprint,
                        author,
                        timestamp_text,
                        raw_text,
                        normalized_text
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, fingerprint) DO NOTHING
                    """,
                    (
                        state.session_id,
                        state.meeting_code,
                        message.fingerprint,
                        message.author,
                        message.timestamp_text,
                        message.raw_text,
                        message.normalized_text,
                    ),
                )
                await conn.commit()

    async def record_transcript_segments(
        self,
        state: MeetingState,
        segments: list[TranscriptSegment],
    ) -> None:
        if not segments:
            return

        await self.ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await self._upsert_session(cur, state)

                for segment in segments:
                    metadata = dict(segment.metadata)
                    metadata["speaker_confidence"] = segment.speaker_confidence
                    metadata["detected_language"] = segment.detected_language
                    embedding = await self._embed(segment.memory_text)

                    await cur.execute(
                        """
                        INSERT INTO orbit_transcript_segments (
                            session_id,
                            meeting_code,
                            source_id,
                            source_type,
                            speaker_label,
                            speaker_confidence,
                            detected_language,
                            start_ms,
                            end_ms,
                            raw_text,
                            clean_text,
                            memory_text,
                            confidence,
                            metadata
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (session_id, source_id) DO UPDATE SET
                            speaker_label = EXCLUDED.speaker_label,
                            speaker_confidence = EXCLUDED.speaker_confidence,
                            detected_language = EXCLUDED.detected_language,
                            start_ms = EXCLUDED.start_ms,
                            end_ms = EXCLUDED.end_ms,
                            raw_text = EXCLUDED.raw_text,
                            clean_text = EXCLUDED.clean_text,
                            memory_text = EXCLUDED.memory_text,
                            confidence = EXCLUDED.confidence,
                            metadata = EXCLUDED.metadata
                        """,
                        (
                            state.session_id,
                            state.meeting_code,
                            segment.source_id,
                            segment.source_type,
                            segment.speaker_label,
                            segment.speaker_confidence,
                            segment.detected_language,
                            segment.start_ms,
                            segment.end_ms,
                            segment.raw_text,
                            segment.clean_text,
                            segment.memory_text,
                            segment.confidence,
                            json.dumps(metadata),
                        ),
                    )
                    await cur.execute(
                        """
                        INSERT INTO orbit_memory_chunks (
                            source_type,
                            source_id,
                            meeting_code,
                            session_id,
                            text,
                            metadata,
                            embedding
                        )
                        VALUES ('meet_transcript', %s, %s, %s, %s, %s::jsonb, %s::vector)
                        ON CONFLICT (source_type, source_id) DO UPDATE SET
                            text = EXCLUDED.text,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding
                        """,
                        (
                            f"{state.session_id}:{segment.source_id}",
                            state.meeting_code,
                            state.session_id,
                            segment.memory_text,
                            json.dumps(
                                {
                                    "speaker_label": segment.speaker_label,
                                    "speaker_confidence": segment.speaker_confidence,
                                    "detected_language": segment.detected_language,
                                    "start_ms": segment.start_ms,
                                    "end_ms": segment.end_ms,
                                    "raw_text": segment.raw_text,
                                    "clean_text": segment.clean_text,
                                }
                            ),
                            vector_literal(embedding),
                        ),
                    )

                await conn.commit()
                log(
                    f"Indexed {len(segments)} transcript segment(s) for Meet {state.meeting_code}.",
                    state.session_id,
                )

    async def finalize_meeting(self, state: MeetingState) -> None:
        await self.ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await self._upsert_session(cur, state)
                await cur.execute(
                    """
                    SELECT id, fingerprint, author, timestamp_text, normalized_text
                    FROM orbit_chat_messages
                    WHERE session_id = %s
                    ORDER BY id
                    """,
                    (state.session_id,),
                )
                rows = await cur.fetchall()

                for row in rows:
                    text = self._format_chat_chunk(state, row)
                    embedding = await self._embed(text)
                    metadata = {
                        "author": row["author"],
                        "timestamp_text": row["timestamp_text"],
                        "fingerprint": row["fingerprint"],
                    }
                    await cur.execute(
                        """
                        INSERT INTO orbit_memory_chunks (
                            source_type,
                            source_id,
                            meeting_code,
                            session_id,
                            text,
                            metadata,
                            embedding
                        )
                        VALUES ('meet_chat', %s, %s, %s, %s, %s::jsonb, %s::vector)
                        ON CONFLICT (source_type, source_id) DO UPDATE SET
                            text = EXCLUDED.text,
                            metadata = EXCLUDED.metadata,
                            embedding = EXCLUDED.embedding
                        """,
                        (
                            f"{state.session_id}:{row['fingerprint']}",
                            state.meeting_code,
                            state.session_id,
                            text,
                            json.dumps(metadata),
                            vector_literal(embedding),
                        ),
                    )

                await conn.commit()
                log(f"Indexed {len(rows)} memory chunk(s) for Meet {state.meeting_code}.", state.session_id)

    async def search_memory(self, query: str) -> list[MemorySearchResult]:
        await self.ensure_ready()
        embedding = await self._embed(query)

        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT
                        text,
                        source_type,
                        meeting_code,
                        metadata,
                        1 - (embedding <=> %s::vector) AS score
                    FROM orbit_memory_chunks
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (
                        vector_literal(embedding),
                        vector_literal(embedding),
                        self.search_limit,
                    ),
                )
                rows = await cur.fetchall()

        results = []
        for row in rows:
            metadata = row["metadata"] or {}
            results.append(
                MemorySearchResult(
                    text=row["text"],
                    score=coerce_float(row["score"]),
                    source=MemorySource(
                        label=format_source(
                            MemorySource(
                                label=row["source_type"],
                                source_type=row["source_type"],
                                meeting_code=row["meeting_code"],
                                author=metadata.get("author"),
                                timestamp_text=metadata.get("timestamp_text"),
                                speaker_label=metadata.get("speaker_label"),
                                start_ms=metadata.get("start_ms"),
                                end_ms=metadata.get("end_ms"),
                            )
                        ),
                        source_type=row["source_type"],
                        meeting_code=row["meeting_code"],
                        author=metadata.get("author"),
                        timestamp_text=metadata.get("timestamp_text"),
                        speaker_label=metadata.get("speaker_label"),
                        start_ms=metadata.get("start_ms"),
                        end_ms=metadata.get("end_ms"),
                    ),
                )
            )
        return results

    async def answer_from_memory(self, question: str) -> MemoryAnswer:
        results = await self.search_memory(question)
        if not results:
            return MemoryAnswer(
                "I do not have enough company memory yet to answer that.",
                mode="insufficient_memory",
            )

        context = "\n\n".join(
            f"Source {index}: {format_source(result.source)}\n{result.text}"
            for index, result in enumerate(results, start=1)
        )
        prompt = (
            "Answer the question using only the company memory context below. "
            "If the context is insufficient, say so briefly. Include no invented facts.\n\n"
            f"Question:\n{question}\n\n"
            f"Company memory:\n{context}"
        )

        response = await self.openai_client.chat.completions.create(
            model=self.answer_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are Orbit, an AI layer for company knowledge. "
                        "Answer concisely using only retrieved company memory."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        )
        answer = response.choices[0].message.content if response.choices else ""
        answer = (answer or "").strip() or "I do not have enough company memory yet to answer that."
        return MemoryAnswer(
            answer=answer,
            sources=[result.source for result in results],
            mode="memory_answer",
        )

    async def _upsert_session(self, cur, state: MeetingState):
        await cur.execute(
            """
            INSERT INTO orbit_meet_sessions (
                session_id,
                meet_url,
                meeting_code,
                display_name,
                status,
                joined_at,
                finished_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE SET
                status = EXCLUDED.status,
                joined_at = COALESCE(EXCLUDED.joined_at, orbit_meet_sessions.joined_at),
                finished_at = COALESCE(EXCLUDED.finished_at, orbit_meet_sessions.finished_at)
            """,
            (
                state.session_id,
                state.meet_url,
                state.meeting_code,
                state.display_name,
                state.status,
                state.joined_at,
                state.finished_at,
            ),
        )

    async def _embed(self, text: str) -> list[float]:
        response = await self.openai_client.embeddings.create(
            model=self.embedding_model,
            input=text,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        return response.data[0].embedding

    def _format_chat_chunk(self, state: MeetingState, row) -> str:
        author = row["author"] or "unknown"
        timestamp = f" [{row['timestamp_text']}]" if row["timestamp_text"] else ""
        return f"Meet {state.meeting_code} chat - {author}{timestamp}: {row['normalized_text']}"
