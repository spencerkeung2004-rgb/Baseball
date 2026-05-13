"""Weather data via wttr.in (free, no API key required)."""
import re
import requests
from config import TEAM_CITIES, DOME_STADIUMS, WIND_OUT_FACTOR_PER_10MPH, WIND_IN_FACTOR_PER_10MPH, TEMP_FACTOR_PER_10F

_CACHE = {}


def get_weather_for_city(city):
    """Return weather dict for a city. Cached per session."""
    if city in _CACHE:
        return _CACHE[city]

    try:
        url = f"https://wttr.in/{city.replace(' ', '+')}?format=j1"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        data = r.json()
        cur = data["current_condition"][0]
        result = {
            "temp_f":    int(cur.get("temp_F", 72)),
            "wind_mph":  int(cur.get("windspeedMiles", 5)),
            "wind_dir":  cur.get("winddir16Point", "N"),
            "condition": cur.get("weatherDesc", [{}])[0].get("value", "Clear"),
            "is_outdoor": True,
        }
    except Exception as e:
        print(f"  [Weather] Failed for {city}: {e}")
        result = {"temp_f": 72, "wind_mph": 5, "wind_dir": "N",
                  "condition": "Unknown", "is_outdoor": True}

    _CACHE[city] = result
    return result


def get_game_weather(home_abbr, mlb_weather_dict=None):
    """Get weather for a game, respecting dome stadiums."""
    if home_abbr in DOME_STADIUMS:
        return {"temp_f": 72, "wind_mph": 0, "wind_dir": "N",
                "condition": "Dome", "is_outdoor": False}

    # Try to parse weather from MLB API payload first
    if mlb_weather_dict:
        parsed = _parse_mlb_weather(mlb_weather_dict)
        if parsed:
            return parsed

    city = TEAM_CITIES.get(home_abbr, "New York")
    return get_weather_for_city(city)


def _parse_mlb_weather(d):
    """Parse MLB API weather dict: {temp, wind, condition}."""
    if not d:
        return None
    try:
        temp_str = d.get("temp", "")
        wind_str = d.get("wind", "")
        condition = d.get("condition", "Outdoor")

        temp = int(re.search(r"\d+", str(temp_str)).group()) if re.search(r"\d+", str(temp_str)) else 72
        wind_match = re.search(r"(\d+)\s*mph", str(wind_str), re.I)
        wind_mph = int(wind_match.group(1)) if wind_match else 5

        wind_dir = "N"
        ws = wind_str.lower()
        if "out" in ws:
            wind_dir = "OUT"
        elif " in" in ws or "in " in ws:
            wind_dir = "IN"

        return {"temp_f": temp, "wind_mph": wind_mph, "wind_dir": wind_dir,
                "condition": condition, "is_outdoor": True}
    except Exception:
        return None


def weather_run_factor(weather):
    """
    Multiplier on expected runs for given weather.
    1.0 = neutral, >1.0 = more runs expected, <1.0 = fewer.
    """
    if not weather.get("is_outdoor", True):
        return 1.0

    temp     = weather.get("temp_f", 72)
    wind_mph = weather.get("wind_mph", 5)
    wind_dir = str(weather.get("wind_dir", "N")).upper()
    factor   = 1.0

    # Temperature: warm air = ball carries farther
    if temp > 70:
        factor += ((temp - 70) / 10) * TEMP_FACTOR_PER_10F
    elif temp < 50:
        factor -= ((50 - temp) / 10) * (TEMP_FACTOR_PER_10F * 1.3)

    # Wind direction
    if "OUT" in wind_dir:
        factor += (wind_mph / 10) * WIND_OUT_FACTOR_PER_10MPH
    elif "IN" in wind_dir:
        factor -= (wind_mph / 10) * WIND_IN_FACTOR_PER_10MPH
    else:
        # Crosswind gives a small lift to fly balls
        factor += (wind_mph / 10) * 0.01

    return max(0.70, min(1.50, factor))
