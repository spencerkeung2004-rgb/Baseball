"""
Core game projection model.

Projects runs scored by each team, win probabilities, pitcher Ks, and NRFI
using MLB season + recent stats, weather, park factors, team defense, and
matchup data.
"""
from scipy.stats import poisson, nbinom

from config import (
    LEAGUE_AVG_RUNS, LEAGUE_AVG_ERA, LEAGUE_K_PCT, LEAGUE_BB_PCT,
    LEAGUE_K9_SP, LEAGUE_BB9_SP,
    LEAGUE_AVG_ERRORS_PER_GAME, LEAGUE_AVG_DP_PER_GAME,
    DEFENSE_ERROR_WEIGHT, DEFENSE_DP_WEIGHT,
    RECENT_GAMES_WINDOW, BLEND_SEASON_RATE, BLEND_SEASON_IP,
    BLEND_SEASON_HOME_AWAY, FIP_BLEND_ERA_WEIGHT, SP_K_PROJ_FACTOR, RUN_PROJ_FACTOR,
    SP_SKILL_REGRESSION_IP, ERA_FACTOR_MIN, ERA_FACTOR_MAX, K_VAR_MULT,
    K_ANCHOR_SCALE, K_ANCHOR_CAP,
    LEAGUE_WOBA, WOBA_SCALE, DEF_MOMENTUM_SHARE,
    PARK_FACTORS, DOME_STADIUMS,
)
from data import mlb_api, weather_api
from models import matchup_model
from models.matchup_model import get_lineup_k_bb_factors


# ── Top-level projection ──────────────────────────────────────────────────────

