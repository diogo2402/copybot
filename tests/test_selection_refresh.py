import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from copybot.clock import DAY_MS, HOUR_MS, FixedClock, ms_to_dt
from copybot.config import Config
from copybot.hl.client import ApiError, FillsPageLimitError
from copybot.hl.models import (
    ClearinghouseState,
    Leaderboard,
    LeaderboardRow,
    MetaAndAssetCtxs,
    Portfolio,
    SubAccount,
    UserRole,
)
from copybot.selection.refresh import Refresher
from tests.factories import NOW, candles, portfolio, steady_portfolio, trip


def chs(av: float) -> ClearinghouseState:
    return ClearinghouseState.model_validate(
        {
            "assetPositions": [],
            "marginSummary": {
                "accountValue": str(av),
                "totalNtlPos": "0",
                "totalRawUsd": "0",
                "totalMarginUsed": "0",
            },
            "time": NOW,
        }
    )


TRIP_STARTS = [NOW - (60 - 2 * i) * DAY_MS for i in range(30)]


def good_fills() -> list[Any]:
    """30 profitable 10h BTC long trips over the last 60 days, 10% exposure on 1M."""
    out = []
    for i in range(30):
        t = TRIP_STARTS[i]
        out += trip("BTC", t_open=t, hours=10, size=1000, px_open=100, px_close=102)
    return out


def rising(ts: int) -> float:
    """Price climbs 100 -> 102 during each 10h trip (matching the trader's fills), else 100."""
    for t0 in TRIP_STARTS:
        if t0 <= ts <= t0 + 10 * HOUR_MS:
            return 100 + 2 * (ts - t0) / (10 * HOUR_MS)
    return 100.0


class FakeClient:
    def __init__(self, wallets: dict[str, dict[str, Any]]) -> None:
        self.wallets = wallets
        self.calls: Counter[tuple[str, str]] = Counter()
        self.bucket = type("B", (), {"total_consumed": 0.0})()
        self.request_count = 0

    def _w(self, kind: str, addr: str) -> dict[str, Any]:
        self.calls[(kind, addr)] += 1
        w = self.wallets[addr]
        if w.get("fail") == kind:
            raise ApiError("boom")
        return w

    def meta_and_asset_ctxs(self) -> MetaAndAssetCtxs:
        ctx = {"funding": "0", "openInterest": "0", "oraclePx": "100", "markPx": "100"}
        return MetaAndAssetCtxs.model_validate(
            {
                "meta": {"universe": [{"name": "BTC", "szDecimals": 3, "maxLeverage": 40}]},
                "ctxs": [ctx],
            }
        )

    def leaderboard(self) -> Leaderboard:
        rows = [w["row"] for w in self.wallets.values()]
        return Leaderboard(leaderboardRows=rows)

    def portfolio(self, a: str) -> Portfolio:
        return self._w("portfolio", a)["portfolio"]  # type: ignore[no-any-return]

    def sub_accounts(self, a: str) -> list[SubAccount]:
        w = self._w("sub_accounts", a)
        return (
            [
                SubAccount(
                    name="s",
                    subAccountUser="0xsub",
                    master=a,
                    clearinghouseState=chs(w.get("sub_value", 0)),
                )
            ]
            if w.get("sub_value")
            else []
        )

    def clearinghouse_state(self, a: str) -> ClearinghouseState:
        return chs(self._w("clearinghouse_state", a).get("master_value", 1_000_000))

    def user_fills_by_time(self, a: str, start: int, end: int, max_pages: int = 10) -> Any:
        w = self._w("fills", a)
        if w.get("too_active"):
            raise FillsPageLimitError("too many")
        return [f for f in w["fills"] if start <= f.time <= end]

    def candles(self, coin: str, interval: str, start: int, end: int) -> Any:
        self.calls[("candles", coin)] += 1
        return candles(coin, start, end + HOUR_MS, self.price)

    def user_role(self, a: str) -> UserRole:
        return UserRole(role=self._w("user_role", a).get("role", "user"))

    price = staticmethod(rising)


def row(addr: str, pnl: float = 100_000) -> LeaderboardRow:
    from tests.factories import lb_row

    return lb_row(addr, av=1_000_000, month_pnl=pnl, month_vlm=50_000_000)


def wallet(addr: str, **kw: Any) -> dict[str, Any]:
    w: dict[str, Any] = {
        "row": row(addr, kw.pop("pnl", 100_000)),
        "portfolio": steady_portfolio(days=120, daily_pnl=500),
        "fills": good_fills(),
    }
    w.update(kw)
    return w


def run(tmp_path: Path, wallets: dict[str, dict[str, Any]], cfg: Config | None = None):  # type: ignore[no-untyped-def]
    client = FakeClient(wallets)
    r = Refresher(cfg or Config(), client, FixedClock(NOW), tmp_path)  # type: ignore[arg-type]
    return r.run(), client


