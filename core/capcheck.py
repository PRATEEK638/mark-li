"""
Capability audit: what can this assistant actually do on THIS machine?

The tool surface advertised to the voice model is 22 tools wide, but a tool
only works if its dependency, its OS integration and its host all happen to be
present here. A missing `pycaw` or an absent Steam install does not stop AURA
from confidently telling the model it can adjust the volume - it just fails at
the moment the user asks. This walks the surface and reports the truth.

    python -m core.capcheck            # safe probes only
    python -m core.capcheck --verbose  # include the reason for each result

Read-only by design. Nothing here launches an app, moves a file, changes a
setting or spends model quota; capabilities whose only real test would do one
of those are reported as REQUIRES-ACTION rather than silently passed.
"""

from __future__ import annotations

import importlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK      = "OK     "
BROKEN  = "BROKEN "
MISSING = "MISSING"
MANUAL  = "MANUAL "
NOTE    = "NOTE   "

VERBOSE = "--verbose" in sys.argv or "-v" in sys.argv
_results: list[tuple[str, str, str, str]] = []


def report(group: str, name: str, status: str, detail: str = "") -> None:
    _results.append((group, name, status, detail))


def _mod(name: str):
    try:
        return importlib.import_module(name), ""
    except Exception as exc:
        return None, type(exc).__name__ + ": " + str(exc)[:90]


def check_python_deps() -> None:
    """The third-party packages each capability actually needs."""
    deps = {
        "pyautogui":     "mouse / keyboard control",
        "pygetwindow":   "window management",
        "pyperclip":     "clipboard",
        "mss":           "screen capture",
        "cv2":           "webcam + vision",
        "numpy":         "audio + image buffers",
        "sounddevice":   "microphone / speaker",
        "psutil":        "system telemetry",
        "PIL":           "image handling",
        "requests":      "HTTP",
        "bs4":           "web scraping",
        "ddgs":          "DuckDuckGo search fallback",
        "playwright":    "browser automation",
        "send2trash":    "safe file deletion",
        "pptx":          "PowerPoint generation",
        "fastapi":       "phone dashboard",
        "qrcode":        "dashboard pairing QR",
        "youtube_transcript_api": "YouTube summaries",
        "google.genai":  "Gemini + Live API",
    }
    win = {
        "comtypes":  "Windows COM",
        "pycaw":     "volume control",
        "win32com":  "shortcuts / COM automation",
        "pywinauto": "Steam / Epic UI automation",
        "win10toast": "toast notifications",
    }
    for name, why in deps.items():
        m, err = _mod(name)
        report("dependencies", name, OK if m else MISSING, why if m else err)
    if sys.platform == "win32":
        for name, why in win.items():
            m, err = _mod(name)
            report("dependencies (windows)", name,
                   OK if m else MISSING, why if m else err)


def check_action_modules() -> None:
    """Every action module imports and exposes its entry point."""
    # Entry-point names taken from main.py's own imports - these modules each
    # export one function named after themselves, not a uniform run().
    entries = {
        "open_app":            ("actions.open_app", "open_app"),
        "web_search":          ("actions.web_search", "web_search"),
        "system_status":       ("actions.system_monitor", "get_system_status"),
        "weather_report":      ("actions.weather_report", "weather_action"),
        "send_message":        ("actions.send_message", "send_message"),
        "reminder":            ("actions.reminder", "reminder"),
        "youtube_video":       ("actions.youtube_video", "youtube_video"),
        "screen_process":      ("actions.screen_processor", "_capture_screen"),
        "close_camera":        ("actions.screen_processor", "_capture_camera"),
        "computer_settings":   ("actions.computer_settings", "computer_settings"),
        "browser_control":     ("actions.browser_control", "browser_control"),
        "file_controller":     ("actions.file_controller", "file_controller"),
        "desktop_control":     ("actions.desktop", "desktop_control"),
        "code_helper":         ("actions.code_helper", "code_helper"),
        "dev_agent":           ("actions.dev_agent", "dev_agent"),
        "computer_control":    ("actions.computer_control", "computer_control"),
        "game_updater":        ("actions.game_updater", "game_updater"),
        "flight_finder":       ("actions.flight_finder", "flight_finder"),
        "file_processor":      ("actions.file_processor", "file_processor"),
        "background_monitor":  ("actions.background_monitor", None),
        "proactive":           ("actions.proactive", "ProactiveEngine"),
    }
    for tool, (mod_name, entry) in entries.items():
        m, err = _mod(mod_name)
        if not m:
            report("tools", tool, BROKEN, err)
            continue
        if entry and not hasattr(m, entry):
            public = [a for a in dir(m) if not a.startswith("_") and callable(getattr(m, a))]
            report("tools", tool, BROKEN,
                   "no " + entry + "(); has " + ", ".join(public[:4]))
            continue
        report("tools", tool, OK, mod_name)