def project_game(game):
    """
    Full game projection including matchup adjustments and NRFI.
    """
    home_id = game["home_team_id"]
    away_id = game["away_team_id"]

    home_hit   = mlb_api.get_team_hitting_stats(home_id)  or {}
    away_hit   = mlb_api.get_team_hitting_stats(away_id)  or {}
    home_pit   = mlb_api.get_team_pitching_stats(home_id) or {}
    away_pit   = mlb_api.get_team_pitching_stats(away_id) or {}
    home_field = mlb_api.get_team_fielding_stats(home_id) or {}
    away_field = mlb_api.get_team_fielding_stats(away_id) or {}

    # Home/away splits — each team's batting is blended toward its OWN split
    # for the venue it's actually playing in (home team's home split, away
    # team's road split); same for pitching. Blended with the season number
    # rather than replacing it outright since a half-season split is noisier.
    home_hit_split = mlb_api.get_team_hitting_home_away(home_id).get("home", {})
    away_hit_split = mlb_api.get_team_hitting_home_away(away_id).get("away", {})
    home_pit_split = mlb_api.get_team_pitching_home_away(home_id).get("home", {})
    away_pit_split = mlb_api.get_team_pitching_home_away(away_id).get("away", {})

    home_hit = _blend_home_away(home_hit, home_hit_split, "woba")
    away_hit = _blend_home_away(away_hit, away_hit_split, "woba")
    home_pit = _blend_home_away(home_pit, home_pit_split, "era")
    away_pit = _blend_home_away(away_pit, away_pit_split, "era")

    home_sp = home_sp_r = away_sp = away_sp_r = {}
    home_platoon = away_platoon = {}
    home_sp_ha = away_sp_ha = {}
    home_rest = away_rest = {"days_rest": 5, "last_ip": 5.5, "est_pitch_count": 80}
    if game.get("home_pitcher_id"):
        home_sp      = mlb_api.get_pitcher_season_stats(game["home_pitcher_id"]) or {}
        home_sp_r    = mlb_api.get_pitcher_recent_stats(game["home_pitcher_id"], last_n=RECENT_GAMES_WINDOW) or {}
        home_platoon = mlb_api.get_pitcher_platoon_splits(game["home_pitcher_id"]) or {}
        home_rest    = mlb_api.get_pitcher_rest_days(game["home_pitcher_id"])
        # Home SP pitches at home -> his own home split
        home_sp_ha   = mlb_api.get_pitcher_home_away_splits(game["home_pitcher_id"]).get("home", {})
    if game.get("away_pitcher_id"):
        away_sp      = mlb_api.get_pitcher_season_stats(game["away_pitcher_id"]) or {}
        away_sp_r    = mlb_api.get_pitcher_recent_stats(game["away_pitcher_id"], last_n=RECENT_GAMES_WINDOW) or {}
        away_platoon = mlb_api.get_pitcher_platoon_splits(game["away_pitcher_id"]) or {}
        away_rest    = mlb_api.get_pitcher_rest_days(game["away_pitcher_id"])
        # Away SP pitches on the road -> his own road split
        away_sp_ha   = mlb_api.get_pitcher_home_away_splits(game["away_pitcher_id"]).get("away", {})

    # Umpire tendency factors for K/BB projection
    from data.umpire_api import get_game_umpire, get_umpire_factors
    ump_name    = get_game_umpire(game["game_pk"])
    ump_k_f, ump_bb_f = get_umpire_factors(ump_name)

    weather     = weather_api.get_game_weather(game["home_team"], game.get("mlb_weather"))
    w_factor    = weather_api.weather_run_factor(weather)
    park_factor = PARK_FACTORS.get(game["home_team"], 1.00)

    # Matchup data (lineup, platoon, BvP)
    matchup = matchup_model.get_matchup_data(game)

    # Lineup handedness-weighted K/BB adjustment for each SP
    home_lineup = matchup.get("home_lineup", [])
    away_lineup = matchup.get("away_lineup", [])
    home_sp_hand = matchup.get("home_pitcher_hand", "R")
    away_sp_hand = matchup.get("away_pitcher_hand", "R")
    # away batters face home SP; home batters face away SP
    away_k_adj, away_bb_adj = get_lineup_k_bb_factors(away_lineup, home_sp_hand)
    home_k_adj, home_bb_adj = get_lineup_k_bb_factors(home_lineup, away_sp_hand)

    # Momentum (last 10 games form) and head-to-head
    home_momentum = mlb_api.get_team_recent_results(home_id)
    away_momentum = mlb_api.get_team_recent_results(away_id)
    h2h_home      = mlb_api.get_head_to_head(home_id, away_id)
    h2h_away      = mlb_api.get_head_to_head(away_id, home_id)

    home_mom_f = _momentum_factor(home_momentum)
    away_mom_f = _momentum_factor(away_momentum)
    home_h2h_f = _h2h_factor(h2h_home)
    away_h2h_f = _h2h_factor(h2h_away)

    # Team defense — each side's own fielding-quality multiplier, applied
    # against the OPPONENT's batting (the team on the field, not at bat)
    home_def_f = _defense_factor(home_field)
    away_def_f = _defense_factor(away_field)

    # Base run projections (pitcher ERA model)
    home_runs_base = _project_runs(
        batting=home_hit, opp_sp=away_sp, opp_sp_recent=away_sp_r,
        opp_team_pit=away_pit, park=park_factor, weather=w_factor,
        defense=away_def_f, is_home=True,
    )
    away_runs_base = _project_runs(
        batting=away_hit, opp_sp=home_sp, opp_sp_recent=home_sp_r,
        opp_team_pit=home_pit, park=park_factor, weather=w_factor,
        defense=home_def_f, is_home=False,
    )

    # Apply matchup (platoon + BvP), momentum, and H2H factors.  Momentum acts on
    # BOTH sides: a team's own form scales its offense (home_mom_f) and, via
    # _def_momentum, the OPPONENT's form scales its run prevention — so a cold
    # opponent lets this team score more, a hot opponent suppresses it.
    home_runs = (home_runs_base * matchup["home_factor"] * home_mom_f * home_h2h_f
                 * _def_momentum(away_mom_f))
    away_runs = (away_runs_base * matchup["away_factor"] * away_mom_f * away_h2h_f
                 * _def_momentum(home_mom_f))

    # Empirical de-bias: run projections ran ~0.36/team hot.  Scaling both sides
    # equally fixes the totals over-lean while preserving the run difference (so
    # the moneyline win prob, which already beats baseline, is left intact).
    home_runs *= RUN_PROJ_FACTOR
    away_runs *= RUN_PROJ_FACTOR

    total     = home_runs + away_runs
    # Coherent Monte Carlo win prob — same NegBin run-distribution family as the
    # totals model (see simulate_game), replacing the old magic-number normal.
    home_wp   = simulate_game(home_runs, away_runs)["home_win_prob"]

    # NRFI projection — blend season + recent K/9 for each SP
    def _blend_k9(sp, sp_r):
        k9_s = sp.get("k9", LEAGUE_K9_SP)
        k9_r = sp_r.get("recent_k9", k9_s)
        return k9_s * BLEND_SEASON_RATE + k9_r * (1 - BLEND_SEASON_RATE)

    nrfi_prob = matchup_model.project_nrfi(
        home_runs, away_runs,
        matchup["nrfi_home"], matchup["nrfi_away"],
        home_sp_k9=_blend_k9(home_sp, home_sp_r),
        away_sp_k9=_blend_k9(away_sp, away_sp_r),
    )

    return {
        "game":              game,
        "home_runs":         round(home_runs, 2),
        "away_runs":         round(away_runs, 2),
        "total_runs":        round(total, 2),
        "home_win_prob":     round(home_wp, 4),
        "away_win_prob":     round(1 - home_wp, 4),
        "home_sp_ks":        _project_pitcher_peripherals(
                                 home_sp, home_sp_r, home_platoon, home_sp_hand,
                                 away_k_adj, away_bb_adj,
                                 ump_k_f, ump_bb_f, home_rest["days_rest"], home_sp_ha),
        "away_sp_ks":        _project_pitcher_peripherals(
                                 away_sp, away_sp_r, away_platoon, away_sp_hand,
                                 home_k_adj, home_bb_adj,
                                 ump_k_f, ump_bb_f, away_rest["days_rest"], away_sp_ha),
        "home_umpire":       ump_name,
        "nrfi_prob":         round(nrfi_prob, 4),
        "weather":           weather,
        "weather_factor":    round(w_factor, 3),
        "park_factor":       park_factor,
        "home_defense_factor": round(home_def_f, 4),
        "away_defense_factor": round(away_def_f, 4),
        "matchup":           matchup,
        "home_momentum":     home_momentum,
        "away_momentum":     away_momentum,
        "home_momentum_f":   round(home_mom_f, 4),
        "away_momentum_f":   round(away_mom_f, 4),
        "h2h_home":          h2h_home,
        "h2h_away":          h2h_away,
        "home_h2h_f":        round(home_h2h_f, 4),
        "away_h2h_f":        round(away_h2h_f, 4),
        "home_sp_rest":      home_rest,
        "away_sp_rest":      away_rest,
    }


