"""
Measure time-to-first-audio on the Live API.

Perceived speed in a voice assistant is almost entirely one number: how long
after you stop talking before you hear the first syllable back. That splits
into three parts, and only one of them is the model:

    end-of-turn detection  (silence_duration_ms - our setting)
  + model first token      (measured here)
  + local playback buffer  (our setting)

This measures the middle term across models and thinking budgets, so the
choices are made on numbers rather than on which config sounds fastest.

    python -m core.latencybench            # default sweep
    python -m core.latencybench --runs 3

Each run opens a session, sends one short text turn and stops the clock on the
first audio byte. It spends a little voice quota - a few seconds of audio per
run - so keep the run count low.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google import genai
from google.genai import types

from core import keyring

PROMPT = "Say exactly: ready. Nothing else."

CANDIDATES = [
    ("2.5-native-audio-12-2025", "models/gemini-2.5-flash-native-audio-preview-12-2025", None),
    ("2.5-native-audio-12-2025 +no-think", "models/gemini-2.5-flash-native-audio-preview-12-2025", 0),
    ("2.5-native-audio-latest",  "models/gemini-2.5-flash-native-audio-latest", None),
    ("2.5-native-audio-latest +no-think", "models/gemini-2.5-flash-native-audio-latest", 0),
    ("3.1-flash-live",           "models/gemini-3.1-flash-live-preview", None),
    ("3.1-flash-live +no-think", "models/gemini-3.1-flash-live-preview", 0),
]


def _config(thinking_budget: int | None) -> types.LiveConnectConfig:
    cfg: dict = {
        "response_modalities": ["AUDIO"],
        "system_instruction": "You are a terse assistant. Answer in one word.",
    }
    if thinking_budget is not None:
        cfg["thinking_config"] = types.ThinkingConfig(
            thinking_budget=thinking_budget, include_thoughts=False)
    return types.LiveConnectConfig(**cfg)


async def _one_run(client, model: str, thinking_budget: int | None,
                   timeout: float = 45.0) -> float | None:
    """Seconds from sending the turn to the first audio byte, or None."""
    try:
        async with client.aio.live.connect(
                model=model, config=_config(thinking_budget)) as session:
            await session.send_client_content(
                turns={"parts": [{"text": PROMPT}]}, turn_complete=True)
            start = time.perf_counter()
            deadline = start + timeout
            async for response in session.receive():
                if time.perf_counter() > deadline:
                    return None
                data = getattr(response, "data", None)
                if data:
                    return time.perf_counter() - start
                sc = getattr(response, "server_content", None)
                if sc is not None:
                    mt = getattr(sc, "model_turn", None)
                    if mt and getattr(mt, "parts", None):
                        for part in mt.parts:
                            inline = getattr(part, "inline_data", None)
                            if inline and getattr(inline, "data", None):
                                return time.perf_counter() - start
    except Exception as exc:
        raise RuntimeError(str(exc)[:150]) from exc
    return None


async def main() -> int:
    runs = 2
    if "--runs" in sys.argv:
        try:
            runs = max(1, int(sys.argv[sys.argv.index("--runs") + 1]))
        except (ValueError, IndexError):
            pass

    key = keyring.gemini_voice_key()
    if not key:
        print("No Gemini voice key configured.")
        return 1
    client = genai.Client(api_key=key, http_options={"api_version": "v1beta"})

    print("=" * 68)
    print("Time to first audio byte  (%d run(s) each, lower is better)" % runs)
    print("=" * 68)

    results: list[tuple[str, float]] = []
    for label, model, budget in CANDIDATES:
        samples: list[float] = []
        error = ""
        for _ in range(runs):
            try:
                took = await _one_run(client, model, budget)
            except RuntimeError as exc:
                error = str(exc)
                break
            if took is not None:
                samples.append(took)
            await asyncio.sleep(1.0)

        if samples:
            median = statistics.median(samples)
            results.append((label, median))
            print("  %-38s %6.0f ms   (%s)"
                  % (label, median * 1000,
                     ", ".join("%.0f" % (s * 1000) for s in samples)))
        else:
            print("  %-38s   n/a       %s" % (label, error or "no audio returned"))

    if results:
        results.sort(key=lambda t: t[1])
        print("\n" + "=" * 68)
        best_label, best_ms = results[0]
        print("Fastest: %s  at %.0f ms" % (best_label, best_ms * 1000))
        if len(results) > 1:
            worst_label, worst_ms = results[-1]
            print("Slowest: %s  at %.0f ms  (%.0f ms worse)"
                  % (worst_label, worst_ms * 1000, (worst_ms - best_ms) * 1000))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
