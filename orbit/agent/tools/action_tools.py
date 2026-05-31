from __future__ import annotations

from orbit.agent.tools._shared import (
    _normalize_limit,
    _query_rows,
    _require_database_url,
)


async def get_open_action_items(
    owner_text: str | None = None,
    since: str | None = None,
    limit: int = 10,
) -> list[dict]:
    safe_limit = _normalize_limit(limit, default=10, maximum=50)
    owner_filter = (owner_text or "").strip()

    params: list[str | int] = []
    where_clauses = ["status = 'open'"]

    if owner_filter:
        where_clauses.append("owner_text = %s")
        params.append(owner_filter)

    if since:
        where_clauses.append("created_at >= %s")
        params.append(since)

    where_clause = "WHERE " + " AND ".join(where_clauses)
    params.append(safe_limit)

    rows = await _query_rows(
        _require_database_url(),
        f"""
        SELECT
            id,
            meeting_id,
            task,
            owner_text,
            due_date,
            confidence,
            created_at
        FROM action_items
        {where_clause}
        ORDER BY (due_date IS NULL), due_date ASC, (confidence IS NULL), confidence DESC, created_at ASC
        LIMIT %s
        """,
        tuple(params),
    )

    return [
        {
            "action_item_id": row["id"],
            "meeting_id": row["meeting_id"],
            "task": row["task"],
            "owner_text": row.get("owner_text"),
            "due_date": row.get("due_date"),
            "confidence": row.get("confidence"),
        }
        for row in rows
    ]
