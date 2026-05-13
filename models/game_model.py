"""
Core game projection model.

Projects runs scored by each team, win probabilities, pitcher Ks, and NRFI
using MLB season + recent stats, weather, park factors, and matchup data.
"""
from scipy import stats
from scipy.stats import poisson

from config import (
    LEAGUE_AVG_RUNS, LEAGUE_AVG_ERA, LEAGUE_K_PCT,
    PARK_FACTORS, DOME_STADIUMS,
)
from data import mlb_api, weather_api
from models import matchup_model


# ── Top-level projection ──────────────────────────────────────────────────────

def project_game(game):
    """
    Full game projection including matchup adjustments and NRFI.
    """
    home_id = game["home_team_id"]
    away_id = game["away_team_id"]

    home_hit = mlb_api.get_team_hitting_stats(home_id)  or {}
    away_hit = mlb_api.get_team_hitting_stats(away_id)  or {}
    home_pit = mlb_api.get_team_pitching_stats(home_id) or {}
    away_pit = mlb_api.get_team_pitching_stats(away_id) or {}

    home_sp = home_sp_r = away_sp = away_sp_r = {}
    if game.get("home_pitcher_id"):
        home_sp   = mlb_api.get_pitcher_season_stats(game["home_pitcher_id"]) or {}
        home_sp_r = mlb_api.get_pitcher_recent_stats(game["home_pitcher_id"]) or {}
    if game.get("away_pitcher_id"):
        away_sp   = mlb_api.get_pitcher_season_stats(game["away_pitcher_id"]) or {}
        away_sp_r = mlb_api.get_pitcher_recent_stats(game["away_pitcher_id"]) or {}

    weather     = weather_api.get_game_weather(game["home_team"], game.get("mlb_weather"))
    w_factor    = weather_api.weather_run_factor(weather)
    park_factor = PARK_FACTORS.get(game["home_team"], 1.00)

    # Matchup data (lineup, platoon, BvP)
    matchup = matchup_model.get_matchup_data(game)

    # Base run projections (pitcher ERA model)
    home_runs_base = _project_runs(
        batting=home_hit, opp_sp=away_sp, opp_sp_recent=away_sp_r,
        opp_team_pit=away_pit, park=park_factor, weather=w_factor, is_home=True,
    )
    away_runs_base = _project_runs(
        batting=away_hit, opp_sp=home_sp, opp_sp_recent=home_sp_r,
        opp_team_pit=home_pit, park=park_factor, weather=w_factor, is_home=False,
    )

    # Apply matchup factors (platoon + BvP)
    home_runs = home_runs_base * matchup["home_factor"]
    away_runs = away_runs_base * matchup["away_factor"]

    total     = home_runs + away_runs
    home_wp   = _win_prob(home_runs, away_runs)

    # NRFI projection
    nrfi_prob = matchup_model.project_nrfi(
        home_runs, away_runs,
        matchup["nrfi_home"], matchup["nrfi_away"],
        park_factor, w_factor,
    )

    return {
        "game":              game,
        "home_runs":         round(home_runs, 2),
        "away_runs":         round(away_runs, 2),
        "total_runs":        round(total, 2),
        "home_win_prob":     round(home_wp, 4),
        "away_win_prob":     round(1 - home_wp, 4),
        "home_sp_ks":        _project_ks(home_sp, home_sp_r, away_hit),
        "away_sp_ks":        _project_ks(away_sp, away_sp_r, home_hit),
        "nrfi_prob":         round(nrfi_prob, 4),
        "weather":           weather,
        "weather_factor":    round(w_factor, 3),
        "park_factor":       park_factor,
        "matchup":           matchup,
    }


# ── Run projection ────────────────────────────────────────────────────────────

def _project_runs(batting, opp_sp, opp_sp_recent, opp_team_pit,
                  park, weather, is_home):
    base = batting.get("runs_per_game", LEAGUE_AVG_RUNS)
    if not (0.5 < base < 15):
        base = LEAGUE_AVG_RUNS

    sp_era       = opp_sp.get("era", LEAGUE_AVG_ERA)
    sp_era_rec   = opp_sp_recent.get("recent_era", sp_era)
    sp_era_blend = sp_era * 0.60 + sp_era_rec * 0.40

    sp_ip = min(max(opp_sp.get("ip_per_start", 5.5), 4.0), 7.0)
    sp_w  = sp_ip / 9.0
    bp_w  = 1.0 - sp_w
    bp_era = opp_team_pit.get("era", LEAGUE_AVG_ERA)

    combined_era = sp_era_blend * sp_w + bp_era * bp_w
    era_factor   = combined_era / LEAGUE_AVG_ERA

    runs = base * era_factor * park * weather
    if is_home:
        runs += 0.12
    return max(1.5, min(12.0, runs))


# ── Strikeout projection ──────────────────────────────────────────────────────

def _project_ks(sp_stats, sp_recent, opp_hitting):
    if not sp_stats:
        return {"projection": 5.0, "ip": 5.5, "confidence": "low"}

    k9     = sp_stats.get("k9", 7.5)
    k9_rec = sp_recent.get("recent_k9", k9)
    k9_b   = k9 * 0.65 + k9_rec * 0.35

    opp_k_pct = opp_hitting.get("k_pct", LEAGUE_K_PCT)
    k_adj     = opp_k_pct / LEAGUE_K_PCT

    avg_ip  = sp_stats.get("ip_per_start", 5.5)
    rec_ip  = sp_recent.get("recent_avg_ip", avg_ip)
    proj_ip = min(8.0, max(4.0, avg_ip * 0.65 + rec_ip * 0.35))

    projection = (k9_b / 9) * proj_ip * k_adj
    sample_ip  = sp_stats.get("innings_pitched", 0)
    confidence = "high" if sample_ip > 50 else ("medium" if sample_ip > 20 else "low")

    return {"projection": round(projection, 1), "ip": round(proj_ip, 1),
            "k9": round(k9_b, 2), "confidence": confidence}


# ── Win probability ───────────────────────────────────────────────────────────

def _win_prob(home_runs, away_runs):
    diff = home_runs - away_runs
    prob = stats.norm.cdf(diff / 3.5 + 0.05)
    return max(0.20, min(0.80, prob))


# ── Probability helpers used by betting engine ────────────────────────────────

def total_over_prob(proj_total, line):
    prob = 1 - poisson.cdf(int(line), proj_total)
    if line != int(line):
        return max(0.05, min(0.95, prob))
    return max(0.05, min(0.95, prob - poisson.pmf(int(line), proj_total) * 0.5))


def total_under_prob(proj_total, line):
    return max(0.05, min(0.95, 1 - total_over_prob(proj_total, line)))


def ks_over_prob(proj_ks, line):
    if proj_ks <= 0:
        return 0.50
    prob = 1 - poisson.cdf(int(line), proj_ks)
    if line != int(line):
        return max(0.05, min(0.95, prob))
    return max(0.05, min(0.95, prob - poisson.pmf(int(line), proj_ks) * 0.5))


def ks_under_prob(proj_ks, line):
    return max(0.05, min(0.95, 1 - ks_over_prob(proj_ks, line)))
