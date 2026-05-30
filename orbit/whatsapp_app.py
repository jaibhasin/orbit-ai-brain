from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse

from orbit.core import ensure_browser_use_runtime, env_int, load_dotenv

ensure_browser_use_runtime(
    "scripts/whatsapp_bot.py",
    extra_imports=["fastapi", "twilio", "openai", "multipart", "websockets"],
)

from fastapi import FastAPI, Form, HTTPException, Request, Response, WebSocket  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

from orbit.whatsapp_service import OrbitWhatsAppService  # noqa: E402


class TwiMLResponse(Response):
    media_type = "application/xml"


TWIML_EXAMPLE = """<?xml version="1.0" encoding="UTF-8"?><Response />"""
TWIML_RESPONSE_DOC: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Twilio TwiML response",
        "content": {
            "application/xml": {
                "example": TWIML_EXAMPLE,
            }
        },
    }
}


def is_valid_twilio_signature(url, params, signature, auth_token):
    if not signature or not auth_token:
        return False
    validator = RequestValidator(auth_token)
    return validator.validate(url, params, signature)


def normalize_bind_host(raw_host):
    host = (raw_host or "0.0.0.0").strip()
    if "://" in host:
        parsed_host = urlparse(host).hostname or host
        print(
            "ORBIT_WEBHOOK_HOST is a public URL. "
            f"Binding locally to 0.0.0.0 instead of {parsed_host}."
        )
        return "0.0.0.0"
    return host


@asynccontextmanager
async def lifespan(app):
    load_dotenv()
    app.state.orbit_service = OrbitWhatsAppService()
    yield


app = FastAPI(lifespan=lifespan)


@app.get("/", summary="Orbit webhook status")
async def root_status():
    return {
        "status": "ok",
        "service": "orbit-whatsapp-webhook",
        "docs_url": "/docs",
        "webhook_paths": [
            "/twilio/whatsapp",
            "/api/whatsapp/inbound",
        ],
        "audio_stream_path": "/internal/audio-stream/{session_id}",
    }


@app.websocket("/internal/audio-stream/{session_id}")
async def live_audio_stream(websocket: WebSocket, session_id: str):
    service = websocket.app.state.orbit_service
    await service.handle_audio_stream(websocket, session_id)


async def handle_whatsapp_webhook(
    request: Request,
    From: str = Form(""),
    Body: str = Form(""),
):
    form = await request.form()
    service = request.app.state.orbit_service
    signature = request.headers.get("X-Twilio-Signature", "").strip()
    if not is_valid_twilio_signature(
        str(request.url),
        dict(form),
        signature,
        service.twilio_auth_token,
    ):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature.")

    xml_body = await service.handle_incoming_message(From, Body)
    return TwiMLResponse(content=xml_body)


@app.post(
    "/twilio/whatsapp",
    summary="Twilio WhatsApp inbound webhook",
    operation_id="twilio_whatsapp_webhook",
    response_class=TwiMLResponse,
    response_description="Twilio TwiML response",
    responses=TWIML_RESPONSE_DOC,
)
async def twilio_whatsapp_webhook(
    request: Request,
    From: str = Form(""),
    Body: str = Form(""),
):
    return await handle_whatsapp_webhook(request, From=From, Body=Body)


@app.post(
    "/api/whatsapp/inbound",
    summary="Legacy WhatsApp inbound webhook alias",
    operation_id="api_whatsapp_inbound_webhook",
    response_class=TwiMLResponse,
    response_description="Twilio TwiML response",
    responses=TWIML_RESPONSE_DOC,
)
async def api_whatsapp_inbound_webhook(
    request: Request,
    From: str = Form(""),
    Body: str = Form(""),
):
    return await handle_whatsapp_webhook(request, From=From, Body=Body)


def main():
    load_dotenv()

    import os
    import uvicorn

    host = normalize_bind_host(os.environ.get("ORBIT_WEBHOOK_HOST", "0.0.0.0"))
    port = env_int("ORBIT_WEBHOOK_PORT", 8000)
    uvicorn.run("orbit.whatsapp_app:app", host=host, port=port, reload=False)
