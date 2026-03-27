"""
Risk Manager - hard rules, daily budget tracking, trade logging
"""

import sqlite3
import logging
from datetime import datetime, timezone
from typing import Optional
from edge_calculator import TradeOpportunity

logger = logging.getLogger(__name__)

DAILY_BUDGET = 10.0
MAX_TRADE_SIZE = 2.0
MAX_OPEN_POSITIONS = 3
MIN_HOURS_TO_RESOLUTION = 6
STOP_LOSS_THRESHOLD = -8.0  # stop if daily P&L below this


class RiskManager:
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self._init_db()
        self._ensure_daily_record()

    # ------------------------------------------------------------------
    # DB schema
    # ------------------------------------------------------------------
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        c.execute("""
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
                reason          TEXT
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_stats (
                date            TEXT PRIMARY KEY,
                spent           REAL DEFAULT 0,
                realized_pnl    REAL DEFAULT 0,
                open_positions  INTEGER DEFAULT 0
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS rejected_trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp       TEXT,
                condition_id    TEXT,
                question        TEXT,
                reason          TEXT,
                edge            REAL,
                position_size   REAL
            )
        """)

        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Daily record helpers
    # ------------------------------------------------------------------
    def _today(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def _ensure_daily_record(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO daily_stats (date) VALUES (?)",
            (self._today(),)
        )
        conn.commit()
        conn.close()

    def get_daily_spent(self) -> float:
        self._ensure_daily_record()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT spent FROM daily_stats WHERE date=?", (self._today(),))
        row = c.fetchone()
        conn.close()
        return row[0] if row else 0.0

    def get_daily_pnl(self) -> float:
        self._ensure_daily_record()
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT realized_pnl FROM daily_stats WHERE date=?", (self._today(),))
        row = c.fetchone()
        conn.close()
        return row[0] if row else 0.0

    def get_open_positions(self) -> int:
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM trades WHERE status IN ('pending','filled','paper') AND date(timestamp)=?",
            (self._today(),)
        )
        row = c.fetchone()
        conn.close()
        return row[0] if row else 0

    def get_budget_remaining(self) -> float:
        return max(0.0, DAILY_BUDGET - self.get_daily_spent())

    # ------------------------------------------------------------------
    # Approval gate
    # ------------------------------------------------------------------
    def approve(self, opp: TradeOpportunity) -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        All hard rules checked here.
        """
        self._ensure_daily_record()

        # 1. Stop-loss
        daily_pnl = self.get_daily_pnl()
        if daily_pnl < STOP_LOSS_THRESHOLD:
            return False, f"Daily stop-loss triggered: P&L={daily_pnl:.2f}"

        # 2. Daily budget
        spent = self.get_daily_spent()
        remaining = DAILY_BUDGET - spent
        if remaining < 0.50:
            return False, f"Daily budget exhausted: spent=${spent:.2f}/${DAILY_BUDGET:.2f}"

        # 3. Too close to resolution
        if opp.hours_left < MIN_HOURS_TO_RESOLUTION:
            return False, f"Too close to resolution: {opp.hours_left:.1f}h left"

        # 4. Max open positions
        open_pos = self.get_open_positions()
        if open_pos >= MAX_OPEN_POSITIONS:
            return False, f"Max open positions reached: {open_pos}/{MAX_OPEN_POSITIONS}"

        # 5. Position size cap
        if opp.position_size > MAX_TRADE_SIZE:
            return False, f"Position size ${opp.position_size:.2f} > max ${MAX_TRADE_SIZE:.2f}"

        # 6. Minimum edge (redundant safety check)
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
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO trades
            (timestamp, condition_id, question, direction, market_price,
             fair_value, edge, kelly_fraction, position_size, limit_price,
             order_id, status, paper, reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
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
        ))
        trade_id = c.lastrowid

        # Update daily spent — always track, even in paper mode
        c.execute(
            "UPDATE daily_stats SET spent=spent+?, open_positions=open_positions+1 WHERE date=?",
            (opp.position_size, self._today())
        )

        conn.commit()
        conn.close()
        return trade_id

    def log_rejection(self, opp: TradeOpportunity, reason: str):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT INTO rejected_trades (timestamp, condition_id, question, reason, edge, position_size)
            VALUES (?,?,?,?,?,?)
        """, (
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
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            UPDATE trades SET status=?, fill_price=?, pnl=? WHERE order_id=?
        """, (status, fill_price, pnl, order_id))
        if status in ("cancelled", "expired") and pnl == 0:
            # Refund the budget if order cancelled without fill
            c.execute("""
                UPDATE daily_stats SET
                    spent = MAX(0, spent - (SELECT position_size FROM trades WHERE order_id=?)),
                    open_positions = MAX(0, open_positions - 1)
                WHERE date=?
            """, (order_id, self._today()))
        elif status == "filled" and pnl != 0:
            c.execute(
                "UPDATE daily_stats SET realized_pnl=realized_pnl+?, open_positions=MAX(0,open_positions-1) WHERE date=?",
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
