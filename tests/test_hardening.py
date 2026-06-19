import os
os.environ["KALSHI_TESTING"] = "1"

import sys
import threading
import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from config import Config
from data.audit_log import AuditLogger
from execution.order_state import OrderAction, OrderSide
from execution.position_manager import Position, PositionManager
from market.order_book import LocalOrderBook
from resilience.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitBreakerRegistry,
)
from resilience.rate_limiter import (
    configure_default_tiers,
    get_rate_limiter,
)


class TestCircuitBreakerHardening:
    def test_record_failure_manually(self):
        cb = CircuitBreaker("test_rf", CircuitBreakerConfig(failure_threshold=3, timeout_seconds=60.0))
        assert cb.state.value == "closed"
        cb.record_failure()
        assert cb.state.value == "closed"
        cb.record_failure()
        assert cb.state.value == "closed"
        cb.record_failure()
        assert cb.state.value == "open"

    def test_bypass_skips_open_check(self):
        cb = CircuitBreaker("test_bypass", CircuitBreakerConfig(failure_threshold=1, timeout_seconds=60.0))
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state.value == "open"
        result = cb.call(lambda: "bypassed", bypass=True)
        assert result == "bypassed"
        assert cb.state.value == "open"

    def test_bypass_still_records_failures(self):
        cb = CircuitBreaker("test_bypass_fail", CircuitBreakerConfig(failure_threshold=2, timeout_seconds=60.0))
        result = cb.call(lambda: "ok", bypass=True)
        assert result == "ok"
        assert cb.state.value == "closed"
        cb.record_failure()
        cb.record_failure()
        assert cb.state.value == "open"
        result = cb.call(lambda: "bypassed", bypass=True)
        assert result == "bypassed"

    def test_record_failure_resets_after_half_open_success(self):
        cb = CircuitBreaker("test_reset", CircuitBreakerConfig(
            failure_threshold=2, success_threshold=1, timeout_seconds=0.1
        ))
        cb.record_failure()
        cb.record_failure()
        assert cb.state.value == "open"
        time.sleep(0.15)
        assert cb.state.value == "half_open"
        cb.call(lambda: "ok")
        assert cb.state.value == "closed"


class TestRateLimiterHardening:
    def test_configure_default_tiers(self):
        rl = get_rate_limiter()
        configure_default_tiers()
        stats = rl.get_all_stats()
        assert "rest_api" in stats
        assert "websocket" in stats
        assert "fred_api" in stats
        assert stats["rest_api"]["capacity"] == 20
        assert stats["websocket"]["capacity"] == 10
        assert stats["fred_api"]["capacity"] == 5

    def test_configure_default_tiers_idempotent(self):
        rl = get_rate_limiter()
        configure_default_tiers()
        configure_default_tiers()
        stats = rl.get_all_stats()
        assert stats["rest_api"]["capacity"] == 20


class TestKillSwitchHardening:
    def test_cancel_all_orders_bypass_circuit(self):
        with (
            patch("safety.kill_switch.Config.get_verified_session") as mock_session,
            patch("safety.kill_switch.Config.request_with_retry") as mock_request,
            patch("safety.kill_switch.Config.get_private_key") as mock_key,
            patch("safety.kill_switch.Config.API_KEY_ID", "test_key"),
            patch("safety.kill_switch.Config.ENV", "demo"),
            patch("safety.kill_switch.Config.API_VERSION", "v2"),
        ):
            mock_key.return_value.sign.return_value = b"fake_signature"
            mock_session.return_value = MagicMock()
            mock_request.return_value = MagicMock(status_code=200, json=lambda: {"orders": []})
            from safety.kill_switch import KillSwitch
            ks = KillSwitch()
            result = ks.cancel_all_orders()
            assert result == 0

    def test_check_and_trigger_with_capital_below_limit(self):
        with (
            patch("safety.kill_switch.Config.get_verified_session") as mock_session,
            patch("safety.kill_switch.Config.request_with_retry") as mock_request,
            patch("safety.kill_switch.Config.get_private_key") as mock_key,
            patch("safety.kill_switch.Config.API_KEY_ID", "test_key"),
            patch("safety.kill_switch.Config.ENV", "demo"),
            patch("safety.kill_switch.Config.API_VERSION", "v2"),
            patch("safety.kill_switch.Config.KILL_SWITCH_MIN_BALANCE", Decimal("100.00")),
        ):
            mock_key.return_value.sign.return_value = b"fake_signature"
            mock_session.return_value = MagicMock()
            mock_request.return_value = MagicMock(status_code=200, json=lambda: {"balance_dollars": "50.00"})
            from safety.kill_switch import KillSwitch
            ks = KillSwitch()
            triggered, balance = ks.check_and_trigger_with_capital()
            assert triggered is True
            assert balance == Decimal("50.00")


