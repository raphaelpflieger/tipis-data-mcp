"""One JSON line per call to the audit log: who, what, on which database, how long, what failed.
Never the rows themselves. Rotated by logrotate (copytruncate), see ansible/templates/logrotate.j2."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Audit:
    def __init__(self, directory: Path, filename: str = "datamcp.jsonl"):
        self.path = directory / filename
        self._lock = threading.Lock()

    def log(self, kind: str, **fields: Any) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "kind": kind}
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        if "sql" in record:
            record["sql_sha256"] = hashlib.sha256(record["sql"].encode("utf-8")).hexdigest()
            record["sql"] = record["sql"][:4000]
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # auditing must never break a call; the process log still has the request
                pass
