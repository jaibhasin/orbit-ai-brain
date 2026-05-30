from __future__ import annotations

import asyncio
import unittest

from orbit.meet_types import ChatMessage, MeetingState
from orbit.memory import MemoryAnswer, MemorySource
from orbit.transcript import TranscriptSegment
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


class FakeMeetingStore:
    def __init__(self):
        self.people = []
        self.sources = []
        self.meetings = []
        self.updates = []

    async def find_or_create_person_by_phone(self, phone, name=None):
        self.people.append((phone, name))
        return "person-1"

    async def create_source(
        self,
        source_type,
        *,
        url=None,
        title=None,
        raw_text=None,
        raw_payload=None,
    ):
        self.sources.append(
            {
                "source_type": source_type,
                "url": url,
                "title": title,
                "raw_text": raw_text,
                "raw_payload": raw_payload,
            }
        )
        return "source-1"

    async def create_meeting(
        self,
        gmeet_url,
        *,
        source_id,
        status,
        requested_by_person_id=None,
        summary_short=None,
        summary_long=None,
        started_at=None,
        ended_at=None,
    ):
        self.meetings.append(
            {
                "gmeet_url": gmeet_url,
                "source_id": source_id,
                "status": status,
                "requested_by_person_id": requested_by_person_id,
                "summary_short": summary_short,
                "summary_long": summary_long,
                "started_at": started_at,
                "ended_at": ended_at,
            }
        )
        return "meeting-1"

    async def update_meeting_status(self, meeting_id, status, **kwargs):
        self.updates.append({"meeting_id": meeting_id, "status": status, "fields": kwargs})


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


