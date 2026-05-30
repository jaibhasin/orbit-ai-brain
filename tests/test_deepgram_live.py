from __future__ import annotations

import json
import unittest

from orbit.deepgram_live import DeepgramLiveConfig, deepgram_live_url, parse_deepgram_payload


class DeepgramLiveTests(unittest.TestCase):
    def test_url_matches_declared_audio_format(self):
        url = deepgram_live_url(
            DeepgramLiveConfig(
                model="nova-3",
                encoding="linear16",
                sample_rate=16000,
                channels=1,
            )
        )

        self.assertIn("encoding=linear16", url)
        self.assertIn("sample_rate=16000", url)
        self.assertIn("channels=1", url)
        self.assertIn("interim_results=false", url)

    def test_final_event_becomes_transcript_segment(self):
        payload = {
            "type": "Results",
            "is_final": True,
            "speech_final": True,
            "start": 1.5,
            "duration": 2.0,
            "channel": {
                "alternatives": [
                    {
                        "transcript": "we should launch on friday",
                        "confidence": 0.91,
                        "words": [
                            {"word": "we", "start": 1.5, "end": 1.7, "speaker": 0},
                            {"word": "friday", "start": 3.1, "end": 3.5, "speaker": 0},
                        ],
                    }
                ]
            },
        }

        segments = parse_deepgram_payload(payload, source_id_prefix="s1")

        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].raw_text, "we should launch on friday")
        self.assertEqual(segments[0].start_ms, 1500)
        self.assertEqual(segments[0].end_ms, 3500)
        self.assertEqual(segments[0].speaker_label, "speaker_0")
        self.assertEqual(segments[0].speaker_source, "deepgram_diarization")

    def test_interim_event_is_not_durable(self):
        payload = {
            "type": "Results",
            "is_final": False,
            "channel": {"alternatives": [{"transcript": "partial words"}]},
        }

        self.assertEqual(parse_deepgram_payload(payload), [])

    def test_non_json_payload_can_be_ignored_by_caller(self):
        payload = json.loads('{"type":"Metadata"}')

        self.assertEqual(parse_deepgram_payload(payload), [])


if __name__ == "__main__":
    unittest.main()
