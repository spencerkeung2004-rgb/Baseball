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

# Bankroll Settings
STARTING_BANKROLL = 1000.0
UNIT_SIZE = 25.0
MAX_UNITS = 4.0
MIN_UNITS = 0.5

# Bet Filters
MIN_ODDS_AMERICAN = 100
MIN_EDGE = 0.04
DAILY_PICKS = 3

# MLB Settings
import datetime as _dt
CURRENT_SEASON = _dt.date.today().year
LEAGUE_AVG_RUNS = 4.50
LEAGUE_AVG_ERA = 4.20
LEAGUE_K_PCT = 0.225

# Weather Adjustments
TEMP_FACTOR_PER_10F = 0.015
WIND_OUT_FACTOR_PER_10MPH = 0.05
WIND_IN_FACTOR_PER_10MPH = 0.04

# Park Factors
PARK_FACTORS = {
    "COL": 1.35, "CIN": 1.12, "TEX": 1.08, "BAL": 1.06, "PHI": 1.05,
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
