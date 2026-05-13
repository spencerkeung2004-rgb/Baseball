"""
Betting engine.

Scans today's games, computes edge vs FanDuel lines, sizes via fractional Kelly,
and returns the top DAILY_PICKS bets (single-leg or parlay).
"""
from config import (
    MIN_ODDS_AMERICAN, MIN_EDGE, MAX_UNITS, MIN_UNITS, UNIT_SIZE, DAILY_PICKS,
)
from data.odds_api import (
    american_to_decimal, american_to_implied_prob, decimal_to_american,
    remove_vig, get_mlb_odds, get_player_props, match_fd_game,
)
from models.game_model import (
    project_game, total_over_prob, total_under_prob, ks_over_prob, ks_under_prob,
)


# ── Main entry point ──────────────────────────────────────────────────────────

def find_daily_bets(games):
    """
    Given today's list of game dicts (from mlb_api.get_schedule),
    return up to DAILY_PICKS bet dicts sorted by edge descending.
    """
    fd_games = get_mlb_odds()
    candidates = []

    for game in games:
        fd = match_fd_game(game, fd_games)
        proj = project_game(game)

        candidates.extend(_moneyline_bets(proj, fd))
        candidates.extend(_total_bets(proj, fd))
        candidates.extend(_nrfi_bets(proj, fd))

        if fd and fd.get("event_id"):
            props = get_player_props(
                fd["event_id"],
                markets=["pitcher_strikeouts", "batter_hits", "batter_total_bases"],
            )
            candidates.extend(_pitcher_k_bets(proj, props))
            candidates.extend(_batter_hits_bets(proj, props))
            candidates.extend(_batter_tb_bets(proj, props))

    # Filter: edge threshold + minimum odds
    qualified = [
        b for b in candidates
        if b["edge"] >= MIN_EDGE and b["american_odds"] >= MIN_ODDS_AMERICAN
    ]
    qualified.sort(key=lambda x: x["edge"], reverse=True)

    # Size each bet
    for b in qualified:
        b["units"]         = _kelly_units(b["our_prob"], b["american_odds"])
        b["stake"]         = round(b["units"] * UNIT_SIZE, 2)
        b["potential_win"] = round(b["stake"] * (american_to_decimal(b["american_odds"]) - 1), 2)

    picks = list(qualified[:DAILY_PICKS])

    # Fill remaining slots with a parlay if we have extra candidates
    if len(picks) < DAILY_PICKS and len(qualified) >= 2:
        parlay = _build_parlay(qualified[:5])
        if parlay:
            picks.append(parlay)

    return picks[:DAILY_PICKS]


# ── Moneyline ─────────────────────────────────────────────────────────────────

def _moneyline_bets(proj, fd):
    bets = []
    if not fd:
        return bets

    home_ml = fd.get("moneyline_home")
    away_ml = fd.get("moneyline_away")
    game_label = _game_label(proj)

    # Remove vig to get fair probabilities
    if home_ml is not None and away_ml is not None:
        fair_home, fair_away = remove_vig(home_ml, away_ml)
    else:
        fair_home = american_to_implied_prob(home_ml) if home_ml else None
        fair_away = american_to_implied_prob(away_ml) if away_ml else None

    for side, ml_odds, our_prob, fair_prob in [
        ("home", home_ml, proj["home_win_prob"], fair_home),
        ("away", away_ml, proj["away_win_prob"], fair_away),
    ]:
        if ml_odds is None or ml_odds < MIN_ODDS_AMERICAN:
            continue
        team = proj["game"][f"{side}_team"]
        implied = american_to_implied_prob(ml_odds)
        edge    = our_prob - implied
        bets.append({
            "type":         "moneyline",
            "description":  f"{team} ML",
            "game":         game_label,
            "american_odds": ml_odds,
            "our_prob":     our_prob,
            "implied_prob": implied,
            "edge":         edge,
            "reasoning": (
                f"Proj: {proj['home_runs']} vs {proj['away_runs']} runs | "
                f"Model {our_prob:.1%} vs implied {implied:.1%} | "
                f"Park {proj['park_factor']:.2f}x | Weather {proj['weather_factor']:.2f}x"
            ),
        })
    return bets


# ── Totals ────────────────────────────────────────────────────────────────────

