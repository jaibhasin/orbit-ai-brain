from __future__ import annotations

from orbit.agent.tools._shared import (
    NotFoundError,
    _normalize_phone_for_whatsapp,
    _query_row,
    _require_database_url,
    _require_uuid,
    _run_sync,
)


def send_whatsapp_reply(person_id: str, text: str) -> dict:
    person_id = _require_uuid(
        person_id,
        field_name="person_id",
        error_code="INVALID_PERSON_ID",
        required_message="person_id must be a valid UUID.",
    )
    message_text = (text or "").strip()
    if not message_text:
        raise ValueError("text must be non-empty.")

    async def _handler():
        database_url = _require_database_url()
        person = await _query_row(
            database_url,
            """
            SELECT id, phone
            FROM people
            WHERE id = %s
            LIMIT 1
            """,
            (person_id,),
        )
        if not person or not person.get("phone"):
            raise NotFoundError(
                code="PERSON_NOT_FOUND",
                message="Person not found.",
            )

        to_number = _normalize_phone_for_whatsapp(person["phone"])
        if not to_number:
            raise ValueError("Person has no valid phone number.")

        sender = _build_twilio_sender()
        if sender is None:
            return {
                "status": "not_implemented",
                "provider_message_id": None,
                "error": "Twilio sender is not implemented yet.",
            }

        messages = sender.get("messages")
        if messages is None:
            return {
                "status": "not_implemented",
                "provider_message_id": None,
                "error": "Twilio sender is not implemented yet.",
            }

        try:
            result = messages.create(
                body=message_text,
                from_=sender["from_number"],
                to=to_number,
            )
            return {
                "status": "sent" if getattr(result, "sid", None) else "queued",
                "provider_message_id": getattr(result, "sid", None),
                "error": None,
            }
        except Exception as error:
            return {
                "status": "failed",
                "provider_message_id": None,
                "error": str(error),
            }

    return _run_sync(_handler())


def _build_twilio_sender():
    try:
        from orbit.whatsapp_service import OrbitWhatsAppService
    except Exception:
        return None

    try:
        service = OrbitWhatsAppService()
    except Exception:
        return None

    twilio_client = getattr(service, "twilio_client", None)
    from_number = getattr(service, "twilio_whatsapp_from", None)
    if not twilio_client or not from_number:
        return None

    return {
        "messages": getattr(twilio_client, "messages", None),
        "from_number": from_number,
    }
