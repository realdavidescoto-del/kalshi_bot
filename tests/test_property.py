import os
from decimal import Decimal

os.environ["KALSHI_TESTING"] = "1"

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from hypothesis import given, strategies as st
from hypothesis import assume

from execution.fee_tracker import FeeAccumulatorTracker
from safety.risk_manager import RiskManager


@given(
    price=st.decimals(min_value="0.0001", max_value="0.9999", places=4),
    quantity=st.decimals(min_value="0.01", max_value="10000.0", places=2),
)
def test_fee_accumulator_invariant(price, quantity):
    """Overpayment is always non-negative, rebates never exceed accumulated overpayments."""
    tracker = FeeAccumulatorTracker()
    result = tracker.record_fill(price, quantity, is_buy=True)

    assert result["overpayment"] >= Decimal("0")
    assert result["overpayment"] < Decimal("0.01")
    assert result["rebate_received"] in (Decimal("0"), Decimal("0.01"))
    assert tracker.accumulator >= Decimal("0")
    assert tracker.accumulator < Decimal("0.01")


@given(
    price=st.decimals(min_value="0.0001", max_value="0.9999", places=4),
    quantity=st.decimals(min_value="0.01", max_value="10000.0", places=2),
)
def test_fee_sell_no_negative_rebate(price, quantity):
    """Sell fills should not create negative overpayments."""
    tracker = FeeAccumulatorTracker()
    result = tracker.record_fill(price, quantity, is_buy=False)

    assert result["overpayment"] >= Decimal("0")
    assert result["rebate_received"] in (Decimal("0"), Decimal("0.01"))


@given(
    estimated_prob=st.decimals(min_value="0.01", max_value="0.99", places=4),
    market_price=st.decimals(min_value="0.01", max_value="0.99", places=4),
)
def test_kelly_fraction_bounds(estimated_prob, market_price):
    """Kelly fraction should always be between 0 and 1."""
    assume(estimated_prob != market_price)
    rm = RiskManager()

    kelly_yes = rm.calculate_kelly_fraction(estimated_prob, market_price, "yes")
    kelly_no = rm.calculate_kelly_fraction(
        estimated_prob, market_price, "no"
    )

    assert Decimal("0") <= kelly_yes <= Decimal("1")
    assert Decimal("0") <= kelly_no <= Decimal("1")


@given(
    estimated_prob=st.decimals(min_value="0.01", max_value="0.99", places=4),
    market_price=st.decimals(min_value="0.01", max_value="0.99", places=4),
    total_capital=st.decimals(min_value="100", max_value="1000000", places=2),
)
def test_wager_never_exceeds_var_limit(estimated_prob, market_price, total_capital):
    """Wager should never exceed VaR limit (2% of capital)."""
    assume(estimated_prob != market_price)
    rm = RiskManager()

    wager = rm.size_order(
        estimated_prob=estimated_prob,
        market_price=market_price,
        side="yes",
        sector="Economics",
        current_sector_exposure=Decimal("0"),
        total_capital=total_capital,
    )

    max_var_wager = total_capital * Decimal("0.02")
    assert wager <= max_var_wager


@given(
    estimated_prob=st.decimals(min_value="0.01", max_value="0.99", places=4),
    market_price=st.decimals(min_value="0.01", max_value="0.99", places=4),
    total_capital=st.decimals(min_value="100", max_value="1000000", places=2),
    current_exposure=st.decimals(min_value="0", max_value="100000", places=2),
)
def test_wager_respects_sector_limit(estimated_prob, market_price, total_capital, current_exposure):
    """Wager should never exceed the sector limit remaining room."""
    assume(estimated_prob != market_price)
    rm = RiskManager()

    wager = rm.size_order(
        estimated_prob=estimated_prob,
        market_price=market_price,
        side="yes",
        sector="Economics",
        current_sector_exposure=current_exposure,
        total_capital=total_capital,
    )

    max_sector = total_capital * Decimal("0.30")
    remaining = max(Decimal("0"), max_sector - current_exposure)
    assert wager <= remaining
