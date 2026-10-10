"""
The non-Gemini half of the text tier: OpenRouter, NVIDIA NIM and Sarvam.

All three speak ``/v1/chat/completions``, so they share one transport and
differ only in base URL, auth header and model name. They exist so that a
Gemini quota wall stops being fatal: when Gemini has burned through its whole
key pool, text work carries on here instead of failing.

Voice never reaches this module. Gemini Live is the only route that can do
real-time native audio, so there is nothing here to degrade to - which is
exactly why ``gemini_voice_api_key`` is kept out of the text pool.
"""

from __future__ import annotations

import json

import requests

from core import keyring

TIMEOUT_S = 90

# base URL, auth header, and how the key is presented in it.
_ENDPOINTS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "header":   "Authorization",
        "prefix":   "Bearer ",
        # OpenRouter asks callers to identify themselves; it also unlocks the
        # higher free-tier rate limit.
        "extra":    {"HTTP-Referer": "https://github.com/ayush/aura",
                     "X-Title":      "AURA"},
    },
    "nvidia": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "header":   "Authorization",
        "prefix":   "Bearer ",
        "extra":    {},
    },
    "sarvam": {
        "base_url": "https://api.sarvam.ai/v1",
        "header":   "api-subscription-key",
        "prefix":   "",
        "extra":    {},
    },
}

OPENAI_COMPAT_PROVIDERS = tuple(_ENDPOINTS)


class CloudLLMError(RuntimeError):
    """Every configured route refused or failed."""


def _as_messages(contents, system: str | None) -> list[dict]:
    """Normalise the caller's prompt into chat messages."""
    msgs: list[dict] = []
    if system:
        msgs.append({"role": "system", "content": system})
    if isinstance(contents, str):
        msgs.append({"role": "user", "content": contents})
    elif isinstance(contents, dict) and "role" in contents:
        msgs.append(contents)
    elif isinstance(contents, (list, tuple)):
        if contents and all(isinstance(c, dict) and "role" in c for c in contents):
            msgs.extend(contents)
        else:
            # A Gemini-style parts list. Only the text parts survive the trip;
            # callers that pass images are filtered out before they get here.
            text = "\n\n".join(str(c) for c in contents if isinstance(c, str))
            msgs.append({"role": "user", "content": text})
    else:
        msgs.append({"role": "user", "content": str(contents)})
    return msgs


def _call_once(provider: str, api_key: str, messages: list[dict],
               model: str, temperature: float, max_tokens: int | None,
               timeout: int) -> str:
    spec    = _ENDPOINTS[provider]
    headers = {"Content-Type": "application/json"}
    headers[spec["header"]] = spec["prefix"] + api_key
    headers.update(spec["extra"])

    payload: dict = {
        "model":       model,
        "messages":    messages,
        "temperature": temperature,
    }
    if max_tokens:
        payload["max_tokens"] = max_tokens

    resp = requests.post(spec["base_url"] + "/chat/completions",
                         headers=headers, json=payload, timeout=timeout)

    if resp.status_code != 200:
        raise RuntimeError(
            "HTTP " + str(resp.status_code) + " from " + provider +
            ": " + resp.text[:400])

    try:
        data = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(provider + " returned non-JSON: " +
                           resp.text[:200]) from exc

    # An error can still arrive inside a 200 on some gateways.
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(provider + " error: " + str(data["error"])[:400])

    choices = (data or {}).get("choices") or []
    if not choices:
        raise RuntimeError(provider + " returned no choices")
    text = (choices[0].get("message") or {}).get("content") or ""
    text = text.strip()
    if not text:
        raise RuntimeError(provider + " returned an empty completion")
    return text


def call_provider(provider: str, contents, *, system: str | None = None,
                  model: str | None = None, temperature: float = 0.7,
                  max_tokens: int | None = None,
                  timeout: int = TIMEOUT_S) -> str:
    """One provider, walking its whole key pool before giving up.

    Rotation happens on quota and auth failures only. A malformed request would
    fail identically on every key, so it is raised immediately rather than
    burning the pool discovering that.
    """
    if provider not in _ENDPOINTS:
        raise CloudLLMError("unknown provider: " + provider)

    messages = _as_messages(contents, system)
    model    = model or keyring.model_for(provider)
    attempts = max(1, keyring.pool_size(provider))
    last_err: Exception | None = None

    for _ in range(attempts):
        api_key = keyring.key(provider)
        if not api_key:
            break
        try:
            return _call_once(provider, api_key, messages, model,
                              temperature, max_tokens, timeout)
        except Exception as exc:
            last_err = exc
            if keyring.is_quota_error(exc) or keyring.is_auth_error(exc):
                if keyring.rotate(provider):
                    continue
                keyring.block(provider)
            break

    raise CloudLLMError(provider + " unavailable: " + str(last_err))


def chat_with_provider(contents, *, system: str | None = None,
                       temperature: float = 0.7, max_tokens: int | None = None,
                       timeout: int = TIMEOUT_S, providers=None):
    """Walk the OpenAI-compatible providers in order until one answers.

    Returns ``(text, provider, model)``. The provider is reported rather than
    inferred: which route answered is the thing worth knowing when an answer
    looks unlike Gemini's, and guessing it from the availability list gets it
    wrong the moment the first provider is the one that failed.
    """
    order = [p for p in (providers or keyring.provider_order())
             if p in _ENDPOINTS]
    errors: list[str] = []

    for provider in order:
        if not keyring.available(provider):
            continue
        model = keyring.model_for(provider)
        try:
            answer = call_provider(provider, contents, system=system,
                                   temperature=temperature,
                                   max_tokens=max_tokens, timeout=timeout)
            print("[CloudLLM] answered by " + provider + " (" + model + ")")
            return answer, provider, model
        except Exception as exc:
            errors.append(str(exc))

    raise CloudLLMError("no cloud provider could answer. " + " | ".join(errors))


def chat(contents, **kwargs) -> str:
    """``chat_with_provider`` for callers that only want the text."""
    return chat_with_provider(contents, **kwargs)[0]


def available_providers() -> list[str]:
    """Which OpenAI-compatible routes are configured and not resting."""
    return [p for p in keyring.provider_order()
            if p in _ENDPOINTS and keyring.available(p)]
