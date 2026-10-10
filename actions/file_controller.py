import difflib
import os
import shutil
import platform
from pathlib import Path
from datetime import datetime

try:
    import send2trash
    _SEND2TRASH = True
except ImportError:
    _SEND2TRASH = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

def _config() -> dict:
    try:
        import json
        cfg_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        return json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _safe_roots() -> list[Path]:
    """Where file operations are permitted.

    Home only, by default. Extra roots can be opted into with
    ``"file_safe_roots": ["D:/work"]`` in config/api_keys.json - useful when
    your projects live on a second drive, which is otherwise entirely
    unreachable. Nothing outside these roots can be touched.
    """
    roots = [Path.home()]
    for extra in _config().get("file_safe_roots") or []:
        try:
            p = Path(str(extra)).expanduser()
            if p.exists():
                roots.append(p)
        except Exception:
            continue
    return roots


# Denied even when a safe root would otherwise cover them. A voice assistant
# acting on a misheard word must not be able to reach into the OS, and the
# assistant's own directory is included so it cannot rewrite itself mid-run.
def _denied_roots() -> list[Path]:
    denied = []
    for env in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        value = os.environ.get(env)
        if value:
            denied.append(Path(value))
    denied.append(Path(__file__).resolve().parent.parent)   # the AURA install
    return denied


def _is_safe_path(target: Path) -> bool:
    """True when `target` sits inside a safe root and outside every denied one."""
    try:
        resolved = target.resolve()
    except Exception:
        return False

    for bad in _denied_roots():
        try:
            bad_r = bad.resolve()
            if resolved == bad_r or resolved.is_relative_to(bad_r):
                return False
        except Exception:
            continue

    for root in _safe_roots():
        try:
            root_r = root.resolve()
            if resolved == root_r or resolved.is_relative_to(root_r):
                return True
        except Exception:
            continue
    return False

def _get_desktop() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DESKTOP_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Desktop"

def _get_downloads() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOWNLOAD_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Downloads"

def _get_documents() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_DOCUMENTS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Documents"

def _get_pictures() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_PICTURES_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Pictures"

def _get_music() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_MUSIC_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Music"

def _get_videos() -> Path:
    if _OS == "Linux":
        xdg = os.environ.get("XDG_VIDEOS_DIR", "")
        if xdg and Path(xdg).exists():
            return Path(xdg)
    return Path.home() / "Videos"


def _resolve_path(raw: str) -> Path:
    shortcuts: dict[str, Path] = {
        "desktop":   _get_desktop(),
        "downloads": _get_downloads(),
        "documents": _get_documents(),
        "pictures":  _get_pictures(),
        "music":     _get_music(),
        "videos":    _get_videos(),
        "home":      Path.home(),
    }
    raw   = raw.strip().strip('"').strip("'")
    if not raw:
        return Path.home()
    lower = raw.lower()
    if lower in shortcuts:
        return shortcuts[lower]

    # Also accept spoken variants: "my desktop", "the downloads folder".
    stripped = lower
    for prefix in ("my ", "the "):
        if stripped.startswith(prefix):
            stripped = stripped[len(prefix):]
    stripped = stripped.removesuffix(" folder").removesuffix(" directory").strip()
    if stripped in shortcuts:
        return shortcuts[stripped]

    # "desktop/notes/a.md" and "desktop\notes\a.md" - a shortcut plus a subpath.
    normalised = raw.replace("\\", "/")
    head, sep, rest = normalised.partition("/")
    if sep and head.lower() in shortcuts:
        rest = rest.strip("/")
        return shortcuts[head.lower()] / rest if rest else shortcuts[head.lower()]

    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return candidate

    # A bare relative name. It must NOT resolve against the process working
    # directory: that is the AURA install folder, so "notes/todo.txt" used to
    # be created inside the assistant's own source tree instead of anywhere the
    # user could find it - silently, with a success message. Resolve against
    # the user's own folders instead, preferring one that already exists.
    for base in (Path.home(), _get_desktop(), _get_documents(), _get_downloads()):
        if (base / normalised).exists():
            return base / normalised
    return Path.home() / normalised

def _target(path: str, name: str = "") -> Path:
    """The file or folder an operation acts on, from `path` plus `name`.

    The two arguments overlap: a voice model may put the whole path in `path`,
    or a folder in `path` and the filename in `name`, or - commonly - the full
    file path in `path` AND repeat the filename in `name`. Joining blindly
    turned that last case into `notes.txt/notes.txt`, silently creating a
    *directory* named notes.txt and reporting "File created: notes.txt".
    """
    base = _resolve_path(path)
    if not name:
        return base

    name = name.strip().strip('"').strip("'").replace("\\", "/").lstrip("/")
    if not name:
        return base

    # `path` already names this file - don't nest it inside itself.
    if not base.is_dir() and base.name.lower() in (name.lower(),
                                                   Path(name).name.lower()):
        return base

    # Nothing can live inside a regular file.
    if base.is_file():
        return base

    return base / name


