# copybot

A deterministic Hyperliquid copy-trading bot. It picks consistently profitable, *copyable*
traders from the public leaderboard and mirrors them in a **paper portfolio** with realistic
costs, next to two control portfolios (BTC buy-and-hold, and copying random traders). The live
module is built but locked off.

**Status:** Phase 1 of 7 (scaffold + API probe). See `CLAUDE.md` for the full spec,
`docs/SETUP.md` to run it, `docs/API_NOTES.md` for verified API shapes and
`docs/DECISIONS.md` for design decisions.

```bash
uv sync
```

```bash
uv run pytest
```

Paper trading. Not financial advice.