def check_system_telemetry() -> None:
    """Live hardware read - the one probe that is genuinely free to run."""
    m, err = _mod("actions.system_monitor")
    if not m:
        report("machine", "system telemetry", BROKEN, err)
        return
    fn = next((getattr(m, n) for n in ("get_system_status", "get_status", "status")
               if hasattr(m, n)), None)
    if fn is None:
        report("machine", "system telemetry", BROKEN, "no status function")
        return
    try:
        out = fn()
        report("machine", "system telemetry", OK, str(out)[:120])
    except Exception as exc:
        report("machine", "system telemetry", BROKEN, str(exc)[:110])


def check_audio_devices() -> None:
    """A microphone and an output device must both exist for voice to work."""
    try:
        import sounddevice as sd
        devs = sd.query_devices()
        ins  = [d for d in devs if d.get("max_input_channels", 0) > 0]
        outs = [d for d in devs if d.get("max_output_channels", 0) > 0]
        try:
            default_in = sd.query_devices(kind="input")
            in_name    = default_in.get("name", "?")
        except Exception:
            in_name = "none"
        report("machine", "microphone",
               OK if ins else BROKEN,
               ("%d input device(s), default: %s" % (len(ins), in_name))
               if ins else "no input device - voice cannot work")
        report("machine", "speaker",
               OK if outs else BROKEN, "%d output device(s)" % len(outs))
    except Exception as exc:
        report("machine", "audio", BROKEN, str(exc)[:110])


def check_camera() -> None:
    try:
        import cv2
    except Exception as exc:
        report("machine", "webcam", MISSING, str(exc)[:90]); return
    # Opening the camera turns the LED on; that is intrusive but momentary and
    # it is the only way to know whether vision will actually work.
    try:
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else 0
        cap = cv2.VideoCapture(0, backend)
        ok  = cap.isOpened()
        if ok:
            got, _ = cap.read()
            ok = got
        cap.release()
        report("machine", "webcam", OK if ok else MISSING,
               "index 0 readable" if ok else "no camera at index 0")
    except Exception as exc:
        report("machine", "webcam", BROKEN, str(exc)[:110])


def check_screen() -> None:
    try:
        import mss
        with mss.mss() as s:
            n = len(s.monitors) - 1
        report("machine", "screen capture", OK, "%d monitor(s)" % n)
    except Exception as exc:
        report("machine", "screen capture", BROKEN, str(exc)[:110])


