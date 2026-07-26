"""
Betting engine.

Scans today's games, computes edge vs FanDuel lines, sizes via fractional Kelly,
and returns the top DAILY_PICKS bets (single-leg or parlay).
"""
import itertools
from config import (
    MIN_ODDS_AMERICAN, MIN_EDGE, MAX_UNITS, MIN_UNITS, UNIT_SIZE,
    STARTING_BANKROLL, KELLY_FRACTION, BATTER_PROPS_BOOKMAKER,
    DAILY_PICKS, CAL_TARGET_SAMPLES, CAL_MAINTENANCE_PER_TYPE,
)
from data.odds_api import (
    american_to_decimal, american_to_implied_prob, decimal_to_american,
    remove_vig, get_mlb_odds, get_player_props, match_fd_game, get_nrfi_odds,
)
from models.game_model import (
    project_game, total_over_prob, total_under_prob, ks_over_prob, ks_under_prob,
)
from models.calibration import calibrated_prob, load_cal_weights

# Load calibration weights once per process — updated by `python main.py calibrate`
_CAL_WEIGHTS = load_cal_weights()


# ── Main entry point ──────────────────────────────────────────────────────────

def find_daily_bets(games):
    """
    Given today's list of game dicts (from mlb_api.get_schedule),
    return up to DAILY_PICKS bet dicts sorted by edge descending.
    Parlays from different games compete directly with single-leg picks.
    """
    fd_games = get_mlb_odds()
    single_leg = []

    for game in games:
        fd = match_fd_game(game, fd_games)
        proj = project_game(game)

        single_leg.extend(_moneyline_bets(proj, fd))
        single_leg.extend(_total_bets(proj, fd))
        single_leg.extend(_nrfi_bets(proj, fd))

        if fd and fd.get("event_id"):
            # Strikeouts from FanDuel; batter props from DraftKings (FanDuel's
            # batter-prop lines aren't exposed through the Odds API).
            props = get_player_props(fd["event_id"], markets=["pitcher_strikeouts"])
            props.update(get_player_props(
                fd["event_id"],
                markets=["batter_hits", "batter_total_bases"],
                bookmaker=BATTER_PROPS_BOOKMAKER,
            ))
            single_leg.extend(_pitcher_k_bets(proj, props))
            single_leg.extend(_batter_hits_bets(proj, props))
            single_leg.extend(_batter_tb_bets(proj, props))

    # Bet types that must have CAL_TARGET_SAMPLES calibration data before
    # they are eligible for staked picks.  Until then they are tracking-only.
    # batter_hits is newly live (FanDuel never exposed the market, so it has 0
    # settled bets and no calibration) and its raw projections are badly skewed
    # to UNDER — gate it until it proves out with real outcome data.
    _REQUIRE_CAL = {"nrfi_nrfi", "nrfi_yrfi", "batter_hits"}
    _by_type_cal = _CAL_WEIGHTS.get("by_type", {})
    from models.calibration import _normalise_type as _nt_pre

    # Per-type edge floor overrides (raised when a type is poorly calibrated).
    # pitcher_k_under is hitting 28% vs predicted 65% — require much higher edge
    # before staking until 30 samples are reached and calibration corrects it.
    _TYPE_MIN_EDGE = {
        "pitcher_k_under": 0.30,
    }

    def _cal_eligible(b):
        t = b.get("type", "")
        if t not in _REQUIRE_CAL:
            return True
        norm = _nt_pre(t)
        samples = _by_type_cal.get(norm, {}).get("samples", 0)
        return samples >= CAL_TARGET_SAMPLES

    def _min_edge_for(b):
        return _TYPE_MIN_EDGE.get(b.get("type", ""), MIN_EDGE)

    # Filter single-leg candidates
    qualified = [
        b for b in single_leg
        if b["edge"] >= _min_edge_for(b)
        and b["american_odds"] >= MIN_ODDS_AMERICAN
        and _cal_eligible(b)
    ]
    qualified.sort(key=lambda x: x["edge"], reverse=True)

    # Calibration pool: all bets that clear min-odds but are below MIN_EDGE,
    # OR are bet types that require calibration data first (e.g. NRFI/YRFI
    # before 30 samples).  Always used for tracking picks, never for staking.
    #
    # Sort priority: uncalibrated types (< CAL_TARGET_SAMPLES) come first so
    # they fill tracking slots before already-calibrated types.
    def _cal_samples(b):
        norm = _nt_pre(b.get("type", ""))
        return _by_type_cal.get(norm, {}).get("samples", 0)

    def _cal_pool_sort_key(b):
        samples = _cal_samples(b)
        needs_cal = samples < CAL_TARGET_SAMPLES
        # Primary: needs calibration (True sorts before False with negation)
        # Secondary: edge descending within each group
        return (0 if needs_cal else 1, -b["edge"])

    cal_pool = [
        b for b in single_leg
        if b["american_odds"] >= MIN_ODDS_AMERICAN and b not in qualified
    ]
    cal_pool.sort(key=_cal_pool_sort_key)

    # Build parlays from the top pool, combining only bets from different games
    parlays = _generate_parlays(qualified[:10])

    # All candidates compete together; parlays capped at 1u (higher variance)
    all_candidates = qualified + parlays
    all_candidates.sort(key=lambda x: x["edge"], reverse=True)

    # Size each bet (parlays already have units set)
    for b in all_candidates:
        if "units" not in b:
            b["units"]         = _kelly_units(b["our_prob"], b["american_odds"],
                                              bet_type=b.get("type"), cal_weights=_CAL_WEIGHTS)
            b["stake"]         = round(b["units"] * UNIT_SIZE, 2)
            b["potential_win"] = round(b["stake"] * (american_to_decimal(b["american_odds"]) - 1), 2)

    # ── Select top DAILY_PICKS real bets ─────────────────────────────────────
    # Seed with best single so at least one straight play appears.
    picks = []
    used_legs = set()
    if qualified:
        best_single = qualified[0]
        picks.append(best_single)
        used_legs.add(best_single["description"])

    for b in all_candidates:
        if len(picks) >= DAILY_PICKS:
            break
        legs = b.get("legs") or [b["description"]]
        if used_legs & set(legs):
            continue
        picks.append(b)
        used_legs.update(legs)

    picks.sort(key=lambda x: x["edge"], reverse=True)

    # ── Append calibration-only bets (stake=0, tracking only) ────────────────
    # Every threshold-qualifying bet becomes a calibration pick.  While a type is
    # still short of CAL_TARGET_SAMPLES, collect with NO cap (rebuild fast); once
    # it reaches the target, throttle to CAL_MAINTENANCE_PER_TYPE picks/day for
    # that type — enough to keep recency-weighted calibration fresh, not flood it.
    # The pool is `qualified` plus gate-excluded `cal_pool` bets (nrfi /
    # batter_hits, pitcher_k_under's raised floor), restricted to those >= MIN_EDGE.
    from models.calibration import _normalise_type as _nt
    _by_type = _CAL_WEIGHTS.get("by_type", {})

    def _samples(bet_type):
        return _by_type.get(_nt(bet_type), {}).get("samples", 0)

    def _make_tracking(b):
        cal = dict(b)
        cal["tracking_only"] = True
        cal["units"]         = 0.0
        cal["stake"]         = 0.0
        cal["potential_win"] = 0.0
        return cal

    cal_legs_used  = set(used_legs)
    added_per_type = {}   # normalised type -> tracking picks added today
    fill_pool = [b for b in (qualified + cal_pool) if b["edge"] >= MIN_EDGE]
    fill_pool.sort(key=lambda x: x["edge"], reverse=True)

    for b in fill_pool:
        norm = _nt(b.get("type", ""))
        # Fully-calibrated types are throttled to the maintenance rate; types
        # still building toward the target collect without limit.
        if (_samples(b.get("type", "")) >= CAL_TARGET_SAMPLES
                and added_per_type.get(norm, 0) >= CAL_MAINTENANCE_PER_TYPE):
            continue
        legs = b.get("legs") or [b["description"]]
        if cal_legs_used & set(legs):
            continue   # leg already used by a staked or earlier tracking pick
        picks.append(_make_tracking(b))
        cal_legs_used.update(legs)
        added_per_type[norm] = added_per_type.get(norm, 0) + 1

    return picks


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

    for side, ml_odds, raw_prob, fair_prob in [
        ("home", home_ml, proj["home_win_prob"], fair_home),
        ("away", away_ml, proj["away_win_prob"], fair_away),
    ]:
        if ml_odds is None or ml_odds < MIN_ODDS_AMERICAN or fair_prob is None:
            continue
        team     = proj["game"][f"{side}_team"]
        our_prob = calibrated_prob(raw_prob, "moneyline", _CAL_WEIGHTS)

        # Anchor model win% toward market when the gap is large.
        # The market is efficient — a 25%+ gap almost always means model error,
        # not a genuine edge. Scale: 0% market weight at no gap, 60% cap at 24%+ gap.
        gap       = abs(our_prob - fair_prob)
        mkt_wt    = min(0.60, gap / 0.40)
        our_prob  = our_prob * (1 - mkt_wt) + fair_prob * mkt_wt

        # Use vig-removed fair probability for edge (not raw implied)
        implied  = fair_prob
        edge     = our_prob - implied

        # Momentum & H2H blurb for this side
        mom     = proj.get(f"{side}_momentum", {})
        h2h     = proj.get(f"h2h_{side}", {})
        mom_f   = proj.get(f"{side}_momentum_f", 1.0)
        h2h_f   = proj.get(f"{side}_h2h_f", 1.0)
        mom_str = ""
        if mom:
            mom_str = (f" | L10: {mom['wins']}-{mom['losses']}"
                       f" RD {mom['run_diff']:+d}"
                       f" ({mom['streak_type']}{mom['streak']})")
        h2h_str = ""
        if h2h and h2h.get("games_played", 0) >= 3:
            h2h_str = f" | H2H {h2h['wins']}-{h2h['losses']}"

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
                f"{mom_str}{h2h_str} | "
                f"Momentum {mom_f:.3f}x H2H {h2h_f:.3f}x "
                f"Defense {proj.get(f'{side}_defense_factor', 1.0):.3f}x"
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

    # Strip vig from the two-sided market when both sides are available
    if over_odds is not None and under_odds is not None:
        fair_over, fair_under = remove_vig(over_odds, under_odds)
    else:
        fair_over  = american_to_implied_prob(over_odds)  if over_odds  is not None else None
        fair_under = american_to_implied_prob(under_odds) if under_odds is not None else None

    for direction, odds, our_prob_fn, fair_implied in [
        ("OVER",  over_odds,  lambda: total_over_prob(pt, line),  fair_over),
        ("UNDER", under_odds, lambda: total_under_prob(pt, line), fair_under),
    ]:
        if odds is None or fair_implied is None or odds < MIN_ODDS_AMERICAN:
            continue
        raw_prob = our_prob_fn()
        our_prob = calibrated_prob(raw_prob, f"total_{direction.lower()}", _CAL_WEIGHTS)
        implied  = fair_implied
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
                f"Defense {proj.get('home_defense_factor', 1.0):.2f}x/"
                f"{proj.get('away_defense_factor', 1.0):.2f}x (home/away) | "
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

            # Strip vig across the over/under pair when both sides exist
            over_d  = directions.get("over",  {})
            under_d = directions.get("under", {})
            o_odds  = over_d.get("odds")
            u_odds  = under_d.get("odds")
            if o_odds is not None and u_odds is not None:
                fair_o, fair_u = remove_vig(o_odds, u_odds)
            else:
                fair_o = american_to_implied_prob(o_odds) if o_odds is not None else None
                fair_u = american_to_implied_prob(u_odds) if u_odds is not None else None

            for direction, prob_fn, fair_implied in [
                ("over",  lambda l: ks_over_prob(proj_ks, l),  fair_o),
                ("under", lambda l: ks_under_prob(proj_ks, l), fair_u),
            ]:
                if direction not in directions or fair_implied is None:
                    continue
                d        = directions[direction]
                line     = d["line"]
                odds     = d["odds"]
                if odds < MIN_ODDS_AMERICAN:
                    continue
                raw_prob = prob_fn(line)
                our_prob = calibrated_prob(raw_prob, f"pitcher_k_{direction}", _CAL_WEIGHTS)
                implied  = fair_implied
                our_prob = _anchor_to_market(our_prob, implied)   # cap implausible prop edges
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
                        f"K/9: {k_data['k9']:.1f} | "
                        f"rest x{k_data.get('rest_k_factor', 1.0):.2f} | "
                        f"ump K x{k_data.get('umpire', (1.0,1.0))[0]:.2f} | "
                        f"confidence: {k_data['confidence']}"
                    ),
                })
    return bets


