from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlparse
from html import escape

from orbit.core import ensure_browser_use_runtime, configure_dependency_logging, env_int, log, load_dotenv

ensure_browser_use_runtime(
    "scripts/whatsapp_bot.py",
    extra_imports=["fastapi", "twilio", "openai", "multipart", "websockets"],
)

from fastapi import FastAPI, Form, HTTPException, Request, Response, WebSocket  # noqa: E402
from twilio.request_validator import RequestValidator  # noqa: E402

from orbit.whatsapp_service import OrbitWhatsAppService  # noqa: E402
from orbit.meeting_intelligence_routes import router as meeting_intelligence_router  # noqa: E402
from orbit.agent.whatsapp.command_handler import handle_whatsapp_command  # noqa: E402


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
        log(
            f"ORBIT_WEBHOOK_HOST is a public URL. Binding locally to 0.0.0.0 instead of {parsed_host}.",
            level="debug",
        )
        return "0.0.0.0"
    return host


@asynccontextmanager
async def lifespan(app):
    load_dotenv()
    app.state.orbit_service = OrbitWhatsAppService()
    yield


app = FastAPI(lifespan=lifespan)
app.include_router(meeting_intelligence_router)


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
    ProfileName: str = Form(""),
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

    _ = ProfileName
    reply_text = await handle_whatsapp_command(From, Body)
    escaped_reply = escape((reply_text or "").strip())
    xml_body = f"<Response><Message>{escaped_reply}</Message></Response>" if escaped_reply else "<Response />"
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
    configure_dependency_logging()

    import os
    import uvicorn

    orbit_log_level = (os.environ.get("ORBIT_LOG_LEVEL", "important") or "important").strip().lower()
    uvicorn_log_level = {
        "quiet": "critical",
        "error": "error",
        "important": "warning",
        "info": "info",
        "debug": "debug",
    }.get(orbit_log_level, "warning")

    host = normalize_bind_host(os.environ.get("ORBIT_WEBHOOK_HOST", "0.0.0.0"))
    port = env_int("ORBIT_WEBHOOK_PORT", 8000)
    uvicorn.run(
        "orbit.whatsapp_app:app",
        host=host,
        port=port,
        reload=False,
        log_level=uvicorn_log_level,
    )
