"""Append-only JSONL log writers (data/logs/*.jsonl)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class JsonlLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, separators=(",", ":")) + "\n")

    def read(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]


def errors_log(data_dir: Path) -> JsonlLog:
    return JsonlLog(data_dir / "logs" / "errors.jsonl")