# ── Home/away splits ──────────────────────────────────────────────────────────

def _blend_home_away(season_stats, split_stats, key):
    """
    Return a copy of season_stats with `key` blended toward the team's
    home/away split value for that key (season-weighted per
    BLEND_SEASON_HOME_AWAY). Other keys pass through unchanged.
    """
    if key not in season_stats or not split_stats.get(key):
        return season_stats
    season_val = season_stats[key]
    split_val  = split_stats[key]
    blended    = season_val * BLEND_SEASON_HOME_AWAY + split_val * (1 - BLEND_SEASON_HOME_AWAY)
    merged = dict(season_stats)
    merged[key] = blended
    return merged


# ── Run projection ────────────────────────────────────────────────────────────

def _offense_base(batting):
    """
    Expected runs/game for a team's offense, from wOBA rather than raw runs/game.

    wOBA weights each offensive event by its real run value and ignores the
    sequencing/timing luck baked into raw runs scored, so it's a steadier and
    more predictive read on offensive quality.  Converted to runs via wRAA:
        exp_rpg = LEAGUE_AVG_RUNS + ((wOBA − lgwOBA) / wOBA_scale) × PA/game
    Falls back to raw runs/game when wOBA can't be computed.
    """
    woba  = batting.get("woba")
    pa_pg = batting.get("pa_per_game", 38.0)
    if woba and 0.200 < woba < 0.500:
        wraa_pg = ((woba - LEAGUE_WOBA) / WOBA_SCALE) * pa_pg
        return LEAGUE_AVG_RUNS + wraa_pg
    return batting.get("runs_per_game", LEAGUE_AVG_RUNS)


