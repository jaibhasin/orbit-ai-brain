from __future__ import annotations

import json
from dataclasses import dataclass

from openai import AsyncOpenAI

from orbit.core import log
from orbit.meet_types import ChatMessage, MeetingState
from orbit.memory import MemoryAnswer, MemorySearchResult, MemorySource
from orbit.postgres_schema import (
    DEFAULT_ORGANIZATION_ID,
    ORBIT_SCHEMA,
    apply_memory_schema,
    backfill_embedding_model,
)
from orbit.transcript import TranscriptSegment, format_timestamp_ms


EMBEDDING_DIMENSIONS = 1536
MAX_INDEX_ERROR_CHARS = 1000


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
    elif source.speaker_name:
        parts.append(source.speaker_name)
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
    organization_id: str = DEFAULT_ORGANIZATION_ID
    search_limit: int = 6
    similarity_threshold: float = 0.35

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
                await apply_memory_schema(cur)
                await backfill_embedding_model(cur, self.embedding_model)
                await conn.commit()

        self._ready = True

    async def record_meeting_chat(self, state: MeetingState, message: ChatMessage) -> None:
        await self.ensure_ready()
        source_id = f"{state.session_id}:{message.fingerprint}"
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await self._upsert_session(cur, state)
                await cur.execute(
                    f"""
                    INSERT INTO {ORBIT_SCHEMA}.orbit_chat_messages (
                        organization_id,
                        session_id,
                        meeting_code,
                        fingerprint,
                        author,
                        timestamp_text,
                        raw_text,
                        normalized_text
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, fingerprint) DO UPDATE SET
                        organization_id = EXCLUDED.organization_id,
                        meeting_code = EXCLUDED.meeting_code,
                        author = EXCLUDED.author,
                        timestamp_text = EXCLUDED.timestamp_text,
                        raw_text = EXCLUDED.raw_text,
                        normalized_text = EXCLUDED.normalized_text
                    RETURNING id
                    """,
                    (
                        self.organization_id,
                        state.session_id,
                        state.meeting_code,
                        message.fingerprint,
                        message.author,
                        message.timestamp_text,
                        message.raw_text,
                        message.normalized_text,
                    ),
                )
                chat_message_id = (await cur.fetchone())["id"]
                await self._upsert_memory_chunk(
                    cur,
                    source_type="meet_chat",
                    source_id=source_id,
                    state=state,
                    text=self._format_chat_message_chunk(state, message),
                    metadata={
                        "author": message.author,
                        "timestamp_text": message.timestamp_text,
                        "fingerprint": message.fingerprint,
                    },
                    chat_message_id=chat_message_id,
                )
                await conn.commit()

        log(
            f"Stored chat message and searchable text chunk for Meet {state.meeting_code}.",
            state.session_id,
            level="debug",
        )
        await self.retry_memory_chunk_indexing(source_ids=[source_id])

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
                    metadata = self._transcript_metadata(segment)
                    await cur.execute(
                        f"""
                        INSERT INTO {ORBIT_SCHEMA}.orbit_transcript_segments (
                            organization_id,
                            session_id,
                            meeting_code,
                            source_id,
                            source_type,
                            speaker_name,
                            speaker_label,
                            speaker_source,
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
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        ON CONFLICT (session_id, source_id) DO UPDATE SET
                            organization_id = EXCLUDED.organization_id,
                            meeting_code = EXCLUDED.meeting_code,
                            source_type = EXCLUDED.source_type,
                            speaker_name = EXCLUDED.speaker_name,
                            speaker_label = EXCLUDED.speaker_label,
                            speaker_source = EXCLUDED.speaker_source,
                            speaker_confidence = EXCLUDED.speaker_confidence,
                            detected_language = EXCLUDED.detected_language,
                            start_ms = EXCLUDED.start_ms,
                            end_ms = EXCLUDED.end_ms,
                            raw_text = EXCLUDED.raw_text,
                            clean_text = EXCLUDED.clean_text,
                            memory_text = EXCLUDED.memory_text,
                            confidence = EXCLUDED.confidence,
                            metadata = EXCLUDED.metadata
                        RETURNING segment_id
                        """,
                        (
                            self.organization_id,
                            state.session_id,
                            state.meeting_code,
                            segment.source_id,
                            segment.source_type,
                            segment.speaker_name,
                            segment.speaker_label,
                            segment.speaker_source,
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
                    transcript_segment_id = (await cur.fetchone())["segment_id"]
                    chunk_source_id = f"{state.session_id}:{segment.source_id}"
                    await self._upsert_memory_chunk(
                        cur,
                        source_type="meet_transcript",
                        source_id=chunk_source_id,
                        state=state,
                        text=segment.memory_text,
                        metadata=metadata,
                        transcript_segment_id=transcript_segment_id,
                    )

                await conn.commit()

        log(
            f"Stored {len(segments)} transcript segment row(s) with raw and normalized text "
            f"for Meet {state.meeting_code}.",
            state.session_id,
            level="debug",
        )
        await self.retry_memory_chunk_indexing(session_id=state.session_id)

    async def finalize_meeting(self, state: MeetingState) -> None:
        await self.ensure_ready()
        chat_source_ids = []
        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await self._upsert_session(cur, state)
                await cur.execute(
                    f"""
                    SELECT id, fingerprint, author, timestamp_text, normalized_text
                    FROM {ORBIT_SCHEMA}.orbit_chat_messages
                    WHERE organization_id = %s AND session_id = %s
                    ORDER BY id
                    """,
                    (self.organization_id, state.session_id),
                )
                rows = await cur.fetchall()

                for row in rows:
                    source_id = f"{state.session_id}:{row['fingerprint']}"
                    await self._upsert_memory_chunk(
                        cur,
                        source_type="meet_chat",
                        source_id=source_id,
                        state=state,
                        text=self._format_chat_chunk(state, row),
                        metadata={
                            "author": row["author"],
                            "timestamp_text": row["timestamp_text"],
                            "fingerprint": row["fingerprint"],
                        },
                        chat_message_id=row["id"],
                    )
                    chat_source_ids.append(source_id)

                await conn.commit()

        log(
            f"Stored {len(chat_source_ids)} chat memory chunk text row(s) for Meet "
            f"{state.meeting_code}.",
            state.session_id,
            level="debug",
        )
        await self.retry_memory_chunk_indexing(session_id=state.session_id)

    async def retry_memory_chunk_indexing(
        self,
        *,
        session_id: str | None = None,
        source_ids: list[str] | None = None,
        limit: int = 100,
    ) -> tuple[int, int]:
        await self.ensure_ready()
        clauses = [
            "organization_id = %s",
            "(index_status IN ('pending', 'failed') OR embedding_model IS DISTINCT FROM %s)",
        ]
        params: list[object] = [self.organization_id, self.embedding_model]
        if session_id:
            clauses.append("session_id = %s")
            params.append(session_id)
        if source_ids:
            clauses.append("source_id = ANY(%s)")
            params.append(source_ids)
        params.append(limit)

        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT id, source_id, text
                    FROM {ORBIT_SCHEMA}.orbit_memory_chunks
                    WHERE {" AND ".join(clauses)}
                    ORDER BY created_at, id
                    LIMIT %s
                    """,
                    tuple(params),
                )
                chunks = await cur.fetchall()

        if not chunks:
            return 0, 0

        indexed = []
        failed = []
        for chunk in chunks:
            try:
                embedding = await self._embed(chunk["text"])
                indexed.append((chunk["id"], embedding))
            except Exception as error:
                failed.append((chunk["id"], str(error)[:MAX_INDEX_ERROR_CHARS]))

        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                for chunk_id, embedding in indexed:
                    await cur.execute(
                        f"""
                        UPDATE {ORBIT_SCHEMA}.orbit_memory_chunks
                        SET embedding = %s::vector,
                            embedding_model = %s,
                            index_status = 'indexed',
                            index_error = NULL,
                            indexed_at = now(),
                            updated_at = now()
                        WHERE id = %s AND organization_id = %s
                        """,
                        (
                            vector_literal(embedding),
                            self.embedding_model,
                            chunk_id,
                            self.organization_id,
                        ),
                    )
                for chunk_id, error_detail in failed:
                    await cur.execute(
                        f"""
                        UPDATE {ORBIT_SCHEMA}.orbit_memory_chunks
                        SET index_status = 'failed',
                            index_error = %s,
                            updated_at = now()
                        WHERE id = %s AND organization_id = %s
                        """,
                        (error_detail, chunk_id, self.organization_id),
                    )
                await conn.commit()

        log(
            f"Memory indexing result: {len(indexed)} indexed, {len(failed)} failed "
            f"for organization {self.organization_id}.",
            session_id,
            level="debug",
        )
        return len(indexed), len(failed)

    async def search_memory(self, query: str) -> list[MemorySearchResult]:
        await self.ensure_ready()
        embedding = await self._embed(query)
        embedding_literal = vector_literal(embedding)

        async with await self._connect() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"""
                    SELECT text, source_type, meeting_code, metadata, score
                    FROM (
                        SELECT
                            text,
                            source_type,
                            meeting_code,
                            metadata,
                            1 - (embedding <=> %s::vector) AS score
                        FROM {ORBIT_SCHEMA}.orbit_memory_chunks
                        WHERE organization_id = %s
                          AND index_status = 'indexed'
                          AND embedding IS NOT NULL
                          AND embedding_model = %s
                    ) AS ranked
                    WHERE score >= %s
                    ORDER BY score DESC
                    LIMIT %s
                    """,
                    (
                        embedding_literal,
                        self.organization_id,
                        self.embedding_model,
                        self.similarity_threshold,
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
                                speaker_name=metadata.get("speaker_name"),
                                speaker_label=metadata.get("speaker_label"),
                                start_ms=metadata.get("start_ms"),
                                end_ms=metadata.get("end_ms"),
                            )
                        ),
                        source_type=row["source_type"],
                        meeting_code=row["meeting_code"],
                        author=metadata.get("author"),
                        timestamp_text=metadata.get("timestamp_text"),
                        speaker_name=metadata.get("speaker_name"),
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
            f"""
            INSERT INTO {ORBIT_SCHEMA}.orbit_meet_sessions (
                organization_id,
                session_id,
                meet_url,
                meeting_code,
                display_name,
                status,
                status_detail,
                leave_reason,
                last_error,
                joined_at,
                finished_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                meet_url = EXCLUDED.meet_url,
                meeting_code = EXCLUDED.meeting_code,
                display_name = EXCLUDED.display_name,
                status = EXCLUDED.status,
                status_detail = EXCLUDED.status_detail,
                leave_reason = EXCLUDED.leave_reason,
                last_error = EXCLUDED.last_error,
                joined_at = COALESCE(EXCLUDED.joined_at, orbit_meet_sessions.joined_at),
                finished_at = COALESCE(EXCLUDED.finished_at, orbit_meet_sessions.finished_at),
                updated_at = now()
            """,
            (
                self.organization_id,
                state.session_id,
                state.meet_url,
                state.meeting_code,
                state.display_name,
                state.status,
                state.status_detail,
                state.leave_reason,
                state.last_error,
                state.joined_at,
                state.finished_at,
            ),
        )

    async def _upsert_memory_chunk(
        self,
        cur,
        *,
        source_type: str,
        source_id: str,
        state: MeetingState,
        text: str,
        metadata: dict,
        chat_message_id=None,
        transcript_segment_id=None,
    ) -> None:
        await cur.execute(
            f"""
            INSERT INTO {ORBIT_SCHEMA}.orbit_memory_chunks AS existing (
                organization_id,
                source_type,
                source_id,
                meeting_code,
                session_id,
                chat_message_id,
                transcript_segment_id,
                text,
                metadata,
                index_status
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, 'pending')
            ON CONFLICT (source_type, source_id) DO UPDATE SET
                organization_id = EXCLUDED.organization_id,
                meeting_code = EXCLUDED.meeting_code,
                session_id = EXCLUDED.session_id,
                chat_message_id = EXCLUDED.chat_message_id,
                transcript_segment_id = EXCLUDED.transcript_segment_id,
                text = EXCLUDED.text,
                metadata = EXCLUDED.metadata,
                embedding = CASE
                    WHEN existing.text IS DISTINCT FROM EXCLUDED.text THEN NULL
                    ELSE existing.embedding
                END,
                embedding_model = CASE
                    WHEN existing.text IS DISTINCT FROM EXCLUDED.text THEN NULL
                    ELSE existing.embedding_model
                END,
                index_status = CASE
                    WHEN existing.text IS DISTINCT FROM EXCLUDED.text THEN 'pending'
                    ELSE existing.index_status
                END,
                index_error = CASE
                    WHEN existing.text IS DISTINCT FROM EXCLUDED.text THEN NULL
                    ELSE existing.index_error
                END,
                indexed_at = CASE
                    WHEN existing.text IS DISTINCT FROM EXCLUDED.text THEN NULL
                    ELSE existing.indexed_at
                END,
                updated_at = now()
            """,
            (
                self.organization_id,
                source_type,
                source_id,
                state.meeting_code,
                state.session_id,
                chat_message_id,
                transcript_segment_id,
                text,
                json.dumps(metadata),
            ),
        )

    async def _embed(self, text: str) -> list[float]:
        response = await self.openai_client.embeddings.create(
            model=self.embedding_model,
            input=text,
            dimensions=EMBEDDING_DIMENSIONS,
        )
        embedding = response.data[0].embedding
        if len(embedding) != EMBEDDING_DIMENSIONS:
            raise RuntimeError(
                f"Embedding dimension mismatch: expected {EMBEDDING_DIMENSIONS}, got {len(embedding)}."
            )
        return embedding

    @staticmethod
    def _transcript_metadata(segment: TranscriptSegment) -> dict:
        metadata = dict(segment.metadata)
        metadata["speaker_confidence"] = segment.speaker_confidence
        metadata["detected_language"] = segment.detected_language
        metadata["speaker_name"] = segment.speaker_name
        metadata["speaker_source"] = segment.speaker_source
        return metadata

    @staticmethod
    def _format_chat_chunk(state: MeetingState, row) -> str:
        author = row["author"] or "unknown"
        timestamp = f" [{row['timestamp_text']}]" if row["timestamp_text"] else ""
        return f"Meet {state.meeting_code} chat - {author}{timestamp}: {row['normalized_text']}"

    @staticmethod
    def _format_chat_message_chunk(state: MeetingState, message: ChatMessage) -> str:
        author = message.author or "unknown"
        timestamp = f" [{message.timestamp_text}]" if message.timestamp_text else ""
        return f"Meet {state.meeting_code} chat - {author}{timestamp}: {message.normalized_text}"
