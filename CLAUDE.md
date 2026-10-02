# CLAUDE.md — Hyperliquid Copy-Trading Bot ("copybot")

You are Claude Code, building this project for its owner (Diogo). Read this whole file before writing any code. It is the single source of truth for what to build, in what order, and what "done" means. When this file and your own judgment disagree, follow this file and raise the disagreement with the owner. When something here turns out to be technically wrong (an API field, an endpoint, a limit), stop, tell the owner what you found, propose a fix, and update this file once agreed.

---

## 0. Ground rules (non-negotiable)

1. **Paper trading is the default and the priority.** The live-trading module exists but ships locked off. You never enable it, never set its flags, never place a real order, and never handle a private key yourself. Live mode is something the owner turns on manually, later, outside your session.
2. **No LLM in the runtime loop.** The bot is deterministic Python. No calls to Claude or any model at runtime. Every decision is reproducible from code + config + data.
3. **Fail safe, not fail open.** Any API error, timeout, malformed response, or stale data means *do nothing this cycle* (and alert if it persists). Missing data must never be interpreted as "the trader closed their position".
4. **Secrets never touch the repo.** All credentials live in GitHub Actions secrets or a local `.env` that is in `.gitignore`. Add a pre-commit check (see §13) that blocks commits containing anything that looks like a private key, token or password.
5. **Verify before trusting.** The Hyperliquid API details in this file are written from documentation and may be outdated. Phase 1 is a probe script that confirms every endpoint and field before anything depends on it.
6. **Ask the owner before any step that needs them**: creating accounts, creating tokens, adding secrets, changing repo visibility, enabling workflows. Give exact click-by-click instructions when you ask.
7. **Small, tested steps.** Each phase in §15 ends with passing tests and a short summary to the owner of what was built and how to check it.

---

## 1. What the bot does (one paragraph)

Once a day, it scans Hyperliquid's public leaderboard, deeply analyses the candidates' trading history, and picks the top N traders whose style is actually *copyable* by a bot that checks every few minutes. Every few minutes, it checks those traders' fills and open positions, and mirrors their moves in a **paper portfolio** with realistic costs (fees, slippage from the real order book, funding, liquidation). In parallel it runs two **control portfolios** — buy-and-hold BTC and "copy N random qualifying traders" — so we can tell whether the strategy has real edge or just rode the market. It emails a daily report and sends immediate emails for important events. A live-trading module mirrors the paper engine's decisions onto a real Hyperliquid account, but is locked off behind several independent gates.

---

## 2. Tech stack

