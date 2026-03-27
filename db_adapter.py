"""
Database adapter — SQLite (local dev) or PostgreSQL (Railway/production).
Set DATABASE_URL env var to use PostgreSQL. Falls back to SQLite automatically.
"""
import os
import sqlite3

DB_PATH = os.getenv("DB_PATH", "trades.db")


def pg() -> bool:
    """True when running against PostgreSQL."""
    return bool(os.getenv("DATABASE_URL", ""))


def connect():
    """Open and return a DB connection (with retry for Railway timeouts)."""
    if pg():
        import psycopg2
        import time
        url = os.getenv("DATABASE_URL")
        for attempt in range(3):
            try:
                return psycopg2.connect(
                    url,
                    keepalives=1,
                    keepalives_idle=30,
                    keepalives_interval=10,
                    keepalives_count=5,
                    connect_timeout=15,
                )
            except psycopg2.OperationalError:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                else:
                    raise
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def adapt(sql: str) -> str:
    """Translate SQLite SQL to PostgreSQL syntax."""
    if not pg():
        return sql
    sql = sql.replace("?", "%s")
    sql = sql.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "SERIAL PRIMARY KEY")
    sql = sql.replace("date(timestamp)", "timestamp::date")
    sql = sql.replace("MAX(0, ", "GREATEST(0, ")
    sql = sql.replace("MAX(0,", "GREATEST(0,")
    return sql


def fetchrows(cur) -> list[dict]:
    """Fetch all rows as list of dicts."""
    if pg():
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    return [dict(r) for r in cur.fetchall()]


def fetchone(cur) -> dict | None:
    """Fetch one row as dict."""
    if pg():
        r = cur.fetchone()
        if r is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, r))
    r = cur.fetchone()
    return dict(r) if r else None
