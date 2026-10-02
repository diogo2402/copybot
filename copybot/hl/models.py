"""Pydantic models of the Hyperliquid responses we use. Shapes verified by scripts/probe_api.py;
see docs/API_NOTES.md.

Numbers arrive as strings and are parsed straight to Decimal; a float in a money field is a
schema error (it would mean the API changed and silently losing precision is not acceptable).
Unknown extra fields are ignored so additive API changes don't break us; missing or retyped
required fields raise.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, RootModel


def _to_decimal(v: Any) -> Decimal:
    if isinstance(v, Decimal):
        return v
    if isinstance(v, bool | float):
        raise ValueError(f"expected a decimal string, got {type(v).__name__}")
    if isinstance(v, int | str):
        try:
            d = Decimal(v)
        except InvalidOperation as exc:
            raise ValueError(f"not a decimal: {v!r}") from exc
        if not d.is_finite():
            raise ValueError(f"non-finite decimal: {v!r}")
        return d
    raise ValueError(f"expected a decimal string, got {type(v).__name__}")


Dec = Annotated[Decimal, BeforeValidator(_to_decimal)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# ---------- meta / metaAndAssetCtxs ----------


class AssetMeta(_Model):
    name: str
    szDecimals: int
    maxLeverage: int
    marginTableId: int | None = None
    onlyIsolated: bool = False
    isDelisted: bool = False


class MarginTier(_Model):
    lowerBound: Dec
    maxLeverage: int


class MarginTable(_Model):
    description: str = ""
    marginTiers: list[MarginTier]


class Meta(_Model):
    universe: list[AssetMeta]
    # Raw form is [[id, {...}], ...]; exposed as a dict by `margin_tables`.
    marginTables: list[tuple[int, MarginTable]] = Field(default_factory=list)

    def margin_tables(self) -> dict[int, MarginTable]:
        return dict(self.marginTables)

    def perp_coins(self, include_delisted: bool = False) -> set[str]:
        return {a.name for a in self.universe if include_delisted or not a.isDelisted}

    def asset(self, coin: str) -> AssetMeta | None:
        for a in self.universe:
            if a.name == coin:
                return a
        return None


class AssetCtx(_Model):
    funding: Dec
    openInterest: Dec
    oraclePx: Dec
    markPx: Dec
    midPx: Dec | None = None  # null for illiquid/delisted assets
    prevDayPx: Dec | None = None
    dayNtlVlm: Dec | None = None
    premium: Dec | None = None


class MetaAndAssetCtxs(_Model):
    meta: Meta
    ctxs: list[AssetCtx]

    def by_coin(self) -> dict[str, tuple[AssetMeta, AssetCtx]]:
        return {m.name: (m, c) for m, c in zip(self.meta.universe, self.ctxs, strict=True)}


# ---------- allMids ----------


class AllMids(RootModel[dict[str, Dec]]):
    """coin -> mid. Also contains spot (`@n`, `PURR/USDC`) and other (`#n`) markets;
    filter with Meta.perp_coins() before use."""

    def perp_only(self, perp_coins: set[str]) -> dict[str, Decimal]:
        return {k: v for k, v in self.root.items() if k in perp_coins}


# ---------- l2Book ----------


class L2Level(_Model):
    px: Dec
    sz: Dec
    n: int


class L2Book(_Model):
    coin: str
    time: int
    levels: tuple[list[L2Level], list[L2Level]]  # (bids desc, asks asc)

    @property
    def bids(self) -> list[L2Level]:
        return self.levels[0]

    @property
    def asks(self) -> list[L2Level]:
        return self.levels[1]


# ---------- clearinghouseState ----------


class Leverage(_Model):
    type: Literal["cross", "isolated"]
    value: int
    rawUsd: Dec | None = None


class CumFunding(_Model):
    allTime: Dec
    sinceOpen: Dec
    sinceChange: Dec


class Position(_Model):
    coin: str
    szi: Dec  # signed size; negative = short
    entryPx: Dec | None = None
    leverage: Leverage
    liquidationPx: Dec | None = None
    positionValue: Dec
    marginUsed: Dec
    unrealizedPnl: Dec
    returnOnEquity: Dec | None = None
    maxLeverage: int | None = None
    cumFunding: CumFunding | None = None


class AssetPosition(_Model):
    type: str
    position: Position


class MarginSummary(_Model):
    accountValue: Dec
    totalNtlPos: Dec
    totalRawUsd: Dec
    totalMarginUsed: Dec


class ClearinghouseState(_Model):
    assetPositions: list[AssetPosition]
    marginSummary: MarginSummary
    crossMarginSummary: MarginSummary | None = None
    crossMaintenanceMarginUsed: Dec | None = None
    withdrawable: Dec | None = None
    time: int

    def positions(self) -> dict[str, Position]:
        return {ap.position.coin: ap.position for ap in self.assetPositions}


# ---------- userFillsByTime ----------


class Fill(_Model):
    coin: str
    px: Dec
    sz: Dec
    side: Literal["A", "B"]  # A = sell (ask side), B = buy (bid side)
    time: int
    startPosition: Dec
    dir: str
    closedPnl: Dec
    hash: str
    oid: int
    tid: int
    crossed: bool
    fee: Dec
    feeToken: str = "USDC"
    builderFee: Dec | None = None
    twapId: int | None = None

    def chrono_key(self) -> tuple[int, str, Decimal, int]:
        """Execution order. One order filling against many resting orders yields several fills
        with the same millisecond `time`, and their `tid` order does NOT follow execution. The
        position chain does: sells walk `startPosition` down, buys walk it up."""
        chain = -self.startPosition if self.side == "A" else self.startPosition
        return (self.time, self.coin, chain, self.tid)

    @property
    def signed_sz(self) -> Decimal:
        return self.sz if self.side == "B" else -self.sz

    @property
    def is_perp(self) -> bool:
        # Spot fills use "@<index>" or "BASE/QUOTE"; outcome markets use "#<index>".
        return not (self.coin.startswith(("@", "#")) or "/" in self.coin)


class Fills(RootModel[list[Fill]]):
    pass


# ---------- portfolio ----------

PortfolioWindow = Literal[
    "day", "week", "month", "allTime", "perpDay", "perpWeek", "perpMonth", "perpAllTime"
]


class PortfolioSeries(_Model):
    accountValueHistory: list[tuple[int, Dec]]
    pnlHistory: list[tuple[int, Dec]]
    vlm: Dec


class Portfolio(RootModel[list[tuple[str, PortfolioSeries]]]):
    def window(self, name: PortfolioWindow) -> PortfolioSeries | None:
        for k, v in self.root:
            if k == name:
                return v
        return None


# ---------- userRole ----------


class UserRole(_Model):
    role: Literal["missing", "user", "agent", "vault", "subAccount"]
    data: dict[str, Any] | None = None


# ---------- subAccounts ----------


class SubAccount(_Model):
    name: str
    subAccountUser: str
    master: str
    clearinghouseState: ClearinghouseState


class SubAccounts(RootModel[list[SubAccount] | None]):
    """null when the user has no sub-accounts."""

    @property
    def items(self) -> list[SubAccount]:
        return self.root or []


# ---------- fundingHistory ----------


class FundingRecord(_Model):
    coin: str
    fundingRate: Dec
    premium: Dec
    time: int


class FundingHistory(RootModel[list[FundingRecord]]):
    pass


# ---------- candleSnapshot ----------


class Candle(_Model):
    t: int  # open time ms
    T: int  # close time ms
    s: str
    i: str
    o: Dec
    c: Dec
    h: Dec
    l: Dec  # noqa: E741  (API field name)
    v: Dec
    n: int


class Candles(RootModel[list[Candle]]):
    pass


# ---------- leaderboard (stats-data, undocumented) ----------


class WindowPerf(_Model):
    pnl: Dec
    roi: Dec
    vlm: Dec


class LeaderboardRow(_Model):
    ethAddress: str
    accountValue: Dec
    displayName: str | None = None
    windowPerformances: list[tuple[str, WindowPerf]]
    prize: int | None = None

    def perf(self, window: Literal["day", "week", "month", "allTime"]) -> WindowPerf | None:
        for k, v in self.windowPerformances:
            if k == window:
                return v
        return None


class Leaderboard(_Model):
    leaderboardRows: list[LeaderboardRow]
