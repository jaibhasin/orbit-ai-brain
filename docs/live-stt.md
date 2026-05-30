# Live STT

Orbit's live STT path captures Google Meet tab audio locally and streams it through the Orbit backend to Deepgram.

## Data Flow

```text
browser-use joins Meet
  -> Meet captions are enabled if possible
  -> Browser Use posts capture config into the Meet tab
  -> content script stores config and injects "Start Orbit audio"
  -> extension action, shortcut, or button calls chrome.tabCapture
  -> offscreen document converts tab audio to mono PCM16
  -> extension streams audio to Orbit local WebSocket
  -> Orbit forwards audio to Deepgram
  -> final Deepgram segments are normalized
  -> optional Meet caption speaker names are merged
  -> transcript memory is stored
```

Deepgram is the source of transcript text. Google Meet captions are only a best-effort speaker attribution layer.

## Security Boundary

- The Deepgram API key is owned by the Orbit backend.
- The extension never receives the Deepgram API key.
- The extension receives only a local Orbit WebSocket URL.
- Audio WebSocket URLs include a per-session token.
- Unknown sessions and invalid tokens are rejected before the WebSocket is accepted.

## Local Setup

Install dependencies and Playwright:

```bash
python3.12 -m venv .venv-browser-use
source .venv-browser-use/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
```

Set live STT environment variables:

```text
DEEPGRAM_API_KEY=...
DEEPGRAM_LIVE_MODEL=nova-3
ORBIT_LIVE_STT_ENABLED=true
ORBIT_AUDIO_WS_BASE_URL=ws://127.0.0.1:8000
ORBIT_CHROME_EXTENSION_PATH=extension/orbit-audio-capture
ORBIT_EXTENSION_CAPTURE_SHORTCUT=Alt+Shift+O
```

Start the FastAPI/WhatsApp app locally:

```bash
source .venv-browser-use/bin/activate
python scripts/whatsapp_bot.py
```

Then start a meeting through WhatsApp by sending a Meet URL.

## Chrome And Extension

For local development, the extension is loaded unpacked from `extension/orbit-audio-capture`. There is no Chrome Web Store deployment step.

When using `ORBIT_CHROME_CDP_URL` with an already-running Chrome process, load the unpacked extension manually first:

1. Open `chrome://extensions`.
2. Enable Developer mode.
3. Choose **Load unpacked**.
4. Select `extension/orbit-audio-capture`.

Use the same manual setup with `GMEET_USE_SYSTEM_CHROME=true`. Official Chrome-branded builds removed command-line unpacked-extension loading starting in Chrome 137. Browser Use managed Chromium sessions can still load the extension through launch arguments.

The extension is Manifest V3 and uses:

- `service_worker.js` for config storage and `chrome.tabCapture.getMediaStreamId`.
- `offscreen.html` and `offscreen.js` for `getUserMedia`, PCM conversion, and WebSocket streaming.
- `content.js` for receiving Orbit config inside the Meet tab and injecting the manual activation button.

Chrome requires the user to invoke an extension before tab capture starts. After joining, Orbit posts the capture config and tries a browser mouse click on the injected `Start Orbit audio` button. Orbit waits for the extension callback instead of treating the click as success. If the button is unavailable or Chrome rejects the request, Orbit attempts the configured shortcut. If capture still does not start, use the extension action icon or the configured `Alt+Shift+O` command manually.

## Browser Use With CDP

If you want Browser Use to connect to an already-running headed Chrome instance, launch Chrome with remote debugging and set:

```text
ORBIT_CHROME_CDP_URL=http://127.0.0.1:9222
```

On a VM later, run headed Chrome under a virtual display such as xvfb and connect Browser Use over CDP. For current local development, a normal visible Chrome session is enough.

## Audio Format

The extension requests a 16 kHz `AudioContext` and sends:

- encoding: `linear16`
- sample rate: actual `AudioContext.sampleRate`
- channels: `1`
- payload: raw PCM16 little-endian binary chunks

Orbit sends the same format values to Deepgram for the live stream. If Chrome chooses a different actual sample rate, the extension reports that actual rate in the initial WebSocket `start` message.

## Stored Transcript Fields

Final transcript segments are normalized and stored with nullable speaker fields:

- `text`
- `start_time`
- `end_time`
- `speaker_name`
- `speaker_label`
- `speaker_source`
- `speaker_confidence`
- `meeting_id`

In code, these map through `TranscriptSegment` fields and the Postgres memory tables.

## Speaker Attribution

Speaker names are not a hard dependency.

The core path works with only tab audio and Deepgram transcript text. If Google Meet captions are available, Orbit scrapes visible caption text and speaker names, then merges names into matching Deepgram final segments.

Caption scraping is unofficial and unstable because Google Meet DOM selectors can change. If caption scraping fails, Orbit continues storing Deepgram transcript segments normally.

## Fallback Monitor Capture

The fallback script is for debugging only:

```bash
.venv-browser-use/bin/python scripts/stream_monitor_audio.py \
  'ws://127.0.0.1:8000/internal/audio-stream/<session-id>?token=<session-token>' \
  --source default
```

Use this only when debugging local PulseAudio/PipeWire monitor sources. It is not the primary architecture.

## Known Limits

- Orbit reports capture as requested first, then sends a separate confirmation only after the backend connects to Deepgram and forwards the first audio chunk.
- A real headed Chrome smoke test is still required to prove `chrome.tabCapture` works on a specific machine/profile.
- Duplicate extension WebSocket connections for one session are not yet fully coordinated.
- The offscreen extension currently captures one Meet tab audio stream at a time. Starting another capture replaces the existing capture.
- Google Meet caption selectors can break without warning.
- If the direct `scripts/join_meet.py` runner is used without the FastAPI app, set `ORBIT_LIVE_STT_ENABLED=false`.

## Test Coverage

Covered by unit tests:

- Deepgram live URL and final event parsing.
- Interim Deepgram event ignoring.
- Live STT final segment normalization and memory writes.
- Caption speaker merge success and failure.
- Caption buffering before audio session creation.
- Extension audio WebSocket chunk forwarding.
- Unknown session and bad-token WebSocket rejection.

Not covered by automated tests yet:

- Real Chrome `tabCapture`.
- Real Google Meet caption DOM behavior.
- Real Deepgram WebSocket integration.
