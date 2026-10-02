"""Stage 1: coarse filter on leaderboard data only (§6.2). Cheap, no extra API calls."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from copybot.config import SelectionConfig
from copybot.hl.models import LeaderboardRow


@dataclass(frozen=True)
class Candidate:
    address: str
    display_name: str | None
    account_value: Decimal
    month_pnl: Decimal
    month_roi: Decimal
    month_vlm: Decimal
    alltime_pnl: Decimal

    @property
    def turnover_ratio(self) -> Decimal:
        return (
            self.month_vlm / self.account_value if self.account_value > 0 else Decimal("Infinity")
        )


@dataclass(frozen=True)
class Stage1Result:
    survivors: list[Candidate]  # all stage-1 survivors, sorted by `pool_sort` desc
    pool: list[Candidate]  # top `stage2_pool` of survivors
    rejected: Counter[str]  # reason -> count


def stage1_reason(c: Candidate, cfg: SelectionConfig, blocklist: set[str]) -> str | None:
    """First failing rule, or None if the candidate passes."""
    if c.address in blocklist:
        return "blocklisted"
    if c.account_value < cfg.min_account_value:
        return "account_value_low"
    if c.alltime_pnl <= 0:
        return "alltime_pnl_not_positive"
    if c.month_pnl <= 0 or c.month_roi <= 0:
        return "month_not_positive"
    if c.month_vlm < cfg.min_month_volume:
        return "month_volume_low"
    if c.turnover_ratio > cfg.max_turnover_ratio:
        return "turnover_too_high"
    return None


def to_candidate(row: LeaderboardRow) -> Candidate | None:
    month = row.perf("month")
    alltime = row.perf("allTime")
    if month is None or alltime is None:
        return None
    return Candidate(
        address=row.ethAddress.lower(),
        display_name=row.displayName,
        account_value=row.accountValue,
        month_pnl=month.pnl,
        month_roi=month.roi,
        month_vlm=month.vlm,
        alltime_pnl=alltime.pnl,
    )


def stage1(rows: list[LeaderboardRow], cfg: SelectionConfig) -> Stage1Result:
    blocklist = {a.lower() for a in cfg.blocklist}
    rejected: Counter[str] = Counter()
    survivors: list[Candidate] = []
    for row in rows:
        c = to_candidate(row)
        if c is None:
            rejected["missing_windows"] += 1
            continue
        reason = stage1_reason(c, cfg, blocklist)
        if reason:
            rejected[reason] += 1
        else:
            survivors.append(c)
    if cfg.pool_sort == "month_roi":
        survivors.sort(key=lambda c: (-c.month_roi, c.address))
    else:
        survivors.sort(key=lambda c: (-c.month_pnl, c.address))
    return Stage1Result(survivors, survivors[: cfg.stage2_pool], rejected)
