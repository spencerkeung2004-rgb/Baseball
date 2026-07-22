"""Terminal display helpers."""
from datetime import date

try:
    from colorama import Fore, Style, init as _init
    _init(autoreset=True)
    _COLOR = True
except ImportError:
    _COLOR = False

try:
    from tabulate import tabulate as _tabulate
    _TABLE = True
except ImportError:
    _TABLE = False

from config import STARTING_BANKROLL, UNIT_SIZE


def _c(color, text):
    return (color + str(text) + Style.RESET_ALL) if _COLOR else str(text)


def print_picks(picks, bankroll, date_str=None):
    date_str = date_str or date.today().isoformat()
    W = 90
    sep = "=" * W
    print()
    print(_c(Fore.CYAN if _COLOR else "", sep))
    print(_c(Fore.CYAN if _COLOR else "", "  BASEBALL BETTING MODEL  -  " + date_str))
    print(_c(Fore.CYAN if _COLOR else "", sep))
    br_color = Fore.GREEN if bankroll >= STARTING_BANKROLL else Fore.RED
    change = bankroll - STARTING_BANKROLL
    sign = "+" if change >= 0 else ""
    print("  Bankroll : " + _c(br_color, "$" + "{:,.2f}".format(bankroll))
          + "  (" + _c(br_color, sign + "$" + "{:,.2f}".format(change)) + " from start)")
    print("  Unit Size: $" + str(int(UNIT_SIZE)) + "  |  Starting: $" + str(int(STARTING_BANKROLL)))
    print(_c(Fore.CYAN if _COLOR else "", sep))
    staked   = [p for p in picks if not p.get("tracking_only")]
    tracking = [p for p in picks if p.get("tracking_only")]

    if not staked:
        print(_c(Fore.YELLOW if _COLOR else "", "  No qualifying bets found today."))
        print("  Check your ODDS_API_KEY in .env and try again.")
        return

    def _print_pick(i, p, label_prefix=""):
        odds  = p["american_odds"]
        edge  = p["edge"]
        o_col = Fore.GREEN if odds >= 150 else Fore.YELLOW
        e_col = Fore.GREEN if edge >= 0.15 else Fore.YELLOW
        odds_s = _c(o_col, "+{:d}".format(odds) if odds >= 0 else "{:d}".format(odds))
        edge_s = _c(e_col, "{:.1f}%".format(edge * 100))

        is_parlay   = p.get("type") == "parlay"
        leg_details = p.get("leg_details", [])

        if is_parlay:
            desc = "{}-Leg Parlay".format(len(leg_details) or len(p.get("legs", [])))
        else:
            desc = p["description"]

        if p.get("tracking_only"):
            print("  {}{:<2}  {:<28}  {}  edge {}  [tracking]".format(
                label_prefix, i, desc, odds_s, edge_s))
        else:
            win_s = _c(Fore.GREEN if _COLOR else "", "${:.2f}".format(p["potential_win"]))
            print("  {}{:<2}  {:<28}  {}  edge {}  {:.1f}u  ${:.2f} → {}".format(
                label_prefix, i, desc, odds_s, edge_s,
                p["units"], p["stake"], win_s))

        if is_parlay and leg_details:
            for leg in leg_details:
                l_odds = leg["odds"]
                l_sign = "+" if l_odds >= 0 else ""
                print("       {:<26}  {:>8}  {}{}".format(
                    leg["description"], leg["game"], l_sign, l_odds))
        elif not is_parlay:
            print("       {}".format(p["game"]))

        reasoning = p.get("reasoning", "")
        if not is_parlay and reasoning:
            print("       {}".format(_c(Fore.CYAN if _COLOR else "", reasoning)))
        print()

    # ── Staked picks ──────────────────────────────────────────────────────────
    print()
    for i, p in enumerate(staked, 1):
        _print_pick(i, p)

    total_units = sum(p["units"] for p in staked)
    total_stake = sum(p["stake"] for p in staked)
    total_win   = sum(p["potential_win"] for p in staked)
    print("  Total at risk: {:.1f}u / ${:.2f}  |  Max return: ${:.2f}".format(
          total_units, total_stake, total_win))
    print(_c(Fore.CYAN if _COLOR else "", sep))

    # ── Tracking picks (calibration only) ────────────────────────────────────
    if tracking:
        print()
        print(_c(Fore.YELLOW if _COLOR else "",
                 "  CALIBRATION TRACKING  (no stake — outcomes update the model)"))
        print("  " + "-" * 52)
        for i, p in enumerate(tracking, 1):
            _print_pick(i, p)
    print()


