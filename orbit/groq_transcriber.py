from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from orbit.transcript import TranscriptSegment


def _model_dump(value):
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, dict):
        return value
    return {}


@dataclass
class GroqTranscriber:
    api_key: str
    model: str = "whisper-large-v3-turbo"
    base_url: str = "https://api.groq.com/openai/v1"

    def __post_init__(self):
        self.client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )

    async def transcribe_file(
        self,
        media_path: Path,
        *,
        language: str | None = None,
        prompt: str | None = None,
        known_speaker_names: list[str] | None = None,
    ) -> list[TranscriptSegment]:
        # Groq Whisper does not accept OpenAI's known_speaker_names parameter.
        transcription_prompt = _build_transcription_prompt(
            prompt=prompt,
            known_speaker_names=known_speaker_names,
        )

        with media_path.open("rb") as audio_file:
            response = await self.client.audio.transcriptions.create(
                file=audio_file,
                model=self.model,
                language=language or None,
                prompt=transcription_prompt,
                response_format="verbose_json",
                temperature=0.0,
                timestamp_granularities=["segment"],
            )

        payload = _model_dump(response)
        segments = payload.get("segments") or []
        if not segments and payload.get("text"):
            return [
                TranscriptSegment(
                    source_id="segment-0001",
                    raw_text=str(payload["text"]).strip(),
                    clean_text=str(payload["text"]).strip(),
                    memory_text=str(payload["text"]).strip(),
                    detected_language=language,
                    speaker_label=None,
                    speaker_confidence="unknown",
                    source_type="audio_transcript",
                )
            ]

        transcript_segments: list[TranscriptSegment] = []
        for index, segment in enumerate(segments, start=1):
            segment_payload = _model_dump(segment)
            raw_text = str(segment_payload.get("text") or "").strip()
            if not raw_text:
                continue

            transcript_segments.append(
                TranscriptSegment(
                    source_id=f"segment-{index:04d}",
                    raw_text=raw_text,
                    clean_text=raw_text,
                    memory_text=raw_text,
                    detected_language=language or payload.get("language"),
                    speaker_label=segment_payload.get("speaker"),
                    speaker_confidence="unknown",
                    start_ms=_seconds_to_ms(segment_payload.get("start")),
                    end_ms=_seconds_to_ms(segment_payload.get("end")),
                    confidence=_coerce_confidence(segment_payload),
                    source_type="audio_transcript",
                    metadata={
                        "avg_logprob": segment_payload.get("avg_logprob"),
                        "compression_ratio": segment_payload.get("compression_ratio"),
                        "no_speech_prob": segment_payload.get("no_speech_prob"),
                    },
                )
            )

        return transcript_segments


def _build_transcription_prompt(
    *,
    prompt: str | None,
    known_speaker_names: list[str] | None,
) -> str | None:
    parts: list[str] = []
    if prompt and prompt.strip():
        parts.append(prompt.strip())

    if known_speaker_names:
        names = ", ".join(name.strip() for name in known_speaker_names if name.strip())
        if names:
            parts.append(
                "Possible speakers in this meeting include: "
                f"{names}. Use these names only when clearly supported by the audio."
            )

    if not parts:
        return None

    return " ".join(parts)


def _seconds_to_ms(value) -> int | None:
    if value is None:
        return None
    return int(float(value) * 1000)


def _coerce_confidence(segment_payload: dict) -> float | None:
    avg_logprob = segment_payload.get("avg_logprob")
    if avg_logprob is None:
        return None

    # Whisper-style log probabilities are negative. Zero is highest confidence.
    confidence = max(0.0, min(1.0, 1.0 + (float(avg_logprob) / 5.0)))
    return round(confidence, 4)
