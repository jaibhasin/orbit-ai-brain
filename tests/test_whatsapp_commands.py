from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from orbit.agent.whatsapp.command_handler import (
    JOIN_HELP_MESSAGE,
    UNREGISTERED_PHONE_MESSAGE,
    handle_whatsapp_command,
)
from orbit.agent.whatsapp.command_parser import (
    HELP_TEXT as PARSER_HELP_TEXT,
    ParsedWhatsAppCommand,
    is_valid_google_meet_url,
    parse_whatsapp_command,
)


VALID_PERSON_ID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
VALID_MEETING_ID = "123e4567-e89b-12d3-a456-426614174000"


class WhatsAppCommandParserTests(unittest.TestCase):
    def test_parse_help_and_recent(self):
        self.assertEqual(parse_whatsapp_command("help").name, "help")
        self.assertEqual(parse_whatsapp_command("recent").name, "recent")

    def test_parse_open_actions(self):
        self.assertEqual(parse_whatsapp_command("open actions").name, "open_actions")
        self.assertNotEqual(parse_whatsapp_command("open something").name, "open_actions")

    def test_parse_join(self):
        parsed = parse_whatsapp_command("join https://meet.google.com/abc-defg-hij")
        self.assertEqual(parsed, ParsedWhatsAppCommand(name="join", argument="https://meet.google.com/abc-defg-hij"))
        self.assertTrue(is_valid_google_meet_url(parsed.argument))

    def test_parse_unknown(self):
        self.assertEqual(parse_whatsapp_command("random message").name, "unknown")

    def test_parse_trimmed_meet_link(self):
        parsed = parse_whatsapp_command("join https://meet.google.com/abc-defg-hij)")
        self.assertEqual(parsed.argument, "https://meet.google.com/abc-defg-hij")


class WhatsAppCommandHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unregistered_phone_receives_safe_message(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=None):
            reply = await handle_whatsapp_command("whatsapp:+15551230000", "help")

        self.assertEqual(reply, UNREGISTERED_PHONE_MESSAGE)

    async def test_help_command_returns_help_text(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            reply = await handle_whatsapp_command("whatsapp:+15551230000", "help")

        self.assertEqual(reply, PARSER_HELP_TEXT)

    async def test_handler_does_not_call_send_whatsapp_reply(self):
        with patch(
            "orbit.agent.whatsapp.command_handler.send_whatsapp_reply",
            new_callable=AsyncMock,
            create=True,
        ) as send_reply:
            with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
                with patch("orbit.agent.whatsapp.command_handler.get_recent_meetings", new_callable=AsyncMock, return_value=[]):
                    reply = await handle_whatsapp_command("whatsapp:+15551230000", "recent")

        self.assertEqual(reply, "Recent meetings:\n\nNo meetings found.")
        send_reply.assert_not_awaited()

    async def test_join_without_url_returns_usage(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch("orbit.agent.whatsapp.command_handler.request_meeting_capture", new_callable=AsyncMock) as create_capture:
                reply = await handle_whatsapp_command("whatsapp:+15551230000", "join")

        self.assertEqual(reply, JOIN_HELP_MESSAGE)
        create_capture.assert_not_awaited()

    async def test_join_valid_schedules_capture(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.request_meeting_capture",
                new_callable=AsyncMock,
                return_value={
                    "meeting_id": VALID_MEETING_ID,
                    "status": "created",
                    "message": "Meeting capture created.",
                },
            ) as request_capture:
                reply = await handle_whatsapp_command(
                    "whatsapp:+15551230000",
                    "join https://meet.google.com/abc-defg-hij",
                )

        request_capture.assert_awaited_once_with(
            gmeet_url="https://meet.google.com/abc-defg-hij",
            requested_by_person_id=VALID_PERSON_ID,
        )
        self.assertIn("Meeting capture created.", reply)
        self.assertIn(f"Meeting ID: {VALID_MEETING_ID}", reply)
        self.assertIn("Status: created", reply)

    async def test_status_formats_started_and_ended(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.get_meeting_capture_status",
                new_callable=AsyncMock,
                return_value={
                    "meeting_id": VALID_MEETING_ID,
                    "status": "processed",
                    "started_at": None,
                    "ended_at": "2026-05-31T11:00:00+00:00",
                },
            ):
                reply = await handle_whatsapp_command(
                    "whatsapp:+15551230000",
                    f"status {VALID_MEETING_ID}",
                )

        self.assertIn("Meeting status: processed", reply)
        self.assertIn("Started: unknown", reply)
        self.assertIn("Ended: 2026-05-31T11:00:00+00:00", reply)

    async def test_summary_when_ready_includes_counts(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.get_meeting_intelligence",
                new_callable=AsyncMock,
                return_value={
                    "meeting": {
                        "id": VALID_MEETING_ID,
                        "summary_short": "Planning completed.",
                    },
                    "meta": {
                        "is_ready": True,
                        "decision_count": 2,
                        "action_item_count": 1,
                        "memory_count": 3,
                    },
                },
            ):
                reply = await handle_whatsapp_command(
                    "whatsapp:+15551230000",
                    f"summary {VALID_MEETING_ID}",
                )

        self.assertEqual(reply, "Summary:\nPlanning completed.\n\nDecisions: 2\nAction items: 1\nMemories: 3")

    async def test_summary_when_not_ready_returns_status_and_message(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.get_meeting_intelligence",
                new_callable=AsyncMock,
                return_value={
                    "meeting": {"status": "created"},
                    "meta": {"is_ready": False, "message": "Meeting capture has not started."},
                },
            ):
                reply = await handle_whatsapp_command(
                    "whatsapp:+15551230000",
                    f"summary {VALID_MEETING_ID}",
                )

        self.assertIn("Meeting status: created", reply)
        self.assertIn("Message: Meeting capture has not started.", reply)

    async def test_decisions_when_not_ready(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.get_meeting_intelligence",
                new_callable=AsyncMock,
                return_value={
                    "meeting": {"status": "created"},
                    "meta": {"is_ready": False, "message": "not ready"},
                },
            ):
                reply = await handle_whatsapp_command(
                    "whatsapp:+15551230000",
                    f"decisions {VALID_MEETING_ID}",
                )

        self.assertEqual(reply, "Meeting intelligence is not ready.")

    async def test_actions_lists_open_action_items(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.get_meeting_intelligence",
                new_callable=AsyncMock,
                return_value={
                    "meeting": {"status": "processed"},
                    "meta": {"is_ready": True},
                    "action_items": [
                        {"task": "Follow up", "owner_text": "Amit", "due_date": None},
                    ],
                },
            ):
                reply = await handle_whatsapp_command(
                    "whatsapp:+15551230000",
                    f"actions {VALID_MEETING_ID}",
                )

        self.assertIn("1. Follow up", reply)
        self.assertIn("Owner: Amit", reply)
        self.assertIn("Due: unknown", reply)

    async def test_open_actions_includes_task_and_fallbacks(self):
        with patch("orbit.agent.whatsapp.command_handler.resolve_person_id_by_whatsapp_phone", return_value=VALID_PERSON_ID):
            with patch(
                "orbit.agent.whatsapp.command_handler.get_open_action_items",
                new_callable=AsyncMock,
                return_value=[
                    {"task": "Finalize roadmap", "owner_text": None, "due_date": "2026-06-01"},
                ],
            ):
                reply = await handle_whatsapp_command("whatsapp:+15551230000", "open actions")

        self.assertIn("1. Finalize roadmap", reply)
        self.assertIn("Owner: unknown", reply)
        self.assertIn("Due: 2026-06-01", reply)
