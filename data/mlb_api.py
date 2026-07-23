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


# ── Schedule & results ────────────────────────────────────────────────────────

def get_final_scores(date_str):
    """
    Return completed games for date_str as a list of dicts:
      game_pk, home_team, away_team, home_score, away_score, winner (abbr or None if tie)
    """
    data = _get("/schedule", params={
        "sportId": 1,
        "date": date_str,
        "hydrate": "linescore,team",
    })
    if not data or not data.get("dates"):
        return []

    games = []
    for raw in data["dates"][0].get("games", []):
        state = raw.get("status", {}).get("abstractGameState", "")
        if state not in ("Final", "Game Over", "Completed Early"):
            continue

        home = raw["teams"]["home"]
        away = raw["teams"]["away"]
        home_abbr  = home["team"]["abbreviation"]
        away_abbr  = away["team"]["abbreviation"]
        home_score = _i(home.get("score", 0))
        away_score = _i(away.get("score", 0))

        if home_score > away_score:
            winner = home_abbr
        elif away_score > home_score:
            winner = away_abbr
        else:
            winner = None  # tie (rain-shortened, etc.)

        # First inning runs for NRFI/YRFI settlement
        innings = raw.get("linescore", {}).get("innings", [])
        first   = innings[0] if innings else {}
        first_away = _i(first.get("away", {}).get("runs", 0))
        first_home = _i(first.get("home", {}).get("runs", 0))

        games.append({
            "game_pk":         raw["gamePk"],
            "home_team":       home_abbr,
            "away_team":       away_abbr,
            "home_score":      home_score,
            "away_score":      away_score,
            "winner":          winner,
            "first_inn_away":  first_away,
            "first_inn_home":  first_home,
        })

    return games


def get_pitcher_ks_from_boxscore(game_pk):
    """Return {full_name: strikeouts} for every pitcher who appeared in the game."""
    data = _get(f"/game/{game_pk}/boxscore")
    if not data:
        return {}
    result = {}
    for side in ("home", "away"):
        team_data = data.get("teams", {}).get(side, {})
        players   = team_data.get("players", {})
        for pid in team_data.get("pitchers", []):
            player = players.get(f"ID{pid}", {})
            name   = player.get("person", {}).get("fullName", "")
            ks     = _i(player.get("stats", {}).get("pitching", {}).get("strikeOuts", 0))
            if name:
                result[name] = ks
    return result


def get_batter_hits_from_boxscore(game_pk):
    """Return {full_name: hits} for every batter who appeared in the game."""
    data = _get(f"/game/{game_pk}/boxscore")
    if not data:
        return {}
    result = {}
    for side in ("home", "away"):
        team_data = data.get("teams", {}).get(side, {})
        players   = team_data.get("players", {})
        for pid in team_data.get("batters", []):
            player = players.get(f"ID{pid}", {})
            name   = player.get("person", {}).get("fullName", "")
            hits   = _i(player.get("stats", {}).get("batting", {}).get("hits", 0))
            if name:
                result[name] = hits
    return result


def get_schedule(date_str, include_started=False):
    """
    Return list of game dicts for a given date (YYYY-MM-DD).

    By default only upcoming (not-yet-started) games are returned — used by the
    live picks flow.  Pass include_started=True to also include Live/Final games,
    which the backtest harness needs to project past-date games.
    """
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
        if not include_started and state in ("Live", "Final", "Game Over", "Completed"):
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

# Session caches — season stats are stable within a run; big speedup for the
# backtest, where the same teams/pitchers recur across many dates.
_PITCHER_SEASON_CACHE = {}
_PITCHER_RECENT_CACHE = {}


def get_pitcher_season_stats(pitcher_id, season=None):
    season = season or CURRENT_SEASON
    key = (pitcher_id, season)
    if key in _PITCHER_SEASON_CACHE:
        return _PITCHER_SEASON_CACHE[key]
    data = _get(f"/people/{pitcher_id}/stats", params={
        "stats": "season", "group": "pitching", "season": season,
    })
    result = _parse_pitcher_season(data)
    _PITCHER_SEASON_CACHE[key] = result
    return result


