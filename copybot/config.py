"""Typed configuration loaded from config.yaml. Loaded and validated once per process."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScheduleConfig(_Strict):
    poll_minutes: int = Field(5, ge=1, le=240)


class PortfolioConfig(_Strict):
    start_equity: Decimal = Field(Decimal(10000), gt=0)
    copy_multiplier: Decimal = Field(Decimal("1.0"), gt=0)


class WeightsConfig(_Strict):
    copy_return: float = 0.35
    return_to_drawdown: float = 0.25
    positive_weeks: float = 0.20
    capture: float = 0.10
    low_leverage: float = 0.10


class SelectionConfig(_Strict):
    n_follow: int = Field(5, ge=1)
    stage2_pool: int = Field(150, ge=1)
    lookback_days: int = Field(90, ge=7)
    min_account_value: Decimal = Decimal(100000)
    min_month_volume: Decimal = Decimal(1000000)
    max_turnover_ratio: Decimal = Decimal(200)
    max_trades_per_day: float = 30
    min_median_holding_hours: float = 4
    max_maker_ratio: float = 0.6
    max_drawdown_pct: float = 40
    max_top_trade_concentration: float = 0.5
    min_positive_weeks_ratio: float = 0.5
    min_active_days_30: int = 8
    max_leverage_seen: float = 25
    min_round_trips: int = 20
    max_subaccount_value_share: float = Field(0.5, ge=0, le=1)
    max_fill_pages: int = Field(5, ge=1)
    min_copy_capture_ratio: float = 0.4
    weights: WeightsConfig = WeightsConfig()
    keep_if_rank_within: int = Field(10, ge=1)
    max_replacements_per_day: int = Field(2, ge=0)
    blocklist: list[str] = Field(default_factory=list)


class SignalsConfig(_Strict):
    rebalance_threshold_pct: Decimal = Decimal(10)
    rebalance_threshold_usd: Decimal = Decimal(15)
    max_entry_drift_pct: Decimal = Decimal("1.5")
    disappear_confirm_polls: int = Field(1, ge=0)


class RiskConfig(_Strict):
    max_position_pct: Decimal = Decimal(20)
    max_gross_leverage: Decimal = Decimal(3)
    max_coin_pct: Decimal = Decimal(35)
    max_position_leverage: Decimal = Decimal(5)
    daily_loss_stop_pct: Decimal = Decimal(5)
    max_drawdown_stop_pct: Decimal = Decimal(25)


class CostsConfig(_Strict):
    taker_fee_bps: Decimal = Decimal("4.5")
    extra_slippage_bps: Decimal = Decimal(2)
    fallback_slippage_bps: Decimal = Decimal(10)
    backtest_slippage_bps: Decimal = Decimal(8)


class EmailConfig(_Strict):
    max_alerts_per_type_per_hour: int = 1
    max_alerts_per_day: int = 6
    timezone_display: str = "Europe/Lisbon"


class LiveConfig(_Strict):
    enabled: bool = False
    network: Literal["testnet", "mainnet"] | None = None
    mainnet_confirmed: bool = False
    require_readiness: bool = True
    override_acknowledged: str = ""
    max_account_equity_usd: Decimal = Decimal(500)
    capital_fraction: Decimal = Field(Decimal("1.0"), gt=0, le=1)
    max_leverage: Decimal = Decimal(3)
    margin_mode: Literal["isolated", "cross"] = "isolated"
    max_slippage_bps: Decimal = Decimal(20)
    max_order_usd: Decimal = Decimal(100)
    max_total_exposure_usd: Decimal = Decimal(1000)
    max_orders_per_poll: int = 10
    daily_loss_stop_usd: Decimal = Decimal(50)


class Config(_Strict):
    schedule: ScheduleConfig = ScheduleConfig()
    portfolio: PortfolioConfig = PortfolioConfig()
    selection: SelectionConfig = SelectionConfig()
    signals: SignalsConfig = SignalsConfig()
    risk: RiskConfig = RiskConfig()
    costs: CostsConfig = CostsConfig()
    email: EmailConfig = EmailConfig()
    live: LiveConfig = LiveConfig()

    @model_validator(mode="after")
    def _check(self) -> Config:
        if self.selection.keep_if_rank_within < self.selection.n_follow:
            raise ValueError("selection.keep_if_rank_within must be >= selection.n_follow")
        return self

    @property
    def effective_min_holding_hours(self) -> float:
        """§6.3: holding time must be far longer than our polling gap."""
        return max(self.selection.min_median_holding_hours, 12 * self.schedule.poll_minutes / 60)


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> Config:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    return Config.model_validate(raw)
