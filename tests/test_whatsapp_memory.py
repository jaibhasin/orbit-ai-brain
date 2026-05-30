from __future__ import annotations

import asyncio
import unittest

from orbit.meet_types import ChatMessage, MeetingState
from orbit.memory import MemoryAnswer, MemorySource
from orbit.whatsapp_service import ActiveMeeting, OrbitWhatsAppService


class FakeMemory:
    def __init__(self, answer=None):
        self.recorded = []
        self.transcripts = []
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

    async def record_transcript_segments(self, state, segments):
        self.transcripts.append((state, segments))

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


class FakeLiveSTT:
    def __init__(self):
        self.available = True
        self.sessions = []
        self.stopped = []

    async def add_captions(self, state, captions):
        return None

    async def get_or_create(self, state, audio_format=None):
        session = FakeLiveSTTSession()
        self.sessions.append((state, audio_format, session))
        return session

    async def stop(self, session_id):
        self.stopped.append(session_id)
        return None


class FakeLiveSTTSession:
    def __init__(self):
        self.audio_chunks = []

    async def send_audio(self, chunk):
        self.audio_chunks.append(chunk)


class FakeWebSocket:
    def __init__(self, messages, query_params=None):
        self.messages = list(messages)
        self.query_params = query_params or {}
        self.accepted = False
        self.sent_json = []
        self.closed = None

    async def accept(self):
        self.accepted = True

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)

    async def receive(self):
        if not self.messages:
            raise RuntimeError("no more messages")
        return self.messages.pop(0)

    async def send_json(self, payload):
        self.sent_json.append(payload)


def build_service(memory=None):
    service = OrbitWhatsAppService.__new__(OrbitWhatsAppService)
    service.twilio_allowed_from = "whatsapp:+15551234567"
    service.model_name = "test-model"
    service.openai_client = FakeOpenAIClient()
    service.max_parallel_meetings = 3
    service.active_sessions = {}
    service.lock = asyncio.Lock()
    service.memory = memory or FakeMemory()
    service.live_stt = FakeLiveSTT()
    return service


class WhatsAppMemoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_question_uses_memory_answer(self):
        service = build_service()

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what is the launch date?",
        )

        self.assertIn("Answer mode: memory-backed recall", xml)
        self.assertIn("The launch date discussed was Friday.", xml)
        self.assertIn("Sources:", xml)
        self.assertEqual(service.memory.questions, ["what is the launch date?"])
        self.assertEqual(service.openai_client.chat.completions.calls, [])

    async def test_normal_question_falls_back_to_general_answer_when_memory_is_empty(self):
        memory = FakeMemory(
            MemoryAnswer(
                "I do not have enough company memory yet to answer that.",
                mode="insufficient_memory",
            )
        )
        service = build_service(memory)

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what is product market fit?",
        )

        self.assertIn("Answer mode: general fallback", xml)
        self.assertIn("This answer is not based on stored company memory.", xml)
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

    async def test_finished_meeting_reports_missing_live_audio(self):
        service = build_service()
        whatsapp_updates = []

        async def fake_send_whatsapp_message(body):
            whatsapp_updates.append(body)

        service.send_whatsapp_message = fake_send_whatsapp_message
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
            joined_at="2026-05-30T09:34:16Z",
            live_stt_requested=True,
        )

        await service.handle_session_finished(state)

        self.assertEqual(
            whatsapp_updates,
            [
                "Orbit finished Meet abc-defg-hij. Captured 0 chat message(s). "
                "Live audio transcription did not start because no audio chunk was received."
            ],
        )

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

    async def test_extension_audio_stream_forwards_chunks_to_live_stt(self):
        service = build_service()
        whatsapp_updates = []

        async def fake_send_whatsapp_message(body):
            whatsapp_updates.append(body)

        service.send_whatsapp_message = fake_send_whatsapp_message
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )
        websocket = FakeWebSocket(
            [
                {"text": '{"type":"start","encoding":"linear16","sample_rate":16000,"channels":1}'},
                {"bytes": b"pcm"},
                {"text": '{"type":"stop"}'},
            ]
        )

        await service.handle_audio_stream(websocket, state.session_id)

        self.assertTrue(websocket.accepted)
        self.assertEqual(websocket.sent_json, [{"type": "ready"}])
        fake_session = service.live_stt.sessions[0][2]
        self.assertEqual(fake_session.audio_chunks, [b"pcm"])
        self.assertEqual(service.live_stt.stopped, [state.session_id])
        self.assertTrue(state.live_stt_started)
        self.assertIsNotNone(state.live_stt_audio_confirmed_at)
        self.assertEqual(
            state.live_stt_status_detail,
            "Deepgram stream connected and first audio chunk forwarded.",
        )
        self.assertEqual(
            whatsapp_updates,
            ["Orbit confirmed live audio transcription for Meet abc-defg-hij."],
        )

    async def test_extension_audio_stream_is_not_confirmed_before_first_chunk(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )
        websocket = FakeWebSocket(
            [
                {"text": '{"type":"start","encoding":"linear16","sample_rate":16000,"channels":1}'},
                {"text": '{"type":"stop"}'},
            ]
        )

        await service.handle_audio_stream(websocket, state.session_id)

        self.assertFalse(state.live_stt_started)
        self.assertIsNone(state.live_stt_audio_confirmed_at)
        self.assertEqual(
            state.live_stt_status_detail,
            "Extension audio WebSocket connected. Waiting for the first audio chunk.",
        )

    async def test_extension_empty_audio_chunk_does_not_confirm_live_stt(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )
        websocket = FakeWebSocket(
            [
                {"text": '{"type":"start","encoding":"linear16","sample_rate":16000,"channels":1}'},
                {"bytes": b""},
                {"text": '{"type":"stop"}'},
            ]
        )

        await service.handle_audio_stream(websocket, state.session_id)

        self.assertFalse(state.live_stt_started)
        fake_session = service.live_stt.sessions[0][2]
        self.assertEqual(fake_session.audio_chunks, [])

    async def test_extension_missing_unknown_session_closes_websocket(self):
        service = build_service()
        websocket = FakeWebSocket([])

        await service.handle_audio_stream(websocket, "missing")

        self.assertEqual(websocket.closed[0], 4404)

    async def test_extension_audio_stream_rejects_bad_session_token(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
            live_stt_audio_token="expected-token",
        )
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )
        websocket = FakeWebSocket([], query_params={"token": "wrong-token"})

        await service.handle_audio_stream(websocket, state.session_id)

        self.assertEqual(websocket.closed[0], 4403)
        self.assertFalse(websocket.accepted)


if __name__ == "__main__":
    unittest.main()