# ── Batter hits ───────────────────────────────────────────────────────────────

# Expected plate appearances by batting-order position (1=leadoff … 9=last).
_PA_BY_POS      = [4.3, 4.2, 4.1, 4.0, 3.9, 3.8, 3.7, 3.6, 3.5]
_WALK_RATE      = 0.085   # league-avg walk rate (PA → AB conversion)
_LEAGUE_AVG     = 0.250   # fallback batting average
_MIN_BVP_PA     = 8       # minimum career PA before BvP is blended in
_MAX_BVP_WEIGHT = 0.35    # cap BvP influence (reached at ~40 PA)
_MIN_BATTER_PA  = 30      # min season PA before a batter's hit projection is trusted


def _batter_hits_bets(proj, props):
    """
    Project each lineup batter's expected hits using five layers:

      1. Season batting average blended with recent form (last 15 games)
      1b. This batter's own home/away split for the side he's on today
      2. Pitcher quality — opposing SP's avg-allowed, using handedness-split
         (platoon) when available: a tough pitcher suppresses hits, a weak one inflates
      3. BvP — career head-to-head average blended in when ≥ MIN_BVP_PA exist
      4. Opposing team defense — fielders convert more/fewer balls in play to
         outs than average (same error/DP-rate factor used for run projection)

    adj_avg  = (season/recent/home-away blended avg) × (pitcher_avg_allowed / league_avg) × defense_factor
             then blended with bvp_avg weighted by career PA vs this pitcher
    proj_hits = adj_avg × expected_AB(lineup_pos)

    Poisson P(hits ≥ line+1) / P(hits ≤ line) vs FanDuel implied prob.
    Requires posted lineups; returns [] silently when pending.
    """
    from scipy.stats import binom
    from config import BLEND_SEASON_RATE, BLEND_SEASON_HOME_AWAY, XBA_WEIGHT
    from data.mlb_api import (
        get_player_season_stats, get_batter_recent_stats, get_player_info,
        get_pitcher_season_stats, get_pitcher_platoon_splits,
        get_batter_vs_pitcher, get_batter_home_away_splits,
    )
    from data.savant_api import get_batter_xstats

    bets    = []
    h_props = props.get("batter_hits", {})
    if not h_props:
        return bets

    xstats = get_batter_xstats()   # {player_id: {est_ba, ...}} — one fetch, cached

    matchup         = proj.get("matchup", {})
    home_ids        = matchup.get("home_lineup", [])
    away_ids        = matchup.get("away_lineup", [])
    if not home_ids and not away_ids:
        return bets   # lineups not yet posted

    game            = proj.get("game", {})
    home_pitcher_id = game.get("home_pitcher_id")
    away_pitcher_id = game.get("away_pitcher_id")

    # Pre-fetch pitcher stats (cached per session)
    def _sp_stats(pid):
        if not pid:
            return {}, {}
        return (
            get_pitcher_season_stats(pid) or {},
            get_pitcher_platoon_splits(pid) or {},
        )

    home_sp_season, home_sp_platoon = _sp_stats(home_pitcher_id)
    away_sp_season, away_sp_platoon = _sp_stats(away_pitcher_id)

    def _pitcher_avg_allowed(sp_season, sp_platoon, bat_side):
        """Return pitcher avg-allowed adjusted for batter handedness."""
        if bat_side == "L":
            platoon_avg = sp_platoon.get("avg_vs_lhb")
        elif bat_side == "R":
            platoon_avg = sp_platoon.get("avg_vs_rhb")
        else:
            platoon_avg = None  # switch hitter — fall back to overall
        return platoon_avg or sp_season.get("opp_avg") or _LEAGUE_AVG

    # Build name → {id, pos, side} lookup tagged by home/away
    name_map: dict[str, dict] = {}
    for lineup, side in ((home_ids, "home"), (away_ids, "away")):
        for pos_idx, pid in enumerate(lineup):
            info = get_player_info(pid)
            name = (info.get("full_name") or "").strip()
            if name:
                name_map[name.lower()] = {
                    "id":   pid,
                    "pos":  pos_idx + 1,
                    "name": name,
                    "side": side,
                    "bat_side": info.get("bat_side", "R"),
                }

    def _match(fd_name: str):
        key = fd_name.lower().strip()
        if key in name_map:
            return name_map[key]
        fw = key.split()
        for mlb_key, data in name_map.items():
            mw = mlb_key.split()
            if fw and mw and fw[-1] == mw[-1] and fw[0][0] == mw[0][0]:
                return data
        return None

    for fd_name, directions in h_props.items():
        player = _match(fd_name)
        if not player:
            continue

        batter_id = player["id"]
        bat_side  = player["bat_side"]

        # Opposing pitcher depends on which team bats
        if player["side"] == "home":
            opp_pid, opp_season, opp_platoon = away_pitcher_id, away_sp_season, away_sp_platoon
        else:
            opp_pid, opp_season, opp_platoon = home_pitcher_id, home_sp_season, home_sp_platoon

        # Layer 1 — season avg blended with recent form (last 15 games)
        batter_stats = get_player_season_stats(batter_id)
        if batter_stats.get("pa", 0) < _MIN_BATTER_PA:
            continue   # too few PA (e.g. fresh call-up) — projection unreliable
        season_avg   = batter_stats.get("avg", _LEAGUE_AVG)

        # Layer 1a — Statcast xBA: blend expected BA (quality of contact, more
        # predictive) into the season figure before anything else.
        xba      = (xstats.get(batter_id) or {}).get("est_ba")
        xba_note = ""
        if xba and 0.100 < xba < 0.500:
            season_avg = season_avg * (1 - XBA_WEIGHT) + xba * XBA_WEIGHT
            xba_note   = f" | xBA {xba:.3f}"

        recent_stats = get_batter_recent_stats(batter_id)
        recent_avg   = recent_stats.get("recent_avg", season_avg)
        recent_games = recent_stats.get("recent_games", 0)
        # Blend: 70% season / 30% recent — same ratio used for pitcher ERA
        base_avg     = season_avg * BLEND_SEASON_RATE + recent_avg * (1 - BLEND_SEASON_RATE)
        form_note    = f"recent {recent_avg:.3f} L{recent_games}G" if recent_games else ""

        # Layer 1b — this batter's own home/away split for the side he's on
        venue_avg  = get_batter_home_away_splits(batter_id).get(player["side"], {}).get("avg")
        venue_note = ""
        if venue_avg:
            base_avg   = base_avg * BLEND_SEASON_HOME_AWAY + venue_avg * (1 - BLEND_SEASON_HOME_AWAY)
            venue_note = f" / {player['side']} {venue_avg:.3f}"

        # Layer 2 — pitcher quality (handedness-split avg allowed)
        p_avg_allowed  = _pitcher_avg_allowed(opp_season, opp_platoon, bat_side)
        pitcher_factor = p_avg_allowed / _LEAGUE_AVG          # >1 = weak SP, <1 = tough SP
        pitcher_factor = max(0.75, min(1.25, pitcher_factor))  # cap at ±25%

        # Layer 2b — opposing team defense (their fielders, not just their pitcher)
        defense_factor = proj.get(
            "away_defense_factor" if player["side"] == "home" else "home_defense_factor", 1.0
        )

        adj_avg = base_avg * pitcher_factor * defense_factor

        # Layer 3 — BvP blend
        bvp_note = ""
        if opp_pid:
            bvp      = get_batter_vs_pitcher(batter_id, opp_pid)
            bvp_pa   = bvp.get("pa", 0)
            bvp_avg  = bvp.get("avg", base_avg)
            if bvp_pa >= _MIN_BVP_PA:
                bvp_w   = min(_MAX_BVP_WEIGHT, bvp_pa / (bvp_pa + 25))
                adj_avg = adj_avg * (1 - bvp_w) + bvp_avg * bvp_w
                bvp_note = f" | BvP {bvp_avg:.3f} ({bvp_pa} PA)"

        pos       = min(player["pos"], 9)
        exp_pa    = _PA_BY_POS[pos - 1]
        exp_ab    = exp_pa * (1 - _WALK_RATE)
        proj_hits = adj_avg * exp_ab

        for direction, d in directions.items():
            line = d["line"]
            odds = d["odds"]
            if odds < MIN_ODDS_AMERICAN:
                continue

            k = int(line)   # floor: 0.5→0, 1.5→1
            # Hits ~ Binomial(AB, avg): a fixed number of at-bats, each a
            # Bernoulli trial.  This is tighter (under-dispersed) than Poisson,
            # which over-stated P(0 hits) and inflated every UNDER.
            n_ab  = max(1, round(exp_ab))
            p_hit = min(0.95, max(0.02, adj_avg))
            if direction == "over":
                our_prob = float(1.0 - binom.cdf(k, n_ab, p_hit))
            else:
                our_prob = float(binom.cdf(k, n_ab, p_hit))

            our_prob = max(0.05, min(0.95, our_prob))
            our_prob = calibrated_prob(our_prob, "batter_hits", _CAL_WEIGHTS)
            implied  = american_to_implied_prob(odds)
            our_prob = _anchor_to_market(our_prob, implied)   # cap implausible prop edges
            edge     = our_prob - implied

            bets.append({
                "type":          "batter_hits",
                "description":   f"{player['name']} {direction.upper()} {line} H",
                "game":          _game_label(proj),
                "american_odds": odds,
                "our_prob":      round(our_prob, 4),
                "implied_prob":  round(implied, 4),
                "edge":          round(edge, 4),
                "reasoning": (
                    f"[{BATTER_PROPS_BOOKMAKER}] "
                    f"Proj {proj_hits:.2f} H | season {season_avg:.3f}"
                    + xba_note
                    + (f" / {form_note}" if form_note else "")
                    + venue_note
                    + f" → base {base_avg:.3f}"
                    f" × SP {pitcher_factor:.2f} ({bat_side} vs {p_avg_allowed:.3f} allowed)"
                    f" × Def {defense_factor:.2f}"
                    f"{bvp_note} | exp AB {exp_ab:.1f} | pos {player['pos']}"
                ),
            })

    return bets


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

    # Fetch real FanDuel 1st-inning O/U odds (NRFI=Under 0.5, YRFI=Over 0.5)
    event_id = fd.get("event_id") if fd else None
    nrfi_market = get_nrfi_odds(event_id) if event_id else {}
    nrfi_yes_odds = nrfi_market.get("nrfi_odds")   # NRFI = Under 0.5
    nrfi_no_odds  = nrfi_market.get("yrfi_odds")   # YRFI = Over  0.5

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

    # Strip vig across the NRFI/YRFI pair
    fair_nrfi, fair_yrfi = remove_vig(nrfi_yes_odds, nrfi_no_odds)

    for label, odds, raw_prob, fair_implied in [
        ("NRFI", nrfi_yes_odds, nrfi_prob,        fair_nrfi),
        ("YRFI", nrfi_no_odds,  1.0 - nrfi_prob,  fair_yrfi),
    ]:
        if odds < MIN_ODDS_AMERICAN:
            continue
        our_prob = calibrated_prob(raw_prob, f"nrfi_{label.lower()}", _CAL_WEIGHTS)
        implied  = fair_implied
        edge     = our_prob - implied
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

