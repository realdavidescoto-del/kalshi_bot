import os
import signal
import sys
import threading
import time
from decimal import Decimal

import uvicorn
from execution.order_state import OrderSide

from config import Config
from data.audit_log import get_audit_logger
from data.database import (
    initialize_db,
    log_portfolio_snapshot,
    vacuum_database,
)
from execution.position_manager import get_position_manager
from market.order_book import LocalOrderBook
from market.websocket_client import KalshiWebSocketClient
from observability import (
    get_logger,
    push_metrics_to_gateway,
    record_strategy_trigger,
    set_correlation_id,
    setup_structured_logging,
    update_db_stats,
    update_position_pnl,
    update_positions_open,
    update_sector_exposure,
    update_system_cpu,
    update_system_memory,
    update_total_capital,
    update_var_usage,
    update_ws_connection_count,
)
from observability.alerts import setup_alerts
from observability.health import create_health_app
from resilience.rate_limiter import configure_default_tiers, get_rate_limiter
from safety.kill_switch import KillSwitch
from strategy.macro_tracker import MacroTrackerStrategy

log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "logs"))
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "kalshi_bot.log")

module_levels = {
    "kalshi_bot.websocket": "DEBUG",
    "kalshi_bot.execution": "INFO",
    "kalshi_bot.risk_manager": "INFO",
    "kalshi_bot.macro_tracker": "INFO",
}

setup_structured_logging(
    level=os.getenv("LOG_LEVEL", "INFO"),
    log_file=log_file,
    module_levels_config=module_levels,
)

logger = get_logger("kalshi_bot.main")

running = True
_health_server = None
_health_thread = None
_shutdown_event = threading.Event()


def handle_shutdown(signum, frame):
    global running
    logger.info(f"Signal {signum} received. Initiating graceful shutdown...")
    running = False
    _shutdown_event.set()


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def verify_startup_health():
    log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "logs"))
    db_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "data"))

    for path in (log_dir, db_dir):
        os.makedirs(path, exist_ok=True)

    if Config.REQUEST_TIMEOUT_SEC <= 0:
        raise ValueError("REQUEST_TIMEOUT_SEC must be positive.")
    if Config.RETRY_MAX_ATTEMPTS < 1:
        raise ValueError("RETRY_MAX_ATTEMPTS must be at least 1.")

    logger.info(
        "Startup health check passed: log/data directories writable and retry settings are valid."
    )


def start_health_server(port: int = 8080):
    global _health_server, _health_thread
    app = create_health_app()

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    _health_server = uvicorn.Server(config)
    _health_thread = threading.Thread(target=_health_server.run, daemon=True)
    _health_thread.start()
    logger.info(f"Health server started on 127.0.0.1:{port}")


def stop_health_server():
    global _health_server
    if _health_server:
        _health_server.should_exit = True


