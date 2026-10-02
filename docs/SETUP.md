# copybot setup guide (owner)

Sections are added phase by phase. Anything marked *later* isn't needed yet.

## 1. Local development (Phase 1)

You need [`uv`](https://docs.astral.sh/uv/) (already installed on your Mac). It installs
Python 3.12 for this project automatically.

```bash
uv sync
```

```bash
uv run pytest
```

```bash
uv run python -m copybot probe
```

The probe calls the real Hyperliquid API (read-only, public data, no keys), refreshes
`tests/fixtures/` and rewrites `docs/API_NOTES.md`. It takes about a minute because it respects
the rate limit.

Optional, enables the commit-time checks (lint, secret scanner, live-disabled guard):

```bash
uv run pre-commit install
```

## 2. GitHub repository and Actions — *later (Phase 4)*
## 3. cron-job.org triggers — *later (Phase 4)*
## 4. Email (Gmail App Password) — *later (Phase 5)*
## 5. Kill switch — *later (Phase 3)*
## 6. Going live — *later (Phase 6), only after the readiness checklist is green*
