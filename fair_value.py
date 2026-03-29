"""
Fair Value Engine - domain-specific probability estimation
Sources: Open-Meteo (weather), ESPN (sports), Binance (crypto), Claude AI (political)
"""

import asyncio
import aiohttp
import json
import logging
import re
from datetime import datetime, timezone
from typing import Optional
import os

logger = logging.getLogger(__name__)

_CLAUDE_SYSTEM = """You are a Senior Quantitative Analyst specializing in prediction markets. Your goal is to provide a calibrated probability for a specific event.

Instructions:
1. Base Rate Analysis: Identify the historical frequency of similar events.
2. Specific Evidence: Analyze the provided news/data points.
3. Inside vs. Outside View: Compare current specific factors against the general trend.
4. Counter-Argument: Explicitly consider 3 reasons why the event might NOT happen.
5. Calibration: Adjust your estimate based on the logic above.

Calibration anchors:
- Multi-team tournaments (16+ teams): winner probability ≈ 1/N adjusted for skill. Weak team in 32-team World Cup = 1-3%.
- Crypto extreme targets: BTC reaching 2x current price in 30 days ≈ 1-3%.
- The market price is one data point but can be wrong — trust data over market.

Output Format: Return ONLY a JSON object, no other text:
{"reasoning": "short_summary", "fair_value_percent": float, "confidence_score": 0.0-1.0}"""


