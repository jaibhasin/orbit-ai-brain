# Google Meet Automation

This repo now has two Python entry points:

- `join_meet.py`: direct Playwright flow with explicit selectors and debug capture
- `join_meet_browser_use.py`: Browser Use agent powered by OpenAI `gpt-5.4-mini` by default

## What it does

- Opens Google Meet in Chrome
- Fills your guest display name when guest join is available
- Prefers joining with mic and camera disabled
- Waits after the join attempt so you can inspect the result
- Saves Browser Use run artifacts under `debug/browser-use/`
- Defaults to a guest-friendly browser session that does not depend on your local Chrome profile

## Setup

1. Create a virtual environment:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

   For `join_meet_browser_use.py`, prefer Python `3.12` or `3.13`. The current `browser-use` package aborts in this macOS setup under Python `3.14`.

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

3. Edit `.env`:

   ```bash
   GMEET_URL=https://meet.google.com/your-meeting-code
   GMEET_DISPLAY_NAME=Orbit
   GMEET_WAIT_AFTER_JOIN_MS=120000
   HEADLESS=false
   OPENAI_API_KEY=your_openai_api_key
   OPENAI_MODEL=gpt-5.4-mini
   GMEET_USE_SYSTEM_CHROME=false
   GMEET_BROWSER_USE_MAX_STEPS=20
   ```

4. Use a Meet link you already have permission to join.

## Run

```bash
source .venv/bin/activate
python join_meet.py
```

Or run the Browser Use version:

```bash
source .venv/bin/activate
python join_meet_browser_use.py
```

## Notes

- This relies on Google Meet's current DOM and button labels. UI changes will require selector updates.
- `channel="chrome"` expects Google Chrome to be installed locally.
- `join_meet_browser_use.py` now defaults to `GMEET_USE_SYSTEM_CHROME=false`, so it uses a managed browser and follows the guest join flow on both laptops and VMs.
- `join_meet_browser_use.py` defaults to `OPENAI_MODEL=gpt-5.4-mini`. You can override it in `.env` if you want to compare models.
- If you want to reuse a signed-in local Chrome profile instead, set `GMEET_USE_SYSTEM_CHROME=true`. You can also set `GMEET_CHROME_PROFILE_DIRECTORY=Default` or another Chrome profile name.
- Some meetings require host approval or a signed-in invited account. Neither script bypasses Google Meet access controls.
