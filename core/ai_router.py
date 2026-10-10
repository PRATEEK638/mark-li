"""
One entry point for every non-voice model call.

The action modules used to each build their own ``genai.Client`` from a single
key read straight out of the config file, which meant one exhausted Gemini
quota took the whole assistant's brain offline. They now go through
``generate_content`` here instead, which:

  1. tries Gemini, walking the whole key pool rather than one key;
  2. on a quota wall, falls back to OpenRouter -> NVIDIA -> Sarvam;
  3. rests a provider that is spent so the next call does not re-discover it.

When Gemini answers, the caller gets Gemini's own response object back
untouched, so ``.candidates``, ``.usage_metadata`` and everything else keep
working. Only a cloud fallback returns a shim, and that shim carries ``.text``
- which is all any caller in this project reads.

Requests carrying images stay on Gemini: the fallback tier is text-only, and a
silent text-only answer to a question about a screenshot is worse than a clean
failure.
"""

from __future__ import annotations

from core import cloud_llm, keyring

DEFAULT_MODEL = "gemini-flash-latest"


class CloudResponse:
    """Minimal stand-in for a Gemini response, produced by a fallback route."""

    def __init__(self, text: str, provider: str, model: str):
        self.text     = text
        self.provider = provider
        self.model    = model
        self.candidates = []

    def __str__(self) -> str:
        return self.text

    def __repr__(self) -> str:
        return "<CloudResponse " + self.provider + "/" + self.model + ">"


class AllRoutesFailed(RuntimeError):
    """Neither Gemini nor any fallback provider could answer."""


def _is_text_only(contents) -> bool:
    """False when the payload carries an image or any other non-text part."""
    if isinstance(contents, str):
        return True
    if isinstance(contents, (list, tuple)):
        return all(isinstance(c, str) for c in contents)
    return False


def _gemini_once(api_key: str, contents, model: str, config):
    from google import genai
    model  = keyring.resolve_model(model)
    client = genai.Client(api_key=api_key)
    if config is not None:
        return client.models.generate_content(model=model, contents=contents,
                                              config=config)
    return client.models.generate_content(model=model, contents=contents)


def _try_gemini(contents, model: str, config):
    """Gemini, walking its key pool. Returns None when the pool is spent."""
    if not keyring.available("gemini"):
        return None

    attempts = max(1, keyring.pool_size("gemini"))
    last_err = None

    for _ in range(attempts):
        api_key = keyring.gemini_key()
        if not api_key:
            return None
        try:
            return _gemini_once(api_key, contents, model, config)
        except Exception as exc:
            last_err = exc
            if keyring.is_quota_error(exc) or keyring.is_auth_error(exc):
                if keyring.rotate("gemini"):
                    continue
                keyring.block("gemini")
                return None
            if keyring.is_transient_error(exc):
                # The model is busy or the endpoint wobbled. Every key would
                # hit the same busy model, so don't spend the pool on it -
                # hand straight over to another provider.
                print("[Router] Gemini unavailable (" + str(exc)[:80] +
                      ") - falling back.")
                return None
            # A genuine request error would fail the same way on every key and
            # on every fallback provider, so surface it instead of hiding it
            # behind a chain of retries.
            raise

    if last_err is not None:
        keyring.block("gemini")
    return None


def _system_from_config(config) -> str | None:
    """Pull ``system_instruction`` out of a Gemini config, dict or object.

    Callers hand Gemini its system prompt inside a ``GenerateContentConfig``.
    The fallback providers take it as a separate ``system`` message, so it has
    to be lifted out rather than dropped - losing it would quietly change the
    answer's format on exactly the calls that parse the result as JSON.
    """
    if config is None:
        return None
    if isinstance(config, dict):
        value = config.get("system_instruction")
    else:
        value = getattr(config, "system_instruction", None)
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value if isinstance(v, str))
    return str(value)


def _grounded(config) -> bool:
    """True when the request depends on Gemini's google_search grounding.

    No fallback provider can search the web, so a grounded request that loses
    Gemini must fail loudly rather than return a confident, ungrounded guess.
    """
    if config is None:
        return False
    tools = config.get("tools") if isinstance(config, dict) \
        else getattr(config, "tools", None)
    return bool(tools)


def generate_content(contents, model: str | None = None, config=None,
                     system: str | None = None, temperature: float = 0.7,
                     max_tokens: int | None = None):
    """Gemini first, then the cloud fallback chain. Never returns None."""
    model = model or DEFAULT_MODEL

    result = _try_gemini(contents, model, config)
    if result is not None:
        return result

    if not _is_text_only(contents):
        raise AllRoutesFailed(
            "Gemini is out of quota and this request carries an image - the "
            "fallback providers are text-only.")

    if _grounded(config):
        raise AllRoutesFailed(
            "Gemini is out of quota and this request needs live web "
            "grounding, which no fallback provider offers.")

    system = system or _system_from_config(config)

    try:
        text, provider, used_model = cloud_llm.chat_with_provider(
            contents, system=system, temperature=temperature,
            max_tokens=max_tokens)
    except Exception as exc:
        raise AllRoutesFailed("every model route failed: " + str(exc)) from exc

    return CloudResponse(text, provider, used_model)


def generate_text(contents, model: str | None = None, **kwargs) -> str:
    """``generate_content`` for callers that only want the string."""
    return (generate_content(contents, model=model, **kwargs).text or "").strip()


class _Models:
    """Mirrors ``client.models`` so existing call sites need no reshaping."""

    def generate_content(self, model: str | None = None, contents=None,
                         config=None, **kwargs):
        return generate_content(contents, model=model, config=config, **kwargs)


class ResilientClient:
    """Drop-in replacement for ``genai.Client()`` across the action modules."""

    def __init__(self, model: str | None = None):
        self.models   = _Models()
        self._default = model or DEFAULT_MODEL

    def generate_content(self, contents, model: str | None = None, **kwargs):
        return generate_content(contents, model=model or self._default, **kwargs)


def client(model: str | None = None) -> ResilientClient:
    return ResilientClient(model)


def route_status() -> dict:
    """What the text tier currently has available - surfaced in the HUD."""
    return keyring.status()