- **Python 3.12**, managed with `uv` (fall back to `pip` + `requirements.txt` if `uv` is unavailable in Actions).
- Libraries: `httpx` (HTTP with timeouts and retries), `pydantic` v2 (config + API response models), `pyyaml`, `pandas` (analytics only, not in the hot path where avoidable), `jinja2` (email HTML templates), `matplotlib` (charts embedded in emails), `pytest` + `pytest-cov`, `respx` (HTTP mocking), `ruff` (lint + format), `mypy` (strict on `copybot/` package).
- Live module only: the official `hyperliquid-python-sdk` (pin the version). Import it lazily inside `copybot/live/` so paper mode never loads it.
- Standard library `smtplib` + `email` for sending email.
- All timestamps internally are **UTC, timezone-aware, in milliseconds** (Hyperliquid's native unit). Convert to Europe/Lisbon only for display in emails.
- All money values are `Decimal` in the accounting engine. Floats are allowed only in analytics/charts.

---

## 3. Hosting: GitHub Actions + cron-job.org

### 3.1 Why and how
- The bot runs entirely on GitHub Actions. No server. The owner's laptop can be off.
- GitHub's built-in `schedule:` cron is unreliable for frequent jobs (runs get delayed or skipped under load). So every workflow is triggered by **cron-job.org** calling GitHub's `workflow_dispatch` REST API. Keep a `schedule:` trigger as a low-frequency fallback only (e.g., the poll workflow also has `schedule: '*/30 * * * *'`), and make every job idempotent so double triggers are harmless.

### 3.2 Actions minutes — IMPORTANT, explain this to the owner before Phase 4
- Private repositories on GitHub Free get a limited monthly allowance of Actions minutes (historically 2,000/month), and each job is billed rounded up to the whole minute. A poll every 5 minutes is ~8,640 job-minutes/month, which exceeds that.
- Public repositories have historically had free, unmetered minutes on standard runners.
- **Verify the current numbers on GitHub's billing docs**, then present the owner with the options:
  - **A. Public repo, poll every 5 min** (recommended for the paper phase). Nothing secret lives in the repo; the paper portfolio and followed wallets being public is harmless. Secrets stay protected in Actions secrets.
  - **B. Private repo, poll every 20–30 min.** Stays within the free allowance; accept more lag (the trader filter in §6 already favours slow traders).
- Make the poll interval a config value (`schedule.poll_minutes`) and make the trader filter's minimum holding time scale with it (§6.3).
- **Live mode must refuse to run from a public repository** (§11.2), so if the owner later goes live they must move to a private repo or another host.
- Note for the owner: GitHub can auto-disable scheduled workflows in repos with no activity for ~60 days. The bot commits state regularly, which counts as activity, but mention it.

### 3.3 Concurrency and state commits
- All workflows that write state share one concurrency group: `concurrency: { group: copybot-state, cancel-in-progress: false }`. Runs queue; they never overlap.
- State is persisted by committing to a dedicated branch **`state`** (never `main`) so code history stays clean. Workflows check out `main` for code and the `state` branch into `./data` (use a second `actions/checkout` with `path: data`, `ref: state`). Create the `state` branch as an orphan branch in Phase 4.
- Commit step: `git add -A && git commit -m "<job> <utc-timestamp>"`; push with up to 3 retries using `git pull --rebase` between attempts. If the push ultimately fails, the job fails loudly (and the next run re-derives from fills; see §7.6).
- Workflow `permissions: contents: write` only. No other permissions.
- Compact the logs monthly (§9.4) so the `state` branch doesn't grow without bound.

### 3.4 Workflows to create (`.github/workflows/`)
| File | Trigger (cron-job.org) | Purpose | Timeout |
|---|---|---|---|
| `refresh.yml` | daily 06:15 UTC | Rebuild the trader shortlist (§6) | 120 min (cold cache; ~30 once the fills cache is warm) |
| `poll.yml` | every `poll_minutes` | Detect trader changes, update all paper portfolios, send event alerts (§7–8, §10.2) | 4 min |
| `report.yml` | daily 07:00 UTC (~08:00 Lisbon summer / 07:00 winter) | Daily email report (§10.1) | 5 min |
| `weekly.yml` | Mondays 07:30 UTC | Weekly deep report + log compaction (§9.4, §10.3) | 10 min |
| `ci.yml` | push / pull_request to `main` | ruff, mypy, pytest | 10 min |
| `live.yml` | **does not exist until the owner asks for it** | See §11 | — |

Each job: `runs-on: ubuntu-latest`, `timeout-minutes` as above, caches the Python environment, runs `python -m copybot <command>`, then the commit step.

### 3.5 cron-job.org setup (write these instructions into `docs/SETUP.md`)
- Owner creates a **fine-grained personal access token** scoped to *only this repository*, permission **Actions: Read and write**, nothing else, with an expiry (e.g., 1 year — note the renewal date in `docs/SETUP.md`).
- For each workflow, a cron-job.org job: `POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/<file>.yml/dispatches`, headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, body `{"ref":"main"}`. Expected response: HTTP 204.
- Enable cron-job.org's failure notifications to the owner's email.

---

## 4. Repository layout

```
copybot/
  __init__.py
  __main__.py            # CLI entry: python -m copybot <command>
  cli.py                 # argparse commands: probe, refresh, poll, report, weekly, backtest, status, kill, unkill
  config.py              # pydantic models for config.yaml, loaded + validated once
  clock.py               # injectable clock (real UTC now / fixed for tests)
  hl/
    client.py            # thin Hyperliquid Info API client (httpx, retries, rate limiting)
    models.py            # pydantic models of API responses we use
    leaderboard.py       # leaderboard fetch + parse
  selection/
    candidates.py        # stage-1 coarse filter (§6.2)
    analytics.py         # per-wallet metrics from fills/portfolio (§6.3)
    copyability.py       # replay-with-lag simulation (§6.4)
    scoring.py           # composite score + hysteresis (§6.5–6.6)
  engine/
    signals.py           # turn fills/snapshots into target-position changes (§7)
    sizing.py            # target notional per copied position (§8.1)
    costs.py             # fees, order-book slippage, funding (§8.3)
    portfolio.py         # Decimal accounting: cash, positions, margin, PnL, liquidation (§8.4)
    risk.py              # portfolio-level limits (§8.2)
    controls.py          # BTC buy-and-hold + random-wallet control portfolios (§9.2)
  state/
    store.py             # load/save JSON state atomically, schema version, migrations
    logs.py              # append-only JSONL writers
  reporting/
    metrics.py           # returns, drawdown, Sharpe, etc. (§9.3)
    charts.py            # PNG charts for email
    email.py             # SMTP sender + rate limiting + templates
    templates/           # jinja2 HTML: daily.html, weekly.html, alert.html
  live/                  # LOCKED module (§11)
    __init__.py
    gates.py
    executor.py
    reconcile.py
scripts/
  probe_api.py           # Phase 1 endpoint verification
  backtest.py            # offline replay (§12)
tests/
  fixtures/              # recorded real API responses (anonymise nothing; data is public)
  ...                    # one test module per source module
docs/
  SETUP.md               # owner's step-by-step setup guide
  API_NOTES.md           # verified endpoint shapes from probe (§5.3)
  DECISIONS.md           # log of design decisions and changes to this spec
config.yaml
CLAUDE.md
README.md
.env.example
.gitignore
pyproject.toml
.pre-commit-config.yaml
```

The `state` branch (checked out at `./data` in Actions) contains:
```
state.json               # see §9.1
shortlist.json           # today's followed wallets + their scores + full metrics
candidates_latest.json   # full scored candidate table from the last refresh (for the report)
logs/
  trades.jsonl           # every paper fill (all portfolios)
  signals.jsonl          # every detected trader change, acted on or skipped, with the reason
  equity.csv             # one row per poll: timestamp, equity of each portfolio
  errors.jsonl           # every handled error
  alerts.jsonl           # every email sent (type, timestamp) for rate limiting
archive/                 # monthly compacted logs (§9.4)
```

---

## 5. Hyperliquid API

### 5.1 Endpoints (verify all of these in Phase 1)
- **Info endpoint:** `POST https://api.hyperliquid.xyz/info`, JSON body with a `type` field. Public, no key. Official docs: https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint (plus the perpetuals sub-page).
- Request types we expect to use:
  - `{"type":"meta"}` / `{"type":"metaAndAssetCtxs"}` — asset list, size decimals, max leverage, current funding, mark/oracle prices.
  - `{"type":"allMids"}` — mid prices for all coins.
  - `{"type":"l2Book","coin":"BTC"}` — order book for slippage modelling.
  - `{"type":"clearinghouseState","user":"0x..."}` — a wallet's open perp positions (`assetPositions[].position` with `coin`, `szi` signed size, `entryPx`, `leverage`, `liquidationPx`, `positionValue`, `marginUsed`, `unrealizedPnl`) and `marginSummary.accountValue`.
  - `{"type":"userFillsByTime","user":"0x...","startTime":ms,"endTime":ms}` — fills in a window (paginated; a response is capped at a fixed number of fills, so loop by advancing `startTime` past the last fill's `time` until the window is exhausted). Fields include `coin`, `px`, `sz`, `side`, `time`, `startPosition`, `dir`, `closedPnl`, `fee`, `crossed`, `hash`, `oid`, `tid`.
  - `{"type":"portfolio","user":"0x..."}` — account value and PnL history series (used for drawdown and consistency).
  - `{"type":"userRole","user":"0x..."}` — whether an address is a normal user, vault, sub-account, or agent. Weight 60 — call it last.
  - `{"type":"subAccounts","user":"0x..."}` — a master wallet's sub-accounts with their `clearinghouseState` (null if none). **Leaderboard rows aggregate a master and its sub-accounts** (verified 2026-10-02, see `docs/DECISIONS.md` D1).
  - Fill history: max 2000 fills per page; only recent history is queryable (docs say 10k fills, observed ~39k). Truncation is silent (D3).
  - `portfolio` has no 90-day window: only day/week/month/allTime (+ perp* variants) (D4).
  - `{"type":"fundingHistory","coin":"BTC","startTime":ms}` — historical funding (backtests and funding accrual cross-check).
- **Leaderboard:** historically served from `https://stats-data.hyperliquid.xyz/Mainnet/leaderboard` (GET). This is **not** part of the documented API and may change or disappear. It has returned `leaderboardRows[]` with `ethAddress`, `accountValue`, `displayName`, and `windowPerformances` (pairs like `["day"|"week"|"month"|"allTime", {"pnl","roi","vlm"}]`). If it is gone, tell the owner and propose an alternative (e.g., a candidate list seeded from large recent fills).
- **Testnet** (live module testing only): `https://api.hyperliquid-testnet.xyz`.

### 5.2 Client requirements (`hl/client.py`)
- One shared `httpx.Client`, 10 s timeout, `User-Agent: copybot/<version>`.
- Retries: 3 attempts with exponential backoff + jitter on timeouts, 429 and 5xx. Never retry 4xx other than 429.
- Client-side rate limiter: Hyperliquid enforces a per-IP request-weight budget of 1200/minute (verified 2026-10-02: weight 2 for l2Book/allMids/clearinghouseState, 60 for userRole, 20 for everything else, plus +1 per 20 fills/funding records returned). Implement a token bucket that stays at ≤ 50% of the documented budget, because GitHub runners share IPs.
- Every response is parsed into a pydantic model. A parse failure raises `ApiSchemaError` → the cycle aborts safely and logs the raw payload (truncated to 5 KB) to `errors.jsonl`.
- Decimal parsing: Hyperliquid returns numbers as strings; parse to `Decimal` directly, never through float.

### 5.3 Phase 1 probe (`scripts/probe_api.py`)
- Calls every request type above with a well-known active address taken from the leaderboard, saves raw responses to `tests/fixtures/`, validates them against the pydantic models, and prints a pass/fail table.
- Writes `docs/API_NOTES.md` with the verified shape of each response, pagination caps, and observed rate-limit behaviour.
- If any field differs from this spec, update the models and this file (log it in `docs/DECISIONS.md`).

---

## 6. Trader selection (`refresh` command, daily)

Goal: find traders who are (a) genuinely and consistently profitable, (b) not market makers, vaults or hedgers, and (c) **copyable with our lag**. Raw leaderboard PnL is never enough on its own.

### 6.1 Output
`data/shortlist.json`: the N followed wallets (default N = 5), each with all metrics, score, rank, date first followed, and the reason for any change vs. yesterday. Also `data/candidates_latest.json` with the full scored table.

### 6.2 Stage 1 — coarse filter (leaderboard data only, cheap)
Keep a wallet only if **all** hold (all thresholds in `config.yaml`):
- `accountValue ≥ 100,000 USD`
- `allTime.pnl > 0`, `month.pnl > 0`, `month.roi > 0`
- `month.vlm ≥ 1,000,000 USD` (actually trades) and `month.vlm / accountValue ≤ 200` (extreme turnover suggests a market maker/HFT)
- Address not on `config.selection.blocklist`
Sort survivors by `month.pnl` and keep the top 150 for stage 2 (configurable). This bounds API usage.

### 6.3 Stage 2 — deep analytics (per candidate, from fills + portfolio)
Process each candidate **cheapest check first, stopping at the first exclusion** (D2): `portfolio` (drawdown, weekly consistency) → `subAccounts` → fills (paginated, at most `max_fill_pages` pages) → fills-based metrics → copyability simulation (§6.4) → `userRole` last. Cache each wallet's fills on the `state` branch and fetch only new fills on later days.

Additional exclusions from the API findings:
- `subaccount_value_share` = sub-account equity / (master + sub-account equity) > `max_subaccount_value_share` (default 0.5) → reason `trades_via_subaccounts` (D1).
- Hitting `max_fill_pages` (default 5 = 10,000 fills, ≥ 111 fills/day over 90 days) → reason `too_active`, no further fetching (D2).
- `userRole` not `user` → reason `not_a_user`.

`max_drawdown_90d` and weekly PnL come from the `perpMonth` series (last ~31 days) joined to `perpAllTime` (older, coarse); when fills cover the window, also build a fills-based equity curve and use the worse drawdown (D4).

Compute:
- `n_trades` (round trips, reconstructed from `startPosition`/`dir`) and `trades_per_day`.
- `median_holding_hours` and `p25_holding_hours`.
- `maker_ratio` = share of fills with `crossed == false`.
- `pnl_90d`, `max_drawdown_90d` (from portfolio history, as % of peak equity), `return_to_drawdown` = 90-day return / max drawdown.
- `positive_weeks_ratio` = share of the last 12 weeks with positive PnL.
- `top_trade_concentration` = largest single round-trip PnL / total positive PnL.
- `avg_leverage` (notional-weighted), `max_leverage` observed.
- `n_coins` traded and `pct_volume_in_supported_coins` (perp coins in `meta`; we do not copy spot).
- `active_days_last_30`.

Hard exclusions (configurable):
- `trades_per_day > 30` (too fast to copy)
- `median_holding_hours < max(4, 12 × poll_minutes / 60)` — holding time must be far longer than our polling gap
- `maker_ratio > 0.6` (likely market making)
- `max_drawdown_90d > 40%`
- `top_trade_concentration > 0.5` (one lucky trade)
- `positive_weeks_ratio < 0.5`
- `active_days_last_30 < 8`
- `max_leverage > 25`
- fewer than 20 round trips in 90 days (not enough evidence)

### 6.4 Stage 3 — copyability simulation (the most important filter)
For each survivor, replay their last 90 days of fills **as our bot would have seen them**: each fill becomes visible to us only at the next poll boundary after its timestamp (using the configured `poll_minutes` plus a random 0–3 min extra delay to mimic Actions jitter). Execute the copy at the historical mid price at that later moment (use the candle/fill data available; document the approximation in `docs/DECISIONS.md`) plus modelled fees and fixed slippage (`costs.backtest_slippage_bps`). Size with the §8.1 rules on a notional $10,000 sleeve.

Outputs: `copy_return_90d`, `copy_max_drawdown`, `copy_capture_ratio` = copy_return / trader_return over the same period. Exclude if `copy_return_90d ≤ 0` or `copy_capture_ratio < 0.4`.

### 6.5 Composite score
Rank survivors by a weighted z-score (weights in config, defaults):
- `copy_return_90d` 0.35
- `return_to_drawdown` 0.25
- `positive_weeks_ratio` 0.20
- `copy_capture_ratio` 0.10
- `−avg_leverage` 0.10
Winsorise each metric at the 5th/95th percentile before z-scoring.

### 6.6 Hysteresis (avoid churn)
- A currently followed wallet stays followed as long as it is in the top `2N` (default 10) **and** still passes all hard exclusions.
- A new wallet joins only if it is in the top N.
- At most 2 wallet replacements per day.
- When a wallet is dropped: its copied paper positions are closed at the next poll (reason `trader_unfollowed`), not held indefinitely.
- If fewer than N wallets qualify, follow fewer; the unused sleeve stays in cash. If zero qualify, send an alert and hold cash.

### 6.7 Random control set
From the stage-1 survivors (not the final ranking), pick N random wallets with a seed derived from the date, also passing only the §6.3 holding-time and trades-per-day exclusions (so they are copyable in principle). This feeds the random-control portfolio (§9.2). Re-draw monthly, not daily.

---

## 7. Signal detection (`poll` command)

### 7.1 Inputs per followed wallet
- Fills since `last_fill_time_ms` for that wallet (paginate; overlap the window by 60 s and de-duplicate by `tid`).
- Current `clearinghouseState` snapshot.

### 7.2 Target-position logic
The trader's **current snapshot is the target**; fills are used for timing, entry-price comparison and diagnostics. For each coin, compute the trader's *exposure fraction* = signed position notional / trader accountValue. Our target for that coin in that wallet's sleeve follows from §8.1.
Changes are classified as: `OPEN`, `INCREASE`, `DECREASE`, `CLOSE`, `FLIP` (sign change → close then open).

### 7.3 Safety rules (fail safe)
- If the snapshot request fails or fails validation for a wallet, **skip that wallet this cycle** (no signals at all, especially no closes). Count consecutive failures; alert after 3.
- If a coin disappears from the snapshot **but no closing fills exist** for it since the last poll, treat it as suspicious: wait one more poll before closing, unless the next snapshot confirms.
- Ignore spot balances and anything not in the perp `meta` universe.
- Ignore changes whose effect on our target is below `rebalance_threshold` (default: 10% of the current copied size, or $15 notional, whichever is larger).

### 7.4 Late-entry protection
For `OPEN`/`INCREASE`: compute `drift = (current_mid − trader_avg_entry_px) / trader_avg_entry_px` in the trade's direction. If the price has already moved more than `max_entry_drift_pct` (default 1.5%) in the trader's favour, **skip** the entry and log `skipped_late_entry` (we'd be buying after the move). Closes and decreases are never skipped.

### 7.5 Missed runs
If the gap since the last successful poll exceeds `3 × poll_minutes`, log a `poll_gap` event, process normally (fills catch us up), and include the gap count in the daily report. If it exceeds 6 hours, also send an alert.

### 7.6 Idempotency
Every processed fill `tid` and every executed paper order gets a deterministic ID; re-running a poll on the same data must produce zero new trades. Test this explicitly.

---

## 8. Sizing, risk and the paper engine

### 8.1 Sizing (per copied position)
- Paper starting equity: **$10,000** (`portfolio.start_equity`).
- Equal sleeves: `sleeve = current_total_equity / N_followed` (recomputed each poll).
- `target_notional = sleeve × trader_exposure_fraction × copy_multiplier` (default multiplier 1.0).
- Clamp `|target_notional| ≤ max_position_pct × total_equity` (default 20%).
- Ignore targets below Hyperliquid's minimum order notional (verify; historically ~$10) — log `skipped_min_size`.
- Round sizes to the coin's `szDecimals` from `meta`.

### 8.2 Portfolio risk limits (`engine/risk.py`), checked before every paper order
- Gross exposure ≤ `max_gross_leverage × equity` (default 3×). If exceeded, scale all new targets down proportionally.
- Net exposure per coin across all sleeves ≤ `max_coin_pct` of equity (default 35%).
- Per-position effective leverage ≤ `max_position_leverage` (default 5×), regardless of what the trader uses.
- **Daily loss stop:** if equity falls more than `daily_loss_stop_pct` (default 5%) from the day's start (00:00 UTC), close everything and pause new entries until the next UTC day. Alert.
- **Max drawdown stop:** if equity falls more than `max_drawdown_stop_pct` (default 25%) from its all-time peak, close everything and pause until the owner runs `python -m copybot unkill`. Alert.
- **Kill switch:** if a file named `KILL_SWITCH` exists at the root of the `state` branch, close everything and take no new positions. `python -m copybot kill` creates it, `unkill` removes it. Document both in `docs/SETUP.md`.
These limits apply identically to paper and live; live adds more (§11.3).

### 8.3 Cost model (`engine/costs.py`) — realism is the point
- **Fees:** taker fee on every paper order (we always assume taker). Default `taker_fee_bps: 4.5` — verify the current base-tier rate on Hyperliquid's fee docs and update config.
- **Slippage:** walk the live `l2Book` for the coin to compute the volume-weighted fill price for our size; add `extra_slippage_bps` (default 2) for latency between snapshot and execution. If the book cannot be fetched, use `fallback_slippage_bps` (default 10).
- **Funding:** Hyperliquid funding is paid hourly. At each poll, accrue funding on every open paper position for the elapsed time using the current funding rate from `metaAndAssetCtxs` (pro-rated). Longs pay positive funding, shorts receive it. Log funding separately.
- **Lag cost:** for every copied entry, record `our_fill_px − trader_fill_px` in bps (direction-adjusted). Report it — this is the price we pay for copying instead of trading.

### 8.4 Paper portfolio accounting (`engine/portfolio.py`)
- Cross-margin model. Track: cash (USDC), positions (coin, signed size, avg entry, accumulated funding, opened_at, source wallet, sleeve), realized PnL, fees paid, funding paid/received.
- Mark-to-market every poll at mid price. Equity = cash + Σ unrealized PnL.
- **Liquidation:** compute a maintenance margin per position using the coin's max leverage from `meta` (maintenance ≈ half of initial at max leverage — verify against Hyperliquid docs). If account equity < total maintenance margin, liquidate positions largest-loss-first at mark minus slippage, and alert. With our leverage caps this should never trigger; if it does, that is a bug or an extreme event and must be investigated.
- Every fill appended to `trades.jsonl` with: portfolio id, order id, timestamp, coin, side, size, price, fee, slippage_bps, reason (`copy_open`, `copy_increase`, `copy_decrease`, `copy_close`, `copy_flip`, `trader_unfollowed`, `risk_scale_down`, `daily_stop`, `drawdown_stop`, `kill_switch`, `liquidation`), source wallet, trader's own fill price.
- Accounting invariants checked after every poll (assert, and abort + alert if violated): equity == cash + Σ uPnL; no position with size 0; no NaN/negative prices; sum of realized PnL in logs == realized PnL in state.

---

## 9. State, controls and metrics

### 9.1 `state.json`
Versioned (`schema_version`), written atomically (write temp file then rename). Contains: last successful poll time, per-wallet `last_fill_time_ms` and consecutive error counts, followed wallets, each portfolio's full state (cash, positions, peak equity, day-start equity, paused flags), the random-control wallet set and its draw date, processed fill-id window (last 7 days of `tid`s for idempotency).

### 9.2 Three portfolios, run side by side on the same data every poll
1. **`copy`** — the strategy.
2. **`btc_hold`** — $10,000 of BTC bought at the start, never traded (no leverage, mark-to-market, no funding since it represents spot-equivalent exposure; document this choice).
3. **`random_copy`** — the identical engine, sizing, costs and risk rules, following the §6.7 random wallets.
All three start on the same day with the same equity. If the strategy can't beat both, it has no demonstrated edge.

### 9.3 Metrics (`reporting/metrics.py`)
For each portfolio, over 1d / 7d / 30d / since start: return %, max drawdown %, annualised volatility, Sharpe (daily returns, risk-free 0), win rate and average win/loss by closed trade, profit factor, number of trades, fees paid, funding net, average lag cost (bps), exposure (average gross leverage). Plus per-followed-wallet attribution: PnL contributed by each sleeve.

### 9.4 Log compaction (weekly job)
On the first weekly run of each month, move the previous month's `trades.jsonl`, `signals.jsonl` and `errors.jsonl` entries into `archive/YYYY-MM/` as gzipped files, and downsample `equity.csv` older than 30 days to hourly rows. Never delete data.

---

## 10. Email reporting (`reporting/email.py`)

### 10.1 Daily report (07:00 UTC)
Subject: `copybot daily — copy +X.X% | BTC +Y.Y% | random +Z.Z% (24h)`. HTML body plus a plain-text alternative. Sections, in order:
1. **Headline table:** the three portfolios' equity, 24h / 7d / since-start return, max drawdown.
2. **Equity chart** (PNG inline via CID): all three portfolios since start.
3. **Open positions** of `copy`: coin, side, size, notional, entry, mark, uPnL, source wallet, held for.
4. **Yesterday's activity:** trades executed, signals skipped by reason (late entry, min size, risk limit), fees, funding, average lag cost.
5. **Followed traders:** address (shortened + link to a Hyperliquid explorer page), score, days followed, sleeve PnL, any change from the refresh with the reason.
6. **Health:** polls run vs. expected, gaps, API errors, any pauses or stops active, kill-switch status.
7. **Live-readiness checklist** (§11.1 gate criteria), each marked met/not met. Informational only.
8. A short fixed footer: "Paper trading. Not financial advice. Past copy performance does not predict future results."

### 10.2 Event alerts (sent from the poll job, immediately)
Types: `daily_stop`, `drawdown_stop`, `kill_switch_engaged`, `liquidation`, `invariant_violation`, `api_failing` (3+ consecutive failures), `poll_gap_6h`, `no_qualifying_traders`, `trader_changed` (batched into one email per refresh), and in live mode the §11 alerts.
Subject prefix: `[copybot ALERT] <type>`. Rate limit: max 1 email per alert type per hour and max 6 alert emails per day in total (except `invariant_violation` and live-mode alerts, which always send). Track sends in `alerts.jsonl`.

### 10.3 Weekly report (Mondays)
Everything in the daily report over 7 days, plus: per-wallet attribution, distribution of lag costs, skipped-signal analysis (would the skipped trades have made money?), turnover of the followed list, and a comparison of the selection score vs. realised performance of followed traders.

### 10.4 SMTP configuration
Secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO`. Default instructions in `docs/SETUP.md` for Gmail: enable 2-step verification, create an **App Password**, use `smtp.gmail.com:587` with STARTTLS. Never the main account password. If sending fails, log to `errors.jsonl` and don't fail the trading part of the job.

---

## 11. Live-trading module — LOCKED OFF

You build this module, test it on **testnet only** with mocks/fixtures, and leave it disabled. You do not create `live.yml`, do not create live secrets, and do not run anything against mainnet with a key.

### 11.1 Live-readiness criteria (computed daily, shown in the report)
- ≥ 60 days of uninterrupted paper trading
- `copy` beats both `btc_hold` and `random_copy` over the full period, net of all costs
- `copy` max drawdown ≤ 20%
- ≥ 50 closed paper trades
- no invariant violations in the last 30 days
The gate `live.require_readiness` (default `true`) blocks live trading until all are met. Overriding it requires the owner to set `live.require_readiness: false` *and* `live.override_acknowledged: "I understand the paper results do not support live trading"` in config.

### 11.2 Independent gates — ALL must pass or the live module exits immediately without connecting
1. `config.yaml` → `live.enabled: true` (ships `false`).
2. Environment variable `COPYBOT_LIVE=1` set in the workflow.
3. Secret `HL_API_WALLET_PRIVATE_KEY` present **and** `HL_ACCOUNT_ADDRESS` present.
4. `live.network` explicitly set to `testnet` or `mainnet` (no default); mainnet additionally requires `live.mainnet_confirmed: true`.
5. The repository is **private** (check `github.event.repository.private` / the GitHub API in the workflow; refuse if public).
6. No `KILL_SWITCH` file.
7. Readiness criteria met (§11.1) unless explicitly overridden.
8. A startup check that the account address's equity is ≤ `live.max_account_equity_usd` (default 500). If the account holds more, refuse — the live account must be a small, dedicated one.
Each failing gate is logged with its name. Write tests proving each gate alone blocks execution.

### 11.3 Live-specific design
- **API (agent) wallet only.** Hyperliquid supports API wallets authorised by the main account that can trade but cannot withdraw. Document in `docs/SETUP.md` how the owner creates one in the Hyperliquid UI and authorises it, and that the main wallet's key is never given to the bot. Recommend a dedicated sub-account funded only with money the owner can afford to lose entirely.
- The live executor **mirrors the `copy` paper portfolio's target positions**, it does not make its own decisions. Each poll: compute the diff between the paper `copy` targets (scaled by `live.capital_fraction` of live equity / paper equity) and actual live positions, then place orders.
- Orders: limit IOC at mid ± `live.max_slippage_bps` (default 20). Never plain market orders. Closes and decreases use `reduce_only`.
- Leverage: set per-coin leverage to `min(our target leverage, live.max_leverage)` (default 3), **isolated** margin by default (configurable).
- Extra live limits: `live.max_order_usd` (default 100), `live.max_total_exposure_usd` (default 1,000), `live.max_orders_per_poll` (default 10), `live.daily_loss_stop_usd` (default 50). Any breach → cancel open orders, close positions with reduce-only IOC orders, set a live-only kill flag, alert.
- **Reconciliation** (`live/reconcile.py`) every poll before trading: fetch actual live positions and open orders; if they differ from the expected state by more than tolerance (e.g., a manual trade by the owner, a partial fill), do not trade this poll, log the diff and alert. Never "fix" an unexpected position automatically except to reduce exposure.
- Every live order request and response is logged to `data/logs/live_orders.jsonl` with the private key and signatures stripped.
- Live alerts (always sent, not rate-limited): every executed order (batched per poll), any rejected order, reconciliation mismatch, any live limit breach, gate failures when `live.enabled` is true.
- A `--dry-run` flag that computes and logs the exact orders it would place without signing or sending anything. This is how live mode is tested on mainnet data.

### 11.4 What the owner does to go live (write in `docs/SETUP.md`, clearly marked "only after the readiness checklist is green")
Move to a private repo or other host, create the API wallet, add secrets, run testnet for ≥ 2 weeks, run mainnet `--dry-run` for ≥ 1 week, then set the flags. Also: "Check that using Hyperliquid and leveraged perpetual futures is legal for you where you live, and understand you can lose your entire deposit."

---

## 12. Backtest tool (`scripts/backtest.py`)
Offline replay over historical fills of a chosen set of wallets and date range, using the same `engine/` code (no duplicate logic), the §6.4 lag model and the §8.3 costs (fixed slippage, historical funding). Output: metrics table + equity chart PNG + trades CSV in `./backtests/<timestamp>/` (gitignored). Use it to sanity-check the selection thresholds; document results in `docs/DECISIONS.md`. Be explicit in the output that backtests over wallets chosen with today's leaderboard suffer survivorship bias.

---

## 13. Quality bar

- **Tests:** ≥ 85% line coverage on `engine/`, `selection/`, `live/`. Must include:
  - accounting invariants under random sequences of fills (property-style test with a fixed seed);
  - idempotency (same poll twice → no new trades);
  - API failure → no closes, no new trades;
  - disappearing position without fills → no immediate close;
  - late-entry skip; min-size skip; risk scale-down; daily stop; drawdown stop; kill switch;
  - liquidation math against a hand-calculated example;
  - funding accrual sign for longs and shorts;
  - order-book slippage against a recorded `l2Book` fixture;
  - hysteresis and the max-2-replacements rule;
  - every live gate individually blocking execution; live `--dry-run` never calling the signing/exchange path (assert with a mock that raises if called).
- All network calls in tests are mocked with recorded fixtures. Tests never hit the real API.
- `ruff check`, `ruff format --check`, `mypy --strict copybot/` all clean in CI.
- `.pre-commit-config.yaml`: ruff, a secrets scanner (e.g., `detect-secrets` or `gitleaks`), and a check that `config.yaml` has `live.enabled: false`.
- Structured logging (JSON lines to stdout in Actions) with a run ID per job.
- Every config value has a comment in `config.yaml` explaining it and its default.

---

## 14. `config.yaml` (create with exactly these keys and defaults; comment each one)

```yaml
schedule:
  poll_minutes: 5            # must match the cron-job.org interval; see §3.2
portfolio:
  start_equity: 10000
  copy_multiplier: 1.0
selection:
  n_follow: 5
  stage2_pool: 150
  lookback_days: 90
  min_account_value: 100000
  min_month_volume: 1000000
  max_turnover_ratio: 200
  max_trades_per_day: 30
  min_median_holding_hours: 4      # effective value = max(this, 12 * poll_minutes / 60)
  max_maker_ratio: 0.6
  max_drawdown_pct: 40
  max_top_trade_concentration: 0.5
  min_positive_weeks_ratio: 0.5
  min_active_days_30: 8
  max_leverage_seen: 25
  min_round_trips: 20
  max_subaccount_value_share: 0.5
  max_fill_pages: 5
  min_copy_capture_ratio: 0.4
  weights: {copy_return: 0.35, return_to_drawdown: 0.25, positive_weeks: 0.20, capture: 0.10, low_leverage: 0.10}
  keep_if_rank_within: 10
  max_replacements_per_day: 2
  blocklist: []
signals:
  rebalance_threshold_pct: 10
  rebalance_threshold_usd: 15
  max_entry_drift_pct: 1.5
  disappear_confirm_polls: 1
risk:
  max_position_pct: 20
  max_gross_leverage: 3
  max_coin_pct: 35
  max_position_leverage: 5
  daily_loss_stop_pct: 5
  max_drawdown_stop_pct: 25
costs:
  taker_fee_bps: 4.5          # verify current rate
  extra_slippage_bps: 2
  fallback_slippage_bps: 10
  backtest_slippage_bps: 8
email:
  max_alerts_per_type_per_hour: 1
  max_alerts_per_day: 6
  timezone_display: Europe/Lisbon
live:
  enabled: false
  network: null               # must be set explicitly to testnet or mainnet
  mainnet_confirmed: false
  require_readiness: true
  override_acknowledged: ""
  max_account_equity_usd: 500
  capital_fraction: 1.0
  max_leverage: 3
  margin_mode: isolated
  max_slippage_bps: 20
  max_order_usd: 100
  max_total_exposure_usd: 1000
  max_orders_per_poll: 10
  daily_loss_stop_usd: 50
```

---

## 15. Build order (phases) — stop and summarise to the owner after each

1. **Scaffold + API probe.** Repo layout, `pyproject.toml`, CI, pre-commit, `hl/client.py`, `hl/models.py`, `scripts/probe_api.py`, fixtures, `docs/API_NOTES.md`. *Done when:* probe passes against the real API and CI is green.
2. **Selection.** Stages 1–3, scoring, hysteresis, random control set; `python -m copybot refresh --local` writes `shortlist.json` locally. *Done when:* tests pass and the owner has reviewed one real shortlist with its metrics table.
3. **Engine + controls.** Signals, sizing, costs, portfolio, risk, three portfolios; `python -m copybot poll --local` runs repeatedly against the real API with local state. Backtest script. *Done when:* invariant/idempotency/failure tests pass and a 2-hour local run produces sane logs.
4. **GitHub Actions + state branch.** Explain §3.2 to the owner and get their choice of public/private and interval. Workflows, orphan `state` branch, commit logic, concurrency. Guide the owner through secrets and cron-job.org. *Done when:* 24 hours of scheduled runs complete with no overlaps and no push failures.
5. **Email.** Daily, weekly and alert emails with charts; rate limiting; `python -m copybot report --preview` writes the HTML to a local file for review. *Done when:* the owner receives and approves a real daily email.
6. **Live module (locked).** Gates, executor, reconciliation, dry-run, testnet tests with mocks. *Done when:* all gate tests pass, `live.enabled` is `false`, `live.yml` does not exist, and `docs/SETUP.md` has the go-live section.
7. **Hardening.** Weekly report, log compaction, `status` command (prints current portfolios, followed traders, health to the terminal), README with architecture diagram (Mermaid), final review of this file vs. the code.

---

## 16. Things to tell the owner proactively
- The Actions-minutes trade-off (§3.2) before Phase 4.
- Any API field or endpoint that differs from this spec.
- If the leaderboard endpoint is unavailable.
- If, during Phase 2, very few wallets survive the filters — propose which thresholds to relax and show the effect, rather than silently loosening them.
- That the fine-grained GitHub token and the Gmail App Password expire/can be revoked, and where to renew them.
- That results from the first weeks are noisy; meaningful conclusions need the full 60-day readiness window.
