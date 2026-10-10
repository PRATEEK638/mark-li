"""
Check every configured model route end to end.

Run before trusting a fresh install or a changed ``api_keys.json``::

    python -m core.selftest          # config + a live call per provider
    python -m core.selftest --dry    # config only, no network, no quota spent

Each provider gets one tiny completion, which is the only way to tell a key
that is present from a key that actually works. ``--dry`` skips that when you
just want to see what is wired up.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import cloud_llm, keyring  # noqa: E402

OK   = "  OK  "
FAIL = " FAIL "
SKIP = " SKIP "


def _line(mark: str, name: str, detail: str = "") -> None:
    print("[" + mark + "] " + name.ljust(16) + detail)


def check_config() -> bool:
    print("\n--- configuration " + "-" * 44)
    if not keyring.CONFIG_PATH.exists():
        _line(FAIL, "api_keys.json", "missing at " + str(keyring.CONFIG_PATH))
        return False

    cfg = keyring.load_config(force=True)
    _line(OK, "api_keys.json", str(keyring.CONFIG_PATH))
    _line(OK, "assistant", str(cfg.get("assistant_name") or "AURA"))
    _line(OK, "user", str(cfg.get("user_name") or "(unset)"))

    print("\n--- credentials " + "-" * 46)
    ok = True
    for name, info in keyring.status().items():
        if info["keys"]:
            detail = str(info["keys"]) + " key(s)"
            if info["model"]:
                detail += ", model " + info["model"]
            _line(OK, name, detail)
        else:
            mark = SKIP if name == "gemini_voice" else FAIL
            note = ("no dedicated voice key - the Live session will share the "
                    "text pool" if name == "gemini_voice" else "no key configured")
            _line(mark, name, note)
            if name == "gemini":
                ok = False

    if keyring.pool_size("gemini_voice") == 0:
        print("\n  Note: voice is the only subsystem with no fallback route.")
        print("  A dedicated gemini_voice_api_key stops heavy text use from")
        print("  muting the assistant.")
    return ok


def check_gemini() -> bool:
    """Gemini on its own, with no fallback.

    Deliberately bypasses the router: routing through it would let a healthy
    fallback provider answer and report a green tick while Gemini was down,
    which is the one thing this test exists to notice.
    """
    print("\n--- gemini (text) " + "-" * 44)
    if not keyring.pool_size("gemini"):
        _line(SKIP, "gemini", "no keys")
        return False
    from core import ai_router
    try:
        start = time.monotonic()
        resp  = ai_router._gemini_once(
            keyring.gemini_key(), "Reply with the single word: ready",
            "gemini-flash-latest", None)
        took = time.monotonic() - start
        _line(OK, "gemini", "%.1fs  %r" % (took, (resp.text or "").strip()[:40]))
        return True
    except Exception as exc:
        if keyring.is_quota_error(exc):
            spare = keyring.pool_size("gemini") - 1
            kind  = ("key %d of %d is rate-limited; the pool has %d more"
                     % (keyring.active_index("gemini") + 1,
                        keyring.pool_size("gemini"), spare)) if spare > 0 \
                else "every key in the pool is rate-limited"
        elif keyring.is_transient_error(exc):
            kind = "transient - the router will fall back"
        elif keyring.is_auth_error(exc):
            kind = "key rejected - check it"
        else:
            kind = "unexpected"
        _line(FAIL, "gemini", "(" + kind + ") " + str(exc)[:100])
        return False


def check_cloud() -> bool:
    print("\n--- fallback tier " + "-" * 44)
    any_ok = False
    for provider in cloud_llm.OPENAI_COMPAT_PROVIDERS:
        if not keyring.pool_size(provider):
            _line(SKIP, provider, "no keys")
            continue
        try:
            start = time.monotonic()
            text  = cloud_llm.call_provider(
                provider, "Reply with the single word: ready",
                max_tokens=16, temperature=0.0)
            took = time.monotonic() - start
            _line(OK, provider,
                  "%.1fs  %r" % (took, text.strip()[:40]))
            any_ok = True
        except Exception as exc:
            _line(FAIL, provider, str(exc)[:140])
    return any_ok


def check_voice_key() -> bool:
    """Confirm the Live key is accepted, without opening a Live session."""
    print("\n--- gemini (voice key) " + "-" * 39)
    key = keyring.gemini_voice_key()
    if not key:
        _line(FAIL, "voice key", "none available")
        return False
    try:
        import requests
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": key, "pageSize": 1}, timeout=30)
        if r.status_code == 200:
            dedicated = "dedicated" if keyring.pool_size("gemini_voice") \
                else "shared with the text pool"
            _line(OK, "voice key", "accepted (" + dedicated + ")")
            return True
        _line(FAIL, "voice key", "HTTP " + str(r.status_code) + " " + r.text[:100])
        return False
    except Exception as exc:
        _line(FAIL, "voice key", str(exc)[:140])
        return False


def check_router() -> bool:
    """What the action modules actually call: answer from wherever you can."""
    print("\n--- router (end to end) " + "-" * 38)
    try:
        from core import ai_router
        start = time.monotonic()
        resp  = ai_router.generate_content("Reply with the single word: ready")
        took  = time.monotonic() - start
        via   = getattr(resp, "provider", "gemini")
        _line(OK, "generate", "%.1fs via %s  %r"
              % (took, via, (resp.text or "").strip()[:40]))
        return True
    except Exception as exc:
        _line(FAIL, "generate", str(exc)[:140])
        return False


def main() -> int:
    dry = "--dry" in sys.argv
    print("=" * 62)
    print("AURA self-test" + ("  (dry run - no network)" if dry else ""))
    print("=" * 62)

    config_ok = check_config()
    if dry:
        print("\n" + "=" * 62)
        return 0 if config_ok else 1

    voice_ok  = check_voice_key()
    gemini_ok = check_gemini()
    cloud_ok  = check_cloud()
    router_ok = check_router()

    print("\n" + "=" * 62)
    if gemini_ok and voice_ok and cloud_ok:
        print("All routes healthy. Start with:  python main.py")
        return 0
    if voice_ok and router_ok:
        print("Ready, with a degraded route - see the FAIL lines above.")
        print("The router answered anyway, so the assistant will work.")
        return 0
    if router_ok and not voice_ok:
        print("Text works but voice does not. Fix the voice key before "
              "starting: Live audio has no fallback.")
        return 1
    print("Not ready. Fix the FAIL lines above before starting.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
