from __future__ import annotations

import asyncio
import unittest

from orbit.meet_types import ChatMessage, MeetingState
from orbit.memory import MemoryAnswer, MemorySource
from orbit.whatsapp_service import OrbitWhatsAppService


class FakeMemory:
    def __init__(self):
        self.recorded = []
        self.finalized = []
        self.questions = []

    async def record_meeting_chat(self, state, message):
        self.recorded.append((state, message))

    async def finalize_meeting(self, state):
        self.finalized.append(state)

    async def search_memory(self, query):
        return []

    async def answer_from_memory(self, question):
        self.questions.append(question)
        return MemoryAnswer(
            answer="The launch date discussed was Friday.",
            sources=[
                MemorySource(
                    label="Meet abc-defg-hij / Priya / 10:05",
                    source_type="meet_chat",
                    meeting_code="abc-defg-hij",
                    author="Priya",
                    timestamp_text="10:05",
                )
            ],
        )


def build_service():
    service = OrbitWhatsAppService.__new__(OrbitWhatsAppService)
    service.twilio_allowed_from = "whatsapp:+15551234567"
    service.max_parallel_meetings = 3
    service.active_sessions = {}
    service.lock = asyncio.Lock()
    service.memory = FakeMemory()
    return service


class WhatsAppMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_question_uses_memory_answer(self):
        service = build_service()

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what is the launch date?",
        )

        self.assertIn("The launch date discussed was Friday.", xml)
        self.assertIn("Sources:", xml)
        self.assertEqual(service.memory.questions, ["what is the launch date?"])

    async def test_meet_link_still_starts_session_path(self):
        service = build_service()

        async def fake_start(meet_links):
            return f"started {len(meet_links)}"

        service.start_meeting_sessions = fake_start

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "join https://meet.google.com/abc-defg-hij",
        )

        self.assertIn("started 1", xml)
        self.assertEqual(service.memory.questions, [])

    async def test_chat_messages_are_forwarded_to_memory(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )
        message = ChatMessage(
            fingerprint="fp-1",
            raw_text="Priya\\nlaunch is Friday",
            normalized_text="launch is Friday",
            author="Priya",
            timestamp_text="10:05",
        )

        await service.handle_chat_message(state, message, "poll")
        await service.handle_session_finished(state)

        self.assertEqual(service.memory.recorded, [(state, message)])
        self.assertEqual(service.memory.finalized, [state])


if __name__ == "__main__":
    unittest.main()
