import os

os.environ["KALSHI_TESTING"] = "1"

import sys
from decimal import Decimal

import pytest

# Add parent directory to path to enable local imports
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
from data.database import get_connection, initialize_db, log_release, log_shadow_trade
from execution.engine import ExecutionEngine
from execution.fee_tracker import FeeAccumulatorTracker
from market.order_book import LocalOrderBook
from safety.risk_manager import RiskManager
from strategy.macro_tracker import MockCalendarProvider


def test_fixed_point_formatting():
    """
    Test Phase 3 Execution payload requirements.
    Prices must have 4 decimal places, quantities 2 decimal places.
    """
    engine = ExecutionEngine()

    assert engine.format_price(Decimal("0.65")) == "0.6500"
    assert engine.format_price(Decimal("0.655523")) == "0.6555"
    assert engine.format_price(Decimal("1")) == "1.0000"

    assert engine.format_quantity(Decimal("10")) == "10"
    assert engine.format_quantity(Decimal("10.556")) == "11"
    assert engine.format_quantity(Decimal("0.25")) == "0"


def test_prod_ws_url_uses_supported_endpoint():
    """
    Production websocket connections should use the supported elections endpoint.
    """
    original_env = Config.ENV
    Config.ENV = "prod"
    try:
        assert Config.get_ws_url() == f"wss://api.elections.kalshi.com/trade-api/ws/{Config.API_VERSION}"
    finally:
        Config.ENV = original_env


def test_fee_accumulator_and_rebates():
    """
    Test Phase 3 Fee rounding accumulator and rebate rules.
    Accumulated overpayments must trigger a whole-cent rebate once they reach $0.01.
    """
    tracker = FeeAccumulatorTracker()

    # 1. Buy: Actual cost 6.915525. Rounded to ceiling = 6.92.
    # Overpayment = 6.92 - 6.915525 = 0.004475
    res1 = tracker.record_fill(Decimal("0.6555"), Decimal("10.55"), is_buy=True)
    assert res1["actual_value"] == Decimal("6.915525")
    assert res1["rounded_value"] == Decimal("6.92")
    assert res1["overpayment"] == Decimal("0.004475")
    assert res1["rebate_received"] == Decimal("0.00")
    assert tracker.accumulator == Decimal("0.004475")

    # 2. Buy: Actual cost 6.915525. Same fill, adds 0.004475 to accumulator.
    # Total accumulator = 0.004475 + 0.004475 = 0.008950 (still below 0.01)
    res2 = tracker.record_fill(Decimal("0.6555"), Decimal("10.55"), is_buy=True)
    assert res2["rebate_received"] == Decimal("0.00")
    assert tracker.accumulator == Decimal("0.008950")

    # 3. Buy: Actual cost 6.915525. Adds 0.004475.
    # Total before rebate = 0.008950 + 0.004475 = 0.013425.
    # Reaches >= 0.01, so a $0.01 rebate is triggered.
    # Remaining accumulator = 0.013425 - 0.01 = 0.003425.
    res3 = tracker.record_fill(Decimal("0.6555"), Decimal("10.55"), is_buy=True)
    assert res3["rebate_received"] == Decimal("0.01")
    assert tracker.accumulator == Decimal("0.003425")
    assert tracker.total_rebates_received == Decimal("0.01")