def get_pitcher_recent_stats(pitcher_id, last_n=5, season=None):
    season = season or CURRENT_SEASON
    key = (pitcher_id, last_n, season)
    if key in _PITCHER_RECENT_CACHE:
        return _PITCHER_RECENT_CACHE[key]
    data = _get(f"/people/{pitcher_id}/stats", params={
        "stats": "gameLog", "group": "pitching", "season": season,
    })
    result = _parse_pitcher_gamelog(data, last_n)
    _PITCHER_RECENT_CACHE[key] = result
    return result


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
        hr  = _i(s.get("homeRuns", 0))
        gp = _i(s.get("gamesPlayed", gs))  # total appearances
        # ip_per_start: only meaningful when pitcher primarily starts.
        # If he has far more appearances than starts, use ip/appearance instead.
        if gs > 0 and gs >= gp * 0.5:
            ip_per_start = ip / gs
        elif gp > 0:
            ip_per_start = ip / gp   # reliever: use per-appearance average
        else:
            ip_per_start = 5.5

        return {
            "era":             era if era < 20 else LEAGUE_AVG_ERA,
            "whip":            _f(s.get("whip", 1.30)),
            "k9":              so / ip * 9 if ip > 0 else 7.5,
            "bb9":             bb / ip * 9 if ip > 0 else 3.0,
            "hr9":             _f(s.get("homeRunsPer9", 1.2)),
            "ip_per_start":    ip_per_start,
            "innings_pitched": ip,
            "strikeouts":      so,
            "walks":           bb,
            "games_started":   gs,
            "games_played":    gp,
            "opp_avg":         _f(s.get("avg", 0.250)),
            "fip":             _fip(so, bb, hr, ip),
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
    total_bb = sum(g["bb"] for g in recent)
    return {
        "recent_era":    sum(g["er"] for g in recent) / total_ip * 9 if total_ip > 0 else LEAGUE_AVG_ERA,
        "recent_k9":     total_so / total_ip * 9 if total_ip > 0 else 7.5,
        "recent_bb9":    total_bb / total_ip * 9 if total_ip > 0 else 3.0,
        "recent_avg_ip": total_ip / len(recent),
        "recent_games":  recent,
    }


# ── Team stats ────────────────────────────────────────────────────────────────

# Session caches for team-level season stats (stable within a run).
_TEAM_HIT_CACHE     = {}
_TEAM_PIT_CACHE     = {}
_TEAM_FIELD_CACHE   = {}
_TEAM_HIT_HA_CACHE  = {}
_TEAM_PIT_HA_CACHE  = {}


def get_team_hitting_stats(team_id, season=None):
    season = season or CURRENT_SEASON
    key = (team_id, season)
    if key in _TEAM_HIT_CACHE:
        return _TEAM_HIT_CACHE[key]
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "season", "group": "hitting", "season": season,
    })
    result = _parse_team_hitting(data)
    _TEAM_HIT_CACHE[key] = result
    return result


def get_team_pitching_stats(team_id, season=None):
    season = season or CURRENT_SEASON
    key = (team_id, season)
    if key in _TEAM_PIT_CACHE:
        return _TEAM_PIT_CACHE[key]
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "season", "group": "pitching", "season": season,
    })
    result = _parse_team_pitching(data)
    _TEAM_PIT_CACHE[key] = result
    return result


def get_team_fielding_stats(team_id, season=None):
    season = season or CURRENT_SEASON
    key = (team_id, season)
    if key in _TEAM_FIELD_CACHE:
        return _TEAM_FIELD_CACHE[key]
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "season", "group": "fielding", "season": season,
    })
    result = _parse_team_fielding(data)
    _TEAM_FIELD_CACHE[key] = result
    return result


def get_team_hitting_home_away(team_id, season=None):
    """Return {'home': {...}, 'away': {...}} team hitting splits (same shape as get_team_hitting_stats)."""
    season = season or CURRENT_SEASON
    key = (team_id, season)
    if key in _TEAM_HIT_HA_CACHE:
        return _TEAM_HIT_HA_CACHE[key]
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "statSplits", "group": "hitting", "season": season, "sitCodes": "h,a",
    })
    result = _parse_home_away_splits(data, _hitting_stat_dict)
    _TEAM_HIT_HA_CACHE[key] = result
    return result


