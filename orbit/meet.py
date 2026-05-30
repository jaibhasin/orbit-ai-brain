from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from orbit.caption_attribution import CaptionSnippet
from orbit.core import (
    CONVERSATION_DIR,
    DEBUG_DIR,
    ensure_browser_use_runtime,
    env_bool,
    env_int,
    extract_meeting_code,
    load_dotenv,
    log,
    normalize_message_text,
    now_iso,
)
from orbit.meet_types import (
    ChatMessage,
    MeetingSessionConfig,
    PermissionEvent,
    build_meeting_state,
)


POLL_INTERVAL_MS = 3000
PARTICIPANT_CHECK_INTERVAL_MS = 30000
SOLO_PARTICIPANT_POLLS_BEFORE_LEAVE = 2
ORBIT_MENTION_PATTERN = re.compile(r"(?<!\w)@orbit(?!\w)", re.IGNORECASE)


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
        "Hi everyone, I’m Orbit. "
        "I’ll be recording and monitoring this meeting."
        "Mention @orbit in chat to get my attention."
    )


def build_browser(Browser, session_id=None):
    headless = env_bool("HEADLESS", False)
    use_system_chrome = env_bool("GMEET_USE_SYSTEM_CHROME", False)
    profile_directory = os.environ.get("GMEET_CHROME_PROFILE_DIRECTORY")
    cdp_url = os.environ.get("ORBIT_CHROME_CDP_URL")
    extension_path = os.environ.get("ORBIT_CHROME_EXTENSION_PATH", "extension/orbit-audio-capture")

    if cdp_url:
        log(f"Connecting Browser Use to existing Chrome over CDP: {cdp_url}", session_id)
        return Browser(
            cdp_url=cdp_url,
            keep_alive=True,
        )

    browser_args = []
    if Path(extension_path).exists():
        resolved_extension_path = str(Path(extension_path).resolve())
        browser_args.extend(
            [
                f"--disable-extensions-except={resolved_extension_path}",
                f"--load-extension={resolved_extension_path}",
            ]
        )

    if use_system_chrome:
        log("Using Browser Use with your installed Chrome profile.", session_id)
        if browser_args:
            log(
                "Official Chrome 137+ ignores command-line unpacked-extension loading. "
                "Load the Orbit extension manually from chrome://extensions before joining.",
                session_id,
            )
        if profile_directory:
            log(f"Requested Chrome profile: {profile_directory}", session_id)
            return Browser.from_system_chrome(
                profile_directory=profile_directory,
                keep_alive=True,
                args=browser_args or None,
            )
        return Browser.from_system_chrome(keep_alive=True, args=browser_args or None)

    log("Using Browser Use managed browser session for guest join flow.", session_id)
    if browser_args:
        log(f"Loading Orbit audio capture extension: {resolved_extension_path}", session_id)
    return Browser(
        headless=headless,
        keep_alive=True,
        window_size={"width": 1440, "height": 960},
        args=browser_args or None,
    )


def build_default_session_config(meet_url, session_id=None):
    load_dotenv()
    meeting_code = extract_meeting_code(meet_url)
    resolved_session_id = session_id or f"manual-{meeting_code}"
    live_stt_enabled = env_bool("ORBIT_LIVE_STT_ENABLED", bool(os.environ.get("DEEPGRAM_API_KEY")))
    audio_ws_base_url = os.environ.get("ORBIT_AUDIO_WS_BASE_URL", "ws://127.0.0.1:8000").rstrip("/")
    audio_stream_token = secrets.token_urlsafe(24) if live_stt_enabled else None
    audio_stream_ws_url = f"{audio_ws_base_url}/internal/audio-stream/{resolved_session_id}"
    if audio_stream_token:
        audio_stream_ws_url = f"{audio_stream_ws_url}?{urlencode({'token': audio_stream_token})}"
    return MeetingSessionConfig(
        session_id=resolved_session_id,
        meet_url=meet_url,
        display_name=os.environ.get("GMEET_DISPLAY_NAME", "Orbit Agent"),
        wait_after_join_ms=env_int("GMEET_WAIT_AFTER_JOIN_MS", 300000),
        max_steps=env_int("GMEET_BROWSER_USE_MAX_STEPS", 20),
        model_name=os.environ.get("OPENAI_MODEL", "gpt-5.4-mini"),
        live_stt_enabled=live_stt_enabled,
        audio_stream_ws_url=audio_stream_ws_url,
        audio_stream_token=audio_stream_token,
    )