def _project_runs(batting, opp_sp, opp_sp_recent, opp_team_pit,
                  park, weather, defense, is_home):
    base = _offense_base(batting)
    if not (0.5 < base < 15):
        base = LEAGUE_AVG_RUNS

    # Season run-allowance skill = 40% ERA + 60% FIP.  FIP strips out defense and
    # sequencing luck (it's built only from K, BB, HR), so it's a less noisy read on
    # the pitcher's own run prevention than ERA alone.
    sp_era       = opp_sp.get("era", LEAGUE_AVG_ERA)
    sp_fip       = opp_sp.get("fip", sp_era)
    sp_skill     = FIP_BLEND_ERA_WEIGHT * sp_era + (1 - FIP_BLEND_ERA_WEIGHT) * sp_fip
    # Recent game-log only carries ERA — blend it onto the FIP-based season skill.
    sp_era_rec   = opp_sp_recent.get("recent_era", sp_skill)
    sp_era_blend = sp_skill * BLEND_SEASON_RATE + sp_era_rec * (1 - BLEND_SEASON_RATE)

    # Regress the starter's estimate toward league average by sample size.  A
    # low-IP line (injury return, call-up) is mostly noise — e.g. Scherzer's 9.49
    # ERA over 24 IP would otherwise blow the opposing offense up ~1.6×.
    sp_ip_sample = opp_sp.get("innings_pitched", 0.0)
    reg_w        = sp_ip_sample / (sp_ip_sample + SP_SKILL_REGRESSION_IP)
    sp_era_blend = sp_era_blend * reg_w + LEAGUE_AVG_ERA * (1 - reg_w)

    sp_ip = min(max(opp_sp.get("ip_per_start", 5.5), 4.0), 7.0)
    sp_w  = sp_ip / 9.0
    bp_w  = 1.0 - sp_w
    bp_era = opp_team_pit.get("era", LEAGUE_AVG_ERA)

    combined_era = sp_era_blend * sp_w + bp_era * bp_w
    era_factor   = combined_era / LEAGUE_AVG_ERA
    # Backstop: no single matchup should swing an offense more than ±35%.
    era_factor   = max(ERA_FACTOR_MIN, min(ERA_FACTOR_MAX, era_factor))

    runs = base * era_factor * defense * park * weather
    if is_home:
        runs += 0.12
    return max(1.5, min(12.0, runs))


# ── Team defense ──────────────────────────────────────────────────────────────

def _defense_factor(fielding):
    """
    Defensive-quality multiplier applied to the OPPONENT's expected runs
    (and, in the betting engine, batting-average-on-balls-in-play props).

    Combines two MLB team fielding-stat-group signals vs league average:
      - error rate      : more errors -> extra baserunners/unearned runs -> more runs allowed
      - double-play rate: fewer DPs   -> fewer rally-killing outs        -> more runs allowed
    Max +-6%.
    """
    if not fielding:
        return 1.0
    err_pg = fielding.get("errors_per_game", LEAGUE_AVG_ERRORS_PER_GAME)
    dp_pg  = fielding.get("dp_per_game", LEAGUE_AVG_DP_PER_GAME)

    err_component = (err_pg - LEAGUE_AVG_ERRORS_PER_GAME) * DEFENSE_ERROR_WEIGHT
    dp_component  = (LEAGUE_AVG_DP_PER_GAME - dp_pg) * DEFENSE_DP_WEIGHT

    return max(0.94, min(1.06, 1.0 + err_component + dp_component))


