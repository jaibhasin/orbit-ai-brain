from __future__ import annotations

import re
from dataclasses import dataclass


MEET_LINK_PATTERN = re.compile(
    r"^https?://(?:www\.)?meet\.google\.com/[a-z0-9-]+(?:/[^\s]*)?(?:\?.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedWhatsAppCommand:
    name: str
    argument: str | None = None


def _strip_command_padding(text: str) -> str:
    return (text or "").strip()


def _strip_link(raw_url: str) -> str:
    return (raw_url or "").strip().rstrip(").,!?:;]>\"'")


def is_valid_google_meet_url(url: str) -> bool:
    cleaned = _strip_link(url or "")
    return bool(MEET_LINK_PATTERN.match(cleaned))


def parse_whatsapp_command(raw_text: str) -> ParsedWhatsAppCommand:
    text = _strip_command_padding(raw_text)
    if not text:
        return ParsedWhatsAppCommand(name="help")

    lowered = text.lower()
    if lowered == "help":
        return ParsedWhatsAppCommand(name="help")
    if lowered == "recent":
        return ParsedWhatsAppCommand(name="recent")
    if lowered == "open actions":
        return ParsedWhatsAppCommand(name="open_actions")

    first, sep, rest = text.partition(" ")
    command = first.lower()

    if command == "join":
        return ParsedWhatsAppCommand(name="join", argument=_strip_link(rest))
    if command == "status":
        return ParsedWhatsAppCommand(name="status", argument=_strip_command_padding(rest))
    if command == "summary":
        return ParsedWhatsAppCommand(name="summary", argument=_strip_command_padding(rest))
    if command == "decisions":
        return ParsedWhatsAppCommand(name="decisions", argument=_strip_command_padding(rest))
    if command == "actions":
        return ParsedWhatsAppCommand(name="actions", argument=_strip_command_padding(rest))
    if command == "help":
        return ParsedWhatsAppCommand(name="help")

    return ParsedWhatsAppCommand(name="unknown")


HELP_TEXT = """Available commands:

join <meet-link>
status <meeting-id>
summary <meeting-id>
decisions <meeting-id>
actions <meeting-id>
recent
open actions
help"""

