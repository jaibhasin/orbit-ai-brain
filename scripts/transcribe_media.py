from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI

from orbit.core import DEBUG_DIR, extract_meeting_code, load_dotenv, now_iso
from orbit.groq_transcriber import GroqTranscriber
from orbit.meet_types import MeetingState
from orbit.memory import build_memory_service
from orbit.transcript import TranscriptDocument
from orbit.transcript_normalizer import normalize_transcript_segments


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Transcribe a meeting recording with Groq Whisper and ingest it into Orbit memory."
    )
    parser.add_argument("media_path", type=Path, help="Path to an audio or video file.")
    parser.add_argument("--meet-url", help="Google Meet URL for provenance labels.")
    parser.add_argument("--meeting-code", help="Explicit meeting code override.")
    parser.add_argument("--session-id", help="Explicit session id override.")
    parser.add_argument("--language", help="Optional ISO-639-1 language hint for Groq Whisper.")
    parser.add_argument(
        "--speaker-names",
        help=(
            "Comma-separated known speaker names. Groq Whisper has no diarization; "
            "these names are added to the transcription prompt as hints only."
        ),
    )
    parser.add_argument("--prompt", help="Optional spelling/context hint for transcription.")
    parser.add_argument(
        "--skip-memory",
        action="store_true",
        help="Do not write transcript segments into Postgres memory.",
    )
    return parser


def build_meeting_state(
    media_path: Path,
    meet_url: str | None,
    meeting_code: str | None,
    session_id: str | None,
) -> MeetingState:
    resolved_meeting_code = meeting_code or extract_meeting_code(meet_url or "") or slugify(media_path.stem)
    if resolved_meeting_code == "unknown-meet":
        resolved_meeting_code = slugify(media_path.stem) or "imported-media"

    resolved_meet_url = meet_url or f"https://meet.google.com/{resolved_meeting_code}"
    resolved_session_id = session_id or f"import-{resolved_meeting_code}-{now_iso().replace(':', '').replace('-', '')}"

    return MeetingState(
        session_id=resolved_session_id,
        meet_url=resolved_meet_url,
        meeting_code=resolved_meeting_code,
        display_name="Orbit Transcript Import",
        status="transcript_imported",
        finished_at=now_iso(),
    )


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower())
    return slug.strip("-")


def write_debug_document(document: TranscriptDocument) -> Path:
    transcript_dir = DEBUG_DIR / "transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    output_path = transcript_dir / f"{document.session_id}.json"
    output_path.write_text(json.dumps(document.to_debug_dict(), indent=2))
    return output_path


async def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    load_dotenv()
    media_path = args.media_path.expanduser().resolve()
    if not media_path.exists():
        raise RuntimeError(f"Media file not found: {media_path}")

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    groq_api_key = os.environ.get("GROQ_API_KEY")
    if not groq_api_key:
        raise RuntimeError("Missing GROQ_API_KEY in .env or environment.")
    if not openai_api_key and not args.skip_memory:
        raise RuntimeError("Missing OPENAI_API_KEY in .env or environment.")

    state = build_meeting_state(
        media_path=media_path,
        meet_url=args.meet_url,
        meeting_code=args.meeting_code,
        session_id=args.session_id,
    )
    speaker_names = [
        item.strip()
        for item in (args.speaker_names or "").split(",")
        if item.strip()
    ]

    transcriber = GroqTranscriber(
        api_key=groq_api_key,
        model=os.environ.get("GROQ_TRANSCRIPTION_MODEL", "whisper-large-v3-turbo"),
    )
    raw_segments = await transcriber.transcribe_file(
        media_path=media_path,
        language=args.language,
        prompt=args.prompt,
        known_speaker_names=speaker_names or None,
    )
    normalized_segments = normalize_transcript_segments(state.meeting_code, raw_segments)

    document = TranscriptDocument(
        session_id=state.session_id,
        meeting_code=state.meeting_code,
        source_path=str(media_path),
        segments=normalized_segments,
        model=transcriber.model,
        language_hint=args.language,
    )
    debug_path = write_debug_document(document)

    if args.skip_memory:
        print(f"Transcript saved to {debug_path} with {len(normalized_segments)} normalized segment(s).")
        return

    openai_client = AsyncOpenAI(api_key=openai_api_key)
    memory = build_memory_service(
        openai_client=openai_client,
        answer_model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
    )
    await memory.record_transcript_segments(state, normalized_segments)

    print(
        f"Imported {len(normalized_segments)} transcript segment(s) for Meet {state.meeting_code}. "
        f"Debug transcript: {debug_path}"
    )


if __name__ == "__main__":
    asyncio.run(main())
