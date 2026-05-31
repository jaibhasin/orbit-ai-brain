from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from orbit.agent.tools import (
    get_meeting_capture_status,
    get_meeting_intelligence,
    get_open_action_items,
    get_recent_meetings,
    request_meeting_capture,
    search_company_memory,
    search_decisions,
    send_whatsapp_reply,
)
from orbit.agent.tools._shared import NotFoundError, ValidationError
from orbit.meeting_intelligence_repository import MeetingIntelligenceRepository
from orbit.meeting_intelligence_service import MeetingNotFoundError


VALID_MEETING_ID = "123e4567-e89b-12d3-a456-426614174000"
VALID_PERSON_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


def build_meeting_payload(meeting_id: str, *, status: str = "processed", **overrides):
    payload = {
        "id": meeting_id,
        "source_id": "222e4567-e89b-12d3-a456-426614174001",
        "status": status,
        "gmeet_url": "https://meet.google.com/abc-defg-hij",
        "summary_short": None,
        "summary_long": None,
        "started_at": None,
        "ended_at": None,
        "created_at": "2026-05-31T10:00:00+00:00",
        "updated_at": "2026-05-31T10:00:00+00:00",
    }
    payload.update(overrides)
    return payload


class FakeMeetingStore:
    def __init__(
        self,
        *,
        meeting,
        decisions=None,
        action_items=None,
        memories=None,
        source_create_count: int = 0,
    ):
        self.meeting = meeting
        self.decisions = decisions or []
        self.action_items = action_items or []
        self.memories = memories or []
        self.source_create_calls = []
        self.meeting_create_input = None
        self.source_create_count = source_create_count
        self.calls = {
            "get_meeting_by_id": 0,
            "get_decisions_by_meeting_id": 0,
            "get_action_items_by_meeting_id": 0,
            "get_memories_by_meeting_id": 0,
            "create_source": 0,
            "create_meeting": 0,
            "get_source_chunks_by_source_id": 0,
            "create_extraction_run": 0,
        }

    async def get_meeting_by_id(self, meeting_id: str):
        self.calls["get_meeting_by_id"] += 1
        return self.meeting if (self.meeting or {}).get("id") == meeting_id else None

    async def get_decisions_by_meeting_id(self, meeting_id: str):
        self.calls["get_decisions_by_meeting_id"] += 1
        return self.decisions if (self.meeting or {}).get("id") == meeting_id else []

    async def get_action_items_by_meeting_id(self, meeting_id: str):
        self.calls["get_action_items_by_meeting_id"] += 1
        return self.action_items if (self.meeting or {}).get("id") == meeting_id else []

    async def get_memories_by_meeting_id(self, meeting_id: str):
        self.calls["get_memories_by_meeting_id"] += 1
        return self.memories if (self.meeting or {}).get("id") == meeting_id else []

    async def create_source(self, source_type: str, *, url: str | None = None, **kwargs):
        self.calls["create_source"] += 1
        self.source_create_calls.append((source_type, url, kwargs))
        return f"source-{self.source_create_count + 1}"

    async def create_meeting(self, gmeet_url, *, source_id=None, status=None, requested_by_person_id=None, **kwargs):
        self.calls["create_meeting"] += 1
        self.meeting_create_input = {
            "gmeet_url": gmeet_url,
            "source_id": source_id,
            "status": status,
            "requested_by_person_id": requested_by_person_id,
            "kwargs": kwargs,
        }
        return f"meeting-{self.source_create_count + 1}"

    async def get_source_chunks_by_source_id(self, source_id: str):
        self.calls["get_source_chunks_by_source_id"] += 1
        return []

    async def create_extraction_run(self, *args, **kwargs):
        self.calls["create_extraction_run"] += 1
        return "run-1"

    # Keep source id stable across create_source/create_meeting sequence.
    @property
    def source_id(self):
        return f"source-{self.source_create_count + 1}"


class FakeCaptureStore:
    def __init__(self):
        self.create_source_calls = []
        self.create_meeting_calls = []
        self.calls = {"create_source": 0, "create_meeting": 0}

    async def create_source(self, source_type, *, url=None, **kwargs):
        self.calls["create_source"] += 1
        self.create_source_calls.append((source_type, url, kwargs))
        return "source-1"

    async def create_meeting(
        self,
        gmeet_url,
        *,
        source_id=None,
        status=None,
        requested_by_person_id=None,
        **kwargs,
    ):
        self.calls["create_meeting"] += 1
        self.create_meeting_calls.append(
            {
                "gmeet_url": gmeet_url,
                "source_id": source_id,
                "status": status,
                "requested_by_person_id": requested_by_person_id,
                "kwargs": kwargs,
            }
        )
        return "meeting-1"


