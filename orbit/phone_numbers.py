from __future__ import annotations


def normalize_whatsapp_phone(value: str | None) -> str:
    normalized = (value or "").strip().replace(" ", "")
    if normalized.startswith("whatsapp:"):
        normalized = normalized[len("whatsapp:") :]
    if normalized and not normalized.startswith("+") and normalized.isdigit():
        normalized = f"+{normalized}"
    return normalized
