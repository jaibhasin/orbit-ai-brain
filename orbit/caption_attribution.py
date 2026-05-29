from __future__ import annotations

from dataclasses import dataclass

from orbit.transcript import TranscriptSegment


@dataclass
class CaptionSnippet:
    speaker_name: str
    text: str
    observed_at_ms: int | None = None


def merge_caption_speakers(
    segments: list[TranscriptSegment],
    captions: list[CaptionSnippet],
) -> list[TranscriptSegment]:
    if not segments or not captions:
        return segments

    merged: list[TranscriptSegment] = []
    for segment in segments:
        match = best_caption_match(segment.clean_text or segment.raw_text, captions)
        if match is None:
            merged.append(segment)
            continue

        segment.speaker_name = match.speaker_name
        segment.speaker_source = "google_meet_captions"
        segment.speaker_confidence = "medium"
        segment.metadata = {
            **segment.metadata,
            "caption_text": match.text,
            "caption_observed_at_ms": match.observed_at_ms,
        }
        merged.append(segment)

    return merged


def best_caption_match(text: str, captions: list[CaptionSnippet]) -> CaptionSnippet | None:
    normalized_text = _tokens(text)
    if not normalized_text:
        return None

    best_caption = None
    best_score = 0.0
    for caption in captions:
        score = _overlap_score(normalized_text, _tokens(caption.text))
        if score > best_score:
            best_caption = caption
            best_score = score

    if best_score < 0.6:
        return None
    return best_caption


def _tokens(text: str) -> set[str]:
    cleaned = "".join(char.lower() if char.isalnum() else " " for char in text or "")
    return {token for token in cleaned.split() if len(token) > 2}


def _overlap_score(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / max(len(left), len(right))