def test_end_to_end_outputs_and_early_exits(tmp_path: Path) -> None:
    pts = list(range(NOW - 120 * DAY_MS, NOW + 1, DAY_MS))
    crash = [(t, 0.0 if t < NOW - 40 * DAY_MS else -600_000.0) for t in pts]
    wallets = {
        "0xgood": wallet("0xgood"),
        "0xdd": wallet("0xdd", portfolio=portfolio([(t, 1_000_000 + v) for t, v in crash], crash)),
        "0xsubs": wallet("0xsubs", sub_value=5_000_000, master_value=10_000),
        "0xfast": wallet("0xfast", too_active=True),
        "0xvault": wallet("0xvault", role="vault"),
    }
    shortlist, client = run(tmp_path, wallets)
    by = {
        c["address"]: c
        for c in json.loads((tmp_path / "candidates_latest.json").read_text())["candidates"]
    }
    assert by["0xdd"]["reason"] == "not_profitable_90d"
    assert by["0xsubs"]["reason"] == "trades_via_subaccounts"
    assert by["0xfast"]["reason"] == "too_active"
    assert by["0xvault"]["reason"] == "not_a_user"
    assert by["0xgood"]["status"] == "eligible", by["0xgood"]
    assert [w["address"] for w in shortlist["followed"]] == ["0xgood"]
    assert shortlist["followed"][0]["first_followed"] == ms_to_dt(NOW).date().isoformat()

    # cheapest-first: excluded wallets never reach the expensive calls. (The random control
    # draw may still read 0xdd's fills: §6.7 applies only the copyability rules there.)
    assert client.calls[("user_role", "0xdd")] == 0
    assert by["0xdd"]["fills"] is None
    assert client.calls[("fills", "0xsubs")] == 0
    assert client.calls[("user_role", "0xfast")] == 0
    assert client.calls[("candles", "BTC")] == 1  # shared across candidates
    assert (tmp_path / "shortlist.json").exists()


def test_followed_wallet_kept_on_api_error(tmp_path: Path) -> None:
    run(tmp_path, {"0xgood": wallet("0xgood")})
    shortlist, _ = run(tmp_path, {"0xgood": wallet("0xgood", fail="portfolio")})
    assert [w["address"] for w in shortlist["followed"]] == ["0xgood"]
    assert shortlist["followed"][0]["change"] == "kept"
    errors = (tmp_path / "logs" / "errors.jsonl").read_text().splitlines()
    assert json.loads(errors[-1])["where"] == "portfolio"


def test_first_followed_date_preserved(tmp_path: Path) -> None:
    s1, _ = run(tmp_path, {"0xgood": wallet("0xgood")})
    first = s1["followed"][0]["first_followed"]
    s2, _ = run(tmp_path, {"0xgood": wallet("0xgood")})
    assert s2["followed"][0]["first_followed"] == first
    assert s2["followed"][0]["change"] == "kept"


def test_followed_wallet_dropped_when_excluded(tmp_path: Path) -> None:
    run(tmp_path, {"0xgood": wallet("0xgood")})
    shortlist, _ = run(tmp_path, {"0xgood": wallet("0xgood", role="vault")})
    assert shortlist["followed"] == []
    ch = next(c for c in shortlist["changes"] if c["address"] == "0xgood")
    assert ch["action"] == "dropped" and "not_a_user" in ch["reason"]


def test_random_control_drawn_and_kept_for_month(tmp_path: Path) -> None:
    s1, _ = run(tmp_path, {"0xgood": wallet("0xgood")})
    rc = s1["random_control"]
    assert rc["month"] == "2026-09" and rc["wallets"] == ["0xgood"]
    s2, _ = run(tmp_path, {"0xgood": wallet("0xgood")})
    assert s2["random_control"] == rc


def test_time_budget_marks_skipped(tmp_path: Path) -> None:
    client = FakeClient({"0xgood": wallet("0xgood")})
    r = Refresher(Config(), client, FixedClock(NOW), tmp_path, time_budget_s=-1)  # type: ignore[arg-type]
    shortlist = r.run()
    assert shortlist["stats"]["status"] == {"skipped": 1}
    assert shortlist["followed"] == []


@pytest.mark.parametrize("n", [3])
def test_stats_shape(tmp_path: Path, n: int) -> None:
    shortlist, _ = run(tmp_path, {f"0x{i}": wallet(f"0x{i}", pnl=1000 + i) for i in range(n)})
    st = shortlist["stats"]
    assert st["pool"] == n and st["analysed"] == n
    assert st["eligible"] == n
    assert [w["rank"] for w in shortlist["followed"]] == [1, 2, 3]
