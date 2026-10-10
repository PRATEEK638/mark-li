"""
Folder organiser: sort a messy folder into categories, reversibly.

Bulk-moving someone's files on a voice command is the highest-consequence
thing in this project short of messaging a person. A misheard folder name
moves the wrong hundred files, so three properties matter more than features:

  * **Preview first.** `preview` reports exactly what would move, and moves
    nothing. It is the natural thing to do before the real run.
  * **Always undoable.** Every run writes a journal of source -> destination.
    `undo` walks it backwards and puts every file back where it was.
  * **Never destructive.** Nothing is deleted or overwritten. A name collision
    gets a numbered suffix; a file that cannot be moved is reported, not
    skipped in silence.

The predecessor (`organize_desktop`) had none of these: desktop only, no
preview, no undo, and conflicts were dropped with a count.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

from actions.file_controller import (
    _format_size, _get_desktop, _is_safe_path, _resolve_path,
)

JOURNAL = Path(__file__).resolve().parent.parent / "memory" / "organize_journal.json"

CATEGORIES: dict[str, set[str]] = {
    "Images":     {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg",
                   ".ico", ".heic", ".tiff", ".raw", ".psd"},
    "Documents":  {".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".md",
                   ".tex", ".epub", ".pages"},
    "Spreadsheets": {".xls", ".xlsx", ".csv", ".ods", ".numbers"},
    "Presentations": {".ppt", ".pptx", ".odp", ".key"},
    "Videos":     {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm",
                   ".m4v", ".mpg", ".mpeg"},
    "Music":      {".mp3", ".wav", ".flac", ".aac", ".ogg", ".wma", ".m4a",
                   ".opus", ".mid"},
    "Archives":   {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"},
    "Code":       {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
                   ".json", ".xml", ".yml", ".yaml", ".cpp", ".c", ".h",
                   ".java", ".cs", ".go", ".rs", ".sh", ".rb", ".php", ".sql",
                   ".ipynb"},
    "Installers": {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".appx"},
    "Shortcuts":  {".lnk", ".url", ".desktop"},
}

OTHER = "Other"
ALL_FOLDERS = set(CATEGORIES) | {OTHER}


def _category(path: Path) -> str:
    ext = path.suffix.lower()
    for name, exts in CATEGORIES.items():
        if ext in exts:
            return name
    return OTHER


def _unique(target: Path) -> Path:
    """A free path next to `target` - never overwrite an existing file."""
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    for i in range(2, 1000):
        candidate = parent / f"{stem} ({i}){suffix}"
        if not candidate.exists():
            return candidate
    return parent / f"{stem} ({datetime.now():%Y%m%d%H%M%S}){suffix}"


def _destination(folder: Path, item: Path, mode: str) -> Path:
    if mode == "date":
        stamp = datetime.fromtimestamp(item.stat().st_mtime)
        return folder / f"{stamp:%Y-%m}"
    if mode == "extension":
        return folder / (item.suffix.lower().lstrip(".") or "no-extension")
    return folder / _category(item)


def _candidates(folder: Path, mode: str, include_folders: bool) -> list[Path]:
    """Files eligible to be moved, in a stable order."""
    items: list[Path] = []
    for item in sorted(folder.iterdir()):
        if item.name.startswith("."):
            continue
        if item.is_dir():
            # Never re-organise the category folders this tool created, and
            # leave other folders alone unless explicitly asked.
            if not include_folders or item.name in ALL_FOLDERS:
                continue
            items.append(item)
            continue
        if item.is_file():
            items.append(item)
    return items


def _plan(folder: Path, mode: str, include_folders: bool) -> list[tuple[Path, Path]]:
    moves: list[tuple[Path, Path]] = []
    for item in _candidates(folder, mode, include_folders):
        dest_dir = _destination(folder, item, mode)
        if dest_dir == item:
            continue
        moves.append((item, dest_dir / item.name))
    return moves


def _summarise(moves: list[tuple[Path, Path]], folder: Path, verb: str,
               sizes: dict[Path, int] | None = None) -> str:
    if not moves:
        return f"Nothing to organise in {folder.name}/ - it is already tidy."

    groups: dict[str, list[Path]] = {}
    total = 0
    for src, dst in moves:
        groups.setdefault(dst.parent.name, []).append(src)
        if sizes is not None:
            total += sizes.get(src, 0)
        else:
            try:
                total += src.stat().st_size
            except OSError:
                pass

    lines = [f"{verb} {len(moves)} item(s) in {folder.name}/ "
             f"({_format_size(total)}) into {len(groups)} folder(s):"]
    for name in sorted(groups, key=lambda k: -len(groups[k])):
        members = groups[name]
        sample  = ", ".join(p.name for p in members[:3])
        if len(members) > 3:
            sample += f", +{len(members) - 3} more"
        lines.append(f"  {name}/  {len(members)} - {sample}")
    return "\n".join(lines)


# -- journal ---------------------------------------------------------------
def _write_journal(folder: Path, done: list[tuple[Path, Path]]) -> None:
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        JOURNAL.write_text(json.dumps({
            "folder": str(folder),
            "at": datetime.now().isoformat(timespec="seconds"),
            "moves": [{"from": str(s), "to": str(d)} for s, d in done],
        }, indent=1), encoding="utf-8")
    except Exception as exc:
        print("[Organizer] could not write undo journal: " + str(exc)[:90])


def _read_journal() -> dict | None:
    try:
        return json.loads(JOURNAL.read_text(encoding="utf-8"))
    except Exception:
        return None


# -- actions ---------------------------------------------------------------
def preview(path: str = "desktop", mode: str = "type",
            include_folders: bool = False) -> str:
    folder = _resolve_path(path)
    if not folder.is_dir():
        return f"Not a folder: {folder}"
    if not _is_safe_path(folder):
        return f"Access denied: {folder}"
    moves = _plan(folder, mode, include_folders)
    out = _summarise(moves, folder, "Would move")
    if moves:
        out += "\n\nNothing has been moved yet. Say 'organize it' to go ahead."
    return out


def organize(path: str = "desktop", mode: str = "type",
             include_folders: bool = False) -> str:
    folder = _resolve_path(path)
    if not folder.is_dir():
        return f"Not a folder: {folder}"
    if not _is_safe_path(folder):
        return f"Access denied: {folder}"

    moves = _plan(folder, mode, include_folders)
    if not moves:
        return f"Nothing to organise in {folder.name}/ - it is already tidy."

    # Sizes have to be read before the move; reading them afterwards stats a
    # path that no longer exists and reports the whole run as 0 B.
    sizes: dict[Path, int] = {}
    for src, _ in moves:
        try:
            sizes[src] = src.stat().st_size
        except OSError:
            sizes[src] = 0

    done: list[tuple[Path, Path]] = []
    failed: list[str] = []
    for src, dst in moves:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            final = _unique(dst)
            shutil.move(str(src), str(final))
            done.append((src, final))
        except Exception as exc:
            # Locked or in-use files are normal; name them rather than hide them.
            failed.append(f"{src.name} ({type(exc).__name__})")

    if done:
        _write_journal(folder, done)

    out = _summarise(done, folder, "Moved", sizes)
    if failed:
        shown = ", ".join(failed[:4]) + (f", +{len(failed) - 4} more" if len(failed) > 4 else "")
        out += f"\nCould not move {len(failed)}: {shown}"
    if done:
        out += "\nSay 'undo the organize' to put everything back."
    return out


def undo() -> str:
    data = _read_journal()
    if not data or not data.get("moves"):
        return "There is no organise run to undo."

    restored, failed = 0, 0
    for entry in reversed(data["moves"]):
        src, dst = Path(entry["from"]), Path(entry["to"])
        try:
            if not dst.exists():
                failed += 1
                continue
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(_unique(src)))
            restored += 1
        except Exception:
            failed += 1

    # Remove the category folders this run put things into, once empty - and
    # ONLY those. Sweeping every empty directory deleted folders the user had
    # created themselves and the organiser never touched.
    folder = Path(data.get("folder", ""))
    created = {Path(e["to"]).parent for e in data["moves"]}
    for child in created:
        try:
            if child.is_dir() and child != folder and not any(child.iterdir()):
                child.rmdir()
        except Exception:
            pass

    try:
        JOURNAL.unlink(missing_ok=True)
    except Exception:
        pass

    out = f"Restored {restored} item(s) to {folder.name}/."
    if failed:
        out += f" {failed} could not be restored (moved or removed since)."
    return out


def file_organizer(parameters: dict = None, response=None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "preview").lower().strip()
    path   = params.get("path") or "desktop"
    mode   = (params.get("mode") or "type").lower().strip()
    folders = bool(params.get("include_folders", False))

    if mode not in ("type", "date", "extension"):
        mode = "type"

    if player:
        player.write_log(f"[organize] {action} {path} by {mode}")

    try:
        if action in ("preview", "plan", "dry_run", "check"):
            return preview(path, mode, folders)
        if action in ("organize", "organise", "sort", "clean", "tidy", "run"):
            return organize(path, mode, folders)
        if action in ("undo", "revert", "restore"):
            return undo()
        return (f"Unknown action '{action}'. Use preview, organize, or undo.")
    except Exception as exc:
        return f"Organizer error ({action}): {exc}"
