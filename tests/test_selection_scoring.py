import pytest

from copybot.config import Config, WeightsConfig
from copybot.selection.candidates import stage1
from copybot.selection.scoring import (
    ScoreInputs,
    apply_hysteresis,
    composite_scores,
    rank,
    shuffled,
    winsorize,
    zscores,
)
from tests.factories import lb_row

# ------------------------------------------------------------ stage 1


def test_stage1_rules_and_order() -> None:
    cfg = Config().selection.model_copy(update={"blocklist": ["0xBLOCK"]})
    rows = [
        lb_row("0xgood1", month_pnl=10_000),
        lb_row("0xgood2", month_pnl=90_000),
        lb_row("0xsmall", av=50_000),
        lb_row("0xloser", alltime_pnl=-1),
        lb_row("0xmonthneg", month_pnl=-5),
        lb_row("0xroineg", month_roi=-0.01),
        lb_row("0xlowvol", month_vlm=500_000),
        lb_row("0xmm", av=200_000, month_vlm=200_000 * 201),
        lb_row("0xblock"),
    ]
    r = stage1(rows, cfg)
    assert [c.address for c in r.survivors] == ["0xgood2", "0xgood1"]
    assert r.rejected == {
        "account_value_low": 1,
        "alltime_pnl_not_positive": 1,
        "month_not_positive": 2,
        "month_volume_low": 1,
        "turnover_too_high": 1,
        "blocklisted": 1,
    }


def test_stage1_pool_cap() -> None:
    cfg = Config().selection.model_copy(update={"stage2_pool": 2})
    rows = [lb_row(f"0x{i}", month_pnl=1000 + i) for i in range(5)]
    r = stage1(rows, cfg)
    assert len(r.survivors) == 5
    assert [c.address for c in r.pool] == ["0x4", "0x3"]


# ------------------------------------------------------------ scoring


def test_winsorize_clips_tails() -> None:
    xs = [float(i) for i in range(21)] + [1000.0]
    w = winsorize(xs)
    assert max(w) < 1000 and min(w) >= 1


def test_zscores_constant_is_zero() -> None:
    assert zscores([3.0, 3.0, 3.0]) == [0.0, 0.0, 0.0]
    assert zscores([1.0]) == [0.0]


def _si(addr: str, cr: float, lev: float = 1.0) -> ScoreInputs:
    return ScoreInputs(addr, cr, 1.0, 0.6, 0.8, lev)


def test_composite_prefers_return_and_low_leverage() -> None:
    s = composite_scores([_si("a", 0.10), _si("b", 0.30), _si("c", 0.20)], WeightsConfig())
    assert rank(s) == ["b", "c", "a"]
    s2 = composite_scores([_si("a", 0.2, lev=1), _si("b", 0.2, lev=5)], WeightsConfig())
    assert rank(s2) == ["a", "b"]


def test_rank_ties_deterministic() -> None:
    assert rank({"0xb": 1.0, "0xa": 1.0}) == ["0xa", "0xb"]


# ------------------------------------------------------------ hysteresis (§6.6)

RANKED = [f"w{i}" for i in range(1, 21)]  # w1 best


def hyst(previous, ranked=RANKED, **kw):  # type: ignore[no-untyped-def]
    args = {"n": 5, "keep_within": 10, "max_additions": 2, "exclusion_reasons": {}}
    args.update(kw)
    return apply_hysteresis(ranked, previous, **args)


def test_first_day_follows_top_n() -> None:
    followed, changes = hyst([])
    assert followed == ["w1", "w2", "w3", "w4", "w5"]
    assert all(c.action == "added" for c in changes)


def test_followed_kept_within_2n() -> None:
    prev = ["w6", "w7", "w8", "w9", "w10"]  # all ranked 6-10: none in top 5, all within 10
    followed, changes = hyst(prev)
    assert followed == prev
    assert {c.action for c in changes} == {"kept"}


def test_dropped_outside_2n_and_max_two_additions() -> None:
    prev = ["w11", "w12", "w13", "w14", "w15"]  # all fell outside top 10
    followed, changes = hyst(prev)
    dropped = [c for c in changes if c.action == "dropped"]
    added = [c for c in changes if c.action == "added"]
    assert len(dropped) == 5
    assert [c.address for c in added] == ["w1", "w2"]  # at most 2 per day
    assert followed == ["w1", "w2"]  # remaining sleeves stay in cash


def test_excluded_wallet_dropped_with_reason() -> None:
    ranked = [a for a in RANKED if a != "w3"]
    followed, changes = hyst(
        ["w1", "w2", "w3", "w4", "w5"], ranked=ranked, exclusion_reasons={"w3": "drawdown_too_high"}
    )
    assert "w3" not in followed
    drop = next(c for c in changes if c.address == "w3")
    assert drop.action == "dropped" and "drawdown_too_high" in drop.reason
    assert followed == ["w1", "w2", "w4", "w5", "w6"]


def test_new_wallet_only_joins_from_top_n() -> None:
    prev = ["w1", "w2", "w3", "w4"]  # one slot open; w5 is the only top-5 non-followed
    followed, _ = hyst(prev)
    assert followed == ["w1", "w2", "w3", "w4", "w5"]
    followed, _ = hyst(["w1", "w2", "w3", "w4"], ranked=["w1", "w2", "w3", "w4"])
    assert followed == ["w1", "w2", "w3", "w4"]  # fewer than N qualify: follow fewer


def test_unknown_wallet_retained_on_our_error() -> None:
    ranked = [a for a in RANKED if a != "w2"]
    followed, changes = hyst(["w1", "w2"], ranked=ranked, unknown={"w2"})
    assert "w2" in followed
    assert next(c for c in changes if c.address == "w2").action == "kept"


def test_zero_qualifying() -> None:
    followed, _ = hyst(["w1"], ranked=[], exclusion_reasons={"w1": "x"})
    assert followed == []


# ------------------------------------------------------------ random draw


def test_random_draw_is_deterministic_per_month() -> None:
    addrs = [f"0x{i:02d}" for i in range(50)]
    assert shuffled(addrs, "2026-10") == shuffled(list(reversed(addrs)), "2026-10")
    assert shuffled(addrs, "2026-10") != shuffled(addrs, "2026-11")


@pytest.mark.parametrize("n", [0, 1])
def test_scores_small_inputs(n: int) -> None:
    items = [_si("a", 0.1)][:n]
    assert len(composite_scores(items, WeightsConfig())) == n


def test_stage1_pool_sorted_by_roi() -> None:
    cfg = Config().selection.model_copy(update={"stage2_pool": 2, "pool_sort": "month_roi"})
    rows = [
        lb_row("0xwhale", month_pnl=900_000, month_roi=0.05),
        lb_row("0xskilled", month_pnl=60_000, month_roi=0.40),
        lb_row("0xmid", month_pnl=80_000, month_roi=0.20),
    ]
    assert [c.address for c in stage1(rows, cfg).pool] == ["0xskilled", "0xmid"]
