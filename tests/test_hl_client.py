import json
from typing import Any

import httpx
import pytest
import respx

from copybot.hl.client import (
    MAINNET_API,
    ApiError,
    ApiSchemaError,
    FillsPageLimitError,
    InfoClient,
    TokenBucket,
    request_weight,
)

INFO = f"{MAINNET_API}/info"


def fill(tid: int, t: int) -> dict[str, Any]:
    return {
        "coin": "BTC",
        "px": "100",
        "sz": "1",
        "side": "B",
        "time": t,
        "startPosition": "0",
        "dir": "Open Long",
        "closedPnl": "0",
        "hash": "0x",
        "oid": tid,
        "tid": tid,
        "crossed": True,
        "fee": "0.01",
    }


class FakeTime:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def make_client(**kw: Any) -> tuple[InfoClient, FakeTime]:
    ft = FakeTime()
    bucket = TokenBucket(600, monotonic=ft.monotonic, sleep=ft.sleep)
    return InfoClient(bucket=bucket, sleep=ft.sleep, **kw), ft


@respx.mock
def test_retries_on_5xx_then_succeeds() -> None:
    route = respx.post(INFO).mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(429),
            httpx.Response(200, json={"role": "user"}),
        ]
    )
    c, ft = make_client()
    assert c.user_role("0xabc").role == "user"
    assert route.call_count == 3
    assert len(ft.slept) >= 2  # backed off between attempts


@respx.mock
def test_gives_up_after_three_attempts() -> None:
    route = respx.post(INFO).mock(side_effect=httpx.ConnectTimeout("t"))
    c, _ = make_client()
    with pytest.raises(ApiError):
        c.all_mids()
    assert route.call_count == 3


@respx.mock
def test_4xx_not_retried() -> None:
    route = respx.post(INFO).mock(return_value=httpx.Response(422, text="bad"))
    c, _ = make_client()
    with pytest.raises(ApiError) as ei:
        c.all_mids()
    assert not isinstance(ei.value, ApiSchemaError)
    assert route.call_count == 1


@respx.mock
def test_schema_error_carries_truncated_raw() -> None:
    respx.post(INFO).mock(return_value=httpx.Response(200, json={"oops": "x" * 10_000}))
    c, _ = make_client()
    with pytest.raises(ApiSchemaError) as ei:
        c.clearinghouse_state("0xabc")
    assert len(ei.value.raw) <= 5 * 1024


@respx.mock
def test_invalid_json_is_schema_error() -> None:
    respx.post(INFO).mock(return_value=httpx.Response(200, text="<html>"))
    c, _ = make_client()
    with pytest.raises(ApiSchemaError):
        c.all_mids()


@respx.mock
def test_meta_and_asset_ctxs_shape_check() -> None:
    respx.post(INFO).mock(return_value=httpx.Response(200, json={"not": "a list"}))
    c, _ = make_client()
    with pytest.raises(ApiSchemaError):
        c.meta_and_asset_ctxs()


@respx.mock
def test_fill_pagination_dedupes_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copybot.hl.client.FILLS_PAGE_CAP", 3)
    pages = [
        [fill(1, 10), fill(2, 20), fill(3, 30)],
        [fill(3, 30), fill(4, 30), fill(5, 40)],  # boundary fill 3 repeated
        [fill(5, 40), fill(6, 50)],
    ]
    starts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        starts.append(json.loads(req.content)["startTime"])
        return httpx.Response(200, json=pages[len(starts) - 1])

    respx.post(INFO).mock(side_effect=handler)
    c, _ = make_client()
    out = c.user_fills_by_time("0xabc", 0, 100)
    assert [f.tid for f in out] == [1, 2, 3, 4, 5, 6]
    assert starts == [0, 30, 40]  # advances to last time, not +1


@respx.mock
def test_fill_pagination_stops_when_no_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copybot.hl.client.FILLS_PAGE_CAP", 2)
    respx.post(INFO).mock(return_value=httpx.Response(200, json=[fill(1, 10), fill(2, 10)]))
    c, _ = make_client()
    assert [f.tid for f in c.user_fills_by_time("0xabc", 0, 100)] == [1, 2]


@respx.mock
def test_fill_page_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("copybot.hl.client.FILLS_PAGE_CAP", 1)
    n = iter(range(1, 100))

    def handler(req: httpx.Request) -> httpx.Response:
        i = next(n)
        return httpx.Response(200, json=[fill(i, i)])

    respx.post(INFO).mock(side_effect=handler)
    c, _ = make_client()
    with pytest.raises(FillsPageLimitError):
        c.user_fills_by_time("0xabc", 0, 100, max_pages=3)


def test_request_weights() -> None:
    assert request_weight("clearinghouseState") == 2
    assert request_weight("l2Book") == 2
    assert request_weight("userRole") == 60
    assert request_weight("portfolio") == 20


def test_token_bucket_throttles() -> None:
    ft = FakeTime()
    b = TokenBucket(60, monotonic=ft.monotonic, sleep=ft.sleep)  # 1 weight/s
    b.acquire(60)
    assert ft.slept == []
    b.acquire(10)
    assert sum(ft.slept) == pytest.approx(10)


def test_token_bucket_debit_delays_next() -> None:
    ft = FakeTime()
    b = TokenBucket(60, monotonic=ft.monotonic, sleep=ft.sleep)
    b.acquire(60)
    b.debit(30)  # per-item weight discovered after the response
    b.acquire(1)
    assert sum(ft.slept) == pytest.approx(31)


@respx.mock
def test_item_weight_charged_for_fills() -> None:
    respx.post(INFO).mock(return_value=httpx.Response(200, json=[fill(i, i) for i in range(40)]))
    c, _ = make_client()
    c.user_fills_page("0xabc", 0)
    assert c.bucket.total_consumed == 20 + 2


@respx.mock
def test_sub_accounts_null() -> None:
    respx.post(INFO).mock(return_value=httpx.Response(200, text="null"))
    c, _ = make_client()
    assert c.sub_accounts("0xabc") == []


@respx.mock
def test_leaderboard_lowercases_addresses() -> None:
    from copybot.hl.client import LEADERBOARD_URL
    from copybot.hl.leaderboard import fetch_leaderboard

    respx.get(LEADERBOARD_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "leaderboardRows": [
                    {
                        "ethAddress": "0xABC",
                        "accountValue": "1",
                        "displayName": None,
                        "prize": 0,
                        "windowPerformances": [["month", {"pnl": "1", "roi": "0.1", "vlm": "5"}]],
                    }
                ]
            },
        )
    )
    c, _ = make_client()
    rows = fetch_leaderboard(c)
    assert rows[0].ethAddress == "0xabc"
