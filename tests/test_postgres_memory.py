from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from orbit.meet_types import ChatMessage, MeetingState
from orbit.postgres_memory import EMBEDDING_DIMENSIONS, PostgresMemoryService
from orbit.postgres_schema import MEMORY_SCHEMA_SQL
from orbit.transcript import TranscriptSegment


class FakeCursor:
    def __init__(self, *, fetchone_results=None, fetchall_results=None, events=None):
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results or [])
        self.events = events if events is not None else []
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def execute(self, sql, params=None):
        normalized_sql = " ".join(sql.split())
        self.executions.append((normalized_sql, params))
        self.events.append(("execute", normalized_sql, params))

    async def fetchone(self):
        return self.fetchone_results.pop(0)

    async def fetchall(self):
        return self.fetchall_results.pop(0)


class FakeConnection:
    def __init__(self, cursor, events=None):
        self._cursor = cursor
        self.events = events if events is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.events.append(("commit",))


def build_state():
    return MeetingState(
        session_id="session-1",
        meet_url="https://meet.google.com/abc-defg-hij",
        meeting_code="abc-defg-hij",
        display_name="Orbit",
    )


def build_segment():
    return TranscriptSegment(
        source_id="segment-1",
        raw_text="launch launch next friday",
        clean_text="Launch next friday.",
        memory_text="Meet abc-defg-hij transcript: Launch next friday.",
    )


def build_service():
    return PostgresMemoryService(
        database_url="postgresql://example",
        openai_client=object(),
        answer_model="answer-model",
        embedding_model="embedding-model",
        organization_id="org-1",
        similarity_threshold=0.42,
    )


class PostgresMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chat_text_is_committed_before_embedding_indexing(self):
        events = []
        cursor = FakeCursor(fetchone_results=[{"id": 11}], events=events)
        connection = FakeConnection(cursor, events=events)
        service = build_service()
        service.ensure_ready = AsyncMock()
        service._connect = AsyncMock(return_value=connection)

        async def retry_indexing(**kwargs):
            events.append(("retry_indexing", kwargs))

        service.retry_memory_chunk_indexing = retry_indexing
        message = ChatMessage(
            fingerprint="message-1",
            raw_text="  hello there  ",
            normalized_text="hello there",
            author="Jai",
            timestamp_text="12:00 AM",
        )

        await service.record_meeting_chat(build_state(), message)

        chat_insert = next(
            event for event in events if "INSERT INTO orbit_private.orbit_chat_messages" in event[1]
        )
        chunk_insert = next(
            event for event in events if "INSERT INTO orbit_private.orbit_memory_chunks" in event[1]
        )
        commit_index = events.index(("commit",))
        retry_index = next(index for index, event in enumerate(events) if event[0] == "retry_indexing")

        self.assertIn("  hello there  ", chat_insert[2])
        self.assertIn("hello there", chat_insert[2])
        self.assertIn("Meet abc-defg-hij chat - Jai [12:00 AM]: hello there", chunk_insert[2])
        self.assertLess(commit_index, retry_index)

    async def test_transcript_text_is_committed_before_embedding_indexing(self):
        events = []
        cursor = FakeCursor(fetchone_results=[{"segment_id": "segment-uuid"}], events=events)
        connection = FakeConnection(cursor, events=events)
        service = build_service()
        service.ensure_ready = AsyncMock()
        service._connect = AsyncMock(return_value=connection)

        async def retry_indexing(**kwargs):
            events.append(("retry_indexing", kwargs))

        service.retry_memory_chunk_indexing = retry_indexing

        await service.record_transcript_segments(build_state(), [build_segment()])

        transcript_insert = next(
            event for event in events if "INSERT INTO orbit_private.orbit_transcript_segments" in event[1]
        )
        chunk_insert = next(
            event for event in events if "INSERT INTO orbit_private.orbit_memory_chunks" in event[1]
        )
        commit_index = events.index(("commit",))
        retry_index = next(index for index, event in enumerate(events) if event[0] == "retry_indexing")

        self.assertIn("launch launch next friday", transcript_insert[2])
        self.assertIn("Launch next friday.", transcript_insert[2])
        self.assertIn("Meet abc-defg-hij transcript: Launch next friday.", chunk_insert[2])
        self.assertLess(commit_index, retry_index)

    async def test_embedding_failure_marks_chunk_failed_without_raising(self):
        select_cursor = FakeCursor(
            fetchall_results=[[{"id": 7, "source_id": "chunk-1", "text": "stored transcript text"}]]
        )
        update_cursor = FakeCursor()
        service = build_service()
        service.ensure_ready = AsyncMock()
        service._connect = AsyncMock(
            side_effect=[
                FakeConnection(select_cursor),
                FakeConnection(update_cursor),
            ]
        )
        service._embed = AsyncMock(side_effect=RuntimeError("embedding unavailable"))

        await service.retry_memory_chunk_indexing(source_ids=["chunk-1"])

        failed_update = next(
            execution for execution in update_cursor.executions if "index_status = 'failed'" in execution[0]
        )
        self.assertIn("embedding unavailable", failed_update[1][0])

    async def test_retry_indexing_includes_chunks_from_old_embedding_model(self):
        cursor = FakeCursor(fetchall_results=[[]])
        service = build_service()
        service.ensure_ready = AsyncMock()
        service._connect = AsyncMock(return_value=FakeConnection(cursor))

        await service.retry_memory_chunk_indexing()

        sql, params = cursor.executions[0]
        self.assertIn("embedding_model IS DISTINCT FROM %s", sql)
        self.assertEqual(params[1], "embedding-model")

    async def test_search_is_scoped_and_thresholded(self):
        cursor = FakeCursor(fetchall_results=[[]])
        service = build_service()
        service.ensure_ready = AsyncMock()
        service._connect = AsyncMock(return_value=FakeConnection(cursor))
        service._embed = AsyncMock(return_value=[0.0] * EMBEDDING_DIMENSIONS)

        await service.search_memory("launch date")

        sql, params = cursor.executions[0]
        self.assertIn("WHERE organization_id = %s", sql)
        self.assertIn("AND index_status = 'indexed'", sql)
        self.assertIn("AND embedding_model = %s", sql)
        self.assertIn("WHERE score >= %s", sql)
        self.assertEqual(params[1], "org-1")
        self.assertEqual(params[2], "embedding-model")
        self.assertEqual(params[3], 0.42)

    async def test_embedding_dimension_mismatch_is_rejected(self):
        embeddings = AsyncMock()
        embeddings.create.return_value = type(
            "Response",
            (),
            {"data": [type("Data", (), {"embedding": [0.0]})()]},
        )()
        service = build_service()
        service.openai_client = type("Client", (), {"embeddings": embeddings})()

        with self.assertRaisesRegex(RuntimeError, "Embedding dimension mismatch"):
            await service._embed("text")


class PostgresSchemaTests(unittest.TestCase):
    def test_private_schema_migration_revokes_api_roles_and_preserves_text(self):
        self.assertIn("ALTER TABLE public.orbit_memory_chunks SET SCHEMA orbit_private", MEMORY_SCHEMA_SQL)
        self.assertIn("REVOKE ALL ON SCHEMA orbit_private FROM anon", MEMORY_SCHEMA_SQL)
        self.assertIn("REVOKE ALL ON SCHEMA orbit_private FROM authenticated", MEMORY_SCHEMA_SQL)
        self.assertIn("raw_text TEXT NOT NULL", MEMORY_SCHEMA_SQL)
        self.assertIn("clean_text TEXT NOT NULL", MEMORY_SCHEMA_SQL)
        self.assertIn("memory_text TEXT NOT NULL", MEMORY_SCHEMA_SQL)
        self.assertIn("text TEXT NOT NULL", MEMORY_SCHEMA_SQL)
        self.assertIn("embedding vector(1536)", MEMORY_SCHEMA_SQL)
        self.assertIn("index_status TEXT NOT NULL DEFAULT 'pending'", MEMORY_SCHEMA_SQL)
        self.assertIn("DROP INDEX IF EXISTS orbit_private.orbit_memory_chunks_embedding_idx", MEMORY_SCHEMA_SQL)


if __name__ == "__main__":
    unittest.main()
