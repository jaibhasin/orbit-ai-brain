from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import orbit.meeting_intelligence_routes as meeting_intelligence_routes
from orbit.meeting_intelligence_repository import MeetingIntelligenceRepository


class FakeStore:
    def __init__(
        self,
        meetings: dict[str, dict],
        decisions: dict[str, list[dict]] | None = None,
        action_items: dict[str, list[dict]] | None = None,
        memories: dict[str, list[dict]] | None = None,
    ):
        self.meetings = meetings
        self.decisions = decisions or {}
        self.action_items = action_items or {}
        self.memories = memories or {}
        self.calls = {
            "get_meeting_by_id": 0,
            "get_decisions_by_meeting_id": 0,
            "get_action_items_by_meeting_id": 0,
            "get_memories_by_meeting_id": 0,
        }

    async def get_meeting_by_id(self, meeting_id: str):
        self.calls["get_meeting_by_id"] += 1
        return self.meetings.get(meeting_id)

    async def get_decisions_by_meeting_id(self, meeting_id: str):
        self.calls["get_decisions_by_meeting_id"] += 1
        return self.decisions.get(meeting_id, [])

    async def get_action_items_by_meeting_id(self, meeting_id: str):
        self.calls["get_action_items_by_meeting_id"] += 1
        return self.action_items.get(meeting_id, [])

    async def get_memories_by_meeting_id(self, meeting_id: str):
        self.calls["get_memories_by_meeting_id"] += 1
        return self.memories.get(meeting_id, [])


