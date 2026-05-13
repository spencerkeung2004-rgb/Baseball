"""MLB Stats API wrapper — no API key required."""
import time
import requests
from config import CURRENT_SEASON, LEAGUE_AVG_ERA, LEAGUE_K_PCT

BASE_URL = "https://statsapi.mlb.com/api/v1"


def _get(endpoint, params=None, retries=3):
    url = f"{BASE_URL}{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, timeout=12)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == retries - 1:
                print(f"  [MLB API] {endpoint} failed: {e}")
                return None
            time.sleep(1.5)
    return None


# ── Schedule ──────────────────────────────────────────────────────────────────

def get_schedule(date_str):
    """Return list of game dicts for a given date (YYYY-MM-DD)."""
    data = _get("/schedule", params={
        "sportId": 1,
        "date": date_str,
        "hydrate": "probablePitcher,weather,linescore,team",
    })
    if not data or not data.get("dates"):
        return []

    games = []
    for raw in data["dates"][0].get("games", []):
        state = raw.get("status", {}).get("abstractGameState", "")
        if state not in ("Preview", "Pre-Game", "Scheduled", "Live", "Final"):
            continue

        home = raw["teams"]["home"]
        away = raw["teams"]["away"]

        g = {
            "game_pk":        raw["gamePk"],
            "date":           date_str,
            "game_time":      raw.get("gameDate", ""),
            "status":         state,
            "venue":          raw.get("venue", {}).get("name", ""),
            "home_team":      home["team"]["abbreviation"],
            "home_team_id":   home["team"]["id"],
            "home_team_name": home["team"]["name"],
            "away_team":      away["team"]["abbreviation"],
            "away_team_id":   away["team"]["id"],
            "away_team_name": away["team"]["name"],
            "home_pitcher":   None, "home_pitcher_id": None,
            "away_pitcher":   None, "away_pitcher_id": None,
            "mlb_weather":    raw.get("weather", {}),
        }

        if "probablePitcher" in home:
            g["home_pitcher"]    = home["probablePitcher"].get("fullName")
            g["home_pitcher_id"] = home["probablePitcher"].get("id")
        if "probablePitcher" in away:
            g["away_pitcher"]    = away["probablePitcher"].get("fullName")
            g["away_pitcher_id"] = away["probablePitcher"].get("id")

        games.append(g)

    return games


# ── Pitcher stats ─────────────────────────────────────────────────────────────

def get_pitcher_season_stats(pitcher_id, season=None):
    season = season or CURRENT_SEASON
    data = _get(f"/people/{pitcher_id}/stats", params={
        "stats": "season", "group": "pitching", "season": season,
    })
    return _parse_pitcher_season(data)


def get_pitcher_recent_stats(pitcher_id, last_n=5, season=None):
    season = season or CURRENT_SEASON
    data = _get(f"/people/{pitcher_id}/stats", params={
        "stats": "gameLog", "group": "pitching", "season": season,
    })
    return _parse_pitcher_gamelog(data, last_n)


def _parse_pitcher_season(data):
    if not data:
        return {}
    for sg in data.get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0]["stat"]
        ip  = _f(s.get("inningsPitched", 0))
        so  = _i(s.get("strikeOuts", 0))
        bb  = _i(s.get("baseOnBalls", 0))
        er  = _i(s.get("earnedRuns", 0))
        gs  = _i(s.get("gamesStarted", 0))
        era = _f(s.get("era", LEAGUE_AVG_ERA))
        return {
            "era":          era if era < 20 else LEAGUE_AVG_ERA,
            "whip":         _f(s.get("whip", 1.30)),
            "k9":           so / ip * 9 if ip > 0 else 7.5,
            "bb9":          bb / ip * 9 if ip > 0 else 3.0,
            "hr9":          _f(s.get("homeRunsPer9", 1.2)),
            "ip_per_start": ip / gs if gs > 0 else 5.5,
            "innings_pitched": ip,
            "strikeouts":   so,
            "walks":        bb,
            "games_started": gs,
            "opp_avg":      _f(s.get("avg", 0.250)),
            "fip":          _fip(so, bb, er, ip),
        }
    return {}


def _parse_pitcher_gamelog(data, last_n):
    if not data:
        return {}
    games = []
    for sg in data.get("stats", []):
        for split in sg.get("splits", []):
            s = split["stat"]
            games.append({
                "date": split.get("date", ""),
                "ip":   _f(s.get("inningsPitched", 0)),
                "er":   _i(s.get("earnedRuns", 0)),
                "so":   _i(s.get("strikeOuts", 0)),
                "bb":   _i(s.get("baseOnBalls", 0)),
                "h":    _i(s.get("hits", 0)),
            })
    games.sort(key=lambda x: x["date"], reverse=True)
    recent = games[:last_n]
    if not recent:
        return {}
    total_ip = sum(g["ip"] for g in recent)
    total_so = sum(g["so"] for g in recent)
    return {
        "recent_era":    sum(g["er"] for g in recent) / total_ip * 9 if total_ip > 0 else LEAGUE_AVG_ERA,
        "recent_k9":     total_so / total_ip * 9 if total_ip > 0 else 7.5,
        "recent_avg_ip": total_ip / len(recent),
        "recent_games":  recent,
    }


# ── Team stats ────────────────────────────────────────────────────────────────

def get_team_hitting_stats(team_id, season=None):
    season = season or CURRENT_SEASON
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "season", "group": "hitting", "season": season,
    })
    return _parse_team_hitting(data)


def get_team_pitching_stats(team_id, season=None):
    season = season or CURRENT_SEASON
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "season", "group": "pitching", "season": season,
    })
    return _parse_team_pitching(data)


def _parse_team_hitting(data):
    if not data:
        return {}
    for sg in data.get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0]["stat"]
        g  = max(_i(s.get("gamesPlayed", 1)), 1)
        pa = max(_i(s.get("plateAppearances", 1)), 1)
        return {
            "avg":           _f(s.get("avg", 0.250)),
            "obp":           _f(s.get("obp", 0.320)),
            "slg":           _f(s.get("slg", 0.400)),
            "ops":           _f(s.get("ops", 0.720)),
            "runs_per_game": _i(s.get("runs", 0)) / g,
            "k_pct":         _i(s.get("strikeOuts", 0)) / pa,
            "bb_pct":        _i(s.get("baseOnBalls", 0)) / pa,
            "games":         g,
        }
    return {}


def _parse_team_pitching(data):
    if not data:
        return {}
    for sg in data.get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0]["stat"]
        g  = max(_i(s.get("gamesPlayed", 1)), 1)
        ip = max(_f(s.get("inningsPitched", 1)), 1)
        return {
            "era":                   _f(s.get("era", LEAGUE_AVG_ERA)),
            "whip":                  _f(s.get("whip", 1.30)),
            "runs_allowed_per_game": _i(s.get("runs", 0)) / g,
            "k9":                    _i(s.get("strikeOuts", 0)) / ip * 9,
            "bb9":                   _i(s.get("baseOnBalls", 0)) / ip * 9,
        }
    return {}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _f(v, default=0.0):
    try:
        return float(v or default)
    except (ValueError, TypeError):
        return default


def _i(v, default=0):
    try:
        return int(v or default)
    except (ValueError, TypeError):
        return default


def _fip(so, bb, er, ip):
    if ip <= 0:
        return LEAGUE_AVG_ERA
    hr_est = er * 0.25
    return max(1.0, ((13 * hr_est + 3 * bb - 2 * so) / ip) + 3.10)