async def maybe_await(result):
    if inspect.isawaitable(result):
        return await result
    return result


async def emit_status(callbacks, state, status, detail=None):
    state.status = status
    state.status_detail = detail
    if callbacks and callbacks.on_status:
        await maybe_await(callbacks.on_status(state, status, detail))


async def emit_chat_message(callbacks, state, message, source):
    if callbacks and callbacks.on_chat_message:
        await maybe_await(callbacks.on_chat_message(state, message, source))


async def emit_captions(callbacks, state, captions):
    if callbacks and callbacks.on_captions:
        await maybe_await(callbacks.on_captions(state, captions))


async def emit_orbit_mention(callbacks, state, message):
    if callbacks and callbacks.on_orbit_mention:
        return await maybe_await(callbacks.on_orbit_mention(state, message))
    return None


async def emit_finished(callbacks, state):
    if callbacks and callbacks.on_finished:
        await maybe_await(callbacks.on_finished(state))


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


def classify_join_failure(status):
    if not status:
        return "join_unconfirmed", "Orbit could not confirm whether Meet admitted it."
    if status["waiting_for_host"]:
        return "waiting_for_host", "Orbit is waiting for a meeting host to admit it."
    if status["denied"]:
        return "join_denied", "Google Meet denied the join request."
    if status["blocked"]:
        return "join_blocked", "Google Meet blocked entry to this meeting."
    return "join_unconfirmed", "Orbit could not confirm whether Meet admitted it."


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


async def get_participant_count(page):
    result = await evaluate_json(
        page,
        """() => {
            const normalize = (text) => (text || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const candidates = Array.from(document.querySelectorAll('button, [role="button"]'));
            const counts = [];

            for (const node of candidates) {
                const label = normalize([
                    node.getAttribute('aria-label'),
                    node.getAttribute('title'),
                    node.textContent,
                ].filter(Boolean).join(' '));
                if (
                    !label.includes('show everyone') &&
                    !label.includes('participants') &&
                    !label.includes('people')
                ) {
                    continue;
                }

                const matches = label.match(/\\d+/g) || [];
                for (const match of matches) counts.push(Number(match));
            }

            return JSON.stringify({ count: counts.length ? Math.max(...counts) : null });
        }""",
    )
    if not result or result.get("count") is None:
        return None
    return int(result["count"])


def should_leave_when_only_orbit_remains(state, participant_count):
    if participant_count is None:
        state.solo_participant_polls = 0
        return False
    if participant_count > 1:
        state.observed_other_participants = True
        state.solo_participant_polls = 0
        return False
    if participant_count != 1 or not state.observed_other_participants:
        state.solo_participant_polls = 0
        return False

    state.solo_participant_polls += 1
    return state.solo_participant_polls >= SOLO_PARTICIPANT_POLLS_BEFORE_LEAVE


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


async def open_chat_panel(page, session_id=None):
    if await is_chat_panel_open(page):
        log("Meet chat panel is already open.", session_id)
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
        log(f"Opened chat panel with selector match: {click_result['label']}", session_id)
        for _ in range(5):
            await asyncio.sleep(0.5)
            if await is_chat_panel_open(page):
                return True
    else:
        log("Chat button not found by selector. Trying keyboard shortcuts.", session_id)
        for key_combo in ("Control+Alt+C", "Meta+Alt+C"):
            try:
                await page.press(key_combo)
                await asyncio.sleep(1)
                if await is_chat_panel_open(page):
                    log(f"Opened chat panel with keyboard shortcut: {key_combo}", session_id)
                    return True
            except Exception as error:
                log(f"Chat shortcut {key_combo} failed: {error}", session_id)

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


