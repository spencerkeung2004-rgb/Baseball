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
    if not picks:
        print(_c(Fore.YELLOW if _COLOR else "", "  No qualifying bets found today."))
        print("  Check your ODDS_API_KEY in .env and try again.")
        return
    print()
    rows = []
    for i, p in enumerate(picks, 1):
        odds    = p["american_odds"]
        edge    = p["edge"]
        model_p = p["our_prob"] * 100
        impl_p  = p["implied_prob"] * 100
        o_col   = Fore.GREEN if odds >= 150 else Fore.YELLOW
        e_col   = Fore.GREEN if edge >= 0.15 else Fore.YELLOW
        rows.append([
            str(i),
            p["game"],
            p["description"],
            _c(o_col, "+" + str(odds)),
            _c(e_col, "{:.1f}%".format(edge * 100)),
            "{:.1f}%".format(model_p),
            "{:.1f}%".format(impl_p),
            str(p["units"]) + "u",
            "${:.2f}".format(p["stake"]),
            _c(Fore.GREEN if _COLOR else "", "${:.2f}".format(p["potential_win"])),
        ])
    hdrs = ["#", "Game", "Bet", "Odds", "Edge", "Model%", "Implied%", "Size", "Stake", "To Win"]
    if _TABLE:
        print(_tabulate(rows, headers=hdrs, tablefmt="simple"))
    else:
        print("  ".join(hdrs))
        for r in rows:
            print("  ".join(str(x) for x in r))
    print()
    for i, p in enumerate(picks, 1):
        print("  #" + str(i) + " " + _c(Fore.CYAN if _COLOR else "", p.get("reasoning", "")))
    print()
    total_stake = sum(p["stake"] for p in picks)
    total_win   = sum(p["potential_win"] for p in picks)
    print("  Total at risk: ${:.2f}  |  Max return: ${:.2f}".format(total_stake, total_win))
    print(_c(Fore.CYAN if _COLOR else "", sep))
    print()


def print_report(perf, bets, days):
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