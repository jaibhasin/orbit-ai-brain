from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def format_timestamp_ms(value: int | None) -> str | None:
    if value is None:
        return None

    total_seconds = max(value, 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


@dataclass
class TranscriptSegment:
    source_id: str
    raw_text: str
    clean_text: str
    memory_text: str
    detected_language: str | None = None
    speaker_name: str | None = None
    speaker_label: str | None = None
    speaker_source: str | None = None
    speaker_confidence: str = "unknown"
    start_ms: int | None = None
    end_ms: int | None = None
    confidence: float | None = None
    source_type: str = "audio_transcript"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_debug_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_timestamp"] = format_timestamp_ms(self.start_ms)
        payload["end_timestamp"] = format_timestamp_ms(self.end_ms)
        return payload


@dataclass
class TranscriptDocument:
    session_id: str
    meeting_code: str
    source_path: str
    segments: list[TranscriptSegment]
    provider: str = "groq"
    model: str = "whisper-large-v3-turbo"
    language_hint: str | None = None

    def to_debug_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "meeting_code": self.meeting_code,
            "source_path": self.source_path,
            "provider": self.provider,
            "model": self.model,
            "language_hint": self.language_hint,
            "segments": [segment.to_debug_dict() for segment in self.segments],
        }
