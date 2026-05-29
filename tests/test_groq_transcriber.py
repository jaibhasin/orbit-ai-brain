from __future__ import annotations

import unittest

from orbit.groq_transcriber import _build_transcription_prompt


class GroqTranscriberPromptTests(unittest.TestCase):
    def test_known_speaker_names_are_folded_into_prompt(self):
        prompt = _build_transcription_prompt(
            prompt="Product launch planning",
            known_speaker_names=["Jai", "Alex"],
        )

        self.assertIn("Product launch planning", prompt or "")
        self.assertIn("Jai", prompt or "")
        self.assertIn("Alex", prompt or "")

    def test_empty_prompt_and_speakers_return_none(self):
        self.assertIsNone(_build_transcription_prompt(prompt=None, known_speaker_names=None))
        self.assertIsNone(_build_transcription_prompt(prompt="  ", known_speaker_names=[]))


if __name__ == "__main__":
    unittest.main()
