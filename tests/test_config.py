from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from copybot.config import DEFAULT_CONFIG_PATH, Config, load_config


def test_repo_config_loads_with_spec_defaults() -> None:
    cfg = load_config()
    assert cfg.schedule.poll_minutes == 5
    assert cfg.selection.n_follow == 5
    assert cfg.risk.max_drawdown_stop_pct == 25
    assert cfg.live.enabled is False  # §0.1: ships locked off
    assert cfg.live.network is None


def test_repo_config_matches_model_defaults() -> None:
    """config.yaml and the pydantic defaults must not drift apart."""
    assert load_config() == Config()


def test_effective_min_holding_scales_with_poll() -> None:
    assert Config().effective_min_holding_hours == 4
    slow = Config.model_validate({"schedule": {"poll_minutes": 30}})
    assert slow.effective_min_holding_hours == 6


def test_unknown_key_rejected(tmp_path: Path) -> None:
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text())
    raw["risk"]["max_leverge"] = 3  # typo must not be silently ignored
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump(raw))
    with pytest.raises(ValidationError):
        load_config(p)


def test_live_network_must_be_explicit_value() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"live": {"network": "main"}})


def test_keep_rank_at_least_n() -> None:
    with pytest.raises(ValidationError):
        Config.model_validate({"selection": {"n_follow": 5, "keep_if_rank_within": 3}})
