"""The Odds API wrapper — pulls FanDuel lines for MLB games."""
import hashlib
import json
import time
import requests
from pathlib import Path
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

# File-based cache settings
_CACHE_DIR = Path(__file__).parent / ".odds_cache"
_CACHE_TTL = 4 * 3600   # 4 hours in seconds — re-fetch if older than this

_requests_remaining = None
_mem_cache = {}          # session-level memory cache: cache_key → data


def _cache_path(cache_key: str) -> Path:
    digest = hashlib.md5(cache_key.encode()).hexdigest()
    return _CACHE_DIR / f"{digest}.json"


def _file_cache_get(cache_key: str):
    """Return cached data if it exists and is within TTL, else None."""
    path = _cache_path(cache_key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - payload["ts"] > _CACHE_TTL:
            return None   # expired
        return payload["data"]
    except Exception:
        return None


def _file_cache_set(cache_key: str, data):
    """Write data to the file cache with current timestamp."""
    _CACHE_DIR.mkdir(exist_ok=True)
    path = _cache_path(cache_key)
    try:
        path.write_text(
            json.dumps({"ts": time.time(), "data": data}),
            encoding="utf-8",
        )
    except Exception:
        pass   # cache write failure is non-fatal


def _get(endpoint, params=None):
    global _requests_remaining
    if not ODDS_API_KEY:
        return None

    # Build a stable string key from endpoint + sorted params (excluding apiKey)
    sorted_params = tuple(sorted((params or {}).items()))
    cache_key = f"{endpoint}|{sorted_params}"

    # 1. Memory cache (same process)
    if cache_key in _mem_cache:
        return _mem_cache[cache_key]

    # 2. File cache (across runs, within TTL)
    cached = _file_cache_get(cache_key)
    if cached is not None:
        _mem_cache[cache_key] = cached
        return cached

    # 3. Live API call
    url = f"{BASE_URL}{endpoint}"
    full_params = {**(params or {}), "apiKey": ODDS_API_KEY}
    try:
        r = requests.get(url, params=full_params, timeout=15)
        _requests_remaining = r.headers.get("x-requests-remaining", "?")
        r.raise_for_status()
        data = r.json()
        _mem_cache[cache_key] = data
        _file_cache_set(cache_key, data)
        return data
    except Exception as e:
        print(f"  [Odds API] {endpoint} failed: {e}")
        return None


def get_requests_remaining():
    return _requests_remaining


# ── Game-level odds ───────────────────────────────────────────────────────────

def get_mlb_odds():
    """Fetch FanDuel moneyline + totals for all today's games."""
    import datetime as _dt
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

    # Cut-off: drop games that commenced more than 3 hours ago (already started/finished)
    _now = _dt.datetime.now(_dt.timezone.utc)
    _cutoff = _now - _dt.timedelta(hours=3)

    games = []
    for event in data:
        ct = event.get("commence_time", "")
        if ct:
            try:
                ct_dt = _dt.datetime.fromisoformat(ct.replace("Z", "+00:00"))
                if ct_dt < _cutoff:
                    continue   # stale — game already started/finished
            except Exception:
                pass
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
            # 1st inning O/U (0.5 line) = YRFI / NRFI
            "nrfi_yes_odds":    None,   # NRFI = Under 0.5 first-inning runs
            "nrfi_no_odds":     None,   # YRFI = Over  0.5 first-inning runs
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
                elif market["key"] == "totals_1st_1_innings":
                    for o in market["outcomes"]:
                        if o.get("point", 0) == 0.5:
                            if o["name"] == "Under":
                                g["nrfi_yes_odds"] = o["price"]   # NRFI
                            else:
                                g["nrfi_no_odds"] = o["price"]    # YRFI
        # Skip games with no active FD lines (already completed or pre-market)
        has_lines = (
            g["moneyline_home"] is not None
            or g["total_line"] is not None
        )
        if not has_lines:
            continue

        games.append(g)

    # Sort ascending by commence_time so upcoming games match before stale ones
    games.sort(key=lambda x: x.get("commence_time", ""))
    return games


# ── First-inning (NRFI/YRFI) odds ────────────────────────────────────────────

def get_nrfi_odds(event_id):
    """
    Return {nrfi_odds, yrfi_odds} from FanDuel's first-inning totals market
    (totals_1st_1_innings, 0.5-run line).  NRFI = Under 0.5, YRFI = Over 0.5.
    Returns {} if market not available.  Cached per session.
    """
    if not event_id:
        return {}
    data = _get(f"/sports/{SPORT}/events/{event_id}/odds", params={
        "regions":    REGION,
        "bookmakers": BOOKMAKER,
        "markets":    "totals_1st_1_innings",
        "oddsFormat": "american",
    })
    if not data:
        return {}
    for bk in data.get("bookmakers", []):
        if bk["key"] != BOOKMAKER:
            continue
        for market in bk.get("markets", []):
            if market["key"] != "totals_1st_1_innings":
                continue
            result = {}
            for o in market.get("outcomes", []):
                if o.get("point", 0) != 0.5:
                    continue
                if o["name"] == "Under":
                    result["nrfi_odds"] = o["price"]   # NRFI
                elif o["name"] == "Over":
                    result["yrfi_odds"] = o["price"]   # YRFI
            if result:
                return result
    return {}


# ── Player props ──────────────────────────────────────────────────────────────

def get_player_props(event_id, markets=None, bookmaker=None):
    """
    Fetch player prop odds for a game from `bookmaker` (default FanDuel).
    Returns dict: {market_key: {player_name: {over: {line, odds}, under: {line, odds}}}}

    Note: FanDuel's Odds API feed only carries pitcher_strikeouts among player
    props; batter props must be sourced from another book (e.g. DraftKings).
    """
    if not ODDS_API_KEY or not event_id:
        return {}

    bookmaker = bookmaker or BOOKMAKER
    markets = markets or ["pitcher_strikeouts", "batter_hits", "batter_total_bases"]
    data = _get(f"/sports/{SPORT}/events/{event_id}/odds", params={
        "regions": REGION,
        "markets": ",".join(markets),
        "bookmakers": bookmaker,
        "oddsFormat": "american",
    })
    if not data:
        return {}

    props = {}
    for bk in data.get("bookmakers", []):
        if bk["key"] != bookmaker:
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
