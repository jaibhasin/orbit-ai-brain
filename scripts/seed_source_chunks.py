from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit.core import load_dotenv
from orbit.meeting_store import build_meeting_store


def parse_args():
    parser = argparse.ArgumentParser(description="Insert sample source_chunks rows for testing.")
    parser.add_argument(
        "--source-id",
        required=True,
        help="UUID of an existing sources row.",
    )
    return parser.parse_args()


async def main() -> None:
    load_dotenv()
    args = parse_args()

    store = build_meeting_store(os.environ.get("DATABASE_URL"))
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required to seed source_chunks.")

    chunks = [
        {
            "speakerLabel": "Orbit",
            "startMs": 0,
            "endMs": 2100,
            "text": "Welcome to the test call.",
            "metadata": {"seed": "script", "step": 1},
        },
        {
            "speakerLabel": "Jai",
            "startMs": 2100,
            "endMs": 4300,
            "text": "This is a fake transcript segment for source_chunks validation.",
            "metadata": {"seed": "script", "step": 2},
        },
        {
            "speakerLabel": "Priya",
            "startMs": 4300,
            "endMs": 6200,
            "text": "The third chunk verifies chunk ordering by index.",
            "metadata": {"seed": "script", "step": 3},
        },
    ]

    inserted = await store.save_transcript_chunks(args.source_id, chunks)
    print(f"Inserted {inserted} chunks for source {args.source_id}")


if __name__ == "__main__":
    asyncio.run(main())