async def send_introduction(page, state):
    if state.introduction_sent:
        return False

    intro_message = build_intro_message(state.display_name)
    sent = await send_chat_message(page, intro_message)
    if sent:
        state.introduction_sent = True
        log(f"Sent meeting introduction: {intro_message}", state.session_id)
        return True

    log("Could not send the meeting introduction message.", state.session_id)
    return False


async def enable_captions(page, session_id=None):
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
            const buttons = Array.from(document.querySelectorAll('button, [role="button"]'));
            const captionsButton = buttons.find((node) => {
                if (!isVisible(node)) return false;
                const label = normalize(node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent || '');
                return (
                    label.includes('turn on captions') ||
                    label.includes('show captions') ||
                    label === 'captions' ||
                    label.includes('captions off')
                );
            });
            if (!captionsButton) return JSON.stringify({ clicked: false, label: '' });
            captionsButton.click();
            return JSON.stringify({
                clicked: true,
                label: captionsButton.getAttribute('aria-label') || captionsButton.getAttribute('title') || captionsButton.textContent || ''
            });
        }""",
    )
    if result and result.get("clicked"):
        log(f"Requested Meet captions with selector match: {result.get('label')}", session_id)
        return True

    log("Could not find a visible Meet captions control. Caption attribution will remain disabled.", session_id)
    return False


async def collect_visible_captions(page):
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
            const roots = Array.from(document.querySelectorAll(
                '[aria-live="polite"], [aria-live="assertive"], [role="region"], [data-self-name]'
            )).filter(isVisible);
            const snippets = [];
            const seen = new Set();

            for (const root of roots) {
                const rawText = normalize(root.innerText || root.textContent || '');
                if (!rawText || rawText.length < 4) continue;
                const lines = rawText.split('\\n').map(normalize).filter(Boolean);
                if (!lines.length) continue;

                let speakerName = '';
                let text = '';
                const speakerNode = root.querySelector('[data-self-name], [data-participant-name], [aria-label*="speaker" i]');
                if (speakerNode) {
                    speakerName = normalize(speakerNode.textContent || speakerNode.getAttribute('aria-label') || '');
                    text = normalize(lines.filter((line) => line !== speakerName).join(' '));
                } else if (lines.length >= 2 && lines[0].length <= 60) {
                    speakerName = lines[0];
                    text = normalize(lines.slice(1).join(' '));
                }

                if (!speakerName || !text || text.length < 4) continue;
                const key = `${speakerName}|${text}`.toLowerCase();
                if (seen.has(key)) continue;
                seen.add(key);
                snippets.push({ speaker_name: speakerName, text });
            }

            return JSON.stringify(snippets.slice(-10));
        }""",
    )

    return [
        CaptionSnippet(
            speaker_name=item["speaker_name"],
            text=item["text"],
        )
        for item in payload or []
        if item.get("speaker_name") and item.get("text")
    ]


