#!/usr/bin/env python3
"""
fetch_odds.py — pulls NRL odds from every AU book and writes nrl.json.

Runs in GitHub Actions. Reads the key from the ODDS_API_KEY secret, never
from a file. Writes a single JSON that Claude reads over raw.githubusercontent.

Why every book and not just Sportsbet: the edge is in disagreement. Betfair's
back/lay gives a near-vig-free probability, and the consensus line across the
field is what reveals when one book is an outlier. A single book's prices,
with nothing to compare them to, tell you nothing.

Quota: the core call costs len(markets) x len(regions) credits. Props are
charged per event at the same rate, so they're only fetched for games starting
inside PROPS_WINDOW_HOURS. Skipped entirely if the budget looks tight.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys

import requests

BASE = "https://api.the-odds-api.com/v4"
SPORT = "rugbyleague_nrl"
REGIONS = os.environ.get("REGIONS", "au").strip()
CORE_MARKETS = "h2h,spreads,totals"

# Betfair only appears if you ask for the exchange lay market explicitly.
# This is the single most valuable field in the whole payload.
LAY_MARKET = "h2h_lay"

PROP_MARKETS = [
    "player_try_scorer_anytime",
    "player_try_scorer_first",
    "player_try_scorer_last",
]

PROPS_WINDOW_HOURS = float(os.environ.get("PROPS_WINDOW", "8"))
FETCH_PROPS = os.environ.get("FETCH_PROPS", "true").lower() != "false"
MIN_CREDITS_FOR_PROPS = 200  # don't burn the last of the quota on props
OUT = "nrl.json"


def get(path: str, params: dict) -> tuple[object | None, dict]:
    try:
        r = requests.get(f"{BASE}{path}", params=params, timeout=25)
    except requests.RequestException as e:
        print(f"  request failed: {e}")
        return None, {}
    if r.status_code != 200:
        print(f"  HTTP {r.status_code}: {r.text[:160]}")
        return None, r.headers
    return r.json(), r.headers


def main() -> int:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    if not key:
        print("ODDS_API_KEY not set — add it under Settings > Secrets > Actions")
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    print(f"Fetching NRL odds at {now:%Y-%m-%d %H:%M} UTC")

    games, headers = get(
        f"/sports/{SPORT}/odds",
        {
            "apiKey": key,
            "regions": REGIONS,
            "markets": f"{CORE_MARKETS},{LAY_MARKET}",
            "oddsFormat": "decimal",
        },
    )
    if games is None:
        print("Core fetch failed — leaving the previous nrl.json untouched.")
        return 1

    remaining = headers.get("x-requests-remaining", "?")
    used = headers.get("x-requests-used", "?")
    print(f"  {len(games)} events | quota remaining {remaining}, used {used}")

    # ---- props, only for games kicking off soon ----------------------------
    props_added, props_market = 0, None
    try:
        budget_ok = int(remaining) >= MIN_CREDITS_FOR_PROPS
    except (TypeError, ValueError):
        budget_ok = False

    if budget_ok and FETCH_PROPS:
        soon = [
            g
            for g in games
            if 0
            <= (
                dt.datetime.fromisoformat(g["commence_time"].replace("Z", "+00:00"))
                - now
            ).total_seconds()
            <= PROPS_WINDOW_HOURS * 3600
        ]
        print(f"  {len(soon)} game(s) inside the {PROPS_WINDOW_HOURS}h props window")

        for g in soon:
            for mkt in PROP_MARKETS:
                data, _ = get(
                    f"/sports/{SPORT}/events/{g['id']}/odds",
                    {
                        "apiKey": key,
                        "regions": REGIONS,
                        "markets": mkt,
                        "oddsFormat": "decimal",
                    },
                )
                if data and data.get("bookmakers"):
                    g.setdefault("bookmakers", []).extend(data["bookmakers"])
                    props_added += 1
                    props_market = mkt
                    break  # one working prop market per game is enough
    elif not FETCH_PROPS:
        print("  props disabled for this run")
    else:
        print(f"  skipping props (quota {remaining} below {MIN_CREDITS_FOR_PROPS})")

    sharp = sorted({
        b["key"]
        for g in games
        for b in g.get("bookmakers", [])
        if b["key"] in ("betfair_ex_au", "pinnacle")
    })
    print(f"  sharp references present: {sharp or 'NONE — fair values will be weak'}")

    payload = {
        "meta": {
            "fetched_at": now.isoformat(),
            "fetched_at_aest": (now + dt.timedelta(hours=10)).strftime(
                "%I:%M %p AEST, %a %d %b"
            ),
            "events": len(games),
            "quota_remaining": remaining,
            "quota_used": used,
            "props_fetched_for": props_added,
            "props_market_used": props_market,
            "regions": REGIONS,
            "sharp_refs": sharp,
            "markets": f"{CORE_MARKETS},{LAY_MARKET}",
        },
        "events": games,
    }

    with open(OUT, "w") as f:
        json.dump(payload, f, separators=(",", ":"))
    print(f"  wrote {OUT} ({os.path.getsize(OUT):,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