# ── Strikeout projection ──────────────────────────────────────────────────────

def _project_pitcher_peripherals(
    sp_stats: dict, sp_recent: dict, platoon_splits: dict, pitcher_hand: str,
    opp_k_adj: float, opp_bb_adj: float,
    ump_k_factor: float, ump_bb_factor: float,
    days_rest: int, home_away_split: dict = None,
) -> dict:
    """
    Project pitcher strikeouts AND walks using only pitcher-controlled inputs
    (FIP components: K, BB, HR, plus this SP's own home/away split).  Does
    NOT use team defense metrics.

    Inputs
    ------
    sp_stats       : season stats dict (k9, bb9, ip_per_start, innings_pitched, …)
    sp_recent      : recent game log dict (recent_k9, recent_bb9, recent_avg_ip, …)
    platoon_splits : pitcher's K9/BB9 split vs LHB and RHB
    pitcher_hand   : 'L' or 'R'
    opp_k_adj      : lineup K% factor vs league avg (from get_lineup_k_bb_factors)
    opp_bb_adj     : lineup BB% factor vs league avg
    ump_k_factor   : umpire zone K multiplier (from umpire_api)
    ump_bb_factor  : umpire zone BB multiplier
    days_rest      : days since last outing
    home_away_split: this pitcher's own {'k9','bb9','era'} for the side of
                      the matchup he's actually on (home SP's home split,
                      away SP's road split)

    Returns dict with:
      projection   : projected strikeouts
      proj_bb      : projected walks
      ip           : projected innings pitched
      k9           : effective K/9 used
      bb9          : effective BB/9 used
      confidence   : 'high' | 'medium' | 'low'
      is_reliever  : bool
      rest_factor  : float
      umpire       : (k_factor, bb_factor) applied
    """
    home_away_split = home_away_split or {}
    if not sp_stats:
        return {
            "projection": 5.0, "proj_bb": 2.2, "ip": 5.5,
            "k9": LEAGUE_K9_SP, "bb9": LEAGUE_BB9_SP,
            "confidence": "low", "is_reliever": False,
            "rest_factor": 1.0, "umpire": (ump_k_factor, ump_bb_factor),
        }

    # ── Step 1: IP projection (rest-adjusted) ─────────────────────────────────
    avg_ip  = sp_stats.get("ip_per_start", 5.5)
    rec_ip  = sp_recent.get("recent_avg_ip", avg_ip)
    is_reliever = rec_ip < 3.5
    if is_reliever:
        proj_ip_base = min(6.0, max(1.0, rec_ip * 0.85 + avg_ip * 0.15))
    else:
        proj_ip_base = min(8.0, max(4.0, avg_ip * BLEND_SEASON_IP + rec_ip * (1 - BLEND_SEASON_IP)))

    # Rest factors — three separate effects:
    #   IP depth    : short rest = pulled earlier; very long layoff = pitch-count caution
    #   K rate      : fresh arm = more velocity = more Ks; tired arm = fewer Ks
    #   BB rate     : fresh arm = sharper command = fewer BBs; tired/rusty arm = more BBs
    if days_rest <= 3:
        rest_ip_factor = 0.88   # get pulled earlier on short rest
        rest_k_factor  = 0.90   # tired arm → less velo, fewer Ks
        rest_bb_factor = 1.08   # tired arm → worse command, more walks
    elif days_rest <= 5:
        rest_ip_factor = 1.00
        rest_k_factor  = 1.00
        rest_bb_factor = 1.00
    elif days_rest <= 8:
        rest_ip_factor = 1.00   # depth unaffected
        rest_k_factor  = 1.03   # fresh arm → extra velo/break, more Ks
        rest_bb_factor = 0.96   # sharper command → fewer walks
    else:
        # 9+ days: fully rested but mechanical rust may affect command
        rest_ip_factor = 0.95   # cautious pitch count on return
        rest_k_factor  = 1.01   # still fresh, minor rust net
        rest_bb_factor = 1.02   # slight command rust

    proj_ip = max(1.0, proj_ip_base * rest_ip_factor)

    # ── Step 2: K9 — season + recent blend, handedness-split weighted ─────────
    k9_season = sp_stats.get("k9", LEAGUE_K9_SP)
    k9_recent = sp_recent.get("recent_k9", k9_season)
    k9_blend  = k9_season * BLEND_SEASON_RATE + k9_recent * (1 - BLEND_SEASON_RATE)

    k9_vs_l = platoon_splits.get("k9_vs_lhb") or k9_blend
    k9_vs_r = platoon_splits.get("k9_vs_rhb") or k9_blend
    k9_hand = (k9_vs_l + k9_vs_r) / 2

    # Blend toward this SP's own home/away split (his home K/9 if he's the
    # home starter, his road K/9 if he's the away starter) — same weighting
    # as the team-level home/away blend, since it's also a smaller sample.
    k9_ha = home_away_split.get("k9")
    if k9_ha:
        k9_hand = k9_hand * BLEND_SEASON_HOME_AWAY + k9_ha * (1 - BLEND_SEASON_HOME_AWAY)

    k9_eff  = k9_hand * opp_k_adj * ump_k_factor * rest_k_factor

    # ── Step 3: BB9 — same approach ──────────────────────────────────────────
    bb9_season = sp_stats.get("bb9", LEAGUE_BB9_SP)
    bb9_recent = sp_recent.get("recent_bb9", bb9_season)
    bb9_blend  = bb9_season * BLEND_SEASON_RATE + bb9_recent * (1 - BLEND_SEASON_RATE)

    bb9_vs_l = platoon_splits.get("bb9_vs_lhb") or bb9_blend
    bb9_vs_r = platoon_splits.get("bb9_vs_rhb") or bb9_blend
    bb9_hand = (bb9_vs_l + bb9_vs_r) / 2

    bb9_ha = home_away_split.get("bb9")
    if bb9_ha:
        bb9_hand = bb9_hand * BLEND_SEASON_HOME_AWAY + bb9_ha * (1 - BLEND_SEASON_HOME_AWAY)

    bb9_eff  = bb9_hand * opp_bb_adj * ump_bb_factor * rest_bb_factor

    # ── Step 4: Final projections ─────────────────────────────────────────────
    # SP_K_PROJ_FACTOR removes the ~0.6-K systematic under-projection measured
    # over historical starts.  Applied to Ks only (BB showed no such bias).
    proj_ks = max(0.0, (k9_eff / 9) * proj_ip * SP_K_PROJ_FACTOR)
    proj_bb = max(0.0, (bb9_eff / 9) * proj_ip)

    sample_ip  = sp_stats.get("innings_pitched", 0)
    confidence = "high" if sample_ip > 50 else ("medium" if sample_ip > 20 else "low")
    if is_reliever:
        confidence = "low"

    return {
        "projection":    round(proj_ks, 1),
        "proj_bb":       round(proj_bb, 1),
        "ip":            round(proj_ip, 1),
        "k9":            round(k9_eff,  2),
        "bb9":           round(bb9_eff, 2),
        "confidence":    confidence,
        "is_reliever":   is_reliever,
        "rest_k_factor": round(rest_k_factor,  3),
        "rest_ip_factor": round(rest_ip_factor, 3),
        "rest_bb_factor": round(rest_bb_factor, 3),
        "umpire":        (round(ump_k_factor, 3), round(ump_bb_factor, 3)),
    }


