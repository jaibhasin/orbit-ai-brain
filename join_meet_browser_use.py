import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


ENV_PATH = Path(".env")
DEBUG_DIR = Path("debug")
CONVERSATION_DIR = DEBUG_DIR / "browser-use"
BROWSER_USE_VENV_DIR = Path(".venv-browser-use")
BROWSER_USE_SENTINEL = "BROWSER_USE_PYTHON_BOOTSTRAPPED"
POLL_INTERVAL_MS = 3000
ORBIT_MENTION_PATTERN = re.compile(r"(?<!\w)@orbit(?!\w)", re.IGNORECASE)


def load_dotenv():
    if not ENV_PATH.exists():
        return

    for raw_line in ENV_PATH.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')

        if key and key not in os.environ:
            os.environ[key] = value


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return int(value)


def log(message):
    print(f"[browser-use-meet] {message}")


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def normalize_message_text(text):
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class PermissionEvent:
    granted_at: str
    author: str
    message_text: str
    fingerprint: str


@dataclass
class ChatMessage:
    fingerprint: str
    raw_text: str
    normalized_text: str
    author: str
    timestamp_text: str


@dataclass
class MeetingState:
    joined_at: str | None = None
    chat_monitor_started_at: str | None = None
    seen_message_fingerprints: set[str] = field(default_factory=set)
    pending_speak_permissions: int = 0
    permission_events: list[PermissionEvent] = field(default_factory=list)
    introduction_sent: bool = False


