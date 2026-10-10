"""
Spotify control.

Two capabilities with very different reliability, kept honestly separate:

  * **Transport** - play/pause, next, previous, volume. These go through the
    OS media keys, which Spotify registers as a global handler. No credentials,
    no window focus needed, works every time.

  * **Playing a named song** - needs the track's Spotify ID, and the only way
    to turn "play bohemian rhapsody" into an ID is Spotify's search API. With
    API credentials configured this plays the exact track. Without them the
    best available action is to open Spotify's search results for the query,
    and the caller is told that is what happened rather than being told the
    song is playing.

Credentials are optional and free: create an app at
https://developer.spotify.com/dashboard and put the ID and secret in
config/api_keys.json as spotify_client_id / spotify_client_secret. This uses
the client-credentials flow, which needs no user login and can only search -
it cannot see or change your account.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

try:
    import pyautogui
    _KEYS = True
except Exception:
    _KEYS = False

import requests

_TOKEN_URL  = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"

_token: str = ""
_token_expires: float = 0.0


def _config() -> dict:
    try:
        p = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _credentials() -> tuple[str, str]:
    cfg = _config()
    return (str(cfg.get("spotify_client_id", "")).strip(),
            str(cfg.get("spotify_client_secret", "")).strip())


def has_credentials() -> bool:
    cid, secret = _credentials()
    return bool(cid and secret)


def _access_token() -> str:
    """Client-credentials token, cached until shortly before it expires."""
    global _token, _token_expires
    if _token and time.time() < _token_expires - 30:
        return _token
    cid, secret = _credentials()
    if not (cid and secret):
        return ""
    r = requests.post(_TOKEN_URL, data={"grant_type": "client_credentials"},
                      auth=(cid, secret), timeout=20)
    if r.status_code != 200:
        raise RuntimeError("Spotify auth failed: HTTP %d %s"
                           % (r.status_code, r.text[:120]))
    data = r.json()
    _token = data.get("access_token", "")
    _token_expires = time.time() + int(data.get("expires_in", 3600))
    return _token


def _search_track(query: str) -> dict | None:
    token = _access_token()
    if not token:
        return None
    r = requests.get(_SEARCH_URL,
                     params={"q": query, "type": "track", "limit": 1},
                     headers={"Authorization": "Bearer " + token}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError("Spotify search failed: HTTP %d %s"
                           % (r.status_code, r.text[:120]))
    items = ((r.json() or {}).get("tracks") or {}).get("items") or []
    if not items:
        return None
    t = items[0]
    return {
        "id":     t.get("id", ""),
        "name":   t.get("name", ""),
        "artist": ", ".join(a.get("name", "") for a in t.get("artists", [])),
        "uri":    t.get("uri", ""),
    }


def _open_uri(uri: str) -> bool:
    try:
        if sys.platform == "win32":
            os.startfile(uri)                                   # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", uri])
        else:
            subprocess.Popen(["xdg-open", uri])
        return True
    except Exception as exc:
        print("[Music] could not open " + uri[:40] + ": " + str(exc)[:80])
        return False


def _tap(key: str) -> bool:
    if not _KEYS:
        return False
    try:
        pyautogui.press(key)
        return True
    except Exception:
        return False


# -- actions ---------------------------------------------------------------
def play(query: str = "") -> str:
    """Play a named track, or resume whatever is loaded when given nothing."""
    if not query:
        return ("Resumed playback." if _tap("playpause")
                else "Could not reach the media keys.")

    if has_credentials():
        try:
            track = _search_track(query)
        except Exception as exc:
            track = None
            print("[Music] search failed: " + str(exc)[:120])
        if track and track["id"]:
            if _open_uri("spotify:track:" + track["id"]):
                return f"Playing {track['name']} by {track['artist']} on Spotify."
        if track is None:
            return f"Spotify has no track matching '{query}'."

    # No credentials: open the search results and say so plainly. Claiming the
    # song is playing here would be a lie - Spotify has only been asked to
    # show results, and nothing has started.
    if _open_uri("spotify:search:" + quote(query)):
        return (f"Opened Spotify search for '{query}'. I cannot start an exact "
                f"track without Spotify API credentials - add "
                f"spotify_client_id and spotify_client_secret to "
                f"config/api_keys.json and I will play songs directly. "
                f"Say 'play' to start the highlighted result.")
    return "Could not reach Spotify. Is it installed?"


def pause() -> str:
    return "Paused." if _tap("playpause") else "Could not reach the media keys."


def next_track() -> str:
    return "Skipped." if _tap("nexttrack") else "Could not reach the media keys."


def previous_track() -> str:
    return "Previous track." if _tap("prevtrack") else "Could not reach the media keys."


def open_spotify() -> str:
    return "Opened Spotify." if _open_uri("spotify:") else "Could not open Spotify."


def music_control(parameters: dict = None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "play").lower().strip()
    query  = (params.get("query") or "").strip()

    if player:
        player.write_log(f"[music] {action} {query}"[:60])

    try:
        if action in ("play", "start", "resume"):
            return play(query)
        if action in ("pause", "stop"):
            return pause()
        if action in ("next", "skip"):
            return next_track()
        if action in ("previous", "prev", "back"):
            return previous_track()
        if action in ("open",):
            return open_spotify()
        return f"Unknown music action '{action}'. Use play, pause, next, previous."
    except Exception as exc:
        return f"Music error ({action}): {exc}"
