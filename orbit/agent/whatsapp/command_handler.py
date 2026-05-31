from __future__ import annotations

import uuid

from orbit.agent.tools import (
    get_meeting_capture_status,
    get_meeting_intelligence,
    get_open_action_items,
    get_recent_meetings,
    request_meeting_capture,
)
from orbit.agent.tools._shared import AgentToolError, _query_row, _require_database_url
from orbit.agent.whatsapp.command_parser import (
    HELP_TEXT,
    ParsedWhatsAppCommand,
    is_valid_google_meet_url,
    parse_whatsapp_command,
)
from orbit.core import log
from orbit.phone_numbers import normalize_whatsapp_phone

UNREGISTERED_PHONE_MESSAGE = (
    "You are not registered yet. Please ask an admin to add your WhatsApp number."
)
JOIN_HELP_MESSAGE = "Please send: join <google-meet-link>"


def _build_phone_candidates(raw_phone: str) -> list[str]:
    normalized = normalize_whatsapp_phone(raw_phone)
    if not normalized:
        return []

    candidates = [normalized]
    if not normalized.startswith("+"):
        candidates.append(f"+{normalized}")
        candidates.append(f"whatsapp:{normalized}")
        candidates.append(f"whatsapp:+{normalized}")
    else:
        candidates.append(normalized.lstrip("+"))
        candidates.append(f"whatsapp:{normalized}")

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        deduped.append(item)
    return deduped


async def resolve_person_id_by_whatsapp_phone(phone: str) -> str | None:
    candidates = _build_phone_candidates(phone)
    if not candidates:
        return None

    clauses = " OR ".join(["phone = %s" for _ in candidates])
    row = await _query_row(
        _require_database_url(),
        f"SELECT id, phone FROM people WHERE ({clauses}) LIMIT 1",
        tuple(candidates),
    )
    return row["id"] if row else None


def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID((value or "").strip())
        return True
    except (TypeError, ValueError):
        return False


def _format_unknown(value: str | None) -> str:
    return (value or "").strip() or "unknown"


def _format_unavailable(value: str | None) -> str:
    return (value or "").strip() or "unavailable"


def _format_summary_text(summary_text: str | None) -> str:
    return summary_text.strip() if (summary_text or "").strip() else "No summary available."


def _safe_response_text(lines: list[str]) -> str:
    return "\n".join(line for line in lines if line is not None)


def _require_meeting_id(command: ParsedWhatsAppCommand) -> tuple[str | None, str | None]:
    if not command.argument:
        return None, f"Please send: {command.name} <meeting-id>"
    if not _is_valid_uuid(command.argument):
        return None, f"Please send: {command.name} <meeting-id>"
    return command.argument, None