class TestOrderBookConcurrency:
    def test_concurrent_clear_and_apply(self):
        book = LocalOrderBook()
        errors = []

        def clear_loop():
            for _ in range(100):
                try:
                    book.clear()
                except Exception as e:
                    errors.append(e)

        def apply_snapshot_loop():
            for _ in range(100):
                try:
                    book.apply_snapshot({"msg": {"yes_dollars_fp": [["0.50", "10"]], "no_dollars_fp": []}})
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=clear_loop) for _ in range(4)]
        threads += [threading.Thread(target=apply_snapshot_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"

    def test_concurrent_read_and_write(self):
        book = LocalOrderBook()
        errors = []

        def write_loop():
            for i in range(100):
                try:
                    price = Decimal(f"0.{i % 99 + 1:02d}")
                    book.apply_snapshot({"msg": {"yes_dollars_fp": [[str(price), "5"]], "no_dollars_fp": []}})
                except Exception as e:
                    errors.append(e)

        def read_loop():
            for _ in range(100):
                try:
                    book.get_yes_bids()
                    book.get_yes_asks()
                    book.get_best_yes_bid()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=write_loop) for _ in range(4)]
        threads += [threading.Thread(target=read_loop) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"

    def test_clear_while_delta(self):
        book = LocalOrderBook()
        book.apply_snapshot({"msg": {"yes_dollars_fp": [["0.50", "10"]], "no_dollars_fp": [["0.40", "5"]]}})
        errors = []

        def delta_loop():
            for _ in range(50):
                try:
                    book.apply_delta({"msg": {"side": "yes", "price_dollars": "0.50", "delta_fp": "1"}})
                except Exception as e:
                    errors.append(e)

        def clear_loop():
            for _ in range(50):
                try:
                    book.clear()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=delta_loop) for _ in range(2)]
        threads += [threading.Thread(target=clear_loop) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Concurrent access errors: {errors}"


class TestPositionManagerHardening:
    TP_TIER1_MULTIPLIER = Decimal("2.0")
    TP_TIER2_MULTIPLIER = Decimal("3.0")

    def test_trailing_stop_triggers(self):
        pm = PositionManager()
        pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                       avg_entry_price=Decimal("0.50"), highest_price=Decimal("0.50"))
        pm._positions["TEST:yes"] = pos
        pos.update_unrealized(Decimal("0.40"))
        triggered = pm.get_trailing_stop_positions(Decimal("0.10"))
        assert len(triggered) == 1
        assert triggered[0].ticker == "TEST"

    def test_trailing_stop_no_trigger_within_tolerance(self):
        pm = PositionManager()
        pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                       avg_entry_price=Decimal("0.50"), highest_price=Decimal("0.60"))
        pm._positions["TEST:yes"] = pos
        pos.update_unrealized(Decimal("0.55"))
        triggered = pm.get_trailing_stop_positions(Decimal("0.10"))
        assert len(triggered) == 0

    def test_take_profit_tier_1_triggers(self):
        with patch("execution.position_manager.Config.TAKE_PROFIT_TIER_1_MULTIPLIER", Decimal("2.0")):
            pm = PositionManager()
            pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                           avg_entry_price=Decimal("0.25"))
            pm._positions["TEST:yes"] = pos
            pos.update_unrealized(Decimal("0.90"))
            hits = pm.get_take_profit_positions()
            assert len(hits) == 1
            assert hits[0][1] == "tier_1"

    def test_take_profit_tier_2_triggers(self):
        with patch("execution.position_manager.Config.TAKE_PROFIT_TIER_1_MULTIPLIER", Decimal("2.0")):
            with patch("execution.position_manager.Config.TAKE_PROFIT_TIER_2_MULTIPLIER", Decimal("3.0")):
                pm = PositionManager()
                pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                               avg_entry_price=Decimal("0.10"))
                pm._positions["TEST:yes"] = pos
                pos.update_unrealized(Decimal("0.90"))
                hits = pm.get_take_profit_positions()
                assert len(hits) == 1
                assert hits[0][1] == "tier_2"

    def test_take_profit_marked_does_not_retrigger(self):
        pm = PositionManager()
        pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                       avg_entry_price=Decimal("0.10"))
        pm._positions["TEST:yes"] = pos
        pos.update_unrealized(Decimal("0.90"))
        pm.mark_take_profit_tier(pos, "tier_1")
        pm.mark_take_profit_tier(pos, "tier_2")
        hits = pm.get_take_profit_positions()
        assert len(hits) == 0

    def test_no_side_trailing_stop(self):
        pm = PositionManager()
        pos = Position(ticker="TEST", side=OrderSide.NO, quantity=Decimal("100"),
                       avg_entry_price=Decimal("0.50"), highest_price=Decimal("0.50"))
        pm._positions["TEST:no"] = pos
        pos.update_unrealized(Decimal("0.35"))
        triggered = pm.get_trailing_stop_positions(Decimal("0.10"))
        assert len(triggered) == 1

    def test_close_partial_updates_position(self):
        pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                       avg_entry_price=Decimal("0.50"))
        pnl = pos.close_partial(Decimal("40"), Decimal("0.60"), Decimal("0"), Decimal("0"))
        assert pos.quantity == Decimal("60")
        assert pnl == Decimal("4.0")

    def test_update_unrealized_tracks_highest_price(self):
        pos = Position(ticker="TEST", side=OrderSide.YES, quantity=Decimal("100"),
                       avg_entry_price=Decimal("0.50"))
        pos.update_unrealized(Decimal("0.70"))
        assert pos.highest_price == Decimal("0.70")
        pos.update_unrealized(Decimal("0.65"))
        assert pos.highest_price == Decimal("0.70")