# ── Momentum & H2H factors ────────────────────────────────────────────────────

def _momentum_factor(momentum):
    """
    Recent-form multiplier on a team's projected OFFENSE. Max ±10%.
      - Win% component:       above/below .500 over last 10 games
      - Run-differential:     average run margin per game
      - Hot/cold streak:      bonus/penalty scaling with streak length (4+)

    (A team's form is also applied to its run PREVENTION separately, via
    _def_momentum, so a slumping team both scores less and concedes more.)
    """
    if not momentum:
        return 1.0

    win_pct      = momentum.get("win_pct", 0.500)
    rd_per_game  = momentum.get("run_diff_per_game", 0.0)
    streak       = momentum.get("streak", 0)
    streak_type  = momentum.get("streak_type", "W")

    win_component = (win_pct - 0.500) * 0.14        # ±7% at 0/1.000 W%
    rd_component  = rd_per_game * 0.008              # ±~2.5% at ±3 RD/game
    streak_bonus  = 0.0
    if streak >= 4:
        # Scale with length: 1.5% at 4 games up to a 4% cap at 9+ games, so an
        # extreme streak (e.g. 0-10) bites harder than a routine 4-game skid.
        mag = min(0.04, 0.015 + (streak - 4) * 0.005)
        streak_bonus = mag * (1 if streak_type == "W" else -1)

    return max(0.90, min(1.10, 1.0 + win_component + rd_component + streak_bonus))


