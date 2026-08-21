"""
Baseball Savant (Statcast) wrapper — expected stats not available in the MLB
Stats API.  Provides per-batter xBA (est_ba) and xwOBA (est_woba), which measure
quality of contact and are more predictive of future hitting than raw AVG.

The whole-league leaderboard is fetched once per season and cached, then looked
up by MLB player_id (shared with the Stats API), so it costs one request per run.
"""
import csv
import io
import requests

from config import CURRENT_SEASON

_XSTATS_CACHE = {}    # season -> {player_id: {est_ba, ba, est_woba, woba}}
_PXSTATS_CACHE = {}   # season -> {player_id: {est_woba, woba, era, xera}}

_LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/expected_statistics"


def get_batter_xstats(season=None):
    """
    Return {player_id: {est_ba, ba, est_woba, woba}} for all qualified batters.
    Cached per season; empty dict (graceful) if Savant is unreachable.
    """
    season = season or CURRENT_SEASON
    if season in _XSTATS_CACHE:
        return _XSTATS_CACHE[season]

    result = {}
    try:
        r = requests.get(
            _LEADERBOARD_URL,
            params={"type": "batter", "year": str(season), "min": "10", "csv": "true"},
            timeout=20,
        )
        r.raise_for_status()
        text = r.text.lstrip("﻿")   # strip BOM
        for row in csv.DictReader(io.StringIO(text)):
            try:
                pid = int(row["player_id"])
            except (KeyError, ValueError, TypeError):
                continue
            result[pid] = {
                "est_ba":   _f(row.get("est_ba")),
                "ba":       _f(row.get("ba")),
                "est_woba": _f(row.get("est_woba")),
                "woba":     _f(row.get("woba")),
            }
    except Exception as e:
        print(f"  [Savant] expected-stats fetch failed: {e}")

    _XSTATS_CACHE[season] = result
    return result


def get_pitcher_xstats(season=None):
    """
    Return {player_id: {est_woba, woba, xera, era, bip}} for all qualified pitchers.
    est_woba / xera are quality-of-contact expected stats ALLOWED by the pitcher —
    luck-stripped run prevention, more predictive than raw ERA.  Cached per season.
    """
    season = season or CURRENT_SEASON
    if season in _PXSTATS_CACHE:
        return _PXSTATS_CACHE[season]

    result = {}
    try:
        r = requests.get(
            _LEADERBOARD_URL,
            params={"type": "pitcher", "year": str(season), "min": "10", "csv": "true"},
            timeout=20,
        )
        r.raise_for_status()
        text = r.text.lstrip("﻿")
        for row in csv.DictReader(io.StringIO(text)):
            try:
                pid = int(row["player_id"])
            except (KeyError, ValueError, TypeError):
                continue
            result[pid] = {
                "est_woba": _f(row.get("est_woba")),
                "woba":     _f(row.get("woba")),
                "xera":     _f(row.get("xera")),
                "era":      _f(row.get("era")),
                "bip":      _f(row.get("bip")),
            }
    except Exception as e:
        print(f"  [Savant] pitcher expected-stats fetch failed: {e}")

    _PXSTATS_CACHE[season] = result
    return result


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