def _generate_parlays(pool, max_legs=3):
    """
    Generate all valid 2- and 3-leg parlays from pool, combining only bets
    from different games.

    Gate: the actual parlay payout must beat the vig-free compounded price
    (product of each leg's vig-removed implied probability).  This ensures the
    book is not stacking extra parlay margin on top of individual-leg vig —
    the parlay must be genuinely better than fair, not just cheap-looking
    because the model also likes each leg individually.
    """
    parlays = []
    for n_legs in (2, 3):
        for combo in itertools.combinations(pool, n_legs):
            # Reject same-game combos (correlated outcomes)
            games = [b["game"] for b in combo]
            if len(set(games)) < n_legs:
                continue

            combined_dec  = 1.0
            combined_prob = 1.0
            fair_combined_prob = 1.0
            for leg in combo:
                combined_dec       *= american_to_decimal(leg["american_odds"])
                combined_prob      *= leg["our_prob"]
                fair_combined_prob *= leg["implied_prob"]  # vig-removed per-leg prob

            combined_american = decimal_to_american(combined_dec)
            if combined_american < MIN_ODDS_AMERICAN:
                continue

            # Core gate: actual parlay payout must beat the fair (vig-free) price.
            # fair_combined_dec = 1/fair_combined_prob is what the parlay should pay
            # at zero-vig odds.  If combined_dec is below that, the book is charging
            # extra parlay vig — no edge exists at the structural level regardless of
            # what the model thinks of the individual legs.
            fair_combined_dec = 1.0 / fair_combined_prob
            if combined_dec < fair_combined_dec:
                continue

            implied = american_to_implied_prob(combined_american)
            edge    = combined_prob - implied
            if edge < MIN_EDGE:
                continue

            units = _kelly_units_parlay(combined_prob, combined_american, cal_weights=_CAL_WEIGHTS)
            parlays.append({
                "type":               "parlay",
                "description":        "{}-Leg Parlay".format(n_legs),
                "game":               " + ".join(dict.fromkeys(games)),
                "legs":               [b["description"] for b in combo],
                "leg_details":        [
                    {
                        "description": b["description"],
                        "game":        b["game"],
                        "odds":        b["american_odds"],
                        "our_prob":    b["our_prob"],
                    }
                    for b in combo
                ],
                "american_odds":      combined_american,
                "our_prob":           round(combined_prob, 4),
                "implied_prob":       round(implied, 4),
                "fair_combined_prob": round(fair_combined_prob, 4),
                "edge":               round(edge, 4),
                "units":              units,
                "stake":              round(units * UNIT_SIZE, 2),
                "potential_win":      round(units * UNIT_SIZE * (combined_dec - 1), 2),
                "reasoning":          "{} legs | actual {} vs fair {} | {}".format(
                    n_legs,
                    combined_american,
                    decimal_to_american(fair_combined_dec),
                    " / ".join(b["description"] for b in combo),
                ),
            })

    # Surface only the best parlay per unique leg-set (no redundant subsets)
    parlays.sort(key=lambda x: x["edge"], reverse=True)
    return parlays


