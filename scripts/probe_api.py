"""Phase 1 API probe.

Calls every Hyperliquid endpoint the bot depends on, saves raw responses to tests/fixtures/,
validates them against copybot.hl.models, measures pagination behaviour, and writes
docs/API_NOTES.md. Prints a pass/fail table; exits non-zero if any check fails.

Run:  uv run python scripts/probe_api.py      (or: uv run python -m copybot probe)
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from copybot.clock import DAY_MS, SystemClock  # noqa: E402
from copybot.hl import client as hlc  # noqa: E402
from copybot.hl.client import ApiError, InfoClient  # noqa: E402
from copybot.hl.models import (  # noqa: E402
    AllMids,
    Candles,
    ClearinghouseState,
    Fills,
    FundingHistory,
    L2Book,
    Leaderboard,
    LeaderboardRow,
    Meta,
    Portfolio,
    SubAccounts,
    UserRole,
)

FIXTURES = ROOT / "tests" / "fixtures"
NOTES = ROOT / "docs" / "API_NOTES.md"
LEADERBOARD_SAMPLE_ROWS = 60


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    notes: list[str] = field(default_factory=list)


def _save(name: str, raw: str) -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / name).write_text(raw, encoding="utf-8")
    return len(raw)


def _shape(obj: Any, depth: int = 0, max_depth: int = 4) -> str:
    """Compact type sketch of a JSON value for the notes file."""
    if depth > max_depth:
        return "…"
    if isinstance(obj, dict):
        items = list(obj.items())[:25]
        inner = ", ".join(f"{k}: {_shape(v, depth + 1)}" for k, v in items)
        return "{" + inner + "}"
    if isinstance(obj, list):
        if not obj:
            return "[]"
        return f"[{_shape(obj[0], depth + 1)}, …] (len {len(obj)})"
    if isinstance(obj, str):
        try:
            Decimal(obj)
            return "str(decimal)"
        except Exception:
            return "str"
    return type(obj).__name__


def _month(r: LeaderboardRow) -> tuple[Decimal, Decimal]:
    p = r.perf("month")
    return (p.pnl, p.vlm) if p else (Decimal(0), Decimal(0))


def run_probe() -> int:
    clock = SystemClock()
    now = clock.now_ms()
    client = InfoClient()
    checks: list[Check] = []
    shapes: dict[str, str] = {}

    def attempt(name: str, fn: Callable[[], Check]) -> None:
        t0 = time.monotonic()
        try:
            c = fn()
        except ApiError as exc:
            c = Check(name, False, f"{type(exc).__name__}: {str(exc)[:300]}")
        except Exception as exc:  # probe must report, not crash
            c = Check(name, False, f"unexpected {type(exc).__name__}: {str(exc)[:300]}")
        c.detail += f" ({time.monotonic() - t0:.1f}s)"
        checks.append(c)
        print(f"  {'PASS' if c.ok else 'FAIL'}  {name}: {c.detail}", flush=True)

    # 1. Leaderboard -------------------------------------------------------------------------
    lb_rows: list[LeaderboardRow] = []

    def leaderboard() -> Check:
        raw = client.leaderboard_raw()
        data = json.loads(raw)
        lb = Leaderboard.model_validate(data)
        lb_rows.extend(lb.leaderboardRows)
        sample = {"leaderboardRows": data["leaderboardRows"][:LEADERBOARD_SAMPLE_ROWS]}
        _save("leaderboard_sample.json", json.dumps(sample))
        shapes["leaderboard"] = _shape(sample)
        windows = sorted({w for r in lb.leaderboardRows for w, _ in r.windowPerformances})
        return Check(
            "leaderboard",
            True,
            f"{len(lb.leaderboardRows)} rows, {len(raw) / 1e6:.1f} MB, windows={windows}",
            [
                f"GET {hlc.LEADERBOARD_URL} → 200, {len(raw) / 1e6:.1f} MB, "
                f"{len(lb.leaderboardRows)} rows. Windows: {windows}.",
                f"Fixture keeps only the first {LEADERBOARD_SAMPLE_ROWS} rows "
                "(full payload is too large to commit).",
            ],
        )

    print("Probing Hyperliquid API…")
    attempt("leaderboard", leaderboard)

    # Pick test addresses: a mid-sized active trader (sample user) and a very high-volume one.
    sample_user = "0x0000000000000000000000000000000000000000"
    heavy_user = sample_user
    if lb_rows:

        def eligible(r: LeaderboardRow) -> bool:
            pnl, vlm = _month(r)
            return (
                Decimal(100_000) <= r.accountValue <= Decimal(10_000_000)
                and pnl > 0
                and Decimal(1_000_000) <= vlm <= 50 * r.accountValue
            )

        cands = sorted(filter(eligible, lb_rows), key=lambda r: -_month(r)[0])
        # Prefer a candidate that actually traded this week, so the fills fixture is useful.
        for r in cands[:15]:
            try:
                recent = client.user_fills_page(r.ethAddress, now - 7 * DAY_MS, now)
            except ApiError:
                continue
            if 20 <= len(recent) < hlc.FILLS_PAGE_CAP:
                sample_user = r.ethAddress
                break
        else:
            if cands:
                sample_user = cands[0].ethAddress
        heavy_user = max(lb_rows, key=lambda r: _month(r)[1]).ethAddress
    print(f"  sample user: {sample_user}\n  high-volume user: {heavy_user}")

    # 2. Typed info endpoints ---------------------------------------------------------------
    meta_holder: list[Meta] = []

    def simple(
        name: str, body: dict[str, Any], model: Any, fixture: str, summarize: Callable[[Any], str]
    ) -> Callable[[], Check]:
        def run() -> Check:
            raw = client.info_raw(body)
            data = json.loads(raw)
            parsed = model.model_validate(data)
            size = _save(fixture, raw)
            shapes[name] = _shape(data)
            return Check(name, True, f"{summarize(parsed)}; {size / 1e3:.0f} KB saved")

        return run

    def meta() -> Check:
        m = client.meta()
        meta_holder.append(m)
        raw = client.info_raw({"type": "meta"})
        _save("meta.json", raw)
        shapes["meta"] = _shape(json.loads(raw))
        delisted = sum(a.isDelisted for a in m.universe)
        return Check(
            "meta",
            True,
            f"{len(m.universe)} assets ({delisted} delisted), {len(m.marginTables)} margin tables",
            [
                "Undocumented-in-spec fields present: `marginTableId`, `onlyIsolated`, "
                "`marginMode`, `isDelisted`, top-level `marginTables` (tiered max leverage by "
                "notional) and `collateralToken`. Liquidation maths must use margin tiers.",
            ],
        )

    def mac() -> Check:
        raw = client.info_raw({"type": "metaAndAssetCtxs"})
        data = json.loads(raw)
        parsed = client.meta_and_asset_ctxs()  # validates via the typed path too
        _save("metaAndAssetCtxs.json", raw)
        shapes["metaAndAssetCtxs"] = _shape(data)
        no_mid = sum(c.midPx is None for c in parsed.ctxs)
        return Check(
            "metaAndAssetCtxs",
            True,
            f"{len(parsed.ctxs)} ctxs, {no_mid} with null midPx",
            [
                "Response is a 2-element array `[meta, [assetCtx…]]`, ctxs index-aligned with "
                "`meta.universe`. `funding` is the current hourly rate. "
                f"`midPx` is null for {no_mid} (illiquid/delisted) assets — use markPx then.",
            ],
        )

    def mids() -> Check:
        raw = client.info_raw({"type": "allMids"})
        parsed = AllMids.model_validate(json.loads(raw))
        _save("allMids.json", raw)
        shapes["allMids"] = "{coin: str(decimal)}"
        perp = meta_holder[0].perp_coins() if meta_holder else set()
        n_perp = len(parsed.perp_only(perp))
        other = len(parsed.root) - n_perp
        return Check(
            "allMids",
            True,
            f"{len(parsed.root)} keys, {n_perp} perp, {other} spot/other",
            [
                "Keys include spot (`@<n>`, `PURR/USDC`) and other markets (`#<n>`) besides "
                "perp coin names. Always filter to `meta.universe` names.",
            ],
        )

    attempt("meta", meta)
    attempt("metaAndAssetCtxs", mac)
    attempt("allMids", mids)
    attempt(
        "l2Book",
        simple(
            "l2Book",
            {"type": "l2Book", "coin": "BTC"},
            L2Book,
            "l2Book_BTC.json",
            lambda b: (
                f"{len(b.bids)} bids / {len(b.asks)} asks, best {b.bids[0].px}/{b.asks[0].px}"
            ),
        ),
    )
    attempt(
        "clearinghouseState",
        simple(
            "clearinghouseState",
            {"type": "clearinghouseState", "user": sample_user},
            ClearinghouseState,
            "clearinghouseState.json",
            lambda s: (
                f"{len(s.assetPositions)} positions, accountValue "
                f"{s.marginSummary.accountValue:.0f}"
            ),
        ),
    )
    attempt(
        "portfolio",
        simple(
            "portfolio",
            {"type": "portfolio", "user": sample_user},
            Portfolio,
            "portfolio.json",
            lambda p: ", ".join(
                f"{k}:{len(v.accountValueHistory)}pts/"
                f"{(v.accountValueHistory[-1][0] - v.accountValueHistory[0][0]) / DAY_MS:.0f}d"
                for k, v in p.root
                if v.accountValueHistory
            ),
        ),
    )
    attempt(
        "userRole",
        simple(
            "userRole",
            {"type": "userRole", "user": sample_user},
            UserRole,
            "userRole.json",
            lambda r: f"role={r.role}",
        ),
    )
    attempt(
        "fundingHistory",
        simple(
            "fundingHistory",
            {"type": "fundingHistory", "coin": "BTC", "startTime": now - 3 * DAY_MS},
            FundingHistory,
            "fundingHistory_BTC.json",
            lambda f: f"{len(f.root)} records over 3d (~{len(f.root) / 72:.2f}/hour)",
        ),
    )
    attempt(
        "userFillsByTime",
        simple(
            "userFillsByTime",
            {
                "type": "userFillsByTime",
                "user": sample_user,
                "startTime": now - 7 * DAY_MS,
                "endTime": now,
            },
            Fills,
            "userFillsByTime.json",
            lambda f: f"{len(f.root)} fills over 7d",
        ),
    )

    attempt(
        "candleSnapshot",
        simple(
            "candleSnapshot",
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": "BTC",
                    "interval": "1h",
                    "startTime": now - 95 * DAY_MS,
                    "endTime": now,
                },
            },
            Candles,
            "candles_BTC_1h.json",
            lambda c: f"{len(c.root)} 1h candles, oldest {(now - c.root[0].t) / DAY_MS:.0f}d ago",
        ),
    )

    # A known vault address must not be classified as a plain user.
    def user_role_vault() -> Check:
        hlp = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"  # HLP vault (public)
        r = client.user_role(hlp)
        _save("userRole_vault.json", json.dumps(r.model_dump()))
        return Check("userRole(vault)", r.role == "vault", f"HLP role={r.role}")

    attempt("userRole(vault)", user_role_vault)

    # 3. Sub-accounts: leaderboard rows aggregate a master and its sub-accounts -------------
    active_user: list[str] = []

    def sub_accounts() -> Check:
        raw = client.info_raw({"type": "subAccounts", "user": heavy_user})
        parsed = SubAccounts.model_validate(json.loads(raw))
        _save("subAccounts.json", raw[:200_000] if len(raw) > 200_000 else raw)
        subs = parsed.items
        master = client.clearinghouse_state(heavy_user)
        sub_value = sum(
            (sa.clearinghouseState.marginSummary.accountValue for sa in subs), Decimal(0)
        )
        if subs:
            shapes["subAccounts"] = _shape(json.loads(raw)[:1])
            top = max(subs, key=lambda sa: sa.clearinghouseState.marginSummary.totalNtlPos)
            active_user.append(top.subAccountUser)
        return Check(
            "subAccounts",
            True,
            f"{len(subs)} sub-accounts; master value {master.marginSummary.accountValue:.0f} vs "
            f"sub-accounts {sub_value:.0f}",
            [
                "**Leaderboard rows aggregate a master wallet and its sub-accounts.** For the "
                f"top-volume row the master holds {master.marginSummary.accountValue:.0f} USD and "
                f"{len(master.assetPositions)} positions while {len(subs)} sub-accounts hold "
                f"{sub_value:.0f} USD. Copying the master address would copy nothing. "
                "`subAccounts` returns null when there are none.",
            ],
        )

    attempt("subAccounts", sub_accounts)

    # 4. Pagination + history depth on the most active wallet we found ------------------------
    def pagination() -> Check:
        user = active_user[0] if active_user else heavy_user
        start = now - 90 * DAY_MS
        max_pages = 6
        pages: list[int] = []
        seen: set[int] = set()
        cursor = start
        earliest = None
        dup_total = 0
        w0 = client.bucket.total_consumed
        for _ in range(max_pages):
            page = client.user_fills_page(user, cursor, now)
            pages.append(len(page))
            new = [f for f in page if f.tid not in seen]
            dup_total += len(page) - len(new)
            seen.update(f.tid for f in page)
            if page:
                t = min(f.time for f in page)
                earliest = t if earliest is None else min(earliest, t)
            if len(page) < hlc.FILLS_PAGE_CAP or not new:
                break
            cursor = max(f.time for f in page)
        span_days = (now - earliest) / DAY_MS if earliest else 0
        weight = client.bucket.total_consumed - w0
        return Check(
            "fills pagination",
            bool(pages) and max(pages) <= hlc.FILLS_PAGE_CAP,
            f"{user[:10]}…: pages={pages}, unique={len(seen)}, dups={dup_total}, "
            f"oldest available fill {span_days:.1f}d ago, weight {weight:.0f}",
            [
                f"Very active wallet, 90d window: page sizes {pages} (stopped at {max_pages}), "
                f"{len(seen)} unique fills, oldest available fill only {span_days:.1f} days ago, "
                f"costing {weight:.0f} weight. Asking for an older startTime silently starts at "
                "the oldest *available* fill — there is no error or flag for truncated history.",
                "Paginating by setting startTime = last page's max `time` re-returns boundary "
                f"fills ({dup_total} duplicates here) — de-duplicate by `tid`.",
                "Docs claim a 10,000-fill history cap; a manual run on 2026-10-02 paginated "
                "~39,000 fills (≈3.8 days) for one sub-account, so the real cap is larger or "
                "time-based. Treat history depth as unknown and detect it.",
            ],
        )

    attempt("fills pagination", pagination)

    # 5. Error behaviour: a 4xx must not be retried and must raise ApiError ------------------
    def bad_request() -> Check:
        before = client.request_count
        try:
            client.info_raw({"type": "definitelyNotAType"})
        except ApiError as exc:
            return Check(
                "4xx handling",
                client.request_count - before == 1,
                f"raised ApiError after {client.request_count - before} request(s): "
                f"{str(exc)[:80]}",
            )
        return Check("4xx handling", False, "unknown type did not raise")

    attempt("4xx handling", bad_request)

    client.close()
    budget_used = client.bucket.total_consumed
    write_notes(checks, shapes, sample_user, heavy_user, budget_used, now)

    print("\n  result  check")
    for c in checks:
        print(f"  {'PASS' if c.ok else 'FAIL':6}  {c.name}")
    n_fail = sum(not c.ok for c in checks)
    print(
        f"\n{len(checks) - n_fail}/{len(checks)} passed. "
        f"Weight consumed ≈ {budget_used:.0f}. Notes → {NOTES.relative_to(ROOT)}"
    )
    return 1 if n_fail else 0


def write_notes(
    checks: list[Check],
    shapes: dict[str, str],
    sample_user: str,
    heavy_user: str,
    weight: float,
    now: int,
) -> None:
    ts = datetime.fromtimestamp(now / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Hyperliquid API notes (verified)",
        "",
        f"Generated by `scripts/probe_api.py` on **{ts}**. Re-run the probe to refresh; "
        "manual additions belong in the *Manual notes* section at the bottom.",
        "",
        f"- Sample user: `{sample_user}`  ",
        f"- High-volume user (pagination test): `{heavy_user}`",
        f"- Approximate request weight consumed by the probe: {weight:.0f}",
        "",
        "## Results",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for c in checks:
        detail = c.detail.replace("|", "\\|")
        lines.append(f"| {c.name} | {'PASS' if c.ok else '**FAIL**'} | {detail} |")
    lines += ["", "## Observations", ""]
    for c in checks:
        for n in c.notes:
            lines.append(f"- **{c.name}:** {n}")
    lines += ["", "## Response shapes", ""]
    for k, v in shapes.items():
        lines += [f"### `{k}`", "", "```", v, "```", ""]
    manual = NOTES.read_text(encoding="utf-8").split("<!-- manual -->", 1) if NOTES.exists() else []
    tail = manual[1] if len(manual) == 2 else "\n" + MANUAL_DEFAULT
    lines += ["<!-- manual -->" + tail.rstrip(), ""]
    NOTES.parent.mkdir(parents=True, exist_ok=True)
    NOTES.write_text("\n".join(lines), encoding="utf-8")


MANUAL_DEFAULT = """\
## Manual notes (kept across probe runs)

### Rate limits (from docs, 2026-10-02)
- Per-IP REST budget: **1200 weight / minute**.
- Weight 2: `l2Book`, `allMids`, `clearinghouseState`, `orderStatus`, `spotClearinghouseState`,
  `exchangeStatus`.
- Weight 60: `userRole`.
- Weight 20: every other info request (`meta`, `metaAndAssetCtxs`, `portfolio`,
  `userFillsByTime`, `fundingHistory`, …).
- `userFills`, `userFillsByTime`, `fundingHistory`: additional weight per 20 items returned
  (we charge +1 per 20 items).
- The bot's token bucket runs at 50% (600/min) because Actions runners share IPs.

### Fills
- `userFillsByTime` returns at most **2000** fills per response and only the **10000 most recent**
  fills of a user are queryable.
- Fill fields beyond the spec: `feeToken`, `twapId` (nullable), `builderFee` (only when non-zero).

### Portfolio
- No 90-day window exists. Windows: day/week/month/allTime + perp* variants. `month` ≈ 30 days
  at a few-hour resolution; `allTime` covers the account's whole life at coarse resolution
  (tens of points). 90-day drawdown must come from `allTime` (coarse) or be reconstructed.
"""


if __name__ == "__main__":
    sys.exit(run_probe())
