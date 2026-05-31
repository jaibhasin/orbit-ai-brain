from __future__ import annotations

from orbit.agent.tools._shared import (
    _normalize_limit,
    _normalize_query,
    _query_rows,
    _require_database_url,
    _run_sync,
    _to_iso_string,
)


def search_decisions(query: str, limit: int = 10) -> list[dict]:
    safe_query = _normalize_query(query, field_name="query")
    safe_limit = _normalize_limit(limit, default=10, maximum=50)
    pattern = f"%{safe_query}%"
    params = (pattern, pattern, pattern, pattern, safe_limit)

    async def _handler():
        rows = await _query_rows(
            _require_database_url(),
            """
            SELECT
                id,
                meeting_id,
                title,
                decision_text,
                rationale,
                owner_text,
                confidence,
                created_at
            FROM decisions
            WHERE
                title ILIKE %s
                OR decision_text ILIKE %s
                OR rationale ILIKE %s
                OR owner_text ILIKE %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            params,
        )

        return [
            {
                "decision_id": row["id"],
                "meeting_id": row["meeting_id"],
                "title": row.get("title"),
                "decision_text": row["decision_text"],
                "rationale": row.get("rationale"),
                "owner_text": row.get("owner_text"),
                "confidence": row.get("confidence"),
                "created_at": _to_iso_string(row.get("created_at")),
            }
            for row in rows
        ]

    return _run_sync(_handler())


def search_company_memory(
    query: str,
    memory_type: str | None = None,
    importance: str | None = None,
    limit: int = 10,
) -> list[dict]:
    safe_query = _normalize_query(query, field_name="query")
    safe_limit = _normalize_limit(limit, default=10, maximum=50)
    pattern = f"%{safe_query}%"
    database_url = _require_database_url()

    clauses: list[str] = ["content ILIKE %s"]
    params: list[str | int | None] = [pattern]
    normalized_type = (memory_type or "").strip()
    normalized_importance = (importance or "").strip()

    if normalized_type:
        clauses.append("memory_type = %s")
        params.append(normalized_type)
    if normalized_importance:
        clauses.append("importance = %s")
        params.append(normalized_importance)

    where_clause = " AND ".join(clauses)
    params.append(safe_limit)
    order_by = """
        CASE
            WHEN LOWER(importance) = 'high' THEN 0
            WHEN LOWER(importance) = 'medium' THEN 1
            WHEN LOWER(importance) = 'low' THEN 2
            ELSE 3
        END,
        created_at DESC
    """

    async def _handler():
        rows = await _query_rows(
            database_url,
            f"""
            SELECT
                id,
                meeting_id,
                memory_type,
                content,
                importance,
                confidence,
                created_at
            FROM memories
            WHERE {where_clause}
            ORDER BY {order_by}
            LIMIT %s
            """,
            tuple(params),
        )

        return [
            {
                "memory_id": row["id"],
                "meeting_id": row["meeting_id"],
                "memory_type": row["memory_type"],
                "content": row["content"],
                "importance": row["importance"],
                "confidence": row["confidence"],
                "created_at": _to_iso_string(row.get("created_at")),
            }
            for row in rows
        ]

    return _run_sync(_handler())