def get_team_pitching_home_away(team_id, season=None):
    """Return {'home': {...}, 'away': {...}} team pitching splits (same shape as get_team_pitching_stats)."""
    season = season or CURRENT_SEASON
    key = (team_id, season)
    if key in _TEAM_PIT_HA_CACHE:
        return _TEAM_PIT_HA_CACHE[key]
    data = _get(f"/teams/{team_id}/stats", params={
        "stats": "statSplits", "group": "pitching", "season": season, "sitCodes": "h,a",
    })
    result = _parse_home_away_splits(data, _pitching_stat_dict)
    _TEAM_PIT_HA_CACHE[key] = result
    return result


def _team_woba(s):
    """Compute team wOBA from counting stats using standard linear weights."""
    from config import WOBA_WEIGHTS as W
    ab  = _i(s.get("atBats", 0))
    bb  = _i(s.get("baseOnBalls", 0))
    ibb = _i(s.get("intentionalWalks", 0))
    hbp = _i(s.get("hitByPitch", 0))
    sf  = _i(s.get("sacFlies", 0))
    h   = _i(s.get("hits", 0))
    d2  = _i(s.get("doubles", 0))
    d3  = _i(s.get("triples", 0))
    hr  = _i(s.get("homeRuns", 0))
    ubb = max(0, bb - ibb)
    b1  = max(0, h - d2 - d3 - hr)   # singles
    num = (W["bb"] * ubb + W["hbp"] * hbp + W["1b"] * b1
           + W["2b"] * d2 + W["3b"] * d3 + W["hr"] * hr)
    den = ab + bb - ibb + sf + hbp
    return (num / den) if den > 0 else None


def _hitting_stat_dict(s):
    g  = max(_i(s.get("gamesPlayed", 1)), 1)
    pa = max(_i(s.get("plateAppearances", 1)), 1)
    return {
        "avg":           _f(s.get("avg", 0.250)),
        "obp":           _f(s.get("obp", 0.320)),
        "slg":           _f(s.get("slg", 0.400)),
        "ops":           _f(s.get("ops", 0.720)),
        "runs_per_game": _i(s.get("runs", 0)) / g,
        "woba":          _team_woba(s),
        "pa_per_game":   pa / g,
        "k_pct":         _i(s.get("strikeOuts", 0)) / pa,
        "bb_pct":        _i(s.get("baseOnBalls", 0)) / pa,
        "games":         g,
    }


def _pitching_stat_dict(s):
    g  = max(_i(s.get("gamesPlayed", 1)), 1)
    ip = max(_f(s.get("inningsPitched", 1)), 1)
    return {
        "era":                   _f(s.get("era", LEAGUE_AVG_ERA)),
        "whip":                  _f(s.get("whip", 1.30)),
        "runs_allowed_per_game": _i(s.get("runs", 0)) / g,
        "k9":                    _i(s.get("strikeOuts", 0)) / ip * 9,
        "bb9":                   _i(s.get("baseOnBalls", 0)) / ip * 9,
    }


def _parse_team_hitting(data):
    if not data:
        return {}
    for sg in data.get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        return _hitting_stat_dict(splits[0]["stat"])
    return {}


def _parse_team_pitching(data):
    if not data:
        return {}
    for sg in data.get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        return _pitching_stat_dict(splits[0]["stat"])
    return {}


def _parse_home_away_splits(data, stat_dict_fn):
    result = {"home": {}, "away": {}}
    if not data:
        return result
    for sg in data.get("stats", []):
        for split in sg.get("splits", []):
            code = split.get("split", {}).get("code", "")
            if code == "h":
                result["home"] = stat_dict_fn(split["stat"])
            elif code == "a":
                result["away"] = stat_dict_fn(split["stat"])
    return result