class FairValueEngine:
    def __init__(self, session: aiohttp.ClientSession, anthropic_api_key: str = ""):
        self.session = session
        raw_key = anthropic_api_key or os.getenv("ANTHROPIC_API_KEY", "")
        self.anthropic_key = raw_key.strip().lstrip("=")
        # Limit concurrent Claude calls to avoid 429 rate limits
        self._claude_sem = asyncio.Semaphore(3)
        # Cache Claude results: condition_id -> (fair_value, timestamp)
        # Valid for 6 hours to avoid repeated expensive API calls
        self._claude_cache: dict = {}
        self._cache_ttl = 6 * 3600  # 6 hours in seconds

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------
    async def estimate(self, market: dict) -> Optional[float]:
        """
        Returns estimated fair probability (0-1) for YES outcome.
        Returns None if no estimate can be made.
        """
        category = market.get("category", "other")

        try:
            if category == "weather":
                return await self._weather_estimate(market)
            elif category == "sports":
                return await self._sports_estimate(market)
            elif category == "crypto":
                return await self._crypto_estimate(market)
            elif category in ("political", "economic"):
                return await self._political_estimate(market)
            else:
                # Fallback: use Claude for anything else
                return await self._claude_estimate(market, headlines=[])
        except Exception as e:
            logger.warning(f"Fair value error [{category}] {market.get('question','')[:50]}: {e}")
            return None

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------
    async def _get(self, url: str, params: dict = None, headers: dict = None) -> Optional[dict | list]:
        try:
            async with self.session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status == 200:
                    return await r.json(content_type=None)
                logger.debug(f"HTTP {r.status}: {url}")
        except Exception as e:
            logger.debug(f"GET error {url}: {e}")
        return None

    # ------------------------------------------------------------------
    # WEATHER — Open-Meteo (free, no key)
    # ------------------------------------------------------------------
    async def _weather_estimate(self, market: dict) -> Optional[float]:
        question = market.get("question", "")

        # Extract location clue — simplified heuristic
        city_coords = {
            "new york":    (40.71, -74.01),
            "los angeles": (34.05, -118.24),
            "chicago":     (41.88, -87.63),
            "houston":     (29.76, -95.37),
            "miami":       (25.77, -80.19),
            "london":      (51.51, -0.13),
            "paris":       (48.85, 2.35),
            "tokyo":       (35.68, 139.69),
        }
        lat, lon = 40.71, -74.01  # default NYC
        for city, coords in city_coords.items():
            if city in question.lower():
                lat, lon = coords
                break

        data = await self._get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max,precipitation_sum,weathercode",
                "forecast_days": 7,
                "timezone": "UTC",
            }
        )
        if not data or "daily" not in data:
            return None

        daily = data["daily"]
        temps = daily.get("temperature_2m_max", [])
        precip = daily.get("precipitation_sum", [])
        codes = daily.get("weathercode", [])

        # Detect what the market is asking
        q_lower = question.lower()

        # Rain/precipitation question
        if any(k in q_lower for k in ["rain", "precipitation", "snow", "storm"]):
            if precip:
                avg_precip = sum(p for p in precip[:3] if p is not None) / max(len(precip[:3]), 1)
                prob = min(avg_precip / 10.0, 0.95)  # normalize: 10mm = 95% chance
                return round(max(prob, 0.05), 3)

        # Temperature threshold question (e.g. "above 30°C" or "be 6°C")
        temp_match = re.search(r"(\d{1,3})\s*°?\s*(c|f)", q_lower)
        if temp_match and temps:
            threshold = float(temp_match.group(1))
            unit = temp_match.group(2)
            if unit == "f":
                threshold = (threshold - 32) * 5 / 9
            # Detect direction: "above/over/exceed" vs "below/under/be exactly"
            is_above_q = any(k in q_lower for k in ["above", "over", "exceed", "higher", "at least"])
            is_below_q = any(k in q_lower for k in ["below", "under", "less", "colder"])
            if is_above_q:
                count = sum(1 for t in temps[:3] if t is not None and t > threshold)
            elif is_below_q:
                count = sum(1 for t in temps[:3] if t is not None and t < threshold)
            else:
                # "be X°C" — check proximity (within 3°C)
                count = sum(1 for t in temps[:3] if t is not None and abs(t - threshold) <= 3)
            return round(count / max(len(temps[:3]), 1), 3)

        # Generic "bad weather" question
        if codes:
            bad_weather_codes = [61, 63, 65, 71, 73, 75, 77, 80, 81, 82, 85, 86, 95, 96, 99]
            bad_days = sum(1 for c in codes[:3] if c in bad_weather_codes)
            return round(bad_days / max(len(codes[:3]), 1), 3)

        return None

    # ------------------------------------------------------------------
    # SPORTS — ESPN unofficial API
    # ------------------------------------------------------------------
    async def _sports_estimate(self, market: dict) -> Optional[float]:
        question = market.get("question", "").lower()

        # Detect sport
        sport_path = None
        if any(k in question for k in ["nba", "lakers", "celtics", "warriors", "heat", "knicks",
                                         "basketball"]):
            sport_path = "basketball/nba"
        elif any(k in question for k in ["nfl", "super bowl", "patriots", "cowboys", "chiefs",
                                          "eagles", "football"]):
            sport_path = "football/nfl"
        elif any(k in question for k in ["mlb", "yankees", "dodgers", "red sox", "baseball"]):
            sport_path = "baseball/mlb"
        elif any(k in question for k in ["nhl", "hockey", "stanley cup"]):
            sport_path = "hockey/nhl"
        elif any(k in question for k in ["soccer", "world cup", "premier league", "mls", "champions"]):
            sport_path = "soccer/usa.1"

        if not sport_path:
            return await self._claude_estimate(market, [])

        # Fetch scoreboard / upcoming games
        data = await self._get(
            f"https://site.api.espn.com/apis/site/v2/sports/{sport_path}/scoreboard"
        )
        if not data:
            return await self._claude_estimate(market, [])

        events = data.get("events", [])
        if not events:
            return await self._claude_estimate(market, [])

        # Find relevant event
        best_event = None
        best_score = 0
        for event in events[:20]:
            event_name = (event.get("name") or event.get("shortName") or "").lower()
            # Score overlap of words in question vs event name
            q_words = set(question.split())
            e_words = set(event_name.split())
            overlap = len(q_words & e_words)
            if overlap > best_score:
                best_score = overlap
                best_event = event

        if not best_event or best_score < 1:
            return await self._claude_estimate(market, [])

        # Extract win probability if available
        competitions = best_event.get("competitions", [{}])
        if competitions:
            comp = competitions[0]
            competitors = comp.get("competitors", [])
            for comp_team in competitors:
                stats = comp_team.get("statistics", [])
                for stat in stats:
                    if stat.get("name") == "winProbability":
                        try:
                            prob = float(stat["value"]) / 100.0
                            # Determine if this is the "home" team in the question
                            team_home = comp_team.get("homeAway") == "home"
                            q_wants_home = any(k in question for k in ["home", "host"])
                            if q_wants_home == team_home:
                                return round(prob, 3)
                        except (ValueError, KeyError):
                            pass

        # Fallback: use odds if present
        odds = comp.get("odds", [{}])
        if odds and isinstance(odds, list) and odds[0]:
            details = odds[0].get("details", "")
            # e.g. "LAL -3.5"
            spread_match = re.search(r"([A-Z]{2,5})\s+([+-]\d+\.?\d*)", details)
            if spread_match:
                spread = float(spread_match.group(2))
                # Rough conversion: spread to win prob
                # approx: each point ≈ 3% in win prob, centered at 50%
                prob = 0.5 + (spread / -35.0)
                return round(max(0.05, min(0.95, prob)), 3)

        return await self._claude_estimate(market, [])

    # ------------------------------------------------------------------
    # CRYPTO — Binance REST (WebSocket would be for streaming; REST is fine here)
    # ------------------------------------------------------------------
    async def _crypto_estimate(self, market: dict) -> Optional[float]:
        question = market.get("question", "").lower()

        # Detect asset
        symbol = "BTCUSDT"
        if "eth" in question or "ethereum" in question:
            symbol = "ETHUSDT"
        elif "sol" in question or "solana" in question:
            symbol = "SOLUSDT"
        elif "bnb" in question:
            symbol = "BNBUSDT"
        elif "xrp" in question or "ripple" in question:
            symbol = "XRPUSDT"

        # Current price
        ticker = await self._get(
            "https://api.binance.com/api/v3/ticker/24hr",
            params={"symbol": symbol}
        )
        if not ticker:
            return await self._claude_estimate(market, [])

        current_price = float(ticker.get("lastPrice", 0))
        price_change_pct = float(ticker.get("priceChangePercent", 0))

        if current_price <= 0:
            return await self._claude_estimate(market, [])

        # Extract price threshold from question
        # e.g. "Bitcoin above $70,000", "BTC > 80k"
        price_match = re.search(
            r"\$?([\d,]+(?:\.\d+)?)\s*(k|K|thousand|million|M)?",
            market.get("question", "")
        )
        if not price_match:
            return await self._claude_estimate(market, [])

        target_raw = float(price_match.group(1).replace(",", ""))
        multiplier_str = price_match.group(2) or ""
        if multiplier_str.lower() in ["k", "thousand"]:
            target_raw *= 1000
        elif multiplier_str.lower() in ["m", "million"]:
            target_raw *= 1_000_000

        target_price = target_raw

        # Determine direction
        is_above = any(k in question for k in [">", "above", "over", "exceed", "higher", "break", "reach", "hit"])
        is_below = any(k in question for k in ["<", "below", "under", "drop", "lower", "fall", "dip", "crash", "less than"])

        # If current price >> target, "reach" means going DOWN (e.g. BTC reach $30k when at $80k)
        if is_above and not is_below and current_price > target_price * 1.5:
            is_above = False
            is_below = True

        if not is_above and not is_below:
            # Default: above
            is_above = True

        # Simple lognormal approximation
        # Based on current price, daily volatility (from 24h range), and time to expiry
        high_24h = float(ticker.get("highPrice", current_price))
        low_24h = float(ticker.get("lowPrice", current_price))
        daily_vol = (high_24h - low_24h) / (current_price * 2) if current_price > 0 else 0.05

        hours_left = market.get("hours_left", 24)
        days_left = hours_left / 24.0

        # Ratio of current to target
        ratio = current_price / target_price if target_price > 0 else 1.0
        import math
        log_ratio = math.log(ratio)
        sigma = daily_vol * math.sqrt(max(days_left, 0.04))

        # Normal CDF approximation (simple)
        def norm_cdf(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        z = log_ratio / sigma if sigma > 0 else 0
        prob_above = norm_cdf(z)

        # Add momentum bias (recent 24h trend)
        momentum_adj = price_change_pct * 0.003  # small weight
        prob_above = max(0.03, min(0.97, prob_above + momentum_adj))

        final_prob = prob_above if is_above else (1.0 - prob_above)
        return round(final_prob, 3)

    # ------------------------------------------------------------------
    # POLITICAL / OTHER — Claude AI
    # ------------------------------------------------------------------
    async def _political_estimate(self, market: dict) -> Optional[float]:
        question = market.get("question", "")
        newsapi_key = os.getenv("NEWSAPI_KEY", "")
        headlines = []

        if newsapi_key:
            # Fetch relevant headlines
            # Extract key terms from question (first 5 words)
            key_terms = " ".join(question.split()[:5])
            news_data = await self._get(
                "https://newsapi.org/v2/everything",
                params={
                    "q": key_terms,
                    "sortBy": "publishedAt",
                    "pageSize": 10,
                    "language": "en",
                    "apiKey": newsapi_key,
                }
            )
            if news_data and "articles" in news_data:
                headlines = [
                    a.get("title", "") for a in news_data["articles"][:10]
                    if a.get("title")
                ]
        else:
            # Try GNews (free, no key needed for basic)
            key_terms = " ".join(question.split()[:4])
            gnews_data = await self._get(
                "https://gnews.io/api/v4/search",
                params={
                    "q": key_terms,
                    "lang": "en",
                    "max": 5,
                    "token": os.getenv("GNEWS_KEY", ""),
                }
            )
            if gnews_data and "articles" in gnews_data:
                headlines = [a.get("title", "") for a in gnews_data["articles"][:5]]

        return await self._claude_estimate(market, headlines)

    # ------------------------------------------------------------------
    # Claude AI fallback estimator
    # ------------------------------------------------------------------
    async def _claude_estimate(self, market: dict, headlines: list[str]) -> Optional[float]:
        if not self.anthropic_key:
            logger.debug("No Anthropic API key — skipping Claude estimate")
            return None

        # Cache check — skip Claude if estimated within last 6 hours
        import time as _time
        cid = market.get("condition_id", market.get("question", ""))
        cached = self._claude_cache.get(cid)
        if cached:
            val, ts = cached
            if _time.time() - ts < self._cache_ttl:
                return val
            del self._claude_cache[cid]

        question = market.get("question", "")
        current_price = market.get("yes_price", 0.5)

        headline_block = ""
        if headlines:
            headline_block = "Recent headlines:\n" + "\n".join(f"- {h}" for h in headlines[:10]) + "\n\n"

        prompt = (
            f"{headline_block}"
            f"Polymarket question: \"{question}\"\n"
            f"Current market price (YES): {current_price:.0%}\n\n"
            f"Estimate the true probability of YES. Return ONLY the JSON object."
        )

        payload = {
            "model": "claude-sonnet-4-6",
            "max_tokens": 300,
            "system": _CLAUDE_SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with self._claude_sem:
            for attempt in range(3):
                try:
                    async with self.session.post(
                        "https://api.anthropic.com/v1/messages",
                        json=payload,
                        headers={
                            "x-api-key": self.anthropic_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        timeout=aiohttp.ClientTimeout(total=20),
                    ) as r:
                        if r.status == 429:
                            wait = 2 ** attempt  # 1s, 2s, 4s
                            logger.debug(f"Claude 429 rate limit, retry in {wait}s")
                            await asyncio.sleep(wait)
                            continue
                        if r.status != 200:
                            body = await r.text()
                            logger.warning(f"Claude API error: {r.status} | {body[:200]}")
                            return None
                        resp = await r.json(content_type=None)
                        text = resp["content"][0]["text"].strip()
                        # Try JSON parse first
                        try:
                            import json as _json
                            data = _json.loads(text)
                            confidence = float(data.get("confidence_score", 1.0))
                            if confidence < 0.5:
                                logger.debug(f"Claude low confidence ({confidence:.2f}), skipping")
                                return None
                            val = float(data["fair_value_percent"])
                            result = round(max(1, min(99, val)) / 100.0, 3)
                            self._claude_cache[cid] = (result, _time.time())
                            return result
                        except Exception:
                            # Fallback: extract any number from text
                            num_match = re.search(r"\d+\.?\d*", text)
                            if num_match:
                                val = float(num_match.group())
                                result = round(max(1, min(99, val)) / 100.0, 3)
                                self._claude_cache[cid] = (result, _time.time())
                                return result
                        return None
                except Exception as e:
                    if attempt < 2:
                        wait = 2 ** attempt
                        logger.debug(f"Claude error (retry {attempt+1}/3): {e}")
                        await asyncio.sleep(wait)
                        continue
                    logger.warning(f"Claude estimate error: {e}")
                    return None

        return None