async def trigger_extension_audio_capture(page, state, audio_stream_ws_url):
    if not audio_stream_ws_url:
        return False

    payload = {
        "source": "orbit",
        "type": "ORBIT_START_CAPTURE",
        "sessionId": state.session_id,
        "meetingId": state.meeting_code,
        "webSocketUrl": audio_stream_ws_url,
        "audioFormat": {
            "encoding": "linear16",
            "sampleRate": 16000,
            "channels": 1,
        },
    }

    try:
        await page.evaluate(
            """(payload) => {
                window.postMessage(payload, window.location.origin);
                return true;
            }""",
            payload,
        )
        state.live_stt_status_detail = "Extension capture start message posted."
        log("Posted Orbit extension capture start message.", state.session_id)
    except Exception as error:
        state.live_stt_status_detail = f"Extension capture start message failed: {error}"
        log(state.live_stt_status_detail, state.session_id)
        return False

    button_clicked = False
    for _ in range(10):
        try:
            click_result = await evaluate_json(
                page,
                """() => {
                    const button = document.getElementById('orbit-audio-capture-button');
                    if (!button) return JSON.stringify({ found: false });
                    const rect = button.getBoundingClientRect();
                    return JSON.stringify({
                        found: true,
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                    });
                }""",
            )
            if click_result and click_result["found"]:
                mouse = await page.mouse
                await mouse.click(int(click_result["x"]), int(click_result["y"]))
                log("Clicked the injected Orbit audio capture button.", state.session_id)
                button_clicked = True
                break
        except Exception as error:
            log(f"Orbit audio capture button click failed: {error}", state.session_id)
            break
        await asyncio.sleep(0.2)

    if button_clicked:
        for _ in range(10):
            try:
                button_status = await evaluate_json(
                    page,
                    """() => {
                        const button = document.getElementById('orbit-audio-capture-button');
                        return JSON.stringify({
                            label: button?.textContent || '',
                            disabled: Boolean(button?.disabled),
                        });
                    }""",
                )
            except Exception as error:
                log(f"Orbit audio capture button status check failed: {error}", state.session_id)
                break
            button_status = button_status or {}
            label = str(button_status.get("label") or "")
            if button_status.get("disabled") or "audio active" in label.lower():
                state.live_stt_status_detail = "Orbit extension accepted the audio capture request."
                log(state.live_stt_status_detail, state.session_id)
                return True
            if "use alt+shift+o" in label.lower():
                log("Orbit audio button requested extension shortcut fallback.", state.session_id)
                break
            await asyncio.sleep(0.2)

    shortcut = os.environ.get("ORBIT_EXTENSION_CAPTURE_SHORTCUT", "Alt+Shift+O")
    try:
        await page.press(shortcut)
        state.live_stt_status_detail = f"Tried Orbit extension activation shortcut: {shortcut}"
        log(f"Tried Orbit extension activation shortcut: {shortcut}", state.session_id)
        return True
    except Exception as error:
        state.live_stt_status_detail = f"Orbit extension activation shortcut failed: {error}"
        log(state.live_stt_status_detail, state.session_id)
        return False


def message_mentions_orbit(message):
    return bool(ORBIT_MENTION_PATTERN.search(message.normalized_text))


def is_orbit_authored_message(state, message):
    author = (message.author or "").strip().lower()
    raw_text = (message.raw_text or "").strip().lower()
    normalized_text = (message.normalized_text or "").strip().lower()
    display_name = (state.display_name or "").strip().lower()
    intro_text = build_intro_message(state.display_name).strip().lower()

    return (
        (display_name and display_name in author)
        or author in {"orbit", "orbit agent", "orbit (ai agent)"}
        or raw_text.startswith(intro_text)
        or normalized_text.startswith(intro_text)
    )


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
        f"(pending={state.pending_speak_permissions}): {message.raw_text}",
        state.session_id,
    )


async def process_messages(page, state, messages, source, callbacks=None):
    new_messages = [
        message
        for message in messages
        if message.fingerprint not in state.seen_message_fingerprints
    ]
    if not new_messages:
        return

    for message in new_messages:
        state.seen_message_fingerprints.add(message.fingerprint)
        if is_orbit_authored_message(state, message):
            log(f"Ignoring Orbit-authored chat message ({source}).", state.session_id)
            continue

        state.captured_messages.append(message)
        log(
            f"New chat message ({source}) from "
            f"{message.author or 'unknown'}: {message.raw_text}",
            state.session_id,
        )
        await emit_chat_message(callbacks, state, message, source)
        if message_mentions_orbit(message):
            grant_speak_permission(state, message)
            reply = await emit_orbit_mention(callbacks, state, message)
            if reply:
                sent = await send_chat_message(page, reply)
                if sent:
                    log(f"Sent Orbit mention reply: {reply}", state.session_id)
                else:
                    log("Could not send Orbit mention reply.", state.session_id)


