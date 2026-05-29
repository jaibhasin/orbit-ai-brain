# Orbit

**Orbit is a source-backed recall layer for meetings and decisions.**

It joins Google Meets, watches meeting chat, can ingest meeting transcripts, stores organizational memory, and makes that memory queryable through AI agents. The current bootstrap control plane is a WhatsApp agent backed by FastAPI, browser automation, OpenAI, Groq Whisper-compatible transcription, and an optional Postgres + pgvector memory layer.

The product thesis is narrower than "a WhatsApp bot": Orbit is trying to answer what was decided, why, and what source evidence backs that answer. WhatsApp is the fastest shell around that workflow today, not the moat.

## Current Capabilities

- Joins Google Meet links through Browser Use + Chrome automation.
- Uses the configured Orbit display name on guest join screens.
- Prefers joining with microphone and camera disabled.
- Sends a short intro message after joining a meeting.
- Opens and monitors Google Meet chat.
- Captures visible Meet chat messages during the session.
- Imports multilingual transcript segments from saved audio or video files through Groq transcription.
- Normalizes transcript segments before writing them into persistent memory.
- Detects `@orbit` mentions inside Meet chat.
- Starts meetings from WhatsApp via Twilio.
- Sends WhatsApp status updates while meetings are running.
- Answers `@orbit ...` WhatsApp questions from live Meet chat context.
- Answers normal WhatsApp questions from persistent company memory when `DATABASE_URL` is configured.
- Labels normal WhatsApp answers as memory-backed recall or general fallback so users can tell when Orbit is grounded in stored company context.
- Stores Meet chat memory in Postgres + pgvector through a swappable memory boundary.

## Architecture

```text
WhatsApp / Twilio
      |
      v
FastAPI webhook
      |
      v
OrbitWhatsAppService
      |
      +--> Google Meet agent
      |       |
      |       v
      |   Browser Use + Chrome
      |       |
      |       v
      |   Meet chat capture
      |
      +--> MemoryService interface
              |
              v
        Postgres + pgvector
              |
              v
        OpenAI embeddings + RAG answers
```

The important design choice is that memory is behind `orbit/memory.py`. The current Postgres schema is intentionally v1 and replaceable. If memory organization changes later, the rest of the system should not need to know about table shapes, vector SQL, chunking, or ranking internals.

## Repository Map

```text
scripts/
  join_meet.py          Direct Google Meet runner
  whatsapp_bot.py       WhatsApp/FastAPI entrypoint
  transcribe_media.py   Transcript import from local audio/video

orbit/
  core.py               Env loading, logging, runtime helpers
  groq_transcriber.py   Groq Whisper-compatible transcription client
  meet.py               Google Meet browser automation + chat monitoring
  meet_types.py         Meeting/session/chat dataclasses
  whatsapp_app.py       FastAPI app and Twilio webhook route
  whatsapp_service.py   WhatsApp orchestration and agent behavior
  memory.py             Swappable memory service interface
  postgres_memory.py    Postgres + pgvector memory implementation
  transcript.py         Transcript dataclasses
  transcript_normalizer.py

tests/
  test_whatsapp_memory.py
```

## How It Works

### 1. Start a meeting from WhatsApp

Send a Google Meet link to the configured WhatsApp number:

```text
https://meet.google.com/abc-defg-hij
```

Orbit starts a browser session, attempts to join the meeting, and sends status updates back to WhatsApp.

### 2. Capture meeting chat

After joining, Orbit opens the Google Meet chat panel, sends an intro message, scans visible chat, and polls for new chat messages.

Captured messages are stored in memory when persistent memory is enabled.

### 3. Ask live meeting questions

Use `@orbit` or `orbit:` on WhatsApp to ask about currently active Meet chat context:

```text
@orbit what did they decide about launch timing?
```

This path only uses live captured Meet chat.

### 4. Import a meeting recording

Transcribe a local audio or video file with Groq and ingest the normalized transcript into Orbit memory:

