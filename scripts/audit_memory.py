from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from orbit.core import load_dotenv
from orbit.postgres_schema import ORBIT_SCHEMA


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only audit for Orbit memory security, indexing, and stored transcript text."
    )
    parser.add_argument(
        "--show-text",
        action="store_true",
        help="Print recent transcript raw, normalized, and searchable memory text.",
    )
    return parser


async def audit(*, show_text: bool) -> None:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("Missing DATABASE_URL in .env or environment.")

    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT c.relname AS table_name, c.relrowsecurity AS rls_enabled
                FROM pg_class AS c
                JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                  AND c.relname LIKE 'orbit_%%'
                ORDER BY c.relname
                """
            )
            public_tables = await cur.fetchall()

            await cur.execute(
                """
                SELECT table_name, grantee, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
                FROM information_schema.role_table_grants
                WHERE table_schema = %s
                  AND table_name LIKE 'orbit_%%'
                  AND grantee IN ('anon', 'authenticated')
                GROUP BY table_name, grantee
                ORDER BY table_name, grantee
                """,
                (ORBIT_SCHEMA,),
            )
            api_grants = await cur.fetchall()

            print("SECURITY")
            print(f"public Orbit tables: {len(public_tables)}")
            print(f"private API-role grants: {len(api_grants)}")
            if public_tables:
                for row in public_tables:
                    print(f"- public.{row['table_name']} rls_enabled={row['rls_enabled']}")
            if api_grants:
                for row in api_grants:
                    print(f"- {row['table_name']} {row['grantee']}: {row['privileges']}")

            print("\nROWS")
            for table in [
                "orbit_meet_sessions",
                "orbit_chat_messages",
                "orbit_transcript_segments",
                "orbit_memory_chunks",
            ]:
                await cur.execute(f"SELECT count(*) AS count FROM {ORBIT_SCHEMA}.{table}")
                count_row = await cur.fetchone()
                assert count_row is not None
                count = count_row["count"]
                print(f"{table}: {count}")

            print("\nSESSION LIFECYCLE")
            await cur.execute(
                f"""
                SELECT status, count(*) AS sessions
                FROM {ORBIT_SCHEMA}.orbit_meet_sessions
                GROUP BY status
                ORDER BY status
                """
            )
            for row in await cur.fetchall():
                print(f"{row['status']}: {row['sessions']}")

            print("\nINDEXING")
            await cur.execute(
                f"""
                SELECT index_status, embedding_model, count(*) AS chunks
                FROM {ORBIT_SCHEMA}.orbit_memory_chunks
                GROUP BY index_status, embedding_model
                ORDER BY index_status, embedding_model NULLS FIRST
                """
            )
            for row in await cur.fetchall():
                print(
                    f"{row['index_status']} model={row['embedding_model'] or 'none'}: "
                    f"{row['chunks']}"
                )

            print("\nTEXT COMPLETENESS")
            await cur.execute(
                f"""
                SELECT
                    count(*) FILTER (WHERE raw_text = '') AS empty_raw_text,
                    count(*) FILTER (WHERE clean_text = '') AS empty_clean_text,
                    count(*) FILTER (WHERE memory_text = '') AS empty_memory_text
                FROM {ORBIT_SCHEMA}.orbit_transcript_segments
                """
            )
            completeness_row = await cur.fetchone()
            assert completeness_row is not None
            print(dict(completeness_row))
            await cur.execute(
                f"""
                SELECT count(*) FILTER (WHERE text = '') AS empty_searchable_text
                FROM {ORBIT_SCHEMA}.orbit_memory_chunks
                """
            )
            chunk_completeness_row = await cur.fetchone()
            assert chunk_completeness_row is not None
            print(dict(chunk_completeness_row))

            print("\nRELATIONAL INTEGRITY")
            await cur.execute(
                f"""
                SELECT
                    count(*) FILTER (
                        WHERE source_type = 'meet_chat' AND chat_message_id IS NULL
                    ) AS unlinked_chat_chunks,
                    count(*) FILTER (
                        WHERE source_type = 'meet_transcript' AND transcript_segment_id IS NULL
                    ) AS unlinked_transcript_chunks
                FROM {ORBIT_SCHEMA}.orbit_memory_chunks
                """
            )
            integrity_row = await cur.fetchone()
            assert integrity_row is not None
            print(dict(integrity_row))

            if show_text:
                print("\nRECENT TRANSCRIPT TEXT")
                await cur.execute(
                    f"""
                    SELECT meeting_code, source_id, raw_text, clean_text, memory_text
                    FROM {ORBIT_SCHEMA}.orbit_transcript_segments
                    ORDER BY created_at DESC
                    LIMIT 10
                    """
                )
                for row in await cur.fetchall():
                    print(dict(row))

                print("\nRECENT SEARCHABLE TEXT")
                await cur.execute(
                    f"""
                    SELECT source_type, meeting_code, source_id, text, index_status, embedding_model
                    FROM {ORBIT_SCHEMA}.orbit_memory_chunks
                    ORDER BY created_at DESC
                    LIMIT 10
                    """
                )
                for row in await cur.fetchall():
                    print(dict(row))

            if public_tables or api_grants:
                raise RuntimeError("Orbit memory security audit failed.")


if __name__ == "__main__":
    args = build_argument_parser().parse_args()
    asyncio.run(audit(show_text=args.show_text))
