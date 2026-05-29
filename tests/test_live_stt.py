from __future__ import annotations

import asyncio
import json
import unittest

from orbit.caption_attribution import CaptionSnippet
from orbit.live_stt import LiveAudioFormat, LiveSTTManager, LiveSTTSession
from orbit.meet_types import MeetingState


class FakeMemory:
    def __init__(self):
        self.transcripts = []

    async def record_meeting_chat(self, state, message):
        return None

    async def record_transcript_segments(self, state, segments):
        self.transcripts.append((state, segments))

    async def finalize_meeting(self, state):
        return None

    async def search_memory(self, query):
        return []

    async def answer_from_memory(self, question):
        raise AssertionError("not used")


class FakeTranscriber:
    def __init__(self, messages=None, connect_error=None):
        self.messages = messages or []
        self.connect_error = connect_error
        self.audio_chunks = []
        self.closed = False

    async def connect(self):
        if self.connect_error:
            raise self.connect_error
        return self

    async def send_audio(self, chunk):
        self.audio_chunks.append(chunk)

    async def receive(self):
        for message in self.messages:
            yield message
        while not self.closed:
            await asyncio.sleep(0.01)

    async def close(self):
        self.closed = True


def build_state():
    return MeetingState(
        session_id="session-1",
        meet_url="https://meet.google.com/abc-defg-hij",
        meeting_code="abc-defg-hij",
        display_name="Orbit",
    )


def final_deepgram_message(text="we should launch on friday"):
    return json.dumps(
        {
            "type": "Results",
            "is_final": True,
            "speech_final": True,
            "start": 0,
            "duration": 2,
            "channel": {
                "alternatives": [
                    {
                        "transcript": text,
                        "confidence": 0.9,
                        "words": [
                            {"word": "we", "start": 0, "end": 0.1},
                            {"word": "friday", "start": 1.5, "end": 2.0},
                        ],
                    }
                ]
            },
        }
    )


class LiveSTTTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_deepgram_message_is_normalized_and_stored(self):
        memory = FakeMemory()
        session = LiveSTTSession(
            state=build_state(),
            memory=memory,
            api_key="dg-key",
            model="nova-3",
        )

        await session.process_deepgram_message(final_deepgram_message())

        self.assertEqual(len(memory.transcripts), 1)
        segments = memory.transcripts[0][1]
        self.assertEqual(segments[0].clean_text, "We should launch on friday.")
        self.assertIn("Meet abc-defg-hij transcript", segments[0].memory_text)

    async def test_caption_names_are_best_effort_enrichment(self):
        memory = FakeMemory()
        session = LiveSTTSession(
            state=build_state(),
            memory=memory,
            api_key="dg-key",
            model="nova-3",
        )
        session.add_captions([CaptionSnippet(speaker_name="Jai", text="we should launch on friday")])

        await session.process_deepgram_message(final_deepgram_message())

        segment = memory.transcripts[0][1][0]
        self.assertEqual(segment.speaker_name, "Jai")
        self.assertEqual(segment.speaker_source, "google_meet_captions")
        self.assertIn("Jai", segment.memory_text)

    async def test_audio_chunk_starts_transcriber_and_is_forwarded(self):
        fake = FakeTranscriber()
        session = LiveSTTSession(
            state=build_state(),
            memory=FakeMemory(),
            api_key="dg-key",
            model="nova-3",
            transcriber_factory=lambda audio_format: fake,
        )

        await session.send_audio(b"pcm")
        await session.close()

        self.assertEqual(fake.audio_chunks, [b"pcm"])
        self.assertEqual(session.audio_chunks_received, 1)

    async def test_deepgram_auth_failure_surfaces(self):
        fake = FakeTranscriber(connect_error=RuntimeError("auth failed"))
        session = LiveSTTSession(
            state=build_state(),
            memory=FakeMemory(),
            api_key="bad-key",
            model="nova-3",
            transcriber_factory=lambda audio_format: fake,
        )

        with self.assertRaises(RuntimeError):
            await session.send_audio(b"pcm")

    async def test_manager_requires_deepgram_key(self):
        manager = LiveSTTManager(memory=FakeMemory(), api_key=None)

        with self.assertRaises(RuntimeError):
            await manager.get_or_create(build_state(), LiveAudioFormat())


if __name__ == "__main__":
    unittest.main()
