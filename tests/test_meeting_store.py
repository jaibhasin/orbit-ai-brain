from __future__ import annotations

import unittest

from orbit.meeting_store import MEETING_SCHEMA_SQL, DisabledMeetingStore, PostgresMeetingStore


class FakeCursor:
    def __init__(self, *, fetchone_results=None, events=None):
        self.fetchone_results = list(fetchone_results or [])
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


if __name__ == "__main__":
    unittest.main()
