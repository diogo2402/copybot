"""Evaluate selection rule sets on <data>/whatif_metrics.json (from whatif_selection.py).

Run:  uv run python scripts/whatif_report.py [data_dir]
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

Rec = dict[str, Any]


@dataclass(frozen=True)
class Rules:
    name: str
    episodes: bool = False  # use FIFO episodes instead of flat-to-flat round trips
    max_dd: float = 0.40
    min_pos_weeks: float = 0.50
    max_trades_per_day: float = 30
    min_hold_h: float = 4
    max_maker: float = 0.6
    max_conc: float = 0.5
    min_active_30: int = 8
    max_lev: float = 25
    min_trades: int = 20
    min_capture: float = 0.4
    lag_capture: bool = False  # capture = lagged / ideal copy instead of copy / trader return


def get(r: Rec, *keys: str) -> Any:
    x: Any = r
    for k in keys:
        x = (x or {}).get(k)
    return x


def checks(r: Rec, ru: Rules) -> dict[str, bool]:
    """rule name -> passes. Only for wallets with full metrics."""
    p, f, c = r["portfolio"], r["fills"], r["copy"]
    n = f["n_episodes"] if ru.episodes else f["n_trades"]
    tpd = f["episodes_per_day"] if ru.episodes else f["trades_per_day"]
    hold = f["median_hold_fifo_hours"] if ru.episodes else f["median_holding_hours"]
    conc = f["top_episode_concentration"] if ru.episodes else f["top_trade_concentration"]
    return {
        "drawdown": p["max_drawdown_90d"] <= ru.max_dd,
        "positive_weeks": p["positive_weeks_ratio"] >= ru.min_pos_weeks,
        "trades_per_day": tpd <= ru.max_trades_per_day,
        "holding_time": hold is not None and hold >= ru.min_hold_h,
        "maker_ratio": f["maker_ratio"] <= ru.max_maker,
        "concentration": conc <= ru.max_conc,
        "active_days": f["active_days_last_30"] >= ru.min_active_30,
        "max_leverage": f["max_leverage"] <= ru.max_lev,
        "min_trades": n >= ru.min_trades,
        "copy_profitable": c["copy_return_90d"] > 0,
        "capture": (c["lag_capture"] if ru.lag_capture else c["copy_capture_ratio"])
        >= ru.min_capture,
    }


def evaluate(recs: list[Rec], ru: Rules) -> tuple[list[Rec], dict[str, int]]:
    full = [r for r in recs if r.get("copy")]
    passing = [r for r in full if all(checks(r, ru).values())]
    leave_one_out = {}
    for rule in checks(full[0], ru) if full else {}:
        leave_one_out[rule] = sum(
            1 for r in full if all(v for k, v in checks(r, ru).items() if k != rule)
        )
    return passing, leave_one_out


def fmt(r: Rec, ru: Rules) -> str:
    p, f, c = r["portfolio"], r["fills"], r["copy"]
    n = f["n_episodes"] if ru.episodes else f["n_trades"]
    hold = f["median_hold_fifo_hours"] if ru.episodes else f["median_holding_hours"]
    return (
        f"  {r['address']}  acct ${r['candidate']['account_value'] / 1e6:5.1f}M  "
        f"ret {p['return_90d'] * 100:+6.1f}%  DD {p['max_drawdown_90d'] * 100:4.0f}%  "
        f"+wks {p['positive_weeks_ratio']:.2f}  trades {n:4}  hold {hold or 0:6.1f}h  "
        f"copy {c['copy_return_90d'] * 100:+6.1f}%  copyDD {c['copy_max_drawdown'] * 100:4.1f}%  "
        f"capture {c['copy_capture_ratio']:.2f}  lagcapt {c['lag_capture']:.2f}"
    )


def main() -> int:
    d = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    recs = json.loads((d / "whatif_metrics.json").read_text())
    full = [r for r in recs if r.get("copy")]
    skipped: dict[str, int] = {}
    for r in recs:
        if not r.get("copy"):
            k = str(r.get("skip", "no metrics")).split(":")[0]
            skipped[k] = skipped.get(k, 0) + 1
    print(
        f"{len(recs)} candidates; {len(full)} with full metrics + copy sim; "
        f"not simulated: {skipped}\n"
    )

    spec = Rules("spec as written")
    ep = replace(spec, name="episodes instead of round trips", episodes=True)
    scenarios = [
        spec,
        ep,
        replace(ep, name="episodes + DD ≤ 50%", max_dd=0.50),
        replace(ep, name="episodes + DD ≤ 60%", max_dd=0.60),
        replace(ep, name="episodes + min 10 trades", min_trades=10),
        replace(ep, name="episodes + DD ≤ 50% + min 10 trades", max_dd=0.50, min_trades=10),
        replace(ep, name="episodes + lag capture", lag_capture=True),
        replace(ep, name="episodes + lag capture + DD ≤ 50%", lag_capture=True, max_dd=0.50),
        replace(spec, name="round trips + lag capture", lag_capture=True),
    ]
    rows: list[tuple[str, int]] = []
    for ru in scenarios:
        passing, loo = evaluate(recs, ru)
        rows.append((ru.name, len(passing)))
        print(f"=== {ru.name}: {len(passing)} pass")
        print(
            "    pass if only this rule were removed: "
            + ", ".join(f"{k} {v}" for k, v in sorted(loo.items(), key=lambda kv: -kv[1]))
        )
        for r in sorted(passing, key=lambda r: -r["copy"]["copy_return_90d"])[:10]:
            print(fmt(r, ru))
        print()

    fail_counts: dict[str, int] = {}
    for r in full:
        for k, v in checks(r, ep).items():
            if not v:
                fail_counts[k] = fail_counts.get(k, 0) + 1
    print("Rule failures among fully-analysed wallets (episode rules; a wallet can fail several):")
    for k, v in sorted(fail_counts.items(), key=lambda kv: -kv[1]):
        print(f"  {v:4}  {k}")
    by: Callable[[Rec], float] = lambda r: r["copy"]["copy_return_90d"]  # noqa: E731
    pos = [r for r in full if by(r) > 0]
    print(f"\nCopy sim profitable for {len(pos)}/{len(full)} wallets (before any other rule).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
