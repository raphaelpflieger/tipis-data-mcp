"""The e-mail allowlist: one Google account per line, hot-reloaded from the file's mtime, so
removing a line revokes access at the next call without a restart."""

from __future__ import annotations

import os
from pathlib import Path


class EmailAllowlist:
    def __init__(self, path: Path):
        self.path = path
        self._mtime: float | None = None
        self._emails: frozenset[str] = frozenset()

    @staticmethod
    def normalize(email: str | None) -> str:
        return (email or "").strip().lower()

    def emails(self) -> frozenset[str]:
        try:
            mtime = os.stat(self.path).st_mtime
        except OSError:
            # a missing file allows nobody (fail closed) — and is logged by the caller
            self._mtime, self._emails = None, frozenset()
            return self._emails
        if mtime != self._mtime:
            lines = self.path.read_text(encoding="utf-8").splitlines()
            self._emails = frozenset(
                self.normalize(line.split("#", 1)[0]) for line in lines if line.split("#", 1)[0].strip()
            )
            self._mtime = mtime
        return self._emails

    def contains(self, email: str | None) -> bool:
        normalized = self.normalize(email)
        return bool(normalized) and normalized in self.emails()