def find_supported_python():
    candidates = [
        "python3.13",
        "python3.12",
        "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3",
        "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3",
        "/usr/local/bin/python3.13",
        "/usr/local/bin/python3.12",
        "/opt/homebrew/bin/python3.13",
        "/opt/homebrew/bin/python3.12",
    ]

    for candidate in candidates:
        resolved = shutil.which(candidate) if "/" not in candidate else candidate
        if not resolved or not Path(resolved).exists():
            continue

        try:
            version = subprocess.check_output(
                [resolved, "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
                text=True,
            ).strip()
        except Exception:
            continue

        if version in {"3.12", "3.13"}:
            return resolved

    return None


def venv_python_path():
    return BROWSER_USE_VENV_DIR / "bin" / "python"


def ensure_browser_use_venv():
    if sys.version_info < (3, 14):
        return

    if os.environ.get(BROWSER_USE_SENTINEL) == "1":
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "Browser Use re-launch was attempted, but the interpreter is still "
            f"Python {version}. Check the dedicated venv at "
            f"{BROWSER_USE_VENV_DIR}."
        )

    supported_python = find_supported_python()
    if not supported_python:
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "browser-use currently fails in this setup on Python "
            f"{version}. Install Python 3.12 or 3.13, then re-run this script."
        )

    target_python = venv_python_path()
    if not target_python.exists():
        log(f"Creating Browser Use venv with {supported_python}")
        subprocess.run(
            [supported_python, "-m", "venv", str(BROWSER_USE_VENV_DIR)],
            check=True,
        )

    try:
        subprocess.run(
            [
                str(target_python),
                "-c",
                "import browser_use, playwright",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "The Browser Use venv exists but dependencies are missing. Run "
            f"`{target_python} -m pip install -r requirements.txt` and retry."
        )

    log(f"Re-launching with {target_python}")
    relaunched_env = os.environ.copy()
    relaunched_env[BROWSER_USE_SENTINEL] = "1"
    os.execve(
        str(target_python),
        [str(target_python), str(Path(__file__).resolve())],
        relaunched_env,
    )


def build_task(meet_url, display_name):
    return f"""
Open this Google Meet URL: {meet_url}

This automation is only allowed to join meetings I already have permission to join. Do not attempt to bypass sign-in, host approval, guest restrictions, or any Google security control.

Steps:
1. Stay on the Google Meet page for this URL.
2. If a modal asks whether people should see or hear you, click "Continue without microphone and camera". If that exact button is not available, choose the option that keeps microphone and camera disabled.
3. If a visible guest name field exists, fill it with "{display_name}".
4. If microphone or camera toggles are on in the pre-join screen, turn them off.
5. Click the best available join button, preferring "Ask to join", then "Join now", then "Request to join", then "Join".
6. Treat the guest pre-join area as the source of truth. If the page shows a name input and a join button, continue the guest join flow even if a top-right "Sign in" link, tooltip, or helper bubble is also visible.
7. Do not treat a generic top-right "Sign in" link or tooltip as a blocking condition by itself. Only treat sign-in as blocking if the main page content explicitly says sign-in is required or the meeting cannot be joined without it.
8. If the page says host approval is required, the request was denied, or the meeting cannot be joined, stop and report the exact visible reason.
9. After clicking the join button, remain on the meeting page and do not navigate away.
10. Finish only after you have either joined successfully or clearly determined that Google Meet blocked entry.
""".strip()


def build_intro_message(display_name):
    return (
        f"Hi everyone, I’m Orbit. "
        "I’ll be recording and monitoring this meeting, and I can help answer questions."
        "Mention @orbit in chat to get my attention."
    )


def assert_supported_python():
    if sys.version_info >= (3, 14):
        version = ".".join(str(part) for part in sys.version_info[:3])
        raise RuntimeError(
            "browser-use currently fails in this setup on Python "
            f"{version}. Create a Python 3.12 or 3.13 virtualenv for "
            "join_meet_browser_use.py."
        )


def build_browser(Browser):
    headless = env_bool("HEADLESS", False)
    use_system_chrome = env_bool("GMEET_USE_SYSTEM_CHROME", False)
    profile_directory = os.environ.get("GMEET_CHROME_PROFILE_DIRECTORY")

    if use_system_chrome:
        log("Using Browser Use with your installed Chrome profile.")
        if profile_directory:
            log(f"Requested Chrome profile: {profile_directory}")
            return Browser.from_system_chrome(
                profile_directory=profile_directory,
                keep_alive=True,
            )
        return Browser.from_system_chrome(keep_alive=True)

    log("Using Browser Use managed browser session for guest join flow.")
    return Browser(
        headless=headless,
        keep_alive=True,
        window_size={"width": 1440, "height": 960},
    )


async def evaluate_json(page, script, *args):
    raw_result = await page.evaluate(script, *args)
    if raw_result is None or raw_result == "":
        return None
    return json.loads(raw_result)


async def get_meeting_status(page):
    return await evaluate_json(
        page,
        """() => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();
            const lowerText = normalize(document.body?.innerText || '').toLowerCase();
            const buttonNodes = Array.from(document.querySelectorAll('button, [role="button"]'));
            const labels = buttonNodes
                .map((node) => normalize(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || ''))
                .filter(Boolean)
                .map((label) => label.toLowerCase());

            return JSON.stringify({
                has_joined_control: labels.some((label) => label.includes('leave call')),
                waiting_for_host:
                    lowerText.includes('asking to be let in') ||
                    lowerText.includes('you\\'ll join the call when someone lets you in') ||
                    lowerText.includes('please wait until a meeting host brings you into the call'),
                denied:
                    lowerText.includes('request to join was denied') ||
                    lowerText.includes('denied your request to join'),
                blocked:
                    lowerText.includes('can\\'t join this video call') ||
                    lowerText.includes('no one can join a meeting unless invited or admitted by the host'),
                page_title: document.title || '',
            });
        }""",
    )


async def ensure_joined(page, timeout_ms=20000):
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)

    while asyncio.get_running_loop().time() < deadline:
        status = await get_meeting_status(page)
        if status and (status["waiting_for_host"] or status["denied"] or status["blocked"]):
            return False
        if status and status["has_joined_control"]:
            return True
        await asyncio.sleep(2)

    return False


async def is_chat_panel_open(page):
    result = await evaluate_json(
        page,
        """() => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };

            const hasMessageBox = Array.from(
                document.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], [role="textbox"]')
            ).some((node) => {
                if (!isVisible(node)) return false;
                const label = normalize(
                    node.getAttribute('aria-label') ||
                    node.getAttribute('placeholder') ||
                    node.getAttribute('title') ||
                    ''
                );
                return (
                    label.includes('send a message') ||
                    label.includes('message everyone') ||
                    label.includes('in-call message') ||
                    label.includes('chat')
                );
            });

            const closeButtons = Array.from(document.querySelectorAll('button, [role="button"]')).some((node) => {
                if (!isVisible(node)) return false;
                const label = normalize(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || '');
                return label.includes('close chat') || label.includes('close in-call messages');
            });

            return JSON.stringify({ open: hasMessageBox || closeButtons });
        }""",
    )
    return bool(result and result["open"])