class AgentToolsImportTests(unittest.TestCase):
    def test_tools_are_importable_from_central_module(self):
        self.assertTrue(callable(get_meeting_intelligence))
        self.assertTrue(callable(get_meeting_capture_status))
        self.assertTrue(callable(request_meeting_capture))
        self.assertTrue(callable(get_recent_meetings))
        self.assertTrue(callable(get_open_action_items))
        self.assertTrue(callable(search_decisions))
        self.assertTrue(callable(search_company_memory))
        self.assertTrue(callable(send_whatsapp_reply))


class MeetingIntelligenceToolTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _forbidden_fields(payload: dict):
        forbidden = {
            "source_chunks",
            "extraction_runs",
            "audio",
            "audio_metadata",
            "capture_logs",
            "chrome",
            "deepgram",
            "capture",
        }
        return forbidden

    async def test_get_meeting_intelligence_returns_shape_and_no_internal_fields(self):
        meeting = build_meeting_payload(VALID_MEETING_ID)
        decisions = [
            {
                "id": "d1",
                "title": "Decision A",
                "decision_text": "ship to prod",
                "rationale": "high confidence",
                "owner_text": "PM",
                "confidence": 0.9,
                "created_at": "2026-05-31T10:05:00+00:00",
            }
        ]
        action_items = [
            {
                "id": "a1",
                "task": "follow up",
                "owner_text": "QA",
                "due_date": "2026-06-01",
                "status": "open",
                "confidence": 0.75,
                "created_at": "2026-05-31T10:10:00+00:00",
            }
        ]
        memories = [
            {
                "id": "m1",
                "memory_type": "risk",
                "content": "Potential latency regression",
                "importance": "high",
                "confidence": 0.8,
                "created_at": "2026-05-31T10:15:00+00:00",
            }
        ]
        store = FakeMeetingStore(
            meeting=meeting,
            decisions=decisions,
            action_items=action_items,
            memories=memories,
            source_create_count=1,
        )
        repository = MeetingIntelligenceRepository(meeting_store=store)

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch(
                "orbit.agent.tools.meeting_tools.build_meeting_intelligence_repository",
                return_value=repository,
            ):
                payload = await get_meeting_intelligence(VALID_MEETING_ID)

        self.assertSetEqual(
            set(payload.keys()),
            {"meeting", "decisions", "action_items", "memories", "meta"},
        )
        self.assertEqual(payload["meeting"]["id"], VALID_MEETING_ID)
        self.assertEqual(payload["decisions"][0]["id"], "d1")
        self.assertEqual(payload["action_items"][0]["id"], "a1")
        self.assertEqual(payload["memories"][0]["id"], "m1")

        forbidden = self._forbidden_fields(payload)
        self.assertFalse(any(key in payload["meeting"] for key in forbidden))
        self.assertFalse(any(key in payload["meta"] for key in forbidden))
        for item in payload["decisions"]:
            self.assertFalse(any(key in item for key in forbidden))
        for item in payload["action_items"]:
            self.assertFalse(any(key in item for key in forbidden))
        for item in payload["memories"]:
            self.assertFalse(any(key in item for key in forbidden))

        for key in [
            "source_chunks",
            "extraction_runs",
            "audio",
            "audio_metadata",
            "chrome",
            "deepgram",
            "capture",
        ]:
            self.assertNotIn(key, payload)

    async def test_get_meeting_intelligence_raises_for_invalid_uuid(self):
        with self.assertRaises(ValidationError) as exc_info:
            await get_meeting_intelligence("not-a-uuid")

        self.assertEqual(exc_info.exception.code, "INVALID_MEETING_ID")

    async def test_get_meeting_intelligence_raises_for_missing_meeting(self):
        store = FakeMeetingStore(meeting=None)
        repository = MeetingIntelligenceRepository(meeting_store=store)

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch(
                "orbit.agent.tools.meeting_tools.build_meeting_intelligence_repository",
                return_value=repository,
            ):
                with self.assertRaises(MeetingNotFoundError) as exc_info:
                    await get_meeting_intelligence(VALID_MEETING_ID)

        self.assertEqual(exc_info.exception.code, "MEETING_NOT_FOUND")


class MeetingCaptureStatusToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_meeting_capture_status_returns_status_payload(self):
        meeting = build_meeting_payload(VALID_MEETING_ID, status="created")
        store = FakeCaptureStore()

        async def get_meeting_by_id(meeting_id: str):
            return meeting if meeting_id == VALID_MEETING_ID else None

        store.get_meeting_by_id = get_meeting_by_id

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools.build_meeting_store", return_value=store):
                payload = await get_meeting_capture_status(VALID_MEETING_ID)

        self.assertEqual(payload["meeting_id"], VALID_MEETING_ID)
        self.assertEqual(payload["status"], "created")
        self.assertIsNone(payload["started_at"])
        self.assertIsNone(payload["ended_at"])
        self.assertIsNone(payload["error"])

    async def test_get_meeting_capture_status_returns_processed_payload(self):
        meeting = build_meeting_payload(VALID_MEETING_ID, status="processed")

        async def get_meeting_by_id(meeting_id: str):
            return meeting

        store = FakeCaptureStore()
        store.get_meeting_by_id = get_meeting_by_id

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools.build_meeting_store", return_value=store):
                payload = await get_meeting_capture_status(VALID_MEETING_ID)

        self.assertEqual(payload["status"], "processed")


class RequestMeetingCaptureToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_meeting_capture_creates_source_and_meeting(self):
        store = FakeCaptureStore()
        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools._query_row", return_value={"id": VALID_PERSON_ID}):
                with patch("orbit.agent.tools.meeting_tools.build_meeting_store", return_value=store):
                    result = await request_meeting_capture(
                        gmeet_url="https://meet.google.com/abc-defg-hij",
                        requested_by_person_id=VALID_PERSON_ID,
                    )

        self.assertEqual(result["meeting_id"], "meeting-1")
        self.assertEqual(result["status"], "created")
        self.assertEqual(result["message"], "Meeting capture created.")

        self.assertEqual(store.calls["create_source"], 1)
        self.assertEqual(store.calls["create_meeting"], 1)
        self.assertEqual(store.create_source_calls[0][0], "gmeet")
        self.assertEqual(store.create_source_calls[0][1], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(store.create_meeting_calls[0]["gmeet_url"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(store.create_meeting_calls[0]["requested_by_person_id"], VALID_PERSON_ID)
        self.assertEqual(store.create_meeting_calls[0]["status"], "created")


class ToolInputValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_uuid_for_intelligence(self):
        with self.assertRaises(ValidationError) as exc_info:
            await get_meeting_intelligence("not-a-uuid")

        self.assertEqual(exc_info.exception.code, "INVALID_MEETING_ID")

    async def test_invalid_uuid_for_capture_status(self):
        with self.assertRaises(ValidationError) as exc_info:
            await get_meeting_capture_status("not-a-uuid")

        self.assertEqual(exc_info.exception.code, "INVALID_MEETING_ID")

    async def test_invalid_person_uuid_for_request_capture(self):
        with self.assertRaises(ValidationError) as exc_info:
            await request_meeting_capture(
                gmeet_url="https://meet.google.com/abc-defg-hij",
                requested_by_person_id="not-a-uuid",
            )

        self.assertEqual(exc_info.exception.code, "INVALID_PERSON_ID")

    async def test_invalid_uuid_for_whatsapp_reply(self):
        with self.assertRaises(ValidationError) as exc_info:
            await send_whatsapp_reply("not-a-uuid", "hello")

        self.assertEqual(exc_info.exception.code, "INVALID_PERSON_ID")

    async def test_empty_query_for_search_decisions(self):
        with self.assertRaises(ValidationError) as exc_info:
            await search_decisions("   ")

        self.assertEqual(exc_info.exception.code, "EMPTY_QUERY")

    async def test_empty_query_for_search_memories(self):
        with self.assertRaises(ValidationError) as exc_info:
            await search_company_memory("")

        self.assertEqual(exc_info.exception.code, "EMPTY_QUERY")


class RecentMeetingsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_meetings_default_limit_and_ordering(self):
        rows = [
            {
                "id": "m-new",
                "status": "processed",
                "started_at": None,
                "ended_at": None,
                "summary_short": "New",
                "created_at": "2026-05-31T10:00:00+00:00",
            },
            {
                "id": "m-old",
                "status": "created",
                "started_at": None,
                "ended_at": None,
                "summary_short": "Old",
                "summary_long": None,
                "created_at": "2026-05-30T10:00:00+00:00",
            },
        ]

        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return rows

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools._query_rows", side_effect=_query_rows) as query_rows:
                payload = await get_recent_meetings()

        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["meeting_id"], "m-new")
        self.assertEqual(payload[1]["meeting_id"], "m-old")
        self.assertEqual(payload[0]["title"], None)
        query_rows.assert_awaited_once()
        sql, params = query_rows.call_args.args[1], query_rows.call_args.args[2]
        self.assertIn("ORDER BY created_at DESC", sql)
        self.assertEqual(params[-1], 5)

    async def test_recent_meetings_custom_and_max_limits(self):
        rows = [{
            "id": f"m-{i}",
            "status": "processed",
            "started_at": None,
            "ended_at": None,
            "summary_short": f"summary-{i}",
            "created_at": f"2026-05-31T10:{i:02d}:00+00:00",
        } for i in range(25)]

        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return rows

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools._query_rows", side_effect=_query_rows) as query_rows:
                await get_recent_meetings(limit=3)

        query_rows.assert_awaited_once()
        sql, params = query_rows.call_args.args[1], query_rows.call_args.args[2]
        self.assertEqual(params[-1], 3)

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools._query_rows", side_effect=_query_rows) as query_rows_large:
                await get_recent_meetings(limit=999)

        self.assertEqual(query_rows_large.call_args.args[2][-1], 20)

    async def test_recent_meetings_status_filter(self):
        rows = [
            {
                "id": "m-1",
                "status": "processed",
                "started_at": None,
                "ended_at": None,
                "summary_short": "Ready",
                "created_at": "2026-05-31T10:00:00+00:00",
            }
        ]

        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return rows

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools._query_rows", side_effect=_query_rows) as query_rows:
                payload = await get_recent_meetings(status="processed")

        self.assertEqual(payload[0]["status"], "processed")
        sql, params = query_rows.call_args.args[1], query_rows.call_args.args[2]
        self.assertIn("WHERE status = %s", sql)
        self.assertEqual(params[0], "processed")


class OpenActionItemsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_open_action_items_filters_and_sorting_shape(self):
        rows = [
            {
                "id": "item-open-high-conf-due-sooner",
                "meeting_id": VALID_MEETING_ID,
                "task": "Priority follow-up",
                "owner_text": "Alice",
                "due_date": "2026-06-01",
                "confidence": 0.96,
                "created_at": "2026-05-31T09:10:00+00:00",
            },
            {
                "id": "item-open-low-conf-due",
                "meeting_id": VALID_MEETING_ID,
                "task": "Later follow-up",
                "owner_text": "Alice",
                "due_date": "2026-06-01",
                "confidence": 0.1,
                "created_at": "2026-05-31T09:20:00+00:00",
            },
            {
                "id": "item-open-no-due",
                "meeting_id": VALID_MEETING_ID,
                "task": "No due date",
                "owner_text": "Bob",
                "due_date": None,
                "confidence": None,
                "created_at": "2026-05-31T08:00:00+00:00",
            },
        ]

        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return rows

        with patch("orbit.agent.tools.action_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.action_tools._query_rows", side_effect=_query_rows) as query_rows:
                payload = await get_open_action_items(limit=10)

        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[0]["action_item_id"], "item-open-high-conf-due-sooner")
        self.assertEqual(payload[1]["action_item_id"], "item-open-low-conf-due")
        self.assertEqual(payload[2]["action_item_id"], "item-open-no-due")
        sql = query_rows.call_args.args[1]
        self.assertIn("WHERE status = 'open'", sql)
        self.assertIn(
            "ORDER BY (due_date IS NULL), due_date ASC, (confidence IS NULL), confidence DESC, created_at ASC",
            sql,
        )

    async def test_open_action_items_owner_and_since_filters(self):
        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return []

        with patch("orbit.agent.tools.action_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.action_tools._query_rows", side_effect=_query_rows) as query_rows:
                await get_open_action_items(owner_text="Alice", since="2026-05-30T00:00:00+00:00", limit=7)

        sql, params = query_rows.call_args.args[1], query_rows.call_args.args[2]
        self.assertIn("owner_text = %s", sql)
        self.assertIn("created_at >= %s", sql)
        self.assertIn("Alice", params)
        self.assertIn("2026-05-30T00:00:00+00:00", params)
        self.assertEqual(params[-1], 7)


class SearchDecisionsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_decisions_query_fields_and_limit(self):
        rows = [
            {
                "id": "d-title",
                "meeting_id": VALID_MEETING_ID,
                "title": "Launch date",
                "decision_text": "Push to Wednesday",
                "rationale": "Team is ready",
                "owner_text": "PM",
                "confidence": 0.91,
                "created_at": "2026-05-31T11:00:00+00:00",
            },
            {
                "id": "d-text",
                "meeting_id": VALID_MEETING_ID,
                "title": None,
                "decision_text": "Decision text has match",
                "rationale": "",
                "owner_text": "Eng",
                "confidence": 0.7,
                "created_at": "2026-05-31T10:00:00+00:00",
            },
        ]

        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return rows

        with patch("orbit.agent.tools.memory_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.memory_tools._query_rows", side_effect=_query_rows) as query_rows:
                payload = await search_decisions("Decision")

        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["decision_id"], "d-title")
        for item in payload:
            self.assertIn("decision_id", item)
            self.assertIn("meeting_id", item)
            self.assertIn("title", item)
            self.assertIn("decision_text", item)
            self.assertIn("rationale", item)
            self.assertIn("owner_text", item)
            self.assertIn("confidence", item)
            self.assertIn("created_at", item)

        sql, params = query_rows.call_args.args[1], query_rows.call_args.args[2]
        self.assertIn("ILIKE", sql)
        self.assertIn("title ILIKE %s", sql)
        self.assertIn("decision_text ILIKE %s", sql)
        self.assertIn("rationale ILIKE %s", sql)
        self.assertIn("owner_text ILIKE %s", sql)
        self.assertEqual(params[-1], 10)
        self.assertEqual(params[0], "%Decision%")


class SearchMemoryToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_memories_filters_and_sorting(self):
        rows = [
            {
                "id": "m-high",
                "meeting_id": VALID_MEETING_ID,
                "memory_type": "risk",
                "content": "High impact",
                "importance": "high",
                "confidence": 0.98,
                "created_at": "2026-05-31T11:00:00+00:00",
            },
            {
                "id": "m-medium",
                "meeting_id": VALID_MEETING_ID,
                "memory_type": "decision",
                "content": "Medium priority",
                "importance": "medium",
                "confidence": 0.72,
                "created_at": "2026-05-31T10:00:00+00:00",
            },
            {
                "id": "m-unknown",
                "meeting_id": VALID_MEETING_ID,
                "memory_type": "note",
                "content": "Other priority",
                "importance": "mystery",
                "confidence": 0.5,
                "created_at": "2026-05-31T09:00:00+00:00",
            },
        ]

        async def _query_rows(*args, **kwargs):
            _ = kwargs
            return rows

        with patch("orbit.agent.tools.memory_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.memory_tools._query_rows", side_effect=_query_rows) as query_rows:
                payload = await search_company_memory(
                    query="priority",
                    memory_type="risk",
                    importance="high",
                    limit=2,
                )

        self.assertEqual(len(payload), 3)
        self.assertEqual(payload[0]["memory_id"], "m-high")
        self.assertEqual(payload[1]["memory_id"], "m-medium")
        self.assertEqual(payload[2]["memory_id"], "m-unknown")

        sql, params = query_rows.call_args.args[1], query_rows.call_args.args[2]
        self.assertIn("content ILIKE %s", sql)
        self.assertIn("memory_type = %s", sql)
        self.assertIn("importance = %s", sql)
        self.assertEqual(params[0], "%priority%")
        self.assertEqual(params[1], "risk")
        self.assertEqual(params[2], "high")
        self.assertIn("ORDER BY", sql)
        self.assertIn("created_at DESC", sql)
        self.assertEqual(params[-1], 2)


class WhatsAppToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_whatsapp_reply_stops_on_missing_person(self):
        with patch("orbit.agent.tools.whatsapp_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.whatsapp_tools._query_row", return_value=None):
                with self.assertRaises(NotFoundError) as exc_info:
                    await send_whatsapp_reply(VALID_PERSON_ID, "hello")

        self.assertEqual(exc_info.exception.code, "PERSON_NOT_FOUND")

    async def test_send_whatsapp_reply_missing_phone(self):
        with patch("orbit.agent.tools.whatsapp_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.whatsapp_tools._query_row", return_value={"id": VALID_PERSON_ID, "phone": None}):
                with self.assertRaises(NotFoundError) as exc_info:
                    await send_whatsapp_reply(VALID_PERSON_ID, "hello")

        self.assertEqual(exc_info.exception.code, "PERSON_NOT_FOUND")

    async def test_send_whatsapp_reply_not_implemented_without_twilio(self):
        with patch("orbit.agent.tools.whatsapp_tools._require_database_url", return_value="postgresql://example"):
            with patch(
                "orbit.agent.tools.whatsapp_tools._query_row",
                return_value={"id": VALID_PERSON_ID, "phone": "+15551234567"},
            ):
                with patch("orbit.agent.tools.whatsapp_tools._build_twilio_sender", return_value=None):
                    response = await send_whatsapp_reply(VALID_PERSON_ID, "hello")

        self.assertEqual(response["status"], "not_implemented")
        self.assertEqual(response["error"], "Twilio sender is not implemented yet.")

    async def test_send_whatsapp_reply_sends_via_twilio_when_configured(self):
        messages = MagicMock()
        twilio_message = type("message", (), {"sid": "SM123"})()
        messages.create.return_value = twilio_message

        with patch("orbit.agent.tools.whatsapp_tools._require_database_url", return_value="postgresql://example"):
            with patch(
                "orbit.agent.tools.whatsapp_tools._query_row",
                return_value={"id": VALID_PERSON_ID, "phone": "+15550001111"},
            ):
                with patch(
                    "orbit.agent.tools.whatsapp_tools._build_twilio_sender",
                    return_value={"messages": messages, "from_number": "whatsapp:+16660002222"},
                ):
                    response = await send_whatsapp_reply(VALID_PERSON_ID, "  hello orbit  ")

        messages.create.assert_called_once_with(
            body="hello orbit",
            from_="whatsapp:+16660002222",
            to="whatsapp:+15550001111",
        )
        self.assertEqual(response["status"], "sent")
        self.assertEqual(response["provider_message_id"], "SM123")
        self.assertIsNone(response["error"])


class ToolSafetyRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_capture_does_not_invoke_extraction_or_capture_jobs(self):
        async def _query_row(_url, _sql, _params):
            return {"id": VALID_PERSON_ID}

        store = FakeCaptureStore()

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch("orbit.agent.tools.meeting_tools._query_row", side_effect=_query_row):
                with patch("orbit.agent.tools.meeting_tools.build_meeting_store", return_value=store):
                    await request_meeting_capture(
                        gmeet_url="https://meet.google.com/abc-defg-hij",
                        requested_by_person_id=VALID_PERSON_ID,
                    )

        self.assertEqual(store.calls["create_source"], 1)
        self.assertEqual(store.calls["create_meeting"], 1)

    async def test_intelligence_response_has_no_prohibited_raw_fields(self):
        meeting = build_meeting_payload(VALID_MEETING_ID)
        store = FakeMeetingStore(meeting=meeting)
        repository = MeetingIntelligenceRepository(meeting_store=store)

        with patch("orbit.agent.tools.meeting_tools._require_database_url", return_value="postgresql://example"):
            with patch(
                "orbit.agent.tools.meeting_tools.build_meeting_intelligence_repository",
                return_value=repository,
            ):
                payload = await get_meeting_intelligence(VALID_MEETING_ID)

        forbidden_keys = {
            "source_chunks",
            "extraction_runs",
            "audio",
            "audio_metadata",
            "deepgram",
            "chrome",
            "capture",
        }
        for key in forbidden_keys:
            self.assertNotIn(key, payload)
            self.assertNotIn(key, payload["meeting"])
            if payload["decisions"]:
                self.assertNotIn(key, payload["decisions"][0])
            if payload["action_items"]:
                self.assertNotIn(key, payload["action_items"][0])
            if payload["memories"]:
                self.assertNotIn(key, payload["memories"][0])


if __name__ == "__main__":
    unittest.main()
