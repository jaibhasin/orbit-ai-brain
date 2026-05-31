from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import os
import re
from uuid import UUID


_GOOGLE_MEET_URL_PATTERN = re.compile(
    r"^https?://(?:www\.)?meet\.google\.com/[a-z0-9-]+(?:/[^\s]*)?(?:\?.*)?$",
    re.IGNORECASE,
)


class AgentToolError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ConfigurationError(AgentToolError):
    code: str
    message: str

    def __post_init__(self):
        super().__init__(self.code, self.message)


@dataclass
class ValidationError(AgentToolError):
    code: str
    message: str

    def __post_init__(self):
        super().__init__(self.code, self.message)


@dataclass
class NotFoundError(AgentToolError):
    code: str
    message: str

    def __post_init__(self):
        super().__init__(self.code, self.message)


def _require_database_url() -> str:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise ConfigurationError(
            code="DATABASE_URL_MISSING",
            message="DATABASE_URL is required for agent tools.",
        )
    return database_url


def _require_uuid(value: str, *, field_name: str, error_code: str, required_message: str) -> str:
    text = (value or "").strip()
    try:
        UUID(text)
    except (TypeError, ValueError):
        raise ValidationError(code=error_code, message=required_message)
    return text


def _require_google_meet_url(url: str) -> str:
    trimmed = (url or "").strip()
    if not _GOOGLE_MEET_URL_PATTERN.match(trimmed):
        raise ValidationError(
            code="INVALID_MEET_URL",
            message="gmeet_url must be a valid Google Meet URL.",
        )
    return trimmed


def _normalize_limit(value: int | None, *, default: int, maximum: int) -> int:
    try:
        limit = int(value if value is not None else default)
    except (TypeError, ValueError):
        limit = default
    if limit < 1:
        limit = 1
    return min(limit, maximum)


def _normalize_query(value: str, *, field_name: str) -> str:
    query = (value or "").strip()
    if not query:
        raise ValidationError(
            code="EMPTY_QUERY",
            message=f"{field_name} must not be empty.",
        )
    return query


def _to_iso_string(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _normalize_optional_owner_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _normalize_phone_for_whatsapp(phone: str | None) -> str | None:
    normalized = (phone or "").strip().replace(" ", "")
    if not normalized:
        return None
    if not normalized.startswith("whatsapp:"):
        if normalized.startswith("+"):
            return f"whatsapp:{normalized}"
        return f"whatsapp:+{normalized}"
    return normalized


async def _query_rows(database_url: str, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
    try:
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
    except Exception as error:
        raise ConfigurationError(
            code="PSYCOPG_MISSING",
            message="Postgres persistence requires psycopg.",
        ) from error

    async with await AsyncConnection.connect(database_url, row_factory=dict_row) as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(sql, params or ())
            return (await cursor.fetchall()) or []


async def _query_row(database_url: str, sql: str, params: tuple[Any, ...] | list[Any] | None = None):
    rows = await _query_rows(database_url, sql, params=params)
    return rows[0] if rows else None
