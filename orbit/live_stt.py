from __future__ import annotations

import asyncio
from dataclasses import dataclass

from orbit.caption_attribution import CaptionSnippet, merge_caption_speakers
from orbit.core import log
from orbit.deepgram_live import (
    DeepgramLiveConfig,
    DeepgramLiveTranscriber,
    parse_deepgram_message,
)
from orbit.meet_types import MeetingState
from orbit.memory import MemoryService
from orbit.transcript_normalizer import normalize_transcript_segments


@dataclass
class LiveAudioFormat:
    encoding: str = "linear16"
    sample_rate: int = 16000
    channels: int = 1

    @classmethod
    def from_payload(cls, payload: dict) -> "LiveAudioFormat":
        return cls(
            encoding=str(payload.get("encoding") or "linear16"),
            sample_rate=int(payload.get("sample_rate") or payload.get("sampleRate") or 16000),
            channels=int(payload.get("channels") or 1),
        )


class LiveSTTSession:
    def __init__(
        self,
        *,
        state: MeetingState,
        memory: MemoryService,
        api_key: str,
        model: str,
        audio_format: LiveAudioFormat | None = None,
        transcriber_factory=None,
    ):
        self.state = state
        self.memory = memory
        self.api_key = api_key
        self.model = model
        self.audio_format = audio_format or LiveAudioFormat()
        self.transcriber_factory = transcriber_factory or self._default_transcriber_factory
        self.transcriber = None
        self.receive_task: asyncio.Task | None = None
        self.captions: list[CaptionSnippet] = []
        self.audio_chunks_received = 0
        self.final_segments_recorded = 0
        self.last_error: str | None = None
        self.closed = False
        self._finalize_timeout_s = 1.5

    async def start(self) -> None:
        if self.transcriber is not None:
            return

        self.transcriber = self.transcriber_factory(self.audio_format)
        assert self.transcriber is not None
        await self.transcriber.connect()
        self.receive_task = asyncio.create_task(self._receive_loop())
        log("Live STT Deepgram stream started.", self.state.session_id)

    async def send_audio(self, chunk: bytes) -> None:
        if self.closed:
            return
        if not chunk:
            return
        if self.transcriber is None:
            await self.start()
        assert self.transcriber is not None
        self.audio_chunks_received += 1
        await self.transcriber.send_audio(chunk)

    def add_captions(self, captions: list[CaptionSnippet]) -> None:
        if not captions:
            return
        self.captions.extend(captions)
        self.captions = self.captions[-100:]

    async def process_deepgram_message(self, message: str | bytes) -> None:
        raw_segments = parse_deepgram_message(
            message,
            source_id_prefix=f"{self.state.session_id}-live",
        )
        if not raw_segments:
            return

        attributed_segments = merge_caption_speakers(raw_segments, self.captions)
        normalized_segments = normalize_transcript_segments(
            self.state.meeting_code,
            attributed_segments,
        )
        if not normalized_segments:
            return

        await self.memory.record_transcript_segments(self.state, normalized_segments)
        self.final_segments_recorded += len(normalized_segments)

    async def close(self) -> None:
        self.closed = True
        if self.transcriber is not None:
            finish = getattr(self.transcriber, "finish", None)
            if finish is not None:
                await finish()
                if self.receive_task is not None:
                    try:
                        await asyncio.wait_for(self.receive_task, timeout=self._finalize_timeout_s)
                    except asyncio.TimeoutError:
                        pass
            await self.transcriber.close()
        if self.receive_task is not None:
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
        if self.audio_chunks_received == 0:
            log("Live STT stream closed without receiving audio chunks.", self.state.session_id)

    async def _receive_loop(self) -> None:
        try:
            assert self.transcriber is not None
            async for message in self.transcriber.receive():
                await self.process_deepgram_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.last_error = str(error)
            log(f"Live STT receive loop failed: {error}", self.state.session_id)

    def _default_transcriber_factory(self, audio_format: LiveAudioFormat):
        return DeepgramLiveTranscriber(
            api_key=self.api_key,
            config=DeepgramLiveConfig(
                model=self.model,
                encoding=audio_format.encoding,
                sample_rate=audio_format.sample_rate,
                channels=audio_format.channels,
            ),
        )


class LiveSTTManager:
    def __init__(
        self,
        *,
        memory: MemoryService,
        api_key: str | None,
        model: str = "nova-3",
        transcriber_factory=None,
    ):
        self.memory = memory
        self.api_key = api_key
        self.model = model
        self.transcriber_factory = transcriber_factory
        self.sessions: dict[str, LiveSTTSession] = {}
        self.pending_captions: dict[str, list[CaptionSnippet]] = {}
        self.lock = asyncio.Lock()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def get_or_create(
        self,
        state: MeetingState,
        audio_format: LiveAudioFormat | None = None,
    ) -> LiveSTTSession:
        if not self.api_key:
            raise RuntimeError("Missing DEEPGRAM_API_KEY in .env or environment.")

        async with self.lock:
            session = self.sessions.get(state.session_id)
            if session is None:
                session = LiveSTTSession(
                    state=state,
                    memory=self.memory,
                    api_key=self.api_key,
                    model=self.model,
                    audio_format=audio_format,
                    transcriber_factory=self.transcriber_factory,
                )
                pending_captions = self.pending_captions.pop(state.session_id, [])
                session.add_captions(pending_captions)
                self.sessions[state.session_id] = session
            return session

    async def add_captions(self, state: MeetingState, captions: list[CaptionSnippet]) -> None:
        async with self.lock:
            session = self.sessions.get(state.session_id)
            if session is not None:
                session.add_captions(captions)
                return
            buffered = self.pending_captions.setdefault(state.session_id, [])
            buffered.extend(captions)
            self.pending_captions[state.session_id] = buffered[-100:]

    async def stop(self, session_id: str) -> None:
        async with self.lock:
            session = self.sessions.pop(session_id, None)
            self.pending_captions.pop(session_id, None)
        if session is not None:
            await session.close()
