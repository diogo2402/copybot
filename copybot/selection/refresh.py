"""`refresh` command: rebuild the trader shortlist (§6).

Per candidate, cheapest check first, stopping at the first exclusion (D2):
  portfolio (20) → subAccounts (20) → fills (≤ max_fill_pages) → copy sim (candles, shared
  across candidates) → userRole (60).
Writes <data>/shortlist.json and <data>/candidates_latest.json.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from copybot.clock import DAY_MS, HOUR_MS, Clock, ms_to_dt
from copybot.config import Config
from copybot.hl.client import ApiError, FillsPageLimitError, InfoClient
from copybot.hl.leaderboard import fetch_leaderboard
from copybot.selection.analytics import (
    FillMetrics,
    PortfolioMetrics,
    fill_metrics,
    joined_account_value,
    portfolio_metrics,
)
from copybot.selection.candidates import Candidate, stage1
from copybot.selection.copyability import CopyResult, PriceSeries, simulate_copy
from copybot.selection.scoring import (
    ScoreInputs,
    apply_hysteresis,
    composite_scores,
    rank,
    shuffled,
)
from copybot.state.fills_cache import FillsCache, fetch_fills
from copybot.state.logs import errors_log

log = logging.getLogger("copybot.refresh")


@dataclass
class Analysis:
    address: str
    candidate: dict[str, Any]
    status: str = "pending"  # eligible | excluded | error | skipped
    reason: str | None = None
    stage: str = "stage1"
    portfolio: dict[str, Any] | None = None
    fills: dict[str, Any] | None = None
    copy: dict[str, Any] | None = None
    subaccount_value_share: float | None = None
    role: str | None = None
    score: float | None = None
    rank: int | None = None
    _pm: PortfolioMetrics | None = field(default=None, repr=False)
    _fm: FillMetrics | None = field(default=None, repr=False)
    _cr: CopyResult | None = field(default=None, repr=False)

    def exclude(self, reason: str) -> Analysis:
        self.status, self.reason = "excluded", reason
        return self

    def public(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if not k.startswith("_")}


def _cand_dict(c: Candidate) -> dict[str, Any]:
    return {
        "display_name": c.display_name,
        "account_value": float(c.account_value),
        "month_pnl": float(c.month_pnl),
        "month_roi": float(c.month_roi),
        "month_vlm": float(c.month_vlm),
        "alltime_pnl": float(c.alltime_pnl),
        "turnover_ratio": float(c.turnover_ratio),
    }


class Refresher:
    def __init__(
        self,
        cfg: Config,
        client: InfoClient,
        clock: Clock,
        data_dir: Path,
        *,
        time_budget_s: float = 110 * 60,
        use_cache: bool = True,
    ) -> None:
        self.cfg = cfg
        self.sel = cfg.selection
        self.client = client
        self.clock = clock
        self.data_dir = data_dir
        self.cache = FillsCache(data_dir) if use_cache else None
        self.errors = errors_log(data_dir)
        self.deadline = time.monotonic() + time_budget_s
        self.now = clock.now_ms()
        self.start = self.now - self.sel.lookback_days * DAY_MS
        self._prices: dict[str, PriceSeries] = {}
        self.supported: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def _log_error(self, address: str, where: str, exc: Exception) -> None:
        rec: dict[str, Any] = {
            "ts": self.clock.now_ms(),
            "job": "refresh",
            "address": address,
            "where": where,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        }
        raw = getattr(exc, "raw", None)
        if raw:
            rec["raw"] = raw
        self.errors.append(rec)

    def _price(self, coin: str) -> PriceSeries:
        if coin not in self._prices:
            candles = self.client.candles(coin, "1h", self.start - HOUR_MS, self.now)
            self._prices[coin] = PriceSeries(candles)
        return self._prices[coin]

    # ------------------------------------------------------------------ per wallet

    def analyse(self, c: Candidate, *, full: bool = True) -> Analysis:
        """Run the stage-2/3 pipeline. `full=False` stops after the fill-based copyability
        checks used for the random control set (§6.7)."""
        a = Analysis(c.address, _cand_dict(c))
        s = self.sel
        try:
            a.stage = "portfolio"
            portfolio = self.client.portfolio(c.address)
            pm = portfolio_metrics(portfolio, self.now, s.lookback_days)
            a._pm = pm
            a.portfolio = asdict(pm) | {"return_to_drawdown": pm.return_to_drawdown}
            if full:
                if pm.pnl_90d <= 0:
                    return a.exclude("not_profitable_90d")
                if pm.max_drawdown_90d * 100 > s.max_drawdown_pct:
                    return a.exclude("drawdown_too_high")
                if pm.positive_weeks_ratio < s.min_positive_weeks_ratio:
                    return a.exclude("inconsistent_weeks")

            a.stage = "subaccounts"
            subs = self.client.sub_accounts(c.address)
            sub_value = sum(float(x.clearinghouseState.marginSummary.accountValue) for x in subs)
            master = self.client.clearinghouse_state(c.address)
            total = sub_value + float(master.marginSummary.accountValue)
            a.subaccount_value_share = sub_value / total if total > 0 else 0.0
            if a.subaccount_value_share > s.max_subaccount_value_share:
                return a.exclude("trades_via_subaccounts")

            a.stage = "fills"
            try:
                fills, truncated = fetch_fills(
                    self.client, self.cache, c.address, self.start, self.now, s.max_fill_pages
                )
            except FillsPageLimitError:
                return a.exclude("too_active")
            if truncated:
                return a.exclude("too_active")
            av_series = joined_account_value(portfolio)
            fm = fill_metrics(
                fills,
                now_ms=self.now,
                lookback_days=s.lookback_days,
                supported_coins=self.supported,
                account_value=av_series,
            )
            a._fm = fm
            a.fills = asdict(fm)
            # D4: use the worse of the portfolio- and fills-based drawdowns.
            pm.max_drawdown_90d = max(pm.max_drawdown_portfolio, fm.max_drawdown_fills)
            a.portfolio = asdict(pm) | {"return_to_drawdown": pm.return_to_drawdown}

            reason = self._fill_exclusion(fm, full)
            if reason:
                return a.exclude(reason)
            if not full:
                a.status = "eligible"
                return a
            if pm.max_drawdown_90d * 100 > s.max_drawdown_pct:
                return a.exclude("drawdown_too_high")

            a.stage = "copy_sim"
            prices = {coin: self._price(coin) for coin in fm.coins if coin in self.supported}
            prices = {k: v for k, v in prices.items() if v}
            cr = simulate_copy(
                fills,
                prices=prices,
                trader_account_value=av_series,
                cfg=self.cfg,
                start_ms=self.start,
                end_ms=self.now,
                seed=c.address,
            )
            a._cr = cr
            capture = cr.capture_ratio(pm.return_90d)
            a.copy = {
                "copy_return_90d": cr.copy_return,
                "copy_max_drawdown": cr.copy_max_drawdown,
                "copy_capture_ratio": capture,
                "n_orders": cr.n_orders,
                "fees": cr.fees,
                "avg_lag_cost_bps": cr.avg_lag_cost_bps,
                "skipped": dict(cr.skipped),
            }
            if cr.copy_return <= 0:
                return a.exclude("copy_unprofitable")
            if capture < s.min_copy_capture_ratio:
                return a.exclude("low_capture")

            a.stage = "role"
            a.role = self.client.user_role(c.address).role
            if a.role != "user":
                return a.exclude("not_a_user")
            a.status = "eligible"
            return a
        except ApiError as exc:
            self._log_error(c.address, a.stage, exc)
            a.status, a.reason = "error", f"api_error at {a.stage}"
            return a

    def _fill_exclusion(self, fm: FillMetrics, full: bool) -> str | None:
        s = self.sel
        if fm.trades_per_day > s.max_trades_per_day:
            return "too_fast"
        if fm.median_holding_hours is None:
            return "insufficient_trades"
        if fm.median_holding_hours < self.cfg.effective_min_holding_hours:
            return "holding_too_short"
        if not full:
            return None
        if fm.maker_ratio > s.max_maker_ratio:
            return "likely_market_maker"
        if fm.top_trade_concentration > s.max_top_trade_concentration:
            return "one_lucky_trade"
        if fm.active_days_last_30 < s.min_active_days_30:
            return "inactive"
        if fm.max_leverage > s.max_leverage_seen:
            return "leverage_too_high"
        if fm.n_trades < s.min_round_trips:
            return "insufficient_trades"
        return None

    # ------------------------------------------------------------------ whole refresh

    def out_of_time(self) -> bool:
        return time.monotonic() > self.deadline

    def run(self, *, pool_limit: int | None = None) -> dict[str, Any]:
        t0 = time.monotonic()
        s = self.sel
        prev = load_previous(self.data_dir)
        mac = self.client.meta_and_asset_ctxs()
        self.supported = mac.meta.perp_coins()
        rows = fetch_leaderboard(self.client)
        st1 = stage1(rows, s)
        pool = st1.pool[:pool_limit] if pool_limit else st1.pool
        log.info(
            "leaderboard rows=%d stage1 survivors=%d pool=%d",
            len(rows),
            len(st1.survivors),
            len(pool),
        )

        # Always (re)analyse currently followed wallets, even if they fell out of the pool.
        by_addr = {c.address: c for c in st1.survivors}
        prev_followed = [w["address"] for w in prev.get("followed", [])]
        order = list(pool) + [
            by_addr[a] for a in prev_followed if a in by_addr and by_addr[a] not in pool
        ]

        analyses: dict[str, Analysis] = {}
        for i, c in enumerate(order):
            if self.out_of_time():
                a = Analysis(c.address, _cand_dict(c), status="skipped", reason="time_budget")
            else:
                a = self.analyse(c)
            analyses[c.address] = a
            if (i + 1) % 10 == 0:
                log.info(
                    "analysed %d/%d, weight used %.0f",
                    i + 1,
                    len(order),
                    self.client.bucket.total_consumed,
                )

        eligible = [a for a in analyses.values() if a.status == "eligible"]
        scores = composite_scores(
            [
                ScoreInputs(
                    address=a.address,
                    copy_return=a._cr.copy_return if a._cr else 0.0,
                    return_to_drawdown=a._pm.return_to_drawdown if a._pm else 0.0,
                    positive_weeks=a._pm.positive_weeks_ratio if a._pm else 0.0,
                    capture=(a.copy or {}).get("copy_capture_ratio", 0.0),
                    avg_leverage=a._fm.avg_leverage if a._fm else 0.0,
                )
                for a in eligible
            ],
            s.weights,
        )
        ranked = rank(scores)
        for i, addr in enumerate(ranked):
            analyses[addr].score = scores[addr]
            analyses[addr].rank = i + 1

        reasons = {
            a.address: a.reason or a.status for a in analyses.values() if a.status == "excluded"
        }
        # Followed wallets dropped off the leaderboard entirely are excluded by stage 1.
        for addr in prev_followed:
            if addr not in analyses:
                reasons[addr] = "failed stage-1 leaderboard filter"
        unknown = {a.address for a in analyses.values() if a.status in ("error", "skipped")}
        followed, changes = apply_hysteresis(
            ranked,
            prev_followed,
            n=s.n_follow,
            keep_within=s.keep_if_rank_within,
            max_additions=s.max_replacements_per_day,
            exclusion_reasons=reasons,
            unknown=unknown,
        )

        random_control = self._random_control(prev, st1.survivors, analyses)

        today = ms_to_dt(self.now).date().isoformat()
        prev_first = {w["address"]: w.get("first_followed") for w in prev.get("followed", [])}
        change_by_addr = {ch.address: ch for ch in changes}
        followed_out = []
        for addr in followed:
            fa = analyses.get(addr)
            ch = change_by_addr.get(addr)
            followed_out.append(
                {
                    "address": addr,
                    "rank": fa.rank if fa else None,
                    "score": fa.score if fa else None,
                    "first_followed": prev_first.get(addr) or today,
                    "change": ch.action if ch else "kept",
                    "change_reason": ch.reason if ch else "",
                    "analysis": fa.public() if fa else None,
                }
            )

        status_counts = Counter(a.status for a in analyses.values())
        excluded_by = Counter(a.reason for a in analyses.values() if a.status == "excluded")
        stats = {
            "leaderboard_rows": len(rows),
            "stage1_survivors": len(st1.survivors),
            "stage1_rejected": dict(st1.rejected.most_common()),
            "pool": len(pool),
            "analysed": len(analyses),
            "status": dict(status_counts),
            "excluded_by_reason": dict(excluded_by.most_common()),
            "eligible": len(eligible),
            "weight_used": round(self.client.bucket.total_consumed),
            "requests": self.client.request_count,
            "duration_s": round(time.monotonic() - t0, 1),
        }
        shortlist = {
            "schema_version": 1,
            "generated_at": ms_to_dt(self.now).isoformat(),
            "generated_at_ms": self.now,
            "n_follow": s.n_follow,
            "poll_minutes": self.cfg.schedule.poll_minutes,
            "followed": followed_out,
            "changes": [asdict(ch) for ch in changes],
            "random_control": random_control,
            "stats": stats,
        }
        if not followed:
            log.warning("no qualifying traders; holding cash (alert: no_qualifying_traders)")
        candidates = {
            "generated_at": shortlist["generated_at"],
            "stats": stats,
            "candidates": sorted(
                (a.public() for a in analyses.values()),
                key=lambda d: (d["rank"] is None, d["rank"] or 0, d["address"]),
            ),
        }
        write_json(self.data_dir / "shortlist.json", shortlist)
        write_json(self.data_dir / "candidates_latest.json", candidates)
        log.info("refresh done: followed=%s stats=%s", followed, json.dumps(stats))
        return shortlist

    def _random_control(
        self, prev: dict[str, Any], survivors: list[Candidate], analyses: dict[str, Analysis]
    ) -> dict[str, Any]:
        """§6.7: N random stage-1 survivors passing the holding-time and trades-per-day
        exclusions. Drawn once per calendar month with a month-derived seed."""
        month = ms_to_dt(self.now).strftime("%Y-%m")
        old = prev.get("random_control") or {}
        if old.get("month") == month and old.get("wallets"):
            return dict(old)
        chosen: list[str] = []
        tried = 0
        by_addr = {c.address: c for c in survivors}
        for addr in shuffled([c.address for c in survivors], month):
            if len(chosen) >= self.sel.n_follow or self.out_of_time() or tried >= 60:
                break
            tried += 1
            prior = analyses.get(addr)
            if prior is not None and prior._fm is not None:
                ok = self._fill_exclusion(prior._fm, full=False) is None and (
                    (prior.subaccount_value_share or 0) <= self.sel.max_subaccount_value_share
                )
            else:
                ok = self.analyse(by_addr[addr], full=False).status == "eligible"
            if ok:
                chosen.append(addr)
        return {
            "month": month,
            "drawn_at": ms_to_dt(self.now).isoformat(),
            "wallets": chosen,
            "tried": tried,
        }


def load_previous(data_dir: Path) -> dict[str, Any]:
    p = data_dir / "shortlist.json"
    if not p.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
        return data
    except (OSError, ValueError):
        return {}


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)