def build_service(memory=None, meeting_store=None):
    service = OrbitWhatsAppService.__new__(OrbitWhatsAppService)
    service.twilio_allowed_from = "whatsapp:+15551234567"
    service.model_name = "test-model"
    service.openai_client = FakeOpenAIClient()
    service.max_parallel_meetings = 3
    service.active_sessions = {}
    service.lock = asyncio.Lock()
    service.memory = memory or FakeMemory()
    service.meeting_store = meeting_store or FakeMeetingStore()
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
        service.dialogue_history = [
            type("Turn", (), {"inbound": "old", "reply": "old reply"})()
        ]

        async def fake_start(meet_links):
            return f"started {len(meet_links)}"

        service.start_meeting_sessions = fake_start

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "join https://meet.google.com/abc-defg-hij",
        )

        self.assertIn("started 1", xml)
        self.assertEqual(service.memory.questions, [])
        self.assertEqual(len(service.dialogue_history), 1)
        self.assertEqual(service.dialogue_history[0].inbound, "join https://meet.google.com/abc-defg-hij")

    async def test_start_single_meeting_session_persists_people_source_and_meeting(self):
        store = FakeMeetingStore()
        service = build_service(memory=FakeMemory(), meeting_store=store)
        result = await service.start_single_meeting_session(
            "https://meet.google.com/abc-defg-hij",
            from_number="whatsapp:+15551234567",
        )

        self.assertEqual(result["status"], "started")
        self.assertEqual(store.people, [("15551234567", None)])
        self.assertEqual(store.sources[0]["url"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(store.meetings[0]["status"], "joining")
        self.assertEqual(store.meetings[0]["requested_by_person_id"], "person-1")
        self.assertEqual(store.meetings[0]["gmeet_url"], "https://meet.google.com/abc-defg-hij")

    async def test_session_status_updates_persistent_meeting_status(self):
        store = FakeMeetingStore()
        service = build_service(meeting_store=store)
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
            meeting_id="meeting-1",
        )

        await service.handle_session_status(state, "starting_join", "opening")
        await service.handle_session_status(state, "joined", "joined")

        self.assertEqual(store.updates[0]["status"], "joining")
        self.assertEqual(store.updates[1]["status"], "live")

    async def test_session_finished_marks_meeting_processed(self):
        store = FakeMeetingStore()
        service = build_service(meeting_store=store)
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
            joined_at="2026-05-31T10:00:00",
            live_stt_requested=True,
            finished_at="2026-05-31T11:00:00",
        )
        state.captured_messages = [
            ChatMessage(
                fingerprint="fp",
                raw_text="Orbit\\nhello",
                normalized_text="hello",
                author="Orbit",
                timestamp_text="10:00",
            )
        ]
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
            meeting_id="meeting-1",
        )
        service.live_stt = FakeLiveSTT()

        async def fake_send_whatsapp_message(body):
            return None

        service.send_whatsapp_message = fake_send_whatsapp_message

        await service.handle_session_finished(state)

        self.assertEqual(store.updates[-1]["status"], "processed")
        fields = store.updates[-1]["fields"]
        self.assertEqual(fields["ended_at"], "2026-05-31T11:00:00")
        self.assertEqual(fields["started_at"], "2026-05-31T10:00:00")
        self.assertIn("chat messages captured", fields["summary_short"])

    async def test_new_resets_dialogue_without_stopping_meetings(self):
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
        service.dialogue_history = [
            type("Turn", (), {"inbound": "old", "reply": "old reply"})()
        ]

        xml = await service.handle_incoming_message("whatsapp:+15551234567", "/new")

        self.assertIn("context reset", xml)
        self.assertEqual(service.dialogue_history, [])
        self.assertIn(state.session_id, service.active_sessions)

    async def test_dialogue_history_is_bounded_and_truncated(self):
        service = build_service()

        for index in range(14):
            service.record_dialogue_turn(f"user-{index}", "x" * 2500)

        self.assertEqual(len(service.dialogue_history), 12)
        self.assertEqual(service.dialogue_history[0].inbound, "user-2")
        self.assertEqual(len(service.dialogue_history[-1].reply), 2000)

    async def test_status_lists_active_meeting(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
            status="joined",
            joined_at="2026-05-30T09:34:16Z",
            live_stt_requested=True,
            live_stt_available=True,
            live_stt_started=True,
        )
        state.live_transcript_segments = [
            TranscriptSegment(
                source_id="s1",
                raw_text="hello",
                clean_text="Hello.",
                memory_text="Hello.",
            )
        ]
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )

        xml = await service.handle_incoming_message("whatsapp:+15551234567", "status")

        self.assertIn("abc-defg-hij", xml)
        self.assertIn("transcript segments: 1", xml)

    async def test_multiple_meetings_ask_for_code_for_live_recall(self):
        service = build_service()
        for code in ["abc-defg-hij", "xyz-abcd-uvw"]:
            state = MeetingState(
                session_id=f"{code}-session",
                meet_url=f"https://meet.google.com/{code}",
                meeting_code=code,
                display_name="Orbit",
            )
            service.active_sessions[state.session_id] = ActiveMeeting(
                session_id=state.session_id,
                meet_url=state.meet_url,
                state=state,
            )

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what are people discussing?",
        )

        self.assertIn("Multiple meetings are active", xml)
        self.assertIn("abc-defg-hij", xml)
        self.assertIn("xyz-abcd-uvw", xml)

    async def test_live_recall_uses_selected_live_transcript(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
            live_stt_requested=True,
            live_stt_available=True,
            live_stt_started=True,
        )
        state.live_transcript_segments = [
            TranscriptSegment(
                source_id="s1",
                raw_text="pricing",
                clean_text="We are discussing pricing.",
                memory_text="We are discussing pricing.",
                speaker_name="Priya",
                start_ms=1000,
                end_ms=3000,
            ),
            TranscriptSegment(
                source_id="s2",
                raw_text="launch",
                clean_text="The launch date is Friday.",
                memory_text="The launch date is Friday.",
                speaker_name="Jai",
                start_ms=4000,
                end_ms=6000,
            ),
        ]
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "summarize the meeting",
        )

        self.assertIn("Answer mode: live transcript", xml)
        self.assertIn("Sources:", xml)
        call = service.openai_client.chat.completions.calls[0]
        self.assertIn("We are discussing pricing.", call["messages"][1]["content"])
        self.assertIn("The launch date is Friday.", call["messages"][1]["content"])
        self.assertEqual(service.memory.questions, [])

    async def test_live_recall_does_not_fall_back_to_memory_when_stt_is_sparse(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
            live_stt_requested=True,
            live_stt_available=True,
            live_stt_started=True,
        )
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
        )

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what are people discussing?",
        )

        self.assertIn("too little live transcript context", xml)
        self.assertEqual(service.memory.questions, [])

    async def test_stop_sets_state_and_waits_for_cleanup(self):
        service = build_service()
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )

        async def done():
            return None

        task = asyncio.create_task(done())
        service.active_sessions[state.session_id] = ActiveMeeting(
            session_id=state.session_id,
            meet_url=state.meet_url,
            state=state,
            task=task,
        )

        xml = await service.handle_incoming_message("whatsapp:+15551234567", "stop monitoring")

        self.assertTrue(state.stop_requested)
        self.assertIn("completed cleanup", xml)

    async def test_leave_in_normal_question_does_not_stop_meeting(self):
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

        xml = await service.handle_incoming_message(
            "whatsapp:+15551234567",
            "what is our parental leave policy?",
        )

        self.assertFalse(state.stop_requested)
        self.assertIn("Answer mode: memory-backed recall", xml)
        self.assertEqual(service.memory.questions, ["what is our parental leave policy?"])

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

    async def test_finished_meeting_reports_leave_reason(self):
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
            leave_reason="Orbit is the only participant left in the meeting.",
        )

        await service.handle_session_finished(state)

        self.assertEqual(
            whatsapp_updates,
            [
                "Orbit finished Meet abc-defg-hij. Captured 0 chat message(s). "
                "Orbit is the only participant left in the meeting."
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
