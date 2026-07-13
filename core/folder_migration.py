"""Folder migration utilities — move client folders under the correct lawyer directory."""
from __future__ import annotations
import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.logger import Logger

def _ts() -> str:
    return datetime.now().strftime("[%Y-%m-%d %H:%M:%S]")

def _name_matches(folder_name: str, target: str) -> bool:
    """Fuzzy match: all significant words of target appear in folder_name."""
    import re
    words = [w for w in re.findall(r'\w+', target) if len(w) > 1]
    name_lower = folder_name.replace("_", " ").lower()
    return all(w.lower() in name_lower for w in words)

def migrate_client_to_lawyer(
    downloads_dir: Path,
    client_name: str,
    lawyer_name: str,
    logger: "Logger | None" = None,
) -> bool:
    """
    If a folder matching client_name exists directly under downloads_dir
    (i.e., not already inside a lawyer subfolder), move it under downloads_dir/{lawyer_name}/.

    Returns True if a migration happened.
    """
    def _log(msg: str, level: str = "info") -> None:
        print(f"{_ts()} [FolderMigrate] {msg}")
        if logger:
            getattr(logger, level, logger.info)(f"[FolderMigrate] {msg}")

    if not downloads_dir.exists():
        return False

    # Find client folder directly under downloads_dir
    client_folder: Path | None = None
    for d in downloads_dir.iterdir():
        if d.is_dir() and _name_matches(d.name, client_name):
            # Make sure it's not already the lawyer folder
            if not _name_matches(d.name, lawyer_name):
                client_folder = d
                break

    if not client_folder:
        return False

    # Ensure lawyer folder exists
    lawyer_folder = downloads_dir / lawyer_name
    lawyer_folder.mkdir(parents=True, exist_ok=True)

    dest = lawyer_folder / client_folder.name
    if dest.exists():
        _log(f"Destination already exists: {dest} — skipping migration.", "warn")
        return False

    _log(f"Moving '{client_folder.name}' -> '{lawyer_folder.name}/{client_folder.name}'")
    shutil.move(str(client_folder), str(dest))
    _log(f"Migration complete.")
    return True

def find_unassigned_client_folders(downloads_dir: Path, known_lawyers: list[str]) -> list[Path]:
    """Return folders directly under downloads_dir that don't match any known lawyer name."""
    if not downloads_dir.exists():
        return []
    result = []
    for d in downloads_dir.iterdir():
        if not d.is_dir():
            continue
        is_lawyer = any(_name_matches(d.name, lawyer) for lawyer in known_lawyers)
        if not is_lawyer:
            result.append(d)
    return result
