import os
from pathlib import Path

# Load .env if present
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        if "=" in _line and not _line.startswith("#"):
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# API Keys
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")

# Bookmakers. Game lines + pitcher strikeouts come from FanDuel, but FanDuel's
# batter-prop lines are not exposed through the Odds API — only DraftKings/BetMGM/
# BetRivers carry batter_hits etc.  Source batter props from DraftKings (fullest
# coverage), which is where those specific prop bets should be placed.
BATTER_PROPS_BOOKMAKER = "draftkings"

# Bankroll Settings
STARTING_BANKROLL = 1000.0
UNIT_SIZE = 25.0
MAX_UNITS = 4.0
MIN_UNITS = 0.5
KELLY_FRACTION = 0.25   # fraction of full Kelly to stake (quarter-Kelly — standard, lower variance)

# Bet Filters
MIN_ODDS_AMERICAN = -300   # allow favourites up to -300
MIN_EDGE = 0.05
DAILY_PICKS = 3            # picks saved with real stakes to the bankroll tracker
# Calibration tracking: while a type is below CAL_TARGET_SAMPLES, every bet
# clearing MIN_EDGE is added as a stake-0 tracking pick (rebuild fast, no cap).
# Once a type reaches the target, throttle it to CAL_MAINTENANCE_PER_TYPE/day.
CAL_TARGET_SAMPLES = 40        # settled bets per type at which calibration is "full"
CAL_MAINTENANCE_PER_TYPE = 3   # tracking picks/day per type once it has reached the target
PLATT_MIN_SAMPLES = 40     # samples at which Platt calibration reaches FULL strength
PLATT_WARMUP_SAMPLES = 15  # samples at which Platt begins applying (confidence-ramped from here to full)
# Calibration reset: only bets settled on/after this date feed calibration.  Bump
# this whenever the model changes materially (the old model's probability→outcome
# mapping no longer applies).  Bet P&L history is unaffected — this only scopes
# which settled bets the calibrator learns from.
CALIBRATION_EPOCH = "2026-07-23"

# MLB Settings
import datetime as _dt
CURRENT_SEASON = _dt.date.today().year
LEAGUE_AVG_RUNS = 4.50
LEAGUE_AVG_ERA = 4.20
LEAGUE_K_PCT = 0.225

# Offense via wOBA (weighted On-Base Average) — strips sequencing/timing luck that
# contaminates raw runs/game, and is more predictive of future run scoring.
LEAGUE_WOBA = 0.318   # approx league-average wOBA
WOBA_SCALE  = 1.25    # divides (wOBA − lgwOBA) to convert to runs (wRAA scale)
# Linear weights (modern-era approximate, stable year to year)
WOBA_WEIGHTS = {"bb": 0.69, "hbp": 0.72, "1b": 0.89, "2b": 1.27, "3b": 1.62, "hr": 2.10}

# Statcast xBA weight in the batter-hits model: blend of expected BA (quality of
# contact — more predictive) with actual season BA (results).
XBA_WEIGHT = 0.50
LEAGUE_BB_PCT  = 0.082   # batter walk rate per PA (MLB 2024 avg)
LEAGUE_K9_SP   = 8.7     # starter K/9 league average
LEAGUE_BB9_SP  = 3.1     # starter BB/9 league average

# Team Defense
LEAGUE_AVG_ERRORS_PER_GAME = 0.58   # approx modern-era MLB team errors/game
LEAGUE_AVG_DP_PER_GAME     = 0.78   # approx modern-era MLB team double plays/game
DEFENSE_ERROR_WEIGHT = 0.05  # run-factor swing per error/game above/below league average
DEFENSE_DP_WEIGHT    = 0.03  # run-factor swing per double-play/game above/below league average