async def open_chat_panel(page):
    if await is_chat_panel_open(page):
        log("Meet chat panel is already open.")
        return True

    click_result = await evaluate_json(
        page,
        """() => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };

            const candidates = Array.from(document.querySelectorAll('button, [role="button"]'));
            const preferredPhrases = [
                'chat with everyone',
                'show everyone chat',
                'open chat',
                'in-call messages',
                'messages',
                'chat',
            ];

            for (const phrase of preferredPhrases) {
                const node = candidates.find((candidate) => {
                    if (!isVisible(candidate)) return false;
                    const label = normalize(candidate.getAttribute('aria-label') || candidate.getAttribute('title') || candidate.textContent || '').toLowerCase();
                    return label.includes(phrase);
                });
                if (node) {
                    node.click();
                    return JSON.stringify({
                        clicked: true,
                        label: normalize(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || ''),
                    });
                }
            }

            return JSON.stringify({ clicked: false, label: '' });
        }""",
    )

    if click_result and click_result["clicked"]:
        log(f"Opened chat panel with selector match: {click_result['label']}")
        for _ in range(5):
            await asyncio.sleep(0.5)
            if await is_chat_panel_open(page):
                return True
    else:
        log("Chat button not found by selector. Trying keyboard shortcuts.")
        for key_combo in ("Control+Alt+C", "Meta+Alt+C"):
            try:
                await page.press(key_combo)
                await asyncio.sleep(1)
                if await is_chat_panel_open(page):
                    log(f"Opened chat panel with keyboard shortcut: {key_combo}")
                    return True
            except Exception as error:
                log(f"Chat shortcut {key_combo} failed: {error}")

    await asyncio.sleep(1)
    return await is_chat_panel_open(page)


async def collect_visible_chat_messages(page):
    payload = await evaluate_json(
        page,
        """() => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim();
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };

            const rootSelectors = [
                '[aria-label*="in-call messages" i]',
                '[aria-label*="chat" i]',
                '[role="complementary"]',
                '[aria-live="polite"]',
                '[data-panel-container-id]',
            ];
            const messageSelectors = [
                '[role="listitem"]',
                '[data-message-id]',
                'article',
                'li',
            ];

            const roots = [];
            for (const selector of rootSelectors) {
                for (const node of document.querySelectorAll(selector)) {
                    if (!isVisible(node)) continue;
                    if (!roots.includes(node)) roots.push(node);
                }
            }

            const collectMessagesFromRoot = (root) => {
                const collected = [];
                const seen = new Set();
                const items = messageSelectors.flatMap((selector) => Array.from(root.querySelectorAll(selector)));

                for (const item of items) {
                    if (!isVisible(item)) continue;
                    const rawText = normalize(item.innerText || item.textContent || '');
                    if (!rawText) continue;

                    const lines = rawText
                        .split('\\n')
                        .map((line) => normalize(line))
                        .filter(Boolean);
                    if (!lines.length) continue;

                    const authorNode = item.querySelector('[data-sender-name], [data-participant-name], [aria-label*="from" i]');
                    const author = normalize(authorNode?.textContent || lines[0] || '');
                    const timestampNode = item.querySelector('time, [data-timestamp], [aria-label*="sent at" i]');
                    const timestampText = normalize(timestampNode?.textContent || '');
                    const normalizedText = normalize(lines.length > 1 ? lines.slice(1).join(' ') : lines[0]);
                    const fingerprint = [author, timestampText, normalizedText].join('|').toLowerCase();

                    if (!normalizedText || seen.has(fingerprint)) continue;
                    seen.add(fingerprint);
                    collected.push({
                        fingerprint,
                        raw_text: rawText,
                        normalized_text: normalizedText,
                        author,
                        timestamp_text: timestampText,
                    });
                }

                return collected;
            };

            let bestMessages = [];
            for (const root of roots) {
                const candidateMessages = collectMessagesFromRoot(root);
                if (candidateMessages.length > bestMessages.length) {
                    bestMessages = candidateMessages;
                }
            }

            return JSON.stringify(bestMessages);
        }""",
    )

    messages = []
    for item in payload or []:
        messages.append(
            ChatMessage(
                fingerprint=item["fingerprint"],
                raw_text=item["raw_text"],
                normalized_text=normalize_message_text(item["normalized_text"]),
                author=item["author"],
                timestamp_text=item["timestamp_text"],
            )
        )
    return messages


