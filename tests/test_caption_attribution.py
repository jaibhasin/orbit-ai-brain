from __future__ import annotations

import unittest

from orbit.caption_attribution import CaptionSnippet, merge_caption_speakers
from orbit.transcript import TranscriptSegment


class CaptionAttributionTests(unittest.TestCase):
    def test_caption_speaker_is_merged_when_text_matches(self):
        segment = TranscriptSegment(
            source_id="s1",
            raw_text="we should launch on friday",
            clean_text="We should launch on friday.",
            memory_text="We should launch on friday.",
        )
        captions = [
            CaptionSnippet(
                speaker_name="Priya",
                text="we should launch on friday",
            )
        ]

        [merged] = merge_caption_speakers([segment], captions)

        self.assertEqual(merged.speaker_name, "Priya")
        self.assertEqual(merged.speaker_source, "google_meet_captions")
        self.assertEqual(merged.speaker_confidence, "medium")

    def test_caption_failure_leaves_segment_without_speaker(self):
        segment = TranscriptSegment(
            source_id="s1",
            raw_text="we should launch on friday",
            clean_text="We should launch on friday.",
            memory_text="We should launch on friday.",
        )
        captions = [CaptionSnippet(speaker_name="Priya", text="budget review next week")]

        [merged] = merge_caption_speakers([segment], captions)

        self.assertIsNone(merged.speaker_name)
        self.assertIsNone(merged.speaker_source)


if __name__ == "__main__":
    unittest.main()