async def handle_whatsapp_command(from_number: str, body: str) -> str:
    command = parse_whatsapp_command(body)
    log(f"Incoming WhatsApp body={body!r} command={command.name}", level="debug")

    person_id = await resolve_person_id_by_whatsapp_phone(from_number)
    if not person_id:
        return UNREGISTERED_PHONE_MESSAGE

    if command.name == "join":
        if not command.argument or not is_valid_google_meet_url(command.argument):
            return JOIN_HELP_MESSAGE

        try:
            capture = await request_meeting_capture(
                gmeet_url=command.argument,
                requested_by_person_id=person_id,
            )
            reply = _safe_response_text(
                [
                    "Meeting capture created.",
                    "",
                    f"Meeting ID: {capture.get('meeting_id')}",
                    f"Status: {capture.get('status')}",
                ]
            )
            return reply
        except AgentToolError as error:
            return f"{error.code}: {error.message}"

    if command.name == "status":
        meeting_id, error_message = _require_meeting_id(command)
        if error_message:
            return error_message
        try:
            payload = await get_meeting_capture_status(meeting_id)
            reply = _safe_response_text(
                [
                    f"Meeting status: {payload.get('status')}",
                    f"Started: {_format_unknown(payload.get('started_at'))}",
                    f"Ended: {_format_unknown(payload.get('ended_at'))}",
                ]
            )
            return reply
        except AgentToolError as error:
            return f"{error.code}: {error.message}"

    if command.name == "summary":
        meeting_id, error_message = _require_meeting_id(command)
        if error_message:
            return error_message
        try:
            payload = await get_meeting_intelligence(meeting_id)
            meta = payload.get("meta") or {}
            if not bool(meta.get("is_ready")):
                return _safe_response_text(
                    [
                        f"Meeting status: {payload.get('meeting', {}).get('status')}",
                        f"Message: {_format_unknown(meta.get('message'))}",
                    ]
                ).strip()

            summary_short = _format_summary_text(payload.get("meeting", {}).get("summary_short"))
            reply = _safe_response_text(
                [
                    "Summary:",
                    summary_short,
                    "",
                    f"Decisions: {meta.get('decision_count', 0)}",
                    f"Action items: {meta.get('action_item_count', 0)}",
                    f"Memories: {meta.get('memory_count', 0)}",
                ]
            )
            return reply
        except AgentToolError as error:
            return f"{error.code}: {error.message}"

    if command.name == "decisions":
        meeting_id, error_message = _require_meeting_id(command)
        if error_message:
            return error_message
        try:
            payload = await get_meeting_intelligence(meeting_id)
            meta = payload.get("meta") or {}
            if not bool(meta.get("is_ready")):
                return "Meeting intelligence is not ready."

            decisions = (payload.get("decisions") or [])[:5]
            if not decisions:
                return "No confirmed decisions were found for this meeting."

            lines = ["Decisions:", ""]
            for index, item in enumerate(decisions, start=1):
                lines.append(f"{index}. {item.get('decision_text') or item.get('title') or 'Decision'}")
            reply = _safe_response_text(lines)
            return reply
        except AgentToolError as error:
            return f"{error.code}: {error.message}"

    if command.name == "actions":
        meeting_id, error_message = _require_meeting_id(command)
        if error_message:
            return error_message
        try:
            payload = await get_meeting_intelligence(meeting_id)
            meta = payload.get("meta") or {}
            if not bool(meta.get("is_ready")):
                return "Meeting intelligence is not ready."

            action_items = (payload.get("action_items") or [])[:5]
            if not action_items:
                return "No action items were found for this meeting."

            lines = ["Action items:", ""]
            for index, item in enumerate(action_items, start=1):
                lines.extend(
                    [
                        f"{index}. {item.get('task') or 'Action item'}",
                        f"   Owner: {_format_unknown(item.get('owner_text'))}",
                        f"   Due: {_format_unknown(item.get('due_date'))}",
                    ]
                )
            reply = _safe_response_text(lines)
            return reply
        except AgentToolError as error:
            return f"{error.code}: {error.message}"

    if command.name == "recent":
        payload = await get_recent_meetings(limit=5)
        lines = ["Recent meetings:", ""]
        for index, item in enumerate(payload, start=1):
            lines.extend(
                [
                    f"{index}. {item.get('meeting_id')}",
                    f"   Status: {item.get('status')}",
                    f"   Summary: {_format_unavailable(item.get('summary_short'))}",
                ]
            )
        if len(lines) == 2:
            lines.append("No meetings found.")
        reply = _safe_response_text(lines)
        return reply

    if command.name == "open_actions":
        payload = await get_open_action_items(limit=10)
        lines = ["Open action items:", ""]
        for index, item in enumerate(payload, start=1):
            lines.extend(
                [
                    f"{index}. {item.get('task') or 'Action item'}",
                    f"   Owner: {_format_unknown(item.get('owner_text'))}",
                    f"   Due: {_format_unknown(item.get('due_date'))}",
                ]
            )
        if len(lines) == 2:
            lines.append("No open action items found.")
        reply = _safe_response_text(lines)
        return reply

    if command.name == "help" or command.name == "unknown":
        return HELP_TEXT

    return HELP_TEXT
