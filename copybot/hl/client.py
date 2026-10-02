"""Thin Hyperliquid Info API client.

- One shared httpx.Client, 10 s timeout.
- Retries (3 attempts, exponential backoff + jitter) on timeouts, transport errors, 429 and 5xx.
  Never retries other 4xx.
- Client-side token bucket at <= 50% of the documented per-IP weight budget (runners share IPs).
- Every response is validated into a pydantic model; failure raises ApiSchemaError carrying the
  raw payload truncated to 5 KB so the caller can log it and abort the cycle safely.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, RootModel, ValidationError

from copybot import __version__
from copybot.hl.models import (
    AllMids,
    ClearinghouseState,
    Fill,
    Fills,
    FundingHistory,
    FundingRecord,
    L2Book,
    Leaderboard,
    Meta,
    MetaAndAssetCtxs,
    Portfolio,
    SubAccount,
    SubAccounts,
    UserRole,
)

MAINNET_API = "https://api.hyperliquid.xyz"
TESTNET_API = "https://api.hyperliquid-testnet.xyz"
LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"

# Documented per-IP REST budget (weight/minute) and info request weights. See docs/API_NOTES.md.
DOCUMENTED_BUDGET_PER_MIN = 1200
BUDGET_FRACTION = 0.5
LIGHT_TYPES = {"l2Book", "allMids", "clearinghouseState", "orderStatus", "spotClearinghouseState"}
HEAVY_TYPES = {"userRole": 60}
DEFAULT_WEIGHT = 20
ITEMS_PER_EXTRA_WEIGHT = 20  # userFills*/fundingHistory: +1 weight per 20 items returned
ITEM_WEIGHTED_TYPES = {"userFills", "userFillsByTime", "fundingHistory"}

FILLS_PAGE_CAP = 2000  # max fills per userFillsByTime response
# Docs say only the 10k most recent fills are queryable; the probe observed ~39k (≈3.8 days for a
# very active sub-account). Either way, deep history of very active wallets is unavailable.
FILLS_HISTORY_CAP = 10000

RAW_LOG_LIMIT = 5 * 1024

M = TypeVar("M", bound=BaseModel)


class ApiError(Exception):
    """Network/HTTP failure after retries.

    Callers must treat this as 'no data', never as 'no position'."""


class FillsPageLimitError(ApiError):
    """More fill pages than allowed. Not a failure of the API: the wallet trades too much to
    analyse cheaply (and is far too fast to copy). Selection classifies it as too active."""


class ApiSchemaError(ApiError):
    """Response did not match the expected model."""

    def __init__(self, message: str, raw: str) -> None:
        super().__init__(message)
        self.raw = raw[:RAW_LOG_LIMIT]


def request_weight(req_type: str) -> int:
    if req_type in LIGHT_TYPES:
        return 2
    return HEAVY_TYPES.get(req_type, DEFAULT_WEIGHT)


class TokenBucket:
    """Weight-based token bucket. capacity == per-minute budget; refills continuously."""

    def __init__(
        self,
        per_minute: float,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.capacity = per_minute
        self.rate = per_minute / 60.0
        self._tokens = per_minute
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()
        self._lock = threading.Lock()
        self.total_consumed = 0.0

    def _refill(self) -> None:
        now = self._monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, weight: float) -> None:
        weight = min(weight, self.capacity)
        with self._lock:
            while True:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    self.total_consumed += weight
                    return
                self._sleep((weight - self._tokens) / self.rate)

    def debit(self, weight: float) -> None:
        """Charge weight discovered after the fact (per-item weights). May go negative."""
        with self._lock:
            self._refill()
            self._tokens -= weight
            self.total_consumed += weight


class InfoClient:
    def __init__(
        self,
        base_url: str = MAINNET_API,
        *,
        http: httpx.Client | None = None,
        bucket: TokenBucket | None = None,
        max_attempts: int = 3,
        backoff_base_s: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.Client(
            timeout=10.0, headers={"User-Agent": f"copybot/{__version__}"}
        )
        self.bucket = bucket or TokenBucket(DOCUMENTED_BUDGET_PER_MIN * BUDGET_FRACTION)
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self._sleep = sleep
        self.request_count = 0

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> InfoClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- transport ----

    def _send(self, method: str, url: str, body: dict[str, Any] | None) -> str:
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            if attempt:
                delay = self.backoff_base_s * (2 ** (attempt - 1))
                self._sleep(delay + random.uniform(0, delay))
            try:
                self.request_count += 1
                resp = self._http.request(method, url, json=body)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_exc = ApiError(f"HTTP {resp.status_code} from {url}")
                continue
            if resp.status_code >= 400:
                raise ApiError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
            return resp.text
        raise ApiError(f"{method} {url} failed after {self.max_attempts} attempts: {last_exc}")

    def info_raw(self, body: dict[str, Any]) -> str:
        req_type = str(body["type"])
        self.bucket.acquire(request_weight(req_type))
        return self._send("POST", f"{self.base_url}/info", body)

    def _parse(self, model: type[M], raw: str, what: str) -> M:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiSchemaError(f"{what}: invalid JSON: {exc}", raw) from exc
        try:
            return model.model_validate(data)
        except ValidationError as exc:
            raise ApiSchemaError(f"{what}: schema mismatch: {exc}", raw) from exc

    def _info(self, model: type[M], body: dict[str, Any]) -> M:
        raw = self.info_raw(body)
        parsed = self._parse(model, raw, str(body["type"]))
        if body["type"] in ITEM_WEIGHTED_TYPES and isinstance(parsed, RootModel):
            n = len(parsed.root)
            self.bucket.debit(n // ITEMS_PER_EXTRA_WEIGHT)
        return parsed

    # ---- typed endpoints ----

    def meta(self) -> Meta:
        return self._info(Meta, {"type": "meta"})

    def meta_and_asset_ctxs(self) -> MetaAndAssetCtxs:
        raw = self.info_raw({"type": "metaAndAssetCtxs"})
        try:
            data = json.loads(raw)
            if not (isinstance(data, list) and len(data) == 2):
                raise ApiSchemaError("metaAndAssetCtxs: expected [meta, ctxs]", raw)
            return MetaAndAssetCtxs.model_validate({"meta": data[0], "ctxs": data[1]})
        except json.JSONDecodeError as exc:
            raise ApiSchemaError(f"metaAndAssetCtxs: invalid JSON: {exc}", raw) from exc
        except ValidationError as exc:
            raise ApiSchemaError(f"metaAndAssetCtxs: schema mismatch: {exc}", raw) from exc

    def all_mids(self) -> AllMids:
        return self._info(AllMids, {"type": "allMids"})

    def l2_book(self, coin: str) -> L2Book:
        return self._info(L2Book, {"type": "l2Book", "coin": coin})

    def clearinghouse_state(self, user: str) -> ClearinghouseState:
        return self._info(ClearinghouseState, {"type": "clearinghouseState", "user": user})

    def portfolio(self, user: str) -> Portfolio:
        return self._info(Portfolio, {"type": "portfolio", "user": user})

    def user_role(self, user: str) -> UserRole:
        return self._info(UserRole, {"type": "userRole", "user": user})

    def sub_accounts(self, user: str) -> list[SubAccount]:
        return self._info(SubAccounts, {"type": "subAccounts", "user": user}).items

    def user_fills_page(self, user: str, start_ms: int, end_ms: int | None = None) -> list[Fill]:
        body: dict[str, Any] = {"type": "userFillsByTime", "user": user, "startTime": start_ms}
        if end_ms is not None:
            body["endTime"] = end_ms
        return self._info(Fills, body).root

    def user_fills_by_time(
        self, user: str, start_ms: int, end_ms: int, max_pages: int = 10
    ) -> list[Fill]:
        """All fills in [start_ms, end_ms], oldest first, de-duplicated by tid.

        Pages are capped at FILLS_PAGE_CAP; we advance startTime to the last fill's time (not +1,
        since several fills can share a millisecond) and de-duplicate. Only the most recent
        FILLS_HISTORY_CAP fills of a user are queryable at all, so older history may be missing.
        """
        seen: set[int] = set()
        out: list[Fill] = []
        cursor = start_ms
        for _ in range(max_pages):
            page = self.user_fills_page(user, cursor, end_ms)
            new = [f for f in page if f.tid not in seen]
            for f in new:
                seen.add(f.tid)
            out.extend(new)
            if len(page) < FILLS_PAGE_CAP or not new:
                break
            cursor = max(f.time for f in page)
        else:
            raise FillsPageLimitError(f"userFillsByTime: exceeded {max_pages} pages for {user}")
        out.sort(key=lambda f: (f.time, f.tid))
        return out

    def funding_history(
        self, coin: str, start_ms: int, end_ms: int | None = None
    ) -> list[FundingRecord]:
        body: dict[str, Any] = {"type": "fundingHistory", "coin": coin, "startTime": start_ms}
        if end_ms is not None:
            body["endTime"] = end_ms
        return self._info(FundingHistory, body).root

    def leaderboard_raw(self) -> str:
        """Undocumented stats endpoint (GET, ~40 MB). Not subject to the info weight budget."""
        return self._send("GET", LEADERBOARD_URL, None)

    def leaderboard(self) -> Leaderboard:
        return self._parse(Leaderboard, self.leaderboard_raw(), "leaderboard")
