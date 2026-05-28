from __future__ import annotations

import unittest

from orbit.meet import build_intro_message, is_orbit_authored_message
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


if __name__ == "__main__":
    unittest.main()
