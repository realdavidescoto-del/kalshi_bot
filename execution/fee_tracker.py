import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

logger = logging.getLogger("kalshi_bot.fee_tracker")

class FeeAccumulatorTracker:
    def __init__(self):
        self.accumulator = Decimal("0.000000")
        self.total_overpayments = Decimal("0.000000")
        self.total_rebates_received = Decimal("0.000000")

    def reset(self):
        self.accumulator = Decimal("0.000000")
        self.total_overpayments = Decimal("0.000000")
        self.total_rebates_received = Decimal("0.000000")

    def record_fill(self, price: Decimal, quantity: Decimal, is_buy: bool = True) -> dict:
        """
        Calculates exact fill cost, rounded cost (whole-cents), and tracks overpayment.
        Triggers rebates when the accumulated overpayments reach or exceed $0.01.
        
        Returns:
            dict containing details of the calculation:
            - actual_value: exact price * quantity
            - rounded_value: whole-cent balance change
            - overpayment: difference added to the accumulator
            - rebate_received: rebate amount triggered ($0.01 increments)
            - new_accumulator_balance: new accumulator state
        """
        # Exact transaction value in dollars
        actual_value = price * quantity

        # Balance changes must be in whole cents
        actual_value_cents = actual_value * Decimal("100.0")

        if is_buy:
            # Buys are rounded up (more money deducted)
            rounded_cents = actual_value_cents.quantize(Decimal("1"), rounding=ROUND_CEILING)
            rounded_value = rounded_cents / Decimal("100.0")
            overpayment = rounded_value - actual_value
        else:
            # Sells are rounded down (less money received)
            rounded_cents = actual_value_cents.quantize(Decimal("1"), rounding=ROUND_FLOOR)
            rounded_value = rounded_cents / Decimal("100.0")
            overpayment = actual_value - rounded_value

        # Update accumulator
        self.accumulator += overpayment
        self.total_overpayments += overpayment

        # Check for rebates ($0.01 increments)
        rebate_received = Decimal("0.00")
        if self.accumulator >= Decimal("0.01"):
            # Number of whole-cent rebates to trigger
            cents_rebated = int(self.accumulator // Decimal("0.01"))
            rebate_received = Decimal(cents_rebated) * Decimal("0.01")

            self.accumulator -= rebate_received
            self.total_rebates_received += rebate_received

            logger.info(
                f"REBATE TRIGGERED: Received ${rebate_received:.2f} rebate. "
                f"Remaining accumulator balance: ${self.accumulator:.6f}"
            )

        return {
            "actual_value": actual_value,
            "rounded_value": rounded_value,
            "overpayment": overpayment,
            "rebate_received": rebate_received,
            "new_accumulator_balance": self.accumulator
        }
