# Design decisions and spec changes

Newest first. Each entry: what, why, status. "Proposed" entries need the owner's agreement
before CLAUDE.md is changed.

---

## 2026-10-02 — Phase 1 findings from the API probe

### D1. Leaderboard rows aggregate master wallets with their sub-accounts — **adopted 2026-10-02 (owner agreed)**
**Found:** The highest-volume leaderboard row (`0x85ec…2052`, $62M accountValue) is a master
wallet holding ~$15k and no positions, and hasn't traded in 164 days. All of its activity is in
**50 sub-accounts** (`{"type":"subAccounts"}`), holding ~$34M. Copying the leaderboard address
would copy nothing, and its leaderboard stats describe a mix of 50 separate strategies.

**Proposal:** In stage 2, call `subAccounts` (weight 20) for each candidate and compute
`subaccount_value_share` = sub-account equity / (master + sub-account equity). Exclude a
candidate if this share exceeds 0.5 (new config key `selection.max_subaccount_value_share: 0.5`,
reason `trades_via_subaccounts`). Rationale: wallets that run many sub-accounts are typically
funds/market makers, and their per-sub-account stats aren't on the leaderboard. Analysing each
sub-account as its own candidate is possible later but multiplies API cost.

### D2. Stage-2 API cost exceeds the refresh timeout — **adopted 2026-10-02 (owner agreed)**
**Found:** Documented budget is 1200 weight/min per IP; our bucket runs at 50% = 600/min.
Weights: `userRole` 60, `portfolio`/`subAccounts`/`userFillsByTime` 20, plus +1 per 20 fills
returned. One page of 2000 fills costs 120. A very active wallet cost 720 weight for 6 pages
(and ~2,400 for 20 pages in a manual test). For 150 candidates, 90-day fills alone could be
30k–100k weight → **50–170 minutes**, vs. the spec's 30-minute `refresh` timeout.

**Proposal (Phase 2):**
1. *Cheapest checks first, early exit:* `portfolio` (20) → drawdown/consistency filters →
   `subAccounts` (20) → fills (paginated, **max 5 pages**) → fills-based filters →
   copyability sim → `userRole` (60) only for survivors.
2. *Page cap = too active:* hitting 5 pages (10,000 fills) means ≥ 111 fills/day over 90 days,
   far beyond `max_trades_per_day: 30`; exclude with reason `too_active` instead of fetching more.
   Implemented as `FillsPageLimitError` in `hl/client.py`.
3. *Incremental fills cache:* store each analysed wallet's fills on the `state` branch and only
   fetch new fills on later days. After the first refresh, daily cost drops by ~90%.
4. Set `refresh.yml` timeout to 120 min for the first (cold-cache) run if needed — free on a
   public repo (§3.2).

### D3. Fill history is capped and truncation is silent — **adopted (in client)**
**Found:** Docs say only the 10,000 most recent fills are queryable. A manual run paginated
~39,000 fills (≈3.8 days) for one sub-account, so the real cap is larger or time-based. Either
way, querying an old `startTime` silently begins at the oldest *available* fill — no error, no
flag. Pagination by `startTime = last page's max time` re-returns boundary fills.

**Done:** De-duplicate by `tid`; advance `startTime` to the last fill's time (not +1, because
several fills share a millisecond); stop on a short page or a page with no new `tid`s; raise
`FillsPageLimitError` past `max_pages`. Phase 2 will treat "oldest available fill much younger
than the lookback, with full pages" as truncated history.

### D4. No 90-day portfolio window — **adopted 2026-10-02 (owner agreed)**
**Found:** `portfolio` returns `day`/`week`/`month`/`allTime` (+ `perp*`). `month` ≈ 31 days at a
few-hour resolution; `allTime` spans the whole account life at coarse resolution (~70–100
points for a 1.5-year-old account → roughly one point per week).

**Proposal:** Compute `max_drawdown_90d` and weekly PnL by combining `allTime` (for days 31–90)
with `month` (for the last 31 days). Coarse points under-estimate intra-week drawdowns, so also
compute a fills-based equity curve when fills cover the window, and use the worse of the two.
Use the `perp*` windows (we only copy perps).

### D5. Margin tiers exist — note for Phase 3
`meta` includes `marginTables` (tiered max leverage by notional) and per-asset `marginTableId`.
Liquidation maths (§8.4) should use the tier for the position's notional. With our $10k paper
sizes we'll always be in the lowest tier, but the code should read the table, not assume.

### D6. Spot/other markets mixed into `allMids` and fills — **adopted**
`allMids` returns 1,141 keys of which only ~178 are live perps; the rest are spot (`@n`,
`BASE/QUOTE`) and other markets (`#n`). `AllMids.perp_only()` and `Fill.is_perp` filter these.
56 of 234 `meta` assets are delisted (`isDelisted`) and have null `midPx`.

### D7. Leaderboard payload is ~40 MB — **adopted**
Still served at `stats-data.hyperliquid.xyz/Mainnet/leaderboard` (47,152 rows, same shape as the
spec). It isn't on the info weight budget. Fixture keeps only the first 60 rows. Fine for a
once-a-day job; noted in case the runner's memory or time becomes an issue.

---

## 2026-10-02 — Phase 1 scaffold choices

- **Layout:** `copybot/` package at the repo root (as in §4), built with `uv_build`
  (`module-root = ""`). Python pinned to 3.12 via `.python-version`.
- **Money in API models:** a custom `Dec` type rejects JSON floats and non-finite values, so a
  silent API change to floats fails validation instead of losing precision (§2, §5.2).
- **Unknown response fields are ignored; missing required fields fail.** Additive API changes
  shouldn't break the bot; removed or retyped fields must.
- **Config is strict:** unknown keys in `config.yaml` are an error (typo protection), and a test
  asserts `config.yaml` equals the model defaults so they can't drift apart.
- **Secrets scanning:** `gitleaks` + `detect-private-key` in pre-commit; a local hook and a CI
  step fail if `config.yaml` doesn't have `live.enabled: false`.
