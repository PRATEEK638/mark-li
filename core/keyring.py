"""
Credential pools and provider rotation.

Every provider here may hold a single key or a pool of them. Free tiers are
capped per key, not per account, so when one key is exhausted the next in the
pool carries the load instead of the whole provider dropping out.

Read order for a provider named ``x``:

    1. environment variable (``X_API_KEY``) - wins over everything
    2. ``config/api_keys.json`` -> ``"x_api_keys"``  (a list, the pool)
    3. ``config/api_keys.json`` -> ``"x_api_key"``   (a single key)

The Gemini pool is special in two ways. ``gemini_api_key`` is always kept in
sync with the currently active pool entry, so the older call sites that read
that field directly keep working untouched. And ``gemini_voice_api_key`` is
held back from the text pool entirely: the Live voice session is the only
subsystem with no fallback route, so its quota is never spent on text work.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR    = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

# Order the text tier is tried in. Gemini first (best quality, generous free
# tier), Sarvam last because it is prepaid and should never be spent on work a
# free route can do.
DEFAULT_PROVIDER_ORDER = ("gemini", "openrouter", "nvidia", "sarvam")

DEFAULT_MODELS = {
    "gemini":     "gemini-flash-latest",
    "openrouter": "YOUR_API_KEY",
    "nvidia":     "YOUR_API_KEY",
    "sarvam":     "YOUR_API_KEY",
}

_ENV_VARS = {
    "gemini":       "GEMINI_API_KEY",
    "gemini_voice": "GEMINI_VOICE_API_KEY",
    "openrouter":   "OPENROUTER_API_KEY",
    "nvidia":       "NVIDIA_API_KEY",
    "sarvam":       "SARVAM_API_KEY",
}

# How long a provider sits out after it burns through its whole pool. Long
# enough that we stop hammering a dead quota, short enough that a per-minute
# limit has recovered by the time we look again.
COOLDOWN_SEC = 15 * 60

_lock                            = threading.RLock()
_cfg_cache: dict | None          = None
_cfg_mtime: float                = -1.0
_active: dict[str, int]          = {}
_blocked_until: dict[str, float] = {}


# -- config ----------------------------------------------------------------
def load_config(force: bool = False) -> dict:
    """Read api_keys.json, re-reading only when the file changes on disk."""
    global _cfg_cache, _cfg_mtime
    with _lock:
        try:
            mtime = CONFIG_PATH.stat().st_mtime
        except OSError:
            return _cfg_cache or {}
        if force or _cfg_cache is None or mtime != _cfg_mtime:
            try:
                _cfg_cache = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _cfg_mtime = mtime
            except Exception:
                _cfg_cache = _cfg_cache or {}
        return _cfg_cache


def _write_config(data: dict) -> None:
    global _cfg_cache, _cfg_mtime
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=4), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
    _cfg_cache = data
    try:
        _cfg_mtime = CONFIG_PATH.stat().st_mtime
    except OSError:
        _cfg_mtime = -1.0


# -- pools -----------------------------------------------------------------
def _clean(values) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in values:
        s = str(v).strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def pool(provider: str) -> list[str]:
    """Every usable key for a provider, in preference order."""
    env = _ENV_VARS.get(provider)
    if env and os.environ.get(env):
        return _clean(os.environ[env].split(","))
    cfg  = load_config()
    keys = _clean(cfg.get(provider + "_api_keys"))
    if not keys:
        keys = _clean(cfg.get(provider + "_api_key"))
    return keys


def pool_size(provider: str) -> int:
    return len(pool(provider))


def key(provider: str) -> str | None:
    """The currently active key for a provider, or None if it has none."""
    keys = pool(provider)
    if not keys:
        return None
    with _lock:
        return keys[_active.get(provider, 0) % len(keys)]


def active_index(provider: str) -> int:
    return _active.get(provider, 0)


def rotate(provider: str) -> bool:
    """Advance to the next key in a provider's pool.

    Returns False when the pool holds a single key or has been fully cycled,
    which is the caller's signal to fail over to a different provider instead.
    """
    keys = pool(provider)
    with _lock:
        if len(keys) <= 1:
            return False
        current = _active.get(provider, 0)
        if current + 1 >= len(keys):
            return False
        _active[provider] = current + 1
    print("[Keyring] " + provider + ": key " + str(current + 1) +
          " exhausted - rotating to key " + str(current + 2) +
          "/" + str(len(keys)))
    if provider == "gemini":
        _sync_gemini_alias()
    return True


def reset(provider: str) -> None:
    """Send a provider back to its first key and clear any cooldown."""
    with _lock:
        _active.pop(provider, None)
        _blocked_until.pop(provider, None)
    if provider == "gemini":
        _sync_gemini_alias()


def reset_all() -> None:
    with _lock:
        _active.clear()
        _blocked_until.clear()
    _sync_gemini_alias()


# -- cooldowns -------------------------------------------------------------
def block(provider: str, seconds: float = COOLDOWN_SEC) -> None:
    """Sit a provider out after it has burned through its whole pool."""
    with _lock:
        already = time.monotonic() < _blocked_until.get(provider, 0.0)
        _blocked_until[provider] = time.monotonic() + seconds
    if not already:
        print("[Keyring] " + provider + " quota exhausted - resting it for " +
              str(int(seconds // 60)) + " min.")


def available(provider: str) -> bool:
    """True when the provider has a key and is not in cooldown."""
    if not pool(provider):
        return False
    with _lock:
        blocked = _blocked_until.get(provider, 0.0)
        if blocked and time.monotonic() >= blocked:
            # Cooldown served: start again from the top of the pool.
            _blocked_until.pop(provider, None)
            _active.pop(provider, None)
            expired = True
        else:
            expired = False
        ok = time.monotonic() >= _blocked_until.get(provider, 0.0)
    if expired and provider == "gemini":
        _sync_gemini_alias()
    return ok


def provider_order() -> list[str]:
    cfg   = load_config()
    order = cfg.get("cloud_fallback_order") or list(DEFAULT_PROVIDER_ORDER)
    return [p for p in order if isinstance(p, str)]


def model_for(provider: str) -> str:
    cfg = load_config()
    return str(cfg.get(provider + "_model") or DEFAULT_MODELS.get(provider, "")).strip()


def resolve_model(name: str) -> str:
    """Apply the configured Gemini model aliases.

    The action modules each hardcode a model name, and Google's floating
    ``-latest`` aliases can sit at 503 for hours while a pinned sibling answers
    in a second. Rather than edit fifteen call sites every time that happens,
    they are remapped here from ``gemini_model_aliases`` in the config - one
    knob, and an empty map means the call sites are used verbatim.
    """
    if not name:
        return name
    aliases = load_config().get("gemini_model_aliases")
    if isinstance(aliases, dict):
        return str(aliases.get(name, name)).strip() or name
    return name


# -- gemini helpers --------------------------------------------------------
def _sync_gemini_alias() -> None:
    """Keep the legacy ``gemini_api_key`` field pointing at the active key.

    Several action modules still read that field straight out of the JSON.
    This keeps them on the same key the rotation logic has chosen, without
    having to touch every one of them.
    """
    active = key("gemini")
    if not active:
        return
    try:
        cfg = dict(load_config())
        if cfg.get("gemini_api_key") != active:
            cfg["gemini_api_key"] = active
            _write_config(cfg)
    except Exception as exc:
        print("[Keyring] could not sync gemini_api_key: " + str(exc))


def gemini_key() -> str:
    """Active Gemini key for text, vision and tool work."""
    return key("gemini") or ""


def gemini_voice_key() -> str:
    """Key for the Live voice session.

    Falls back to the text pool when no dedicated voice key is configured, but
    a dedicated one is strongly preferred: voice is the only subsystem that has
    nothing to degrade to when its quota runs out.
    """
    return key("gemini_voice") or gemini_key()


# -- error classification --------------------------------------------------
_QUOTA_MARKERS = (
    "429", "resource_exhausted", "resourceexhausted", "quota",
    "rate limit", "rate_limit", "ratelimit", "too many requests",
    "insufficient_quota", "insufficient credits",
)


def is_quota_error(exc) -> bool:
    """True when an error means 'this key is spent', not 'this request is bad'."""
    msg = str(exc).lower()
    return any(m in msg for m in _QUOTA_MARKERS)


_AUTH_MARKERS = ("401", "403", "unauthorized", "invalid api key",
                 "api key not valid", "permission_denied", "api_key_invalid")


def is_auth_error(exc) -> bool:
    """True when a key is rejected outright - rotating past it is the fix."""
    msg = str(exc).lower()
    return any(m in msg for m in _AUTH_MARKERS)


_TRANSIENT_MARKERS = (
    "503", "502", "500", "unavailable", "overloaded", "high demand",
    "internal error", "deadline", "timed out", "timeout",
    "connection reset", "connection aborted", "temporarily",
)


def is_transient_error(exc) -> bool:
    """True for a server-side wobble rather than anything about the key.

    Worth separating from a quota error, because the two want opposite
    responses. A spent key is fixed by rotating to the next key; a model that
    is "experiencing high demand" is not - every key hits the same busy model,
    so the only useful move is a different provider, and the key pool must not
    be burned discovering that.
    """
    msg = str(exc).lower()
    if is_quota_error(msg) or is_auth_error(msg):
        return False
    return any(m in msg for m in _TRANSIENT_MARKERS)


def status() -> dict:
    """Snapshot of every provider, for the UI and for setupcheck."""
    out: dict = {}
    for p in ("gemini", "gemini_voice", "openrouter", "nvidia", "sarvam"):
        keys = pool(p)
        out[p] = {
            "keys":      len(keys),
            "active":    active_index(p) + 1 if keys else 0,
            "model":     model_for(p),
            "available": available(p),
        }
    return out


# Bring the legacy alias in line with the pool as soon as the module loads.
try:
    _sync_gemini_alias()
except Exception:
    pass
