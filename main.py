#!/usr/bin/env python3
"""
Baseball Betting Model — main runner.

Usage:
  python main.py picks             # Generate today's picks (display only)
  python main.py picks --save      # Generate and save to bankroll DB
  python main.py picks --date 2025-05-13
  python main.py settle                        # Interactively settle today's pending bets
  python main.py settle --auto                 # Auto-settle via MLB API scores
  python main.py settle --date 2025-05-12
  python main.py settle --results 1=w 2=l 3=p  # Non-interactive: w=win l=loss p=push
  python main.py calibrate         # Re-run calibration and show report
  python main.py report            # P&L report (last 30 days)
  python main.py report --days 7
  python main.py pending           # List pending/unsettled bets

Environment variables:
  ODDS_API_KEY   — Free key from https://the-odds-api.com
"""
import argparse
import sys
from datetime import date

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _today():
    return date.today().isoformat()


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_picks(date_str, save):
    from data.mlb_api import get_schedule
    from models.betting_engine import find_daily_bets
    from bankroll.tracker import init_db, get_bankroll, save_picks
    from reports.display import print_picks
    from data.odds_api import get_requests_remaining

    init_db()

    print(f"\n  Fetching schedule for {date_str}…")
    games = get_schedule(date_str)

    if not games:
        print(f"  No games found for {date_str}.")
        return

    print(f"  {len(games)} games found — running model…\n")
    picks = find_daily_bets(games)

    bankroll = get_bankroll()
    print_picks(picks, bankroll, date_str)

    rem = get_requests_remaining()
    if rem is not None:
        print(f"  [Odds API] Requests remaining this month: {rem}\n")

    if save and picks:
        staked_ids, tracking_ids = save_picks(picks, date_str)
        print(f"  Saved {len(staked_ids)} staked picks (IDs: {staked_ids}).")
        if tracking_ids:
            print(f"  Saved {len(tracking_ids)} tracking picks (IDs: {tracking_ids}) — no stake, calibration only.")
        print("  Run  python main.py settle  after games to record results.\n")
    elif not save:
        print("  Tip: re-run with --save to persist picks for P&L tracking.\n")


_RESULT_MAP = {"w": "win", "l": "loss", "p": "push"}


# ── Auto-settle helpers ───────────────────────────────────────────────────────
# Bet-grading logic lives in models/grading.py.


def _apply_result(b, raw, settle_bet):
    """Apply a single w/l/p result to bet b. Returns True if settled."""
    result = _RESULT_MAP.get(raw)
    if result is None:
        return False
    settle_bet(b["id"], result)
    if b.get("tracking_only"):
        symbol = {"win": "✓", "loss": "✗", "push": "~"}[result]
        print(f"       {symbol} {result.upper()}  [tracking]")
    elif result == "win":
        print(f"       ✓ WIN  +${b['potential_win']:.2f}")
    elif result == "loss":
        print(f"       ✗ LOSS  -${b['stake']:.2f}")
    else:
        print("       ~ PUSH  $0")
    return True


def cmd_settle(date_str, results=None, auto=False):
    """
    results : dict {bet_id: 'w'|'l'|'p'} for non-interactive mode.
    auto    : fetch scores from MLB API and settle automatically.
    """
    from bankroll.tracker import get_pending_bets, settle_bet

    pending = get_pending_bets(date_str, latest_batch_only=auto, include_tracking=True)
    if not pending:
        print(f"\n  No pending bets for {date_str}.")
        return

    print(f"\n  Settling bets for {date_str}")
    print("  " + "─" * 52)

    # ── Auto mode: fetch scores and determine results ─────────────────────────
    if auto:
        from data.mlb_api import get_final_scores
        from models.grading import grade_bet
        print("  Fetching final scores from MLB Stats API…")
        final_games = get_final_scores(date_str)
        if not final_games:
            print("  No completed games found yet. Try again after games finish.\n")
            return
        print(f"  {len(final_games)} completed game(s) found.\n")

        ks_cache = {}
        settled = skipped = 0
        for b in pending:
            raw, detail = grade_bet(b, final_games, ks_cache)
            print(f"  #{b['id']}  {b['description']}  ({b['game']})")
            if raw is None:
                print(f"       ⚠ Could not determine — {detail}. Skipped.")
                skipped += 1
            else:
                _apply_result(b, raw, settle_bet)
                print(f"       → {detail}")
                settled += 1

        print(f"\n  Auto-settled {settled} bet(s), skipped {skipped}.")
        if skipped:
            print(f"  Settle skipped bets manually:  python main.py settle --results ID=w/l/p")
        print(f"  Run  python main.py report  to see updated P&L.\n")
        _maybe_recalibrate()
        return

    # ── Non-interactive mode ──────────────────────────────────────────────────
    if results is not None:
        unknown = set(results) - {b["id"] for b in pending}
        if unknown:
            print(f"  Warning: IDs not found in pending bets: {sorted(unknown)}")

        for b in pending:
            print(f"\n  #{b['id']}  {b['description']}")
            print(f"       {b['game']}")
            print(f"       Odds +{b['american_odds']} | {b['units']}u | "
                  f"${b['stake']:.2f} to win ${b['potential_win']:.2f}")
            raw = results.get(b["id"])
            if raw is None:
                print("       Skipped.")
            elif not _apply_result(b, raw, settle_bet):
                print(f"       Invalid result '{raw}' — use w / l / p. Skipped.")

    # ── Interactive mode ──────────────────────────────────────────────────────
    else:
        for b in pending:
            print(f"\n  #{b['id']}  {b['description']}")
            print(f"       {b['game']}")
            print(f"       Odds +{b['american_odds']} | {b['units']}u | "
                  f"${b['stake']:.2f} to win ${b['potential_win']:.2f}")
            while True:
                raw = input("       Result  [w=win / l=loss / p=push / s=skip]: ").strip().lower()
                if raw == "s":
                    print("       Skipped.")
                    break
                elif _apply_result(b, raw, settle_bet):
                    break
                else:
                    print("       Invalid — use w / l / p / s")

    print(f"\n  Done. Run  python main.py report  to see updated P&L.\n")
    _maybe_recalibrate()


