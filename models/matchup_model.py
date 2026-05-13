"""
Matchup model: platoon splits, batter vs pitcher history, NRFI projection.
"""
from scipy.stats import poisson
from data import mlb_api
from config import CURRENT_SEASON

LEAGUE_AVG_OPS  = 0.720
LEAGUE_AVG_ERA  = 4.20
FIRST_INN_MULT  = 1.15   # top of lineup bats in 1st; historically ~15% more runs/inning
MIN_BVP_PA      = 8      # minimum PA to apply BvP adjustment
MAX_BVP_WEIGHT  = 0.40   # cap BvP influence at 40% (at 30+ PA)

# Historical platoon multipliers on OPS (batter_hand, pitcher_hand)
# Same hand = pitcher advantage; opposite = batter advantage
_PLATOON = {
    ("R", "R"): 0.965,
    ("R", "L"): 1.045,
    ("L", "L"): 0.945,
    ("L", "R"): 1.060,
    ("S", "R"): 1.015,   # switch hitter bats left vs RHP
    ("S", "L"): 1.015,   # switch hitter bats right vs LHP
}


# ── Public entry point ────────────────────────────────────────────────────────

def get_matchup_data(game):
    """
    Fetch lineup, platoon splits, and BvP for a game.
    Returns matchup dict used by game_model and betting_engine.
    """
    lineup = mlb_api.get_lineup(game["game_pk"])
    home_ids = lineup.get("home", [])
    away_ids = lineup.get("away", [])

    if not home_ids or not away_ids:
        return _default()

    home_sp_id = game.get("home_pitcher_id")
    away_sp_id = game.get("away_pitcher_id")

    home_hand = _hand(home_sp_id)
    away_hand = _hand(away_sp_id)

    home_sp_season = mlb_api.get_pitcher_season_stats(home_sp_id) or {} if home_sp_id else {}
    away_sp_season = mlb_api.get_pitcher_season_stats(away_sp_id) or {} if away_sp_id else {}

    home_platoon = mlb_api.get_pitcher_platoon_splits(home_sp_id) if home_sp_id else {}
    away_platoon = mlb_api.get_pitcher_platoon_splits(away_sp_id) if away_sp_id else {}

    # Away batters face the home SP; home batters face the away SP
    away_factor = _lineup_factor(away_ids, home_sp_id, home_hand,
                                  home_sp_season, home_platoon, top_n=9)
    home_factor = _lineup_factor(home_ids, away_sp_id, away_hand,
                                  away_sp_season, away_platoon, top_n=9)

    # NRFI: only top-3 batters are near-guaranteed to face the SP in the 1st
    nrfi_away = _lineup_factor(away_ids, home_sp_id, home_hand,
                                home_sp_season, home_platoon, top_n=3)
    nrfi_home = _lineup_factor(home_ids, away_sp_id, away_hand,
                                away_sp_season, away_platoon, top_n=3)

    return {
        "has_lineup":     True,
        "home_factor":    round(home_factor, 4),
        "away_factor":    round(away_factor, 4),
        "nrfi_home":      round(nrfi_home,   4),
        "nrfi_away":      round(nrfi_away,   4),
        "home_pitcher_hand": home_hand,
        "away_pitcher_hand": away_hand,
        "home_lineup":    home_ids,
        "away_lineup":    away_ids,
    }


def project_nrfi(home_runs, away_runs, nrfi_home, nrfi_away,
                  park_factor, weather_factor):
    """
    P(NRFI) = P(away scores 0 in top 1st) x P(home scores 0 in bottom 1st).
    Uses Poisson with expected 1st-inning runs per side.
    """
    away_1st = (away_runs / 9) * FIRST_INN_MULT * nrfi_away * park_factor * weather_factor
    home_1st = (home_runs / 9) * FIRST_INN_MULT * nrfi_home * park_factor * weather_factor

    p_away_0 = poisson.pmf(0, max(0.05, away_1st))
    p_home_0 = poisson.pmf(0, max(0.05, home_1st))
    return max(0.10, min(0.90, p_away_0 * p_home_0))


