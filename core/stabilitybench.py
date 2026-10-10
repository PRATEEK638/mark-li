"""
Which Live config survives a real conversation?

A live session died ten times in one sitting with
``1011 Deadline expired before operation could complete``, each death costing a
reconnect - which is what "it goes silent after a sentence or two" actually is.

Latency benchmarking could not catch this: it opens a session, takes one turn
and closes. Instability only shows up across several turns with human-length
pauses between them, so that is what this does. Each config gets one session,
several turns, and a gap in between; anything that closes the socket is
recorded against that config.

    python -m core.stabilitybench
    python -m core.stabilitybench --turns 4 --gap 6
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from google.genai import types

from core import keyring

PROMPTS = [
    "Say the word one.",
    "Now say the word two.",
    "Now say the word three.",
    "Now say the word four.",
    "Now say the word five.",
]

CURRENT = "models/gemini-2.5-flash-native-audio-latest"
PREVIOUS = "models/gemini-2.5-flash-native-audio-preview-12-2025"


def _vad() -> types.RealtimeInputConfig:
    return types.RealtimeInputConfig(
        automatic_activity_detection=types.AutomaticActivityDetection(
            start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
            end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
            prefix_padding_ms=200,
            silence_duration_ms=550,
        ),
        activity_handling=types.ActivityHandling.START_OF_ACTIVITY_INTERRUPTS,
        turn_coverage=types.TurnCoverage.TURN_INCLUDES_ONLY_ACTIVITY,
    )


def build(no_think: bool, vad: bool, transcripts: bool) -> types.LiveConnectConfig:
    cfg: dict = {
        "response_modalities": ["AUDIO"],
        "system_instruction": "Answer with exactly the word asked for.",
        "session_resumption": types.SessionResumptionConfig(),
        "context_window_compression": types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow()),
    }
    if no_think:
        cfg["thinking_config"] = types.ThinkingConfig(
            thinking_budget=0, include_thoughts=False)
    if vad:
        cfg["realtime_input_config"] = _vad()
    if transcripts:
        cfg["output_audio_transcription"] = {}
        cfg["input_audio_transcription"] = types.AudioTranscriptionConfig(
            language_codes=["en-IN", "hi-IN", "en-US"])
    return types.LiveConnectConfig(**cfg)


CASES = [
    # label,                       model,    no_think, vad,  transcripts
    ("current (everything on)",    CURRENT,  True,     True,  True),
    ("current, thinking left on",  CURRENT,  False,    True,  True),
    ("current, no VAD tuning",     CURRENT,  True,     False, True),
    ("current, bare",              CURRENT,  False,    False, False),
    ("previous model, everything", PREVIOUS, True,     True,  True),
    ("previous model, bare",       PREVIOUS, False,    False, False),
]


async def run_case(client, model, cfg, turns: int, gap: float) -> tuple[int, str]:
    """Turns completed before the socket died, plus the failure reason."""
    completed = 0
    try:
        async with client.aio.live.connect(model=model, config=cfg) as session:
            for i in range(turns):
                await session.send_client_content(
                    turns={"parts": [{"text": PROMPTS[i % len(PROMPTS)]}]},
                    turn_complete=True)
                got_audio = False
                deadline = time.monotonic() + 45
                async for response in session.receive():
                    if getattr(response, "data", None):
                        got_audio = True
                    sc = getattr(response, "server_content", None)
                    if sc is not None and getattr(sc, "turn_complete", False):
                        break
                    if time.monotonic() > deadline:
                        return completed, "timed out waiting for turn_complete"
                if got_audio:
                    completed += 1
                # A human pause. Idle time is what the deadline errors punish.
                await asyncio.sleep(gap)
    except Exception as exc:
        return completed, type(exc).__name__ + ": " + str(exc)[:110]
    return completed, ""


async def main() -> int:
    turns, gap = 4, 5.0
    if "--turns" in sys.argv:
        turns = int(sys.argv[sys.argv.index("--turns") + 1])
    if "--gap" in sys.argv:
        gap = float(sys.argv[sys.argv.index("--gap") + 1])

    key = keyring.gemini_voice_key()
    if not key:
        print("No Gemini voice key.")
        return 1
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})

    print("=" * 72)
    print("Live session stability: %d turns, %.0fs pause between them" % (turns, gap))
    print("=" * 72)

    for label, model, no_think, vad, transcripts in CASES:
        cfg = build(no_think, vad, transcripts)
        done, err = await run_case(client, model, cfg, turns, gap)
        verdict = "OK  " if done == turns and not err else "FAIL"
        print("  [%s] %-30s %d/%d turns  %s" % (verdict, label, done, turns, err))
        await asyncio.sleep(2)

    print("\nA config that cannot finish %d turns will drop the conversation "
          "in normal use." % turns)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
