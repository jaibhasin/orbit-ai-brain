from __future__ import annotations

import unittest

from orbit.meeting_store import MEETING_SCHEMA_SQL, DisabledMeetingStore, PostgresMeetingStore


class FakeCursor:
    def __init__(self, *, fetchone_results=None, fetchall_results=None, events=None):
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results if fetchall_results is not None else [])
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
        return self.fetchall_results


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


class FakeMeetingStore(PostgresMeetingStore):
    def __init__(self, cursors):
        super().__init__(database_url="postgresql://example")
        self._cursors = list(cursors)

    async def _connect(self):
        cursor = self._cursors.pop(0)
        return FakeConnection(cursor, events=cursor.events)


class MeetingStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_meeting_store_nop_with_missing_urls(self):
        store = DisabledMeetingStore()
        self.assertIsNone(await store.create_source("gmeet"))

    async def test_existing_person_is_reused(self):
        store = FakeMeetingStore(
            [
                FakeCursor(),
                FakeCursor(fetchone_results=[{"id": "person-123"}]),
            ]
        )

        person_id = await store.find_or_create_person_by_phone("15551234567")

        self.assertEqual(person_id, "person-123")
        # Select query should return existing row; insert must not run.
        self.assertEqual(len(store._cursors), 0)

    async def test_new_person_is_inserted_and_returned(self):
        lookup_cursor = FakeCursor(fetchone_results=[None, {"id": "person-new"}])
        store = FakeMeetingStore([lookup_cursor])
        store._ready = True

        person_id = await store.find_or_create_person_by_phone("15551234567", name="Orbit")

        self.assertEqual(person_id, "person-new")
        insert_sql = lookup_cursor.executions[1][0]
        self.assertIn("INSERT INTO people", insert_sql)

    async def test_create_source_and_meeting_write_rows(self):
        source_cursor = FakeCursor(fetchone_results=[{"id": "source-1"}])
        meeting_cursor = FakeCursor(fetchone_results=[{"id": "meeting-1"}])
        store = FakeMeetingStore([FakeCursor(), source_cursor, meeting_cursor])

        source_id = await store.create_source("gmeet", url="https://meet.google.com/abc-defg-hij")
        meeting_id = await store.create_meeting(
            "https://meet.google.com/abc-defg-hij",
            source_id=source_id,
            status="joining",
            requested_by_person_id="person-1",
        )

        self.assertEqual(source_id, "source-1")
        self.assertEqual(meeting_id, "meeting-1")
        self.assertIn("INSERT INTO sources", source_cursor.executions[0][0])
        self.assertIn("INSERT INTO meetings", meeting_cursor.executions[0][0])

    async def test_update_meeting_status_updates_processed(self):
        cursor = FakeCursor(events=[])
        store = FakeMeetingStore([cursor])
        store._ready = True
        # Pre-seed execute with no prior rows required.
        await store.update_meeting_status(
            "meeting-1",
            "processed",
            started_at="2026-05-31T00:00:00",
            ended_at="2026-05-31T01:00:00",
            summary_short="done",
            summary_long=None,
        )
        sql, params = cursor.executions[0]
        self.assertIn("UPDATE meetings SET", sql)
        self.assertEqual(params[0], "processed")
        self.assertEqual(params[-1], "meeting-1")

    async def test_schema_sql_includes_expected_new_tables(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS people", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS sources", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS meetings", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS source_chunks", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS extraction_runs", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS decisions", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS action_items", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE TABLE IF NOT EXISTS memories", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_source_chunks_source_id", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_decisions_meeting_id", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_action_items_meeting_id", MEETING_SCHEMA_SQL)
        self.assertIn("CREATE INDEX IF NOT EXISTS idx_memories_meeting_id", MEETING_SCHEMA_SQL)

    async def test_save_source_chunks_inserts_text_with_order_and_metadata(self):
        cursor = FakeCursor()
        store = FakeMeetingStore([cursor])
        store._ready = True
        source_id = "source-1"
        inserted = await store.save_transcript_chunks(
            source_id,
            [
                {
                    "speakerLabel": "Orbit",
                    "startMs": 1000,
                    "endMs": 2000,
                    "text": "one",
                    "metadata": {"topic": "test"},
                },
                {"text": "   "},
                {
                    "speaker_label": "Priya",
                    "startMs": 2500,
                    "endMs": 3300,
                    "text": "two",
                },
                {
                    "text": "",
                    "speakerLabel": "Ignored",
                },
            ],
        )

        self.assertEqual(inserted, 2)
        self.assertIn("INSERT INTO source_chunks", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][1], 0)
        self.assertEqual(cursor.executions[1][1][1], 1)
        self.assertEqual(cursor.executions[0][1][2], "Orbit")
        self.assertEqual(cursor.executions[1][1][2], "Priya")
        self.assertEqual(cursor.executions[0][1][3], 1000)
        self.assertEqual(cursor.executions[1][1][4], 3300)
        self.assertEqual(cursor.executions[0][1][5], "one")
        self.assertEqual(cursor.executions[1][1][5], "two")

    async def test_save_source_chunks_returns_zero_without_source_id(self):
        cursor = FakeCursor()
        store = FakeMeetingStore([cursor])
        store._ready = True

        inserted = await store.save_transcript_chunks("", [{"text": "one"}])

        self.assertEqual(inserted, 0)
        self.assertEqual(cursor.executions, [])

    async def test_saveTranscriptChunks_payload_format_works(self):
        cursor = FakeCursor()
        store = FakeMeetingStore([cursor])
        store._ready = True

        inserted = await store.saveTranscriptChunks(
            {"sourceId": "source-1", "chunks": [{"text": "hello"}]}
        )

        self.assertEqual(inserted, 1)
        self.assertIn("INSERT INTO source_chunks", cursor.executions[0][0])

    async def test_get_source_chunks_by_source_id_orders_results(self):
        cursor = FakeCursor(
            fetchall_results=[
                {
                    "chunk_index": 0,
                    "speaker_label": "Aman",
                    "start_ms": 0,
                    "end_ms": 2500,
                    "text": "We should launch next week.",
                    "metadata": {"a": 1},
                },
                {
                    "chunk_index": 1,
                    "speaker_label": "Ravi",
                    "start_ms": 3000,
                    "end_ms": 4200,
                    "text": "Payments are still failing.",
                    "metadata": {"a": 2},
                },
            ]
        )
        store = FakeMeetingStore([cursor])
        store._ready = True

        chunks = await store.get_source_chunks_by_source_id("source-1")
        order_query = cursor.executions[0][0]
        self.assertEqual(order_query.count("ORDER BY chunk_index ASC"), 1)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0]["chunk_index"], 0)
        self.assertEqual(chunks[1]["chunk_index"], 1)

    async def test_create_extraction_run_inserts_payload(self):
        cursor = FakeCursor(fetchone_results=[{"id": "extraction-1"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        extraction_id = await store.create_extraction_run(
            source_id="source-1",
            meeting_id="meeting-1",
            run_type="full_meeting_extraction",
            model="gpt-test",
            prompt_version="meeting-extractor-v1",
            output_json={"summary_short": "ok"},
            status="success",
        )

        self.assertEqual(extraction_id, "extraction-1")
        self.assertIn("INSERT INTO extraction_runs", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][0], "source-1")
        self.assertEqual(cursor.executions[0][1][1], "meeting-1")

    async def test_create_decision_inserts_trimmed_fields(self):
        cursor = FakeCursor(fetchone_results=[{"id": "decision-1"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        decision_id = await store.create_decision(
            meeting_id="meeting-1",
            source_id="source-1",
            title="  Delay launch  ",
            decision_text="  Extend timeline by a week.  ",
            rationale="  We need more time.  ",
            owner_text="  Engineering  ",
            confidence="0.86",
        )

        self.assertEqual(decision_id, "decision-1")
        self.assertIn("INSERT INTO decisions", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][2], "Delay launch")
        self.assertEqual(cursor.executions[0][1][3], "Extend timeline by a week.")
        self.assertEqual(cursor.executions[0][1][6], 0.86)

    async def test_createDecisionsFromExtraction_is_idempotent_and_skips_invalid(self):
        cursor = FakeCursor(
            fetchone_results=[{"id": "decision-1"}, {"id": "decision-2"}]
        )
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createDecisionsFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            decisions=[
                {
                    "title": "Delay launch",
                    "decision_text": "The team decided to delay launch.",
                },
                {"title": "Empty", "decision_text": "   "},
                {
                    "title": "No text",
                    "decisionText": None,
                },
                {
                    "title": "Use camel",
                    "decisionText": "Use updated date.",
                    "rationale": "",
                    "ownerText": "PM",
                    "confidence": "0.72",
                },
            ],
        )

        self.assertEqual(inserted, 2)
        self.assertIn("DELETE FROM decisions", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][0], "meeting-1")
        self.assertIn("INSERT INTO decisions", cursor.executions[1][0])

    async def test_createDecisionsFromExtraction_accepts_decision_and_text_aliases(self):
        cursor = FakeCursor(fetchone_results=[{"id": "decision-1"}, {"id": "decision-2"}, {"id": "decision-3"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createDecisionsFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            decisions=[
                {
                    "title": "Delay launch",
                    "decision": "The team decided to delay launch.",
                    "owner": "PM",
                    "confidence": "0.91",
                },
                {
                    "title": "Text key fallback",
                    "text": "Use the transcript text field.",
                    "ownerText": "Eng",
                    "confidence": 0.87,
                },
            ],
        )

        self.assertEqual(inserted, 2)
        self.assertEqual(cursor.executions[1][1][2], "Delay launch")
        self.assertEqual(cursor.executions[1][1][3], "The team decided to delay launch.")
        self.assertEqual(cursor.executions[1][1][6], 0.91)
        self.assertEqual(cursor.executions[2][1][2], "Text key fallback")
        self.assertEqual(cursor.executions[2][1][3], "Use the transcript text field.")

    async def test_create_action_item_inserts_trimmed_fields(self):
        cursor = FakeCursor(fetchone_results=[{"id": "item-1"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        item_id = await store.create_action_item(
            meeting_id="meeting-1",
            source_id="source-1",
            task="  Send invoice by Friday  ",
            owner_text="  PM  ",
            due_date="  2026-06-01  ",
            status="  in_progress ",
            confidence="0.71",
        )

        self.assertEqual(item_id, "item-1")
        self.assertIn("INSERT INTO action_items", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][2], "Send invoice by Friday")
        self.assertEqual(cursor.executions[0][1][3], "PM")
        self.assertEqual(cursor.executions[0][1][4], "2026-06-01")
        self.assertEqual(cursor.executions[0][1][5], "in_progress")
        self.assertEqual(cursor.executions[0][1][6], 0.71)

    async def test_createActionItemsFromExtraction_is_idempotent_and_skips_invalid(self):
        cursor = FakeCursor(
            fetchone_results=[{"id": "item-1"}, {"id": "item-2"}]
        )
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createActionItemsFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            action_items=[
                {
                    "task": "Follow up with finance.",
                    "ownerText": "PM",
                    "dueDate": "2026-06-02",
                    "status": "open",
                    "confidence": "0.66",
                },
                {
                    "ownerText": "PM",
                    "dueDate": "2026-06-03",
                    "status": "open",
                },
                {
                    "task": "  ",
                    "ownerText": "PM",
                },
            ],
        )

        self.assertEqual(inserted, 1)
        self.assertIn("DELETE FROM action_items", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][0], "meeting-1")
        self.assertIn("INSERT INTO action_items", cursor.executions[1][0])

    async def test_createActionItemsFromExtraction_accepts_owner_and_due_fallbacks(self):
        cursor = FakeCursor(fetchone_results=[{"id": "item-1"}, {"id": "item-2"}, {"id": "item-3"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createActionItemsFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            action_items=[
                {
                    "task": "Prepare demo",
                    "owner": "Product Manager",
                    "due": "2026-07-01",
                },
                {
                    "task": "Email updates",
                    "owner_text": "Ops",
                    "due_date": "2026-07-02",
                },
                {
                    "task": "Legacy keys",
                    "ownerText": "Finance",
                    "dueDate": "2026-07-03",
                },
            ],
        )

        self.assertEqual(inserted, 3)
        self.assertEqual(cursor.executions[1][1][3], "Product Manager")
        self.assertIsNone(cursor.executions[1][1][4])
        self.assertEqual(cursor.executions[2][1][3], "Ops")
        self.assertEqual(cursor.executions[2][1][4], "2026-07-02")
        self.assertEqual(cursor.executions[3][1][3], "Finance")
        self.assertEqual(cursor.executions[3][1][4], "2026-07-03")

    async def test_create_memory_inserts_defaults_and_validation(self):
        cursor = FakeCursor(fetchone_results=[{"id": "memory-1"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        memory_id = await store.create_memory(
            meeting_id="meeting-1",
            source_id="source-1",
            memory_type="",
            content="  Payment reliability is currently a launch risk. ",
            importance="VERY_HIGH",
            confidence="0.84",
        )

        self.assertEqual(memory_id, "memory-1")
        self.assertIn("INSERT INTO memories", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][2], "important_fact")
        self.assertEqual(cursor.executions[0][1][3], "Payment reliability is currently a launch risk.")
        self.assertEqual(cursor.executions[0][1][4], "medium")
        self.assertEqual(cursor.executions[0][1][5], 0.84)

    async def test_createMemoriesFromExtraction_is_idempotent_and_skips_invalid(self):
        cursor = FakeCursor(fetchone_results=[{"id": "memory-1"}, {"id": "memory-2"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createMemoriesFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            memories=[
                {
                    "memory_type": "risk",
                    "content": "Payment reliability is currently a launch risk.",
                    "importance": "high",
                    "confidence": 0.84,
                },
                {"memory_type": "project_update", "content": "   "},
                {
                    "content": "Onboarding project is blocked by missing design approval.",
                    "importance": "low",
                    "confidence": "not-a-number",
                    "memoryType": "project_update",
                },
            ],
        )

        self.assertEqual(inserted, 2)
        self.assertIn("DELETE FROM memories", cursor.executions[0][0])
        self.assertEqual(cursor.executions[0][1][0], "meeting-1")
        self.assertIn("INSERT INTO memories", cursor.executions[1][0])

    async def test_createMemoriesFromExtraction_uses_aliases_and_defaults(self):
        cursor = FakeCursor(fetchone_results=[{"id": "memory-1"}, {"id": "memory-2"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createMemoriesFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            memories=[
                {
                    "memory_type": "risk",
                    "content": "Payments are flaky.",
                    "importance": "high",
                },
                {
                    "memoryType": "open_question",
                    "content": "There is an unresolved pricing question.",
                    "importance": "invalid",
                },
            ],
        )

        self.assertEqual(inserted, 2)
        self.assertEqual(cursor.executions[1][1][2], "risk")
        self.assertEqual(cursor.executions[1][1][4], "high")
        self.assertEqual(cursor.executions[2][1][2], "open_question")
        self.assertEqual(cursor.executions[2][1][4], "medium")

    async def test_createMemoriesFromExtraction_accepts_memory_alias(self):
        cursor = FakeCursor(fetchone_results=[{"id": "memory-1"}])
        store = FakeMeetingStore([cursor])
        store._ready = True
        inserted = await store.createMemoriesFromExtraction(
            meeting_id="meeting-1",
            source_id="source-1",
            memories=[
                {
                    "memory": "Launch date shifted to Monday.",
                    "memoryType": "important_fact",
                    "importance": "medium",
                }
            ],
        )

        self.assertEqual(inserted, 1)
        self.assertEqual(cursor.executions[1][1][3], "Launch date shifted to Monday.")

    async def test_get_decisions_by_meeting_id(self):
        cursor = FakeCursor(
            fetchall_results=[
                {
                    "title": "Decision 1",
                    "decision_text": "First decision",
                    "rationale": "First rationale",
                    "owner_text": "PM",
                    "confidence": 0.5,
                }
            ]
        )
        store = FakeMeetingStore([cursor])
        store._ready = True
        rows = await store.get_decisions_by_meeting_id("meeting-1")
        self.assertEqual(rows, cursor.fetchall_results)
        self.assertIn("ORDER BY created_at DESC", cursor.executions[0][0])


if __name__ == "__main__":
    unittest.main()
