"""Leaderboard fetch + parse. The endpoint is undocumented; see docs/API_NOTES.md."""

from __future__ import annotations

from copybot.hl.client import InfoClient
from copybot.hl.models import LeaderboardRow


def fetch_leaderboard(client: InfoClient) -> list[LeaderboardRow]:
    rows = client.leaderboard().leaderboardRows
    # Normalise addresses so blocklist/state comparisons are case-insensitive.
    return [r.model_copy(update={"ethAddress": r.ethAddress.lower()}) for r in rows]