# Blending weights: season vs recent performance
# Rate stats (ERA, K/9, BB/9) are noisy over 5 starts — lean on season
# IP/start reflects workload signals more quickly — lean more recent
RECENT_GAMES_WINDOW = 5     # number of recent starts used in all blends
BLEND_SEASON_RATE   = 0.70  # season weight for ERA, K/9, BB/9
BLEND_SEASON_IP     = 0.55  # season weight for IP/start
BLEND_SEASON_HOME_AWAY = 0.60  # season weight vs. home/away split (~half-season sample, noisier than full season)

# Run-allowance skill estimate: blend ERA (noisy, defense-contaminated) with FIP
# (skill-based, defense-independent).  ERA kept at 40% per requirement.
FIP_BLEND_ERA_WEIGHT = 0.40   # ERA weight; FIP gets the remaining 0.60 (fallback when
                              # xERA is unavailable — see SP_SKILL_*_W below)

# Statcast expected stats (Baseball Savant) blended in — quality of contact predicts
# future performance better than results.  SP run-prevention skill: when the pitcher's
# xERA is available, blend ERA/FIP/xERA (weights sum to 1.0); otherwise fall back to
# the ERA/FIP split above.
SP_SKILL_ERA_W  = 0.30   # actual ERA (results, defense/luck-contaminated)
SP_SKILL_FIP_W  = 0.35   # FIP (K, BB, HR)
SP_SKILL_XERA_W = 0.35   # xERA (Statcast expected ERA from contact quality allowed)
# Team offense nudged toward the lineup's expected wOBA: factor = Σest_woba / Σwoba
# over the posted lineup, bounded so it stays a regression nudge, not a rewrite.
XWOBA_FACTOR_MIN = 0.94
XWOBA_FACTOR_MAX = 1.06

# Starter run-prevention estimate is regressed toward league average by sample
# size:  true ≈ observed·IP/(IP+K) + league·K/(IP+K),  K = SP_SKILL_REGRESSION_IP.
# Without this, a tiny-sample ERA (injury return, September call-up) is read at
# face value — e.g. a 24-IP, 9.49-ERA line inflated the opposing offense ~1.6×.
# ~70 IP is roughly where a starter's ERA/FIP half-stabilizes.
SP_SKILL_REGRESSION_IP = 70
# Backstop clamp on the opposing-pitching run multiplier (combined_era / lg ERA)
# so no single pitcher/bullpen line can swing a team's projected offense more than
# ±35% before park/weather.  Bites only on extreme small-sample or data-glitch lines.
ERA_FACTOR_MIN = 0.75
ERA_FACTOR_MAX = 1.35

# SP strikeout projection ran ~0.6 Ks low across 1,600+ historical starts (a bias
# that also drove the pitcher_k_under miscalibration — projecting Ks low makes
# UNDERs look strong and OVERs weak).  Empirical multiplicative correction to
# remove that measured bias; re-tune if the SP-K projection bias shifts.
SP_K_PROJ_FACTOR = 1.12

# Strikeout-prop distribution width.  The K PROJECTION is unbiased in the mean
# (measured −0.13 K over 146 starts, no tail bias), but actual Ks scatter ~20%
# wider than Poisson around it — Var(actual | projection) ≈ 1.20×mean — because the
# projection carries its own estimation error on top of Poisson game noise.  Plain
# Poisson understated this, so P(over) was overconfident whenever the projection sat
# above the line, inflating exactly the over-edges the optimizer selects (that, plus
# selection vs a sharp market, is why pitcher_k_over ran 12-19 despite an unbiased
# projection).  Model K props with a mildly overdispersed negative binomial instead,
# same family the totals model already uses.  Var = mu(1 + mu/r) = K_VAR_MULT·mu.
K_VAR_MULT = 1.20

