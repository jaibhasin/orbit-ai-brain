# Orbit Meet + WhatsApp Automation

This repo now supports two runtimes:

- `scripts/join_meet.py`: direct Google Meet join from `GMEET_URL`
- `scripts/whatsapp_bot.py`: Twilio WhatsApp webhook that starts Meet joins from chat and answers WhatsApp Q&A from live Meet chat context

## Code structure

- `scripts/join_meet.py`: public direct Meet entrypoint
- `scripts/whatsapp_bot.py`: public WhatsApp/Twilio entrypoint
- `orbit/core.py`: shared env loading, logging, Python-version bootstrap, and Meet-code parsing
- `orbit/meet_types.py`: shared dataclasses for Meet state, messages, callbacks, and session config
- `orbit/meet.py`: Browser Use Google Meet session runner and Meet chat DOM automation
- `orbit/whatsapp_app.py`: FastAPI webhook app
- `orbit/whatsapp_service.py`: WhatsApp orchestration, Twilio replies, parallel session tracking, and Q&A

## What Orbit does

- Opens Google Meet in Chrome through Browser Use
- Fills the guest display name when guest join is available
- Prefers joining with mic and camera disabled
- Sends a short introduction message in Meet chat after joining
- Monitors Meet chat after joining
- Grants one pending speak permission whenever chat contains `@orbit`, case-insensitively
- Scans visible Meet chat history once at startup, then watches new messages during the configured wait window
- Accepts Meet links from one configured WhatsApp sender through Twilio
- Runs up to 3 Meet sessions in parallel by default
- Answers WhatsApp `@orbit ...` questions using captured Meet chat from active sessions only

## Setup

1. Create a Python `3.12` or `3.13` virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   `browser-use` currently fails in this setup under Python `3.14`.

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

3. Create `.env` from the example and fill in your values:

   ```bash
   cp .env.example .env
   ```

4. For WhatsApp mode, expose the local webhook with ngrok and point Twilio to:

   ```text
   POST https://your-ngrok-domain/twilio/whatsapp
   ```

## Run

Direct Meet mode:

```bash
source .venv/bin/activate
python scripts/join_meet.py
```

WhatsApp mode:

```bash
source .venv/bin/activate
python scripts/whatsapp_bot.py
```

## WhatsApp behavior

- Orbit only listens to `TWILIO_ALLOWED_FROM`.
- A WhatsApp message containing a Meet link starts a new session if capacity is available.
- If the same Meet link is already active, Orbit does not start it again.
- If `ORBIT_MAX_PARALLEL_MEETINGS` sessions are already active, Orbit rejects additional links.
- WhatsApp Q&A must start with `@orbit` or `orbit:`.
- Q&A uses Meet chat captured from all active sessions. Orbit does not claim audio transcription or recording intelligence it does not have.

## Notes

- This automation relies on Google Meet's current DOM and button labels. UI changes will require selector updates.
- `GMEET_USE_SYSTEM_CHROME=false` uses a managed browser that is friendlier to guest joins.
- If you want to reuse a signed-in local Chrome profile instead, set `GMEET_USE_SYSTEM_CHROME=true`.
- Mention matching is fixed to the exact token `@orbit`, case-insensitive. Bare `orbit` does not count.
- Some meetings require host approval or a signed-in invited account. Orbit does not bypass Google Meet or WhatsApp platform access controls.