def _parse_team_fielding(data):
    if not data:
        return {}
    for sg in data.get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0]["stat"]
        g  = max(_i(s.get("gamesPlayed", 1)), 1)
        errors = _i(s.get("errors", 0))
        dp     = _i(s.get("doublePlays", 0))
        return {
            "fielding_pct":    _f(s.get("fielding", 0.984)),
            "errors":          errors,
            "double_plays":    dp,
            "errors_per_game": errors / g,
            "dp_per_game":     dp / g,
            "games":           g,
        }
    return {}


# ── Momentum & head-to-head ───────────────────────────────────────────────────

_MOMENTUM_CACHE = {}
_H2H_CACHE      = {}


def get_team_recent_results(team_id, last_n=10):
    """
    Last `last_n` completed games for a team.
    Returns {wins, losses, games_played, win_pct, run_diff,
             run_diff_per_game, streak, streak_type} or {}.
    """
    if team_id in _MOMENTUM_CACHE:
        return _MOMENTUM_CACHE[team_id]

    import datetime as _dt
    end_date   = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    start_date = (_dt.date.today() - _dt.timedelta(days=35)).isoformat()

    data = _get("/schedule", params={
        "sportId":   1,
        "teamId":    team_id,
        "startDate": start_date,
        "endDate":   end_date,
        "hydrate":   "linescore,team",
    })

    games = []
    for date_entry in (data or {}).get("dates", []):
        for raw in date_entry.get("games", []):
            state = raw.get("status", {}).get("abstractGameState", "")
            if state not in ("Final", "Game Over", "Completed Early"):
                continue
            home       = raw["teams"]["home"]
            away       = raw["teams"]["away"]
            is_home    = (home["team"]["id"] == team_id)
            my_score   = _i((home if is_home else away).get("score", 0))
            opp_score  = _i((away if is_home else home).get("score", 0))
            games.append({
                "date":     date_entry["date"],
                "won":      my_score > opp_score,
                "run_diff": my_score - opp_score,
            })

    games.sort(key=lambda x: x["date"])
    recent = games[-last_n:]
    if not recent:
        _MOMENTUM_CACHE[team_id] = {}
        return {}

    wins     = sum(1 for g in recent if g["won"])
    losses   = len(recent) - wins
    run_diff = sum(g["run_diff"] for g in recent)

    # Current streak
    streak_type = "W" if recent[-1]["won"] else "L"
    streak = 0
    for g in reversed(recent):
        if (g["won"] and streak_type == "W") or (not g["won"] and streak_type == "L"):
            streak += 1
        else:
            break

    result = {
        "wins":              wins,
        "losses":            losses,
        "games_played":      len(recent),
        "win_pct":           wins / len(recent),
        "run_diff":          run_diff,
        "run_diff_per_game": run_diff / len(recent),
        "streak":            streak,
        "streak_type":       streak_type,
    }
    _MOMENTUM_CACHE[team_id] = result
    return result


def get_head_to_head(team_id, opp_team_id, season=None):
    """
    This-season H2H record for team_id vs opp_team_id.
    Returns {wins, losses, games_played, win_pct, run_diff} or {}.
    """
    key = (team_id, opp_team_id, season or CURRENT_SEASON)
    if key in _H2H_CACHE:
        return _H2H_CACHE[key]

    season = season or CURRENT_SEASON
    data   = _get("/schedule", params={
        "sportId": 1,
        "teamId":  team_id,
        "season":  season,
        "hydrate": "linescore,team",
    })

    wins = losses = run_diff = games_played = 0
    for date_entry in (data or {}).get("dates", []):
        for raw in date_entry.get("games", []):
            state = raw.get("status", {}).get("abstractGameState", "")
            if state not in ("Final", "Game Over", "Completed Early"):
                continue
            home     = raw["teams"]["home"]
            away     = raw["teams"]["away"]
            home_id  = home["team"]["id"]
            away_id  = away["team"]["id"]
            if {team_id, opp_team_id} != {home_id, away_id}:
                continue
            is_home   = (home_id == team_id)
            my_score  = _i((home if is_home else away).get("score", 0))
            opp_score = _i((away if is_home else home).get("score", 0))
            if my_score > opp_score:
                wins += 1
            elif opp_score > my_score:
                losses += 1
            run_diff     += my_score - opp_score
            games_played += 1

    if games_played == 0:
        _H2H_CACHE[key] = {}
        return {}

    result = {
        "wins":         wins,
        "losses":       losses,
        "games_played": games_played,
        "win_pct":      wins / games_played,
        "run_diff":     run_diff,
    }
    _H2H_CACHE[key] = result
    return result


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


