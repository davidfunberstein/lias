"""Timestamped dual logger — writes to console and to the active log file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


class Logger:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def _write(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level}] {message}"
        print(line)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def info(self, msg: str) -> None:
        self._write("INFO", msg)

    def warn(self, msg: str) -> None:
        self._write("WARN", msg)

    def error(self, msg: str) -> None:
        self._write("ERROR", msg)

    def ok(self, msg: str) -> None:
        self._write("OK", msg)

    def section(self, title: str) -> None:
        self._write("====", "=" * 60)
        self._write("====", f"  {title}")
        self._write("====", "=" * 60)