def print_report(perf, by_units, bets, days):
    W = 90
    sep = "=" * W
    print()
    print(_c(Fore.CYAN if _COLOR else "", sep))
    print(_c(Fore.CYAN if _COLOR else "", "  PERFORMANCE REPORT  -  Last " + str(days) + " Days"))
    print(_c(Fore.CYAN if _COLOR else "", sep))
    br     = perf["current_bankroll"]
    chg    = br - STARTING_BANKROLL
    sign   = "+" if chg >= 0 else ""
    br_col  = Fore.GREEN if chg >= 0 else Fore.RED
    roi_col = Fore.GREEN if perf["roi"] >= 0 else Fore.RED
    print("  Bankroll : " + _c(br_col, "$" + "{:,.2f}".format(br))
          + " (" + _c(br_col, sign + "$" + "{:,.2f}".format(chg)) + ")")
    print("  Record   : {}W - {}L - {}P  ({:.1f}% win rate)".format(
          perf["wins"], perf["losses"], perf["pushes"], perf["win_rate"]))
    print("  ROI      : " + _c(roi_col, "{:+.1f}%".format(perf["roi"])))
    print("  P&L      : " + _c(roi_col, "${:+.2f}".format(perf["pnl"])) + "  (${:.2f} staked)".format(perf["staked"]))
    print("  Avg Edge : {:+.1f}%".format(perf["avg_edge"]))
    print("  Total    : {} bets".format(perf["total_bets"]))
    if by_units:
        print()
        print("  BY UNIT SIZE")
        if _TABLE:
            unit_rows = []
            for u in by_units:
                wl    = "{}W - {}L".format(u["wins"], u["losses"])
                if u["pushes"]:
                    wl += " - {}P".format(u["pushes"])
                wr    = "{:.1f}%".format(u["win_rate"]) if (u["wins"] + u["losses"]) else "—"
                pnl_c = Fore.GREEN if u["pnl"] >= 0 else Fore.RED if _COLOR else ""
                unit_rows.append([
                    "{:.1f}u".format(u["units"]),
                    u["total"],
                    wl,
                    wr,
                    "${:.2f}".format(u["staked"]),
                    _c(pnl_c, "${:+.2f}".format(u["pnl"])),
                    _c(pnl_c, "{:+.1f}%".format(u["roi"])),
                ])
            print(_tabulate(unit_rows,
                            headers=["Units", "Bets", "Record", "Win%", "Staked", "P&L", "ROI"],
                            tablefmt="simple"))
        else:
            for u in by_units:
                wl = "{}W-{}L".format(u["wins"], u["losses"])
                print("  {:.1f}u  {}  P&L: ${:+.2f}".format(u["units"], wl, u["pnl"]))
    if bets and _TABLE:
        print()
        print("  RECENT BETS")
        rows = []
        for b in bets[:15]:
            rc = {"win": Fore.GREEN, "loss": Fore.RED, "push": Fore.YELLOW,
                  "pending": Fore.WHITE}.get(b["result"], Fore.WHITE) if _COLOR else ""
            pc = (Fore.GREEN if (b["pnl"] or 0) > 0
                  else Fore.RED if (b["pnl"] or 0) < 0 else Fore.WHITE) if _COLOR else ""
            pv = "${:+.2f}".format(b["pnl"]) if b["pnl"] else "-"
            desc = (b["description"][:35] + "...") if len(b["description"]) > 36 else b["description"]
            os2  = "+" + str(b["odds"]) if b["odds"] >= 0 else str(b["odds"])
            rows.append([b["date"], desc, os2, str(b["units"]) + "u",
                         "${:.2f}".format(b["stake"]),
                         _c(rc, b["result"].upper()), _c(pc, pv)])
        print(_tabulate(rows,
                        headers=["Date", "Bet", "Odds", "Units", "Stake", "Result", "P&L"],
                        tablefmt="simple"))
    elif bets:
        for b in bets[:10]:
            pv = "${:+.2f}".format(b["pnl"]) if b["pnl"] else "-"
            print("  {}  {:<35}  {:<7}  {}".format(
                  b["date"], b["description"][:35], b["result"].upper(), pv))
    print(_c(Fore.CYAN if _COLOR else "", sep))
    print()