"""
Backtest harness — measures model *projection accuracy* against historical outcomes.

For each completed game in a date range, project the game and compare the model's
probabilities / point-projections to what actually happened.  Reports:

  - Brier scores (binary markets: ML, NRFI, totals O/U at a reference line)
  - MAE / bias (continuous projections: total runs, team runs, SP strikeouts)

This needs NO betting odds — it grades the model's own numbers — so it runs entirely
on the free MLB Stats API and is exactly what's needed to validate calibration and
game-model changes.

────────────────────────────────────────────────────────────────────────────────
v1 CAVEAT — LOOKAHEAD BIAS: this uses current season-TOTAL stats, which include
games played *after* the backtested date.  Absolute Brier/MAE numbers are therefore
optimistic.  What IS valid is RELATIVE comparison: run the backtest before and after
a model change (both on the same biased data) — if the metric improves, the change
helped.  A point-in-time (as-of-date) stats mode is the rigorous future upgrade.
────────────────────────────────────────────────────────────────────────────────

Usage:
  python main.py backtest --start 2026-04-01 --end 2026-05-31
"""
import datetime as _dt

from data.mlb_api import get_schedule, get_final_scores, get_pitcher_ks_from_boxscore
from models.grading import match_pitcher_ks

REFERENCE_TOTAL_LINE = 8.5   # standard line for grading total O/U calibration


def _daterange(start, end):
    d = start
    while d <= end:
        yield d.isoformat()
        d += _dt.timedelta(days=1)


class _Metric:
    """Accumulates squared errors (Brier) or absolute errors (MAE) plus outcomes."""
    def __init__(self):
        self.sq_err   = []   # for Brier
        self.abs_err  = []   # for MAE
        self.signed   = []   # for bias (signed proj - actual)
        self.outcomes = []   # binary actuals, for base-rate context

    def add_brier(self, prob, actual):
        self.sq_err.append((prob - actual) ** 2)
        self.outcomes.append(actual)

    def add_point(self, proj, actual):
        self.abs_err.append(abs(proj - actual))
        self.signed.append(proj - actual)

    @property
    def n(self):
        return max(len(self.sq_err), len(self.abs_err))

    def brier(self):
        return sum(self.sq_err) / len(self.sq_err) if self.sq_err else None

    def base_rate(self):
        return sum(self.outcomes) / len(self.outcomes) if self.outcomes else None

    def baseline_brier(self):
        """Brier of always predicting the observed base rate (climatology)."""
        p = self.base_rate()
        return p * (1 - p) if p is not None else None

    def mae(self):
        return sum(self.abs_err) / len(self.abs_err) if self.abs_err else None

    def bias(self):
        return sum(self.signed) / len(self.signed) if self.signed else None


