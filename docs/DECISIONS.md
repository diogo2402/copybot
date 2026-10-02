# Design decisions and spec changes

Newest first. Each entry: what, why, status. "Proposed" entries need the owner's agreement
before CLAUDE.md is changed.

---

## 2026-10-02 — Phase 2 threshold review (first full refresh: 0 qualified)

### D13. Trades are exit episodes; capture is measured against an ideal copy — **adopted 2026-10-02 (owner agreed)**
**Bug found and fixed first:** one order sweeping the book yields several fills with the same
millisecond `time`, and their `tid` order isn't execution order. Sorting by `(time, tid)` broke
the position chain on ~90% of fills, which zeroed round-trip counts for many wallets.
`Fill.chrono_key` orders same-ms fills along the `startPosition` chain; chain mismatches over
156k real fills fell to 0.25%.

**What-if over the top 150 (by month PnL), after the fix** (`scripts/whatif_report.py`):

| Rule set | Pass |
|---|---|
| Spec as written (round trips, capture vs trader return) | 0 |
| Exit episodes instead of round trips | 1 |
| + max drawdown 50% or 60% | 1 |
| + min 10 trades | 1 |
| + capture vs ideal copy | 1 |

Of 150: 79 too active, 26 unprofitable over 90 days; of the 45 fully analysed, most are whales
with a handful of very large trades. Relaxing thresholds doesn't help; the pool is the
bottleneck (see D14).

**Adopted:**
1. A trade = an exit episode (FIFO lot matching; reducing fills clustered with < 1h gaps).
   Round trips remain as diagnostic fields.
2. `copy_capture_ratio` = lagged copy return / ideal copy return (same sizing and caps, copied
   instantly at the trader's fill prices, no costs). The old trader-relative ratio penalised
   traders for leverage we cap anyway; e.g. a trader at +1,488% with a +29% capped copy scored
   0.02. Kept as `trader_capture_ratio`.
3. Drawdown limit stays at 40%: relaxing it gained nothing.

### D14. Rank the stage-2 pool by month ROI instead of month PnL — **experiment running**
Ranking 2,596 stage-1 survivors by dollar PnL fills the pool with whales (huge accounts, few
huge trades, or market makers). Ranking by ROI should favour skilled mid-sized traders. New
config key `selection.pool_sort` (default `month_pnl` until the experiment is reviewed).

---

## 2026-10-02 — Phase 2 (selection) decisions

### D8. Copyability sim prices come from 1h candles — **adopted**
`candleSnapshot` only serves the most recent ~5000 candles per interval: 15m candles reach back
52 days, 1h candles 208 days. To cover the 90-day lookback with one request per coin (~58 weight,
shared across all candidates), the sim uses 1h candles and interpolates linearly between each
candle's open and close. This smooths moves inside an hour, so the late-entry check is a bit more
lenient than it will be live. Funding is ignored in the selection sim (the Phase 3 backtest
includes it). The sim starts flat, so positions held before the window are copied only once the
trader trades that coin.

### D9. Fills cache lives in GitHub Actions' cache, not on the `state` branch — **adopted 2026-10-02 (owner agreed)**
D2 said "cache each wallet's fills on the `state` branch". Up to 150 wallets × up to 10,000
fills, rewritten daily, would add tens of MB of git history every day. The cache is pure
optimisation (losing it only makes one refresh slower), so it belongs in `actions/cache`
(persists between runs, evicted after 7 days unused, never in git). Code: `state/fills_cache.py`
writes `<data>/cache/fills/*.json.gz`; Phase 4 will gitignore `cache/` on the state branch and
restore/save it with `actions/cache`.

### D10. Trader returns are time-weighted (Modified Dietz) on total equity — **adopted**
(refines D4, which the owner agreed.) The first real run showed two distortions:
1. Measuring PnL against the account value 90 days ago ignores deposits: a trader who started
   with $1M and deposited $15M had later PnL swings measured against $1M.
2. Many large traders move money between spot and perps. The perp-only account value can swing
   to ~0 while their total equity is $30–50M, which turned a $1M loss into "−52%".

Method: chain per-interval returns `r = Δ perp PnL / (total account value at start + ½ × net
deposits)`, where net deposits = Δ total account value − Δ total PnL. This is the standard
Modified Dietz correction for flows at unknown times inside an interval (the all-time series has
~weekly points). Intervals on a near-empty account (< $1,000) are skipped. `return_90d`,
`max_drawdown_90d` and the copy sim's capture ratio use this index. The fills-based drawdown
divides each fill's realized PnL by the total account value at that time.

**Phase 3 consequence — adopted 2026-10-02 (owner agreed), §7.1–7.2 updated:** §7.2 defines exposure fraction as position notional /
`clearinghouseState.accountValue`, which is perp-only. For traders holding most of their capital
in spot, that overstates their conviction (a $10M position looks like 500% of a $2M perp account
when it's 25% of a $40M total). Proposal: divide by perp + spot equity (`spotClearinghouseState`,
weight 2, valued at mids). The selection sim already uses total equity.

### D11. "At most 2 replacements per day" limits additions — **adopted (interpretation)**
§6.6 says a followed wallet stays while it ranks within 2N *and* passes all exclusions, and that
at most 2 wallets are replaced per day. These conflict when more than 2 followed wallets fail on
the same day. Resolution: wallets that fail an exclusion or fall outside the top 2N are always
dropped (keeping an unsafe wallet is worse than an empty sleeve); the 2-per-day limit applies to
*additions*. On the first refresh (nothing followed yet) up to N are added. The unused sleeves
stay in cash.

Fail safe (§0.3): a followed wallet we couldn't analyse because of *our* problem (API error,
time budget) is kept, not dropped. Missing data never removes a trader.

### D12. Smaller selection details — **adopted**
- `pnl_90d ≤ 0` excludes a candidate at the cheap portfolio step (`not_profitable_90d`). Not a new
  rule: such a wallet would fail `copy_capture_ratio ≥ 0.4` anyway; this just stops early.
- Random control set (§6.7): besides the holding-time and trades-per-day rules, a drawn wallet
  must not trade via sub-accounts and must have closed trades, otherwise "copying" it copies
  nothing and the control is biased toward cash. At most 60 draws per month.
- The refresh has a time budget (default 110 min). Wallets not reached are marked `skipped` and
  treated like API errors for hysteresis.
- Followed wallets are always re-analysed, even if they fall out of the top-150 pool.
- Orchestration lives in `copybot/selection/refresh.py` (not listed in §4's layout).

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