def test_risk_manager_kelly_sizing():
    """
    Test Phase 4 Kelly Criterion calculations and Fractional/VaR caps.
    """
    rm = RiskManager()

    # YES calculation: P = 0.60, p = 0.70. b = (1-0.6)/0.6 = 0.6667
    # Raw Kelly = (p - P) / (1 - P) = (0.7 - 0.6) / 0.4 = 0.25
    raw_kelly_yes = rm.calculate_kelly_fraction(Decimal("0.70"), Decimal("0.60"), "yes")
    assert pytest.approx(float(raw_kelly_yes)) == 0.25

    # NO calculation: P_yes = 0.60, p_yes = 0.50 (so NO prob is 0.50, NO price is 0.40)
    # Raw Kelly NO = (0.50 - 0.40) / 0.60 = 1/6 = 0.1667
    raw_kelly_no = rm.calculate_kelly_fraction(Decimal("0.50"), Decimal("0.60"), "no")
    assert pytest.approx(float(raw_kelly_no)) == 1 / 6

    # Underpriced YES (P=0.60, p=0.55 -> prob < price, Kelly YES should be 0.0)
    assert rm.calculate_kelly_fraction(Decimal("0.55"), Decimal("0.60"), "yes") == Decimal("0")

    # Constrained position sizing: Quarter Kelly (0.25x) and 2% VaR limit
    # For YES raw_kelly = 0.25, Fractional Kelly = 0.25 * 0.25 = 0.0625.
    # Capped at 2% VaR limit -> 0.02.
    final_fraction_yes = rm.get_position_size_fraction(Decimal("0.70"), Decimal("0.60"), "yes")
    assert final_fraction_yes == Decimal("0.02")


def test_sector_concentration():
    """
    Test Phase 4 Sector Concentration limits.
    Max exposure per sector is capped at 30% of total capital.
    """
    rm = RiskManager()
    total_capital = Decimal("10000.00")

    # Cap is 30% * 10000 = $3000
    # Proposed wager on YES with raw Kelly = 0.02 * 10000 = $200.
    # 1. No current exposure. Wager should be fully allowed.
    wager1 = rm.size_order(
        estimated_prob=Decimal("0.70"),
        market_price=Decimal("0.60"),
        side="yes",
        sector="Economics",
        current_sector_exposure=Decimal("0.00"),
        total_capital=total_capital,
    )
    assert wager1 == Decimal("200.00")

    # 2. High current exposure: $2900.
    # Remaining room = 3000 - 2900 = $100.
    # Wager of $200 should be scaled down to $100.
    wager2 = rm.size_order(
        estimated_prob=Decimal("0.70"),
        market_price=Decimal("0.60"),
        side="yes",
        sector="Economics",
        current_sector_exposure=Decimal("2900.00"),
        total_capital=total_capital,
    )
    assert wager2 == Decimal("100.00")


def test_order_book_synthetic_asks():
    """
    Test Phase 2 Reciprocal pricing and synthetic ask levels:
    YES Ask Price = 1.00 - NO Bid Price
    Also test depth capping to 200 levels.
    """
    book = LocalOrderBook(max_depth=3)  # Limit depth to 3 for testing

    snapshot = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "FED-26",
            "market_id": "test-id",
            "yes_dollars_fp": [["0.5800", "100.00"], ["0.5700", "200.00"]],
            "no_dollars_fp": [["0.4100", "50.00"], ["0.4000", "150.00"]],
        },
    }

    book.apply_snapshot(snapshot)

    # Check YES bids directly from yes_dollars_fp
    yes_bids = book.get_yes_bids()
    assert yes_bids == [
        (Decimal("0.5800"), Decimal("100.00")),
        (Decimal("0.5700"), Decimal("200.00")),
    ]

    # Check YES asks calculated from NO bids:
    # 1.00 - 0.4100 = 0.5900 (qty 50)
    # 1.00 - 0.4000 = 0.6000 (qty 150)
    # Sorted ascending: 0.5900, 0.6000
    yes_asks = book.get_yes_asks()
    assert yes_asks == [
        (Decimal("0.5900"), Decimal("50.00")),
        (Decimal("0.6000"), Decimal("150.00")),
    ]

    # Test memory capping:
    # Add new NO bids. Currently we have 0.4100, 0.4000.
    # Let's add 0.4300, 0.4200. Total NO bids becomes 4.
    # Since depth is capped at 3, we should keep the top 3 highest prices:
    # 0.4300, 0.4200, 0.4100. (0.4000 should be pruned).
    book.apply_delta(
        {
            "type": "orderbook_delta",
            "msg": {"side": "no", "price_dollars": "0.4200", "delta_fp": "80.00"},
        }
    )
    book.apply_delta(
        {
            "type": "orderbook_delta",
            "msg": {"side": "no", "price_dollars": "0.4300", "delta_fp": "90.00"},
        }
    )

    no_bids = book.get_no_bids()
    # Pruned to top 3 highest prices
    assert no_bids == [
        (Decimal("0.4300"), Decimal("90.00")),
        (Decimal("0.4200"), Decimal("80.00")),
        (Decimal("0.4100"), Decimal("50.00")),
    ]
    # Check YES asks:
    # 1.00 - 0.4300 = 0.5700
    # 1.00 - 0.4200 = 0.5800
    # 1.00 - 0.4100 = 0.5900
    assert book.get_yes_asks() == [
        (Decimal("0.5700"), Decimal("90.00")),
        (Decimal("0.5800"), Decimal("80.00")),
        (Decimal("0.5900"), Decimal("50.00")),
    ]