async def monitor_chat(page, state, wait_after_run_ms, callbacks=None):
    state.chat_monitor_started_at = now_iso()
    seen_caption_fingerprints = set()

    chat_open = await open_chat_panel(page, state.session_id)
    state.chat_monitor_available = chat_open
    if not chat_open:
        log("Meet chat could not be opened. Monitoring disabled for this session.", state.session_id)
        await emit_status(
            callbacks,
            state,
            "chat_monitor_unavailable",
            "Orbit joined, but Meet chat could not be opened.",
        )
    else:
        await send_introduction(page, state)
        initial_messages = await collect_visible_chat_messages(page)
        log(f"Scanned {len(initial_messages)} visible chat messages at monitor start.", state.session_id)
        await process_messages(page, state, initial_messages, "startup", callbacks)

    deadline = asyncio.get_running_loop().time() + (wait_after_run_ms / 1000)
    next_participant_check_at = 0.0
    while asyncio.get_running_loop().time() < deadline:
        if state.stop_requested:
            state.leave_reason = state.stop_reason or "Orbit was asked to stop monitoring this meeting."
            log(state.leave_reason, state.session_id)
            break
        now = asyncio.get_running_loop().time()
        if now >= next_participant_check_at:
            next_participant_check_at = now + (PARTICIPANT_CHECK_INTERVAL_MS / 1000)
            try:
                participant_count = await get_participant_count(page)
                if should_leave_when_only_orbit_remains(state, participant_count):
                    state.leave_reason = "Orbit is the only participant left in the meeting."
                    log(state.leave_reason, state.session_id)
                    break
            except Exception as error:
                log(f"Participant count check failed: {error}", state.session_id)
        if chat_open:
            try:
                messages = await collect_visible_chat_messages(page)
                await process_messages(page, state, messages, "poll", callbacks)
            except Exception as error:
                log(f"Chat polling failed: {error}", state.session_id)
        if state.live_stt_available:
            try:
                captions = await collect_visible_captions(page)
                new_captions = []
                for caption in captions:
                    fingerprint = f"{caption.speaker_name}|{caption.text}".lower()
                    if fingerprint in seen_caption_fingerprints:
                        continue
                    seen_caption_fingerprints.add(fingerprint)
                    new_captions.append(caption)
                if new_captions:
                    await emit_captions(callbacks, state, new_captions)
            except Exception as error:
                log(f"Caption scraping failed: {error}", state.session_id)
        await asyncio.sleep(POLL_INTERVAL_MS / 1000)

    if state.leave_reason is None:
        state.leave_reason = "Meeting monitoring duration elapsed."
    log(
        "Chat monitor finished with "
        f"{state.pending_speak_permissions} pending speak permission(s).",
        state.session_id,
    )


