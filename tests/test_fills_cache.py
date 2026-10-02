from pathlib import Path
from typing import Any

from copybot.clock import DAY_MS
from copybot.hl.models import Fill
from copybot.state.fills_cache import OVERLAP_MS, CachedFills, FillsCache, fetch_fills
from tests.factories import NOW, fill


class FakeClient:
    def __init__(self, fills: list[Fill]) -> None:
        self.fills = fills
        self.calls: list[tuple[int, int]] = []

    def user_fills_by_time(self, user: str, start: int, end: int, max_pages: int = 10) -> Any:
        self.calls.append((start, end))
        return [f for f in self.fills if start <= f.time <= end]


def test_cold_then_incremental(tmp_path: Path) -> None:
    start = NOW - 90 * DAY_MS
    old = [fill(t=NOW - 10 * DAY_MS, start=0, sz=1, side="B", tid=1)]
    client = FakeClient(old)
    cache = FillsCache(tmp_path)
    got, trunc = fetch_fills(client, cache, "0xA", start, NOW, 5)  # type: ignore[arg-type]
    assert [f.tid for f in got] == [1] and not trunc
    assert client.calls == [(start, NOW)]

    later = NOW + DAY_MS
    client.fills = [*old, fill(t=NOW + 1000, start=1, sz=1, side="A", tid=2)]
    got, _ = fetch_fills(client, cache, "0xA", start + DAY_MS, later, 5)  # type: ignore[arg-type]
    assert [f.tid for f in got] == [1, 2]
    assert client.calls[-1] == (NOW - OVERLAP_MS, later)  # only new window fetched


def test_overlap_dedupes_and_prunes(tmp_path: Path) -> None:
    cache = FillsCache(tmp_path)
    a = fill(t=NOW - 100 * DAY_MS, start=0, sz=1, side="B", tid=10)
    b = fill(t=NOW - 1000, start=1, sz=1, side="A", tid=11)
    cache.save("0xa", CachedFills([a, b], NOW - 100 * DAY_MS, NOW))
    client = FakeClient([b])  # overlap returns b again
    got, _ = fetch_fills(client, cache, "0xA", NOW - 90 * DAY_MS, NOW + 10, 5)  # type: ignore[arg-type]
    assert [f.tid for f in got] == [11]  # a pruned (before window), b not duplicated


def test_corrupt_cache_refetches(tmp_path: Path) -> None:
    cache = FillsCache(tmp_path)
    cache.root.mkdir(parents=True)
    (cache.root / "0xa.json.gz").write_bytes(b"not gzip")
    assert cache.load("0xa") is None
    client = FakeClient([])
    fetch_fills(client, cache, "0xa", NOW - DAY_MS, NOW, 5)  # type: ignore[arg-type]
    assert client.calls == [(NOW - DAY_MS, NOW)]


def test_longer_lookback_triggers_cold_fetch(tmp_path: Path) -> None:
    cache = FillsCache(tmp_path)
    cache.save("0xa", CachedFills([], NOW - 30 * DAY_MS, NOW))
    client = FakeClient([])
    fetch_fills(client, cache, "0xa", NOW - 90 * DAY_MS, NOW, 5)  # type: ignore[arg-type]
    assert client.calls == [(NOW - 90 * DAY_MS, NOW)]


def test_no_cache(tmp_path: Path) -> None:
    client = FakeClient([])
    got, _ = fetch_fills(client, None, "0xa", NOW - DAY_MS, NOW, 5)  # type: ignore[arg-type]
    assert got == []
