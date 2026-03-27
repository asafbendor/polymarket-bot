"""
Risk Manager - hard rules, daily budget tracking, trade logging
"""

import logging
from datetime import datetime, timezone
from edge_calculator import TradeOpportunity
import db_adapter

logger = logging.getLogger(__name__)

DAILY_BUDGET = 10.0
MAX_TRADE_SIZE = 1.0
MAX_OPEN_POSITIONS = 5
MIN_HOURS_TO_RESOLUTION = 6
MAX_HOURS_TO_RESOLUTION = 168  # only trade markets resolving within 7 days
DATA_DRIVEN_CATEGORIES = {"sports", "crypto", "weather", "political", "economic"}
STOP_LOSS_THRESHOLD = -8.0


class RiskManager:
    def __init__(self, db_path: str = "trades.db"):
        self._init_db()
        self._ensure_daily_record()

    # ------------------------------------------------------------------
    # DB schema
    # ------------------------------------------------------------------
    def _init_db(self):
        conn = db_adapter.connect()
        c = conn.cursor()

        c.execute(db_adapter.adapt("""
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT NOT NULL,
                condition_id    TEXT NOT NULL,
                question        TEXT,
                direction       TEXT,
                market_price    REAL,
                fair_value      REAL,
                edge            REAL,
                kelly_fraction  REAL,
                position_size   REAL,
                limit_price     REAL,
                order_id        TEXT,
                status          TEXT DEFAULT 'pending',
                fill_price      REAL,
                pnl             REAL DEFAULT 0,
                paper           INTEGER DEFAULT 1,
                reason          TEXT,
                end_date        TEXT DEFAULT '',
                slug            TEXT DEFAULT ''
            )
        """))

        c.execute(db_adapter.adapt("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date            TEXT PRIMARY KEY,
                spent           REAL DEFAULT 0,
                realized_pnl    REAL DEFAULT 0,
                open_positions  INTEGER DEFAULT 0
            )
        """))

        c.execute(db_adapter.adapt("""
            CREATE TABLE IF NOT EXISTS rejected_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT,
                condition_id    TEXT,
                question        TEXT,
                reason          TEXT,
                edge            REAL,
                position_size   REAL
            )
        """))

        # Migration: add columns if missing (safe to run on existing DBs)
        for col, definition in [("end_date", "TEXT DEFAULT ''"), ("slug", "TEXT DEFAULT ''"),
                                 ("category", "TEXT DEFAULT ''"), ("market_url", "TEXT DEFAULT ''")]:
            try:
                c.execute(db_adapter.adapt(f"ALTER TABLE trades ADD COLUMN {col} {definition}"))
            except Exception:
                pass  # column already exists

        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Daily record helpers
    # ------------------------------------------------------------------
    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_daily_record(self):
        conn = db_adapter.connect()
        c = conn.cursor()
        if db_adapter.pg():
            c.execute(
                "INSERT INTO daily_stats (date) VALUES (%s) ON CONFLICT (date) DO NOTHING",
                (self._today(),)
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)",
                (self._today(),)
            )
        conn.commit()
        conn.close()

    def get_daily_spent(self) -> float:
        self._ensure_daily_record()
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "SELECT spent FROM daily_stats WHERE date=?"), (self._today(),))
        row = db_adapter.fetchone(c)
        conn.close()
        return row["spent"] if row else 0.0

    def get_daily_pnl(self) -> float:
        self._ensure_daily_record()
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "SELECT realized_pnl FROM daily_stats WHERE date=?"), (self._today(),))
        row = db_adapter.fetchone(c)
        conn.close()
        return row["realized_pnl"] if row else 0.0

    def get_open_categories(self) -> set:
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "SELECT DISTINCT category FROM trades WHERE status IN ('pending','filled','paper') AND date(timestamp)=?"),
            (self._today(),))
        rows = db_adapter.fetchrows(c)
        conn.close()
        return {r["category"] for r in rows if r.get("category")}

    def get_open_positions(self) -> int:
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "SELECT COUNT(*) as n FROM trades WHERE status IN ('pending','filled','paper') AND date(timestamp)=?"),
            (self._today(),))
        row = db_adapter.fetchone(c)
        conn.close()
        return row["n"] if row else 0

    def get_budget_remaining(self) -> float:
        return max(0.0, DAILY_BUDGET - self.get_daily_spent())

    # ------------------------------------------------------------------
    # Approval gate
    # ------------------------------------------------------------------
    def approve(self, opp: TradeOpportunity) -> tuple[bool, str]:
        self._ensure_daily_record()

        daily_pnl = self.get_daily_pnl()
        if daily_pnl < STOP_LOSS_THRESHOLD:
            return False, f"Daily stop-loss triggered: P&L={daily_pnl:.2f}"

        spent = self.get_daily_spent()
        remaining = DAILY_BUDGET - spent
        if remaining < 0.50:
            return False, f"Daily budget exhausted: spent=${spent:.2f}/${DAILY_BUDGET:.2f}"

        if opp.hours_left < MIN_HOURS_TO_RESOLUTION:
            return False, f"Too close to resolution: {opp.hours_left:.1f}h left"

        if opp.hours_left > MAX_HOURS_TO_RESOLUTION:
            return False, f"Resolves too far: {opp.hours_left:.0f}h > {MAX_HOURS_TO_RESOLUTION}h max"

        if opp.category not in DATA_DRIVEN_CATEGORIES:
            return False, f"Category '{opp.category}' not supported — skipping"

        open_cats = self.get_open_categories()
        if opp.category in open_cats:
            return False, f"Already have open position in {opp.category}"

        open_pos = self.get_open_positions()
        if open_pos >= MAX_OPEN_POSITIONS:
            return False, f"Max open positions reached: {open_pos}/{MAX_OPEN_POSITIONS}"

        if opp.position_size > MAX_TRADE_SIZE:
            return False, f"Position size ${opp.position_size:.2f} > max ${MAX_TRADE_SIZE:.2f}"

        if abs(opp.edge) < 0.08:
            return False, f"Edge {opp.edge:.1%} below minimum 8%"

        return True, "approved"

    # ------------------------------------------------------------------
    # Trade logging
    # ------------------------------------------------------------------
    def log_trade(
        self,
        opp: TradeOpportunity,
        limit_price: float,
        order_id: str = "",
        paper: bool = True,
        reason: str = "",
    ) -> int:
        conn = db_adapter.connect()
        c = conn.cursor()

        params = (
            datetime.now(timezone.utc).isoformat(),
            opp.condition_id,
            opp.question,
            opp.direction,
            opp.market_price,
            opp.fair_value,
            opp.edge,
            opp.fractional_kelly,
            opp.position_size,
            limit_price,
            order_id,
            "paper" if paper else "pending",
            1 if paper else 0,
            reason,
            getattr(opp, "end_date", ""),
            getattr(opp, "slug", ""),
            getattr(opp, "category", ""),
            getattr(opp, "market_url", ""),
        )

        if db_adapter.pg():
            c.execute("""
                INSERT INTO trades
                (timestamp, condition_id, question, direction, market_price,
                 fair_value, edge, kelly_fraction, position_size, limit_price,
                 order_id, status, paper, reason, end_date, slug, category, market_url)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                RETURNING id
            """, params)
            trade_id = c.fetchone()[0]
        else:
            c.execute("""
                INSERT INTO trades
                (timestamp, condition_id, question, direction, market_price,
                 fair_value, edge, kelly_fraction, position_size, limit_price,
                 order_id, status, paper, reason, end_date, slug, category, market_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, params)
            trade_id = c.lastrowid

        c.execute(db_adapter.adapt(
            "UPDATE daily_stats SET spent=spent+?, open_positions=open_positions+1 WHERE date=?"),
            (opp.position_size, self._today())
        )

        conn.commit()
        conn.close()
        return trade_id

    def log_rejection(self, opp: TradeOpportunity, reason: str):
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt("""
            INSERT INTO rejected_trades (timestamp, condition_id, question, reason, edge, position_size)
            VALUES (?,?,?,?,?,?)
        """), (
            datetime.now(timezone.utc).isoformat(),
            opp.condition_id,
            opp.question,
            reason,
            opp.edge,
            opp.position_size,
        ))
        conn.commit()
        conn.close()

    def update_trade_status(self, order_id: str, status: str, fill_price: float = 0.0, pnl: float = 0.0):
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "UPDATE trades SET status=?, fill_price=?, pnl=? WHERE order_id=?"),
            (status, fill_price, pnl, order_id))

        if status in ("cancelled", "expired") and pnl == 0:
            c.execute(db_adapter.adapt("""
                UPDATE daily_stats SET
                    spent = MAX(0, spent - (SELECT position_size FROM trades WHERE order_id=?)),
                    open_positions = MAX(0, open_positions - 1)
                WHERE date=?
            """), (order_id, self._today()))
        elif status == "filled" and pnl != 0:
            c.execute(db_adapter.adapt(
                "UPDATE daily_stats SET realized_pnl=realized_pnl+?, open_positions=MAX(0,open_positions-1) WHERE date=?"),
                (pnl, self._today())
            )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Status summary
    # ------------------------------------------------------------------
    def status_line(self) -> str:
        spent = self.get_daily_spent()
        pnl = self.get_daily_pnl()
        remaining = DAILY_BUDGET - spent
        sign = "+" if pnl >= 0 else ""
        return (
            f"Daily P&L: {sign}${pnl:.2f} | "
            f"Budget remaining: ${remaining:.2f}/${DAILY_BUDGET:.2f} | "
            f"Open positions: {self.get_open_positions()}/{MAX_OPEN_POSITIONS}"
        )
