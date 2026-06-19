import logging
from decimal import Decimal

from config import Config

logger = logging.getLogger("kalshi_bot.risk_manager")

class RiskManager:
    def __init__(self):
        Config.validate()
        self.var_limit = Config.MAX_VAR_LIMIT_PCT
        self.sector_limit = Config.MAX_SECTOR_LIMIT_PCT
        self.kelly_mult = Config.KELLY_MULTIPLIER

    def calculate_kelly_fraction(self, estimated_prob: Decimal, market_price: Decimal, side: str) -> Decimal:
        """
        Calculates the raw Kelly Criterion fraction of the portfolio to wager.
        Formula for binary contract:
          f* = (p * b - q) / b
        For YES contract at price P:
          b = (1 - P) / P
          f* = (p - P) / (1 - P)
        For NO contract (represented by buying NO at price 1 - P):
          P_no = 1 - P
          p_no = 1 - p
          b = P / (1 - P)
          f* = (P - p) / P
        """
        if market_price <= Decimal("0.0") or market_price >= Decimal("1.0"):
            return Decimal("0.0")

        if estimated_prob < Decimal("0.0") or estimated_prob > Decimal("1.0"):
            raise ValueError(f"Estimated probability {estimated_prob} must be between 0.0 and 1.0.")

        side_lower = side.lower()

        if side_lower == "yes":
            if estimated_prob <= market_price:
                return Decimal("0.0") # No edge/negative edge
            raw_kelly = (estimated_prob - market_price) / (Decimal("1.0") - market_price)
        elif side_lower == "no":
            # Market price of YES is P. Price of NO is 1 - P.
            # Estimated probability of YES is p. Prob of NO is 1 - p.
            no_price = Decimal("1.0") - market_price
            no_prob = Decimal("1.0") - estimated_prob
            if no_prob <= no_price:
                return Decimal("0.0") # No edge/negative edge
            raw_kelly = (no_prob - no_price) / (Decimal("1.0") - no_price)
            # Simplifies to: (market_price - estimated_prob) / market_price
        else:
            raise ValueError(f"Invalid side: {side}. Must be 'yes' or 'no'.")

        return max(Decimal("0.0"), raw_kelly)

    def get_position_size_fraction(self, estimated_prob: Decimal, market_price: Decimal, side: str) -> Decimal:
        """
        Calculates the final constrained portfolio fraction to wager using:
        1. Kelly Criterion
        2. Fractional Kelly constraint (0.25x)
        3. Value-at-Risk (VaR) position size limit (max 2% of total capital)
        """
        # 1. Raw Kelly
        raw_kelly = self.calculate_kelly_fraction(estimated_prob, market_price, side)

        # 2. Fractional Kelly (0.25x)
        fractional_kelly = raw_kelly * self.kelly_mult

        # 3. VaR Cap (max 2% of total capital per position)
        final_fraction = min(fractional_kelly, self.var_limit)

        logger.debug(
            f"Sizing: Raw Kelly={raw_kelly:.4f}, Fractional={fractional_kelly:.4f}, "
            f"Final Constrained={final_fraction:.4f} for side={side} @ price={market_price}"
        )
        return final_fraction

    def get_max_allowed_wager_for_sector(
        self,
        sector: str,
        current_sector_exposure: Decimal,
        total_capital: Decimal
    ) -> Decimal:
        """
        Enforces strict sector concentration limits, capping maximum portfolio exposure
        at 30% per sector. Returns the maximum additional wager allowed for the sector.
        """
        if total_capital <= Decimal("0.0"):
            return Decimal("0.0")

        max_sector_exposure = total_capital * self.sector_limit
        allowed_additional = max_sector_exposure - current_sector_exposure

        logger.debug(
            f"Sector {sector}: current_exposure=${current_sector_exposure:.2f}, "
            f"max_allowed=${max_sector_exposure:.2f}, allowed_additional=${allowed_additional:.2f}"
        )
        return max(Decimal("0.0"), allowed_additional)

    def size_order(
        self,
        estimated_prob: Decimal,
        market_price: Decimal,
        side: str,
        sector: str,
        current_sector_exposure: Decimal,
        total_capital: Decimal
    ) -> Decimal:
        """
        Determines the absolute dollar amount to wager on the position, taking into account
        the Kelly Criterion, VaR caps, and sector concentration limits.
        """
        if total_capital <= Decimal("0.0"):
            return Decimal("0.0")

        # 1. Determine size fraction based on Kelly + VaR cap
        fraction = self.get_position_size_fraction(estimated_prob, market_price, side)
        proposed_wager = total_capital * fraction

        # 2. Check sector concentration limits
        max_allowed_for_sector = self.get_max_allowed_wager_for_sector(
            sector,
            current_sector_exposure,
            total_capital
        )

        # 3. Take the minimum of proposed wager and allowed sector room
        final_wager = min(proposed_wager, max_allowed_for_sector)

        logger.info(
            f"Order Sizing for {side} in {sector}: Proposed=${proposed_wager:.2f}, "
            f"Sector Limit Limit=${max_allowed_for_sector:.2f} -> Final Wager=${final_wager:.2f}"
        )
        return final_wager
