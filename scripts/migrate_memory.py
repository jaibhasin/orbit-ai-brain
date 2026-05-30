from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit.core import load_dotenv
from orbit.postgres_schema import (
    MEMORY_SCHEMA_VERSION,
    apply_memory_schema,
    backfill_embedding_model,
)


async def migrate() -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL in .env or environment.")
    embedding_model = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

    from psycopg import AsyncConnection

    async with await AsyncConnection.connect(database_url) as conn:
        async with conn.cursor() as cur:
            await apply_memory_schema(cur)
            await backfill_embedding_model(cur, embedding_model)
            await conn.commit()

    print(f"Applied Orbit memory schema migration: {MEMORY_SCHEMA_VERSION}")


if __name__ == "__main__":
    asyncio.run(migrate())