# ── Kelly sizing ──────────────────────────────────────────────────────────────

def _kelly_fraction(our_prob, american_odds):
    """
    Full-Kelly fraction of bankroll for a bet:  f* = (b·p − q) / b
    where b = decimal_odds − 1 (net fractional odds), p = our_prob, q = 1 − p.
    Returns 0.0 when there's no positive-expectation edge.

    Unlike the old edge/prob tiers, this is ODDS-AWARE: two bets with the same
    edge but different prices get different sizes, because the payout matters.
    """
    b = american_to_decimal(american_odds) - 1.0
    if b <= 0:
        return 0.0
    p = our_prob
    q = 1.0 - p
    f = (b * p - q) / b
    return max(0.0, f)


# Bankroll expressed in units, used to convert a Kelly bankroll-fraction into
# the engine's fixed-size unit currency (1 unit = UNIT_SIZE).
_BANKROLL_UNITS = STARTING_BANKROLL / UNIT_SIZE


def _kelly_units(our_prob, american_odds, bet_type=None, cal_weights=None):
    """
    Continuous fractional-Kelly unit sizing for singles.
      units = KELLY_FRACTION · f*(p, odds) · bankroll_units · calibration_bias
    then rounded to the nearest 0.5u (keeps the by-unit-size reports meaningful)
    and clamped to [MIN_UNITS, MAX_UNITS].

    The calibration bias still scales the stake (bias < 1 → shrink when the model
    has been overconfident on this type; > 1 → grow).
    """
    f_star = _kelly_fraction(our_prob, american_odds)
    if f_star <= 0:
        return MIN_UNITS
    bias   = _get_bias(bet_type, cal_weights)
    units  = KELLY_FRACTION * f_star * _BANKROLL_UNITS * bias
    units  = round(units * 2) / 2                # nearest 0.5u
    return max(MIN_UNITS, min(MAX_UNITS, units))