# Market anchoring for K props — the K-prop analogue of the totals _anchored_proj.
# The K projection is unbiased but noisy (~2.3 K/start); a large model-vs-line gap
# is almost all that noise, and the optimizer selects those inflated gaps as "edges."
# Even after calibration + the probability-level anchor, pitcher_k_over's staking
# prob still ran +20pt overconfident (56% predicted vs 36% actual).  Shrink the
# projection toward the (sharp) book line before pricing, proportional to the gap:
#   gap 0 -> pure model;  gap = K_ANCHOR_SCALE -> full weight, then capped.
K_ANCHOR_SCALE = 3.5   # K of model-vs-line gap at which market weight hits the cap
K_ANCHOR_CAP   = 0.65  # max weight placed on the market line

# Team run projection ran ~0.36/team (~0.72/game) hot across 800+ historical games
# — the LEAGUE_AVG_RUNS anchor and the stacked multiplicative factors compound a
# little high.  Applied equally to both teams, so the run DIFFERENCE (and thus the
# moneyline win prob) is preserved while the totals over-lean is removed.
RUN_PROJ_FACTOR = 0.92

# Recent-form (momentum) also applied to run PREVENTION, not just offense: a cold
# team concedes more, a hot team fewer.  Scaled relative to the offensive effect
# (form correlates across sides but not perfectly).  Keep modest — baseball
# momentum is a weak, regression-prone signal.
DEF_MOMENTUM_SHARE = 0.50

# Weather Adjustments
TEMP_FACTOR_PER_10F = 0.015
WIND_OUT_FACTOR_PER_10MPH = 0.05
WIND_IN_FACTOR_PER_10MPH = 0.04

# Park Factors
# NOTE: "ATH" is the relocated Athletics at Sutter Health Park (Sacramento) — a
# tiny, hot minor-league park that plays as the most extreme hitters' park in MLB.
# 2026 A's games: 12.14 total runs/game at home vs 7.95 on the road → raw factor
# 1.53 (higher than Coors!).  Regressed toward 1.0 for single-season noise and set
# to 1.35 (Coors-level).  Was MISSING before, defaulting to a neutral 1.00, which
# systematically under-projected ATH totals and manufactured false UNDER edges.
# "OAK" (0.94) is retained for pre-2025 Oakland Coliseum games in the backtest.
PARK_FACTORS = {
    "COL": 1.35, "ATH": 1.35, "CIN": 1.12, "TEX": 1.08, "BAL": 1.06, "PHI": 1.05,
    "NYY": 1.04, "BOS": 1.03, "MIL": 1.02, "HOU": 1.01, "CHC": 1.00,
    "STL": 0.99, "ATL": 0.99, "LAD": 0.98, "NYM": 0.98, "TOR": 0.98,
    "MIN": 0.97, "ARI": 0.97, "DET": 0.97, "CLE": 0.96, "CWS": 0.96,
    "PIT": 0.96, "MIA": 0.95, "TB":  0.95, "KC":  0.95, "OAK": 0.94,
    "SEA": 0.94, "SF":  0.93, "SD":  0.93, "LAA": 0.93, "WSH": 0.97,
}

# Dome/Retractable Stadiums
DOME_STADIUMS = {"TB", "MIA", "TOR", "ARI", "HOU", "MIL"}

# Team to city for weather
TEAM_CITIES = {
    "NYY": "New York",    "BOS": "Boston",       "LAD": "Los Angeles",
    "SF":  "San Francisco","CHC": "Chicago",     "CWS": "Chicago",
    "ATL": "Atlanta",     "HOU": "Houston",      "NYM": "New York",
    "PHI": "Philadelphia","STL": "St. Louis",    "MIL": "Milwaukee",
    "MIN": "Minneapolis", "DET": "Detroit",      "CLE": "Cleveland",
    "KC":  "Kansas City", "TOR": "Toronto",      "TB":  "Tampa",
    "BAL": "Baltimore",   "WSH": "Washington",   "COL": "Denver",
    "ARI": "Phoenix",     "SD":  "San Diego",    "SEA": "Seattle",
    "OAK": "Oakland",     "LAA": "Anaheim",      "TEX": "Arlington",
    "MIA": "Miami",       "PIT": "Pittsburgh",   "CIN": "Cincinnati",
}
