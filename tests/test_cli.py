import json

import pytest

from copybot.cli import main
from copybot.clock import FixedClock, dt_to_ms, ms_to_dt


def test_unimplemented_command_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["poll"]) == 2
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert lines[-1]["level"] == "ERROR" and "phase 3" in lines[-1]["msg"]
    assert all("run_id" in line for line in lines)


def test_clock_roundtrip() -> None:
    c = FixedClock(1_700_000_000_000)
    c.advance(1000)
    assert dt_to_ms(ms_to_dt(c.now_ms())) == 1_700_000_001_000


def test_naive_datetime_rejected() -> None:
    from datetime import datetime

    with pytest.raises(ValueError):
        dt_to_ms(datetime(2026, 1, 1))  # noqa: DTZ001
