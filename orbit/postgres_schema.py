from __future__ import annotations

ORBIT_SCHEMA = "orbit_private"
DEFAULT_ORGANIZATION_ID = "default"
MEMORY_SCHEMA_VERSION = "20260531_private_memory_v2"


MEMORY_SCHEMA_SQL = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS orbit_private;
REVOKE ALL ON SCHEMA orbit_private FROM PUBLIC;

DO $$
BEGIN
    IF to_regclass('orbit_private.orbit_meet_sessions') IS NULL
       AND to_regclass('public.orbit_meet_sessions') IS NOT NULL THEN
        ALTER TABLE public.orbit_meet_sessions SET SCHEMA orbit_private;
    END IF;
    IF to_regclass('orbit_private.orbit_chat_messages') IS NULL
       AND to_regclass('public.orbit_chat_messages') IS NOT NULL THEN
        ALTER TABLE public.orbit_chat_messages SET SCHEMA orbit_private;
    END IF;
    IF to_regclass('orbit_private.orbit_transcript_segments') IS NULL
       AND to_regclass('public.orbit_transcript_segments') IS NOT NULL THEN
        ALTER TABLE public.orbit_transcript_segments SET SCHEMA orbit_private;
    END IF;
    IF to_regclass('orbit_private.orbit_memory_chunks') IS NULL
       AND to_regclass('public.orbit_memory_chunks') IS NOT NULL THEN
        ALTER TABLE public.orbit_memory_chunks SET SCHEMA orbit_private;
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS orbit_private.orbit_meet_sessions (
    session_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT 'default',
    meet_url TEXT NOT NULL,
    meeting_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    status_detail TEXT,
    leave_reason TEXT,
    last_error TEXT,
    joined_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orbit_private.orbit_chat_messages (
    id BIGSERIAL PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT NOT NULL REFERENCES orbit_private.orbit_meet_sessions(session_id) ON DELETE CASCADE,
    meeting_code TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    author TEXT,
    timestamp_text TEXT,
    raw_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS orbit_private.orbit_transcript_segments (
    id BIGSERIAL PRIMARY KEY,
    segment_id UUID NOT NULL DEFAULT gen_random_uuid(),
    organization_id TEXT NOT NULL DEFAULT 'default',
    session_id TEXT NOT NULL REFERENCES orbit_private.orbit_meet_sessions(session_id) ON DELETE CASCADE,
    meeting_code TEXT NOT NULL,
    source_id TEXT NOT NULL,
    source_type TEXT NOT NULL,
    speaker_name TEXT,
    speaker_label TEXT,
    speaker_source TEXT,
    speaker_confidence TEXT NOT NULL,
    detected_language TEXT,
    start_ms INTEGER,
    end_ms INTEGER,
    raw_text TEXT NOT NULL,
    clean_text TEXT NOT NULL,
    memory_text TEXT NOT NULL,
    confidence REAL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (segment_id),
    UNIQUE (session_id, source_id)
);

CREATE TABLE IF NOT EXISTS orbit_private.orbit_memory_chunks (
    id BIGSERIAL PRIMARY KEY,
    organization_id TEXT NOT NULL DEFAULT 'default',
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    meeting_code TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES orbit_private.orbit_meet_sessions(session_id) ON DELETE CASCADE,
    chat_message_id BIGINT REFERENCES orbit_private.orbit_chat_messages(id) ON DELETE CASCADE,
    transcript_segment_id UUID REFERENCES orbit_private.orbit_transcript_segments(segment_id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    embedding vector(1536),
    embedding_model TEXT,
    index_status TEXT NOT NULL DEFAULT 'pending',
    index_error TEXT,
    indexed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_type, source_id)
);

ALTER TABLE orbit_private.orbit_meet_sessions
    ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS status_detail TEXT,
    ADD COLUMN IF NOT EXISTS leave_reason TEXT,
    ADD COLUMN IF NOT EXISTS last_error TEXT,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

ALTER TABLE orbit_private.orbit_chat_messages
    ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE orbit_private.orbit_transcript_segments
    ADD COLUMN IF NOT EXISTS segment_id UUID NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS speaker_name TEXT,
    ADD COLUMN IF NOT EXISTS speaker_source TEXT;

ALTER TABLE orbit_private.orbit_memory_chunks
    ADD COLUMN IF NOT EXISTS organization_id TEXT NOT NULL DEFAULT 'default',
    ADD COLUMN IF NOT EXISTS chat_message_id BIGINT,
    ADD COLUMN IF NOT EXISTS transcript_segment_id UUID,
    ADD COLUMN IF NOT EXISTS embedding_model TEXT,
    ADD COLUMN IF NOT EXISTS index_status TEXT NOT NULL DEFAULT 'indexed',
    ADD COLUMN IF NOT EXISTS index_error TEXT,
    ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

DO $$
DECLARE
    joined_type TEXT;
    finished_type TEXT;
BEGIN
    SELECT data_type INTO joined_type
    FROM information_schema.columns
    WHERE table_schema = 'orbit_private'
      AND table_name = 'orbit_meet_sessions'
      AND column_name = 'joined_at';

    SELECT data_type INTO finished_type
    FROM information_schema.columns
    WHERE table_schema = 'orbit_private'
      AND table_name = 'orbit_meet_sessions'
      AND column_name = 'finished_at';

    IF joined_type = 'text' THEN
        ALTER TABLE orbit_private.orbit_meet_sessions
            ALTER COLUMN joined_at TYPE TIMESTAMPTZ
            USING NULLIF(joined_at, '')::TIMESTAMPTZ;
    END IF;
    IF finished_type = 'text' THEN
        ALTER TABLE orbit_private.orbit_meet_sessions
            ALTER COLUMN finished_at TYPE TIMESTAMPTZ
            USING NULLIF(finished_at, '')::TIMESTAMPTZ;
    END IF;
END
$$;

ALTER TABLE orbit_private.orbit_memory_chunks
    ALTER COLUMN meeting_code SET NOT NULL,
    ALTER COLUMN session_id SET NOT NULL,
    ALTER COLUMN embedding DROP NOT NULL;

UPDATE orbit_private.orbit_meet_sessions
SET status = 'completed',
    updated_at = now()
WHERE finished_at IS NOT NULL
  AND status IN ('joined', 'live_stt_capture_requested');

UPDATE orbit_private.orbit_memory_chunks
SET index_status = CASE WHEN embedding IS NULL THEN 'pending' ELSE 'indexed' END,
    indexed_at = CASE
        WHEN embedding IS NOT NULL AND indexed_at IS NULL THEN created_at
        ELSE indexed_at
    END,
    updated_at = now();

UPDATE orbit_private.orbit_memory_chunks AS chunk
SET chat_message_id = message.id
FROM orbit_private.orbit_chat_messages AS message
WHERE chunk.source_type = 'meet_chat'
  AND chunk.chat_message_id IS NULL
  AND chunk.source_id = message.session_id || ':' || message.fingerprint;

UPDATE orbit_private.orbit_memory_chunks AS chunk
SET transcript_segment_id = segment.segment_id
FROM orbit_private.orbit_transcript_segments AS segment
WHERE chunk.source_type = 'meet_transcript'
  AND chunk.transcript_segment_id IS NULL
  AND chunk.source_id = segment.session_id || ':' || segment.source_id;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orbit_transcript_segments_segment_id_key'
          AND conrelid = 'orbit_private.orbit_transcript_segments'::regclass
    ) THEN
        ALTER TABLE orbit_private.orbit_transcript_segments
            ADD CONSTRAINT orbit_transcript_segments_segment_id_key UNIQUE (segment_id);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orbit_memory_chunks_session_id_fkey'
          AND conrelid = 'orbit_private.orbit_memory_chunks'::regclass
    ) THEN
        ALTER TABLE orbit_private.orbit_memory_chunks
            ADD CONSTRAINT orbit_memory_chunks_session_id_fkey
            FOREIGN KEY (session_id)
            REFERENCES orbit_private.orbit_meet_sessions(session_id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orbit_memory_chunks_chat_message_id_fkey'
          AND conrelid = 'orbit_private.orbit_memory_chunks'::regclass
    ) THEN
        ALTER TABLE orbit_private.orbit_memory_chunks
            ADD CONSTRAINT orbit_memory_chunks_chat_message_id_fkey
            FOREIGN KEY (chat_message_id)
            REFERENCES orbit_private.orbit_chat_messages(id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orbit_memory_chunks_transcript_segment_id_fkey'
          AND conrelid = 'orbit_private.orbit_memory_chunks'::regclass
    ) THEN
        ALTER TABLE orbit_private.orbit_memory_chunks
            ADD CONSTRAINT orbit_memory_chunks_transcript_segment_id_fkey
            FOREIGN KEY (transcript_segment_id)
            REFERENCES orbit_private.orbit_transcript_segments(segment_id)
            ON DELETE CASCADE;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orbit_memory_chunks_index_status_check'
          AND conrelid = 'orbit_private.orbit_memory_chunks'::regclass
    ) THEN
        ALTER TABLE orbit_private.orbit_memory_chunks
            ADD CONSTRAINT orbit_memory_chunks_index_status_check
            CHECK (index_status IN ('pending', 'indexed', 'failed'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'orbit_memory_chunks_source_link_check'
          AND conrelid = 'orbit_private.orbit_memory_chunks'::regclass
    ) THEN
        ALTER TABLE orbit_private.orbit_memory_chunks
            ADD CONSTRAINT orbit_memory_chunks_source_link_check
            CHECK (
                (source_type <> 'meet_chat' OR (chat_message_id IS NOT NULL AND transcript_segment_id IS NULL))
                AND
                (source_type <> 'meet_transcript' OR (transcript_segment_id IS NOT NULL AND chat_message_id IS NULL))
            );
    END IF;
END
$$;

DROP INDEX IF EXISTS orbit_private.orbit_memory_chunks_embedding_idx;

CREATE INDEX IF NOT EXISTS orbit_meet_sessions_organization_created_idx
    ON orbit_private.orbit_meet_sessions (organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS orbit_chat_messages_organization_session_idx
    ON orbit_private.orbit_chat_messages (organization_id, session_id, created_at);
CREATE INDEX IF NOT EXISTS orbit_transcript_segments_organization_session_idx
    ON orbit_private.orbit_transcript_segments (organization_id, session_id, created_at);
CREATE INDEX IF NOT EXISTS orbit_memory_chunks_organization_index_status_idx
    ON orbit_private.orbit_memory_chunks (organization_id, index_status);
CREATE INDEX IF NOT EXISTS orbit_memory_chunks_session_idx
    ON orbit_private.orbit_memory_chunks (session_id);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON SCHEMA orbit_private FROM anon;
        REVOKE ALL ON ALL TABLES IN SCHEMA orbit_private FROM anon;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA orbit_private FROM anon;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
        REVOKE ALL ON SCHEMA orbit_private FROM authenticated;
        REVOKE ALL ON ALL TABLES IN SCHEMA orbit_private FROM authenticated;
        REVOKE ALL ON ALL SEQUENCES IN SCHEMA orbit_private FROM authenticated;
    END IF;
END
$$;
"""


async def apply_memory_schema(cur) -> None:
    await cur.execute("SELECT pg_advisory_xact_lock(hashtext('orbit_memory_schema'))")
    await cur.execute(MEMORY_SCHEMA_SQL)
    await cur.execute(
        """
        CREATE TABLE IF NOT EXISTS orbit_private.orbit_schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await cur.execute(
        """
        INSERT INTO orbit_private.orbit_schema_migrations (version)
        VALUES (%s)
        ON CONFLICT (version) DO NOTHING
        """,
        (MEMORY_SCHEMA_VERSION,),
    )


async def backfill_embedding_model(cur, embedding_model: str) -> None:
    await cur.execute(
        """
        UPDATE orbit_private.orbit_memory_chunks
        SET embedding_model = %s,
            updated_at = now()
        WHERE embedding IS NOT NULL
          AND embedding_model IS NULL
        """,
        (embedding_model,),
    )