class TestShadowModeOverride:
    def test_shadow_mode_override_enabled(self):
        from execution.engine import ExecutionEngine
        with patch("execution.engine.Config.SHADOW_MODE", False):
            engine = ExecutionEngine()
            engine.set_shadow_mode(True)
            effective = engine._shadow_mode_override if engine._shadow_mode_override is not None else Config.SHADOW_MODE
            assert effective is True

    def test_shadow_mode_override_disabled(self):
        from execution.engine import ExecutionEngine
        with patch("execution.engine.Config.SHADOW_MODE", True):
            engine = ExecutionEngine()
            engine.set_shadow_mode(False)
            effective = engine._shadow_mode_override if engine._shadow_mode_override is not None else Config.SHADOW_MODE
            assert effective is False

    def test_shadow_mode_override_reset(self):
        from execution.engine import ExecutionEngine
        with patch("execution.engine.Config.SHADOW_MODE", True):
            engine = ExecutionEngine()
            engine.set_shadow_mode(True)
            engine.set_shadow_mode(None)
            effective = engine._shadow_mode_override if engine._shadow_mode_override is not None else Config.SHADOW_MODE
            assert effective is True

    def test_shadow_mode_overrides_global(self):
        from execution.engine import ExecutionEngine
        engine = ExecutionEngine()
        engine.set_shadow_mode(True)
        effective = engine._shadow_mode_override if engine._shadow_mode_override is not None else Config.SHADOW_MODE
        assert effective is True