def check_browsers() -> None:
    """Which browsers exist, and whether Playwright has a driver installed."""
    candidates = {
        "chrome": [r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                   r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"],
        "edge":   [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                   r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"],
        "brave":  [r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"],
        "firefox":[r"C:\Program Files\Mozilla Firefox\firefox.exe"],
    }
    found = []
    for name, paths in candidates.items():
        hit = next((p for p in paths if Path(p).exists()), None) or shutil.which(name)
        if hit:
            found.append(name)
    report("machine", "browsers installed",
           OK if found else MISSING, ", ".join(found) or "none detected")

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
        ready = bool(path and Path(path).exists())
        report("machine", "playwright driver", OK if ready else MISSING,
               "chromium installed" if ready
               else "run: python -m playwright install chromium")
    except Exception as exc:
        report("machine", "playwright driver", BROKEN, str(exc)[:110])


def check_windows_integrations() -> None:
    if sys.platform != "win32":
        return
    try:
        from actions.computer_settings import get_volume
        level = get_volume()
        report("machine", "volume control",
               OK if level >= 0 else BROKEN,
               ("currently %d%%" % level) if level >= 0
               else "pycaw endpoint unavailable")
    except Exception as exc:
        report("machine", "volume control", BROKEN, str(exc)[:110])

    try:
        import subprocess
        r = subprocess.run(["schtasks", "/query", "/fo", "csv"],
                           capture_output=True, text=True, timeout=25)
        report("machine", "task scheduler", OK if r.returncode == 0 else BROKEN,
               "reminders can be scheduled" if r.returncode == 0
               else "schtasks returned " + str(r.returncode))
    except Exception as exc:
        report("machine", "task scheduler", BROKEN, str(exc)[:110])

    for name, exe in (("steam", "steam.exe"), ("epic", "EpicGamesLauncher.exe")):
        roots = [Path(r"C:\Program Files (x86)\Steam"), Path(r"C:\Program Files\Steam"),
                 Path(r"C:\Program Files (x86)\Epic Games"), Path(r"C:\Program Files\Epic Games")]
        hit = any((r / exe).exists() or r.exists() and any(r.rglob(exe)) for r in roots if r.exists())
        report("machine", name + " (game_updater)",
               OK if hit else MISSING,
               "installed" if hit else "not installed - game_updater will fail")


def check_routes() -> None:
    """Model routes, without spending a token."""
    try:
        from core import keyring
        for p, info in keyring.status().items():
            if info["keys"]:
                report("model routes", p, OK, "%d key(s)" % info["keys"])
            else:
                report("model routes", p,
                       MANUAL if p == "gemini_voice" else MISSING,
                       "no key")
    except Exception as exc:
        report("model routes", "keyring", BROKEN, str(exc)[:110])


def check_manual() -> None:
    """Capabilities whose only honest test would change something."""
    for name, why in (
        ("open_app",       "would launch an application"),
        ("send_message",   "would open WhatsApp/Telegram and send"),
        ("reminder",       "would register a scheduled OS task"),
        ("file_controller","would create, move or delete files"),
        ("computer_control","would move the mouse and press keys"),
        ("game_updater",   "would launch Steam/Epic and start a download"),
        ("dev_agent",      "would generate and run code"),
    ):
        report("requires action to verify", name, MANUAL, why)


def main() -> int:
    print("=" * 72)
    print("AURA capability audit - " + sys.platform)
    print("=" * 72)

    check_python_deps()
    check_action_modules()
    check_system_telemetry()
    check_audio_devices()
    check_screen()
    check_camera()
    check_browsers()
    check_windows_integrations()
    check_routes()
    check_manual()

    groups: dict[str, list] = {}
    for g, n, s, d in _results:
        groups.setdefault(g, []).append((n, s, d))

    for g, rows in groups.items():
        print("\n--- " + g + " " + "-" * max(2, 66 - len(g)))
        for n, s, d in rows:
            line = "[" + s + "] " + n.ljust(26)
            if d and (VERBOSE or s != OK):
                line += d
            print(line)

    counts: dict[str, int] = {}
    for _, _, s, _ in _results:
        counts[s] = counts.get(s, 0) + 1
    print("\n" + "=" * 72)
    print("  ".join(k.strip() + ": " + str(v) for k, v in sorted(counts.items())))

    blocking = [(n, d) for g, n, s, d in _results
                if s == BROKEN and g in ("tools", "machine", "model routes")]
    if blocking:
        print("\nNeeds attention:")
        for n, d in blocking:
            print("  - " + n + ": " + d)
    else:
        print("\nNothing broken. MISSING entries are optional or not installed here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