class MeetingIntelligenceEndpointTests(unittest.TestCase):
    def _get(self, meeting_id: str, repository):
        app = FastAPI()
        app.include_router(meeting_intelligence_routes.router)
        with patch.object(
            meeting_intelligence_routes,
            "build_meeting_intelligence_repository",
            return_value=repository,
        ):
            with TestClient(app) as client:
                return client.get(f"/meetings/{meeting_id}/intelligence")

    @staticmethod
    def _meeting_payload(
        meeting_id: str,
        *,
        status="processed",
        source_id="source-1",
        summary_short=None,
        summary_long=None,
    ):
        return {
            "id": meeting_id,
            "source_id": source_id,
            "status": status,
            "gmeet_url": "https://meet.google.com/abc-defg-hij",
            "summary_short": summary_short,
            "summary_long": summary_long,
            "started_at": None,
            "ended_at": None,
            "created_at": "2026-05-31T10:00:00+00:00",
            "updated_at": "2026-05-31T10:00:00+00:00",
        }

    def _assert_no_internal_payload_fields(self, payload: dict):
        forbidden = [
            "source_chunks",
            "extraction_runs",
            "audio",
            "audio_metadata",
            "capture_logs",
            "chrome",
            "deepgram",
            "capture",
            "source_chunks_count",
        ]
        for key in forbidden:
            self.assertNotIn(key, payload, f"Unexpected field {key} found in response.")

        for decision in payload["decisions"]:
            allowed_decision_keys = {
                "id",
                "title",
                "decision_text",
                "rationale",
                "owner_text",
                "confidence",
                "created_at",
            }
            self.assertEqual(set(decision.keys()), allowed_decision_keys)

        for action_item in payload["action_items"]:
            allowed_action_item_keys = {
                "id",
                "task",
                "owner_text",
                "due_date",
                "status",
                "confidence",
                "created_at",
            }
            self.assertEqual(set(action_item.keys()), allowed_action_item_keys)

        for memory in payload["memories"]:
            allowed_memory_keys = {
                "id",
                "memory_type",
                "content",
                "importance",
                "confidence",
                "created_at",
            }
            self.assertEqual(set(memory.keys()), allowed_memory_keys)

    def test_invalid_uuid(self):
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(meetings={}, decisions={}, action_items={}, memories={})
        )

        response = self._get("not-a-uuid", repository)
        data = response.json()

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            data,
            {
                "error": {
                    "code": "INVALID_MEETING_ID",
                    "message": "Meeting id must be a valid UUID.",
                }
            },
        )

    def test_invalid_uuid_does_not_lookup_meeting(self):
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={"b9b3f2f0-3fa8-4ed3-a5d7-0dc3f67c7b77": None},
            )
        )

        response = self._get("not-a-uuid", repository)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(repository.meeting_store.calls["get_meeting_by_id"], 0)

    def test_unknown_meeting(self):
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(meetings={})
        )

        response = self._get("b9b3f2f0-3fa8-4ed3-a5d7-0dc3f67c7b77", repository)
        data = response.json()

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            data,
            {
                "error": {
                    "code": "MEETING_NOT_FOUND",
                    "message": "Meeting not found.",
                }
            },
        )

    def test_non_processed_status_created_returns_empty_intelligence(self):
        meeting_id = "d7b0f2f0-3fa8-4ed3-a5d7-0dc3f67c7b77"
        meeting = self._meeting_payload(meeting_id, status="created")
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={meeting_id: meeting},
                decisions={"other": [{"id": "d1"}]},
                action_items={"other": [{"id": "a1"}]},
                memories={"other": [{"id": "m1"}]},
            )
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["meeting"]["status"], "created")
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["action_items"], [])
        self.assertEqual(payload["memories"], [])
        self.assertFalse(payload["meta"]["is_ready"])
        self.assertEqual(payload["meta"]["decision_count"], 0)
        self.assertEqual(payload["meta"]["action_item_count"], 0)
        self.assertEqual(payload["meta"]["memory_count"], 0)
        self.assertEqual(
            payload["meta"]["message"],
            "Meeting capture has been created but has not started yet.",
        )
        self.assertEqual(repository.meeting_store.calls["get_decisions_by_meeting_id"], 0)
        self.assertEqual(repository.meeting_store.calls["get_action_items_by_meeting_id"], 0)
        self.assertEqual(repository.meeting_store.calls["get_memories_by_meeting_id"], 0)
        self._assert_no_internal_payload_fields(payload)

    def test_non_processed_status_processing_returns_empty_intelligence(self):
        meeting_id = "c1a8d3f0-9f72-4f4a-bff8-7f39f2b5be42"
        meeting = self._meeting_payload(meeting_id, status="processing")
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(meetings={meeting_id: meeting})
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["meta"]["is_ready"], False)
        self.assertEqual(payload["meta"]["message"], "Meeting is still processing.")
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["action_items"], [])
        self.assertEqual(payload["memories"], [])

    def test_non_processed_status_failed_returns_empty_intelligence(self):
        meeting_id = "2fa5b2f0-4f18-4d8c-b2a8-b3d4cfd1e4de"
        meeting = self._meeting_payload(meeting_id, status="failed")
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(meetings={meeting_id: meeting})
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["meta"]["is_ready"], False)
        self.assertEqual(payload["meta"]["message"], "Meeting processing failed.")
        self.assertEqual(payload["decisions"], [])
        self.assertEqual(payload["action_items"], [])
        self.assertEqual(payload["memories"], [])

    def test_processed_meeting_returns_intelligence(self):
        meeting_id = "9f8f2f9c-4d89-4f3f-9a95-5f3f7e9f7a01"
        meeting = self._meeting_payload(meeting_id, status="processed")
        decisions = [
            {
                "id": "decision-1",
                "title": "Decision A",
                "decision_text": "Use feature flags.",
                "rationale": "Quality reasons.",
                "owner_text": "PM",
                "confidence": 0.91,
                "created_at": "2026-05-31T09:00:00+00:00",
            },
            {
                "id": "decision-2",
                "title": "Decision B",
                "decision_text": "Ship to staging.",
                "rationale": "Early feedback.",
                "owner_text": "Eng",
                "confidence": 0.75,
                "created_at": "2026-05-31T09:05:00+00:00",
            },
        ]
        action_items = [
            {
                "id": "item-1",
                "task": "Ship hotfix",
                "owner_text": None,
                "due_date": "2026-06-02",
                "status": "open",
                "confidence": 0.64,
                "created_at": "2026-05-31T09:10:00+00:00",
            },
            {
                "id": "item-2",
                "task": "Follow up users",
                "owner_text": "QA",
                "due_date": None,
                "status": "open",
                "confidence": 0.95,
                "created_at": "2026-05-31T09:20:00+00:00",
            },
        ]
        memories = [
            {
                "id": "memory-1",
                "memory_type": "risk",
                "content": "Potential latency issue on login.",
                "importance": "high",
                "confidence": 0.76,
                "created_at": "2026-05-31T09:30:00+00:00",
            },
            {
                "id": "memory-2",
                "memory_type": "decision",
                "content": "Feature freeze confirmed.",
                "importance": "medium",
                "confidence": 0.54,
                "created_at": "2026-05-31T09:35:00+00:00",
            },
            {
                "id": "memory-3",
                "memory_type": "open_question",
                "content": "Need input from design.",
                "importance": "low",
                "confidence": 0.4,
                "created_at": "2026-05-31T09:40:00+00:00",
            },
        ]
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={meeting_id: meeting},
                decisions={meeting_id: decisions},
                action_items={meeting_id: action_items},
                memories={meeting_id: memories},
            )
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertTrue(payload["meta"]["is_ready"])
        self.assertEqual(payload["meta"]["message"], "Meeting intelligence is ready.")
        self.assertEqual(payload["meta"]["decision_count"], 2)
        self.assertEqual(payload["meta"]["action_item_count"], 2)
        self.assertEqual(payload["meta"]["memory_count"], 3)
        self.assertEqual(payload["meeting"]["id"], meeting_id)
        self.assertEqual(payload["meeting"]["status"], "processed")
        self.assertEqual(len(payload["decisions"]), 2)
        self.assertEqual(len(payload["action_items"]), 2)
        self.assertEqual(len(payload["memories"]), 3)
        self._assert_no_internal_payload_fields(payload)

    def test_decision_sorting(self):
        meeting_id = "b2c4e2f0-5f88-4a9a-a4b2-4d2f8ea4d4f0"
        meeting = self._meeting_payload(meeting_id, status="processed")
        decisions = [
            {
                "id": "d2",
                "title": "Tied old",
                "decision_text": "Tie item older",
                "rationale": None,
                "owner_text": None,
                "confidence": 0.8,
                "created_at": "2026-05-31T09:20:00+00:00",
            },
            {
                "id": "d4",
                "title": "Null confidence",
                "decision_text": "No confidence",
                "rationale": None,
                "owner_text": None,
                "confidence": None,
                "created_at": "2026-05-30T09:00:00+00:00",
            },
            {
                "id": "d1",
                "title": "Tied new",
                "decision_text": "Tie item newer",
                "rationale": None,
                "owner_text": None,
                "confidence": 0.8,
                "created_at": "2026-05-31T09:10:00+00:00",
            },
            {
                "id": "d3",
                "title": "Low confidence",
                "decision_text": "Lower confidence",
                "rationale": None,
                "owner_text": None,
                "confidence": 0.2,
                "created_at": "2026-05-29T09:00:00+00:00",
            },
        ]
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={meeting_id: meeting},
                decisions={meeting_id: decisions},
            )
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in payload["decisions"]], ["d1", "d2", "d3", "d4"])

    def test_action_item_sorting(self):
        meeting_id = "c6a6f4f0-9e8c-4c2d-9a1e-11d9f2e4a111"
        meeting = self._meeting_payload(meeting_id, status="processed")
        action_items = [
            {
                "id": "a1",
                "task": "Open due in March",
                "owner_text": None,
                "due_date": "2026-06-01",
                "status": "open",
                "confidence": 0.4,
                "created_at": "2026-05-31T09:10:00+00:00",
            },
            {
                "id": "a2",
                "task": "Closed before due",
                "owner_text": "PM",
                "due_date": "2026-06-01",
                "status": "closed",
                "confidence": 0.3,
                "created_at": "2026-05-31T09:11:00+00:00",
            },
            {
                "id": "a3",
                "task": "Open same due, earlier",
                "owner_text": None,
                "due_date": "2026-06-01",
                "status": "open",
                "confidence": 0.7,
                "created_at": "2026-05-31T09:05:00+00:00",
            },
            {
                "id": "a4",
                "task": "Open tie older",
                "owner_text": None,
                "due_date": "2026-06-01",
                "status": "open",
                "confidence": 0.7,
                "created_at": "2026-05-31T09:01:00+00:00",
            },
            {
                "id": "a5",
                "task": "Open missing due",
                "owner_text": None,
                "due_date": None,
                "status": "open",
                "confidence": 0.9,
                "created_at": "2026-05-31T09:30:00+00:00",
            },
            {
                "id": "a6",
                "task": "Closed missing due",
                "owner_text": None,
                "due_date": None,
                "status": "done",
                "confidence": 0.95,
                "created_at": "2026-05-31T09:31:00+00:00",
            },
        ]
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={meeting_id: meeting},
                action_items={meeting_id: action_items},
            )
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in payload["action_items"]],
            ["a4", "a3", "a1", "a5", "a2", "a6"],
        )

    def test_memory_sorting(self):
        meeting_id = "d9b0e8f0-1c2d-4e8a-b2f5-2d7c8a3fbb02"
        meeting = self._meeting_payload(meeting_id, status="processed")
        memories = [
            {
                "id": "m1",
                "memory_type": "risk",
                "content": "Medium importance risk.",
                "importance": "medium",
                "confidence": 0.8,
                "created_at": "2026-05-31T09:10:00+00:00",
            },
            {
                "id": "m2",
                "memory_type": "update",
                "content": "Unknown importance value.",
                "importance": "mystery",
                "confidence": 0.99,
                "created_at": "2026-05-31T09:05:00+00:00",
            },
            {
                "id": "m3",
                "memory_type": "question",
                "content": "High importance note.",
                "importance": "high",
                "confidence": 0.2,
                "created_at": "2026-05-31T09:20:00+00:00",
            },
            {
                "id": "m4",
                "memory_type": "question",
                "content": "Old high importance note.",
                "importance": "high",
                "confidence": 0.2,
                "created_at": "2026-05-31T09:15:00+00:00",
            },
            {
                "id": "m5",
                "memory_type": "update",
                "content": "Low importance note.",
                "importance": "low",
                "confidence": 0.95,
                "created_at": "2026-05-31T09:25:00+00:00",
            },
        ]
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={meeting_id: meeting},
                memories={meeting_id: memories},
            )
        )

        response = self._get(meeting_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in payload["memories"]],
            ["m4", "m3", "m1", "m5", "m2"],
        )

    def test_meeting_isolation_between_meetings(self):
        meeting_a_id = "a6f8d1f0-4a3b-4f2a-b1f1-8d8d3d2caa12"
        meeting_b_id = "b8f1d8f0-5d7b-4f8d-8f22-2e9b1c4af9bf"
        meeting_a = self._meeting_payload(meeting_a_id, status="processed")
        meeting_b = self._meeting_payload(
            meeting_b_id,
            status="processed",
            source_id="source-2",
        )
        repository = MeetingIntelligenceRepository(
            meeting_store=FakeStore(
                meetings={
                    meeting_a_id: meeting_a,
                    meeting_b_id: meeting_b,
                },
                decisions={
                    meeting_a_id: [{"id": "decision-a"}],
                    meeting_b_id: [{"id": "decision-b"}],
                },
                action_items={
                    meeting_a_id: [{"id": "item-a"}],
                    meeting_b_id: [{"id": "item-b"}],
                },
                memories={
                    meeting_a_id: [{"id": "memory-a"}],
                    meeting_b_id: [{"id": "memory-b"}],
                },
            )
        )

        response = self._get(meeting_a_id, repository)
        payload = response.json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in payload["decisions"]], ["decision-a"])
        self.assertEqual([item["id"] for item in payload["action_items"]], ["item-a"])
        self.assertEqual([item["id"] for item in payload["memories"]], ["memory-a"])

    def test_summary_meta_behavior(self):
        cases = [
            (
                "55af7a00-1111-4b2a-8f00-0f2c7de00111",
                self._meeting_payload(
                    "55af7a00-1111-4b2a-8f00-0f2c7de00111",
                    status="processed",
                    summary_short="Short summary",
                    summary_long=None,
                ),
                True,
            ),
            (
                "55af7a00-2222-4b2a-8f11-0f2c7de00222",
                self._meeting_payload(
                    "55af7a00-2222-4b2a-8f11-0f2c7de00222",
                    status="processed",
                    summary_short=None,
                    summary_long="Long summary",
                ),
                True,
            ),
            (
                "55af7a00-3333-4b2a-8f22-0f2c7de00333",
                self._meeting_payload(
                    "55af7a00-3333-4b2a-8f22-0f2c7de00333",
                    status="processed",
                    summary_short="Short summary",
                    summary_long="Long summary",
                ),
                True,
            ),
            (
                "55af7a00-4444-4b2a-8f33-0f2c7de00444",
                self._meeting_payload(
                    "55af7a00-4444-4b2a-8f33-0f2c7de00444",
                    status="processed",
                    summary_short=None,
                    summary_long=None,
                ),
                False,
            ),
            (
                "55af7a00-5555-4b2a-8f44-0f2c7de00555",
                self._meeting_payload(
                    "55af7a00-5555-4b2a-8f44-0f2c7de00555",
                    status="processed",
                    summary_short="   ",
                    summary_long="  \t\n",
                ),
                False,
            ),
        ]

        for meeting_id, meeting, expected_has_summary in cases:
            repository = MeetingIntelligenceRepository(
                meeting_store=FakeStore(meetings={meeting_id: meeting})
            )
            response = self._get(meeting_id, repository)
            payload = response.json()
            self.assertEqual(response.status_code, 200)
            self.assertEqual(payload["meta"]["has_summary"], expected_has_summary)


if __name__ == "__main__":
    unittest.main()