```bash
source .venv-browser-use/bin/activate
python scripts/transcribe_media.py ./recordings/demo-meeting.m4a --meet-url https://meet.google.com/abc-defg-hij
```

This writes a debug transcript JSON file under `debug/transcripts/` and, when `DATABASE_URL` is configured, indexes transcript segments into the same memory layer used by WhatsApp Q&A.

### 5. Ask company-memory questions

Send a normal WhatsApp question without `@orbit`:

```text
what did we last discuss about onboarding?
```

Orbit searches persistent company memory, generates an answer from retrieved context, and includes short source labels when available.

Normal WhatsApp answers now expose the answer mode:

- `Answer mode: memory-backed recall` means the response is grounded in stored Orbit memory.
- `Answer mode: general fallback` means Orbit could not find enough stored company context, so the reply is a general model answer instead.

## Setup

Use Python `3.12` or `3.13`. Browser Use currently does not work reliably in this setup under Python `3.14`.

```bash
python3.12 -m venv .venv-browser-use
source .venv-browser-use/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Create your environment file:

```bash
cp .env.example .env
```

Fill in the required values:

```text
OPENAI_API_KEY=...
OPENAI_MODEL=gpt-5.4-mini
OPENAI_EMBEDDING_MODEL=text-embedding-3-small

TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
TWILIO_WHATSAPP_FROM=whatsapp:+14155238886
TWILIO_ALLOWED_FROM=whatsapp:+15551234567

ORBIT_WEBHOOK_HOST=0.0.0.0
ORBIT_WEBHOOK_PORT=8000
ORBIT_MAX_PARALLEL_MEETINGS=3
ORBIT_MEMORY_SEARCH_LIMIT=6

DATABASE_URL=postgresql://orbit:orbit@localhost:5432/orbit
```

`DATABASE_URL` is optional. If it is missing, Orbit still runs, but persistent company-memory Q&A is disabled.

## Postgres Memory

Persistent memory requires Postgres with the `vector` extension available.

For local development, start the included pgvector database:

```bash
docker compose up -d orbit-postgres
```

Then set this in `.env` and restart the WhatsApp agent:

```text
DATABASE_URL=postgresql://orbit:orbit@localhost:5432/orbit
```

Orbit creates its v1 tables automatically on first memory use:

- `orbit_meet_sessions`
- `orbit_chat_messages`
- `orbit_memory_chunks`

The schema stores meeting/session metadata, raw captured chat messages, and vectorized memory chunks. This schema is deliberately treated as replaceable while the product memory model evolves.

## Run

Direct Google Meet runner:

```bash
source .venv-browser-use/bin/activate
python scripts/join_meet.py
```

WhatsApp agent:

```bash
source .venv-browser-use/bin/activate
python scripts/whatsapp_bot.py
```

Expose the FastAPI webhook for Twilio:

```bash
ngrok http 8000
```

Configure the Twilio WhatsApp webhook as:

```text
POST https://your-ngrok-domain/twilio/whatsapp
```

The app also accepts this equivalent inbound URL:

```text
POST https://your-ngrok-domain/api/whatsapp/inbound
```

If port `8000` is busy, use another port:

```bash
ORBIT_WEBHOOK_PORT=8001 python scripts/whatsapp_bot.py
ngrok http 8001
```

## WhatsApp Commands

```text
https://meet.google.com/abc-defg-hij
```

Starts Orbit for that meeting.

```text
@orbit what is happening in the meeting?
```

Answers from live captured Meet chat.

```text
what did we discuss about hiring?
```

Answers from persistent company memory.

Fallback/debug live audio stream from a PulseAudio/PipeWire monitor source:

```bash
.venv-browser-use/bin/python scripts/stream_monitor_audio.py \
  ws://127.0.0.1:8000/internal/audio-stream/<session-id> \
  --source default
