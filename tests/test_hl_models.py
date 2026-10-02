"""Recorded real responses must parse; malformed ones must not."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from copybot.hl.models import (
    AllMids,
    ClearinghouseState,
    Fill,
    Fills,
    FundingHistory,
    L2Book,
    Leaderboard,
    Meta,
    MetaAndAssetCtxs,
    Portfolio,
    SubAccounts,
    UserRole,
)

FIX = Path(__file__).parent / "fixtures"


def load(name: str) -> object:
    return json.loads((FIX / name).read_text())


def test_meta_fixture() -> None:
    m = Meta.model_validate(load("meta.json"))
    btc = m.asset("BTC")
    assert btc is not None and btc.szDecimals >= 0 and btc.maxLeverage > 1
    assert "BTC" in m.perp_coins()
    assert all(not a.isDelisted for a in m.universe if a.name in m.perp_coins())
    assert m.margin_tables()  # tiered margin tables present


def test_meta_and_asset_ctxs_alignment() -> None:
    raw = load("metaAndAssetCtxs.json")
    assert isinstance(raw, list)
    mac = MetaAndAssetCtxs.model_validate({"meta": raw[0], "ctxs": raw[1]})
    _, ctx = mac.by_coin()["BTC"]
    assert ctx.markPx > 0 and isinstance(ctx.funding, Decimal)


def test_all_mids_filters_to_perps() -> None:
    mids = AllMids.model_validate(load("allMids.json"))
    perps = Meta.model_validate(load("meta.json")).perp_coins()
    only = mids.perp_only(perps)
    assert "BTC" in only
    assert not any(k.startswith(("@", "#")) for k in only)


def test_l2book_sorted_sides() -> None:
    book = L2Book.model_validate(load("l2Book_BTC.json"))
    assert book.bids[0].px < book.asks[0].px
    assert [lvl.px for lvl in book.bids] == sorted((lvl.px for lvl in book.bids), reverse=True)
    assert [lvl.px for lvl in book.asks] == sorted(lvl.px for lvl in book.asks)


def test_clearinghouse_state() -> None:
    s = ClearinghouseState.model_validate(load("clearinghouseState.json"))
    assert s.marginSummary.accountValue > 0
    for coin, p in s.positions().items():
        assert p.coin == coin and p.szi != 0


def test_fills_fixture() -> None:
    fills = Fills.model_validate(load("userFillsByTime.json")).root
    assert fills
    for f in fills:
        assert f.sz > 0 and f.px > 0
        assert f.signed_sz == (f.sz if f.side == "B" else -f.sz)
    assert len({f.tid for f in fills}) == len(fills)


def test_portfolio_windows() -> None:
    p = Portfolio.model_validate(load("portfolio.json"))
    month = p.window("month")
    assert month is not None and month.accountValueHistory
    assert p.window("allTime") is not None


def test_user_role_and_vault() -> None:
    assert UserRole.model_validate(load("userRole.json")).role == "user"
    assert UserRole.model_validate(load("userRole_vault.json")).role == "vault"


def test_funding_history() -> None:
    recs = FundingHistory.model_validate(load("fundingHistory_BTC.json")).root
    assert recs and all(r.coin == "BTC" for r in recs)


def test_leaderboard_sample() -> None:
    lb = Leaderboard.model_validate(load("leaderboard_sample.json"))
    row = lb.leaderboardRows[0]
    assert row.ethAddress.startswith("0x")
    assert row.perf("month") is not None


def test_sub_accounts_fixture_and_null() -> None:
    subs = SubAccounts.model_validate(load("subAccounts.json")).items
    assert subs and subs[0].master.startswith("0x")
    assert SubAccounts.model_validate(None).items == []


FILL = {
    "coin": "BTC",
    "px": "100.5",
    "sz": "0.1",
    "side": "B",
    "time": 1,
    "startPosition": "0",
    "dir": "Open Long",
    "closedPnl": "0",
    "hash": "0x",
    "oid": 1,
    "tid": 1,
    "crossed": True,
    "fee": "0.01",
}


def test_float_money_rejected() -> None:
    with pytest.raises(ValidationError):
        Fill.model_validate({**FILL, "px": 100.5})


def test_missing_required_field_rejected() -> None:
    bad = dict(FILL)
    del bad["startPosition"]
    with pytest.raises(ValidationError):
        Fill.model_validate(bad)


def test_non_finite_rejected() -> None:
    with pytest.raises(ValidationError):
        Fill.model_validate({**FILL, "px": "NaN"})


def test_decimal_exact() -> None:
    f = Fill.model_validate({**FILL, "px": "0.1"})
    assert f.px == Decimal("0.1")


@pytest.mark.parametrize(
    "coin,perp", [("BTC", True), ("@107", False), ("PURR/USDC", False), ("#14720", False)]
)
def test_is_perp(coin: str, perp: bool) -> None:
    assert Fill.model_validate({**FILL, "coin": coin}).is_perp is perp
