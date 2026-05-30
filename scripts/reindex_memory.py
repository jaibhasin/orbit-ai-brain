from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import AsyncOpenAI

from orbit.core import env_float, env_int, load_dotenv
from orbit.postgres_memory import PostgresMemoryService


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retry pending or failed Orbit memory embeddings without replaying meetings."
    )
    parser.add_argument("--session-id", help="Only retry chunks for one meeting session.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum chunks to retry.")
    return parser


async def reindex(*, session_id: str | None, limit: int) -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL in .env or environment.")
    if not openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in .env or environment.")

    service = PostgresMemoryService(
        database_url=database_url,
        openai_client=AsyncOpenAI(api_key=openai_api_key),
        answer_model=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        embedding_model=os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small"),
        organization_id=os.environ.get("ORBIT_ORGANIZATION_ID", "default"),
        search_limit=env_int("ORBIT_MEMORY_SEARCH_LIMIT", 6),
        similarity_threshold=env_float("ORBIT_MEMORY_SIMILARITY_THRESHOLD", 0.35),
    )
    indexed, failed = await service.retry_memory_chunk_indexing(
        session_id=session_id,
        limit=limit,
    )
    print(f"Orbit memory reindex complete: {indexed} indexed, {failed} failed.")


if __name__ == "__main__":
    args = build_argument_parser().parse_args()
    asyncio.run(reindex(session_id=args.session_id, limit=args.limit))