def _norm_name(text: str) -> str:
    """Loose form of a filename for comparison: lowercase, letters+digits only."""
    return "".join(c for c in str(text).lower() if c.isalnum())


def _resolve_existing(path: str, name: str = "") -> Path:
    """Like `_target`, but finds the file the user probably meant.

    Spoken filenames almost never match what is on disk. The paper's File
    Search criterion is explicitly "by name, PARTIAL name, or content
    description", and an exact-string lookup fails all three: asking to delete
    the "ultra viewer icon" returned "Not found" while `UltraViewer.lnk` sat
    right there. When the literal target is missing, the containing folder is
    searched for the closest name, ignoring case, spacing, punctuation and
    extension.
    """
    target = _target(path, name)
    if target.exists() or not name:
        return target

    folder = target.parent
    if not folder.is_dir():
        return target

    wanted = _norm_name(name)
    # Drop words that describe the thing rather than name it.
    for filler in ("icon", "shortcut", "file", "folder", "the", "my"):
        wanted = wanted.replace(filler, "")
    if not wanted:
        return target

    best, best_score = None, 0.0
    for item in folder.iterdir():
        for candidate in (item.name, item.stem):
            cand = _norm_name(candidate)
            if not cand:
                continue
            if cand == wanted:
                return item
            score = difflib.SequenceMatcher(None, wanted, cand).ratio()
            if wanted in cand or cand in wanted:
                score = max(score, 0.90)
            if score > best_score:
                best, best_score = item, score

    # Only accept a clearly close match; a vague one would act on the wrong file.
    return best if (best is not None and best_score >= 0.72) else target


def _destination(destination: str, src: Path) -> Path:
    """Where a move or copy should land.

    An existing directory means "put it inside". A destination that does not
    exist yet is the ambiguous case: `move a.txt to archive` should create the
    folder `archive/` and put the file in it, but the old code treated any
    missing destination as a literal target filename - so `archive` became a
    *file* containing a.txt's bytes, and the report still said "Moved". A
    trailing slash or the absence of a file extension means folder.
    """
    dst = _resolve_path(destination)

    if dst.exists():
        return dst / src.name if dst.is_dir() else dst

    raw = destination.strip().strip('"').strip("'").replace("\\", "/")
    looks_like_folder = raw.endswith("/") or not Path(raw).suffix
    if looks_like_folder:
        dst.mkdir(parents=True, exist_ok=True)
        return dst / src.name
    return dst


def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def _safe_trash(target: Path) -> str:

    if not _SEND2TRASH:
        return (
            "send2trash is not installed. "
            "Run: pip install send2trash — "
            "Permanent deletion is disabled for safety."
        )
    send2trash.send2trash(str(target))
    return f"Moved to Trash: {target.name}"


def list_files(path: str = "desktop", show_hidden: bool = False) -> str:
    try:
        target = _resolve_path(path)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Path not found: {target}"
        if not target.is_dir():
            return f"Not a directory: {target}"

        items = []
        for item in sorted(target.iterdir()):
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                items.append(f"📁 {item.name}/")
            else:
                size = _format_size(item.stat().st_size)
                items.append(f"📄 {item.name} ({size})")

        if not items:
            return f"Directory is empty: {target.name}/"

        return f"Contents of {target.name}/ ({len(items)} items):\n" + "\n".join(items)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Error listing files: {e}"


