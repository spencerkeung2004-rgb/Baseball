#!/usr/bin/env python3
"""
Baseball Betting Model — main runner.

Usage:
  python main.py picks             # Generate today's picks (display only)
  python main.py picks --save      # Generate and save to bankroll DB
  python main.py picks --date 2025-05-13
  python main.py settle            # Interactively settle today's pending bets
  python main.py settle --date 2025-05-12
  python main.py report            # P&L report (last 30 days)
  python main.py report --days 7
  python main.py pending           # List pending/unsettled bets

Environment variables:
  ODDS_API_KEY   — Free key from https://the-odds-api.com
"""
import argparse
import sys
from datetime import date


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
        ids = save_picks(picks, date_str)
        print(f"  Saved {len(ids)} picks (IDs: {ids}).")
        print("  Run  python main.py settle  after games to record results.\n")
    elif not save:
        print("  Tip: re-run with --save to persist picks for P&L tracking.\n")


def cmd_settle(date_str):
    from bankroll.tracker import get_pending_bets, settle_bet

    pending = get_pending_bets(date_str)
    if not pending:
        print(f"\n  No pending bets for {date_str}.")
        return

    print(f"\n  Settling bets for {date_str}")
    print("  " + "─" * 52)

    for b in pending:
        print(f"\n  #{b['id']}  {b['description']}")
        print(f"       {b['game']}")
        print(f"       Odds +{b['american_odds']} | {b['units']}u | "
              f"${b['stake']:.2f} to win ${b['potential_win']:.2f}")
        while True:
            raw = input("       Result  [w=win / l=loss / p=push / s=skip]: ").strip().lower()
            if raw == "w":
                settle_bet(b["id"], "win")
                print(f"       ✓ WIN  +${b['potential_win']:.2f}")
                break
            elif raw == "l":
                settle_bet(b["id"], "loss")
                print(f"       ✗ LOSS  -${b['stake']:.2f}")
                break
            elif raw == "p":
                settle_bet(b["id"], "push")
                print("       ~ PUSH  $0")
                break
            elif raw == "s":
                print("       Skipped.")
                break
            else:
                print("       Invalid — use w / l / p / s")

    print(f"\n  Done. Run  python main.py report  to see updated P&L.\n")


def cmd_report(days):
    from bankroll.tracker import get_performance, get_all_bets
    from reports.display import print_report

    perf = get_performance(days)
    bets = get_all_bets(50)
    print_report(perf, bets, days)


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

    p_report = sub.add_parser("report", help="P&L performance report")
    p_report.add_argument("--days", type=int, default=30)

    p_pending = sub.add_parser("pending", help="List unsettled bets")
    p_pending.add_argument("--date", default=None)

    args = parser.parse_args()

    if args.cmd == "picks":
        cmd_picks(args.date or _today(), args.save)
    elif args.cmd == "settle":
        cmd_settle(args.date or _today())
    elif args.cmd == "report":
        cmd_report(args.days)
    elif args.cmd == "pending":
        cmd_pending(args.date or _today())
    else:
        parser.print_help()
        sys.exit(0)


if __name__ == "__main__":
    main()
