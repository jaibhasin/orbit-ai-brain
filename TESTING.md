# Testing

Orbit uses Python's built-in `unittest` framework for backend behavior tests. CI also runs Ruff, mypy, Python compilation checks, and Chrome extension syntax validation.

## Setup

Create or activate a Python 3.12 virtual environment, then install development dependencies:

```bash
python -m pip install -r requirements-dev.txt
```

The existing local Browser Use environment can also be used:

```bash
.venv-browser-use/bin/python -m pip install -r requirements-dev.txt
```

## Run Tests

Run the full Python suite:

```bash
python -m unittest discover -s tests
```

Run one test module while iterating:

```bash
python -m unittest tests.test_meet_chat
```

## Run CI Checks Locally

```bash
python -m ruff check orbit scripts tests
python -m mypy
python -m unittest discover -s tests
python -m compileall -q orbit scripts
node --check extension/orbit-audio-capture/content.js
node --check extension/orbit-audio-capture/service_worker.js
node --check extension/orbit-audio-capture/offscreen.js
python -m json.tool extension/orbit-audio-capture/manifest.json >/dev/null
```

## Test Layers

- Unit tests live in `tests/test_*.py` and cover transcript parsing, normalization, caption attribution, configuration helpers, and chat behavior.
- Integration-style tests exercise the extension audio WebSocket handler with fake WebSocket and STT services.
- Chrome extension files receive syntax and manifest validation in CI.
- A real headed-Chrome smoke test is still required for `chrome.tabCapture`, Google Meet DOM behavior, and live Deepgram streaming.

## Conventions

- Name files `tests/test_<module>.py`.
- Use `unittest.TestCase` or `unittest.IsolatedAsyncioTestCase`.
- Assert observable behavior and state transitions, not only that a function returns.
- Mock external services such as Twilio, OpenAI, Deepgram, and browser APIs.
- Add a regression test whenever a bug fix adds or changes a branch.
