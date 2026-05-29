from __future__ import annotations

import re

from orbit.transcript import TranscriptSegment, format_timestamp_ms


_WHITESPACE_PATTERN = re.compile(r"\s+")
_PUNCT_ONLY_PATTERN = re.compile(r"^[\W_]+$")
_START_REPEAT_PATTERN = re.compile(r"^(\b[^\W\d_]+\b)(?:\s+\1\b){1,3}\s+", re.IGNORECASE)
_FILLER_ONLY_SEGMENTS = {
    "um",
    "uh",
    "umm",
    "uhh",
    "hmm",
    "mm",
    "mmm",
    "erm",
    "ah",
    "eh",
}


def normalize_transcript_segments(
    meeting_code: str,
    segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    normalized_segments: list[TranscriptSegment] = []

    for segment in segments:
        clean_text = normalize_transcript_text(segment.raw_text)
        if not clean_text:
            continue

        normalized_segments.append(
            TranscriptSegment(
                source_id=segment.source_id,
                raw_text=segment.raw_text.strip(),
                clean_text=clean_text,
                memory_text=build_memory_text(
                    meeting_code=meeting_code,
                    clean_text=clean_text,
                    speaker_name=segment.speaker_name,
                    speaker_label=segment.speaker_label,
                    start_ms=segment.start_ms,
                    end_ms=segment.end_ms,
                ),
                detected_language=segment.detected_language,
                speaker_name=segment.speaker_name,
                speaker_label=segment.speaker_label,
                speaker_source=segment.speaker_source,
                speaker_confidence=segment.speaker_confidence,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                confidence=segment.confidence,
                source_type=segment.source_type,
                metadata=dict(segment.metadata),
            )
        )

    return normalized_segments


def normalize_transcript_text(raw_text: str) -> str:
    text = _WHITESPACE_PATTERN.sub(" ", (raw_text or "").strip())
    if not text or _PUNCT_ONLY_PATTERN.match(text):
        return ""

    lowercase_text = text.casefold()
    if lowercase_text in _FILLER_ONLY_SEGMENTS:
        return ""

    text = _START_REPEAT_PATTERN.sub(r"\1 ", text)
    text = _WHITESPACE_PATTERN.sub(" ", text).strip(" -")

    if not text:
        return ""

    if text[-1].isalnum() and len(text) >= 12:
        text = f"{text}."

    if text:
        text = text[0].upper() + text[1:]

    return text


def build_memory_text(
    meeting_code: str,
    clean_text: str,
    speaker_name: str | None,
    speaker_label: str | None,
    start_ms: int | None,
    end_ms: int | None,
) -> str:
    parts = [f"Meet {meeting_code} transcript"]
    if speaker_name:
        parts.append(speaker_name)
    elif speaker_label:
        parts.append(speaker_label)

    if start_ms is not None:
        start_text = format_timestamp_ms(start_ms)
        end_text = format_timestamp_ms(end_ms)
        if start_text and end_text:
            parts.append(f"{start_text}-{end_text}")
        elif start_text:
            parts.append(start_text)

    return " - ".join(parts) + f": {clean_text}"