def create_file(path: str, name: str = "", content: str = "") -> str:
    try:
        target = _target(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"File created: {target.name}"
    except Exception as e:
        return f"Could not create file: {e}"


def create_folder(path: str, name: str = "") -> str:
    try:
        target = _target(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.mkdir(parents=True, exist_ok=True)
        return f"Folder created: {target.name}"
    except Exception as e:
        return f"Could not create folder: {e}"


def delete_file(path: str, name: str = "") -> str:
    try:
        target = _resolve_existing(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        # Güvenli dizin kontrolü — kritik kullanıcı klasörlerini koru
        protected = {
            _get_desktop(), _get_downloads(), _get_documents(),
            _get_pictures(), _get_music(), _get_videos(), Path.home()
        }
        if target.resolve() in {p.resolve() for p in protected}:
            return f"Protected directory, cannot delete: {target.name}"

        return _safe_trash(target)

    except PermissionError:
        return f"Permission denied: {path}"
    except Exception as e:
        return f"Could not delete: {e}"


def move_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        src    = _resolve_existing(path, name)
        dst    = _destination(destination, src) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        return f"Moved: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not move: {e}"


def copy_file(path: str, name: str = "", destination: str = "") -> str:
    try:
        src  = _resolve_existing(path, name)
        dst  = _destination(destination, src) if destination else None

        if not src.exists():
            return f"Source not found: {src.name}"
        if dst is None:
            return "No destination specified."
        if not _is_safe_path(src):
            return f"Access denied (source): {src}"
        if not _is_safe_path(dst):
            return f"Access denied (destination): {dst}"

        dst.parent.mkdir(parents=True, exist_ok=True)

        if src.is_dir():
            shutil.copytree(str(src), str(dst))
        else:
            shutil.copy2(str(src), str(dst))

        return f"Copied: {src.name} → {dst.parent.name}/"

    except Exception as e:
        return f"Could not copy: {e}"


def rename_file(path: str, name: str = "", new_name: str = "") -> str:
    try:
        target   = _resolve_existing(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"
        if not new_name:
            return "No new name provided."

        new_path = target.parent / new_name
        if new_path.exists():
            return f"A file named '{new_name}' already exists here."

        target.rename(new_path)
        return f"Renamed: {target.name} → {new_name}"

    except Exception as e:
        return f"Could not rename: {e}"


def read_file(path: str, name: str = "", max_chars: int = 4000) -> str:
    try:
        target = _resolve_existing(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"File not found: {target.name}"
        if not target.is_file():
            return f"Not a file: {target.name}"

        content = target.read_text(encoding="utf-8", errors="ignore")
        if len(content) > max_chars:
            content = content[:max_chars] + f"\n\n[Truncated — {len(content)} total chars]"
        return content

    except Exception as e:
        return f"Could not read file: {e}"


def write_file(path: str, name: str = "", content: str = "",
               append: bool = False) -> str:
    try:
        target = _target(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)
        action = "Appended to" if append else "Written to"
        return f"{action}: {target.name}"
    except Exception as e:
        return f"Could not write file: {e}"


def find_files(name: str = "", extension: str = "",
               path: str = "home", max_results: int = 20) -> str:
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Search path not found: {path}"

        results    = []
        dir_count  = 0
        max_dirs   = 500  # performans + güvenlik limiti

        for item in search_path.rglob("*"):
            if item.is_dir():
                dir_count += 1
                if dir_count > max_dirs:
                    break
                continue
            if not item.is_file():
                continue
            if extension and item.suffix.lower() != extension.lower():
                continue
            if name and name.lower() not in item.name.lower():
                continue
            size = _format_size(item.stat().st_size)
            results.append(f"📄 {item.name} ({size}) — {item.parent}")
            if len(results) >= max_results:
                break

        if not results:
            query = name or extension or "files"
            return f"No {query} found in {search_path.name}/"

        return f"Found {len(results)} file(s):\n" + "\n".join(results)

    except Exception as e:
        return f"Search error: {e}"


def get_largest_files(path: str = "downloads", count: int = 10) -> str:
    count = min(count, 50)  # maksimum 50
    try:
        search_path = _resolve_path(path)
        if not _is_safe_path(search_path):
            return f"Access denied: {search_path}"
        if not search_path.exists():
            return f"Path not found: {path}"

        files = []
        for item in search_path.rglob("*"):
            if item.is_file():
                try:
                    files.append((item.stat().st_size, item))
                except Exception:
                    continue

        files.sort(reverse=True)
        top = files[:count]

        if not top:
            return "No files found."

        lines = [f"Top {len(top)} largest files in {search_path.name}/:"]
        for size, f in top:
            lines.append(f"  {_format_size(size):>10}  {f.name}  ({f.parent})")

        return "\n".join(lines)

    except Exception as e:
        return f"Error: {e}"


def get_disk_usage(path: str = "home") -> str:
    try:
        target = _resolve_path(path)
        usage  = shutil.disk_usage(target)
        pct    = usage.used / usage.total * 100
        return (
            f"Disk usage ({target}):\n"
            f"  Total : {_format_size(usage.total)}\n"
            f"  Used  : {_format_size(usage.used)} ({pct:.1f}%)\n"
            f"  Free  : {_format_size(usage.free)}"
        )
    except Exception as e:
        return f"Could not get disk usage: {e}"


def organize_desktop() -> str:
    type_map = {
        "Images":    {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".ico", ".heic"},
        "Documents": {".pdf", ".doc", ".docx", ".txt", ".xls", ".xlsx",
                      ".ppt", ".pptx", ".csv", ".odt", ".ods", ".odp"},
        "Videos":    {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm", ".m4v"},
        "Music":     {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a"},
        "Archives":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
        "Code":      {".py", ".js", ".ts", ".html", ".css", ".json", ".xml",
                      ".cpp", ".java", ".cs", ".go", ".rs", ".sh"},
    }

    desktop = _get_desktop()
    moved, skipped = [], []

    try:
        for item in desktop.iterdir():
            # Klasörlere, gizli dosyalara ve organize klasörlerine dokunma
            if item.is_dir() or item.name.startswith("."):
                continue
            if item.name in {k for k in type_map}:
                continue

            ext        = item.suffix.lower()
            target_dir = desktop / "Others"
            for folder, exts in type_map.items():
                if ext in exts:
                    target_dir = desktop / folder
                    break

            target_dir.mkdir(exist_ok=True)
            new_path = target_dir / item.name

            if new_path.exists():
                skipped.append(item.name)
                continue

            shutil.move(str(item), str(new_path))
            moved.append(f"{item.name} → {target_dir.name}/")

        result = f"Desktop organized: {len(moved)} files moved."
        if moved:
            preview = moved[:8]
            result += "\n" + "\n".join(preview)
            if len(moved) > 8:
                result += f"\n... and {len(moved) - 8} more."
        if skipped:
            result += f"\n{len(skipped)} file(s) skipped (name conflict)."
        return result

    except Exception as e:
        return f"Could not organize desktop: {e}"


def get_file_info(path: str, name: str = "") -> str:
    try:
        target = _resolve_existing(path, name)
        if not _is_safe_path(target):
            return f"Access denied: {target}"
        if not target.exists():
            return f"Not found: {target.name}"

        stat = target.stat()
        info = {
            "Name":      target.name,
            "Type":      "Folder" if target.is_dir() else "File",
            "Size":      _format_size(stat.st_size),
            "Location":  str(target.parent),
            "Created":   datetime.fromtimestamp(stat.st_ctime).strftime("%Y-%m-%d %H:%M"),
            "Modified":  datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
            "Extension": target.suffix or "—",
        }
        return "\n".join(f"  {k}: {v}" for k, v in info.items())

    except Exception as e:
        return f"Could not get file info: {e}"

def open_location(path: str, name: str = "") -> str:
    """Open a folder (or reveal a file) in the system file manager.

    Nothing else here could do this: `list` reads a folder's contents aloud,
    which is not what "open my downloads folder" means, and open_app only
    knows about installed applications. Asking for a folder therefore did
    nothing at all.
    """
    import subprocess as _sp
    target = _resolve_existing(path, name)
    if not _is_safe_path(target):
        return f"Access denied: {target}"
    if not target.exists():
        return f"There is no folder or file at {target}."
    try:
        if _OS == "Windows":
            if target.is_dir():
                _sp.Popen(["explorer", str(target)])
            else:
                # /select, puts the file in view inside its folder.
                _sp.Popen(["explorer", "/select,", str(target)])
        elif _OS == "Darwin":
            _sp.Popen(["open", "-R" if target.is_file() else "-a", "Finder", str(target)]
                      if target.is_file() else ["open", str(target)])
        else:
            _sp.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
        kind = "folder" if target.is_dir() else "file location"
        return f"Opened the {kind}: {target.name or target}."
    except Exception as e:
        return f"Could not open {target.name}: {e}"


def file_controller(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params = parameters or {}
    action = params.get("action", "").lower().strip()
    path   = params.get("path", "desktop")
    name   = params.get("name", "")

    if player:
        player.write_log(f"[file] {action} {name or path}")

    try:
        if action in ("open", "open_folder", "show", "reveal"):
            return open_location(path, name)

        if action == "list":
            return list_files(path)

        elif action == "create_file":
            return create_file(path, name=name, content=params.get("content", ""))

        elif action == "create_folder":
            return create_folder(path, name=name)

        elif action == "delete":
            return delete_file(path, name=name)

        elif action == "move":
            return move_file(path, name=name, destination=params.get("destination", ""))

        elif action == "copy":
            return copy_file(path, name=name, destination=params.get("destination", ""))

        elif action == "rename":
            return rename_file(path, name=name, new_name=params.get("new_name", ""))

        elif action == "read":
            return read_file(path, name=name)

        elif action == "write":
            return write_file(
                path, name=name,
                content=params.get("content", ""),
                append=params.get("append", False)
            )

        elif action == "find":
            return find_files(
                name=name or params.get("name", ""),
                extension=params.get("extension", ""),
                path=path,
                max_results=min(int(params.get("max_results", 20)), 50),
            )

        elif action == "largest":
            return get_largest_files(
                path=path,
                count=int(params.get("count", 10)),
            )

        elif action == "disk_usage":
            return get_disk_usage(path)

        elif action == "organize_desktop":
            return organize_desktop()

        elif action == "info":
            return get_file_info(path, name=name)

        else:
            return f"Unknown action: '{action}'"

    except Exception as e:
        return f"File controller error ({action}): {e}"