def test_shadow_mode_interception():
    """
    Test Phase 1: Live Shadowing Mode.
    Ensures that when SHADOW_MODE is True, placing an order does not call REST API,
    and correctly logs payload and fee tracker states.
    """
    import json

    original_shadow_mode = Config.SHADOW_MODE
    Config.SHADOW_MODE = True

    initialize_db()

    from execution.engine import _close_shadow_logger
    _close_shadow_logger()

    shadow_log_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "logs", "shadow_trades.log")
    )
    if os.path.exists(shadow_log_path):
        os.remove(shadow_log_path)

    engine = ExecutionEngine()
    engine.fee_tracker.reset()

    resp = engine.place_order(
        ticker="FED-MOCK",
        outcome_side="yes",
        price=Decimal("0.60"),
        quantity=Decimal("10"),
        action="buy",
        synthetic_ask=Decimal("0.61"),
    )

    assert resp["status"] == "filled"
    assert resp["ticker"] == "FED-MOCK"
    assert "shadow-" in resp["order_id"]

    assert os.path.exists(shadow_log_path)
    with open(shadow_log_path) as f:
        log_line = f.readline().strip()
        log_data = json.loads(log_line)

        assert log_data["ticker"] == "FED-MOCK"
        assert log_data["price"] == "0.6000"
        assert log_data["quantity"] == "10"
        assert log_data["synthetic_ask"] == 0.61
        assert log_data["fee_accumulator"] == 0.0

    Config.SHADOW_MODE = original_shadow_mode
    _close_shadow_logger()
    if os.path.exists(shadow_log_path):
        os.remove(shadow_log_path)


def test_database_logging():
    """
    Test Phase 3 SQLite database storage.
    """
    initialize_db()

    release_id = log_release(
        indicator="CPI", release_date="2026-05-27T01:00:00Z", actual=3.2, forecast=3.0, previous=3.1
    )
    assert release_id > 0

    trade_id = log_shadow_trade(
        ticker="CPI-MOCK",
        timestamp="2026-05-27T01:01:00Z",
        action="buy",
        outcome_side="yes",
        price=0.60,
        quantity=10.0,
        synthetic_ask=0.60,
        proposed_kelly=0.25,
        final_wager=200.00,
        fee_accumulator=0.005,
        release_id=release_id,
    )
    assert trade_id > 0

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT indicator, actual_value FROM macro_releases WHERE id = ?", (release_id,))
    rel_row = cursor.fetchone()
    assert rel_row is not None
    assert rel_row[0] == "CPI"
    assert rel_row[1] == 3.2

    cursor.execute("SELECT ticker, price, release_id FROM shadow_trades WHERE id = ?", (trade_id,))
    trade_row = cursor.fetchone()
    assert trade_row is not None
    assert trade_row[0] == "CPI-MOCK"
    assert trade_row[1] == 0.60
    assert trade_row[2] == release_id

    conn.close()


