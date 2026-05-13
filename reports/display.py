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
    return f"{color}{text}{Style.RESET_ALL}" if _COLOR else text


def print_picks(picks, bankroll, date_str=None):
    date_str = date_str or date.today().isoformat()
    W = 66

    print()
    print(_c(Fore.CYAN if _COLOR else "", "=" * W))
    print(_c(Fore.CYAN if _COLOR else "", f"  BASEBALL BETTING MODEL  —  {date_str}"))
    print(_c(Fore.CYAN if _COLOR else "", "=" * W))
    br_color = Fore.GREEN if bankroll >= STARTING_BANKROLL else Fore.RED
    change = bankroll - STARTING_BANKROLL
    sign = "+" if change >= 0 else ""
    print(f"  Bankroll : {_c(br_color, f'${bankroll:,.2f}')}  "
          f"({_c(br_color, f'{sign}${change:,.2f}')} from start)")
    print(f"  Unit Size: ${UNIT_SIZE:.0f}  |  Starting: ${STARTING_BANKROLL:,.0f}")
    print(_c(Fore.CYAN if _COLOR else "", "=" * W))

    if not picks:
        print(_c(Fore.YELLOW if _COLOR else "", "\n  No qualifying bets found today."))
        print("  Check your ODDS_API_KEY in .env and try again.\n")
        return

    print(f"\n  TODAY'S PICKS  ({len(picks)} bets)\n")

    for i, p in enumerate(picks, 1):
        odds     = p["american_odds"]
        edge     = p["edge"]
        odds_col = Fore.GREEN if odds >= 150 else Fore.YELLOW
        edge_col = Fore.GREEN if edge >= 0.06 else Fore.YELLOW

        print(f"  {'─'*62}")
        print(f"  BET #{i}  [{p['type'].upper().replace('_',' ')}]  —  {p['game']}")
        print(f"  {_c(Fore.WHITE if _COLOR else '', p['description'])}")
        if p.get("legs"):
            for leg in p["legs"]:
                print(f"    └ {leg}")
        print(
            f"  Odds: {_c(odds_col, f'+{odds}')}  |  "
            f"Edge: {_c(edge_col, f\"{edge*100:.1f}%\")}  |  "
            f"Model: {p['our_prob']*100:.1f}%  |  "
            f"Implied: {p['implied_prob']*100:.1f}%"
        )
        print(
            f"  Size: {p['units']}u  |  "
            f"Stake: ${p['stake']:.2f}  |  "
            f"To Win: {_c(Fore.GREEN if _COLOR else '', f\"${p['potential_win']:.2f}\")}"
        )
        print(f"  {_c(Fore.CYAN if _COLOR else '', p.get('reasoning', ''))}")

    print(f"\n  {'─'*62}")
    total_stake = sum(p["stake"] for p in picks)
    total_win   = sum(p["potential_win"] for p in picks)
    print(f"  Total at risk: ${total_stake:.2f}  |  Max return: ${total_win:.2f}")
    print(_c(Fore.CYAN if _COLOR else "", "=" * W))
    print()


def print_report(perf, bets, days):
    W = 66
    print()
    print(_c(Fore.CYAN if _COLOR else "", "=" * W))
    print(_c(Fore.CYAN if _COLOR else "", f"  PERFORMANCE REPORT  —  Last {days} Days"))
    print(_c(Fore.CYAN if _COLOR else "", "=" * W))

    br    = perf["current_bankroll"]
    chg   = br - STARTING_BANKROLL
    sign  = "+" if chg >= 0 else ""
    br_col = Fore.GREEN if chg >= 0 else Fore.RED
    roi_col = Fore.GREEN if perf["roi"] >= 0 else Fore.RED

    print(f"\n  Bankroll : {_c(br_col, f'${br:,.2f}')} ({_c(br_col, f'{sign}${chg:,.2f}')})")
    print(f"  Record   : {perf['wins']}W - {perf['losses']}L - {perf['pushes']}P  "
          f"({perf['win_rate']:.1f}% win rate)")
    print(f"  ROI      : {_c(roi_col, f\"{perf['roi']:+.1f}%\")}")
    print(f"  P&L      : {_c(roi_col, f\"${perf['pnl']:+.2f}\")}  (${perf['staked']:.2f} staked)")
    print(f"  Avg Edge : {perf['avg_edge']:+.1f}%")
    print(f"  Total    : {perf['total_bets']} bets")

    if bets and _TABLE:
        print(f"\n  RECENT BETS")
        rows = []
        for b in bets[:15]:
            res_col = {
                "win": Fore.GREEN, "loss": Fore.RED,
                "push": Fore.YELLOW, "pending": Fore.WHITE,
            }.get(b["result"], Fore.WHITE) if _COLOR else ""
            pnl_col = (Fore.GREEN if (b["pnl"] or 0) > 0
                       else Fore.RED if (b["pnl"] or 0) < 0
                       else Fore.WHITE) if _COLOR else ""
            pnl_str = f"${b['pnl']:+.2f}" if b["pnl"] else "—"
            rows.append([
                b["date"],
                (b["description"][:32] + "…") if len(b["description"]) > 33 else b["description"],
                f"+{b['odds']}" if b["odds"] >= 0 else str(b["odds"]),
                f"{b['units']}u",
                f"${b['stake']:.2f}",
                _c(res_col, b["result"].upper()),
                _c(pnl_col, pnl_str),
            ])
        print(_tabulate(rows,
                        headers=["Date", "Bet", "Odds", "Units", "Stake", "Result", "P&L"],
                        tablefmt="simple"))
    elif bets:
        for b in bets[:10]:
            print(f"  {b['date']}  {b['description'][:30]:<30}  "
                  f"{b['result'].upper():<7}  {f'${b[\"pnl\"]:+.2f}' if b['pnl'] else '—'}")

    print(_c(Fore.CYAN if _COLOR else "", "\n" + "=" * W))
    print()