def cmd_calibrate(silent=False):
    """
    Pull all settled bets, recompute calibration factors, persist to
    models/cal_weights.json, and print a report (unless silent=True).
    Returns the weights dict.
    """
    from bankroll.tracker import get_settled_bets_for_calibration
    from models.calibration import run_calibration, print_calibration_report

    bets    = get_settled_bets_for_calibration()
    weights = run_calibration(bets)
    if not silent:
        print_calibration_report(weights)
    return weights


def cmd_report(days):
    from bankroll.tracker import get_performance, get_performance_by_units, get_all_bets
    from reports.display import print_report

    perf       = get_performance(days)
    by_units   = get_performance_by_units(days)
    bets       = get_all_bets(50)
    print_report(perf, by_units, bets, days)


def _maybe_recalibrate():
    """
    Silently re-run calibration after every settle so cal_weights.json stays
    current.  Only prints a one-liner; full report available via `calibrate`.
    """
    from bankroll.tracker import get_settled_bets_for_calibration
    from models.calibration import run_calibration
    bets = get_settled_bets_for_calibration()
    if not bets:
        return
    # Always recompute so per-type calibration engages at its own threshold
    # (Platt warms up per bet type, not on a total-across-types count).
    weights = run_calibration(bets)
    platt = sum(1 for d in weights.get("by_type", {}).values() if d.get("method") == "platt")
    print(f"  [Calibration] Updated — {len(bets)} settled bets since epoch, "
          f"{platt} type(s) calibrating.  "
          f"Run  python main.py calibrate  for full report.\n")


def cmd_pending(date_str):
    from bankroll.tracker import get_pending_bets

    pending = get_pending_bets(date_str)
    if not pending:
        print(f"\n  No pending bets for {date_str}.\n")
        return

    print(f"\n  Pending bets — {date_str}")
    print("  " + "─" * 52)
    for b in pending:
        print(f"  #{b['id']}  {b['description']}")
        print(f"       {b['game']}  |  +{b['american_odds']}  |  "
              f"{b['units']}u  |  ${b['stake']:.2f} → ${b['potential_win']:.2f}")
    print()


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Baseball Betting Model",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd")

    p_picks = sub.add_parser("picks", help="Generate today's betting picks")
    p_picks.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    p_picks.add_argument("--save", action="store_true", help="Save picks to DB")

    p_settle = sub.add_parser("settle", help="Settle pending bets")
    p_settle.add_argument("--date", default=None)
    p_settle.add_argument(
        "--results", nargs="+", metavar="ID=RESULT",
        help="Non-interactive: e.g. --results 1=w 2=l 3=p  (w=win l=loss p=push)",
    )
    p_settle.add_argument(
        "--auto", action="store_true",
        help="Fetch final scores from MLB API and settle automatically",
    )

    sub.add_parser("calibrate", help="Recompute calibration factors and show report")

    p_report = sub.add_parser("report", help="P&L performance report")
    p_report.add_argument("--days", type=int, default=30)

    p_pending = sub.add_parser("pending", help="List unsettled bets")
    p_pending.add_argument("--date", default=None)

    p_backtest = sub.add_parser("backtest", help="Backtest projection accuracy over a date range")
    p_backtest.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_backtest.add_argument("--end", required=True, help="YYYY-MM-DD")

    args = parser.parse_args()

    if args.cmd == "calibrate":
        cmd_calibrate()
    elif args.cmd == "picks":
        cmd_picks(args.date or _today(), args.save)
    elif args.cmd == "settle":
        results = None
        if args.results:
            results = {}
            for token in args.results:
                try:
                    id_str, raw = token.split("=", 1)
                    results[int(id_str)] = raw.strip().lower()
                except ValueError:
                    print(f"  Error: bad --results token '{token}' — expected format ID=RESULT (e.g. 1=w)")
                    sys.exit(1)
        cmd_settle(args.date or _today(), results=results, auto=args.auto)
    elif args.cmd == "report":
        cmd_report(args.days)
    elif args.cmd == "pending":
        cmd_pending(args.date or _today())
    elif args.cmd == "backtest":
        from backtest import run_backtest
        run_backtest(args.start, args.end)
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
