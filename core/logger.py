"""Timestamped dual logger — writes to console and to the active log file.

Log format (screen + file): [HH:MM:SS] [PORTAL] [LEVEL] message
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class Logger:
    def __init__(self, log_path: Path, portal: str = "") -> None:
        self.log_path = log_path
        self.portal = portal.upper() if portal else ""
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        portal_tag = f" [{self.portal}]" if self.portal else ""
        line = f"[{ts}]{portal_tag} [{level}] {message}"
        print(line)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def debug(self, msg: str) -> None:
        self._write("DEBUG", msg)

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

    def ok(self, msg: str) -> None:
        self._write("INFO", msg)

    def section(self, title: str) -> None:
        sep = "─" * 50
        self._write("INFO", sep)
        self._write("INFO", f"  {title}")
        self._write("INFO", sep)
