"""Umpire tendency factors for pitcher K and BB rates.

k_factor > 1.0 → wider strike zone → more Ks for pitcher, fewer BBs
k_factor < 1.0 → tighter zone → fewer Ks, more BBs

bb_factor is the inverse relationship (tight zone → more walks issued).
League average is (1.0, 1.0).  Magnitudes based on historical zone-size data
(typical range ±6–10% from average for the most extreme umpires).
"""
from __future__ import annotations

BASE_URL = "https://statsapi.mlb.com/api/v1"

# Static tendency table.  Populated from multi-year umpire scorecards.
# Format: "Full Name": (k_factor, bb_factor)
_UMPIRE_TENDENCIES: dict[str, tuple[float, float]] = {
    # ── Wide zone (more Ks, fewer BBs for pitcher) ────────────────────────────
    "Hunter Wendelstedt":  (1.08, 0.90),
    "Marvin Hudson":       (1.06, 0.92),
    "Dan Bellino":         (1.05, 0.93),
    "Nic Lentz":           (1.05, 0.93),
    "Alan Porter":         (1.04, 0.94),
    "Gabe Morales":        (1.04, 0.94),
    "Ramon De Jesus":      (1.04, 0.94),
    "Todd Tichenor":       (1.03, 0.95),
    "Jordan Baker":        (1.03, 0.95),
    "Eric Cooper":         (1.03, 0.95),
    "Scott Barry":         (1.03, 0.96),
    "Tim Timmons":         (1.02, 0.97),
    "Ted Barrett":         (1.02, 0.97),
    "Jeff Nelson":         (1.02, 0.97),
    "Paul Nauert":         (1.02, 0.97),
    "Sam Holbrook":        (1.01, 0.98),
    "Mike Winters":        (1.01, 0.98),
    "Tony Randazzo":       (1.01, 0.99),
    # ── Near neutral ─────────────────────────────────────────────────────────
    "Tripp Gibson":        (1.00, 1.00),
    "Adam Hamari":         (1.00, 1.00),
    "Fieldin Culbreth":    (1.00, 1.00),
    "Tom Hallion":         (1.00, 1.00),
    "Dave Meals":          (1.00, 1.00),
    "Bill Welke":          (1.00, 1.00),
    "Brian Gorman":        (1.00, 1.00),
    "Toby Basner":         (1.00, 1.00),
    "John Libka":          (1.01, 0.99),
    "Edwin Moscoso":       (1.00, 1.00),
    "Roberto Ortiz":       (0.99, 1.01),
    "Chris Guccione":      (0.99, 1.01),
    "Doug Eddings":        (0.99, 1.01),
    # ── Tight zone (fewer Ks, more BBs for pitcher) ───────────────────────────
    "James Hoye":          (0.98, 1.03),
    "Mark Carlson":        (0.98, 1.03),
    "Mike Estabrook":      (0.97, 1.04),
    "Alfonso Marquez":     (0.97, 1.04),
    "Phil Cuzzi":          (0.97, 1.04),
    "Jim Wolf":            (0.96, 1.06),
    "Paul Emmel":          (0.96, 1.05),
    "Ron Kulpa":           (0.95, 1.07),
    "Laz Diaz":            (0.95, 1.07),
    "Joe West":            (0.94, 1.08),
    "CB Bucknor":          (0.93, 1.09),
    "Angel Hernandez":     (0.92, 1.10),
}

_UMPIRE_CACHE: dict[int, str] = {}


def get_game_umpire(game_pk: int) -> str | None:
    """Return the home plate umpire's full name for game_pk, or None."""
    if game_pk in _UMPIRE_CACHE:
        return _UMPIRE_CACHE[game_pk]

    import requests
    try:
        r = requests.get(
            f"{BASE_URL}/schedule",
            params={"gamePks": game_pk, "hydrate": "officials", "sportId": 1},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
    except Exception:
        return None

    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            for official in game.get("officials", []):
                if official.get("officialType") == "Home Plate":
                    name = official.get("official", {}).get("fullName")
                    if name:
                        _UMPIRE_CACHE[game_pk] = name
                        return name
    return None


def get_umpire_factors(umpire_name: str | None) -> tuple[float, float]:
    """
    Return (k_factor, bb_factor) for the named umpire.
    Falls back to (1.0, 1.0) if unknown.
    Does a case-insensitive partial-name match as a fallback.
    """
    if not umpire_name:
        return 1.0, 1.0
    if umpire_name in _UMPIRE_TENDENCIES:
        return _UMPIRE_TENDENCIES[umpire_name]
    # Partial match (handles "H. Wendelstedt" or last-name only)
    lower = umpire_name.lower()
    for name, factors in _UMPIRE_TENDENCIES.items():
        last = name.split()[-1].lower()
        if last in lower or lower in name.lower():
            return factors
    return 1.0, 1.0
