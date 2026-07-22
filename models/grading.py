"""
Shared bet-grading logic.

Grades a bet (moneyline / total / NRFI-YRFI / strikeout prop / parlay) against a
list of completed games from mlb_api.get_final_scores.  Used by both the
`settle --auto` flow (main.py) and the backtest harness (backtest.py).

Result codes are 'w' / 'l' / 'p' (win/loss/push) or None when undetermined
(game not final, pitcher not in boxscore, unrecognised description, …).
"""
import re


def parse_bet_description(desc):
    """Return structured form of a bet description, or None if unrecognised."""
    ml = re.match(r'^([A-Z]{2,3})\s+ML$', desc)
    if ml:
        return {"type": "ML", "team": ml.group(1)}

    kp = re.match(r'^(.+?)\s+(OVER|UNDER)\s+([\d.]+)\s+Ks?$', desc, re.IGNORECASE)
    if kp:
        return {
            "type":      "K_PROP",
            "pitcher":   kp.group(1).strip(),
            "direction": kp.group(2).upper(),
            "line":      float(kp.group(3)),
        }

    # Total runs: "OVER 7.5 Runs", "UNDER 8.0 Runs"
    tp = re.match(r'^(OVER|UNDER)\s+([\d.]+)\s+Runs?$', desc, re.IGNORECASE)
    if tp:
        return {
            "type":      "TOTAL",
            "direction": tp.group(1).upper(),
            "line":      float(tp.group(2)),
        }

    # NRFI/YRFI: "NRFI - TOR @ NYY" or "YRFI - TOR @ NYY"
    nrfi = re.match(r'^(NRFI|YRFI)\s*[-–]\s*(.+)$', desc, re.IGNORECASE)
    if nrfi:
        return {
            "type":  nrfi.group(1).upper(),   # "NRFI" or "YRFI"
        }

    return None


def get_pitcher_ks_from_boxscore(game_pk):
    from data.mlb_api import get_pitcher_ks_from_boxscore as _ks
    return _ks(game_pk)


def match_pitcher_ks(pitcher_name, ks_map):
    """
    Fuzzy-match a pitcher name against a {full_name: strikeouts} boxscore map.
    Returns (actual_ks, matched_full_name) or (None, None).
    """
    pitcher = (pitcher_name or "").lower()
    for name, ks in ks_map.items():
        if pitcher and (pitcher in name.lower() or name.lower() in pitcher):
            return ks, name
    return None, None


def grade_bet(bet, final_games, ks_cache):
    """
    Determine 'w'/'l'/'p'/None for a bet given a list of final_games.
    ks_cache is mutated in-place: {game_pk: {name: ks}}.
    Returns (raw_result, detail_str).
    """
    # ── Parlay: evaluate each leg, combine results ────────────────────────────
    if bet.get("type") == "parlay":
        legs = bet.get("legs") or []
        if not legs:
            return None, "parlay has no legs stored"

        # Each leg is a description string — pair with game from bet["game"]
        game_list = [g.strip() for g in bet["game"].split("+")]

        leg_results = []
        for i, leg_desc in enumerate(legs):
            game_str = game_list[i] if i < len(game_list) else ""
            synthetic = {"description": leg_desc, "game": game_str, "type": "single"}
            result, detail = grade_single(synthetic, final_games, ks_cache)
            leg_results.append((leg_desc, result, detail))

        details = []
        for desc, res, det in leg_results:
            details.append("{}: {} ({})".format(desc, res or "?", det))

        if any(r is None for _, r, _ in leg_results):
            undetermined = [d for _, r, d in leg_results if r is None]
            return None, "leg(s) undetermined — " + " | ".join(undetermined)
        if any(r == "l" for _, r, _ in leg_results):
            return "l", " | ".join(details)
        if all(r == "p" for _, r, _ in leg_results):
            return "p", " | ".join(details)
        if any(r == "p" for _, r, _ in leg_results):
            # At least one push, no losses — treat as push
            return "p", " | ".join(details)
        return "w", " | ".join(details)

    # ── Single bet ────────────────────────────────────────────────────────────
    return grade_single(bet, final_games, ks_cache)


def grade_single(bet, final_games, ks_cache):
    """Determine result for a single-leg bet."""
    parsed = parse_bet_description(bet["description"])
    if not parsed:
        return None, "unrecognised bet type"

    # Parse "AWAY @ HOME" from bet["game"]
    game_str = bet.get("game", "")
    parts = game_str.split(" @ ")
    bet_away = parts[0].strip() if len(parts) == 2 else None
    bet_home = parts[1].strip() if len(parts) == 2 else None

    # Find matching completed game
    matched = None
    for g in final_games:
        if bet_away and bet_home:
            if g["away_team"] == bet_away and g["home_team"] == bet_home:
                matched = g
                break
        else:
            # Fallback: match by team in ML bet
            if parsed["type"] == "ML" and parsed["team"] in (g["home_team"], g["away_team"]):
                matched = g
                break

    if not matched:
        return None, f"game {game_str} not final yet"

    # ── Moneyline ─────────────────────────────────────────────────────────────
    if parsed["type"] == "ML":
        team = parsed["team"]
        w = matched["winner"]
        score = f"{matched['away_team']} {matched['away_score']}–{matched['home_score']} {matched['home_team']}"
        if w is None:
            return "p", f"tie  {score}"
        elif w == team:
            return "w", f"won  {score}"
        else:
            return "l", f"lost  {score}"

    # ── Total runs ────────────────────────────────────────────────────────────
    if parsed["type"] == "TOTAL":
        total  = matched["home_score"] + matched["away_score"]
        line   = parsed["line"]
        direction = parsed["direction"]
        score  = f"{matched['away_team']} {matched['away_score']}–{matched['home_score']} {matched['home_team']}"
        detail = f"total {total} runs  (line {direction} {line})  {score}"
        if total == line:
            return "p", detail
        hit = total > line if direction == "OVER" else total < line
        return ("w" if hit else "l"), detail

    # ── NRFI / YRFI ──────────────────────────────────────────────────────────
    if parsed["type"] in ("NRFI", "YRFI"):
        first_runs = matched["first_inn_away"] + matched["first_inn_home"]
        detail = (f"1st inn: {matched['away_team']} {matched['first_inn_away']} "
                  f"/ {matched['home_team']} {matched['first_inn_home']} "
                  f"({first_runs} run(s))")
        if parsed["type"] == "NRFI":
            return ("w" if first_runs == 0 else "l"), detail
        else:  # YRFI
            return ("w" if first_runs > 0 else "l"), detail

    # ── Strikeout prop ────────────────────────────────────────────────────────
    pk = matched["game_pk"]
    if pk not in ks_cache:
        ks_cache[pk] = get_pitcher_ks_from_boxscore(pk)

    ks_map  = ks_cache[pk]
    actual, matched_name = match_pitcher_ks(parsed["pitcher"], ks_map)

    if actual is None:
        return None, f"pitcher '{parsed['pitcher']}' not found in boxscore"

    line      = parsed["line"]
    direction = parsed["direction"]
    detail    = f"{matched_name} had {actual} K  (line {direction} {line})"

    if actual == line:
        return "p", detail
    hit = actual > line if direction == "OVER" else actual < line
    return ("w" if hit else "l"), detail
