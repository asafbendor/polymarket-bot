"""
Edge Calculator - computes Kelly-sized position for each opportunity
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

MIN_EDGE = 0.08          # 8% minimum edge to trade
KELLY_FRACTION = 0.25    # fractional Kelly (25% of full Kelly)
MAX_POSITION_USD = 1.0   # hard cap per trade


@dataclass
class TradeOpportunity:
    condition_id: str
    question: str
    category: str
    direction: str            # "YES" or "NO"
    market_price: float       # current polymarket price for direction
    fair_value: float         # estimated true probability
    edge: float               # fair_value - market_price (signed)
    full_kelly: float
    fractional_kelly: float
    position_size: float      # USD to bet
    yes_token_id: Optional[str]
    no_token_id: Optional[str]
    hours_left: float
    end_date: str = ""
    slug: str = ""
    market_url: str = ""

    def __str__(self):
        sign = "+" if self.edge >= 0 else ""
        return (
            f"{self.question[:60]}\n"
            f"  {self.direction}: market={self.market_price:.0%}  "
            f"fair={self.fair_value:.0%}  edge={sign}{self.edge:.1%}\n"
            f"  Kelly={self.full_kelly:.1%} → adj={self.fractional_kelly:.1%}  "
            f"size=${self.position_size:.2f}"
        )


class EdgeCalculator:
    def __init__(self, bankroll: float = 10.0):
        self.bankroll = bankroll  # total daily budget

    def evaluate(
        self,
        market: dict,
        fair_value: float,
        daily_budget_remaining: float,
    ) -> Optional[TradeOpportunity]:
        """
        Returns a TradeOpportunity if edge exceeds threshold, else None.
        fair_value is probability of YES (0-1).
        """
        yes_price = market.get("yes_price", 0.5)
        no_price = market.get("no_price", 1 - yes_price)

        # Clamp prices to valid range
        yes_price = max(0.01, min(0.99, yes_price))
        no_price = max(0.01, min(0.99, no_price))
        fair_value = max(0.01, min(0.99, fair_value))

        # Edge for YES bet
        edge_yes = fair_value - yes_price
        # Edge for NO bet (true_prob_no = 1 - fair_value)
        edge_no = (1.0 - fair_value) - no_price

        # Pick best direction — always pick the POSITIVE edge (bet with the edge, not against it)
        if edge_yes >= edge_no:
            edge = edge_yes
            direction = "YES"
            market_price = yes_price
        else:
            edge = edge_no
            direction = "NO"
            market_price = no_price

        if abs(edge) < MIN_EDGE:
            logger.debug(
                f"Edge {edge:.1%} below threshold for: {market.get('question','')[:50]}"
            )
            return None

        # Kelly formula
        # For a binary bet at price p with true prob q:
        # Kelly fraction = (q - p) / (1 - p)   [when betting YES]
        # Kelly fraction = ((1-q) - (1-p)) / p = (p-q) / p  [when betting NO]
        if direction == "YES":
            fair_prob = fair_value
            odds_against = 1.0 / market_price  # decimal odds
            # Standard Kelly: f = (b*p - q) / b  where b = net odds, p = win prob, q = lose prob
            net_odds = (1.0 / market_price) - 1.0  # profit per $1 bet if win
            win_prob = fair_prob
            lose_prob = 1.0 - win_prob
        else:
            fair_prob = 1.0 - fair_value
            net_odds = (1.0 / market_price) - 1.0
            win_prob = fair_prob
            lose_prob = 1.0 - win_prob

        # Kelly fraction (as fraction of bankroll)
        if net_odds <= 0 or win_prob <= 0:
            return None

        full_kelly = max(0.0, (net_odds * win_prob - lose_prob) / net_odds)

        if full_kelly <= 0:
            return None

        fractional_kelly = full_kelly * KELLY_FRACTION

        # Position size in USD
        raw_size = fractional_kelly * self.bankroll
        position_size = round(min(raw_size, daily_budget_remaining, MAX_POSITION_USD), 2)

        if position_size < 0.50:  # minimum trade size
            logger.debug(f"Position size ${position_size:.2f} too small, skipping")
            return None

        return TradeOpportunity(
            condition_id=market["condition_id"],
            question=market.get("question", ""),
            category=market.get("category", "other"),
            direction=direction,
            market_price=market_price,
            fair_value=fair_value,
            edge=edge,
            full_kelly=full_kelly,
            fractional_kelly=fractional_kelly,
            position_size=position_size,
            yes_token_id=market.get("yes_token_id"),
            no_token_id=market.get("no_token_id"),
            hours_left=market.get("hours_left", 0),
            end_date=market.get("end_date", ""),
            slug=market.get("slug", ""),
            market_url=market.get("market_url", ""),
        )
