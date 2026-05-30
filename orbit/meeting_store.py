from __future__ import annotations

from dataclasses import dataclass


MEETING_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS people (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT,
    phone TEXT,
    email TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_type TEXT NOT NULL,
    url TEXT,
    title TEXT,
    raw_text TEXT,
    raw_payload JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meetings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID REFERENCES sources(id) ON DELETE CASCADE,
    gmeet_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    requested_by_person_id UUID REFERENCES people(id),
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    summary_short TEXT,
    summary_long TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


@dataclass
class DisabledMeetingStore:
    async def find_or_create_person_by_phone(self, phone: str, name: str | None = None) -> str | None:
        return None

    async def create_source(
        self,
        source_type: str,
        *,
        url: str | None = None,
        title: str | None = None,
        raw_text: str | None = None,
        raw_payload: str | None = None,
    ) -> str | None:
        return None

    async def create_meeting(
        self,
        gmeet_url: str,
        *,
        source_id: str | None,
        status: str,
        requested_by_person_id: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> str | None:
        return None

    async def update_meeting_status(
        self,
        meeting_id: str,
        status: str,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
    ) -> None:
        return None


@dataclass
class PostgresMeetingStore:
    database_url: str

    async def _connect(self):
        try:
            from psycopg import AsyncConnection
            from psycopg.rows import dict_row
        except ImportError as error:
            raise RuntimeError(
                "Postgres meeting persistence requires psycopg. Run `pip install -r requirements.txt`."
            ) from error

        return await AsyncConnection.connect(self.database_url, row_factory=dict_row)

    async def _ensure_ready(self):
        if getattr(self, "_ready", False):
            return

        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(MEETING_SCHEMA_SQL)
            await conn.commit()

        self._ready = True

    async def find_or_create_person_by_phone(self, phone: str, name: str | None = None) -> str | None:
        if not phone:
            return None

        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT id
                    FROM people
                    WHERE phone = %s
                    LIMIT 1
                    """,
                    (phone,),
                )
                existing = await cursor.fetchone()
                if existing:
                    return existing["id"]

                await cursor.execute(
                    """
                    INSERT INTO people (name, phone)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (name, phone),
                )
                created = await cursor.fetchone()
                await conn.commit()
                return created["id"] if created else None

    async def create_source(
        self,
        source_type: str,
        *,
        url: str | None = None,
        title: str | None = None,
        raw_text: str | None = None,
        raw_payload: str | None = None,
    ) -> str | None:
        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO sources (source_type, url, title, raw_text, raw_payload)
                    VALUES (%s, %s, %s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (source_type, url, title, raw_text, raw_payload),
                )
                row = await cursor.fetchone()
                await conn.commit()
                return row["id"] if row else None

    async def create_meeting(
        self,
        gmeet_url: str,
        *,
        source_id: str | None,
        status: str,
        requested_by_person_id: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
        started_at: str | None = None,
        ended_at: str | None = None,
    ) -> str | None:
        await self._ensure_ready()
        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO meetings (
                        source_id,
                        gmeet_url,
                        status,
                        requested_by_person_id,
                        summary_short,
                        summary_long,
                        started_at,
                        ended_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        source_id,
                        gmeet_url,
                        status,
                        requested_by_person_id,
                        summary_short,
                        summary_long,
                        started_at,
                        ended_at,
                    ),
                )
                row = await cursor.fetchone()
                await conn.commit()
                return row["id"] if row else None

    async def update_meeting_status(
        self,
        meeting_id: str,
        status: str,
        *,
        started_at: str | None = None,
        ended_at: str | None = None,
        summary_short: str | None = None,
        summary_long: str | None = None,
    ) -> None:
        if not meeting_id:
            return

        await self._ensure_ready()
        updates = ["status = %s", "updated_at = now()"]
        values = [status]

        if started_at:
            updates.append("started_at = COALESCE(started_at, %s)")
            values.append(started_at)
        if ended_at:
            updates.append("ended_at = COALESCE(ended_at, %s)")
            values.append(ended_at)
        if summary_short is not None:
            updates.append("summary_short = COALESCE(summary_short, %s)")
            values.append(summary_short)
        if summary_long is not None:
            updates.append("summary_long = COALESCE(summary_long, %s)")
            values.append(summary_long)

        values.append(meeting_id)
        sql = f"""
            UPDATE meetings
            SET {", ".join(updates)}
            WHERE id = %s
        """

        async with await self._connect() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, values)
                await conn.commit()


def build_meeting_store(database_url: str | None):
    if not database_url:
        return DisabledMeetingStore()

    return PostgresMeetingStore(database_url=database_url)
