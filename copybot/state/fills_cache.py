"""Per-wallet fills cache (D2, D9). A pure cache: deleting it only makes the next refresh slower.

Stored as gzipped JSON under <data>/cache/fills/. In Actions this directory is persisted with
actions/cache rather than committed to the `state` branch (it would bloat git history).
"""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import dataclass
from pathlib import Path

from copybot.clock import DAY_MS
from copybot.hl.client import FILLS_PAGE_CAP, InfoClient
from copybot.hl.models import Fill
from copybot.selection.analytics import looks_truncated

OVERLAP_MS = 60_000
CACHE_VERSION = 1


@dataclass
class CachedFills:
    fills: list[Fill]
    covered_from: int  # fills are complete from this time...
    fetched_until: int  # ...up to this time


class FillsCache:
    def __init__(self, root: Path) -> None:
        self.root = root / "cache" / "fills"

    def _path(self, address: str) -> Path:
        return self.root / f"{address.lower()}.json.gz"

    def load(self, address: str) -> CachedFills | None:
        p = self._path(address)
        if not p.exists():
            return None
        try:
            with gzip.open(p, "rt", encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("version") != CACHE_VERSION:
                return None
            return CachedFills(
                fills=[Fill.model_validate(f) for f in raw["fills"]],
                covered_from=int(raw["covered_from"]),
                fetched_until=int(raw["fetched_until"]),
            )
        except (OSError, ValueError, KeyError):
            return None  # corrupt cache entry: refetch

    def save(self, address: str, data: CachedFills) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        p = self._path(address)
        tmp = p.with_suffix(".tmp")
        payload = {
            "version": CACHE_VERSION,
            "covered_from": data.covered_from,
            "fetched_until": data.fetched_until,
            "fills": [f.model_dump(mode="json") for f in data.fills],
        }
        with gzip.open(tmp, "wt", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, p)


def fetch_fills(
    client: InfoClient,
    cache: FillsCache | None,
    address: str,
    start_ms: int,
    end_ms: int,
    max_pages: int,
) -> tuple[list[Fill], bool]:
    """Fills in [start_ms, end_ms], using and updating the cache. Returns (fills, truncated).

    Raises FillsPageLimitError (too active) and ApiError like the client does; nothing is
    cached in that case.
    """
    cached = cache.load(address) if cache else None
    if cached and cached.covered_from <= start_ms + DAY_MS and cached.fetched_until <= end_ms:
        new = client.user_fills_by_time(
            address, cached.fetched_until - OVERLAP_MS, end_ms, max_pages=max_pages
        )
        by_tid = {f.tid: f for f in cached.fills}
        by_tid.update({f.tid: f for f in new})
        fills = sorted((f for f in by_tid.values() if f.time >= start_ms), key=Fill.chrono_key)
        covered_from = max(cached.covered_from, start_ms)
        truncated = False
    else:
        fills = client.user_fills_by_time(address, start_ms, end_ms, max_pages=max_pages)
        covered_from = start_ms
        truncated = looks_truncated(fills, start_ms, FILLS_PAGE_CAP)
    if cache and not truncated:
        cache.save(address, CachedFills(fills, covered_from, end_ms))
    return fills, truncated