def _def_momentum(defender_mom_f):
    """
    A team's recent form applied to run PREVENTION.  Returns the multiplier on the
    OPPONENT's runs: a cold defender (mom_f < 1) concedes more, a hot one fewer,
    scaled by DEF_MOMENTUM_SHARE relative to the offensive effect.
    """
    return 1.0 + (1.0 - defender_mom_f) * DEF_MOMENTUM_SHARE


def _h2h_factor(h2h):
    """
    Season H2H multiplier. Only applied with 3+ meetings; max ±4%.
    """
    if not h2h or h2h.get("games_played", 0) < 3:
        return 1.0
    adj = (h2h.get("win_pct", 0.500) - 0.500) * 0.08
    return max(0.96, min(1.04, 1.0 + adj))


# ── Coherent game simulation ──────────────────────────────────────────────────

_SIM_N    = 20000    # Monte Carlo draws per game (SE on a prob ≈ 0.0035)
_SIM_SEED = 12345    # fixed → win probs are reproducible across re-runs


def simulate_game(home_mu, away_mu, n=_SIM_N, seed=_SIM_SEED):
    """
    Monte Carlo game simulation.  Draws n (home_runs, away_runs) pairs from
    per-team negative binomials using the SAME dispersion family as the totals
    model (_nb_params), then derives the win probability from the run difference.

    Why this replaces the old normal-CDF win prob:
      Each team's NegBin has variance 2.5·mu, so the run DIFFERENCE has
      sd = sqrt(2.5·(home_mu+away_mu)) ≈ 4.6 for a typical 8.5-run game.  The old
      formula used norm.cdf(diff/3.5) — an implied sd of 3.5, which understated
      variance and made the favourite's win prob overconfident (the exact bias the
      moneyline calibration showed: predicted 61.8% vs actual 49.4%).  Drawing from
      the real run distribution both fixes that and makes ML and totals coherent —
      they now share one run-distribution family.

    Ties (simulated equal runs → extra innings) are split 50/50.  Returns:
      home_win_prob, away_win_prob, home_samples, away_samples
    """
    import numpy as np
    rng = np.random.default_rng(seed)
    rh, ph = _nb_params(max(0.05, home_mu))
    ra, pa = _nb_params(max(0.05, away_mu))
    home = rng.negative_binomial(rh, ph, n)
    away = rng.negative_binomial(ra, pa, n)

    home_wins = int(np.count_nonzero(home > away))
    ties      = int(np.count_nonzero(home == away))
    hwp = (home_wins + 0.5 * ties) / n
    hwp = max(0.02, min(0.98, float(hwp)))
    return {
        "home_win_prob": hwp,
        "away_win_prob": 1.0 - hwp,
        "home_samples":  home,
        "away_samples":  away,
    }


# ── Probability helpers used by betting engine ────────────────────────────────

