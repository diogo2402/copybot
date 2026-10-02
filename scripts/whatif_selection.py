"""What-if analysis for selection thresholds (CLAUDE.md §16).

Recomputes every metric and the copy simulation for the candidates in the last refresh,
without early exits, then counts how many wallets survive under alternative rule sets. Writes
<data>/whatif_metrics.json. Uses (and fills) the same fills cache as `refresh`.

Run:  uv run python scripts/whatif_selection.py [data_dir]
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from copybot.clock import SystemClock  # noqa: E402
from copybot.config import load_config  # noqa: E402
from copybot.hl.client import ApiError, FillsPageLimitError, InfoClient  # noqa: E402
from copybot.selection.analytics import (  # noqa: E402
    fill_metrics,
    joined_account_value,
    portfolio_metrics,
)
from copybot.selection.copyability import simulate_copy  # noqa: E402
from copybot.selection.refresh import Refresher  # noqa: E402
from copybot.state.fills_cache import fetch_fills  # noqa: E402

log = logging.getLogger("whatif")


def collect(data_dir: Path) -> list[dict[str, Any]]:
    cfg = load_config()
    s = cfg.selection
    prev = json.loads((data_dir / "candidates_latest.json").read_text())["candidates"]
    out: list[dict[str, Any]] = []
    with InfoClient() as client:
        r = Refresher(cfg, client, SystemClock(), data_dir)
        r.supported = client.meta_and_asset_ctxs().meta.perp_coins()
        for i, c in enumerate(prev):
            addr = c["address"]
            rec: dict[str, Any] = {
                "address": addr,
                "candidate": c["candidate"],
                "original_reason": c["reason"],
            }
            out.append(rec)
            try:
                portfolio = client.portfolio(addr)
                pm = portfolio_metrics(portfolio, r.now, s.lookback_days)
                rec["portfolio"] = asdict(pm)
                rec["subaccount_value_share"] = c.get("subaccount_value_share")
                if pm.pnl_90d <= 0 or c["reason"] in ("too_active", "trades_via_subaccounts"):
                    rec["skip"] = c["reason"] or "not_profitable_90d"
                    continue
                try:
                    fills, truncated = fetch_fills(
                        client, r.cache, addr, r.start, r.now, s.max_fill_pages
                    )
                except FillsPageLimitError:
                    rec["skip"] = "too_active"
                    continue
                if truncated:
                    rec["skip"] = "too_active"
                    continue
                av = joined_account_value(portfolio)
                fm = fill_metrics(
                    fills,
                    now_ms=r.now,
                    lookback_days=s.lookback_days,
                    supported_coins=r.supported,
                    account_value=av,
                )
                rec["fills"] = asdict(fm)
                rec["portfolio"]["max_drawdown_90d"] = max(
                    pm.max_drawdown_portfolio, fm.max_drawdown_fills
                )
                prices = {k: r._price(k) for k in fm.coins if k in r.supported}
                cr = simulate_copy(
                    fills,
                    prices={k: v for k, v in prices.items() if v},
                    trader_account_value=av,
                    cfg=cfg,
                    start_ms=r.start,
                    end_ms=r.now,
                    seed=addr,
                )
                ideal = simulate_copy(
                    fills,
                    prices={k: v for k, v in prices.items() if v},
                    trader_account_value=av,
                    cfg=cfg,
                    start_ms=r.start,
                    end_ms=r.now,
                    seed=addr,
                    ideal=True,
                )
                rec["copy"] = {
                    "ideal_return_90d": ideal.copy_return,
                    "lag_capture": (
                        cr.copy_return / ideal.copy_return if ideal.copy_return > 0 else 0.0
                    ),
                    "copy_return_90d": cr.copy_return,
                    "copy_max_drawdown": cr.copy_max_drawdown,
                    "copy_capture_ratio": cr.capture_ratio(pm.return_90d),
                    "n_orders": cr.n_orders,
                    "avg_lag_cost_bps": cr.avg_lag_cost_bps,
                    "skipped": dict(cr.skipped),
                }
            except ApiError as exc:
                rec["skip"] = f"api_error: {exc}"[:200]
            if (i + 1) % 10 == 0:
                log.info("whatif %d/%d weight %.0f", i + 1, len(prev), client.bucket.total_consumed)
    return out


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data"
    recs = collect(data_dir)
    (data_dir / "whatif_metrics.json").write_text(json.dumps(recs, indent=1, default=str))
    log.info("wrote %s", data_dir / "whatif_metrics.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
