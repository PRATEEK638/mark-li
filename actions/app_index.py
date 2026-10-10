"""
An index of the applications actually installed on this machine.

The launcher used to work from a hardcoded table of ~50 app names. Anything
outside it fell through to simulating keystrokes at the Start menu - pressing
Win, typing the name and pressing Enter - which steals focus, races a UI that
may not have opened yet, and types the app name into whatever window happens to
be focused when it loses that race.

This asks the OS what is installed instead, from the same sources the Start
menu itself uses:

  * Start Menu shortcuts (.lnk) - per-user and system-wide
  * Store / UWP apps, via `Get-StartApps`, launched through shell:AppsFolder
  * the App Paths registry key, which is how `start <name>` resolves
  * executables on PATH

The result is cached on disk and rebuilt when it gets stale, because walking
the Start menu takes a moment and the set of installed apps rarely changes.
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

_OS       = platform.system()
_CACHE    = Path(__file__).resolve().parent.parent / "config" / "app_index.json"
_MAX_AGE  = 24 * 3600          # rebuild daily
_NO_WINDOW = {"creationflags": subprocess.CREATE_NO_WINDOW} if _OS == "Windows" else {}

# Entries whose names collide with real apps but which are uninstallers,
# helpers or documentation - never what someone means by "open X".
_NOISE = re.compile(
    r"\b(uninstall|readme|documentation|help|license|changelog|release notes|"
    r"repair|modify|remove|website|homepage|manual|support|feedback|"
    r"safe mode|troubleshoot|command prompt for|debug)\b", re.I)


class App:
    __slots__ = ("name", "target", "kind", "source")

    def __init__(self, name: str, target: str, kind: str, source: str = ""):
        self.name   = name
        self.target = target
        self.kind   = kind        # "lnk" | "uwp" | "exe" | "uri"
        self.source = source

    def as_dict(self) -> dict:
        return {"name": self.name, "target": self.target,
                "kind": self.kind, "source": self.source}

    @staticmethod
    def from_dict(d: dict) -> "App":
        return App(d.get("name", ""), d.get("target", ""),
                   d.get("kind", "exe"), d.get("source", ""))


# -- discovery -------------------------------------------------------------
def _start_menu_dirs() -> list[Path]:
    dirs = []
    for env, tail in (
        ("ProgramData", r"Microsoft\Windows\Start Menu\Programs"),
        ("APPDATA",     r"Microsoft\Windows\Start Menu\Programs"),
    ):
        base = os.environ.get(env)
        if base:
            p = Path(base) / tail
            if p.is_dir():
                dirs.append(p)
    return dirs


def _scan_start_menu() -> list[App]:
    apps: list[App] = []
    for d in _start_menu_dirs():
        try:
            for lnk in d.rglob("*.lnk"):
                stem = lnk.stem.strip()
                if not stem or _NOISE.search(stem):
                    continue
                apps.append(App(stem, str(lnk), "lnk", "start-menu"))
        except Exception:
            continue
    return apps


def _scan_uwp() -> list[App]:
    """Store apps. `Get-StartApps` is the only reliable enumeration."""
    if _OS != "Windows":
        return []
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=60, **_NO_WINDOW)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            data = [data]
        apps = []
        for entry in data:
            name   = (entry.get("Name") or "").strip()
            app_id = (entry.get("AppID") or "").strip()
            if not name or not app_id or _NOISE.search(name):
                continue
            # A plain path here is a classic app Get-StartApps also reports;
            # only the ones with a package-style AppID need shell:AppsFolder.
            kind = "exe" if app_id.lower().endswith(".exe") and "\\" in app_id else "uwp"
            apps.append(App(name, app_id, kind, "start-apps"))
        return apps
    except Exception:
        return []


def _scan_app_paths() -> list[App]:
    """HKLM/HKCU App Paths - what `start <name>` looks at."""
    if _OS != "Windows":
        return []
    try:
        import winreg
    except ImportError:
        return []
    apps: list[App] = []
    key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key_path) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        sub = winreg.EnumKey(key, i)
                        with winreg.OpenKey(key, sub) as sk:
                            target = winreg.QueryValueEx(sk, "")[0]
                        if not target:
                            continue
                        target = os.path.expandvars(target).strip('"')
                        if Path(target).exists():
                            apps.append(App(Path(sub).stem, target, "exe", "app-paths"))
                    except Exception:
                        continue
        except Exception:
            continue
    return apps


def _scan_path_executables() -> list[App]:
    apps: list[App] = []
    seen: set[str] = set()
    exts = [".exe", ".bat", ".cmd"] if _OS == "Windows" else [""]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d:
            continue
        try:
            entries = list(Path(d).iterdir())
        except Exception:
            continue
        for f in entries:
            try:
                if not f.is_file():
                    continue
                if _OS == "Windows" and f.suffix.lower() not in exts:
                    continue
                if _OS != "Windows" and not os.access(f, os.X_OK):
                    continue
                stem = f.stem.lower()
                if stem in seen:
                    continue
                seen.add(stem)
                apps.append(App(f.stem, str(f), "exe", "path"))
            except Exception:
                continue
    return apps


def _scan_macos() -> list[App]:
    apps = []
    for base in ("/Applications", "/System/Applications",
                 str(Path.home() / "Applications")):
        p = Path(base)
        if not p.is_dir():
            continue
        try:
            for app in p.rglob("*.app"):
                apps.append(App(app.stem, str(app), "exe", "applications"))
        except Exception:
            continue
    return apps


def _scan_linux() -> list[App]:
    apps = []
    for base in ("/usr/share/applications",
                 "/var/lib/flatpak/exports/share/applications",
                 str(Path.home() / ".local/share/applications")):
        p = Path(base)
        if not p.is_dir():
            continue
        try:
            for desktop in p.glob("*.desktop"):
                name = desktop.stem
                try:
                    for line in desktop.read_text(errors="ignore").splitlines():
                        if line.startswith("Name="):
                            name = line.split("=", 1)[1].strip()
                            break
                except Exception:
                    pass
                apps.append(App(name, desktop.stem, "desktop", "applications"))
        except Exception:
            continue
    return apps


def build_index() -> list[App]:
    """Walk every source. Later sources never displace an earlier name."""
    if _OS == "Windows":
        groups = [_scan_start_menu(), _scan_uwp(), _scan_app_paths(),
                  _scan_path_executables()]
    elif _OS == "Darwin":
        groups = [_scan_macos(), _scan_path_executables()]
    else:
        groups = [_scan_linux(), _scan_path_executables()]

    merged: dict[str, App] = {}
    for group in groups:
        for app in group:
            key = app.name.lower()
            if key and key not in merged:
                merged[key] = app
    return list(merged.values())


# -- cache -----------------------------------------------------------------
def load_index(force: bool = False) -> list[App]:
    if not force and _CACHE.exists():
        try:
            blob = json.loads(_CACHE.read_text(encoding="utf-8"))
            if time.time() - blob.get("built_at", 0) < _MAX_AGE:
                apps = [App.from_dict(d) for d in blob.get("apps", [])]
                if apps:
                    return apps
        except Exception:
            pass

    apps = build_index()
    try:
        _CACHE.parent.mkdir(parents=True, exist_ok=True)
        _CACHE.write_text(json.dumps(
            {"built_at": time.time(), "platform": _OS,
             "apps": [a.as_dict() for a in apps]}, indent=1), encoding="utf-8")
    except Exception:
        pass
    return apps


def refresh() -> int:
    return len(load_index(force=True))


# -- matching --------------------------------------------------------------
_STOP = re.compile(r"\b(app|application|program|please|open|launch|start|run)\b", re.I)

# Words people attach to an app name that carry no identity of their own.
# Stripped from the QUERY only, never from the indexed name: "brave browser"
# should find Brave, but "Safe Exam Browser" must keep its own word "browser"
# so it is not mistaken for every request containing it.
_FILLER = {
    "the", "my", "a", "an", "some", "that", "this",
    "app", "apps", "application", "program", "software", "tool",
    "open", "launch", "start", "run", "play", "please", "go", "to",
    "browser", "editor", "document", "window", "desktop",
    "music", "player", "client", "studio" if False else "",
}
_FILLER.discard("")

# Tried as an extra query with the vendor dropped. "microsoft word" shares its
# more distinctive half with Word and its less distinctive half with a dozen
# Microsoft products - matching on the vendor picked Microsoft Store.
_VENDORS = {"microsoft", "ms", "google", "adobe", "apple", "windows"}


def _clean(text: str) -> str:
    text = _STOP.sub(" ", text.lower())
    text = re.sub(r"[^a-z0-9+#\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _content_tokens(query: str) -> list[str]:
    """The words that actually identify an app, filler removed."""
    tokens = [t for t in _clean(query).split() if t]
    kept   = [t for t in tokens if t not in _FILLER]
    return kept or tokens          # all filler? then it is all we have


def _shares_a_word(qt: list[str], nt: set[str]) -> bool:
    """Does at least one identifying word actually appear in the name?

    This is the guard that stops confident nonsense. Without it, plain string
    similarity ranked "microsoft word" -> Microsoft **Store** at 0.83, above
    real Word, and "brave browser" -> Safe Exam **Browser**. Requiring a shared
    identifying word means a near-miss on spelling is still tolerated, while a
    match built entirely out of the generic half of the phrase is rejected.
    """
    for q in qt:
        for n in nt:
            if q == n:
                return True
            if len(q) >= 4 and (n.startswith(q) or q.startswith(n)) and len(n) >= 4:
                return True
    return False


def _score(query: str, name: str) -> float:
    """How well an index entry matches what was asked for."""
    n = _clean(name)
    if not n:
        return 0.0

    qt = _content_tokens(query)
    if not qt:
        return 0.0
    q = " ".join(qt)
    nt = set(n.split())

    if q == n:
        return 1.0
    # A whole-word prefix is a strong signal: "word" -> "Word", and it stops
    # "code" matching "Code Cleaner" ahead of "Visual Studio Code".
    if n.startswith(q + " "):
        return 0.95
    if set(qt) <= nt:
        # every identifying word present; prefer the shortest such name
        return 0.92 - min(0.2, 0.02 * (len(nt) - len(qt)))

    if not _shares_a_word(qt, nt):
        return 0.0

    ratio = difflib.SequenceMatcher(None, q, n).ratio()
    if q in n:
        ratio = max(ratio, 0.80)
    # Nothing structural matched - only the spelling looks alike. That is worth
    # acting on for a typo or a mis-transcription, not for two different
    # products that happen to share a prefix ("photoshop" / "Photos" = 0.80).
    return ratio if ratio >= 0.85 else 0.0


# Spoken forms that do not resemble the installed name closely enough for
# fuzzy matching to bridge on its own.
_SPOKEN_ALIASES = {
    "vs code": "visual studio code", "vscode": "visual studio code",
    "code": "visual studio code",
    "vs": "visual studio", "cmd": "command prompt",
    "explorer": "file explorer", "files": "file explorer",
    "task manager": "task manager", "control panel": "control panel",
    "ppt": "powerpoint", "powerpnt": "powerpoint",
    "chrome": "google chrome", "docs": "word", "sheets": "excel",
    "vlc": "vlc media player", "photoshop": "adobe photoshop",
    "browser": "google chrome", "notes": "notepad",
    "calc": "calculator", "sound": "settings", "wifi": "settings",
    "my computer": "file explorer", "this pc": "file explorer",
    "my pc": "file explorer", "folder": "file explorer",
    "sound settings": "settings", "wifi settings": "settings",
    "display settings": "settings", "bluetooth": "settings",
    "terminal": "terminal", "shell": "terminal",
}

# How good a match has to be, per source. PATH entries are bare executable
# stems rather than names anyone would speak, so a loose fuzzy hit there is far
# more likely to be a coincidence than an answer - "discord" once resolved to
# "diskraid", a disk-partitioning tool, at 0.67. Names that came from the Start
# menu or the Store are the ones a person actually reads off their screen, so
# they can be matched loosely.
_MIN_SCORE = {
    "start-menu": 0.62, "start-apps": 0.62, "applications": 0.62,
    "app-paths": 0.72, "path": 0.90,
}


def resolve(query: str, limit: int = 5) -> list[tuple[App, float]]:
    """Best index matches for a spoken app name, strongest first."""
    apps    = load_index()
    cleaned = _clean(query)
    content = " ".join(_content_tokens(query))
    queries = [query]

    # Alias lookup on both the raw phrase and the filler-stripped one.
    for key in (cleaned, content):
        alias = _SPOKEN_ALIASES.get(key)
        if alias and alias not in queries:
            queries.insert(0, alias)

    # And once more with the vendor name dropped.
    bare = " ".join(t for t in _content_tokens(query) if t not in _VENDORS)
    if bare and bare != content:
        queries.append(bare)
        alias = _SPOKEN_ALIASES.get(bare)
        if alias and alias not in queries:
            queries.insert(0, alias)

    # Prefer Start-menu and Store entries over a bare PATH executable: both
    # carry the name a person would actually say. This has to weigh on the
    # score rather than only break ties, or a coincidental prefix hit on PATH
    # outranks the real app - "edge" scored 0.95 against the `edge-tts` binary
    # and only 0.88 against Microsoft Edge.
    weight = {"start-menu": 1.0, "start-apps": 1.0, "applications": 1.0,
              "app-paths": 0.97, "path": 0.90}
    rank   = {"start-menu": 0, "start-apps": 0, "applications": 0,
              "app-paths": 1, "path": 2}

    scored: list[tuple[App, float]] = []
    for a in apps:
        s = max(_score(q, a.name) for q in queries)
        if s >= _MIN_SCORE.get(a.source, 0.75):
            scored.append((a, s * weight.get(a.source, 0.9)))

    scored.sort(key=lambda t: (-round(t[1], 3), rank.get(t[0].source, 3),
                               len(t[0].name)))
    return scored[:limit]


def best(query: str) -> App | None:
    hits = resolve(query, limit=1)
    return hits[0][0] if hits else None


# -- launching -------------------------------------------------------------
def launch(app: App) -> bool:
    """Start an indexed app. Returns False rather than raising."""
    try:
        if app.kind == "uwp":
            os.startfile("shell:AppsFolder\\" + app.target)   # noqa: S606
            return True
        if app.kind == "lnk":
            os.startfile(app.target)                           # noqa: S606
            return True
        if app.kind == "desktop":
            subprocess.Popen(["gtk-launch", app.target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        if _OS == "Darwin":
            subprocess.Popen(["open", "-a", app.target])
            return True
        target = app.target
        if _OS == "Windows" and Path(target).exists():
            os.startfile(target)                               # noqa: S606
            return True
        exe = shutil.which(target) or target
        subprocess.Popen([exe], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception as exc:
        print("[AppIndex] launch failed for " + app.name + ": " + str(exc)[:100])
        return False


if __name__ == "__main__":
    if "--refresh" in sys.argv:
        print("indexed " + str(refresh()) + " applications")
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if args:
        q = " ".join(args)
        print("matches for " + repr(q) + ":")
        for app, score in resolve(q):
            print("  %.2f  %-42s %-11s %s"
                  % (score, app.name[:42], app.source, app.target[:60]))
    else:
        idx = load_index()
        print(str(len(idx)) + " applications indexed")
        for a in idx[:25]:
            print("  %-44s %-11s %s" % (a.name[:44], a.source, a.kind))