class TestConfigValidation:
    def test_validate_passes_with_defaults(self):
        save_key = Config._private_key
        Config._private_key = MagicMock()
        try:
            with patch.dict(os.environ, {"KALSHI_TESTING": "0"}):
                Config.validate()
        finally:
            Config._private_key = save_key

    def test_validate_raises_on_bad_var_limit(self):
        save_key = Config._private_key
        Config._private_key = MagicMock()
        try:
            Config.MAX_VAR_LIMIT_PCT = Decimal("0")
            with patch.dict(os.environ, {"KALSHI_TESTING": "0"}):
                with pytest.raises(ValueError, match="MAX_VAR_LIMIT_PCT"):
                    Config.validate()
        finally:
            Config.MAX_VAR_LIMIT_PCT = Decimal(os.getenv("MAX_VAR_LIMIT_PCT", "0.02"))
            Config._private_key = save_key

    def test_validate_raises_on_bad_sector_limit(self):
        save_key = Config._private_key
        Config._private_key = MagicMock()
        try:
            Config.MAX_VAR_LIMIT_PCT = Decimal("0.02")
            Config.MAX_SECTOR_LIMIT_PCT = Decimal("1.5")
            with patch.dict(os.environ, {"KALSHI_TESTING": "0"}):
                with pytest.raises(ValueError, match="MAX_SECTOR_LIMIT_PCT"):
                    Config.validate()
        finally:
            Config.MAX_SECTOR_LIMIT_PCT = Decimal(os.getenv("MAX_SECTOR_LIMIT_PCT", "0.30"))
            Config._private_key = save_key

    def test_validate_raises_on_bad_kelly(self):
        save_key = Config._private_key
        Config._private_key = MagicMock()
        try:
            Config.MAX_VAR_LIMIT_PCT = Decimal("0.02")
            Config.MAX_SECTOR_LIMIT_PCT = Decimal("0.30")
            Config.KELLY_MULTIPLIER = Decimal("0")
            with patch.dict(os.environ, {"KALSHI_TESTING": "0"}):
                with pytest.raises(ValueError, match="KELLY_MULTIPLIER"):
                    Config.validate()
        finally:
            Config.KELLY_MULTIPLIER = Decimal(os.getenv("KELLY_MULTIPLIER", "0.25"))
            Config._private_key = save_key

    def test_validate_skipped_in_testing(self):
        os.environ["KALSHI_TESTING"] = "1"
        Config.API_KEY_ID = None
        Config.validate()
        Config.API_KEY_ID = "test_key"


class TestHealthEndpoint:
    def test_ready_returns_503_when_circuit_breaker_open(self):
        from starlette.testclient import TestClient
        cb = CircuitBreakerRegistry().get_or_create("test_breaker")
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        from observability.health import create_health_app
        app = create_health_app()
        client = TestClient(app)
        response = client.get("/ready")
        assert response.status_code == 503
        cb.reset()

    def test_ready_returns_200_when_all_checks_pass(self):
        from starlette.testclient import TestClient
        from observability.health import create_health_app
        app = create_health_app()
        client = TestClient(app)
        response = client.get("/ready")
        assert response.status_code == 200

    def test_health_endpoint_exists(self):
        from starlette.testclient import TestClient
        from observability.health import create_health_app
        app = create_health_app()
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200


class TestAuditLogCleanup:
    def test_cleanup_removes_old_files(self, tmp_path):
        old_dir = tmp_path / "audit"
        old_dir.mkdir()
        old_file = old_dir / "audit-2020-01-01.log"
        old_file.write_text("old entry\n")
        old_mtime = time.time() - (31 * 86400)
        os.utime(str(old_file), (old_mtime, old_mtime))
        new_file = old_dir / "audit-2099-01-01.log"
        new_file.write_text("new entry\n")

        logger = AuditLogger(base_dir=str(tmp_path))
        logger.cleanup_old_logs(max_age_days=30)
        assert not old_file.exists()
        assert new_file.exists()

    def test_cleanup_keeps_recent_files(self, tmp_path):
        old_dir = tmp_path / "audit"
        old_dir.mkdir()
        today = time.strftime("%Y-%m-%d")
        f = old_dir / f"audit-{today}.log"
        f.write_text("entry\n")
        logger = AuditLogger(base_dir=str(tmp_path))
        logger.cleanup_old_logs(max_age_days=30)
        assert f.exists()


