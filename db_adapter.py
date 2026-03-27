"""
Database adapter — SQLite (local dev) or PostgreSQL (Railway/production).
Set DATABASE_URL env var to use PostgreSQL. Falls back to SQLite automatically.
"""
import os
import sqlite3

DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_PATH = os.getenv("DB_PATH", "trades.db")


def pg() -> bool:
    """True when running against PostgreSQL."""
    return bool(DATABASE_URL)


def connect():
    """Open and return a DB connection."""
    if pg():
        import psycopg2
        return psycopg2.connect(DATABASE_URL)
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
