from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime

from orbit.core import (
    env_int,
    configure_dependency_logging,
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
from orbit.meeting_store import build_meeting_store
from orbit.memory import MemoryAnswer, MemorySource, build_memory_service
from orbit.live_stt import LiveAudioFormat, LiveSTTManager
from orbit.transcript import TranscriptSegment, format_timestamp_ms
from openai import AsyncOpenAI
try:
    from twilio.rest import Client
    from twilio.twiml.messaging_response import MessagingResponse
except Exception:
    class Client:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("Twilio SDK is not installed.")

    class MessagingResponse:
        def __init__(self, *args, **kwargs):
            self._value = ""

        def message(self, body=None):
            if body is not None:
                self._value = body
            return self._value

        def __str__(self):
            return str(self._value)


MEET_LINK_PATTERN = re.compile(r"https://meet\.google\.com/[^\s<>\"]+", re.IGNORECASE)
MEETING_CODE_PATTERN = re.compile(r"\b[a-z]{3}-[a-z]{4}-[a-z]{3}\b", re.IGNORECASE)
QNA_TRIGGER_PATTERN = re.compile(r"^\s*(?:@orbit\b|orbit\s*:)\s*", re.IGNORECASE)
STOP_COMMAND_PATTERN = re.compile(
    r"^\s*(?:please\s+)?(?:"
    r"leave(?:\s+(?:meet(?:ing)?\s+)?)?|"
    r"stop\s+(?:recording|monitoring|orbit)|"
    r"end\s+(?:meeting|recording)"
    r")(?:\s+[a-z]{3}-[a-z]{4}-[a-z]{3})?\s*[.!?]?\s*$",
    re.IGNORECASE,
)
MAX_DIALOGUE_TURNS = 12
MAX_DIALOGUE_MESSAGE_CHARS = 2000
LIVE_RECALL_MAX_SEGMENTS = 30
LIVE_RECAP_MAX_SEGMENTS = 40
LIVE_RECALL_MAX_PROMPT_CHARS = 8000
STOP_TIMEOUT_SECONDS = 10
MEETING_EXTRACTION_PROMPT_VERSION = "meeting-extractor-v1"
MEETING_EXTRACTION_RUN_TYPE = "full_meeting_extraction"
MEETING_EXTRACT_PROMPT = """You are extracting structured company memory from a meeting transcript.

Return only valid JSON. Do not include markdown.

Extract:
- short summary
- long summary
- decisions
- action items
- risks
- open questions
- durable memories

Rules:
- Do not invent information.
- If there are no decisions, return an empty decisions array.
- Do not treat suggestions as decisions.
- Only extract action items if there is a clear task.
- If owner is unclear, use null or empty string.
- Use confidence from 0 to 1.
- Keep output concise.

Transcript:
{transcript}
"""
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
    source_id: str | None = None
    meeting_id: str | None = None
    task: asyncio.Task | None = None
    created_at: str = field(default_factory=now_iso)


@dataclass
class DialogueTurn:
    inbound: str
    reply: str


@dataclass
class WhatsAppIntent:
    kind: str
    text: str
    meeting_code: str | None = None


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


def extract_meeting_codes(text):
    return [match.lower() for match in MEETING_CODE_PATTERN.findall(text or "")]


def format_twiml(message_text):
    response = MessagingResponse()
    if message_text:
        response.message(message_text)
    return str(response)


class OrbitWhatsAppService:
    def __init__(self):
        load_dotenv()
        configure_dependency_logging()

        self.twilio_account_sid = self._require_env("TWILIO_ACCOUNT_SID")
        self.twilio_auth_token = self._require_env("TWILIO_AUTH_TOKEN")
        self.twilio_whatsapp_from = self._require_env("TWILIO_WHATSAPP_FROM")
        self.twilio_allowed_from = self._require_env("TWILIO_ALLOWED_FROM", self._read_env("TWILIO_WHATSAPP_TO"))
        self.openai_api_key = self._require_env("OPENAI_API_KEY")
        self.model_name = self._require_env("OPENAI_MODEL", "gpt-5.4-mini")
        self.max_parallel_meetings = env_int("ORBIT_MAX_PARALLEL_MEETINGS", 3)

        self.twilio_client = Client(self.twilio_account_sid, self.twilio_auth_token)
        self.openai_client = AsyncOpenAI(api_key=self.openai_api_key)
        self.meeting_store = build_meeting_store(self._read_env("DATABASE_URL"))
        self.memory = build_memory_service(self.openai_client, self.model_name)
        self.live_stt = LiveSTTManager(
            memory=self.memory,
            api_key=self._read_env("DEEPGRAM_API_KEY"),
            model=self._read_env("DEEPGRAM_LIVE_MODEL", "nova-3"),
        )
        self.active_sessions: dict[str, ActiveMeeting] = {}
        # TODO: make dialogue history durable before running multiple workers or relying on restart continuity.
        self.dialogue_history: list[DialogueTurn] = []
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

    async def handle_incoming_message(self, from_number, body, profile_name=None):
        if from_number != self.twilio_allowed_from:
            return format_twiml("")

        self._ensure_runtime_fields()
        body = (body or "").strip()
        if not body:
            return self._reply_without_history(
                "Send a Google Meet link to start Orbit, or send @orbit followed by a question."
            )

        meet_links = extract_meet_links(body)
        if meet_links:
            self.reset_dialogue_history()
            reply = await self.start_meeting_sessions(
                meet_links,
                from_number=from_number,
                profile_name=profile_name,
            )
            return self._reply_and_record(body, reply)

        intent = self.parse_whatsapp_intent(body)
        if intent.kind == "new":
            self.reset_dialogue_history()
            return self._reply_without_history("WhatsApp context reset. Active meetings are still running.")

        if intent.kind in {"status", "stop", "live_recall"}:
            reply = await self.execute_whatsapp_intent(intent)
            return self._reply_and_record(body, reply)

        if is_qna_message(body):
            question = strip_qna_trigger(body)
            if not question:
                return self._reply_and_record(body, "Send @orbit followed by your question.")
            answer = await self.answer_question(question)
            return self._reply_and_record(body, answer)

        answer = await self.answer_general_question(body)
        return self._reply_and_record(body, answer)

    def _ensure_runtime_fields(self):
        if not hasattr(self, "dialogue_history"):
            self.dialogue_history = []

    def _reply_without_history(self, reply):
        return format_twiml(reply)

    def _reply_and_record(self, inbound, reply):
        self.record_dialogue_turn(inbound, reply)
        return format_twiml(reply)

    def reset_dialogue_history(self):
        self.dialogue_history = []

    def record_dialogue_turn(self, inbound, reply):
        self._ensure_runtime_fields()
        self.dialogue_history.append(
            DialogueTurn(
                inbound=self.truncate_dialogue_message(inbound),
                reply=self.truncate_dialogue_message(reply),
            )
        )
        self.dialogue_history = self.dialogue_history[-MAX_DIALOGUE_TURNS:]

    def truncate_dialogue_message(self, text):
        text = text or ""
        if len(text) <= MAX_DIALOGUE_MESSAGE_CHARS:
            return text
        return text[:MAX_DIALOGUE_MESSAGE_CHARS]

    def format_dialogue_history(self):
        self._ensure_runtime_fields()
        if not self.dialogue_history:
            return ""
        lines = []
        for turn in self.dialogue_history:
            lines.append(f"User: {turn.inbound}")
            lines.append(f"Orbit: {turn.reply}")
        return "\n".join(lines)

    def parse_whatsapp_intent(self, body):
        text = strip_qna_trigger(body).strip() if is_qna_message(body) else body.strip()
        lowered = text.lower()
        codes = extract_meeting_codes(text)
        meeting_code = codes[0] if codes else None

        if lowered == "/new":
            return WhatsAppIntent("new", text, meeting_code)

        status_patterns = (
            "status",
            "list meetings",
            "active meetings",
            "what meetings are active",
            "which meetings are active",
            "current meeting",
            "meeting status",
        )
        if any(pattern in lowered for pattern in status_patterns):
            return WhatsAppIntent("status", text, meeting_code)

        if STOP_COMMAND_PATTERN.match(text):
            return WhatsAppIntent("stop", text, meeting_code)

        live_patterns = (
            "what are people discussing",
            "what is being discussed",
            "what are they discussing",
            "summarize the meeting",
            "summarise the meeting",
            "recap the meeting",
            "meeting recap",
            "what happened",
            "what happened in the meeting",
            "what's happening in the meeting",
            "what is happening in the meeting",
            "live meeting",
        )
        if any(pattern in lowered for pattern in live_patterns):
            return WhatsAppIntent("live_recall", text, meeting_code)

        return WhatsAppIntent("fallback", text, meeting_code)

    async def execute_whatsapp_intent(self, intent):
        if intent.kind == "status":
            return await self.format_active_meeting_status(intent.meeting_code)
        if intent.kind == "stop":
            return await self.stop_active_meeting(intent.meeting_code)
        if intent.kind == "live_recall":
            return await self.answer_live_recall(intent.text, intent.meeting_code)
        return await self.answer_general_question(intent.text)

    async def start_meeting_sessions(self, meet_links, from_number=None, profile_name=None):
        started = []
        duplicates = []
        rejected = []

        for meet_url in meet_links:
            start_result = await self.start_single_meeting_session(
                meet_url,
                from_number=from_number,
                profile_name=profile_name,
            )
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

    async def start_single_meeting_session(self, meet_url, from_number=None, profile_name=None):
        meeting_code = extract_meeting_code(meet_url)

        async with self.lock:
            if any(active.state.meeting_code == meeting_code for active in self.active_sessions.values()):
                return {"status": "duplicate", "meeting_code": meeting_code}

            if len(self.active_sessions) >= self.max_parallel_meetings:
                return {"status": "capacity", "meeting_code": meeting_code}

            meeting_record = await self._create_meeting_record(meet_url, from_number, profile_name)
            meeting_id, source_id = meeting_record
            session_id = self.build_session_id(meeting_code)
            config = self.build_session_config(meet_url, session_id)
            state = build_meeting_state(config)
            active = ActiveMeeting(
                session_id=session_id,
                meet_url=meet_url,
                state=state,
                meeting_id=meeting_id,
                source_id=source_id,
                created_at=now_iso(),
            )
            self.active_sessions[session_id] = active
            active.task = asyncio.create_task(self._run_session(active, config))

            return {"status": "started", "meeting_code": meeting_code}

    async def _create_meeting_record(self, meet_url, from_number, profile_name=None):
        if not from_number:
            return None, None

        store = getattr(self, "meeting_store", None)
        if not store:
            return None, None

        try:
            person_id = await store.find_or_create_person_by_phone(
                self._normalize_person_phone(from_number),
                name=profile_name,
            )
            source_id = await store.create_source(
                "gmeet",
                url=meet_url,
            )
            meeting_id = await store.create_meeting(
                gmeet_url=meet_url,
                source_id=source_id,
                status="joining",
                requested_by_person_id=person_id,
            )
            return meeting_id, source_id
        except Exception as error:
            log(f"Failed to create meeting persistence row for {meet_url}: {error}", level="error")
            return None, None

    @staticmethod
    def _normalize_person_phone(raw_phone):
        return (raw_phone or "").replace("whatsapp:", "").strip() or None

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
            audio_stream_token=default_config.audio_stream_token,
        )

    async def snapshot_active_meetings(self):
        async with self.lock:
            return list(self.active_sessions.values())

    async def resolve_active_meeting(self, meeting_code=None):
        active_meetings = await self.snapshot_active_meetings()
        if not active_meetings:
            return None, "Orbit is not monitoring an active meeting."

        if meeting_code:
            for active in active_meetings:
                if active.state.meeting_code.lower() == meeting_code.lower():
                    return active, None
            codes = ", ".join(active.state.meeting_code for active in active_meetings)
            return None, f"Meet {meeting_code} is not active. Active meeting(s): {codes}."

        if len(active_meetings) == 1:
            return active_meetings[0], None

        codes = ", ".join(active.state.meeting_code for active in active_meetings)
        return None, f"Multiple meetings are active. Send the meeting code to choose one: {codes}."

    async def format_active_meeting_status(self, meeting_code=None):
        active, error = await self.resolve_active_meeting(meeting_code)
        if error and (meeting_code or "Multiple" in error or "not monitoring" in error):
            if not meeting_code and "Multiple" in error:
                active_meetings = await self.snapshot_active_meetings()
            else:
                active_meetings = []
            if not active_meetings:
                return error
        else:
            active_meetings = [active] if active is not None else await self.snapshot_active_meetings()

        if not active_meetings:
            return "Orbit is not monitoring an active meeting."

        lines = ["Active Orbit meeting(s):"]
        for active_meeting in active_meetings:
            state = active_meeting.state
            stt_state = "started" if state.live_stt_started else "not started"
            if state.live_stt_requested and not state.live_stt_available:
                stt_state = "unavailable"
            elif not state.live_stt_requested:
                stt_state = "not requested"
            joined = state.joined_at or "not joined yet"
            lines.append(
                f"- {state.meeting_code}: {state.status}; joined: {joined}; "
                f"chat messages: {len(state.captured_messages)}; "
                f"STT: {stt_state}; transcript segments: {len(state.live_transcript_segments)}"
            )
        return "\n".join(lines)

    async def stop_active_meeting(self, meeting_code=None):
        active, error = await self.resolve_active_meeting(meeting_code)
        if error:
            return error

        state = active.state
        state.stop_requested = True
        state.stop_reason = "Orbit was asked from WhatsApp to stop monitoring this meeting."

        if active.task is None:
            async with self.lock:
                self.active_sessions.pop(active.session_id, None)
            return f"Orbit marked Meet {state.meeting_code} as stopped."

        try:
            await asyncio.wait_for(asyncio.shield(active.task), timeout=STOP_TIMEOUT_SECONDS)
            return f"Orbit stopped monitoring Meet {state.meeting_code} and completed cleanup."
        except asyncio.TimeoutError:
            active.task.cancel()
            try:
                await active.task
            except asyncio.CancelledError:
                pass
            return f"Orbit forced cleanup for Meet {state.meeting_code} after waiting {STOP_TIMEOUT_SECONDS} seconds."
        except Exception as error:
            return f"Orbit tried to stop Meet {state.meeting_code}, but cleanup failed: {error}"

    async def answer_live_recall(self, question, meeting_code=None):
        active, error = await self.resolve_active_meeting(meeting_code)
        if error:
            return error

        state = active.state
        if state.live_stt_requested and not state.live_stt_available:
            detail = f" {state.live_stt_status_detail}" if state.live_stt_status_detail else ""
            return f"Live transcription is unavailable for Meet {state.meeting_code}.{detail}"
        if not state.live_stt_requested:
            return f"Live transcription has not been requested for Meet {state.meeting_code}."
        if not state.live_stt_started:
            return f"Live transcription has not started for Meet {state.meeting_code} yet. Wait until Orbit confirms the first audio chunk."
        if len(state.live_transcript_segments) < 2:
            return f"Orbit has too little live transcript context for Meet {state.meeting_code} yet. Ask again after more discussion is captured."

        broad = self.is_broad_live_recap(question)
        segments = self.select_live_segments(state.live_transcript_segments, question, broad=broad)
        if not segments:
            return f"Orbit does not have enough relevant live transcript context for Meet {state.meeting_code} yet."

        context = self.format_live_segments_for_prompt(state.meeting_code, segments)
        prompt = (
            "Answer the WhatsApp question using only the live transcript excerpts below. "
            "If the excerpts are insufficient, say so. Keep the answer concise and cite sources inline "
            "using the provided source labels.\n\n"
            f"Question:\n{question}\n\n"
            f"Live transcript excerpts:\n{context}"
        )

        try:
            response = await self.openai_client.chat.completions.create(
                model=self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You are Orbit answering from live Google Meet transcript excerpts only. "
                            "Do not use historical company memory or invent uncited meeting details."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as error:
            log(f"Live recall answer failed: {error}", state.session_id, level="error")
            return "I could not answer from the live transcript right now because the model call failed."

        content = response.choices[0].message.content if response.choices else ""
        answer = (content or "").strip()
        sources = self.format_live_source_summary(state.meeting_code, segments)
        if not answer:
            answer = f"Orbit does not have enough live transcript context for Meet {state.meeting_code} yet."
        return self.format_answer_mode_message("live transcript", answer, sources)

    def is_broad_live_recap(self, question):
        lowered = question.lower()
        return any(
            phrase in lowered
            for phrase in (
                "summarize",
                "summarise",
                "recap",
                "what happened",
                "what are people discussing",
                "what is being discussed",
                "what are they discussing",
            )
        )

    def select_live_segments(self, segments, question, broad=False):
        if broad:
            return self.limit_segments_for_prompt(segments[-LIVE_RECAP_MAX_SEGMENTS:])

        query_terms = {
            term
            for term in re.findall(r"[a-z0-9]+", question.lower())
            if len(term) > 2
            and term
            not in {
                "the",
                "and",
                "are",
                "what",
                "who",
                "why",
                "how",
                "meeting",
                "live",
                "orbit",
            }
        }
        scored: list[tuple[float, int, TranscriptSegment]] = []
        for index, segment in enumerate(segments):
            segment_terms = set(re.findall(r"[a-z0-9]+", segment.clean_text.lower()))
            score = float(len(query_terms & segment_terms))
            score += index / max(len(segments), 1)
            if score > 0:
                scored.append((score, index, segment))
        selected = [item[2] for item in sorted(scored, key=lambda item: item[0], reverse=True)]
        if not selected:
            selected = segments[-LIVE_RECALL_MAX_SEGMENTS:]
        selected = sorted(selected[:LIVE_RECALL_MAX_SEGMENTS], key=lambda segment: segments.index(segment))
        return self.limit_segments_for_prompt(selected)

    def limit_segments_for_prompt(self, segments):
        total_chars = 0
        selected: list[TranscriptSegment] = []
        for segment in segments:
            segment_chars = len(segment.clean_text)
            if selected and total_chars + segment_chars > LIVE_RECALL_MAX_PROMPT_CHARS:
                break
            total_chars += segment_chars
            selected.append(segment)
        return selected

    def format_live_segments_for_prompt(self, meeting_code, segments):
        lines = []
        for segment in segments:
            label = self.format_live_source_label(meeting_code, segment)
            lines.append(f"[{label}] {segment.clean_text}")
        return "\n".join(lines)

    def format_live_source_summary(self, meeting_code, segments):
        labels = []
        seen = set()
        for segment in segments:
            label = self.format_live_source_label(meeting_code, segment)
            if label in seen:
                continue
            seen.add(label)
            labels.append(label)
            if len(labels) >= 3:
                break
        return "; ".join(labels)

    def format_live_source_label(self, meeting_code, segment: TranscriptSegment):
        parts = [f"Meet {meeting_code}"]
        speaker = segment.speaker_name or segment.speaker_label
        if speaker:
            parts.append(speaker)
        timestamp = self.format_segment_time_range(segment)
        if timestamp:
            parts.append(timestamp)
        return " / ".join(parts)

    def format_segment_time_range(self, segment: TranscriptSegment):
        start = format_timestamp_ms(segment.start_ms)
        end = format_timestamp_ms(segment.end_ms)
        if start and end:
            return f"{start}-{end}"
        return start or end or ""

    async def handle_session_status(self, state, status, detail):
        await self._update_persistent_meeting_status(state, status)

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
                f"Orbit requested live audio transcription for Meet {state.meeting_code}. "
                "Waiting for the first audio chunk."
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

    def _persistent_meeting_status(self, status, state):
        if status in {"joined", "live_stt_capture_requested", "chat_monitor_unavailable", "live_stt_unavailable"}:
            return "live"
        if status in {
            "starting_join",
            "waiting_for_host",
            "join_denied",
            "join_blocked",
            "join_unconfirmed",
            "no_active_page",
            "error",
        }:
            return "joining"
        if state.joined_at:
            return "live"
        return None

    async def _update_persistent_meeting_status(self, state, status):
        active = self.active_sessions.get(state.session_id)
        if not active or not active.meeting_id:
            return
        meeting_status = self._persistent_meeting_status(status, state)
        if not meeting_status:
            return

        store = getattr(self, "meeting_store", None)
        if not store:
            return

        started_at = state.joined_at if meeting_status == "live" else None
        try:
            await store.update_meeting_status(
                active.meeting_id,
                meeting_status,
                started_at=started_at,
            )
        except Exception as error:
            log(
                f"Failed to update persistent meeting status for {active.meeting_id}: {error}",
                state.session_id,
                level="error",
            )

    async def handle_chat_message(self, state: MeetingState, message: ChatMessage, source: str):
        try:
            await self.memory.record_meeting_chat(state, message)
        except Exception as error:
            log(f"Memory write failed for Meet {state.meeting_code}: {error}", state.session_id, level="error")

    async def handle_captions(self, state: MeetingState, captions):
        try:
            await self.live_stt.add_captions(state, captions)
        except Exception as error:
            log(f"Caption attribution buffer failed for Meet {state.meeting_code}: {error}", state.session_id, level="error")

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
            log(f"Meet chat mention answer failed: {error}", state.session_id, level="error")
            return "I’m here, but I could not generate an answer right now."

        content = response.choices[0].message.content if response.choices else ""
        return (content or "").strip() or "I’m here."

    async def runMeetingExtraction(self, payload: dict):
        if not isinstance(payload, dict):
            raise TypeError("runMeetingExtraction expects a payload dict.")

        return await self.run_meeting_extraction(
            meeting_id=payload.get("meetingId") or payload.get("meeting_id"),
            source_id=payload.get("sourceId") or payload.get("source_id"),
            run_type=payload.get("runType") or payload.get("run_type", MEETING_EXTRACTION_RUN_TYPE),
            model=payload.get("model") or self.model_name,
            prompt_version=payload.get("promptVersion") or payload.get("prompt_version", MEETING_EXTRACTION_PROMPT_VERSION),
            summary_short=payload.get("summary_short"),
            summary_long=payload.get("summary_long"),
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
            skip_status_updates=payload.get("skip_status_updates", False),
        )

    async def run_meeting_extraction(
        self,
        meeting_id: str,
        source_id: str,
        *,
        run_type: str = MEETING_EXTRACTION_RUN_TYPE,
        model: str | None = None,
        prompt_version: str = MEETING_EXTRACTION_PROMPT_VERSION,
        summary_short: str | None = None,
        summary_long: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
        skip_status_updates: bool = False,
    ) -> dict:
        if not meeting_id:
            raise ValueError("meetingId is required for extraction.")
        if not source_id:
            raise ValueError("sourceId is required for extraction.")

        store = getattr(self, "meeting_store", None)
        if not store:
            raise RuntimeError("Meeting store is not configured.")

        if not skip_status_updates:
            await store.update_meeting_status(
                meeting_id,
                "processing",
                ended_at=ended_at,
                started_at=started_at,
                summary_short=summary_short,
                summary_long=summary_long,
            )

        try:
            chunks = await store.get_source_chunks_by_source_id(source_id) or []
            transcript = self._build_transcript_text(chunks)
            if not transcript:
                output_json = self._empty_extraction_output()
                extraction_run_id = await store.create_extraction_run(
                    source_id=source_id,
                    meeting_id=meeting_id,
                    run_type=run_type,
                    model=model or self.model_name,
                    prompt_version=prompt_version,
                    output_json=output_json,
                    status="success",
                    error=None,
                )
                if not skip_status_updates:
                    await store.update_meeting_status(
                        meeting_id,
                        "processed",
                        ended_at=ended_at,
                        started_at=started_at,
                        summary_short=summary_short,
                        summary_long=summary_long,
                    )
                return {
                    "extraction_run_id": extraction_run_id,
                    "status": "success",
                    "output_json": output_json,
                }

            extraction_prompt = MEETING_EXTRACT_PROMPT.format(transcript=transcript)
            response = await self.openai_client.chat.completions.create(
                model=model or self.model_name,
                messages=[
                    {
                        "role": "system",
                        "content": MEETING_EXTRACT_PROMPT.split("Transcript:")[0].strip(),
                    },
                    {"role": "user", "content": extraction_prompt},
                ],
            )
            raw_output = (response.choices[0].message.content or "").strip() if response.choices else ""
            output_json = self._parse_extraction_json(raw_output)
            output_json = self._normalize_extraction_output(output_json)
            extraction_run_id = await store.create_extraction_run(
                source_id=source_id,
                meeting_id=meeting_id,
                run_type=run_type,
                model=model or self.model_name,
                prompt_version=prompt_version,
                output_json=output_json,
                status="success",
                error=None,
            )

            if not skip_status_updates:
                await store.update_meeting_status(
                    meeting_id,
                    "processed",
                    ended_at=ended_at,
                    started_at=started_at,
                    summary_short=summary_short,
                    summary_long=summary_long,
                )
            return {
                "extraction_run_id": extraction_run_id,
                "status": "success",
                "output_json": output_json,
            }
        except Exception as error:
            error_message = str(error)
            try:
                await store.create_extraction_run(
                    source_id=source_id,
                    meeting_id=meeting_id,
                    run_type=run_type,
                    model=model or self.model_name,
                    prompt_version=prompt_version,
                    output_json=None,
                    status="failed",
                    error=error_message,
                )
            except Exception as nested_error:
                log(
                    f"Failed to save extraction failure row for {meeting_id}: {nested_error}",
                    session_id=meeting_id,
                    level="error",
                )

            if not skip_status_updates:
                try:
                    await store.update_meeting_status(
                        meeting_id,
                        "failed",
                        ended_at=ended_at,
                        started_at=started_at,
                        summary_short=summary_short,
                        summary_long=summary_long,
                    )
                except Exception as nested_error:
                    log(
                        f"Failed to mark meeting as failed for {meeting_id}: {nested_error}",
                        session_id=meeting_id,
                        level="error",
                    )
            return {"extraction_run_id": None, "status": "failed", "error": error_message}

    def _build_transcript_text(self, chunks):
        lines = []
        for chunk in chunks or []:
            if not isinstance(chunk, dict):
                continue
            text = (chunk.get("text") or "").strip()
            if not text:
                continue
            speaker = (chunk.get("speaker_label") or "Unknown") or "Unknown"
            lines.append(f"{speaker}: {text}")
        return "\n".join(lines)

    def _parse_extraction_json(self, raw_output: str):
        if not raw_output:
            raise ValueError("Empty extraction output.")

        trimmed = raw_output.strip()
        try:
            return json.loads(trimmed)
        except json.JSONDecodeError:
            pass

        if trimmed.startswith("```"):
            trimmed = trimmed.strip("`")
            if not trimmed:
                raise ValueError("Extraction output was wrapped in an empty markdown block.")

        brace_start = trimmed.find("{")
        brace_end = trimmed.rfind("}")
        if brace_start == -1 or brace_end <= brace_start:
            raise ValueError("Could not locate JSON object in extraction output.")

        candidate = trimmed[brace_start : brace_end + 1]
        return json.loads(candidate)

    @staticmethod
    def _coerce_output_list(value):
        return value if isinstance(value, list) else []

    @staticmethod
    def _normalize_extraction_output(value):
        if not isinstance(value, dict):
            raise ValueError("Extraction output is not a JSON object.")

        return {
            "summary_short": str(value.get("summary_short", "")) if value.get("summary_short") is not None else "",
            "summary_long": str(value.get("summary_long", "")) if value.get("summary_long") is not None else "",
            "decisions": OrbitWhatsAppService._coerce_output_list(value.get("decisions")),
            "action_items": OrbitWhatsAppService._coerce_output_list(value.get("action_items")),
            "risks": OrbitWhatsAppService._coerce_output_list(value.get("risks")),
            "open_questions": OrbitWhatsAppService._coerce_output_list(value.get("open_questions")),
            "durable_memories": OrbitWhatsAppService._coerce_output_list(value.get("durable_memories")),
        }

    @staticmethod
    def _empty_extraction_output():
        return {
            "summary_short": "",
            "summary_long": "",
            "decisions": [],
            "action_items": [],
            "risks": [],
            "open_questions": [],
            "durable_memories": [],
        }

    async def handle_session_finished(self, state):
        await self.live_stt.stop(state.session_id)
        await self._finalize_persistent_meeting(state)
        try:
            await self.memory.finalize_meeting(state)
        except Exception as error:
            log(f"Memory indexing failed for Meet {state.meeting_code}: {error}", state.session_id, level="error")

        if state.joined_at:
            live_stt_summary = ""
            if state.live_stt_requested and not state.live_stt_started:
                live_stt_summary = " Live audio transcription did not start because no audio chunk was received."
            leave_summary = f" {state.leave_reason}" if state.leave_reason else ""
            await self.send_whatsapp_message(
                f"Orbit finished Meet {state.meeting_code}. Captured {len(state.captured_messages)} chat message(s)."
                f"{live_stt_summary}"
                f"{leave_summary}"
            )
            return

        if state.status == "waiting_for_host":
            await self.send_whatsapp_message(
                f"Orbit stopped waiting for Meet {state.meeting_code} without being admitted."
            )

    async def _finalize_persistent_meeting(self, state):
        active = self.active_sessions.get(state.session_id)
        if not active or not active.meeting_id:
            return

        store = getattr(self, "meeting_store", None)
        if not store:
            return

        summary_short, summary_long = self._build_meeting_summary(state)
        ended_at = state.finished_at or now_iso()
        try:
            await store.update_meeting_status(
                active.meeting_id,
                "processing",
                ended_at=ended_at,
                started_at=state.joined_at,
                summary_short=summary_short,
                summary_long=summary_long,
            )
            chunks = self._build_transcript_source_chunks(state)
            if chunks:
                await store.save_transcript_chunks(active.source_id, chunks)
            extraction = await self.run_meeting_extraction(
                active.meeting_id,
                active.source_id,
                summary_short=summary_short,
                summary_long=summary_long,
                started_at=state.joined_at,
                ended_at=ended_at,
                skip_status_updates=True,
            )
            if extraction.get("status") == "failed":
                await store.update_meeting_status(
                    active.meeting_id,
                    "failed",
                    ended_at=ended_at,
                    started_at=state.joined_at,
                    summary_short=summary_short,
                    summary_long=summary_long,
                )
                return
            await store.update_meeting_status(
                active.meeting_id,
                "processed",
                ended_at=ended_at,
                started_at=state.joined_at,
                summary_short=summary_short,
                summary_long=summary_long,
            )
        except Exception as error:
            log(
                f"Failed to mark meeting as processed for {active.meeting_id}: {error}",
                state.session_id,
            )
            try:
                await store.update_meeting_status(
                    active.meeting_id,
                    "failed",
                    ended_at=ended_at,
                    started_at=state.joined_at,
                    summary_short=summary_short,
                    summary_long=summary_long,
                )
            except Exception as failed_error:
                log(
                    f"Failed to mark meeting as failed for {active.meeting_id}: {failed_error}",
                    state.session_id,
                )

    def _build_transcript_source_chunks(self, state):
        chunks = []
        for segment in state.live_transcript_segments:
            text = (segment.clean_text or segment.raw_text or "").strip()
            if not text:
                continue

            chunks.append(
                {
                    "speakerLabel": segment.speaker_label or segment.speaker_name,
                    "startMs": segment.start_ms,
                    "endMs": segment.end_ms,
                    "text": text,
                    "metadata": {
                        "source_type": segment.source_type,
                        "speaker_name": segment.speaker_name,
                        "speaker_source": segment.speaker_source,
                        "memory_text": segment.memory_text,
                    },
                }
            )
        return chunks

    def _build_meeting_summary(self, state):
        parts = []
        if state.live_stt_requested:
            parts.append("live STT requested")
        if state.captured_messages:
            parts.append(f"{len(state.captured_messages)} chat messages captured")
        if state.live_transcript_segments:
            parts.append(f"{len(state.live_transcript_segments)} transcript segments captured")
        summary_short = "; ".join(parts) if parts else None
        summary_long = None
        if state.live_transcript_segments:
            summary_long = " | ".join(
                segment.clean_text.strip()
                for segment in state.live_transcript_segments
                if segment.clean_text
            )
            if summary_long and len(summary_long) > 4000:
                summary_long = summary_long[:4000]
        return summary_short, summary_long

    async def send_whatsapp_message(self, body):
        log(f"Sending WhatsApp update: {body}", level="info")
        try:
            configure_dependency_logging()
            await asyncio.to_thread(
                self.twilio_client.messages.create,
                body=body,
                from_=self.twilio_whatsapp_from,
                to=self.twilio_allowed_from,
            )
        except Exception as error:
            log(f"WhatsApp send failed: {error}", level="error")

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
            log(f"WhatsApp Q&A failed: {error}", level="error")
            return "I could not answer that right now because the Q&A model call failed."

        content = response.choices[0].message.content if response.choices else ""
        return (content or "").strip() or "I do not have enough meeting context to answer that."

    async def answer_general_question(self, question):
        try:
            memory_answer = await self.memory.answer_from_memory(question)
        except Exception as error:
            log(f"Memory Q&A failed: {error}", level="error")
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
        dialogue_context = self.format_dialogue_history()
        prompt = question
        if dialogue_context:
            prompt = (
                "Use the recent WhatsApp dialogue only to resolve references in the user's latest message. "
                "Do not treat it as a source of company facts.\n\n"
                f"Recent dialogue:\n{dialogue_context}\n\n"
                f"Latest message:\n{question}"
            )
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
                    {"role": "user", "content": prompt},
                ],
            )
        except Exception as error:
            log(f"General WhatsApp answer failed: {error}", level="error")
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
        expected_token = active.state.live_stt_audio_token
        if expected_token and websocket.query_params.get("token") != expected_token:
            await websocket.close(code=4403, reason="Invalid Orbit audio stream token.")
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
                    if not message["bytes"]:
                        continue
                    if session is None:
                        session = await self.live_stt.get_or_create(active.state)
                    await session.send_audio(message["bytes"])
                    if not active.state.live_stt_started:
                        active.state.live_stt_started = True
                        active.state.live_stt_audio_confirmed_at = now_iso()
                        active.state.live_stt_status_detail = (
                            "Deepgram stream connected and first audio chunk forwarded."
                        )
                        await self.send_whatsapp_message(
                            f"Orbit confirmed live audio transcription for Meet "
                            f"{active.state.meeting_code}."
                        )
                    continue

                raw_text = message.get("text")
                if raw_text is None:
                    continue

                payload = json.loads(raw_text)
                message_type = payload.get("type")
                if message_type in {"start", "config"}:
                    audio_format = LiveAudioFormat.from_payload(payload)
                    session = await self.live_stt.get_or_create(active.state, audio_format)
                    active.state.live_stt_status_detail = (
                        "Extension audio WebSocket connected. Waiting for the first audio chunk."
                    )
                    await websocket.send_json({"type": "ready"})
                elif message_type == "stop":
                    break
        except Exception as error:
            log(
                f"Live audio WebSocket failed for Meet {active.state.meeting_code}: {error}",
                session_id,
                level="error",
            )
        finally:
            await self.live_stt.stop(session_id)
