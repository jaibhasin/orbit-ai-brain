from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from orbit.core import extract_meeting_code


@dataclass
class PermissionEvent:
    granted_at: str
    author: str
    message_text: str
    fingerprint: str


@dataclass
class ChatMessage:
    fingerprint: str
    raw_text: str
    normalized_text: str
    author: str
    timestamp_text: str


@dataclass
class MeetingState:
    session_id: str
    meet_url: str
    meeting_code: str
    display_name: str
    status: str = "created"
    status_detail: str | None = None
    joined_at: str | None = None
    chat_monitor_started_at: str | None = None
    finished_at: str | None = None
    browser_use_final_result: str | None = None
    browser_use_success: bool = False
    browser_use_had_errors: bool = False
    chat_monitor_available: bool = False
    live_stt_requested: bool = False
    live_stt_available: bool = False
    live_stt_started: bool = False
    live_stt_audio_confirmed_at: str | None = None
    live_stt_audio_token: str | None = None
    live_stt_status_detail: str | None = None
    observed_other_participants: bool = False
    solo_participant_polls: int = 0
    leave_reason: str | None = None
    seen_message_fingerprints: set[str] = field(default_factory=set)
    captured_messages: list[ChatMessage] = field(default_factory=list)
    pending_speak_permissions: int = 0
    permission_events: list[PermissionEvent] = field(default_factory=list)
    introduction_sent: bool = False
    last_error: str | None = None


@dataclass
class MeetingSessionConfig:
    session_id: str
    meet_url: str
    display_name: str
    wait_after_join_ms: int
    max_steps: int
    model_name: str
    live_stt_enabled: bool = False
    audio_stream_ws_url: str | None = None
    audio_stream_token: str | None = None


@dataclass
class MeetingSessionCallbacks:
    on_status: Callable[[MeetingState, str, str | None], Any] | None = None
    on_chat_message: Callable[[MeetingState, ChatMessage, str], Any] | None = None
    on_captions: Callable[[MeetingState, list[Any]], Any] | None = None
    on_orbit_mention: Callable[[MeetingState, ChatMessage], Any] | None = None
    on_finished: Callable[[MeetingState], Any] | None = None


def build_meeting_state(config):
    return MeetingState(
        session_id=config.session_id,
        meet_url=config.meet_url,
        meeting_code=extract_meeting_code(config.meet_url),
        display_name=config.display_name,
        live_stt_audio_token=config.audio_stream_token,
    )
