"""
Polymarket Trading Bot - Main Orchestrator
Default: paper trading mode. Use --live to execute real trades.

Usage:
  python main.py              # paper mode (safe)
  python main.py --live       # live trading (real money!)
  python main.py --once       # single scan then exit
"""

import asyncio
import aiohttp
import argparse
import logging
import sys
import os
from datetime import datetime, timezone

# Fix Windows console encoding
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
import urllib.request
import urllib.parse

load_dotenv()  # load .env if present, but Railway env vars take priority

from market_scanner import MarketScanner, SCAN_INTERVAL_SECONDS
from fair_value import FairValueEngine
from edge_calculator import EdgeCalculator
from risk_manager import RiskManager, MAX_OPEN_POSITIONS
from executor import Executor, ORDER_CHECK_INTERVAL

RESOLUTION_CHECK_INTERVAL = 900  # 15 minutes

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
class DbLogHandler(logging.Handler):
    """Writes WARNING+ log records to bot_log table in DB (for dashboard).
    INFO-level goes to file/stdout only — avoids flooding PostgreSQL with thousands of rows."""
    def emit(self, record):
        if record.levelno < logging.WARNING:
            return  # skip INFO/DEBUG — too noisy for DB
        try:
            import db_adapter
            msg = self.format(record)
            conn = db_adapter.connect()
            c = conn.cursor()
            c.execute(db_adapter.adapt(
                "INSERT INTO bot_log (timestamp, level, message) VALUES (?,?,?)"
            ), (datetime.now(timezone.utc).isoformat(), record.levelname, msg[:500]))
            c.execute(db_adapter.adapt(
                "DELETE FROM bot_log WHERE id NOT IN (SELECT id FROM bot_log ORDER BY id DESC LIMIT 200)"
            ))
            conn.commit()
            conn.close()
        except Exception:
            pass  # never let logging crash the bot


# Helper to force-write an INFO line to bot_log (for key events like scan summary, bets placed)
def log_to_db(message: str):
    try:
        import db_adapter
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "INSERT INTO bot_log (timestamp, level, message) VALUES (?,?,?)"
        ), (datetime.now(timezone.utc).isoformat(), "INFO", message[:500]))
        c.execute(db_adapter.adapt(
            "DELETE FROM bot_log WHERE id NOT IN (SELECT id FROM bot_log ORDER BY id DESC LIMIT 200)"
        ))
        conn.commit()
        conn.close()
    except Exception:
        pass


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-8s %(message)s"
    datefmt = "%H:%M:%S"
    logging.basicConfig(level=level, format=fmt, datefmt=datefmt,
                        handlers=[
                            logging.StreamHandler(sys.stdout),
                            logging.FileHandler("bot.log", encoding="utf-8"),
                            DbLogHandler(),
                        ])
    # Quiet noisy libraries
    for lib in ["aiohttp", "asyncio", "httpx", "httpcore", "hpack"]:
        logging.getLogger(lib).setLevel(logging.WARNING)
    # Suppress py_clob_client HTTP request logs
    logging.getLogger("py_clob_client").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Pending orders tracker (order_id -> trade info)
# ------------------------------------------------------------------
pending_orders: dict[str, dict] = {}


