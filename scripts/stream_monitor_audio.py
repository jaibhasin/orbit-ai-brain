from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Fallback/debug live STT audio streamer. Captures a PulseAudio/PipeWire "
            "monitor source with ffmpeg and streams PCM16 mono audio to Orbit."
        )
    )
    parser.add_argument("websocket_url", help="Orbit audio WebSocket URL for the meeting session.")
    parser.add_argument(
        "--source",
        default="default",
        help="PulseAudio/PipeWire source name, for example alsa_output...monitor.",
    )
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--chunk-size", type=int, default=4096)
    return parser


async def stream_monitor_audio(
    *,
    websocket_url: str,
    source: str,
    sample_rate: int,
    chunk_size: int,
) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg is required for monitor audio fallback capture.")

    import websockets

    process = subprocess.Popen(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "pulse",
            "-i",
            source,
            "-ac",
            "1",
            "-ar",
            str(sample_rate),
            "-f",
            "s16le",
            "-",
        ],
        stdout=subprocess.PIPE,
    )
    assert process.stdout is not None

    try:
        async with websockets.connect(websocket_url) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "start",
                        "encoding": "linear16",
                        "sample_rate": sample_rate,
                        "channels": 1,
                        "source": "pulseaudio_monitor",
                    }
                )
            )
            while True:
                chunk = await asyncio.to_thread(process.stdout.read, chunk_size)
                if not chunk:
                    break
                await websocket.send(chunk)
            await websocket.send(json.dumps({"type": "stop"}))
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


async def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()
    await stream_monitor_audio(
        websocket_url=args.websocket_url,
        source=args.source,
        sample_rate=args.sample_rate,
        chunk_size=args.chunk_size,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(130)