async def send_chat_message(page, message_text):
    focus_result = await evaluate_json(
        page,
        """(messageText) => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };

            const candidates = Array.from(
                document.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], [role="textbox"]')
            );

            const input = candidates.find((candidate) => {
                if (!isVisible(candidate)) return false;
                const label = normalize(
                    candidate.getAttribute('aria-label') ||
                    candidate.getAttribute('placeholder') ||
                    candidate.getAttribute('title') ||
                    ''
                );
                return (
                    label.includes('send a message') ||
                    label.includes('message everyone') ||
                    label.includes('in-call message') ||
                    label.includes('chat')
                );
            });

            if (!input) {
                return JSON.stringify({ focused: false });
            }

            input.focus();
            if (input.tagName === 'TEXTAREA' || input.tagName === 'INPUT') {
                input.value = messageText;
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            } else {
                input.textContent = '';
                document.execCommand('insertText', false, messageText);
                if (!normalize(input.textContent).includes(normalize(messageText))) {
                    input.textContent = messageText;
                }
                input.dispatchEvent(new InputEvent('input', { bubbles: true, data: messageText, inputType: 'insertText' }));
            }

            return JSON.stringify({ focused: true });
        }""",
        message_text,
    )

    if not focus_result or not focus_result["focused"]:
        return False

    await asyncio.sleep(0.5)
    await page.press("Enter")
    await asyncio.sleep(0.5)

    send_result = await evaluate_json(
        page,
        """(messageText) => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const isVisible = (node) => {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.visibility !== 'hidden' && style.display !== 'none' && rect.width > 0 && rect.height > 0;
            };

            const sendButton = Array.from(document.querySelectorAll('button, [role="button"]')).find((node) => {
                if (!isVisible(node)) return false;
                const label = normalize(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || '');
                return label.includes('send') && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
            });

            if (sendButton) {
                sendButton.click();
                return JSON.stringify({ sent: true, method: 'button' });
            }

            const boxes = Array.from(
                document.querySelectorAll('textarea, input[type="text"], [contenteditable="true"], [role="textbox"]')
            );
            const messageStillInBox = boxes.some((box) => isVisible(box) && normalize(box.value || box.textContent || '').includes(normalize(messageText)));

            return JSON.stringify({ sent: !messageStillInBox, method: 'enter' });
        }""",
        message_text,
    )
    return True if send_result is None else bool(send_result.get("sent"))


async def send_introduction(page, state, display_name):
    if state.introduction_sent:
        return False

    intro_message = build_intro_message(display_name)
    sent = await send_chat_message(page, intro_message)
    if sent:
        state.introduction_sent = True
        log(f"Sent meeting introduction: {intro_message}")
        return True

    log("Could not send the meeting introduction message.")
    return False


def message_mentions_orbit(message):
    return bool(ORBIT_MENTION_PATTERN.search(message.normalized_text))


def grant_speak_permission(state, message):
    state.pending_speak_permissions += 1
    state.permission_events.append(
        PermissionEvent(
            granted_at=now_iso(),
            author=message.author,
            message_text=message.raw_text,
            fingerprint=message.fingerprint,
        )
    )
    log(
        "Speak permission granted by chat mention "
        f"(pending={state.pending_speak_permissions}): {message.raw_text}"
    )


def process_messages(state, messages, source):
    new_messages = [
        message
        for message in messages
        if message.fingerprint not in state.seen_message_fingerprints
    ]
    if not new_messages:
        return

    for message in new_messages:
        state.seen_message_fingerprints.add(message.fingerprint)
        log(
            f"New chat message ({source}) from "
            f"{message.author or 'unknown'}: {message.raw_text}"
        )
        if message_mentions_orbit(message):
            grant_speak_permission(state, message)