def _fip(so, bb, hr, ip):
    """
    Fielding Independent Pitching from actual K, BB, HR — the three true-outcome
    events a pitcher controls, independent of defense.  Using real HR (not an
    earned-runs proxy) keeps FIP genuinely decoupled from ERA.
    """
    if ip <= 0:
        return LEAGUE_AVG_ERA
    return max(1.0, ((13 * hr + 3 * bb - 2 * so) / ip) + 3.10)


# ── Batter season stats ───────────────────────────────────────────────────────

_BATTER_STATS_CACHE  = {}
_BATTER_RECENT_CACHE = {}

def get_player_season_stats(player_id, season=None):
    """Return hitting season stats for a batter: {avg, obp, slg, ops, ...}"""
    season = season or CURRENT_SEASON
    key = (player_id, season)
    if key in _BATTER_STATS_CACHE:
        return _BATTER_STATS_CACHE[key]

    data = _get(f"/people/{player_id}/stats", params={
        "stats": "season", "group": "hitting", "season": season,
    })
    result = {}
    for sg in (data or {}).get("stats", []):
        splits = sg.get("splits", [])
        if not splits:
            continue
        s = splits[0]["stat"]
        pa = max(_i(s.get("plateAppearances", 1)), 1)
        result = {
            "avg":    _f(s.get("avg",  0.250)),
            "obp":    _f(s.get("obp",  0.320)),
            "slg":    _f(s.get("slg",  0.400)),
            "ops":    _f(s.get("ops",  0.720)),
            "k_pct":  _i(s.get("strikeOuts",   0)) / pa,
            "bb_pct": _i(s.get("baseOnBalls",  0)) / pa,
            "games":  _i(s.get("gamesPlayed",  0)),
            "pa":     pa,
        }
        break
    _BATTER_STATS_CACHE[key] = result
    return result


def get_batter_recent_stats(player_id, last_n=15, season=None):
    """
    Return batting stats over the last *last_n* games the batter appeared in.
    Keys: recent_avg, recent_obp, recent_ab, recent_games
    Falls back to empty dict if no game-log data is available.
    """
    season = season or CURRENT_SEASON
    key = (player_id, last_n, season)
    if key in _BATTER_RECENT_CACHE:
        return _BATTER_RECENT_CACHE[key]

    data = _get(f"/people/{player_id}/stats", params={
        "stats": "gameLog", "group": "hitting", "season": season,
    })
    games = []
    for sg in (data or {}).get("stats", []):
        for split in sg.get("splits", []):
            s = split["stat"]
            ab = _i(s.get("atBats", 0))
            if ab == 0:          # skip off-days / pinch appearances w/ 0 AB
                continue
            games.append({
                "date": split.get("date", ""),
                "ab":   ab,
                "h":    _i(s.get("hits", 0)),
                "bb":   _i(s.get("baseOnBalls", 0)),
                "pa":   _i(s.get("plateAppearances", ab)),
            })

    games.sort(key=lambda x: x["date"], reverse=True)
    recent = games[:last_n]

    result = {}
    if recent:
        total_ab = sum(g["ab"] for g in recent)
        total_h  = sum(g["h"]  for g in recent)
        total_pa = sum(g["pa"] for g in recent)
        total_bb = sum(g["bb"] for g in recent)
        result = {
            "recent_avg":   total_h  / total_ab if total_ab > 0 else 0.250,
            "recent_obp":   (total_h + total_bb) / total_pa if total_pa > 0 else 0.320,
            "recent_ab":    total_ab,
            "recent_games": len(recent),
        }

    _BATTER_RECENT_CACHE[key] = result
    return result