# ── Lineup quality factor ─────────────────────────────────────────────────────

def _lineup_factor(batter_ids, pitcher_id, pitcher_hand,
                   pitcher_season, pitcher_platoon, top_n=9):
    """
    Aggregate batting-quality multiplier for `top_n` batters vs this pitcher.
    Combines:
      1. Pitcher platoon split (ERA vs LHB / RHB relative to overall ERA)
      2. BvP career history for each batter (blended by sample-size weight)
    """
    ids = batter_ids[:top_n]
    if not ids:
        return 1.0

    overall_era = pitcher_season.get("era", LEAGUE_AVG_ERA)
    if overall_era <= 0:
        overall_era = LEAGUE_AVG_ERA

    factors = []
    for bid in ids:
        f = _batter_factor(bid, pitcher_id, pitcher_hand,
                           overall_era, pitcher_platoon)
        factors.append(f)

    # Geometric mean across batters
    prod = 1.0
    for f in factors:
        prod *= f
    return max(0.75, min(1.30, prod ** (1.0 / len(factors))))


def _batter_factor(batter_id, pitcher_id, pitcher_hand,
                   pitcher_overall_era, pitcher_platoon):
    """
    Individual batter quality factor vs this pitcher (1.0 = league-average matchup).

    Platoon component:
      - Look up pitcher ERA vs this batter's handedness
      - If pitcher is weak vs that hand → factor > 1.0 (good for batter)

    BvP component:
      - If batter has 8+ career PA vs this pitcher, blend career OPS
        into season OPS, weighted by sample size (max 40% weight at 30 PA)
    """
    info     = mlb_api.get_player_info(batter_id) or {}
    bat_side = info.get("bat_side", "R")

    # ── 1. Platoon factor ────────────────────────────────────────────────────
    platoon_mult = _PLATOON.get((bat_side, pitcher_hand), 1.0)

    # Pitcher ERA vs this batter handedness vs overall
    if bat_side in ("L", "S"):
        split_era = pitcher_platoon.get("era_vs_lhb", None)
    else:
        split_era = pitcher_platoon.get("era_vs_rhb", None)

    if split_era and pitcher_overall_era > 0 and split_era < 15:
        platoon_era_ratio = split_era / pitcher_overall_era
    else:
        platoon_era_ratio = 1.0

    # Blend platoon ERA ratio with platoon OPS multiplier (60/40)
    platoon_factor = platoon_era_ratio * 0.60 + platoon_mult * 0.40

    # ── 2. BvP factor ────────────────────────────────────────────────────────
    bvp = mlb_api.get_batter_vs_pitcher(batter_id, pitcher_id) or {}
    pa  = bvp.get("pa", 0)

    bvp_factor = 1.0
    if pa >= MIN_BVP_PA:
        season  = mlb_api.get_player_season_stats(batter_id) or {}
        s_ops   = season.get("ops", LEAGUE_AVG_OPS)
        bvp_ops = bvp.get("ops", s_ops)
        if s_ops > 0:
            weight     = min(pa / 30.0, MAX_BVP_WEIGHT)
            blended    = s_ops * (1 - weight) + bvp_ops * weight
            bvp_factor = blended / s_ops

    return max(0.70, min(1.40, platoon_factor * bvp_factor))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _hand(pitcher_id):
    if not pitcher_id:
        return "R"
    return (mlb_api.get_player_info(pitcher_id) or {}).get("pitch_hand", "R")


def _default():
    return {
        "has_lineup": False,
        "home_factor": 1.0, "away_factor": 1.0,
        "nrfi_home":   1.0, "nrfi_away":   1.0,
        "home_pitcher_hand": "R", "away_pitcher_hand": "R",
        "home_lineup": [], "away_lineup": [],
    }