async def run_meeting_session(config, callbacks=None, state=None):
    load_dotenv()

    if state is None:
        state = build_meeting_state(config)

    openai_api_key = os.environ.get("OPENAI_API_KEY")
    if not openai_api_key:
        raise RuntimeError("Missing OPENAI_API_KEY in .env or environment.")

    DEBUG_DIR.mkdir(exist_ok=True)
    session_conversation_dir = CONVERSATION_DIR / config.session_id
    session_conversation_dir.mkdir(parents=True, exist_ok=True)
    session_gif_path = DEBUG_DIR / f"browser-use-meet-{config.session_id}.gif"

    from browser_use import Agent, Browser, ChatOpenAI

    llm = ChatOpenAI(model=config.model_name)
    browser = None
    history = None

    await emit_status(callbacks, state, "starting_join", f"Opening Meet URL: {config.meet_url}")
    log(f"Opening Meet URL: {config.meet_url}", state.session_id)
    log(f"Model: {config.model_name}", state.session_id)
    log(f"Agent max steps: {config.max_steps}", state.session_id)

    try:
        browser = build_browser(Browser, state.session_id)
        agent: Any = Agent(
            task=build_task(config.meet_url, config.display_name),
            llm=llm,
            browser=browser,
            use_vision=True,
            max_failures=3,
            save_conversation_path=str(session_conversation_dir),
            generate_gif=str(session_gif_path),
        )

        history = await agent.run(max_steps=config.max_steps)
        log(f"Agent finished after {history.number_of_steps()} steps.", state.session_id)

        final_result = history.final_result()
        if final_result:
            state.browser_use_final_result = final_result
            log(f"Final result: {final_result}", state.session_id)

        if history.has_errors():
            state.browser_use_had_errors = True
            log("Agent reported one or more step errors. Check debug/browser-use.", state.session_id)

        state.browser_use_success = history.is_successful()
        if state.browser_use_success:
            log("Browser Use marked the run as successful.", state.session_id)
        else:
            log("Browser Use did not mark the run as successful.", state.session_id)

        page = await browser.get_current_page()
        if not page:
            detail = "Browser Use did not leave an active page handle."
            await emit_status(callbacks, state, "no_active_page", detail)
            log(
                f"{detail} Keeping browser open for {config.wait_after_join_ms // 1000} seconds.",
                state.session_id,
            )
            await asyncio.sleep(config.wait_after_join_ms / 1000)
            return state

        joined = await ensure_joined(page)
        if joined:
            state.joined_at = now_iso()
            detail = f"Orbit joined the meeting successfully. Monitoring chat for {config.wait_after_join_ms // 1000} seconds."
            await emit_status(callbacks, state, "joined", detail)
            log(detail, state.session_id)
            if config.live_stt_enabled:
                state.live_stt_requested = True
                await enable_captions(page, state.session_id)
                state.live_stt_available = await trigger_extension_audio_capture(
                    page,
                    state,
                    config.audio_stream_ws_url,
                )
                if state.live_stt_available:
                    await emit_status(
                        callbacks,
                        state,
                        "live_stt_capture_requested",
                        "Orbit requested tab audio capture through the Chrome extension. Click the in-page Orbit audio button if Chrome requires manual activation.",
                    )
                else:
                    await emit_status(
                        callbacks,
                        state,
                        "live_stt_unavailable",
                        state.live_stt_status_detail or "Orbit could not trigger tab audio capture.",
                    )
            await monitor_chat(page, state, config.wait_after_join_ms, callbacks)
        else:
            status = await get_meeting_status(page)
            join_status, detail = classify_join_failure(status)
            await emit_status(callbacks, state, join_status, detail)
            log(
                f"{detail} Keeping browser open for {config.wait_after_join_ms // 1000} seconds.",
                state.session_id,
            )
            await asyncio.sleep(config.wait_after_join_ms / 1000)
    except Exception as error:
        state.last_error = str(error)
        await emit_status(callbacks, state, "error", str(error))
        log(f"Meeting session failed: {error}", state.session_id)
    finally:
        state.finished_at = now_iso()
        if state.permission_events:
            log(f"Recorded {len(state.permission_events)} permission event(s).", state.session_id)
        if browser is not None:
            log("Closing browser.", state.session_id)
            try:
                await browser.kill()
            except Exception as error:
                log(f"Browser shutdown failed: {error}", state.session_id)
        await emit_finished(callbacks, state)

    return state


async def main():
    ensure_browser_use_runtime("scripts/join_meet.py")

    meet_url = os.environ.get("GMEET_URL")
    if not meet_url:
        raise RuntimeError("Missing GMEET_URL in .env or environment.")

    config = build_default_session_config(meet_url)
    state = await run_meeting_session(config)
    if state.last_error:
        raise RuntimeError(state.last_error)


if __name__ == "__main__":
    asyncio.run(main())
