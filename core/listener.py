"""
The listening layer: decides, frame by frame, whether the microphone should be
heard.

The problem it solves
---------------------
The original capture callback dropped every microphone frame while the
assistant was speaking::

    if not jarvis_speaking and not muted and not phone_active:
        forward(frame)

That is a hard half-duplex gate, and it makes voice barge-in impossible - you
cannot talk over the assistant, only click a button. It exists for a real
reason: with the mic open, the assistant hears its own voice through the
speakers, treats it as user speech, and interrupts itself in a loop.

So the gate cannot simply be deleted; the echo has to be distinguished from a
person. Without a full acoustic echo canceller, the workable signal is loudness
- but a fixed threshold fails, because it depends on speaker volume, mic gain
and room. Instead this measures the echo directly:

  * while the assistant is speaking and nobody has interrupted, whatever the
    microphone picks up *is* the echo. Tracking its level gives a live estimate
    of exactly what has to be spoken over.
  * a barge-in is then energy well above that measured echo, sustained long
    enough not to be a cough, a keyboard, or a consonant burst.

On headphones the echo level sits near the noise floor and barge-in becomes
effortless. On speakers it adapts to however loud they are.

The noise floor itself is tracked the same way, from quiet frames only, so a
noisy room raises the bar instead of triggering constantly.
"""

from __future__ import annotations

import math
import time

try:
    import numpy as np
    _NUMPY = True
except ImportError:
    _NUMPY = False


class Decision:
    """What to do with one captured frame."""

    __slots__ = ("forward", "barge_in", "rms", "reason")

    def __init__(self, forward: bool, barge_in: bool = False,
                 rms: float = 0.0, reason: str = ""):
        self.forward  = forward
        self.barge_in = barge_in
        self.rms      = rms
        self.reason   = reason


# Presets, chosen by "listener_sensitivity" in config/api_keys.json.
#   headphones - no acoustic path from speaker to mic, so barge-in can be easy
#   balanced   - laptop speakers at normal volume (the default)
#   noisy      - loud speakers or a busy room; needs a clearer interruption
PRESETS = {
    "headphones": {"echo_margin_db": 6.0,  "barge_ms": 120, "floor_margin_db": 8.0},
    "balanced":   {"echo_margin_db": 11.0, "barge_ms": 220, "floor_margin_db": 11.0},
    "noisy":      {"echo_margin_db": 16.0, "barge_ms": 340, "floor_margin_db": 15.0},
}

_EPS = 1e-9


def _rms(pcm: bytes) -> float:
    """Root-mean-square of a little-endian int16 frame, normalised to 0-1."""
    if not pcm:
        return 0.0
    if _NUMPY:
        samples = np.frombuffer(pcm, dtype=np.int16)
        if samples.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(samples.astype(np.float32) ** 2)) / 32768.0)
    total = count = 0
    for i in range(0, len(pcm) - 1, 2):
        v = int.from_bytes(pcm[i:i + 2], "little", signed=True)
        total += v * v
        count += 1
    return math.sqrt(total / count) / 32768.0 if count else 0.0


def _db(value: float) -> float:
    return 20.0 * math.log10(max(value, _EPS))