# ── Lineup & player info ───────────────────────────────────────────────────────

_PLAYER_INFO_CACHE = {}
_PLATOON_CACHE     = {}
_BVP_CACHE         = {}

def get_lineup(game_pk):
    """Return {home: [player_id, ...], away: [player_id, ...]} in batting order."""
    data = _get(f"/game/{game_pk}/boxscore")
    if not data:
        return {}
    result = {}
    for side in ("home", "away"):
        order = data.get("teams", {}).get(side, {}).get("battingOrder", [])
        result[side] = [int(p) for p in order if p]
    return result


def get_player_info(player_id):
    """Return {bat_side: L/R/S, pitch_hand: L/R, full_name: str}."""
    if player_id in _PLAYER_INFO_CACHE:
        return _PLAYER_INFO_CACHE[player_id]
    data = _get(f"/people/{player_id}")
    if not data or not data.get("people"):
        return {"bat_side": "R", "pitch_hand": "R", "full_name": ""}
    p = data["people"][0]
    info = {
        "bat_side":   p.get("batSide",   {}).get("code", "R"),
        "pitch_hand": p.get("pitchHand", {}).get("code", "R"),
        "full_name":  p.get("fullName",  ""),
    }
    _PLAYER_INFO_CACHE[player_id] = info
    return info


def get_pitcher_platoon_splits(pitcher_id, season=None):
    """
    Return pitcher splits by batter handedness (vl = vs LHB, vr = vs RHB).
    Includes ERA, WHIP, AVG, K9, BB9 for each handedness.
    """
    key = (pitcher_id, season or CURRENT_SEASON)
    if key in _PLATOON_CACHE:
        return _PLATOON_CACHE[key]
    season = season or CURRENT_SEASON
    data = _get(f"/people/{pitcher_id}/stats", params={
        "stats": "statSplits", "group": "pitching",
        "season": season, "sitCodes": "vl,vr",
    })
    result = {}
    for sg in (data or {}).get("stats", []):
        for split in sg.get("splits", []):
            code = split.get("split", {}).get("code", "")
            s    = split.get("stat", {})
            ip   = _f(s.get("inningsPitched", 0))
            so   = _i(s.get("strikeOuts", 0))
            bb   = _i(s.get("baseOnBalls", 0))
            era  = _f(s.get("era",  None))
            whip = _f(s.get("whip", None))
            avg  = _f(s.get("avg",  None))
            k9   = so / ip * 9 if ip > 0 else None
            bb9  = bb / ip * 9 if ip > 0 else None
            if code == "vl":
                result["era_vs_lhb"]  = era  if era  else None
                result["whip_vs_lhb"] = whip if whip else None
                result["avg_vs_lhb"]  = avg  if avg  else None
                result["k9_vs_lhb"]   = k9
                result["bb9_vs_lhb"]  = bb9
            elif code == "vr":
                result["era_vs_rhb"]  = era  if era  else None
                result["whip_vs_rhb"] = whip if whip else None
                result["avg_vs_rhb"]  = avg  if avg  else None
                result["k9_vs_rhb"]   = k9
                result["bb9_vs_rhb"]  = bb9
    _PLATOON_CACHE[key] = result
    return result


_PITCHER_HA_CACHE = {}
_BATTER_HA_CACHE  = {}


def get_pitcher_home_away_splits(pitcher_id, season=None):
    """Return {'home': {'era','k9','bb9'}, 'away': {...}} for this pitcher individually."""
    key = (pitcher_id, season or CURRENT_SEASON)
    if key in _PITCHER_HA_CACHE:
        return _PITCHER_HA_CACHE[key]
    season = season or CURRENT_SEASON
    data = _get(f"/people/{pitcher_id}/stats", params={
        "stats": "statSplits", "group": "pitching", "season": season, "sitCodes": "h,a",
    })
    result = {"home": {}, "away": {}}
    for sg in (data or {}).get("stats", []):
        for split in sg.get("splits", []):
            code = split.get("split", {}).get("code", "")
            side = "home" if code == "h" else "away" if code == "a" else None
            if not side:
                continue
            s  = split.get("stat", {})
            ip = _f(s.get("inningsPitched", 0))
            result[side] = {
                "era": _f(s.get("era", None)) or None,
                "k9":  (_i(s.get("strikeOuts", 0)) / ip * 9) if ip > 0 else None,
                "bb9": (_i(s.get("baseOnBalls", 0)) / ip * 9) if ip > 0 else None,
            }
    _PITCHER_HA_CACHE[key] = result
    return result


