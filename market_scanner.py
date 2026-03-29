"""
Market Scanner - fetches and filters Polymarket markets
Connects to CLOB API + Gamma API
"""

import asyncio
import aiohttp
import sqlite3
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional
import time

logger = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

MIN_LIQUIDITY = 500       # USD
MIN_HOURS_TO_RESOLUTION = 6
SCAN_INTERVAL_SECONDS = 900  # 15 minutes


class MarketScanner:
    def __init__(self, db_path: str = "trades.db"):
        self.db_path = db_path
        self.session: Optional[aiohttp.ClientSession] = None
        self._init_db()

    # ------------------------------------------------------------------
    # DB
    # ------------------------------------------------------------------
    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                condition_id    TEXT PRIMARY KEY,
                question        TEXT,
                category        TEXT,
                end_date        TEXT,
                yes_price       REAL,
                no_price        REAL,
                volume          REAL,
                liquidity       REAL,
                last_scanned    TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _upsert_market(self, market: dict):
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("""
            INSERT OR REPLACE INTO markets
            (condition_id, question, category, end_date,
             yes_price, no_price, volume, liquidity, last_scanned)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            market["condition_id"],
            market["question"],
            market.get("category", ""),
            market.get("end_date", ""),
            market.get("yes_price", 0.5),
            market.get("no_price", 0.5),
            market.get("volume", 0),
            market.get("liquidity", 0),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # API helpers
    # ------------------------------------------------------------------
    async def _get(self, url: str, params: dict = None) -> Optional[dict | list]:
        try:
            async with self.session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                logger.warning(f"HTTP {r.status} from {url}")
        except asyncio.TimeoutError:
            logger.warning(f"Timeout: {url}")
        except Exception as e:
            logger.warning(f"Request error {url}: {e}")
        return None

    # ------------------------------------------------------------------
    # Gamma API — rich metadata (category, end_date, volume, liquidity)
    # ------------------------------------------------------------------
    async def _fetch_gamma_markets(self, offset: int = 0, limit: int = 100) -> list[dict]:
        data = await self._get(
            f"{GAMMA_BASE}/markets",
            params={
                "active": "true",
                "closed": "false",
                "offset": offset,
                "limit": limit,
                "order": "volume24hr",
                "ascending": "false",
            },
        )
        if not data:
            return []
        # Gamma returns a list directly
        if isinstance(data, list):
            return data
        # Sometimes wrapped
        return data.get("markets", data.get("data", []))

    # ------------------------------------------------------------------
    # CLOB API — real-time orderbook prices
    # ------------------------------------------------------------------
    async def _fetch_clob_price(self, token_id: str) -> Optional[float]:
        """Returns mid-price for a token (YES token)."""
        data = await self._get(f"{CLOB_BASE}/midpoint", params={"token_id": token_id})
        if data and "mid" in data:
            try:
                return float(data["mid"])
            except (ValueError, TypeError):
                pass
        return None

    async def _fetch_clob_book(self, token_id: str) -> Optional[dict]:
        """Returns best bid/ask for a YES token."""
        data = await self._get(f"{CLOB_BASE}/book", params={"token_id": token_id})
        return data

    # ------------------------------------------------------------------
    # Parse helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_end_date(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ",
                    "%Y-%m-%dT%H:%M:%S+00:00", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[:26], fmt[:len(fmt)])
                return dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        return None

    @staticmethod
    def _classify_category(question: str, tags: list) -> str:
        q = question.lower()
        t = " ".join(tags).lower() if tags else ""
        combined = q + " " + t

        if any(k in combined for k in ["bitcoin", "btc", "eth", "crypto", "price"]):
            return "crypto"
        if any(k in combined for k in ["nba", "nfl", "nhl", "mlb", "soccer", "football",
                                         "basketball", "championship", "playoff", "win tonight",
                                         "super bowl", "world cup", "match"]):
            return "sports"
        if any(k in combined for k in ["weather", "temperature", "rain", "snow", "hurricane",
                                         "storm", "celsius", "fahrenheit"]):
            return "weather"
        if any(k in combined for k in ["gdp", "inflation", "fed", "interest rate", "economic",
                                         "unemployment", "cpi", "recession", "tariff", "trade war"]):
            return "economic"
        if any(k in combined for k in ["election", "president", "senate", "congress", "vote",
                                         "poll", "minister", "parliament", "political"]):
            return "political"
        return "other"

    # ------------------------------------------------------------------
    # Main scan
    # ------------------------------------------------------------------
    async def scan_all_markets(self) -> list[dict]:
        """Fetch up to 1000 markets, filter, return qualifying ones."""
        all_raw = []
        batch = 100
        max_markets = 1000

        # Page through Gamma API
        for offset in range(0, max_markets, batch):
            chunk = await self._fetch_gamma_markets(offset=offset, limit=batch)
            if not chunk:
                break
            all_raw.extend(chunk)
            if len(chunk) < batch:
                break  # no more pages
            await asyncio.sleep(0.2)  # polite pacing

        logger.info(f"Raw markets fetched: {len(all_raw)}")

        now = datetime.now(timezone.utc)
        qualified = []
        skip_no_date = skip_time = skip_liq = skip_err = 0

        for raw in all_raw:
            try:
                # --- end date check ---
                end_raw = raw.get("endDate") or raw.get("end_date_iso") or raw.get("endDateIso") or ""
                end_dt = self._parse_end_date(end_raw)
                if not end_dt:
                    skip_no_date += 1
                    continue
                hours_left = (end_dt - now).total_seconds() / 3600
                if hours_left < MIN_HOURS_TO_RESOLUTION:
                    skip_time += 1
                    continue

                # --- liquidity check ---
                liquidity = float(raw.get("liquidityNum") or raw.get("liquidity") or 0)
                volume = float(raw.get("volume") or raw.get("volumeNum") or 0)
                if liquidity < MIN_LIQUIDITY:
                    skip_liq += 1
                    continue

                # --- extract tokens (YES/NO) ---
                yes_token_id = None
                no_token_id = None
                yes_price = 0.5
                no_price = 0.5

                # Gamma API format: outcomePrices + outcomes + clobTokenIds
                # outcomePrices may arrive as JSON string '["0.02","0.98"]'
                import json as _json
                _op = raw.get("outcomePrices") or []
                if isinstance(_op, str):
                    _op = _json.loads(_op)
                outcome_prices = _op

                _on = raw.get("outcomes") or []
                if isinstance(_on, str):
                    _on = _json.loads(_on)
                outcome_names = _on

                _ct = raw.get("clobTokenIds") or []
                if isinstance(_ct, str):
                    _ct = _json.loads(_ct)
                clob_token_ids = _ct

                if outcome_prices and outcome_names:
                    for i, name in enumerate(outcome_names):
                        name_l = str(name).lower()
                        price = float(outcome_prices[i]) if i < len(outcome_prices) else 0.5
                        token_id = clob_token_ids[i] if i < len(clob_token_ids) else None
                        if isinstance(token_id, str):
                            # Pattern is always '<garbage>=<real_value>' e.g. '\t=0x168...'
                            if '=' in token_id:
                                token_id = token_id.split('=', 1)[-1]
                            token_id = token_id.strip()
                        if name_l == "yes":
                            yes_price = price
                            yes_token_id = token_id
                        elif name_l == "no":
                            no_price = price
                            no_token_id = token_id
                else:
                    # Fallback: CLOB tokens format
                    tokens = raw.get("tokens") or []
                    if tokens and isinstance(tokens[0], dict):
                        for tok in tokens:
                            outcome = str(tok.get("outcome", "")).lower()
                            tid = tok.get("token_id") or tok.get("tokenId") or ""
                            if isinstance(tid, str):
                                import re as _re2
                                m2 = _re2.search(r'0x[0-9a-fA-F]+|\d{10,}', tid)
                                tid = m2.group(0) if m2 else tid.strip()
                            if outcome == "yes":
                                yes_token_id = tid
                                yes_price = float(tok.get("price", 0.5))
                            elif outcome == "no":
                                no_token_id = tid
                                no_price = float(tok.get("price", 0.5))

                # Sanity check: prices should sum to ~1
                if abs(yes_price + no_price - 1.0) > 0.15:
                    yes_price = 0.5
                    no_price = 0.5

                # Skip markets without token IDs — can't place orders
                if not yes_token_id or not no_token_id:
                    skip_err += 1
                    continue

                question = raw.get("question") or raw.get("title") or ""
                tags = raw.get("tags") or []
                if isinstance(tags, list) and tags and isinstance(tags[0], dict):
                    tags = [t.get("label", "") for t in tags]

                condition_id = raw.get("conditionId") or raw.get("condition_id") or raw.get("id") or ""
                # Build Polymarket URL: /event/<event_slug>/<market_slug>
                market_slug = raw.get("slug") or ""
                events = raw.get("events") or []
                event_slug = events[0].get("slug", "") if events else ""
                slug = event_slug or market_slug
                if event_slug and market_slug:
                    market_url = f"https://polymarket.com/event/{event_slug}/{market_slug}"
                elif slug:
                    market_url = f"https://polymarket.com/event/{slug}"
                else:
                    market_url = ""

                market = {
                    "condition_id": condition_id,
                    "question": question,
                    "category": self._classify_category(question, tags),
                    "end_date": end_raw,
                    "slug": slug,
                    "market_url": market_url,
                    "hours_left": round(hours_left, 1),
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "volume": volume,
                    "liquidity": liquidity,
                    "yes_token_id": yes_token_id,
                    "no_token_id": no_token_id,
                    "raw": raw,
                }

                qualified.append(market)
                self._upsert_market(market)

            except Exception as e:
                skip_err += 1
                logger.warning(f"Parse error: {e} | market: {raw.get('question','?')[:40]}")
                continue

        logger.info(f"Qualified: {len(qualified)} | skipped: no_date={skip_no_date} too_soon={skip_time} low_liq={skip_liq} errors={skip_err}")

        # Improvement 1: enrich markets that still have default 0.5 price with real CLOB midpoints
        qualified = await self._enrich_with_clob_prices(qualified)

        return qualified

    async def _enrich_with_clob_prices(self, markets: list[dict]) -> list[dict]:
        """
        For markets where price is still the 0.5 default (Gamma didn't return a price),
        fetch real midpoint prices from CLOB API concurrently.
        """
        needs_price = [
            m for m in markets
            if m.get("yes_token_id") and abs(m["yes_price"] - 0.5) < 0.005
        ]

        if not needs_price:
            return markets

        logger.info(f"Fetching CLOB prices for {len(needs_price)} markets without real prices...")

        # Cap concurrency to be polite to the API
        semaphore = asyncio.Semaphore(15)

        async def fetch_one(market: dict):
            async with semaphore:
                price = await self._fetch_clob_price(market["yes_token_id"])
                if price is not None and 0.01 <= price <= 0.99:
                    market["yes_price"] = round(price, 4)
                    market["no_price"] = round(1.0 - price, 4)
                    market["price_source"] = "clob_midpoint"

        await asyncio.gather(*[fetch_one(m) for m in needs_price])

        enriched = sum(1 for m in needs_price if m.get("price_source") == "clob_midpoint")
        logger.info(f"CLOB price enrichment: {enriched}/{len(needs_price)} markets updated")

        # Re-upsert updated prices to DB
        for m in needs_price:
            if m.get("price_source") == "clob_midpoint":
                self._upsert_market(m)

        return markets

    # ------------------------------------------------------------------
    # Async session lifecycle
    # ------------------------------------------------------------------
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Accept": "application/json", "User-Agent": "polymarket-bot/1.0"}
        )
        return self

    async def __aexit__(self, *_):
        if self.session:
            await self.session.close()


# ------------------------------------------------------------------
# Quick test
# ------------------------------------------------------------------
async def _test():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    async with MarketScanner() as scanner:
        markets = await scanner.scan_all_markets()
        print(f"\nFound {len(markets)} qualifying markets")
        for m in markets[:5]:
            print(f"  [{m['category']:10}] {m['question'][:70]}")
            print(f"           YES={m['yes_price']:.2f}  liq=${m['liquidity']:,.0f}  {m['hours_left']:.0f}h left")


if __name__ == "__main__":
    asyncio.run(_test())
