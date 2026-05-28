from __future__ import annotations

import asyncio
import unittest

from orbit.meet_types import ChatMessage, MeetingState
from orbit.memory import MemoryAnswer, MemorySource
from orbit.whatsapp_service import OrbitWhatsAppService


class FakeMemory:
    def __init__(self, answer=None):
        self.recorded = []
        self.finalized = []
        self.questions = []
        self.answer = answer or MemoryAnswer(
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

    async def record_meeting_chat(self, state, message):
        self.recorded.append((state, message))

    async def finalize_meeting(self, state):
        self.finalized.append(state)

    async def search_memory(self, query):
        return []

    async def answer_from_memory(self, question):
        self.questions.append(question)
        return self.answer


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChoice:
    def __init__(self, content):
        self.message = FakeMessage(content)


class FakeResponse:
    def __init__(self, content):
        self.choices = [FakeChoice(content)]


class FakeCompletions:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse("General answer from Orbit.")


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeOpenAIClient:
    def __init__(self):
        self.chat = FakeChat()


def build_service(memory=None):
    service = OrbitWhatsAppService.__new__(OrbitWhatsAppService)
    service.twilio_allowed_from = "whatsapp:+15551234567"
    service.model_name = "test-model"
    service.openai_client = FakeOpenAIClient()
    service.max_parallel_meetings = 3
    service.active_sessions = {}
    service.lock = asyncio.Lock()
    service.memory = memory or FakeMemory()
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
        self.assertEqual(service.openai_client.chat.completions.calls, [])

    async def test_normal_question_falls_back_to_general_answer_when_memory_is_empty(self):
        memory = FakeMemory(
            MemoryAnswer("I do not have enough company memory yet to answer that.")
        )
        service = build_service(memory)

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what is product market fit?",
        )

        self.assertIn("General answer from Orbit.", xml)
        self.assertEqual(memory.questions, ["what is product market fit?"])
        self.assertEqual(len(service.openai_client.chat.completions.calls), 1)

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

    async def test_meet_chat_mention_gets_model_reply(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )
        previous = ChatMessage(
            fingerprint="fp-1",
            raw_text="Jai\\nwe are discussing pricing",
            normalized_text="we are discussing pricing",
            author="Jai",
            timestamp_text="10:04",
        )
        mention = ChatMessage(
            fingerprint="fp-2",
            raw_text="Jai\\n@orbit are you there?",
            normalized_text="@orbit are you there?",
            author="Jai",
            timestamp_text="10:05",
        )
        state.captured_messages = [previous, mention]

        reply = await service.handle_orbit_mention(state, mention)

        self.assertEqual(reply, "General answer from Orbit.")
        call = service.openai_client.chat.completions.calls[0]
        self.assertIn("are you there?", call["messages"][1]["content"])
        self.assertIn("we are discussing pricing", call["messages"][1]["content"])


if __name__ == "__main__":
    unittest.main()
