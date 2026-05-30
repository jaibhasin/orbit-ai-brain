from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from orbit.meet import (
    build_intro_message,
    is_orbit_authored_message,
    trigger_extension_audio_capture,
)
from orbit.meet_types import ChatMessage, MeetingState


class MeetChatTests(unittest.TestCase):
    def test_orbit_intro_is_detected_as_orbit_authored(self):
        state = MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )
        intro = build_intro_message(state.display_name)
        message = ChatMessage(
            fingerprint="fp-1",
            raw_text=f"{intro} Hover over a message to pin it",
            normalized_text=f"{intro} Hover over a message to pin it",
            author=intro,
            timestamp_text="",
        )

        self.assertTrue(is_orbit_authored_message(state, message))


class TriggerExtensionAudioCaptureTests(unittest.IsolatedAsyncioTestCase):
    class FakePage:
        def __init__(self, evaluate_results):
            self.evaluate_results = iter(evaluate_results)
            self._mouse = AsyncMock()
            self.press = AsyncMock()

        async def evaluate(self, script, *args):
            return next(self.evaluate_results)

        @property
        async def mouse(self):
            return self._mouse

    def build_state(self):
        return MeetingState(
            session_id="session-1",
            meet_url="https://meet.google.com/abc-defg-hij",
            meeting_code="abc-defg-hij",
            display_name="Orbit",
        )

    async def test_clicks_injected_audio_button(self):
        page = self.FakePage(
            [
                True,
                '{"found": true, "x": 120.8, "y": 45.2}',
                '{"label": "Orbit audio active", "disabled": true}',
            ]
        )
        state = self.build_state()

        result = await trigger_extension_audio_capture(
            page,
            state,
            "ws://127.0.0.1:8000/internal/audio-stream/session-1",
        )

        self.assertTrue(result)
        self.assertEqual(
            state.live_stt_status_detail,
            "Orbit extension accepted the audio capture request.",
        )
        page._mouse.click.assert_awaited_once_with(120, 45)
        page.press.assert_not_awaited()

    @patch("orbit.meet.asyncio.sleep", new_callable=AsyncMock)
    async def test_uses_shortcut_when_audio_button_is_missing(self, sleep):
        page = self.FakePage([True] + ['{"found": false}'] * 10)
        state = self.build_state()

        result = await trigger_extension_audio_capture(
            page,
            state,
            "ws://127.0.0.1:8000/internal/audio-stream/session-1",
        )

        self.assertTrue(result)
        page.press.assert_awaited_once_with("Alt+Shift+O")
        self.assertEqual(sleep.await_count, 10)

    async def test_uses_shortcut_when_audio_button_rejects_capture(self):
        page = self.FakePage(
            [
                True,
                '{"found": true, "x": 120.8, "y": 45.2}',
                '{"label": "Use Alt+Shift+O or the extension icon", "disabled": false}',
            ]
        )
        state = self.build_state()

        result = await trigger_extension_audio_capture(
            page,
            state,
            "ws://127.0.0.1:8000/internal/audio-stream/session-1",
        )

        self.assertTrue(result)
        page.press.assert_awaited_once_with("Alt+Shift+O")

    @patch("orbit.meet.asyncio.sleep", new_callable=AsyncMock)
    async def test_returns_false_when_button_and_shortcut_activation_fail(self, sleep):
        page = self.FakePage([True] + ['{"found": false}'] * 10)
        page.press.side_effect = RuntimeError("shortcut unavailable")
        state = self.build_state()

        result = await trigger_extension_audio_capture(
            page,
            state,
            "ws://127.0.0.1:8000/internal/audio-stream/session-1",
        )

        self.assertFalse(result)
        self.assertIn("shortcut unavailable", state.live_stt_status_detail)


if __name__ == "__main__":
    unittest.main()