async def monitor_chat(page, state, wait_after_run_ms, display_name):
    state.chat_monitor_started_at = now_iso()

    chat_open = await open_chat_panel(page)
    if not chat_open:
        log("Meet chat could not be opened. Monitoring disabled for this session.")
    else:
        await send_introduction(page, state, display_name)
        initial_messages = await collect_visible_chat_messages(page)
        log(f"Scanned {len(initial_messages)} visible chat messages at monitor start.")
        process_messages(state, initial_messages, "startup")

    deadline = asyncio.get_running_loop().time() + (wait_after_run_ms / 1000)
    while asyncio.get_running_loop().time() < deadline:
        if chat_open:
            try:
                messages = await collect_visible_chat_messages(page)
                process_messages(state, messages, "poll")
            except Exception as error:
                log(f"Chat polling failed: {error}")
        await asyncio.sleep(POLL_INTERVAL_MS / 1000)

    log(
        "Chat monitor finished with "
        f"{state.pending_speak_permissions} pending speak permission(s)."
    )


async def main():
    load_dotenv()
    ensure_browser_use_venv()
    assert_supported_python()

    from browser_use import Agent, Browser, ChatOpenAI

    meet_url = os.environ.get("GMEET_URL")
    display_name = os.environ.get("GMEET_DISPLAY_NAME", "Orbit Agent")
    wait_after_run_ms = env_int("GMEET_WAIT_AFTER_JOIN_MS", 120000)
    max_steps = env_int("GMEET_BROWSER_USE_MAX_STEPS", 20)
    model_name = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    openai_api_key = os.environ.get("OPENAI_API_KEY")

    if not meet_url:
        raise RuntimeError("Missing GMEET_URL in .env or environment.")
    if not openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in .env or environment.")

    DEBUG_DIR.mkdir(exist_ok=True)
    CONVERSATION_DIR.mkdir(parents=True, exist_ok=True)

    llm = ChatOpenAI(model=model_name)
    browser = build_browser(Browser)
    state = MeetingState()

    agent = Agent(
        task=build_task(meet_url, display_name),
        llm=llm,
        browser=browser,
        use_vision=True,
        max_failures=3,
        save_conversation_path=str(CONVERSATION_DIR),
        generate_gif=str(DEBUG_DIR / "browser-use-meet.gif"),
    )

    log(f"Opening Meet URL: {meet_url}")
    log(f"Model: {model_name}")
    log(f"Agent max steps: {max_steps}")
    history = None

    try:
        history = await agent.run(max_steps=max_steps)
        log(f"Agent finished after {history.number_of_steps()} steps.")

        final_result = history.final_result()
        if final_result:
            log(f"Final result: {final_result}")

        if history.has_errors():
            log("Agent reported one or more step errors. Check debug/browser-use.")

        if history.is_successful():
            log("Browser Use marked the run as successful.")
        else:
            log("Browser Use did not mark the run as successful.")

        page = await browser.get_current_page()
        if not page:
            log(
                "Browser Use did not leave an active page handle. "
                f"Keeping browser open for {wait_after_run_ms // 1000} seconds."
            )
            await asyncio.sleep(wait_after_run_ms / 1000)
            return

        joined = await ensure_joined(page)
        if joined:
            state.joined_at = now_iso()
            log(
                "Orbit joined the meeting successfully. "
                f"Monitoring chat for {wait_after_run_ms // 1000} seconds."
            )
            await monitor_chat(page, state, wait_after_run_ms, display_name)
        else:
            log(
                "Orbit was not confirmed inside the meeting after Browser Use completed. "
                f"Keeping browser open for {wait_after_run_ms // 1000} seconds."
            )
            await asyncio.sleep(wait_after_run_ms / 1000)
    finally:
        if state.permission_events:
            log(f"Recorded {len(state.permission_events)} permission event(s).")
        log("Closing browser.")
        await browser.kill()


if __name__ == "__main__":
    asyncio.run(main())
