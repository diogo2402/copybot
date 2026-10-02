"""CLI entry: python -m copybot <command>."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from copybot.config import DEFAULT_CONFIG_PATH, load_config

RUN_ID = uuid.uuid4().hex[:12]
REPO_ROOT = Path(__file__).resolve().parent.parent

COMMANDS = ("probe", "refresh", "poll", "report", "weekly", "backtest", "status", "kill", "unkill")
NOT_YET = {
    "refresh": 2,
    "poll": 3,
    "backtest": 3,
    "report": 5,
    "weekly": 7,
    "status": 7,
    "kill": 3,
    "unkill": 3,
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "run_id": RUN_ID,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="copybot", description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to config.yaml")
    p.add_argument("command", choices=COMMANDS)
    p.add_argument("--local", action="store_true", help="use local ./data state (no git)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging()
    log = logging.getLogger("copybot")
    cfg = load_config(args.config)  # validate config on every command, fail fast
    log.info("start command=%s poll_minutes=%s", args.command, cfg.schedule.poll_minutes)

    if args.command == "probe":
        sys.path.insert(0, str(REPO_ROOT))
        from scripts.probe_api import run_probe

        return run_probe()

    log.error(
        "command %r is not implemented yet (planned for phase %d)",
        args.command,
        NOT_YET[args.command],
    )
    return 2