def get_batter_home_away_splits(batter_id, season=None):
    """Return {'home': {'avg'}, 'away': {'avg'}} for this batter individually."""
    key = (batter_id, season or CURRENT_SEASON)
    if key in _BATTER_HA_CACHE:
        return _BATTER_HA_CACHE[key]
    season = season or CURRENT_SEASON
    data = _get(f"/people/{batter_id}/stats", params={
        "stats": "statSplits", "group": "hitting", "season": season, "sitCodes": "h,a",
    })
    result = {"home": {}, "away": {}}
    for sg in (data or {}).get("stats", []):
        for split in sg.get("splits", []):
            code = split.get("split", {}).get("code", "")
            side = "home" if code == "h" else "away" if code == "a" else None
            if not side:
                continue
            avg = _f(split.get("stat", {}).get("avg", None)) or None
            result[side] = {"avg": avg}
    _BATTER_HA_CACHE[key] = result
    return result


def get_batter_vs_pitcher(batter_id, pitcher_id):
    """
    Return career batter vs pitcher matchup stats:
    {pa, ab, hits, hr, avg, ops, k, bb}
    """
    key = (batter_id, pitcher_id)
    if key in _BVP_CACHE:
        return _BVP_CACHE[key]
    data = _get(f"/people/{batter_id}/stats", params={
        "stats": "vsPlayer", "group": "hitting",
        "opposingPlayerId": pitcher_id,
    })
    result = {}
    for sg in (data or {}).get("stats", []):
        splits = sg.get("splits", [])
        if splits:
            s = splits[0]["stat"]
            result = {
                "pa":   _i(s.get("plateAppearances", 0)),
                "ab":   _i(s.get("atBats", 0)),
                "hits": _i(s.get("hits", 0)),
                "hr":   _i(s.get("homeRuns", 0)),
                "k":    _i(s.get("strikeOuts", 0)),
                "bb":   _i(s.get("baseOnBalls", 0)),
                "avg":  _f(s.get("avg",  0.250)),
                "ops":  _f(s.get("ops",  0.720)),
                "obp":  _f(s.get("obp",  0.320)),
                "slg":  _f(s.get("slg",  0.400)),
            }
    _BVP_CACHE[key] = result
    return result


def get_pitcher_rest_days(pitcher_id, season=None):
    """
    Return days since the pitcher's last appearance, estimated last-outing
    pitch count, and IP from that appearance.

    Returns dict: {days_rest, last_ip, est_pitch_count}
    Defaults to {days_rest: 5, last_ip: 5.5, est_pitch_count: 80} when no data.
    """
    import datetime as _dt
    recent = get_pitcher_recent_stats(pitcher_id, last_n=1, season=season)
    games  = recent.get("recent_games", [])
    if not games:
        return {"days_rest": 5, "last_ip": 5.5, "est_pitch_count": 80}
    last = games[0]
    try:
        last_date = _dt.date.fromisoformat(last["date"])
        days_rest = (_dt.date.today() - last_date).days
    except Exception:
        days_rest = 5
    last_ip        = last.get("ip", 5.5)
    est_pitch_count = round(last_ip * 15.5)
    return {
        "days_rest":       max(0, days_rest),
        "last_ip":         last_ip,
        "est_pitch_count": est_pitch_count,
    }


def get_game_officials(game_pk):
    """
    Return {home_plate_ump: str, ump_id: int} for a game, or {} if unavailable.
    Uses the umpire API module to avoid duplicating the schedule fetch.
    """
    from data.umpire_api import get_game_umpire
    name = get_game_umpire(game_pk)
    return {"home_plate_ump": name} if name else {}