class IntelligentListener:
    """Frame-level gate in front of the Live session's own VAD.

    This does not decide when a turn ends - Gemini's server-side VAD does that,
    and it is far better at it. This decides only whether a frame is worth
    sending at all, which is the one judgement the server cannot make: it
    never hears the assistant's own output coming back through the mic.
    """

    def __init__(self, sample_rate: int = 16000, sensitivity: str = "balanced",
                 barge_in: bool = False, on_log=None):
        preset = PRESETS.get(sensitivity, PRESETS["balanced"])
        self.sample_rate     = sample_rate
        self.sensitivity     = sensitivity if sensitivity in PRESETS else "balanced"
        self.echo_margin_db  = preset["echo_margin_db"]
        self.floor_margin_db = preset["floor_margin_db"]
        self.barge_ms        = preset["barge_ms"]
        self._log            = on_log

        # Off by default. Talking over the assistant is a real improvement, but
        # only when the echo estimate is right; when it is wrong the assistant
        # interrupts itself and the conversation falls apart, which is far
        # worse than simply having to wait for it to finish. Opt in with
        # "barge_in": true once the levels are known to be sane - the listener
        # keeps measuring either way, so `describe()` shows what it would do.
        self.barge_in_enabled = bool(barge_in)

        # Ignore the opening moments of each reply while the echo tracker
        # catches up to this turn's actual loudness.
        self.calibrate_ms  = 700.0
        self._speaking_ms  = 0.0

        # Start pessimistic: a quiet room reveals itself within a second, and
        # starting low would fire on the first breath.
        self._noise_floor = 0.02
        self._echo_level  = 0.02

        # Accumulated duration of consecutive loud audio, in milliseconds of
        # *audio*, not wall clock. Frames can be delivered late or in bursts by
        # the host audio stack; measuring how much sound actually arrived is
        # both truer to the signal and deterministic to test.
        self._loud_ms: float    = 0.0
        self._barged            = False
        self._assistant_was     = False
        self._frames            = 0
        self._forwarded         = 0
        self._suppressed        = 0
        self._barge_ins         = 0
        self._last_speech_at    = 0.0

    # -- adaptation --------------------------------------------------------
    def _track_noise(self, rms: float) -> None:
        """Follow the room's quiet level: fall fast, rise slowly."""
        alpha = 0.05 if rms < self._noise_floor else 0.005
        self._noise_floor = (1 - alpha) * self._noise_floor + alpha * rms

    def _track_echo(self, rms: float) -> None:
        """Follow what the assistant's own voice measures at the mic.

        This must run on EVERY frame while the assistant speaks, including the
        loud ones. An earlier version only sampled frames that fell below the
        barge-in threshold, which deadlocks: if the speakers are louder than
        the initial guess, no frame ever qualifies, the estimate never rises,
        and every frame of the assistant's own voice looks like an
        interruption. It then interrupted itself a second into every reply -
        which presents as the assistant getting confused and going quiet.
        """
        alpha = 0.12 if rms > self._echo_level else 0.04
        self._echo_level = (1 - alpha) * self._echo_level + alpha * rms

    # -- thresholds --------------------------------------------------------
    @property
    def speech_threshold(self) -> float:
        return self._noise_floor * (10 ** (self.floor_margin_db / 20.0))

    @property
    def barge_threshold(self) -> float:
        """Loud enough to be a person talking over the assistant.

        Referenced to the measured echo AND the room floor, whichever is
        higher: on headphones the echo is negligible and the room floor is what
        matters, on loud speakers it is the other way round.
        """
        from_echo  = self._echo_level * (10 ** (self.echo_margin_db / 20.0))
        from_floor = self.speech_threshold
        return max(from_echo, from_floor)

    # -- main entry point --------------------------------------------------
    def observe(self, pcm: bytes, assistant_speaking: bool,
                muted: bool = False) -> Decision:
        self._frames += 1
        rms = _rms(pcm)
        now = time.monotonic()

        if muted:
            self._reset_barge()
            return Decision(False, rms=rms, reason="muted")

        # The assistant just stopped: forget the barge-in latch so the next
        # reply starts from a clean state.
        if self._assistant_was and not assistant_speaking:
            self._reset_barge()
        self._assistant_was = assistant_speaking

        if not assistant_speaking:
            # Free-running: let the server VAD do its job and send everything.
            # Quiet frames still teach us the room's noise floor.
            if rms < self.speech_threshold:
                self._track_noise(rms)
            else:
                self._last_speech_at = now
            self._forwarded += 1
            return Decision(True, rms=rms, reason="open")

        # -- the assistant is talking -------------------------------------
        if self._barged:
            # Already interrupted this turn; stay open until it stops.
            self._forwarded += 1
            return Decision(True, rms=rms, reason="post-barge")

        # Learn the echo from every frame, loud ones included - see _track_echo.
        threshold = self.barge_threshold
        self._track_echo(rms)

        # Barge-in stays disabled until the echo estimate has had time to
        # settle on this turn. Without this, the very first frames of a reply
        # are judged against a stale estimate from a quieter moment and trip
        # the interrupt before the tracker has caught up.
        self._speaking_ms += self._frame_ms(pcm)
        if self._speaking_ms < self.calibrate_ms:
            self._loud_ms = 0.0
            self._suppressed += 1
            return Decision(False, rms=rms, reason="calibrating")

        if not self.barge_in_enabled:
            self._loud_ms = 0.0
            self._suppressed += 1
            return Decision(False, rms=rms, reason="half-duplex")

        if rms <= threshold:
            self._loud_ms = 0.0
            self._suppressed += 1
            return Decision(False, rms=rms, reason="echo")

        # Louder than the echo. Require it to persist: a door, a cough or a
        # plosive clears the threshold for one frame, a sentence does not.
        self._loud_ms += self._frame_ms(pcm)
        if self._loud_ms < self.barge_ms:
            self._suppressed += 1
            return Decision(False, rms=rms, reason="confirming")

        self._barged = True
        self._barge_ins += 1
        self._last_speech_at = now
        if self._log:
            self._log("barge-in: %.0f dB over echo" % (_db(rms) - _db(self._echo_level)))
        self._forwarded += 1
        return Decision(True, barge_in=True, rms=rms, reason="barge-in")

    def _frame_ms(self, pcm: bytes) -> float:
        """Duration of one int16 mono frame, in milliseconds."""
        return (len(pcm) / 2) / self.sample_rate * 1000.0

    def _reset_barge(self) -> None:
        self._barged      = False
        self._loud_ms     = 0.0
        self._speaking_ms = 0.0

    # -- introspection -----------------------------------------------------
    def stats(self) -> dict:
        return {
            "frames":          self._frames,
            "forwarded":       self._forwarded,
            "suppressed":      self._suppressed,
            "barge_ins":       self._barge_ins,
            "noise_floor_db":  round(_db(self._noise_floor), 1),
            "echo_level_db":   round(_db(self._echo_level), 1),
            "speech_thr_db":   round(_db(self.speech_threshold), 1),
            "barge_thr_db":    round(_db(self.barge_threshold), 1),
            "sensitivity":     self.sensitivity,
        }

    def describe(self) -> str:
        s = self.stats()
        return ("listener[%s] floor %.0fdB echo %.0fdB barge>%.0fdB "
                "(%d frames, %d suppressed, %d barge-ins)"
                % (s["sensitivity"], s["noise_floor_db"], s["echo_level_db"],
                   s["barge_thr_db"], s["frames"], s["suppressed"],
                   s["barge_ins"]))
