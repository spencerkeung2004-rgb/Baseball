"""The Odds API wrapper — pulls FanDuel lines for MLB games."""
import requests
from config import ODDS_API_KEY

BASE_URL  = "https://api.the-odds-api.com/v4"
SPORT     = "baseball_mlb"
REGION    = "us"
BOOKMAKER = "fanduel"

# Available FanDuel player-prop markets
PROP_MARKETS = [
    "pitcher_strikeouts",
    "batter_hits",
    "batter_total_bases",
    "batter_home_runs",
    "batter_walks",
    "batter_strikeouts",
    "batter_rbis",
    "pitcher_hits_allowed",
    "pitcher_walks",
    "pitcher_earned_runs",
]

_requests_remaining = None


def _get(endpoint, params=None):
    global _requests_remaining
    if not ODDS_API_KEY:
        return None
    url = f"{BASE_URL}{endpoint}"
    params = {**(params or {}), "apiKey": ODDS_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=15)
        _requests_remaining = r.headers.get("x-requests-remaining", "?")
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [Odds API] {endpoint} failed: {e}")
        return None


def get_requests_remaining():
    return _requests_remaining


# ── Game-level odds ───────────────────────────────────────────────────────────

def get_mlb_odds():
    """Fetch FanDuel moneyline + totals for all today's games."""
    if not ODDS_API_KEY:
        print("  [Odds API] No ODDS_API_KEY set — skipping odds fetch.")
        return []

    data = _get(f"/sports/{SPORT}/odds", params={
        "regions": REGION,
        "markets": "h2h,totals",
        "bookmakers": BOOKMAKER,
        "oddsFormat": "american",
    })
    if not data:
        return []

    games = []
    for event in data:
        g = {
            "event_id":         event["id"],
            "home_team":        event["home_team"],
            "away_team":        event["away_team"],
            "commence_time":    event.get("commence_time", ""),
            "moneyline_home":   None,
            "moneyline_away":   None,
            "total_line":       None,
            "total_over_odds":  None,
            "total_under_odds": None,
        }
        for bk in event.get("bookmakers", []):
            if bk["key"] != BOOKMAKER:
                continue
            for market in bk.get("markets", []):
                if market["key"] == "h2h":
                    for o in market["outcomes"]:
                        if o["name"] == event["home_team"]:
                            g["moneyline_home"] = o["price"]
                        else:
                            g["moneyline_away"] = o["price"]
                elif market["key"] == "totals":
                    for o in market["outcomes"]:
                        g["total_line"] = o.get("point", 8.5)
                        if o["name"] == "Over":
                            g["total_over_odds"] = o["price"]
                        else:
                            g["total_under_odds"] = o["price"]
        games.append(g)

    return games


# ── Player props ──────────────────────────────────────────────────────────────

def get_player_props(event_id, markets=None):
    """
    Fetch FanDuel player prop odds for a game.
    Returns dict: {market_key: {player_name: {over: {line, odds}, under: {line, odds}}}}
    """
    if not ODDS_API_KEY or not event_id:
        return {}

    markets = markets or ["pitcher_strikeouts", "batter_hits", "batter_total_bases"]
    data = _get(f"/sports/{SPORT}/events/{event_id}/odds", params={
        "regions": REGION,
        "markets": ",".join(markets),
        "bookmakers": BOOKMAKER,
        "oddsFormat": "american",
    })
    if not data:
        return {}

    props = {}
    for bk in data.get("bookmakers", []):
        if bk["key"] != BOOKMAKER:
            continue
        for market in bk.get("markets", []):
            key = market["key"]
            props.setdefault(key, {})
            for o in market.get("outcomes", []):
                player    = o.get("description") or o.get("name", "")
                direction = "over" if o["name"] == "Over" else "under"
                props[key].setdefault(player, {})[direction] = {
                    "line": o.get("point", 0),
                    "odds": o["price"],
                }
    return props


# ── Odds math ─────────────────────────────────────────────────────────────────

def american_to_decimal(odds):
    if odds >= 100:
        return (odds / 100) + 1.0
    return (100 / abs(odds)) + 1.0


def american_to_implied_prob(odds):
    """No-vig implied probability from American odds."""
    if odds >= 100:
        return 100 / (odds + 100)
    return abs(odds) / (abs(odds) + 100)


def decimal_to_american(dec):
    if dec >= 2.0:
        return int((dec - 1) * 100)
    return int(-100 / (dec - 1))


def remove_vig(home_odds, away_odds):
    """Return fair (no-vig) probabilities for a two-outcome market."""
    raw_home = american_to_implied_prob(home_odds)
    raw_away = american_to_implied_prob(away_odds)
    total    = raw_home + raw_away
    return raw_home / total, raw_away / total


# ── Fuzzy team matching ───────────────────────────────────────────────────────

def match_fd_game(mlb_game, fd_games):
    """Match an MLB API game dict to a FanDuel odds dict by team name."""
    if not fd_games:
        return None
    home_words = set(mlb_game["home_team_name"].lower().split())
    away_words = set(mlb_game["away_team_name"].lower().split())
    for fd in fd_games:
        fh = set(fd["home_team"].lower().split())
        fa = set(fd["away_team"].lower().split())
        if (home_words & fh) and (away_words & fa):
            return fd
        if (home_words & fa) and (away_words & fh):
            # FanDuel has sides swapped
            return {
                **fd,
                "moneyline_home": fd.get("moneyline_away"),
                "moneyline_away": fd.get("moneyline_home"),
            }
    return None