def run_backtest(start_date, end_date):
    from models.game_model import project_game, total_over_prob

    start = _dt.date.fromisoformat(start_date)
    end   = _dt.date.fromisoformat(end_date)

    ml        = _Metric()   # home_win_prob vs home won (ties skipped)
    nrfi      = _Metric()   # nrfi_prob vs first-inning scoreless
    total_ou  = _Metric()   # P(over ref line) vs actual (pushes skipped)
    total_pt  = _Metric()   # projected total runs vs actual
    team_pt   = _Metric()   # projected team runs vs actual (both sides)
    ks_pt     = _Metric()   # projected SP Ks vs actual (both starters)

    dates_with_games = 0
    games_scored     = 0

    for date_str in _daterange(start, end):
        finals = get_final_scores(date_str)
        if not finals:
            continue
        finals_by_pk = {g["game_pk"]: g for g in finals}

        proj_games = get_schedule(date_str, include_started=True)
        if not proj_games:
            continue
        dates_with_games += 1

        for game in proj_games:
            outcome = finals_by_pk.get(game["game_pk"])
            if not outcome:
                continue   # not final / no linescore
            try:
                proj = project_game(game)
            except Exception as e:
                print(f"  [skip] {date_str} {game.get('away_team')}@{game.get('home_team')}: {e}")
                continue
            games_scored += 1

            # ── Moneyline (skip ties, which have winner=None) ─────────────────
            if outcome["winner"] is not None:
                home_won = 1.0 if outcome["winner"] == outcome["home_team"] else 0.0
                ml.add_brier(proj["home_win_prob"], home_won)

            # ── NRFI ──────────────────────────────────────────────────────────
            first_runs  = outcome["first_inn_away"] + outcome["first_inn_home"]
            nrfi_actual = 1.0 if first_runs == 0 else 0.0
            nrfi.add_brier(proj["nrfi_prob"], nrfi_actual)

            # ── Totals ────────────────────────────────────────────────────────
            actual_total = outcome["home_score"] + outcome["away_score"]
            if actual_total != REFERENCE_TOTAL_LINE:
                p_over = total_over_prob(proj["total_runs"], REFERENCE_TOTAL_LINE)
                total_ou.add_brier(p_over, 1.0 if actual_total > REFERENCE_TOTAL_LINE else 0.0)
            total_pt.add_point(proj["total_runs"], actual_total)

            # ── Team runs (point projection, both sides) ──────────────────────
            team_pt.add_point(proj["home_runs"], outcome["home_score"])
            team_pt.add_point(proj["away_runs"], outcome["away_score"])

            # ── SP strikeouts (point projection, both starters) ───────────────
            ks_map = get_pitcher_ks_from_boxscore(game["game_pk"])
            for side in ("home", "away"):
                pname = game.get(f"{side}_pitcher")
                kd    = proj.get(f"{side}_sp_ks", {})
                if not pname or not kd:
                    continue
                proj_k = kd.get("projection")
                actual_k, _ = match_pitcher_ks(pname, ks_map)
                if proj_k is not None and actual_k is not None:
                    ks_pt.add_point(proj_k, actual_k)

    _print_report(start_date, end_date, dates_with_games, games_scored,
                  ml, nrfi, total_ou, total_pt, team_pt, ks_pt)


def _print_report(start_date, end_date, dates, games,
                  ml, nrfi, total_ou, total_pt, team_pt, ks_pt):
    W = 78
    print()
    print("=" * W)
    print(f"  BACKTEST  —  {start_date} → {end_date}")
    print("=" * W)
    print(f"  Dates with games: {dates}   Games projected & scored: {games}")
    print("  NOTE: v1 uses season-total stats (lookahead bias) — valid for")
    print("        RELATIVE before/after comparison, not absolute accuracy.")
    print()

    # ── Binary markets (Brier) ────────────────────────────────────────────────
    print("  BINARY MARKETS  (Brier: lower is better; vs base-rate baseline)")
    print("  " + "-" * (W - 4))
    print("  {:<22} {:>6} {:>10} {:>10} {:>10}".format(
        "Market", "N", "Brier", "Baseline", "BaseRate"))
    for label, m in (("moneyline (home)", ml),
                     ("NRFI", nrfi),
                     (f"total O/U @ {REFERENCE_TOTAL_LINE}", total_ou)):
        b  = m.brier()
        bl = m.baseline_brier()
        br = m.base_rate()
        if b is None:
            print("  {:<22} {:>6} {:>10}".format(label, m.n, "—"))
        else:
            flag = "  ✓ beats base" if bl is not None and b < bl else "  ✗ worse"
            print("  {:<22} {:>6} {:>10.4f} {:>10.4f} {:>9.1f}%{}".format(
                label, len(m.sq_err), b, bl, br * 100, flag))
    print()

    # ── Continuous projections (MAE / bias) ───────────────────────────────────
    print("  POINT PROJECTIONS  (MAE: lower is better; Bias: proj − actual)")
    print("  " + "-" * (W - 4))
    print("  {:<22} {:>6} {:>10} {:>10}".format("Projection", "N", "MAE", "Bias"))
    for label, m in (("total runs", total_pt),
                     ("team runs", team_pt),
                     ("SP strikeouts", ks_pt)):
        mae = m.mae()
        if mae is None:
            print("  {:<22} {:>6} {:>10}".format(label, m.n, "—"))
        else:
            print("  {:<22} {:>6} {:>10.3f} {:>+10.3f}".format(
                label, len(m.abs_err), mae, m.bias()))
    print("=" * W)
    print()
