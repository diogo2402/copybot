"""Pre-commit/CI guard: config.yaml must ship with live.enabled == false (CLAUDE.md §0.1, §13)."""

from __future__ import annotations

import sys

import yaml


def main(paths: list[str]) -> int:
    bad = 0
    for path in paths or ["config.yaml"]:
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        live = cfg.get("live") or {}
        if live.get("enabled") is not False:
            print(f"{path}: live.enabled must be exactly `false` (found {live.get('enabled')!r})")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