def test_macro_strategy_trigger():
    """
    Test Phase 2 Macroeconomic Strategy Trigger Logic.
    """
    original_shadow_mode = Config.SHADOW_MODE
    Config.SHADOW_MODE = True

    initialize_db()

    risk_manager = RiskManager()
    execution_engine = ExecutionEngine()
    order_book = LocalOrderBook()

    snapshot = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "FED-MOCK",
            "market_id": "test-id",
            "yes_dollars_fp": [["0.5800", "100.00"]],
            "no_dollars_fp": [["0.4100", "50.00"]],
        },
    }
    order_book.apply_snapshot(snapshot)

    res_yes = MockCalendarProvider.trigger_mock_release(
        indicator="CPI",
        actual=3.2,
        forecast=3.0,
        previous=3.1,
        ticker="FED-MOCK",
        sector="Economics",
        risk_manager=risk_manager,
        execution_engine=execution_engine,
        order_book=order_book,
        total_capital=Decimal("10000.00"),
        surprise_std=0.07,
    )

    assert res_yes["status"] == "executed"
    assert res_yes["side"] == "yes"
    assert res_yes["wager"] == 29.5
    assert res_yes["price"] == 0.59

    res_no = MockCalendarProvider.trigger_mock_release(
        indicator="CPI",
        actual=2.8,
        forecast=3.0,
        previous=3.1,
        ticker="FED-MOCK",
        sector="Economics",
        risk_manager=risk_manager,
        execution_engine=execution_engine,
        order_book=order_book,
        total_capital=Decimal("10000.00"),
        surprise_std=0.07,
    )

    assert res_no["status"] == "executed"
    assert res_no["side"] == "no"
    assert res_no["price"] == 0.42

    Config.SHADOW_MODE = original_shadow_mode
    from execution.engine import _close_shadow_logger
    _close_shadow_logger()
    shadow_log_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "logs", "shadow_trades.log")
    )
    if os.path.exists(shadow_log_path):
        os.remove(shadow_log_path)


def test_shadow_log_redirection():
    """
    Test Phase 1: Verify that logging redirects correctly to logs/shadow_trades.log
    """

    from execution.engine import _close_shadow_logger
    _close_shadow_logger()

    original_shadow_mode = Config.SHADOW_MODE
    Config.SHADOW_MODE = True

    initialize_db()

    logs_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
    shadow_log_path = os.path.join(logs_dir, "shadow_trades.log")

    if os.path.exists(shadow_log_path):
        os.remove(shadow_log_path)

    engine = ExecutionEngine()
    engine.place_order(
        ticker="LOG-TEST",
        outcome_side="yes",
        price=Decimal("0.50"),
        quantity=Decimal("10"),
        action="buy",
    )

    assert os.path.exists(logs_dir)
    assert os.path.exists(shadow_log_path)

    with open(shadow_log_path) as f:
        log_line = f.readline().strip()
        assert "LOG-TEST" in log_line

    Config.SHADOW_MODE = original_shadow_mode
    from execution.engine import _close_shadow_logger
    _close_shadow_logger()
    if os.path.exists(shadow_log_path):
        os.remove(shadow_log_path)


def test_main_daemon_loop_mock():
    """
    Test continuous main.py loop behavior by running it in a mocked state.
    """
    from unittest.mock import MagicMock, patch

    import main

    os.environ["KALSHI_TESTING"] = "1"
    main.running = True

    def stop_loop_after_one_iteration(*args, **kwargs):
        main.running = False
        main._shutdown_event.set()
        return True

    with (
        patch("main.initialize_db") as mock_init_db,
        patch("main.KalshiWebSocketClient") as mock_ws_client,
        patch("main.KillSwitch") as mock_kill_switch,
        patch("main.MacroTrackerStrategy") as mock_strategy,
        patch("main.start_health_server"),
        patch("main.setup_alerts"),
        patch("time.sleep"),
    ):
        mock_ks_instance = MagicMock()
        mock_ks_instance.check_and_trigger_with_capital.return_value = (False, Decimal("10000.00"))
        mock_kill_switch.return_value = mock_ks_instance

        mock_strategy_instance = MagicMock()
        mock_strategy_instance.check_for_new_release.side_effect = stop_loop_after_one_iteration
        mock_strategy.return_value = mock_strategy_instance

        mock_ws_client_instance = MagicMock()
        mock_ws_client_instance.wait_for_connection.return_value = True
        mock_ws_client.return_value = mock_ws_client_instance

        main.run_loop()

        mock_init_db.assert_called_once()
        mock_ws_client.assert_called_once()
        mock_kill_switch.assert_called()
        mock_strategy_instance.check_for_new_release.assert_called_once()
