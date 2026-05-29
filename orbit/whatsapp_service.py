from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from orbit.core import (
    env_int,
    extract_meeting_code,
    load_dotenv,
    log,
    now_iso,
)
from orbit.meet import build_default_session_config, run_meeting_session
from orbit.meet_types import (
    ChatMessage,
    MeetingSessionCallbacks,
    MeetingSessionConfig,
    MeetingState,
    build_meeting_state,
)
from orbit.memory import MemoryAnswer, MemorySource, build_memory_service
from orbit.live_stt import LiveAudioFormat, LiveSTTManager
from openai import AsyncOpenAI
from twilio.rest import Client
from twilio.twiml.messaging_response import MessagingResponse


MEET_LINK_PATTERN = re.compile(r"https://meet\.google\.com/[^\s<>\"]+", re.IGNORECASE)
QNA_TRIGGER_PATTERN = re.compile(r"^\s*(?:@orbit\b|orbit\s*:)\s*", re.IGNORECASE)
ANSWER_MODE_LABELS = {
    "memory_answer": "memory-backed recall",
    "insufficient_memory": "insufficient company memory",
    "general_fallback": "general fallback",
}


@dataclass
class ActiveMeeting:
    session_id: str
    meet_url: str
    state: MeetingState
    task: asyncio.Task | None = None
    created_at: str = field(default_factory=now_iso)


def clean_meet_link(raw_url):
    return raw_url.rstrip(").,!?:;]>\"'")


def extract_meet_links(text):
    urls = []
    for match in MEET_LINK_PATTERN.findall(text or ""):
        clean_url = clean_meet_link(match)
        if clean_url not in urls:
            urls.append(clean_url)
    return urls


def is_qna_message(text):
    return bool(QNA_TRIGGER_PATTERN.match(text or ""))


def strip_qna_trigger(text):
    return QNA_TRIGGER_PATTERN.sub("", text or "", count=1).strip()


def format_twiml(message_text):
    response = MessagingResponse()
    if message_text:
        response.message(message_text)
    return str(response)


