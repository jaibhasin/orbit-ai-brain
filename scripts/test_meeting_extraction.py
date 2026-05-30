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


async def testPersistDecisionsFromLatestExtraction(meeting_id: str):
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    store = build_meeting_store(database_url)
    meeting = await store.get_meeting_by_id(meeting_id)
    if not meeting:
        raise RuntimeError(f"Meeting {meeting_id} not found.")

    source_id = meeting.get("source_id")
    if not source_id:
        raise RuntimeError(f"Meeting {meeting_id} has no source_id.")

    # Fetch the latest successful extraction for this meeting.
    try:
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
    except Exception as error:
        raise RuntimeError(
            "psycopg is required to query extraction_runs for this script."
        ) from error

    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, output_json
                FROM extraction_runs
                WHERE meeting_id = %s
                  AND status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (meeting_id,),
            )
            extraction_run = await cursor.fetchone()

    if not extraction_run:
        raise RuntimeError(f"No successful extraction found for meeting {meeting_id}.")

    output_json = extraction_run.get("output_json") or {}
    decisions = output_json.get("decisions")
    inserted = await store.createDecisionsFromExtraction(
        meeting_id=meeting_id,
        source_id=source_id,
        decisions=decisions,
    )

    print(f"Replayed extraction run id: {extraction_run.get('id')}")
    print(f"Inserted decisions for meeting {meeting_id}: {inserted}")


async def testPersistActionItemsFromLatestExtraction(meeting_id: str):
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is required.")

    store = build_meeting_store(database_url)
    meeting = await store.get_meeting_by_id(meeting_id)
    if not meeting:
        raise RuntimeError(f"Meeting {meeting_id} not found.")

    source_id = meeting.get("source_id")
    if not source_id:
        raise RuntimeError(f"Meeting {meeting_id} has no source_id.")

    try:
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
    except Exception as error:
        raise RuntimeError(
            "psycopg is required to query extraction_runs for this script."
        ) from error

    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, output_json
                FROM extraction_runs
                WHERE meeting_id = %s
                  AND status = 'success'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (meeting_id,),
            )
            extraction_run = await cursor.fetchone()

    if not extraction_run:
        raise RuntimeError(f"No successful extraction found for meeting {meeting_id}.")

    output_json = extraction_run.get("output_json") or {}
    action_items = output_json.get("action_items")
    inserted = await store.createActionItemsFromExtraction(
        meeting_id=meeting_id,
        source_id=source_id,
        action_items=action_items,
    )

    print(f"Replayed extraction run id: {extraction_run.get('id')}")
    print(f"Inserted action items for meeting {meeting_id}: {inserted}")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Run extraction or persist decisions / action items from latest successful extraction."
        ),
    )
    parser.add_argument("--meeting-id", required=True, help="UUID of meeting row")
    parser.add_argument(
        "--persist-decisions",
        action="store_true",
        help="Persist decisions from latest successful extraction for this meeting",
    )
    parser.add_argument(
        "--persist-action-items",
        action="store_true",
        help="Persist action items from latest successful extraction for this meeting",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.persist_decisions:
        asyncio.run(testPersistDecisionsFromLatestExtraction(args.meeting_id))
    elif args.persist_action_items:
        asyncio.run(testPersistActionItemsFromLatestExtraction(args.meeting_id))
    else:
        asyncio.run(testMeetingExtraction(args.meeting_id))