def _kelly_units_parlay(our_prob, american_odds, cal_weights=None):
    """
    Continuous fractional-Kelly for parlays — same formula as singles but hard-
    capped at 2u (parlays are higher variance).
    """
    f_star = _kelly_fraction(our_prob, american_odds)
    if f_star <= 0:
        return MIN_UNITS
    bias  = _get_bias("parlay", cal_weights)
    units = KELLY_FRACTION * f_star * _BANKROLL_UNITS * bias
    units = round(units * 2) / 2
    return max(MIN_UNITS, min(2.0, units))       # parlays hard-capped at 2u


def _get_bias(bet_type, cal_weights):
    """
    Return the bias factor to use for unit sizing.
    - If the type has any calibration data, use its bias.
    - Otherwise fall back to the overall bias.
    - If no calibration data at all, default to 1.0.
    """
    if not cal_weights:
        return 1.0
    by_type = cal_weights.get("by_type", {})
    if bet_type:
        from models.calibration import _normalise_type
        norm = _normalise_type(bet_type)
        if norm in by_type:
            return by_type[norm].get("bias", 1.0)
    # Fallback: overall bias (conservative — model is generally overconfident)
    return cal_weights.get("overall_bias", 1.0)


# ── Market anchoring ──────────────────────────────────────────────────────────

def _anchor_to_market(our_prob, market_prob, gap_scale=0.40, cap=0.60):
    """
    Pull a prop probability toward the market's implied probability when the gap
    is large — the same efficient-market anchor moneyline and totals already use.

    Player-prop lines are efficiently priced, so a big model/market disagreement
    almost always means the model over-projected that specific line (and the
    optimizer then *selects* those over-projections because they look like the
    biggest edges), not a genuine 20%+ edge.  Anchoring caps those: at the 0.60
    cap the model keeps only 40% of its disagreement with the market.
    """
    gap = abs(our_prob - market_prob)
    wt = min(cap, gap / gap_scale)
    return our_prob * (1 - wt) + market_prob * wt


# ── Helpers ───────────────────────────────────────────────────────────────────

def _game_label(proj):
    g = proj["game"]
    return f"{g['away_team']} @ {g['home_team']}"