def _total_bets(proj, fd):
    bets = []
    if not fd:
        return bets

    line       = fd.get("total_line")
    over_odds  = fd.get("total_over_odds")
    under_odds = fd.get("total_under_odds")
    game_label = _game_label(proj)
    pt         = proj["total_runs"]

    if line is None:
        return bets

    for direction, odds, our_prob_fn in [
        ("OVER",  over_odds,  lambda: total_over_prob(pt, line)),
        ("UNDER", under_odds, lambda: total_under_prob(pt, line)),
    ]:
        if odds is None or odds < MIN_ODDS_AMERICAN:
            continue
        our_prob = our_prob_fn()
        implied  = american_to_implied_prob(odds)
        edge     = our_prob - implied
        bets.append({
            "type":         f"total_{direction.lower()}",
            "description":  f"{direction} {line} Runs",
            "game":         game_label,
            "american_odds": odds,
            "our_prob":     our_prob,
            "implied_prob": implied,
            "edge":         edge,
            "reasoning": (
                f"Proj total {pt:.1f} vs line {line} | "
                f"Weather {proj['weather_factor']:.2f}x | "
                f"Park {proj['park_factor']:.2f}x | "
                f"{proj['weather'].get('condition', '')} "
                f"{proj['weather'].get('temp_f', '')}°F "
                f"wind {proj['weather'].get('wind_mph', '')} mph "
                f"{proj['weather'].get('wind_dir', '')}"
            ),
        })
    return bets


# ── Pitcher strikeouts ────────────────────────────────────────────────────────

def _pitcher_k_bets(proj, props):
    bets = []
    k_props = props.get("pitcher_strikeouts", {})
    if not k_props:
        return bets

    for side in ("home", "away"):
        pitcher_name = proj["game"].get(f"{side}_pitcher")
        k_data       = proj.get(f"{side}_sp_ks", {})
        if not pitcher_name or not k_data:
            continue
        if k_data.get("confidence") == "low":
            continue

        proj_ks = k_data["projection"]

        for prop_player, directions in k_props.items():
            last_name = pitcher_name.split()[-1].lower()
            if last_name not in prop_player.lower():
                continue
            for direction, prob_fn in [
                ("over",  lambda l: ks_over_prob(proj_ks, l)),
                ("under", lambda l: ks_under_prob(proj_ks, l)),
            ]:
                if direction not in directions:
                    continue
                d    = directions[direction]
                line = d["line"]
                odds = d["odds"]
                if odds < MIN_ODDS_AMERICAN:
                    continue
                our_prob = prob_fn(line)
                implied  = american_to_implied_prob(odds)
                edge     = our_prob - implied
                bets.append({
                    "type":         f"pitcher_k_{direction}",
                    "description":  f"{pitcher_name} {direction.upper()} {line} Ks",
                    "game":         _game_label(proj),
                    "american_odds": odds,
                    "our_prob":     our_prob,
                    "implied_prob": implied,
                    "edge":         edge,
                    "reasoning": (
                        f"Proj {proj_ks:.1f} Ks over {k_data['ip']:.1f} IP | "
                        f"K/9: {k_data['k9']:.1f} | confidence: {k_data['confidence']}"
                    ),
                })
    return bets


# ── Batter hits ───────────────────────────────────────────────────────────────

def _batter_hits_bets(proj, props):
    """
    Compare FanDuel batter-hits lines against a simple projected hit total.
    Projection: batter_avg × expected_AB (league avg ~3.8 AB/game).
    """
    bets = []
    h_props = props.get("batter_hits", {})
    if not h_props:
        return bets

    # We don't have individual batter stats here — use batting-order position as proxy.
    # Only act when we have a clear edge on line vs projection.
    from data.mlb_api import get_player_season_stats

    for player_name, directions in h_props.items():
        for direction, d in directions.items():
            line = d["line"]
            odds = d["odds"]
            if odds < MIN_ODDS_AMERICAN:
                continue
            # Without batter ID we skip — logged once then break
            # (player props without IDs need lineup data; handled below)
        break  # placeholder loop — full batter prop analysis requires lineup fetch

    return bets  # batter hits deferred to lineup-enriched run (see future roadmap)


# ── Batter total bases ────────────────────────────────────────────────────────

def _batter_tb_bets(proj, props):
    """Placeholder: total-bases props require lineup + individual batter stats."""
    return []


# ── NRFI (No Run First Inning) ────────────────────────────────────────────────