class OrbitWhatsAppService:
    def __init__(self):
        load_dotenv()

        self.twilio_account_sid = self._require_env("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = self._require_env("TWILIO_AUTH_TOKEN")
        self.twilio_whatsapp_from = self._require_env("TWILIO_WHATSAPP_FROM")
        self.twilio_allowed_from = self._require_env("TWILIO_ALLOWED_FROM", self._read_env("TWILIO_WHATSAPP_TO"))
        self.openai_api_key = self._require_env("OPENAI_API_KEY")
        self.model_name = self._require_env("OPENAI_MODEL", "gpt-5.4-mini")
        self.max_parallel_meetings = env_int("ORBIT_MAX_PARALLEL_MEETINGS", 3)

        self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)
        self.openai_client = AsyncOpenAI(api_key=self.openai_api_key)
        self.memory = build_memory_service(self.openai_client, self.model_name)
        self.live_stt = LiveSTTManager(
            memory=self.memory,
            api_key=self._read_env("DEEPGRAM_API_KEY"),
            model=self._read_env("DEEPGRAM_LIVE_MODEL", "nova-3"),
        )
        self.active_sessions: dict[str, ActiveMeeting] = {}
        self.lock = asyncio.Lock()

    def _require_env(self, name, default=None):
        value = self._read_env(name, default)
        if not value:
            raise RuntimeError(f"Missing {name} in .env or environment.")
        return value

    @staticmethod
    def _read_env(name, default=None):
        import os

        return os.environ.get(name, default)

    async def handle_incoming_message(self, from_number, body):
        if from_number != self.twilio_allowed_from:
            return format_twiml("")

        body = (body or "").strip()
        if not body:
            return format_twiml(
                "Send a Google Meet link to start Orbit, or send @orbit followed by a question."
            )

        meet_links = extract_meet_links(body)
        if meet_links:
            reply = await self.start_meeting_sessions(meet_links)
            return format_twiml(reply)

        if is_qna_message(body):
            question = strip_qna_trigger(body)
            if not question:
                return format_twiml("Send @orbit followed by your question.")
            answer = await self.answer_question(question)
            return format_twiml(answer)

        answer = await self.answer_general_question(body)
        return format_twiml(answer)

    async def start_meeting_sessions(self, meet_links):
        started = []
        duplicates = []
        rejected = []

        for meet_url in meet_links:
            start_result = await self.start_single_meeting_session(meet_url)
            if start_result["status"] == "started":
                started.append(start_result["meeting_code"])
            elif start_result["status"] == "duplicate":
                duplicates.append(start_result["meeting_code"])
            else:
                rejected.append(start_result["meeting_code"])

        if started and not duplicates and not rejected:
            codes = ", ".join(started)
            return f"Starting Orbit for Google Meet: {codes}. I will send status updates here."

        parts = []
        if started:
            parts.append(f"Started: {', '.join(started)}.")
        if duplicates:
            parts.append(f"Already active: {', '.join(duplicates)}.")
        if rejected:
            parts.append(
                f"At capacity ({self.max_parallel_meetings} meetings), so I skipped: {', '.join(rejected)}."
            )
        return " ".join(parts)

    async def start_single_meeting_session(self, meet_url):
        meeting_code = extract_meeting_code(meet_url)

        async with self.lock:
            if any(active.state.meeting_code == meeting_code for active in self.active_sessions.values()):
                return {"status": "duplicate", "meeting_code": meeting_code}

            if len(self.active_sessions) >= self.max_parallel_meetings:
                return {"status": "capacity", "meeting_code": meeting_code}

            session_id = self.build_session_id(meeting_code)
            config = self.build_session_config(meet_url, session_id)
            state = build_meeting_state(config)
            active = ActiveMeeting(
                session_id=session_id,
                meet_url=meet_url,
                state=state,
                created_at=now_iso(),
            )
            self.active_sessions[session_id] = active
            active.task = asyncio.create_task(self._run_session(active, config))

        return {"status": "started", "meeting_code": meeting_code}

    async def _run_session(self, active, config):
        callbacks = MeetingSessionCallbacks(
            on_status=self.handle_session_status,
            on_chat_message=self.handle_chat_message,
            on_captions=self.handle_captions,
            on_orbit_mention=self.handle_orbit_mention,
            on_finished=self.handle_session_finished,
        )
        try:
            await run_meeting_session(config, callbacks=callbacks, state=active.state)
        finally:
            async with self.lock:
                self.active_sessions.pop(active.session_id, None)

    def build_session_id(self, meeting_code):
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        return f"{meeting_code}-{timestamp}"

    def build_session_config(self, meet_url, session_id):
        default_config = build_default_session_config(meet_url, session_id=session_id)
        return MeetingSessionConfig(
            session_id=session_id,
            meet_url=meet_url,
            display_name=default_config.display_name,
            wait_after_join_ms=default_config.wait_after_join_ms,
            max_steps=default_config.max_steps,
            model_name=default_config.model_name,
            live_stt_enabled=default_config.live_stt_enabled,
            audio_stream_ws_url=default_config.audio_stream_ws_url,
        )

    async def handle_session_status(self, state, status, detail):
        if status == "starting_join":
            await self.send_whatsapp_message(
                f"Orbit is starting the join flow for Meet {state.meeting_code}."
            )
            return

        if status == "waiting_for_host":
            await self.send_whatsapp_message(
                f"Orbit is waiting for host approval for Meet {state.meeting_code}."
            )
            return

        if status == "joined":
            await self.send_whatsapp_message(
                f"Orbit joined Meet {state.meeting_code} and is monitoring the meeting chat."
            )
            return

        if status == "live_stt_capture_requested":
            await self.send_whatsapp_message(
                f"Orbit requested live audio transcription for Meet {state.meeting_code}."
            )
            return

        if status == "live_stt_unavailable":
            await self.send_whatsapp_message(
                f"Orbit could not start live audio transcription for Meet {state.meeting_code}: {detail}"
            )
            return

        if status == "chat_monitor_unavailable":
            await self.send_whatsapp_message(
                f"Orbit joined Meet {state.meeting_code}, but it could not open the Meet chat panel."
            )
            return

        if status == "join_denied":
            await self.send_whatsapp_message(
                f"Google Meet denied Orbit's join request for {state.meeting_code}."
            )
            return

        if status == "join_blocked":
            await self.send_whatsapp_message(
                f"Google Meet blocked Orbit from joining {state.meeting_code}."
            )
            return

        if status == "join_unconfirmed":
            await self.send_whatsapp_message(
                f"Orbit could not confirm whether it joined Meet {state.meeting_code}."
            )
            return

        if status == "no_active_page":
            await self.send_whatsapp_message(
                f"Orbit lost the browser page while handling Meet {state.meeting_code}."
            )
            return

        if status == "error":
            await self.send_whatsapp_message(
                f"Orbit hit an error while handling Meet {state.meeting_code}: {detail}"
            )

    async def handle_chat_message(self, state: MeetingState, message: ChatMessage, source: str):
        try:
            await self.memory.record_meeting_chat(state, message)
        except Exception as error:
            log(f"Memory write failed for Meet {state.meeting_code}: {error}", state.session_id)

    async def handle_captions(self, state: MeetingState, captions):
        try:
            await self.live_stt.add_captions(state, captions)
        except Exception as error:
            log(f"Caption attribution buffer failed for Meet {state.meeting_code}: {error}", state.session_id)

    async def handle_orbit_mention(self, state: MeetingState, message: ChatMessage):
        question = strip_qna_trigger(message.normalized_text)
        if not question:
            return "I’m here. Ask me a question after @orbit."

        recent_messages = [
            chat_message
            for chat_message in state.captured_messages[-15:]
            if chat_message.fingerprint != message.fingerprint
        ]
        context = "\n".join(
            f"{chat_message.author or 'unknown'}"
            f"{f' [{chat_message.timestamp_text}]' if chat_message.timestamp_text else ''}: "
            f"{chat_message.normalized_text}"
            for chat_message in recent_messages
        )

        prompt = (
            "Answer the in-meeting chat question briefly. Use the meeting chat context when relevant. "
            "If the chat context is not needed or is insufficient, answer as a general assistant. "
            "Keep the reply short enough for Google Meet chat.\n\n"
            f"Question:\n{question}\n\n"
            f"Recent meeting chat:\n{context or '(no prior chat captured)'}"
        )

        try:
            response = await self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Orbit inside a Google Meet chat. Reply concisely, helpfully, "
                            "and do not claim access to audio or transcript content."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as error:
            log(f"Meet chat mention answer failed: {error}", state.session_id)
            return "I’m here, but I could not generate an answer right now."

        content = response.choices[0].message.content if response.choices else ""
        return (content or "").strip() or "I’m here."

    async def handle_session_finished(self, state):
        await self.live_stt.stop(state.session_id)
        try:
            await self.memory.finalize_meeting(state)
        except Exception as error:
            log(f"Memory indexing failed for Meet {state.meeting_code}: {error}", state.session_id)

        if state.joined_at:
            await self.send_whatsapp_message(
                f"Orbit finished Meet {state.meeting_code}. Captured {len(state.captured_messages)} chat message(s)."
            )
            return

        if state.status == "waiting_for_host":
            await self.send_whatsapp_message(
                f"Orbit stopped waiting for Meet {state.meeting_code} without being admitted."
            )

    async def send_whatsapp_message(self, body):
        log(f"Sending WhatsApp update: {body}")
        try:
            await asyncio.to_thread(
                self.twilio_client.messages.create,
                body=body,
                from_=self.twilio_whatsapp_from,
                to=self.twilio_allowed_from,
            )
        except Exception as error:
            log(f"WhatsApp send failed: {error}")

    async def answer_question(self, question):
        context_sections = await self.build_meeting_context()
        if not context_sections:
            return "I do not have enough live Meet chat context yet to answer that."

        prompt = (
            "Answer the WhatsApp question using only the meeting chat context below. "
            "If the context is insufficient, say so. If multiple meetings are relevant, name the meeting codes.\n\n"
            f"Question:\n{question}\n\n"
            f"Meeting chat context:\n{context_sections}"
        )

        try:
            response = await self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Orbit. Answer briefly and only from the supplied Google Meet chat context. "
                            "Do not invent meeting details or claim audio/transcript access."
                        ),
                    },
                    {
                        "role": "user",
                        "content": prompt,
                    },
                ],
            )
        except Exception as error:
            log(f"WhatsApp Q&A failed: {error}")
            return "I could not answer that right now because the Q&A model call failed."

        content = response.choices[0].message.content if response.choices else ""
        return (content or "").strip() or "I do not have enough meeting context to answer that."

    async def answer_general_question(self, question):
        try:
            memory_answer = await self.memory.answer_from_memory(question)
        except Exception as error:
            log(f"Memory Q&A failed: {error}")
            memory_answer = MemoryAnswer(
                "Stored company memory was unavailable for this question.",
                mode="insufficient_memory",
            )

        answer = memory_answer.answer.strip()
        sources = self.format_memory_sources(memory_answer.sources)
        if memory_answer.mode == "memory_answer" and answer:
            return self.format_answer_mode_message("memory_answer", answer, sources)

        general_answer = await self.answer_general_model_question(question)
        fallback_intro = (
            answer
            or "Stored company memory did not have enough grounded context for this question."
        )
        fallback_body = (
            f"{fallback_intro}\n\n"
            "This answer is not based on stored company memory.\n\n"
            f"{general_answer}"
        )
        return self.format_answer_mode_message("general_fallback", fallback_body)

    async def answer_general_model_question(self, question):
        try:
            response = await self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Orbit, a concise WhatsApp assistant. Answer general world, business, "
                            "and technology questions directly. If the question appears to ask about company "
                            "or meeting memory and no memory was available, say that you do not have stored "
                            "company context yet, then answer generally if useful."
                        ),
                    },
                    {"role": "user", "content": question},
                ],
            )
        except Exception as error:
            log(f"General WhatsApp answer failed: {error}")
            return "I could not answer that right now because the general model call failed."

        content = response.choices[0].message.content if response.choices else ""
        return (content or "").strip() or "I could not generate an answer for that."

    def format_memory_sources(self, sources: list[MemorySource]):
        labels = []
        seen = set()
        for source in sources:
            label = source.label
            if not label or label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= 3:
                break
        return "; ".join(labels)

    def format_answer_mode_message(self, mode: str, answer: str, sources: str = ""):
        label = ANSWER_MODE_LABELS.get(mode, mode.replace("_", " "))
        sections = [f"Answer mode: {label}", answer.strip()]
        if sources:
            sections.append(f"Sources: {sources}")
        return "\n\n".join(section for section in sections if section)

    async def build_meeting_context(self):
        async with self.lock:
            active_states = [active.state for active in self.active_sessions.values()]

        sections = []
        for state in active_states:
            if not state.captured_messages:
                continue

            recent_messages = state.captured_messages[-15:]
            lines = []
            for message in recent_messages:
                author = message.author or "unknown"
                timestamp = f" [{message.timestamp_text}]" if message.timestamp_text else ""
                lines.append(f"{author}{timestamp}: {message.normalized_text}")

            sections.append(
                f"Meet {state.meeting_code} ({state.status}):\n" + "\n".join(lines)
            )

        return "\n\n".join(sections)

    async def handle_audio_stream(self, websocket, session_id: str):
        active = self.active_sessions.get(session_id)
        if active is None:
            await websocket.close(code=4404, reason="Unknown Orbit meeting session.")
            return
        if not self.live_stt.available:
            await websocket.close(code=4401, reason="Missing DEEPGRAM_API_KEY.")
            return

        await websocket.accept()
        session = None
        try:
            while True:
                message = await websocket.receive()
                if message.get("bytes") is not None:
                    if session is None:
                        session = await self.live_stt.get_or_create(active.state)
                        active.state.live_stt_started = True
                    await session.send_audio(message["bytes"])
                    continue

                raw_text = message.get("text")
                if raw_text is None:
                    continue

                payload = json.loads(raw_text)
                message_type = payload.get("type")
                if message_type in {"start", "config"}:
                    audio_format = LiveAudioFormat.from_payload(payload)
                    session = await self.live_stt.get_or_create(active.state, audio_format)
                    active.state.live_stt_started = True
                    active.state.live_stt_status_detail = "Extension audio WebSocket connected."
                    await websocket.send_json({"type": "ready"})
                elif message_type == "stop":
                    break
        except Exception as error:
            log(f"Live audio WebSocket failed for Meet {active.state.meeting_code}: {error}", session_id)
        finally:
            await self.live_stt.stop(session_id)