def _nb_params(mu):
    """
    Negative binomial params for a given mean (mu).

    Empirically, MLB game-total run distributions have variance ≈ 2.5× Poisson
    (observed std ≈ 4.5 runs vs Poisson std ≈ 3 for an 8.5-run mean).
    We model this as NegBin with dispersion r = mu / 1.5
    → variance = mu + mu²/r = mu + 1.5·mu = 2.5·mu  ✓

    scipy nbinom(n, p) parameterisation: mean = n(1−p)/p, so p = r/(r+mu).
    """
    r = max(2.0, mu / 1.5)
    p = r / (r + mu)
    return r, p


def _anchored_proj(proj_total, line):
    """
    Anchor projection toward the market line when the gap is large.

    The market is highly efficient at pricing totals; gaps > 1.5 runs almost
    always reflect model uncertainty, not a genuine 3-5 run edge.  We shrink
    the effective projection toward the line proportionally to the gap:
      gap 0  → 0 % market weight  (pure model)
      gap 3  → ~43 % market weight
      gap 5+ → 60 % market weight (cap)
    """
    gap = abs(proj_total - line)
    market_wt = min(0.60, gap / 7.0)
    return proj_total * (1 - market_wt) + line * market_wt


def total_over_prob(proj_total, line):
    """P(actual runs > line) using neg-binomial + market anchoring."""
    mu = _anchored_proj(proj_total, line)
    n, p = _nb_params(mu)
    prob = 1 - nbinom.cdf(int(line), n, p)
    if line != int(line):
        return max(0.05, min(0.95, prob))
    # Integer line: split push probability evenly between OVER and UNDER
    return max(0.05, min(0.95, prob + nbinom.pmf(int(line), n, p) * 0.5))


def total_under_prob(proj_total, line):
    return max(0.05, min(0.95, 1 - total_over_prob(proj_total, line)))


def _nb_params_k(mu):
    """
    Negative-binomial params for a strikeout mean (mu), overdispersed so that
    Var = K_VAR_MULT·mu (vs Poisson's Var = mu).

    Var = mu(1 + mu/r) = K_VAR_MULT·mu  →  r = mu / (K_VAR_MULT − 1).
    Reduces to Poisson as K_VAR_MULT → 1.  Same nbinom(r, p) parameterisation as
    _nb_params: mean = r(1−p)/p, so p = r/(r+mu).
    """
    r = mu / (K_VAR_MULT - 1.0)
    p = r / (r + mu)
    return r, p


def _anchored_k_proj(proj_ks, line):
    """
    Shrink a strikeout projection toward the market line when they disagree — the
    K-prop analogue of _anchored_proj for totals.

    The K projection is unbiased but noisy (~2.3 K/start), so a large gap to the
    (sharp) book line is mostly that noise, not real edge — and the optimizer
    selects exactly those inflated gaps.  Lean on the line proportionally to the gap:
      gap 0            → pure model
      gap K_ANCHOR_SCALE→ K_ANCHOR_CAP weight on the line (capped)
    """
    gap = abs(proj_ks - line)
    market_wt = min(K_ANCHOR_CAP, gap / K_ANCHOR_SCALE)
    return proj_ks * (1 - market_wt) + line * market_wt


def ks_over_prob(proj_ks, line):
    """P(strikeouts > line) using market-anchored projection + overdispersed NB."""
    if proj_ks <= 0:
        return 0.50
    mu = _anchored_k_proj(proj_ks, line)
    r, p = _nb_params_k(mu)
    prob = 1 - nbinom.cdf(int(line), r, p)
    if line != int(line):
        return max(0.05, min(0.95, prob))
    # Integer line: split the push at exactly the line evenly (totals convention)
    return max(0.05, min(0.95, prob + nbinom.pmf(int(line), r, p) * 0.5))


def ks_under_prob(proj_ks, line):
    return max(0.05, min(0.95, 1 - ks_over_prob(proj_ks, line)))
