from pathlib import Path

from scripts.check_live_disabled import main


def test_repo_config_passes() -> None:
    assert main(["config.yaml"]) == 0


def test_enabled_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("live:\n  enabled: true\n")
    assert main([str(p)]) == 1


def test_missing_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("live: {}\n")
    assert main([str(p)]) == 1