class TestProfitabilityTracking:
    def test_update_signal_profitability_sets_flag(self):
        from data.database import (
            initialize_db, log_strategy_signal, update_signal_profitability,
            get_strategy_performance, store_order, get_orders_for_position,
        )
        initialize_db()

        sig_id = log_strategy_signal("CPI", 3.0, 3.2, 0.2, 1.5, "moderate", 0.65, "yes", 100.0)
        assert sig_id > 0

        update_signal_profitability(sig_id, True)
        records = get_strategy_performance("CPI")
        match = [r for r in records if r["id"] == sig_id]
        assert len(match) == 1
        assert match[0]["profitable"] == 1

    def test_update_signal_profitability_does_not_overwrite(self):
        from data.database import (
            initialize_db, log_strategy_signal, update_signal_profitability,
            get_strategy_performance,
        )
        initialize_db()

        sig_id = log_strategy_signal("PCE", 2.0, 2.5, 0.5, 2.0, "strong", 0.80, "yes", 200.0,
                                      profitable=True)
        assert sig_id > 0

        update_signal_profitability(sig_id, False)
        records = get_strategy_performance("PCE")
        match = [r for r in records if r["id"] == sig_id]
        assert len(match) == 1
        assert match[0]["profitable"] == 1

    def test_store_order_with_signal_id(self):
        from data.database import (
            initialize_db, store_order, get_orders_for_position, log_strategy_signal,
        )
        initialize_db()

        sig_id = log_strategy_signal("CPI", 3.0, 3.2, 0.2, 1.5, "moderate", 0.65, "yes", 100.0)
        assert sig_id > 0

        order_id = store_order("test-sig-link", "TEST", signal_id=sig_id,
                                outcome_side="yes")
        assert order_id > 0

        orders = get_orders_for_position("TEST", "yes")
        matches = [o for o in orders if o["client_order_id"] == "test-sig-link"]
        assert len(matches) == 1
        assert matches[0]["signal_id"] == sig_id

    def test_on_order_fill_close_updates_profitability(self):
        from data.database import (
            initialize_db, log_strategy_signal, store_order,
            get_strategy_performance,
        )
        from execution.position_manager import PositionManager, OrderSide, OrderAction
        from decimal import Decimal
        initialize_db()

        sig_id = log_strategy_signal("FOMC", 5.0, 5.5, 0.5, 2.0, "strong", 0.75, "yes", 100.0)
        assert sig_id > 0

        store_order("profit-test-buy", "RATE-TEST", signal_id=sig_id,
                     action="buy", outcome_side="yes", price=0.50, quantity=10)

        pm = PositionManager()
        pm.on_order_fill("RATE-TEST", OrderSide.YES, OrderAction.BUY,
                         Decimal("10"), Decimal("0.50"), Decimal("0"), Decimal("0"))

        store_order("profit-test-sell", "RATE-TEST", signal_id=sig_id,
                     action="sell", outcome_side="yes", price=0.60, quantity=10)

        pm.on_order_fill("RATE-TEST", OrderSide.YES, OrderAction.SELL,
                         Decimal("10"), Decimal("0.60"), Decimal("0"), Decimal("0"))

        records = get_strategy_performance("FOMC")
        match = [r for r in records if r["id"] == sig_id]
        assert len(match) == 1
        assert match[0]["profitable"] == 1


class TestConfigSession:
    def test_session_is_cached(self):
        s1 = Config.get_verified_session()
        s2 = Config.get_verified_session()
        assert s1 is s2

    def test_session_has_connection_pooling(self):
        session = Config.get_verified_session()
        adapter = session.get_adapter("https://")
        assert adapter._pool_connections == 10
        assert adapter._pool_maxsize == 20
