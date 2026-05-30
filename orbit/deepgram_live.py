from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlencode

from orbit.transcript import TranscriptSegment


DEEPGRAM_LIVE_URL = "wss://api.deepgram.com/v1/listen"


@dataclass(frozen=True)
class DeepgramLiveConfig:
    model: str = "nova-3"
    encoding: str = "linear16"
    sample_rate: int = 16000
    channels: int = 1
    interim_results: bool = False
    smart_format: bool = True
    punctuate: bool = True
    diarize: bool = False
    language: str | None = None

    def query_string(self) -> str:
        params: dict[str, str | int | bool] = {
            "model": self.model,
            "encoding": self.encoding,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "interim_results": str(self.interim_results).lower(),
            "smart_format": str(self.smart_format).lower(),
            "punctuate": str(self.punctuate).lower(),
            "diarize": str(self.diarize).lower(),
        }
        if self.language:
            params["language"] = self.language
        return urlencode(params)


def deepgram_live_url(config: DeepgramLiveConfig) -> str:
    return f"{DEEPGRAM_LIVE_URL}?{config.query_string()}"


def parse_deepgram_message(raw_message: str | bytes, source_id_prefix: str = "live") -> list[TranscriptSegment]:
    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")

    try:
        payload = json.loads(raw_message)
    except json.JSONDecodeError:
        return []

    return parse_deepgram_payload(payload, source_id_prefix=source_id_prefix)


def parse_deepgram_payload(payload: dict, source_id_prefix: str = "live") -> list[TranscriptSegment]:
    if payload.get("type") and payload.get("type") != "Results":
        return []
    if payload.get("is_final") is not True:
        return []

    channel = payload.get("channel") or {}
    alternatives = channel.get("alternatives") or []
    if not alternatives:
        return []

    alternative = alternatives[0] or {}
    transcript = str(alternative.get("transcript") or "").strip()
    if not transcript:
        return []

    words = alternative.get("words") or []
    start_seconds = _first_numeric(payload.get("start"), _word_time(words, "start"))
    end_seconds = _first_numeric(_word_time(list(reversed(words)), "end"))
    if end_seconds is None and start_seconds is not None and payload.get("duration") is not None:
        end_seconds = start_seconds + float(payload["duration"])

    speaker_label = _speaker_label(words)
    confidence = _coerce_float(alternative.get("confidence"))
    start_ms = _seconds_to_ms(start_seconds)
    end_ms = _seconds_to_ms(end_seconds)
    source_id = f"{source_id_prefix}-{start_ms or 0}-{end_ms or 0}"

    return [
        TranscriptSegment(
            source_id=source_id,
            raw_text=transcript,
            clean_text=transcript,
            memory_text=transcript,
            speaker_label=speaker_label,
            speaker_source="deepgram_diarization" if speaker_label else None,
            speaker_confidence="medium" if speaker_label else "unknown",
            start_ms=start_ms,
            end_ms=end_ms,
            confidence=confidence,
            source_type="live_audio_transcript",
            metadata={
                "deepgram_request_id": payload.get("metadata", {}).get("request_id"),
                "speech_final": payload.get("speech_final"),
                "is_final": payload.get("is_final"),
            },
        )
    ]


class DeepgramLiveTranscriber:
    def __init__(
        self,
        api_key: str,
        config: DeepgramLiveConfig | None = None,
        *,
        connect=None,
    ):
        self.api_key = api_key
        self.config = config or DeepgramLiveConfig()
        self._connect = connect
        self._ws = None
        self._finish_sent = False

    async def connect(self):
        if self._ws is not None:
            return self

        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect

        try:
            self._ws = await connect(
                deepgram_live_url(self.config),
                additional_headers={"Authorization": f"Token {self.api_key}"},
            )
        except TypeError:
            self._ws = await connect(
                deepgram_live_url(self.config),
                extra_headers={"Authorization": f"Token {self.api_key}"},
            )
        return self

    async def send_audio(self, audio_chunk: bytes) -> None:
        if not audio_chunk:
            return
        if self._ws is None:
            await self.connect()
        assert self._ws is not None
        await self._ws.send(audio_chunk)

    async def receive(self):
        if self._ws is None:
            await self.connect()
        assert self._ws is not None
        async for message in self._ws:
            yield message

    async def finish(self) -> None:
        if self._ws is None:
            return
        if self._finish_sent:
            return
        await self._ws.send(json.dumps({"type": "CloseStream"}))
        self._finish_sent = True

    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self.finish()
        except Exception:
            pass
        await self._ws.close()
        self._ws = None
        self._finish_sent = False


def _first_numeric(*values) -> float | None:
    for value in values:
        coerced = _coerce_float(value)
        if coerced is not None:
            return coerced
    return None


def _word_time(words: list[dict], key: str) -> float | None:
    for word in words:
        value = _coerce_float(word.get(key))
        if value is not None:
            return value
    return None


def _speaker_label(words: list[dict]) -> str | None:
    for word in words:
        speaker = word.get("speaker")
        if speaker is not None:
            return f"speaker_{speaker}"
    return None


def _seconds_to_ms(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value * 1000)


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
