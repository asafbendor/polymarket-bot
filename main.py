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

load_dotenv()  # load .env if present, but Railway env vars take priority

from market_scanner import MarketScanner, SCAN_INTERVAL_SECONDS
from fair_value import FairValueEngine
from edge_calculator import EdgeCalculator
from risk_manager import RiskManager
from executor import Executor, ORDER_CHECK_INTERVAL

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
class DbLogHandler(logging.Handler):
    """Writes log records to bot_log table in DB (for dashboard)."""
    def emit(self, record):
        try:
            import db_adapter
            msg = self.format(record)
            conn = db_adapter.connect()
            c = conn.cursor()
            c.execute(db_adapter.adapt(
                "INSERT INTO bot_log (timestamp, level, message) VALUES (?,?,?)"
            ), (datetime.now(timezone.utc).isoformat(), record.levelname, msg[:500]))
            # Keep only last 200 rows
            c.execute(db_adapter.adapt(
                "DELETE FROM bot_log WHERE id NOT IN (SELECT id FROM bot_log ORDER BY id DESC LIMIT 200)"
            ))
            conn.commit()
            conn.close()
        except Exception:
            pass  # never let logging crash the bot


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
    for lib in ["aiohttp", "asyncio"]:
        logging.getLogger(lib).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Pending orders tracker (order_id -> trade info)
# ------------------------------------------------------------------
pending_orders: dict[str, dict] = {}


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
    if budget_check < 0.50 or positions_check >= 3:
        print(f"[{now_str}] Skipping analysis — budget=${budget_check:.2f}, positions={positions_check}/3")
        return

    # Phase 2 (Improvement 3): Estimate fair value for ALL markets concurrently,
    # then sort by edge so we always trade the best opportunities first.
    #
    # Claude is only called for "other/political" markets — cap that to top 100
    # by liquidity to avoid blowing rate limits on 700+ requests.
    MAX_CLAUDE_MARKETS = 100
    non_claude_categories = {"crypto", "sports", "weather"}

    # Sort: non-Claude categories first, then by liquidity desc
    def sort_key(m):
        is_fast = m.get("category") in non_claude_categories
        return (0 if is_fast else 1, -m.get("liquidity", 0))

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

    # Print near-misses
    for question, mkt_p, fair_value, direction, edge in near_misses[:5]:
        print(
            f"[{now_str}] {question[:55]}: "
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
        print(
            f"[{now_str}] {opp.question[:55]}: "
            f"market={opp.market_price:.0%} fair={opp.fair_value:.0%} "
            f"{opp.direction} edge={sign}{opp.edge:.0%} >> TRADING"
        )

        # Risk manager approval
        approved, reason = risk_mgr.approve(opp)
        if not approved:
            print(f"[{now_str}] REJECTED: {reason}")
            risk_mgr.log_rejection(opp, reason)
            continue

        # Execute
        result = await executor.execute(opp)
        limit_price = result.get("limit_price", opp.fair_value)

        print(
            f"[{now_str}] {mode_tag} Position: ${opp.position_size:.2f} {opp.direction} "
            f"@ {limit_price:.3f} limit | "
            f"Kelly: {opp.full_kelly:.1%} → adj: {opp.fractional_kelly:.1%}"
        )

        # Log to DB
        trade_id = risk_mgr.log_trade(
            opp,
            limit_price=limit_price,
            order_id=result.get("order_id", ""),
            paper=paper,
            reason=result.get("message", ""),
        )

        if result.get("order_id"):
            pending_orders[result["order_id"]] = {
                "trade_id": trade_id,
                "opp": opp,
                "placed_at": asyncio.get_event_loop().time(),
            }

        trades_attempted += 1
        await asyncio.sleep(0.5)

    # Summary
    print(
        f"[{now_str}] Scanned {total_markets} markets | "
        f"{opportunities_found} opportunities found | "
        f"{trades_attempted} trades placed"
    )
    print(f"[{now_str}] {risk_mgr.status_line()}")
    print()


# ------------------------------------------------------------------
# Order follow-up loop
# ------------------------------------------------------------------
async def order_followup_loop(executor: Executor, risk_mgr: RiskManager):
    """Check pending orders every ORDER_CHECK_INTERVAL seconds."""
    while True:
        await asyncio.sleep(ORDER_CHECK_INTERVAL)

        to_remove = []
        for order_id, info in list(pending_orders.items()):
            result = await executor.check_order_status(order_id)
            status = result.get("status", "unknown")

            if status in ("filled", "MATCHED"):
                fill_price = result.get("fill_price", 0.0)
                opp = info["opp"]
                # Rough P&L estimate (actual P&L only known at resolution)
                logger.info(f"Order filled: {order_id} @ {fill_price:.3f}")
                risk_mgr.update_trade_status(order_id, "filled", fill_price)
                to_remove.append(order_id)

            elif status in ("cancelled", "CANCELLED", "UNMATCHED"):
                logger.info(f"Order cancelled/expired: {order_id}")
                risk_mgr.update_trade_status(order_id, "cancelled")
                to_remove.append(order_id)

            else:
                # Still open — cancel if too old
                age = asyncio.get_event_loop().time() - info.get("placed_at", 0)
                if age > ORDER_CHECK_INTERVAL * 2:
                    logger.info(f"Order too old, cancelling: {order_id}")
                    await executor.cancel_order(order_id)
                    risk_mgr.update_trade_status(order_id, "cancelled")
                    to_remove.append(order_id)

        for oid in to_remove:
            pending_orders.pop(oid, None)


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

    # Startup diagnostics
    if anthropic_key:
        masked = anthropic_key[:10] + "..." + anthropic_key[-4:]
        print(f"  Claude API key: {masked} (length={len(anthropic_key)})")
    else:
        print("  WARNING: ANTHROPIC_API_KEY is empty! Claude estimates will fail.")
    print(f"  All env vars with 'ANTHROPIC': {[k for k in os.environ if 'ANTHROPIC' in k.upper()]}")

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

        if once:
            await run_scan_cycle(scanner, fair_engine, edge_calc, risk_mgr, executor, paper)
            return

        # Launch background tasks
        tasks = [
            asyncio.create_task(order_followup_loop(executor, risk_mgr)),
            asyncio.create_task(midnight_reset_loop(risk_mgr)),
            asyncio.create_task(hourly_status_loop(risk_mgr)),
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
