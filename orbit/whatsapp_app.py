from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlparse

from orbit.core import ensure_browser_use_runtime, env_int, load_dotenv

ensure_browser_use_runtime(
    "scripts/whatsapp_bot.py",
    extra_imports=["fastapi", "twilio", "openai", "multipart"],
)

from fastapi import FastAPI, Form, Request, Response

from orbit.whatsapp_service import OrbitWhatsAppService


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


@app.post("/twilio/whatsapp")
async def whatsapp_webhook(
    request: Request,
    From: str = Form(""),
    Body: str = Form(""),
):
    service = request.app.state.orbit_service
    xml_body = await service.handle_incoming_message(From, Body)
    return Response(content=xml_body, media_type="application/xml")


def main():
    load_dotenv()

    import os
    import uvicorn

    host = normalize_bind_host(os.environ.get("ORBIT_WEBHOOK_HOST", "0.0.0.0"))
    port = env_int("ORBIT_WEBHOOK_PORT", 8000)
    uvicorn.run("orbit.whatsapp_app:app", host=host, port=port, reload=False)