def _nrfi_bets(proj, fd):
    """
    Evaluate NRFI props.  FanDuel lists these under alternate-runline or
    first-inning markets.  We fall back to a synthetic line when FD doesn't
    carry the market so we never miss an edge that surfaces later.
    """
    bets = []
    nrfi_prob = proj.get("nrfi_prob")
    if nrfi_prob is None:
        return bets

    game_label = _game_label(proj)

    # Try to pull FanDuel NRFI odds from the fd dict (key injected by odds_api)
    nrfi_yes_odds = fd.get("nrfi_yes_odds") if fd else None
    nrfi_no_odds  = fd.get("nrfi_no_odds")  if fd else None

    # If FD doesn't carry the market, synthesise fair odds with ~5% book margin
    if nrfi_yes_odds is None:
        fair_dec      = 1.0 / max(0.01, nrfi_prob)
        nrfi_yes_odds = decimal_to_american(round(fair_dec * 0.95, 3))
    if nrfi_no_odds is None:
        yrfi_prob     = 1.0 - nrfi_prob
        fair_dec      = 1.0 / max(0.01, yrfi_prob)
        nrfi_no_odds  = decimal_to_american(round(fair_dec * 0.95, 3))

    matchup = proj.get("matchup", {})
    home_sp = proj["game"].get("home_pitcher", "Home SP")
    away_sp = proj["game"].get("away_pitcher", "Away SP")
    has_lineup = matchup.get("has_lineup", False)
    lineup_note = "lineup posted" if has_lineup else "lineup pending"

    for label, odds, our_prob in [
        ("NRFI", nrfi_yes_odds, nrfi_prob),
        ("YRFI", nrfi_no_odds,  1.0 - nrfi_prob),
    ]:
        if odds < MIN_ODDS_AMERICAN:
            continue
        implied = american_to_implied_prob(odds)
        edge    = our_prob - implied
        bets.append({
            "type":          f"nrfi_{label.lower()}",
            "description":   f"{label} - {game_label}",
            "game":          game_label,
            "american_odds": odds,
            "our_prob":      round(our_prob, 4),
            "implied_prob":  round(implied, 4),
            "edge":          round(edge, 4),
            "reasoning": (
                "NRFI model {:.1f}% | away SP {} | home SP {} | "
                "platoon factor away {:.2f}x home {:.2f}x | {}".format(
                    nrfi_prob * 100,
                    away_sp, home_sp,
                    matchup.get("away_factor", 1.0),
                    matchup.get("home_factor", 1.0),
                    lineup_note,
                )
            ),
        })

    return bets


# ── Parlay builder ────────────────────────────────────────────────────────────

def _build_parlay(candidates, max_legs=3):
    """Combine up to max_legs independent bets into a parlay."""
    legs = candidates[:max_legs]
    if len(legs) < 2:
        return None

    combined_dec  = 1.0
    combined_prob = 1.0
    for leg in legs:
        combined_dec  *= american_to_decimal(leg["american_odds"])
        combined_prob *= leg["our_prob"]

    combined_american = decimal_to_american(combined_dec)
    if combined_american < MIN_ODDS_AMERICAN:
        return None

    implied = american_to_implied_prob(combined_american)
    edge    = combined_prob - implied
    if edge < MIN_EDGE:
        return None

    units = MIN_UNITS
    return {
        "type":         "parlay",
        "description":  f"{len(legs)}-Leg Parlay",
        "game":         " + ".join(set(l["game"] for l in legs)),
        "legs":         [l["description"] for l in legs],
        "american_odds": combined_american,
        "our_prob":     round(combined_prob, 4),
        "implied_prob": round(implied, 4),
        "edge":         round(edge, 4),
        "units":        units,
        "stake":        round(units * UNIT_SIZE, 2),
        "potential_win": round(units * UNIT_SIZE * (combined_dec - 1), 2),
        "reasoning":    f"{len(legs)} legs combined @ +{combined_american}",
    }


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def _kelly_units(our_prob, american_odds):
    """
    Tiered unit sizing:
    - 4u  : highest conviction (edge >= 25% and model prob >= 65%)
    - 2u  : strong play (edge >= 15% and model prob >= 58%)
    - 1u  : standard play (everything else that passed MIN_EDGE filter)
    """
    edge = our_prob - american_to_implied_prob(american_odds)
    if edge >= 0.25 and our_prob >= 0.65:
        return 4.0
    if edge >= 0.15 and our_prob >= 0.58:
        return 2.0
    return 1.0


# ── Helpers ───────────────────────────────────────────────────────────────────

def _game_label(proj):
    g = proj["game"]
    return f"{g['away_team']} @ {g['home_team']}"
