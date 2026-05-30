from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit.core import load_dotenv
from orbit.meeting_store import build_meeting_store
from orbit.whatsapp_service import OrbitWhatsAppService
from openai import AsyncOpenAI


async def testMeetingExtraction(meeting_id: str):
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is required.")

    store = build_meeting_store(database_url)
    meeting = await store.get_meeting_by_id(meeting_id)
    if not meeting:
        raise RuntimeError(f"Meeting {meeting_id} not found.")

    source_id = meeting.get("source_id")
    if not source_id:
        raise RuntimeError(f"Meeting {meeting_id} has no source_id.")

    service = OrbitWhatsAppService.__new__(OrbitWhatsAppService)
    service.model_name = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    service.openai_client = AsyncOpenAI(api_key=openai_api_key)
    service.meeting_store = store

    result = await service.runMeetingExtraction(
        {
            "meetingId": meeting_id,
            "sourceId": source_id,
            "skip_status_updates": False,
        }
    )

    run_id = result.get("extraction_run_id")
    if result.get("status") != "success":
        print(f"Extraction failed for meeting {meeting_id}: {result.get('error')}")
        print(f"Extraction run id: {run_id}")
        return

    output_json = result.get("output_json") or {}
    print(f"Extraction run id: {run_id}")
    print(f"summary_short: {output_json.get('summary_short', '')}")
    print(json.dumps(output_json, indent=2))


def parse_args():
    parser = argparse.ArgumentParser(description="Run meeting extraction and print the output row summary.")
    parser.add_argument("--meeting-id", required=True, help="UUID of meeting row")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(testMeetingExtraction(args.meeting_id))