def run_loop():
    global running

    set_correlation_id()

    initialize_db()

    try:
        Config.validate()
        verify_startup_health()
        logger.info(
            f"Configuration verified. Running in ENV: '{Config.ENV}', SHADOW_MODE: '{Config.SHADOW_MODE}'"
        )
    except Exception as e:
        logger.error(f"Configuration validation failed: {e}")
        if os.getenv("KALSHI_TESTING") != "1":
            sys.exit(1)

    tickers_raw = os.getenv("KALSHI_TICKERS", os.getenv("KALSHI_TICKER", "FED-24DEC-T4.00"))
    ticker_configs = []
    for entry in tickers_raw.split(";"):
        parts = entry.split(",")
        t = parts[0].strip()
        s = (parts[1].strip() if len(parts) > 1 else os.getenv("KALSHI_SECTOR", "Economics"))
        ind_raw = (parts[2].strip() if len(parts) > 2 else os.getenv("MACRO_INDICATOR", "CPI"))
        # Support multi-indicator: "CPI+PCE" -> ["CPI", "PCE"]
        i_list = [x.strip().upper() for x in ind_raw.replace("+", ",").split(",")]
        ticker_configs.append((t, s, i_list[0], i_list))

    indicator = ticker_configs[0][2]
    poll_interval = int(os.getenv("POLL_INTERVAL_SEC", "60"))
    health_port = int(os.getenv("HEALTH_PORT", "8080"))
    stop_loss_pct = Decimal(os.getenv("STOP_LOSS_PCT", "0.15"))
    vacuum_interval = int(os.getenv("VACUUM_INTERVAL_SEC", "86400"))

    shadow_auto_approve_sec = int(os.getenv("SHADOW_AUTO_APPROVE_SEC", "0"))
    _shadow_start_time = time.time() if Config.SHADOW_MODE and shadow_auto_approve_sec > 0 else None
    _shadow_mode_active = Config.SHADOW_MODE and shadow_auto_approve_sec > 0

    if not Config.SHADOW_MODE and not os.getenv("LIVE_TRADE_CONFIRMED"):
        logger.critical(
            "LIVE MODE REQUIRES CONFIRMATION. Set LIVE_TRADE_CONFIRMED=1 in .env to proceed. "
            "Start in SHADOW_MODE=True first, verify logs, then add LIVE_TRADE_CONFIRMED=1."
        )
        sys.exit(1)

    logger.info(
        f"Starting Continuous Trading Loop. Tickers: {[t for t, _, _, _ in ticker_configs]}, "
        f"Indicator: {indicator}"
    )

    start_health_server(health_port)
    alert_manager = setup_alerts()
    alert_manager.start()
    from resilience.dead_letter_queue import get_dlq_registry
    get_dlq_registry().start_all_processors()
    configure_default_tiers()
    get_audit_logger().cleanup_old_logs()

    order_book = LocalOrderBook()
    kill_switch = KillSwitch()
    strategies = [
        MacroTrackerStrategy(ticker=t, sector=s, indicator=i, indicators=inds)
        for t, s, i, inds in ticker_configs
    ]
    position_manager = get_position_manager()

    ticker_to_sector = {t: s for t, s, _, _ in ticker_configs}
    for strat in strategies:
        strat.order_book = order_book
        strat.set_ticker_to_sector(ticker_to_sector)

    ws_client = KalshiWebSocketClient(ticker_configs[0][0], order_book)
    ws_client.connect()

    if not ws_client.wait_for_connection(timeout=10.0):
        logger.error(
            "WebSocket did not establish a valid connection within timeout. "
            "Please verify credentials, endpoint settings, and network access."
        )
        ws_client.disconnect()
        stop_health_server()
        sys.exit(1)

    update_ws_connection_count(1)

    logger.info("Bot components successfully initialized and monitoring...")

    if not Config.SHADOW_MODE:
        try:
            api_positions = kill_switch.get_positions()
            ticker_to_price = {}
            for ticker, _ in ticker_configs:
                best_bid, _ = order_book.get_best_yes_bid()
                best_ask, _ = order_book.get_best_yes_ask()
                ticker_to_price[ticker] = {
                    OrderSide.YES: best_bid or Decimal("0.50"),
                    OrderSide.NO: best_ask or Decimal("0.50"),
                }
            position_manager.sync_from_api_positions(api_positions, ticker_to_price)
            logger.info(f"Reconciled {len(api_positions)} positions from Kalshi API")
        except Exception as e:
            logger.warning(f"Position reconciliation failed (non-fatal): {e}")

    last_health_check = 0
    health_check_interval = 30
    last_vacuum_time = 0
    last_snapshot_time = 0
    snapshot_interval = 300

    while running:
        try:
            kill_switch_active, capital = kill_switch.check_and_trigger_with_capital()
            if kill_switch_active:
                logger.critical(
                    "Kill Switch safety triggered! Severing connections and halting bot."
                )
                break

            update_total_capital(float(capital))

            if capital > 0:
                update_var_usage(0.0)

            stop_loss_positions = position_manager.get_positions_for_stop_loss(stop_loss_pct)
            if stop_loss_positions:
                for pos in stop_loss_positions:
                    logger.critical(
                        f"STOP-LOSS TRIGGERED: {pos.ticker} {pos.side.value} "
                        f"unrealized PnL=${pos.unrealized_pnl:.2f} exceeds {stop_loss_pct * 100}% threshold"
                    )
                kill_switch_active = True
                kill_switch.cancel_all_orders()
                break

            trailing_stop_positions = position_manager.get_trailing_stop_positions(Config.TRAILING_STOP_PCT)
            for pos in trailing_stop_positions:
                if position_manager.has_pending_exit(pos.ticker, pos.side):
                    continue
                logger.info(
                    f"TRAILING STOP TRIGGERED: {pos.ticker} {pos.side.value} "
                    f"drawdown exceeded {Config.TRAILING_STOP_PCT * 100}% from high"
                )
                if pos.quantity > 0 and strategies:
                    exit_price = order_book.get_best_yes_bid()[0] if pos.side == OrderSide.YES else order_book.get_best_no_bid()[0]
                    if exit_price is not None:
                        engine = next(
                            (s.execution_engine for s in strategies if s.ticker == pos.ticker),
                            strategies[0].execution_engine,
                        )
                        position_manager.set_pending_exit(pos.ticker, pos.side, True)
                        engine.place_order(
                            ticker=pos.ticker, outcome_side=pos.side.value,
                            price=exit_price, quantity=pos.quantity, action="sell",
                        )

            take_profit_positions = position_manager.get_take_profit_positions()
            for pos, tier in take_profit_positions:
                if position_manager.has_pending_exit(pos.ticker, pos.side):
                    continue
                logger.info(
                    f"TAKE PROFIT ({tier}): {pos.ticker} {pos.side.value} "
                    f"entry={pos.avg_entry_price} pnl={pos.unrealized_pnl}"
                )
                position_manager.mark_take_profit_tier(pos, tier)
                if pos.quantity > 0 and strategies:
                    exit_price = order_book.get_best_yes_bid()[0] if pos.side == OrderSide.YES else order_book.get_best_no_bid()[0]
                    if exit_price is not None:
                        sell_qty = (pos.quantity * Decimal("0.3")).to_integral_value(rounding="ROUND_HALF_UP")
                        if sell_qty > 0:
                            engine = next(
                                (s.execution_engine for s in strategies if s.ticker == pos.ticker),
                                strategies[0].execution_engine,
                            )
                            position_manager.set_pending_exit(pos.ticker, pos.side, True)
                            engine.place_order(
                                ticker=pos.ticker, outcome_side=pos.side.value,
                                price=exit_price, quantity=sell_qty, action="sell",
                            )

            for p in position_manager.get_all_positions():
                update_positions_open(p.ticker, p.side.value, 1 if p.quantity > 0 else 0)
                update_position_pnl(p.ticker, p.side.value, "realized", float(p.realized_pnl))
                update_position_pnl(p.ticker, p.side.value, "unrealized", float(p.unrealized_pnl))
                update_position_pnl(p.ticker, p.side.value, "total", float(p.total_pnl))

            for strategy in strategies:
                try:
                    sector_exposure = position_manager.get_sector_exposure(
                        strategy.sector, ticker_to_sector
                    )
                    update_sector_exposure(strategy.sector, float(sector_exposure))
                    triggered = strategy.check_for_new_release(capital, sector_exposure)
                    if triggered:
                        logger.info(
                            f"Strategy {strategy.ticker} triggered: Shadow trade logged."
                        )
                        record_strategy_trigger(strategy.indicator, "triggered")
                except Exception as e:
                    logger.error(f"Strategy {strategy.ticker} check failed: {e}")

            if not Config.SHADOW_MODE:
                for strategy in strategies:
                    strategy.execution_engine.poll_order_statuses(kill_switch)

            best_bid_price, _ = order_book.get_best_yes_bid()
            best_ask_price, _ = order_book.get_best_yes_ask()
            mid_price = (
                (best_bid_price + best_ask_price) / Decimal("2.0")
                if (best_bid_price and best_ask_price)
                else None
            )

            now = time.time()
            if now - last_health_check >= health_check_interval:
                import psutil

                process = psutil.Process()
                mem_mb = process.memory_info().rss / 1024 / 1024
                cpu_pct = process.cpu_percent()
                update_system_memory(int(mem_mb * 1024 * 1024))
                update_system_cpu(cpu_pct)

                from data.database import get_db_stats
                db_stats = get_db_stats()
                update_db_stats(db_stats)

                push_metrics_to_gateway()

            if _shadow_start_time is not None and _shadow_mode_active:
                elapsed = now - _shadow_start_time
                if elapsed >= shadow_auto_approve_sec:
                    logger.info(
                        f"Shadow mode auto-approval: {elapsed:.0f}s elapsed >= {shadow_auto_approve_sec}s threshold. "
                        "Switching to live mode."
                    )
                    _shadow_mode_active = False
                    _shadow_start_time = None
                    for strat in strategies:
                        strat.execution_engine.set_shadow_mode(False)

            last_health_check = now

            if now - last_vacuum_time >= vacuum_interval:
                try:
                    vacuum_database()
                except Exception as e:
                    logger.warning(f"Database vacuum failed: {e}")
                last_vacuum_time = now

            if now - last_snapshot_time >= snapshot_interval:
                try:
                    open_pos = position_manager.get_open_positions()
                    total_exposure = float(position_manager.get_total_exposure())
                    realized_pnl = float(position_manager.get_total_realized_pnl())
                    unrealized_pnl = float(position_manager.get_total_unrealized_pnl())
                    log_portfolio_snapshot(
                        balance=float(capital),
                        total_exposure=total_exposure,
                        open_positions=len(open_pos),
                        total_realized_pnl=realized_pnl,
                        total_unrealized_pnl=unrealized_pnl,
                        sector=indicator,
                    )
                    last_snapshot_time = now
                except Exception as e:
                    logger.warning(f"Portfolio snapshot failed: {e}")

            total_fees = sum(
                s.execution_engine.fee_tracker.accumulator for s in strategies
            )
            logger.info(
                f"[STATUS REPORT] Capital Balance: ${capital:.2f} | "
                f"Market YES Bid: ${best_bid_price if best_bid_price else 'N/A'} | "
                f"Market YES Ask: ${best_ask_price if best_ask_price else 'N/A'} | "
                f"Mid: ${mid_price if mid_price else 'N/A'} | "
                f"Fee Accumulator: ${total_fees:.6f}"
            )

        except Exception as e:
            logger.error(f"Error in main loop iteration: {e}")

        _shutdown_event.wait(timeout=poll_interval)
        if not running:
            break

    logger.info("Shutting down background connection channels...")
    update_ws_connection_count(0)
    ws_client.disconnect()
    stop_health_server()
    logger.info("Shutdown sequence finalized.")


if __name__ == "__main__":
    run_loop()
