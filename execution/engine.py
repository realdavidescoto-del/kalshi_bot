import json
import logging
import os
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from config import Config, sign_kalshi_headers
from data.audit_log import get_audit_logger
from data.database import order_exists, store_order, update_order_status
from data.database import log_shadow_trade
from execution.fee_tracker import FeeAccumulatorTracker
from execution.order_state import (
    OrderAction,
    OrderSide,
    OrderState,
    OrderStateMachine,
    get_order_state_machine,
)
from execution.position_manager import get_position_manager
from observability.metrics import (
    record_order_latency,
    record_order_placed,
    record_order_rejected,
    record_shadow_trade,
)
from resilience.circuit_breaker import CircuitBreakerConfig, CircuitBreakerRegistry
from resilience.dead_letter_queue import get_dlq_registry
from resilience.rate_limiter import get_rate_limiter

_SHADOW_LOGGER = None


def _get_shadow_logger():
    global _SHADOW_LOGGER
    if _SHADOW_LOGGER is not None:
        return _SHADOW_LOGGER
    from logging.handlers import RotatingFileHandler
    shadow_log_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "logs", "shadow_trades.log")
    )
    os.makedirs(os.path.dirname(shadow_log_path), exist_ok=True)
    _SHADOW_LOGGER = logging.getLogger("kalshi_bot.shadow_trades")
    _SHADOW_LOGGER.setLevel(logging.INFO)
    _SHADOW_LOGGER.propagate = False
    handler = RotatingFileHandler(
        shadow_log_path, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    _SHADOW_LOGGER.addHandler(handler)
    return _SHADOW_LOGGER


def _close_shadow_logger():
    global _SHADOW_LOGGER
    if _SHADOW_LOGGER is not None:
        for h in _SHADOW_LOGGER.handlers[:]:
            h.close()
            _SHADOW_LOGGER.removeHandler(h)
        _SHADOW_LOGGER = None

logger = logging.getLogger("kalshi_bot.execution")


class ExecutionEngine:
    def __init__(self):
        Config.validate()
        self.api_key_id = Config.API_KEY_ID
        self.private_key = None
        self.base_url = Config.get_rest_url()
        self.fee_tracker = FeeAccumulatorTracker()
        self._shadow_mode_override: bool | None = None

        self._circuit_breaker = CircuitBreakerRegistry().get_or_create(
            "rest_api", CircuitBreakerConfig(failure_threshold=5, timeout_seconds=30.0)
        )
        self._rate_limiter = get_rate_limiter().get_limiter("rest_api")
        self._dlq = get_dlq_registry().get_or_create("order_placement")
        self._order_machine = get_order_state_machine()
        self._position_manager = get_position_manager()

    def set_shadow_mode(self, shadow: bool | None):
        self._shadow_mode_override = shadow

    def format_price(self, price: Decimal) -> str:
        return f"{price:.4f}"

    def format_quantity(self, quantity: Decimal) -> str:
        return str(int(quantity.to_integral_value(rounding="ROUND_HALF_UP")))

    def poll_order_statuses(self, kill_switch):
        open_orders = self._order_machine.get_open_orders()
        if not open_orders:
            return
        logger.info(f"Polling Kalshi API for {len(open_orders)} open order statuses...")
        for order in open_orders:
            if not order.kalshi_order_id:
                continue
            try:
                path = f"/trade-api/{Config.API_VERSION}/portfolio/orders/{order.kalshi_order_id}"
                url = f"{self.base_url}{path}"
                headers = self.sign_headers("GET", path)

                if not self._rate_limiter.acquire(timeout=10.0):
                    continue

                session = Config.get_verified_session()
                response = Config.request_with_retry(
                    method="GET", url=url, headers=headers,
                    session=session, timeout=Config.REQUEST_TIMEOUT_SEC,
                )
                if response.status_code != 200:
                    continue

                data = response.json()
                api_status = data.get("status", "")
                if api_status in ("filled", "cancelled", "expired", "rejected"):
                    if api_status == "filled":
                        remaining = order.quantity - order.filled_quantity
                        if remaining > 0:
                            self._order_machine.fill_order(order.id, remaining, order.price)
                            self._position_manager.on_order_fill(
                                ticker=order.ticker, side=order.side,
                                action=order.action,
                                fill_qty=remaining,
                                fill_price=order.price,
                                fee=Decimal("0"), rebate=Decimal("0"),
                            )
                    else:
                        self._order_machine.transition(order.id, OrderState(api_status))
            except Exception as e:
                logger.debug(f"Order poll failed for {order.kalshi_order_id}: {e}")

    def sign_headers(self, method: str, path: str) -> dict:
        if not self.private_key:
            self.private_key = Config.get_private_key()
        return sign_kalshi_headers(self.api_key_id, self.private_key, method, path)

    def _place_order_with_resilience(
        self, path: str, url: str, headers: dict, payload: dict
    ) -> dict:
        def _do_request():
            if not self._rate_limiter.acquire(timeout=30.0):
                raise RuntimeError("Rate limiter timeout")

            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="POST",
                url=url,
                json=payload,
                headers=headers,
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 1.0))
                get_rate_limiter().handle_retry_after("rest_api", retry_after)
                raise RuntimeError(f"Rate limited, retry after {retry_after}s")

            return response

        return self._circuit_breaker.call(_do_request)

    def place_order(
        self,
        ticker: str,
        outcome_side: str,
        price: Decimal,
        quantity: Decimal,
        action: str = "buy",
        type_opts: str = "limit",
        time_in_force: str = "good_till_canceled",
        synthetic_ask: Decimal | None = None,
        release_id: int | None = None,
        proposed_kelly: Decimal | None = None,
        final_wager: Decimal | None = None,
        signal_id: int | None = None,
    ) -> dict:
        if not ticker or not isinstance(ticker, str):
            raise ValueError(f"ticker must be a non-empty string, got: {ticker!r}")
        if not outcome_side or not isinstance(outcome_side, str):
            raise ValueError(f"outcome_side must be a non-empty string, got: {outcome_side!r}")
        if not action or not isinstance(action, str):
            raise ValueError(f"action must be a non-empty string, got: {action!r}")
        if not isinstance(price, Decimal) or price <= Decimal("0") or price >= Decimal("1.0"):
            raise ValueError(f"price must be a Decimal in (0, 1.0), got: {price!r}")
        if not isinstance(quantity, Decimal) or quantity <= Decimal("0"):
            raise ValueError(f"quantity must be a positive Decimal, got: {quantity!r}")

        start_time = time.time()
        price_str = self.format_price(price)
        qty_int = int(quantity.to_integral_value(rounding="ROUND_HALF_UP"))
        qty_str = str(qty_int)
        client_order_id = str(uuid.uuid4())
        _audit = get_audit_logger()

        outcome_side_lower = outcome_side.strip().lower()
        action_lower = action.strip().lower()

        if outcome_side_lower == "yes":
            book_side = "bid"
            order_side = OrderSide.YES
        elif outcome_side_lower == "no":
            book_side = "ask"
            order_side = OrderSide.NO
        else:
            raise ValueError(f"Invalid outcome_side: {outcome_side}. Must be 'yes' or 'no'.")

        if action_lower == "buy":
            order_action = OrderAction.BUY
        elif action_lower == "sell":
            order_action = OrderAction.SELL
        else:
            raise ValueError(f"Invalid action: {action}. Must be 'buy' or 'sell'.")

        if order_exists(client_order_id):
            logger.warning(
                f"Duplicate order detected (client_order_id={client_order_id}). "
                f"Skipping to prevent double execution."
            )
            _audit.log(
                "order_duplicate_skipped", ticker, action_lower,
                outcome_side=outcome_side_lower,
                client_order_id=client_order_id,
            )
            return {
                "order_id": f"dup-{client_order_id}",
                "ticker": ticker,
                "status": "skipped_duplicate",
                "client_order_id": client_order_id,
            }

        store_order(
            client_order_id=client_order_id,
            ticker=ticker,
            status="pending",
            action=action_lower,
            outcome_side=outcome_side_lower,
            price=float(price),
            quantity=float(quantity),
            signal_id=signal_id,
        )

        order = self._order_machine.create_order(
            ticker=ticker,
            side=order_side,
            action=order_action,
            price=price,
            quantity=quantity,
            client_order_id=client_order_id,
            time_in_force=time_in_force,
            order_type=type_opts,
        )

        payload = {
            "ticker": ticker,
            "action": action_lower,
            "type": type_opts,
            "price": price_str,
            "count": qty_str,
            "outcome_side": outcome_side_lower,
            "book_side": book_side,
            "client_order_id": client_order_id,
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
        }

        self._order_machine.transition(order.id, OrderState.SUBMITTED)

        _effective_shadow = self._shadow_mode_override if self._shadow_mode_override is not None else Config.SHADOW_MODE

        if _effective_shadow:
            self.handle_order_fill(price, quantity, is_buy=(action_lower == "buy"))

            _get_shadow_logger().info(json.dumps({
                "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
                "ticker": ticker,
                "action": action_lower,
                "price": price_str,
                "quantity": qty_str,
                "outcome_side": outcome_side_lower,
                "synthetic_ask": float(synthetic_ask) if synthetic_ask is not None else None,
                "fee_accumulator": float(self.fee_tracker.accumulator),
            }))

            log_shadow_trade(
                ticker=ticker,
                timestamp=datetime.now(UTC).isoformat(timespec="seconds"),
                action=action_lower,
                outcome_side=outcome_side_lower,
                price=float(price),
                quantity=float(quantity),
                synthetic_ask=float(synthetic_ask) if synthetic_ask is not None else None,
                proposed_kelly=float(proposed_kelly) if proposed_kelly is not None else None,
                final_wager=float(final_wager) if final_wager is not None else None,
                fee_accumulator=float(self.fee_tracker.accumulator),
                release_id=release_id,
            )

            self._order_machine.transition(order.id, OrderState.FILLED)
            self._position_manager.on_order_fill(
                ticker=ticker,
                side=order_side,
                action=order_action,
                fill_qty=quantity,
                fill_price=price,
                fee=Decimal("0"),
                rebate=Decimal("0"),
            )

            update_order_status(client_order_id, "filled")
            _audit.log_order_placed(
                ticker, action_lower, outcome_side_lower,
                price=float(price), quantity=float(quantity),
                client_order_id=client_order_id,
            )
            record_shadow_trade(ticker, outcome_side_lower)
            record_order_placed(ticker, outcome_side_lower, action_lower, "filled")
            record_order_latency(ticker, outcome_side_lower, time.time() - start_time)

            logger.info(
                f"[SHADOW MODE] Intercepted order for {ticker}. Payload logged to shadow_trades.log & SQLite."
            )

            return {
                "order_id": f"shadow-{uuid.uuid4()}",
                "ticker": ticker,
                "action": action_lower,
                "type": type_opts,
                "price": price_str,
            "count": qty_int,
                "outcome_side": outcome_side_lower,
                "book_side": book_side,
                "status": "filled",
                "client_order_id": client_order_id,
            }

        path = f"/trade-api/{Config.API_VERSION}/portfolio/orders"
        url = f"{self.base_url}{path}"
        headers = self.sign_headers("POST", path)

        logger.info(
            f"Placing live {action_lower} order: outcome={outcome_side_lower} ({book_side}) "
            f"qty={qty_str} @ price={price_str} for {ticker}"
        )

        try:
            response = self._place_order_with_resilience(path, url, headers, payload)

            if response.status_code not in (200, 201):
                error_msg = response.text
                self._order_machine.transition(order.id, OrderState.REJECTED, error_msg)
                record_order_rejected(ticker, outcome_side_lower, "api_error")
                update_order_status(client_order_id, "rejected", error_message=error_msg)
                _audit.log_order_rejected(
                    ticker, action_lower, outcome_side_lower,
                    reason=error_msg, client_order_id=client_order_id,
                )
                logger.error(f"Failed to place order: {error_msg}")
                raise RuntimeError(f"API Error placing order: {error_msg}")

            order_data = response.json()
            kalshi_order_id = order_data.get("order_id")
            order.kalshi_order_id = kalshi_order_id
            order.api_status = order_data.get("status", "submitted")

            if order.api_status == "filled":
                self._order_machine.transition(order.id, OrderState.FILLED)
                self._position_manager.on_order_fill(
                    ticker=ticker, side=order_side, action=order_action,
                    fill_qty=quantity, fill_price=price,
                    fee=Decimal("0"), rebate=Decimal("0"),
                )
                update_order_status(client_order_id, "filled", kalshi_order_id=kalshi_order_id)
            else:
                update_order_status(client_order_id, order.api_status, kalshi_order_id=kalshi_order_id)
            _audit.log_order_placed(
                ticker, action_lower, outcome_side_lower,
                price=float(price), quantity=float(quantity),
                order_id=kalshi_order_id, client_order_id=client_order_id,
            )
            record_order_placed(ticker, outcome_side_lower, action_lower, order.api_status)
            record_order_latency(ticker, outcome_side_lower, time.time() - start_time)

            logger.info(f"Order successfully placed: {kalshi_order_id}")
            return order_data

        except Exception as e:
            self._order_machine.transition(order.id, OrderState.REJECTED, str(e))
            record_order_rejected(ticker, outcome_side_lower, "exception")
            update_order_status(client_order_id, "failed", error_message=str(e))

            self._dlq.add(payload, str(e))
            logger.error(f"Order placement failed, added to DLQ: {e}")
            raise

    def handle_order_fill(self, price: Decimal, quantity: Decimal, is_buy: bool = True) -> dict:
        result = self.fee_tracker.record_fill(price, quantity, is_buy)
        logger.info(
            f"Processed fill of {quantity:.2f} contracts @ ${price:.4f}. "
            f"Exact cost: ${result['actual_value']:.6f}, Rounded: ${result['rounded_value']:.2f}, "
            f"Overpayment: ${result['overpayment']:.6f}, Rebate: ${result['rebate_received']:.2f}"
        )
        return result

    def get_order_state_machine(self) -> OrderStateMachine:
        return self._order_machine

    def get_position_manager(self):
        return self._position_manager
