"""Print the latest shortlist and candidate table for review.

Run:  uv run python scripts/show_shortlist.py [data_dir] [--top N]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def pct(x: Any) -> str:
    return "—" if x is None else f"{x * 100:+.1f}%"


def num(x: Any, nd: int = 1) -> str:
    return "—" if x is None else f"{x:.{nd}f}"


def money(x: Any) -> str:
    if x is None:
        return "—"
    for unit, div in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(x) >= div:
            return f"${x / div:.1f}{unit}"
    return f"${x:.0f}"


COLS = [
    ("rank", 4),
    ("address", 12),
    ("score", 6),
    ("acct", 7),
    ("ret90", 7),
    ("maxDD", 6),
    ("ret/DD", 6),
    ("+wks", 5),
    ("trd/d", 5),
    ("holdH", 6),
    ("maker", 5),
    ("lev", 4),
    ("topTr", 5),
    ("copy90", 7),
    ("copyDD", 6),
    ("capt", 5),
    ("lagbp", 5),
]


def row(c: dict[str, Any]) -> list[str]:
    p, f, cp = c.get("portfolio") or {}, c.get("fills") or {}, c.get("copy") or {}
    return [
        str(c.get("rank") or ""),
        c["address"][:6] + "…" + c["address"][-4:],
        num(c.get("score"), 2),
        money(c["candidate"]["account_value"]),
        pct(p.get("return_90d")),
        pct(p.get("max_drawdown_90d")),
        num(p.get("return_to_drawdown")),
        num(p.get("positive_weeks_ratio"), 2),
        num(f.get("trades_per_day")),
        num(f.get("median_holding_hours")),
        num(f.get("maker_ratio"), 2),
        num(f.get("avg_leverage")),
        num(f.get("top_trade_concentration"), 2),
        pct(cp.get("copy_return_90d")),
        pct(cp.get("copy_max_drawdown")),
        num(cp.get("copy_capture_ratio"), 2),
        num(cp.get("avg_lag_cost_bps")),
    ]


def table(items: list[dict[str, Any]]) -> str:
    lines = ["  ".join(name.rjust(w) for name, w in COLS)]
    for c in items:
        lines.append("  ".join(v.rjust(w) for v, (_, w) in zip(row(c), COLS, strict=True)))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", nargs="?", default=str(ROOT / "data"))
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()
    d = Path(args.data_dir)
    sl = json.loads((d / "shortlist.json").read_text())
    cands = json.loads((d / "candidates_latest.json").read_text())["candidates"]
    st = sl["stats"]

    print(f"Shortlist generated {sl['generated_at']}  (poll every {sl['poll_minutes']} min)\n")
    print(
        f"Leaderboard rows {st['leaderboard_rows']:,} → stage-1 survivors "
        f"{st['stage1_survivors']:,} → analysed {st['analysed']} → eligible {st['eligible']}"
    )
    print(
        f"API weight {st['weight_used']:,}, {st['requests']} requests, "
        f"{st['duration_s'] / 60:.0f} min\n"
    )

    print(f"FOLLOWED ({len(sl['followed'])} of {sl['n_follow']}):")
    if sl["followed"]:
        print(table([w["analysis"] for w in sl["followed"] if w.get("analysis")]))
    else:
        print("  none — all sleeves in cash")
    print("\nChanges:")
    for ch in sl["changes"]:
        print(f"  {ch['action']:8} {ch['address']}  {ch['reason']}")

    ranked = [c for c in cands if c.get("rank")]
    print(f"\nTOP {min(args.top, len(ranked))} ELIGIBLE:")
    print(table(ranked[: args.top]))

    print("\nExcluded by reason (stage 2/3):")
    for reason, n in st["excluded_by_reason"].items():
        print(f"  {n:4}  {reason}")
    print("Stage-1 rejections:")
    for reason, n in st["stage1_rejected"].items():
        print(f"  {n:6,}  {reason}")
    if st["status"].get("error") or st["status"].get("skipped"):
        print(f"\nNot analysed: {st['status']}")

    rc = sl.get("random_control") or {}
    print(
        f"\nRandom control ({rc.get('month')}): {len(rc.get('wallets', []))} wallets "
        f"after {rc.get('tried')} draws"
    )
    for w in rc.get("wallets", []):
        print(f"  {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
