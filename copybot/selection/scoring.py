"""Composite score (§6.5), hysteresis (§6.6) and the random control draw (§6.7)."""

from __future__ import annotations

import hashlib
import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from copybot.config import WeightsConfig


@dataclass(frozen=True)
class ScoreInputs:
    address: str
    copy_return: float
    return_to_drawdown: float
    positive_weeks: float
    capture: float
    avg_leverage: float


def winsorize(xs: Sequence[float], lo_q: float = 0.05, hi_q: float = 0.95) -> list[float]:
    if len(xs) < 3:
        return list(xs)
    s = sorted(xs)
    n = len(s) - 1
    lo = s[round(lo_q * n)]
    hi = s[round(hi_q * n)]
    return [min(max(x, lo), hi) for x in xs]


def zscores(xs: Sequence[float]) -> list[float]:
    if len(xs) < 2:
        return [0.0 for _ in xs]
    mu = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if sd == 0:
        return [0.0 for _ in xs]
    return [(x - mu) / sd for x in xs]


def composite_scores(items: Sequence[ScoreInputs], w: WeightsConfig) -> dict[str, float]:
    """Weighted sum of winsorised z-scores. Higher is better."""
    if not items:
        return {}
    cols: list[tuple[float, list[float]]] = [
        (w.copy_return, [i.copy_return for i in items]),
        (w.return_to_drawdown, [i.return_to_drawdown for i in items]),
        (w.positive_weeks, [i.positive_weeks for i in items]),
        (w.capture, [i.capture for i in items]),
        (w.low_leverage, [-i.avg_leverage for i in items]),
    ]
    totals = [0.0] * len(items)
    for weight, values in cols:
        for idx, z in enumerate(zscores(winsorize(values))):
            totals[idx] += weight * z
    return {i.address: s for i, s in zip(items, totals, strict=True)}


def rank(scores: dict[str, float]) -> list[str]:
    """Addresses best-first; ties broken by address for determinism."""
    return sorted(scores, key=lambda a: (-scores[a], a))


@dataclass(frozen=True)
class Change:
    address: str
    action: str  # "added" | "dropped" | "kept"
    reason: str


def apply_hysteresis(
    ranked: list[str],
    previous: list[str],
    *,
    n: int,
    keep_within: int,
    max_additions: int,
    exclusion_reasons: dict[str, str],
    unknown: set[str] | frozenset[str] = frozenset(),
) -> tuple[list[str], list[Change]]:
    """§6.6. `ranked` holds only wallets that pass every hard exclusion, best first.

    - A followed wallet stays while it ranks within `keep_within` and passes all exclusions;
      otherwise it is dropped (always — keeping an unsafe wallet is worse than an empty sleeve).
    - New wallets join only from the top `n`, best first, until `n` are followed.
    - At most `max_additions` new wallets per day, except on the first day (nothing followed).
    - Fail safe: a followed wallet in `unknown` (our API error or time budget, not the trader's
      fault) is kept rather than dropped because of missing data.
    """
    pos = {a: i + 1 for i, a in enumerate(ranked)}
    changes: list[Change] = []
    kept: list[str] = []
    for a in previous:
        r = pos.get(a)
        if a in unknown:
            kept.append(a)
            changes.append(Change(a, "kept", "not analysed this refresh (our error); retained"))
        elif r is not None and r <= keep_within:
            kept.append(a)
            changes.append(Change(a, "kept", f"rank {r}"))
        elif r is not None:
            changes.append(Change(a, "dropped", f"rank {r} is outside the top {keep_within}"))
        else:
            reason = exclusion_reasons.get(a, "not analysed this refresh")
            changes.append(Change(a, "dropped", f"excluded: {reason}"))
    budget = n if not previous else max_additions
    followed = list(kept)
    for a in ranked[:n]:
        if len(followed) >= n or budget <= 0:
            break
        if a not in followed:
            followed.append(a)
            budget -= 1
            changes.append(Change(a, "added", f"rank {pos[a]}"))
    followed.sort(key=lambda a: pos.get(a, 10**9))
    return followed, changes


def month_seed(month: str) -> int:
    """Deterministic seed from 'YYYY-MM' so a month's random draw is reproducible."""
    return int.from_bytes(hashlib.sha256(f"copybot-random-{month}".encode()).digest()[:8], "big")


def shuffled(addresses: Sequence[str], month: str) -> list[str]:
    out = sorted(addresses)
    random.Random(month_seed(month)).shuffle(out)
    return out