```

## Configuration

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | OpenAI API key for chat and embeddings |
| `OPENAI_MODEL` | Chat model used by Orbit |
| `OPENAI_EMBEDDING_MODEL` | Embedding model for memory search |
| `DEEPGRAM_API_KEY` | Deepgram API key for live STT; backend only, never stored in the extension |
| `DEEPGRAM_LIVE_MODEL` | Deepgram live STT model, default `nova-3` |
| `ORBIT_LIVE_STT_ENABLED` | Enable live Meet audio transcription, defaults on when `DEEPGRAM_API_KEY` is set |
| `ORBIT_AUDIO_WS_BASE_URL` | Local WebSocket base URL for extension audio, default `ws://127.0.0.1:8000` |
| `ORBIT_CHROME_EXTENSION_PATH` | Unpacked MV3 extension path, default `extension/orbit-audio-capture` |
| `ORBIT_CHROME_CDP_URL` | Existing headed Chrome CDP URL for browser-use to connect to |
| `ORBIT_EXTENSION_CAPTURE_SHORTCUT` | Shortcut used to activate extension capture, default `Alt+Shift+O` |
| `GROQ_API_KEY` | Groq API key for transcript import |
| `GROQ_TRANSCRIPTION_MODEL` | Groq speech-to-text model, default `whisper-large-v3-turbo` |
| `DATABASE_URL` | Enables Postgres + pgvector memory |
| `TWILIO_ACCOUNT_SID` | Twilio account SID |
| `TWILIO_AUTH_TOKEN` | Twilio auth token |
| `TWILIO_WHATSAPP_FROM` | Twilio WhatsApp sender |
| `TWILIO_ALLOWED_FROM` | Only this WhatsApp sender can control Orbit |
| `ORBIT_WEBHOOK_HOST` | FastAPI bind host |
| `ORBIT_WEBHOOK_PORT` | FastAPI bind port |
| `ORBIT_MAX_PARALLEL_MEETINGS` | Meeting concurrency limit |
| `ORBIT_MEMORY_SEARCH_LIMIT` | Number of memory chunks retrieved for RAG |
| `GMEET_DISPLAY_NAME` | Name Orbit uses in Google Meet |
| `GMEET_WAIT_AFTER_JOIN_MS` | How long to monitor after joining |
| `GMEET_USE_SYSTEM_CHROME` | Use installed Chrome profile instead of managed browser |
| `HEADLESS` | Run browser in headless mode |

## Development

Run tests:

```bash
.venv-browser-use/bin/python -m unittest discover -s tests
```

Compile-check the core modules:

```bash
.venv-browser-use/bin/python -m py_compile \
  orbit/core.py \
  orbit/meet.py \
  orbit/meet_types.py \
  orbit/memory.py \
  orbit/postgres_memory.py \
  orbit/whatsapp_app.py \
  orbit/whatsapp_service.py \
  orbit/groq_transcriber.py \
  orbit/transcript.py \
  orbit/transcript_normalizer.py
```

## Current Limits

- Orbit reads Google Meet chat live and can request live audio transcription through the local Chrome extension.
- The primary live STT path is headed Chrome under a virtual display, browser-use over CDP, extension tab audio capture, and backend-owned Deepgram streaming.
- PulseAudio/PipeWire monitor capture is a fallback/debug path, not the primary architecture.
- Speaker attribution is best effort. Google Meet caption scraping can enrich speaker names, but selectors are unstable and failures do not block Deepgram transcript storage.
- Persistent memory indexes both captured Meet chat and imported transcript segments.
- Slack, email, document ingestion, dashboards, multi-company tenancy, and auth are future layers.
- Google Meet UI changes may require selector updates.
- Orbit does not bypass Google Meet, WhatsApp, or company access controls.
- If a meeting requires host approval or a signed-in invited account, Orbit waits or reports the block.

## Direction

Orbit is moving toward an agent-native company operating layer:

- Reliable meeting agents
- Persistent organizational memory
- Retrieval over company activity
- Agent workflows across meetings, chat, docs, Slack, and email
- Queryable context for every team

The current repo is the foundation: Meet agent + WhatsApp control plane + swappable memory + RAG.
