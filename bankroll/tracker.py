"""SQLite-backed bankroll tracker."""
import json
import sqlite3
from datetime import date
from pathlib import Path

from config import STARTING_BANKROLL

DB_PATH = Path(__file__).parent / "bankroll.db"


# ── Schema ────────────────────────────────────────────────────────────────────

def init_db():
    conn = _conn()
    c = conn.cursor()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS bets (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            date           TEXT    NOT NULL,
            type           TEXT    NOT NULL,
            description    TEXT    NOT NULL,
            game           TEXT,
            american_odds  INTEGER,
            units          REAL,
            stake          REAL,
            potential_win  REAL,
            result         TEXT    DEFAULT 'pending',
            pnl            REAL    DEFAULT 0,
            bankroll_before REAL,
            bankroll_after  REAL,
            legs           TEXT,
            reasoning      TEXT,
            our_prob       REAL,
            edge           REAL,
            created_at     TEXT    DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS snapshots (
            date        TEXT UNIQUE PRIMARY KEY,
            bankroll    REAL,
            daily_pnl   REAL,
            total_bets  INTEGER,
            wins        INTEGER,
            losses      INTEGER
        );
    """)
    conn.commit()
    conn.close()


# ── Read ──────────────────────────────────────────────────────────────────────

def get_bankroll():
    init_db()
    conn = _conn()
    row = conn.execute(
        "SELECT bankroll FROM snapshots ORDER BY date DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else STARTING_BANKROLL


def get_pending_bets(date_str=None):
    date_str = date_str or _today()
    init_db()
    conn = _conn()
    rows = conn.execute(
        """SELECT id, type, description, game, american_odds, units,
                  stake, potential_win, reasoning
           FROM bets WHERE date = ? AND result = 'pending'
           ORDER BY id""",
        (date_str,),
    ).fetchall()
    conn.close()
    return [_row_to_bet(r) for r in rows]


def get_all_bets(limit=50):
    init_db()
    conn = _conn()
    rows = conn.execute(
        """SELECT id, date, type, description, game, american_odds,
                  units, stake, potential_win, result, pnl
           FROM bets ORDER BY date DESC, id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {"id": r[0], "date": r[1], "type": r[2], "description": r[3],
         "game": r[4], "odds": r[5], "units": r[6], "stake": r[7],
         "potential_win": r[8], "result": r[9], "pnl": r[10]}
        for r in rows
    ]


def get_performance(days=30):
    init_db()
    conn = _conn()
    row = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN result='win'  THEN 1 ELSE 0 END),
                  SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),
                  SUM(CASE WHEN result='push' THEN 1 ELSE 0 END),
                  COALESCE(SUM(pnl), 0),
                  COALESCE(SUM(stake), 0),
                  COALESCE(AVG(edge), 0)
           FROM bets
           WHERE result != 'pending'
             AND date >= date('now', ?)""",
        (f"-{days} days",),
    ).fetchone()
    conn.close()

    total, wins, losses, pushes, pnl, staked, avg_edge = row
    total   = total   or 0
    wins    = wins    or 0
    losses  = losses  or 0
    pushes  = pushes  or 0
    pnl     = pnl     or 0.0
    staked  = staked  or 0.0
    avg_edge = avg_edge or 0.0

    return {
        "total_bets":       total,
        "wins":             wins,
        "losses":           losses,
        "pushes":           pushes,
        "pnl":              round(pnl, 2),
        "staked":           round(staked, 2),
        "roi":              round(pnl / staked * 100, 2) if staked else 0,
        "win_rate":         round(wins / (wins + losses) * 100, 2) if (wins + losses) else 0,
        "avg_edge":         round(avg_edge * 100, 2),
        "current_bankroll": round(get_bankroll(), 2),
    }


# ── Write ─────────────────────────────────────────────────────────────────────

def save_picks(picks, date_str=None):
    """Persist today's picks; returns list of inserted bet IDs."""
    date_str = date_str or _today()
    init_db()
    bankroll = get_bankroll()
    conn = _conn()
    ids = []
    for p in picks:
        c = conn.execute(
            """INSERT INTO bets
               (date, type, description, game, american_odds, units, stake,
                potential_win, result, bankroll_before, legs, reasoning, our_prob, edge)
               VALUES (?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""",
            (
                date_str, p["type"], p["description"], p.get("game", ""),
                p["american_odds"], p["units"], p["stake"], p["potential_win"],
                bankroll,
                json.dumps(p["legs"]) if p.get("legs") else None,
                p.get("reasoning", ""), p.get("our_prob", 0), p.get("edge", 0),
            ),
        )
        ids.append(c.lastrowid)
    conn.commit()
    conn.close()
    return ids


def settle_bet(bet_id, result):
    """result: 'win' | 'loss' | 'push'"""
    init_db()
    conn = _conn()
    row = conn.execute(
        "SELECT stake, potential_win, bankroll_before FROM bets WHERE id = ?", (bet_id,)
    ).fetchone()
    if not row:
        conn.close()
        return False

    stake, pot_win, br_before = row
    if result == "win":
        pnl = pot_win
    elif result == "loss":
        pnl = -stake
    else:
        pnl = 0.0

    conn.execute(
        "UPDATE bets SET result=?, pnl=?, bankroll_after=? WHERE id=?",
        (result, pnl, br_before + pnl, bet_id),
    )
    conn.commit()
    conn.close()
    _refresh_snapshot()
    return True


def _refresh_snapshot(date_str=None):
    date_str = date_str or _today()
    conn = _conn()
    row = conn.execute(
        """SELECT COUNT(*),
                  SUM(CASE WHEN result='win'  THEN 1 ELSE 0 END),
                  SUM(CASE WHEN result='loss' THEN 1 ELSE 0 END),
                  COALESCE(SUM(pnl), 0)
           FROM bets WHERE date=? AND result!='pending'""",
        (date_str,),
    ).fetchone()
    total, wins, losses, dpnl = row
    total  = total  or 0
    wins   = wins   or 0
    losses = losses or 0
    dpnl   = dpnl   or 0.0

    prev = conn.execute(
        "SELECT bankroll FROM snapshots WHERE date < ? ORDER BY date DESC LIMIT 1",
        (date_str,),
    ).fetchone()
    prev_br = prev[0] if prev else STARTING_BANKROLL

    conn.execute(
        """INSERT OR REPLACE INTO snapshots (date, bankroll, daily_pnl, total_bets, wins, losses)
           VALUES (?,?,?,?,?,?)""",
        (date_str, prev_br + dpnl, dpnl, total, wins, losses),
    )
    conn.commit()
    conn.close()


# ── Internals ─────────────────────────────────────────────────────────────────

def _conn():
    return sqlite3.connect(DB_PATH)


def _today():
    return date.today().isoformat()


def _row_to_bet(r):
    return {
        "id": r[0], "type": r[1], "description": r[2], "game": r[3],
        "american_odds": r[4], "units": r[5], "stake": r[6],
        "potential_win": r[7], "reasoning": r[8],
    }
