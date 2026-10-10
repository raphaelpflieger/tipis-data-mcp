from __future__ import annotations

import os
import time
from pathlib import Path

from datamcp.allowlist import EmailAllowlist


def test_contains_normalizes_and_ignores_comments(tmp_path: Path):
    path = tmp_path / "allowed.txt"
    path.write_text("# owners\n Alice@Example.org   # shared account\n\nbob@example.org\n", encoding="utf-8")
    allowlist = EmailAllowlist(path)
    assert allowlist.contains("alice@example.org")
    assert allowlist.contains("ALICE@example.org ")
    assert allowlist.contains("bob@example.org")
    assert not allowlist.contains("eve@example.org")
    assert not allowlist.contains("")
    assert not allowlist.contains(None)


def test_reload_on_change_and_missing_file_allows_nobody(tmp_path: Path):
    path = tmp_path / "allowed.txt"
    path.write_text("alice@example.org\n", encoding="utf-8")
    allowlist = EmailAllowlist(path)
    assert allowlist.contains("alice@example.org")
    time.sleep(0.01)
    path.write_text("bob@example.org\n", encoding="utf-8")
    os.utime(path, (time.time() + 5, time.time() + 5))  # a new mtime whatever the filesystem granularity
    assert not allowlist.contains("alice@example.org")
    assert allowlist.contains("bob@example.org")
    path.unlink()
    assert not allowlist.contains("bob@example.org")