# ------------------------------------------------------------------
# Telegram notifications
# ------------------------------------------------------------------
def send_telegram(message: str, silent: bool = False):
    """Send a message to Telegram. Silently fails if not configured."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.debug("Telegram not configured (missing token or chat_id)")
        return
    try:
        import json as _json
        payload = _json.dumps({
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=5).read()
        logger.info(f"Telegram sent OK: {message[:50]}")
    except Exception as e:
        logger.warning(f"Telegram send failed: {e}")


# ------------------------------------------------------------------
# Single scan cycle
# ------------------------------------------------------------------
async def run_scan_cycle(
    scanner: MarketScanner,
    fair_engine: FairValueEngine,
    edge_calc: EdgeCalculator,
    risk_mgr: RiskManager,
    executor: Executor,
    paper: bool,
):
    now_str = datetime.now(timezone.utc).strftime("%H:%M")
    mode_tag = "[PAPER]" if paper else "[LIVE]"

    # Phase 1: Fetch markets
    markets = await scanner.scan_all_markets()
    total_markets = len(markets)

    # Early exit: no point calling Claude if budget/positions are exhausted
    budget_check = risk_mgr.get_budget_remaining()
    positions_check = risk_mgr.get_open_positions()
    if budget_check < 0.50 or positions_check >= MAX_OPEN_POSITIONS:
        print(f"[{now_str}] Skipping analysis — budget=${budget_check:.2f}, positions={positions_check}/{MAX_OPEN_POSITIONS}")
        return

    # Phase 2 (Improvement 3): Estimate fair value for ALL markets concurrently,
    # then sort by edge so we always trade the best opportunities first.
    #
    # Claude is only called for "other/political" markets — cap that to top 100
    # by liquidity to avoid blowing rate limits on 700+ requests.
    MAX_CLAUDE_MARKETS = int(os.getenv("MAX_CLAUDE_MARKETS", "8"))
    non_claude_categories = {"crypto", "sports", "weather"}

    # Sort: non-Claude first, then fewest hours left (soonest resolution), then by liquidity
    def sort_key(m):
        is_fast = m.get("category") in non_claude_categories
        hours = m.get("hours_left", 9999)
        return (0 if is_fast else 1, hours, -m.get("liquidity", 0))

    sorted_markets = sorted(markets, key=sort_key)

    # Count how many Claude-dependent markets we'll include
    claude_count = 0
    selected_markets = []
    for m in sorted_markets:
        if m.get("category") in non_claude_categories:
            selected_markets.append(m)
        elif claude_count < MAX_CLAUDE_MARKETS:
            selected_markets.append(m)
            claude_count += 1

    logger.info(
        f"Estimating fair values: {len(selected_markets)} markets "
        f"({claude_count} Claude, {len(selected_markets)-claude_count} data APIs)"
    )

    sem = asyncio.Semaphore(12)  # cap concurrent API calls

    async def estimate_one(market: dict):
        async with sem:
            fv = await fair_engine.estimate(market)
            return market, fv

    all_results = await asyncio.gather(*[estimate_one(m) for m in selected_markets])

    got_fv = sum(1 for _, fv in all_results if fv is not None)
    logger.info(f"Fair values obtained: {got_fv}/{len(all_results)}")
    # Log per-category breakdown
    from collections import Counter
    cat_counts = Counter(m.get("category") for m, fv in all_results if fv is not None)
    logger.info(f"Fair values by category: {dict(cat_counts)}")

    # Phase 3: Score all results, collect candidates sorted by edge
    budget_remaining = risk_mgr.get_budget_remaining()
    candidates = []
    near_misses = []

    for market, fair_value in all_results:
        if fair_value is None:
            continue
        opp = edge_calc.evaluate(market, fair_value, budget_remaining)
        if opp is not None:
            candidates.append(opp)
        else:
            # Track near-misses for logging
            yes_price = market.get("yes_price", 0.5)
            edge_approx = max(fair_value - yes_price, (1 - fair_value) - (1 - yes_price))
            if edge_approx > 0.05:
                direction = "YES" if fair_value > yes_price else "NO"
                mkt_p = yes_price if direction == "YES" else market.get("no_price", 1 - yes_price)
                near_misses.append((market["question"], mkt_p, fair_value, direction, edge_approx))

    # Sort: highest absolute edge first
    candidates.sort(key=lambda o: abs(o.edge), reverse=True)

    # Log near-misses
    for question, mkt_p, fair_value, direction, edge in near_misses[:5]:
        logger.info(
            f"near-miss: {question[:55]} "
            f"market={mkt_p:.0%} fair={fair_value:.0%} "
            f"{direction} edge={edge:.0%} (below threshold)"
        )

    # Phase 4: Trade top candidates
    opportunities_found = 0
    trades_attempted = 0

    for opp in candidates:
        budget_remaining = risk_mgr.get_budget_remaining()
        if budget_remaining < 0.50:
            logger.info(f"{now_str} Budget exhausted, stopping")
            break

        opportunities_found += 1
        sign = "+" if opp.edge >= 0 else ""
        logger.info(
            f"candidate: {opp.question[:55]} "
            f"market={opp.market_price:.0%} fair={opp.fair_value:.0%} "
            f"{opp.direction} edge={sign}{opp.edge:.0%}"
        )

        # Risk manager approval
        approved, reason = risk_mgr.approve(opp)
        if not approved:
            logger.info(f"REJECTED: {reason}")
            risk_mgr.log_rejection(opp, reason)
            log_to_db(f"REJECTED: {opp.direction} '{opp.question[:45]}' | {reason}")
            continue

        # Execute
        result = await executor.execute(opp)
        limit_price = result.get("limit_price", opp.fair_value)

        logger.info(
            f"{mode_tag} BET: ${opp.position_size:.2f} {opp.direction} "
            f"on '{opp.question[:50]}' @ {limit_price:.3f}"
        )

        # Telegram notification — only if order actually succeeded
        poly_url = getattr(opp, 'market_url', '') or ''
        link = f'<a href="{poly_url}">Polymarket</a>' if poly_url else ''
        if result.get("status") == "error":
            logger.error(f"Order FAILED: {result.get('message','')}")
            continue  # don't log failed orders to DB, don't send Telegram

        # Log to DB only on success
        trade_id = risk_mgr.log_trade(
            opp,
            limit_price=limit_price,
            order_id=result.get("order_id", ""),
            paper=paper,
            reason=result.get("message", ""),
        )

        if True:
            send_telegram(
                f"{'LIVE' if not paper else 'PAPER'} Bet placed!\n"
                f"{opp.question[:80]}\n"
                f"{opp.direction} ${opp.position_size:.2f} @ {limit_price:.1%}\n"
                f"Edge: {opp.edge:+.1%} | Fair: {opp.fair_value:.0%}\n"
                f"OrderID: {result.get('order_id','none')}\n"
                f"{link}"
            )

        order_id = result.get("order_id", "")
        log_to_db(
            f"{'LIVE' if not paper else 'PAPER'} BET: {opp.direction} ${opp.position_size:.2f} "
            f"@ {limit_price:.1%} | edge={opp.edge:+.1%} | "
            f"'{opp.question[:50]}' | order={order_id[:16] or 'no-id'}"
        )
        if order_id:
            pending_orders[order_id] = {
                "trade_id": trade_id,
                "opp": opp,
                "placed_at": asyncio.get_event_loop().time(),
            }

        trades_attempted += 1
        await asyncio.sleep(0.5)

    # Summary — force-write to DB so dashboard log always shows scan results
    summary = (
        f"Scan done: {total_markets} markets | "
        f"{len(candidates)} candidates | "
        f"{trades_attempted} placed"
    )
    logger.info(summary)
    logger.info(risk_mgr.status_line())
    log_to_db(summary)
    log_to_db(risk_mgr.status_line())



# ------------------------------------------------------------------
# Order follow-up loop
# ------------------------------------------------------------------
async def _run_order_checks(executor: Executor, risk_mgr: RiskManager):
    """Check all pending orders once — update status in DB."""
    import db_adapter
    # Reload pending orders from DB (catch any added since last check)
    try:
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute(db_adapter.adapt(
            "SELECT order_id, timestamp FROM trades WHERE status='pending' "
            "AND order_id IS NOT NULL AND order_id != ''"
        ))
        rows = db_adapter.fetchrows(c)
        conn.close()
        for row in rows:
            oid = row.get("order_id", "")
            if oid and oid not in pending_orders:
                pending_orders[oid] = {"trade_id": None, "opp": None, "placed_at": 0,
                                        "db_timestamp": row.get("timestamp", "")}
    except Exception as e:
        logger.debug(f"Could not reload pending orders: {e}")

    if not pending_orders:
        return

    logger.info(f"Order check: {len(pending_orders)} pending order(s)")
    to_remove = []
    now_utc = datetime.now(timezone.utc)

    for order_id, info in list(pending_orders.items()):
        result = await executor.check_order_status(order_id)
        status = result.get("status", "unknown")
        logger.info(f"  order {order_id[:20]}... → CLOB status={status}")

        if status in ("filled", "matched", "MATCHED"):
            fill_price = result.get("fill_price", 0.0)
            logger.info(f"Order filled: {order_id[:20]}... @ {fill_price:.3f}")
            risk_mgr.update_trade_status(order_id, "filled", fill_price)
            to_remove.append(order_id)

        elif status in ("cancelled", "CANCELLED", "unmatched", "UNMATCHED"):
            logger.info(f"Order cancelled/expired: {order_id[:20]}...")
            risk_mgr.update_trade_status(order_id, "cancelled")
            to_remove.append(order_id)

        else:
            # Order still live on CLOB — check age, auto-expire after 48h
            db_ts = info.get("db_timestamp", "")
            age_hours = 0
            if db_ts:
                try:
                    placed = datetime.fromisoformat(db_ts.replace("Z", "+00:00"))
                    age_hours = (now_utc - placed).total_seconds() / 3600
                except Exception:
                    pass
            logger.info(f"Order live on CLOB: {order_id[:20]}... age={age_hours:.1f}h (status={status})")
            if age_hours > 12:
                logger.warning(f"Cancelling stale order {order_id[:20]}... ({age_hours:.0f}h unfilled)")
                await executor.cancel_order(order_id)
                risk_mgr.update_trade_status(order_id, "cancelled")
                to_remove.append(order_id)

    for oid in to_remove:
        pending_orders.pop(oid, None)


async def order_followup_loop(executor: Executor, risk_mgr: RiskManager):
    """Check pending orders immediately on startup, then every ORDER_CHECK_INTERVAL seconds."""
    # Run immediately on startup
    try:
        await _run_order_checks(executor, risk_mgr)
    except Exception as e:
        logger.error(f"Startup order check error: {e}")

    while True:
        await asyncio.sleep(ORDER_CHECK_INTERVAL)
        try:
            await _run_order_checks(executor, risk_mgr)
        except Exception as e:
            logger.error(f"Order followup error: {e}")


# ------------------------------------------------------------------
# Market resolution checker
# ------------------------------------------------------------------
async def check_resolved_positions(risk_mgr: RiskManager, session: aiohttp.ClientSession):
    """Check all filled trades against Gamma API — update pnl when market resolves."""
    import db_adapter, json as _json
    conn = db_adapter.connect()
    c = conn.cursor()
    c.execute(db_adapter.adapt(
        "SELECT id, order_id, condition_id, direction, position_size, limit_price, fill_price "
        "FROM trades WHERE status='filled'"
    ))
    rows = db_adapter.fetchrows(c)
    conn.close()

    if not rows:
        return

    logger.info(f"Resolution check: {len(rows)} filled position(s) to check")

    for row in rows:
        condition_id = row.get("condition_id", "")
        if not condition_id:
            continue
        try:
            url = f"https://gamma-api.polymarket.com/markets?condition_ids={condition_id}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json()
                if not data:
                    continue
                market = data[0] if isinstance(data, list) else data

                if not (market.get("closed") or market.get("resolved")):
                    continue

                outcome_prices_raw = market.get("outcomePrices", "")
                if isinstance(outcome_prices_raw, str):
                    outcome_prices = _json.loads(outcome_prices_raw) if outcome_prices_raw else []
                else:
                    outcome_prices = outcome_prices_raw or []

                if len(outcome_prices) < 2:
                    continue

                yes_won = float(outcome_prices[0]) >= 0.99
                direction = row.get("direction", "YES")
                position_size = float(row.get("position_size") or 0)
                price = float(row.get("fill_price") or row.get("limit_price") or 0.5)
                if price <= 0:
                    price = 0.5

                shares = position_size / price
                if (direction == "YES" and yes_won) or (direction == "NO" and not yes_won):
                    pnl = round(shares - position_size, 4)
                    new_status = "won"
                else:
                    pnl = round(-position_size, 4)
                    new_status = "lost"

                order_id = row.get("order_id", "")
                logger.info(
                    f"Resolved: {market.get('question','')[:55]} | "
                    f"{'YES' if yes_won else 'NO'} won | our {direction} → {new_status} | pnl=${pnl:+.2f}"
                )
                risk_mgr.update_trade_status(order_id, new_status, price, pnl)
                send_telegram(
                    f"Market resolved: {market.get('question','')[:60]}\n"
                    f"Result: {'YES' if yes_won else 'NO'} won\n"
                    f"Our {direction} position: {'WON' if new_status == 'won' else 'LOST'}\n"
                    f"P&L: {pnl:+.2f} USDC"
                )

        except Exception as e:
            logger.debug(f"Resolution check error for {condition_id}: {e}")


async def resolution_check_loop(risk_mgr: RiskManager, session: aiohttp.ClientSession):
    """Runs every 15 minutes to detect resolved markets and book P&L."""
    while True:
        await asyncio.sleep(RESOLUTION_CHECK_INTERVAL)
        try:
            await check_resolved_positions(risk_mgr, session)
        except Exception as e:
            logger.error(f"Resolution check loop error: {e}")


# ------------------------------------------------------------------
# Close overdue positions (>7 days to resolution)
# ------------------------------------------------------------------
async def close_overdue_positions(executor: Executor, risk_mgr: RiskManager, session: aiohttp.ClientSession):
    """At startup: find filled positions that resolve >7 days out and sell at break-even."""
    import db_adapter, json as _json
    from risk_manager import MAX_HOURS_TO_RESOLUTION

    conn = db_adapter.connect()
    c = conn.cursor()
    c.execute(db_adapter.adapt(
        "SELECT id, order_id, condition_id, direction, position_size, limit_price, fill_price, question, end_date "
        "FROM trades WHERE status='filled'"
    ))
    rows = db_adapter.fetchrows(c)
    conn.close()

    if not rows:
        return

    now = datetime.now(timezone.utc)
    for row in rows:
        end_date_str = row.get("end_date", "")
        if not end_date_str:
            continue
        try:
            end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            if not end_dt.tzinfo:
                end_dt = end_dt.replace(tzinfo=timezone.utc)
            hours_left = (end_dt - now).total_seconds() / 3600
        except Exception:
            continue

        if hours_left <= MAX_HOURS_TO_RESOLUTION:
            continue  # within limit, skip

        question = row.get("question", "")[:60]
        logger.warning(f"Overdue position ({hours_left:.0f}h left): {question}")
        log_to_db(f"Attempting to close overdue position ({hours_left:.0f}h): {question}")

        # Get token_id from CLOB
        condition_id = row.get("condition_id", "")
        direction = row.get("direction", "YES")
        try:
            url = f"https://clob.polymarket.com/markets/{condition_id}"
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    logger.warning(f"Could not fetch market data for {condition_id}")
                    continue
                data = await resp.json(content_type=None)
                token_id = None
                for tok in (data.get("tokens") or []):
                    if str(tok.get("outcome", "")).upper() == direction:
                        token_id = str(tok.get("token_id", ""))
                        break
                if not token_id:
                    logger.warning(f"No token_id found for {direction} on {condition_id}")
                    continue
        except Exception as e:
            logger.warning(f"CLOB market fetch failed: {e}")
            continue

        # Break-even price = what we paid
        entry_price = float(row.get("fill_price") or row.get("limit_price") or 0)
        if entry_price <= 0:
            entry_price = 0.029  # fallback for Russia/Ukraine
        position_size = float(row.get("position_size") or 0)
        shares = round(position_size / entry_price, 2)

        result = await executor.sell_position(token_id, shares, entry_price)
        if result.get("status") == "error":
            logger.warning(f"Sell failed: {result.get('message')}")
            send_telegram(f"Could not auto-close overdue position:\n{question}\nReason: {result.get('message')}")
        else:
            logger.warning(f"Sell order placed for overdue position: {question}")
            log_to_db(f"SELL order placed (overdue close): {direction} {shares:.1f} shares @ {entry_price:.4f} | {question}")
            send_telegram(f"Auto-closing overdue position (>{MAX_HOURS_TO_RESOLUTION}h):\n{question}\nSELL {shares:.1f} shares @ {entry_price:.4f}")


# ------------------------------------------------------------------
# Midnight reset
# ------------------------------------------------------------------
async def midnight_reset_loop(risk_mgr: RiskManager):
    """Ensure daily stats record exists at midnight UTC."""
    while True:
        now = datetime.now(timezone.utc)
        # Sleep until next midnight UTC
        seconds_to_midnight = (
            (24 - now.hour) * 3600
            - now.minute * 60
            - now.second
        )
        await asyncio.sleep(max(seconds_to_midnight, 60))
        risk_mgr._ensure_daily_record()
        logger.info("Daily budget reset at midnight UTC")


# ------------------------------------------------------------------
# Hourly status
# ------------------------------------------------------------------
async def hourly_status_loop(risk_mgr: RiskManager):
    while True:
        await asyncio.sleep(3600)
        now_str = datetime.now(timezone.utc).strftime("%H:%M")
        print(f"\n{'='*60}")
        print(f"[{now_str}] HOURLY PORTFOLIO STATUS")
        print(f"[{now_str}] {risk_mgr.status_line()}")
        print(f"{'='*60}\n")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
async def main(paper: bool = True, once: bool = False, verbose: bool = False):
    setup_logging(verbose)

    mode = "PAPER TRADING" if paper else "*** LIVE TRADING — REAL MONEY ***"
    print(f"\n{'='*60}")
    print(f"  Polymarket Bot — {mode}")
    print(f"  Daily budget: $10.00 USDC")
    print(f"  Min edge: 8% | Max trade: $2.00 | Max positions: 3")
    print(f"{'='*60}\n")

    if not paper:
        print("WARNING: Live mode active. Press Ctrl+C within 5s to abort.")
        await asyncio.sleep(5)

    db_path = "trades.db"
    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "").strip().lstrip("=")

    # Clear old log entries on startup + cancel fake pending trades (no order_id)
    try:
        import db_adapter
        conn = db_adapter.connect()
        c = conn.cursor()
        c.execute("DELETE FROM bot_log")
        # Cancel pending trades that never got an order_id (failed silently)
        c.execute(db_adapter.adapt(
            "UPDATE trades SET status='cancelled' WHERE status='pending' AND (order_id IS NULL OR order_id='')"
        ))
        # Recalculate daily spent from actual active trades only
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        c.execute(db_adapter.adapt("""
            UPDATE daily_stats SET spent = (
                SELECT COALESCE(SUM(position_size), 0) FROM trades
                WHERE status IN ('pending','filled') AND timestamp LIKE ?
            ) WHERE date = ?
        """), (today + "%", today))
        conn.commit()
        conn.close()
        print("  Bot log cleared (fresh start)")
        print("  Stale pending trades cancelled, budget recalculated")
    except Exception as e:
        print(f"  Startup cleanup warning: {e}")

    # Startup diagnostics
    if anthropic_key:
        masked = anthropic_key[:10] + "..." + anthropic_key[-4:]
        print(f"  Claude API key: {masked} (length={len(anthropic_key)})")
    else:
        print("  WARNING: ANTHROPIC_API_KEY is empty! Claude estimates will fail.")
    print(f"  All env vars with 'ANTHROPIC': {[k for k in os.environ if 'ANTHROPIC' in k.upper()]}")

    # Test Telegram on startup
    mode_str = "LIVE" if not paper else "PAPER"
    send_telegram(f"Polymarket Bot started ({mode_str} mode)")

    risk_mgr = RiskManager(db_path=db_path)
    executor = Executor(paper=paper)
    edge_calc = EdgeCalculator(bankroll=10.0)

    async with aiohttp.ClientSession(
        headers={"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}
    ) as session:
        scanner = MarketScanner.__new__(MarketScanner)
        scanner.db_path = db_path
        scanner._init_db()
        scanner.session = session

        fair_engine = FairValueEngine(session=session, anthropic_api_key=anthropic_key)

        # Check for resolved positions immediately on startup
        try:
            await check_resolved_positions(risk_mgr, session)
        except Exception as e:
            logger.debug(f"Startup resolution check: {e}")

        # Close any positions that exceed the 7-day horizon (new rule)
        try:
            await close_overdue_positions(executor, risk_mgr, session)
        except Exception as e:
            logger.warning(f"Overdue position close error: {e}")

        if once:
            await run_scan_cycle(scanner, fair_engine, edge_calc, risk_mgr, executor, paper)
            return

        # Launch background tasks
        tasks = [
            asyncio.create_task(order_followup_loop(executor, risk_mgr)),
            asyncio.create_task(midnight_reset_loop(risk_mgr)),
            asyncio.create_task(hourly_status_loop(risk_mgr)),
            asyncio.create_task(resolution_check_loop(risk_mgr, session)),
        ]

        # Main scan loop
        try:
            while True:
                try:
                    await run_scan_cycle(scanner, fair_engine, edge_calc, risk_mgr, executor, paper)
                except Exception as e:
                    logger.error(f"Scan cycle error: {e}", exc_info=True)

                logger.info(f"Next scan in {SCAN_INTERVAL_SECONDS // 60} minutes...")
                await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            print("\nBot stopped by user.")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Trading Bot")
    parser.add_argument(
        "--live", action="store_true",
        help="Enable live trading (default: paper mode)"
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Run a single scan cycle and exit"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging"
    )
    args = parser.parse_args()

    asyncio.run(main(
        paper=not args.live,
        once=args.once,
        verbose=args.verbose,
    ))
