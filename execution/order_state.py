import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum

logger = logging.getLogger("kalshi_bot.order_state")


class OrderState(Enum):
    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class OrderSide(Enum):
    YES = "yes"
    NO = "no"


class OrderAction(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    id: str
    client_order_id: str
    ticker: str
    side: OrderSide
    action: OrderAction
    price: Decimal
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")
    state: OrderState = OrderState.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    submitted_at: float | None = None
    filled_at: float | None = None
    error_message: str | None = None
    kalshi_order_id: str | None = None
    api_status: str | None = None
    time_in_force: str = "good_till_canceled"
    type: str = "limit"

    def can_transition_to(self, new_state: OrderState) -> bool:
        valid_transitions = {
            OrderState.PENDING: {
                OrderState.SUBMITTED,
                OrderState.REJECTED,
                OrderState.CANCELLED,
            },
            OrderState.SUBMITTED: {
                OrderState.PARTIAL,
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.REJECTED,
                OrderState.EXPIRED,
            },
            OrderState.PARTIAL: {
                OrderState.FILLED,
                OrderState.CANCELLED,
                OrderState.EXPIRED,
            },
            OrderState.FILLED: set(),
            OrderState.CANCELLED: set(),
            OrderState.REJECTED: set(),
            OrderState.EXPIRED: set(),
        }
        return new_state in valid_transitions.get(self.state, set())

    def transition_to(
        self, new_state: OrderState, error_message: str | None = None
    ) -> bool:
        if not self.can_transition_to(new_state):
            logger.warning(
                f"Invalid state transition for order {self.id}: {self.state} -> {new_state}"
            )
            return False

        self.state = new_state
        self.updated_at = time.time()
        if error_message:
            self.error_message = error_message

        if new_state == OrderState.SUBMITTED:
            self.submitted_at = time.time()
        elif new_state in (OrderState.FILLED, OrderState.PARTIAL):
            self.filled_at = time.time()

        logger.info(f"Order {self.id} transitioned to {new_state.value}")
        return True

    def fill(self, quantity: Decimal, price: Decimal | None = None) -> Decimal:
        if self.state not in (OrderState.SUBMITTED, OrderState.PARTIAL):
            raise ValueError(f"Cannot fill order in state {self.state}")

        fill_qty = min(quantity, self.quantity - self.filled_quantity)
        self.filled_quantity += fill_qty
        self.updated_at = time.time()

        if self.filled_quantity >= self.quantity:
            self.transition_to(OrderState.FILLED)
        else:
            self.transition_to(OrderState.PARTIAL)

        if price:
            self.price = price

        logger.info(
            f"Order {self.id} filled {fill_qty} @ {self.price}, total filled: {self.filled_quantity}"
        )
        return fill_qty

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_order_id": self.client_order_id,
            "ticker": self.ticker,
            "side": self.side.value,
            "action": self.action.value,
            "price": str(self.price),
            "quantity": str(self.quantity),
            "filled_quantity": str(self.filled_quantity),
            "state": self.state.value,
            "created_at": datetime.fromtimestamp(
                self.created_at, tz=UTC
            ).isoformat(),
            "updated_at": datetime.fromtimestamp(
                self.updated_at, tz=UTC
            ).isoformat(),
            "submitted_at": datetime.fromtimestamp(
                self.submitted_at, tz=UTC
            ).isoformat()
            if self.submitted_at
            else None,
            "filled_at": datetime.fromtimestamp(
                self.filled_at, tz=UTC
            ).isoformat()
            if self.filled_at
            else None,
            "error_message": self.error_message,
            "kalshi_order_id": self.kalshi_order_id,
            "api_status": self.api_status,
            "time_in_force": self.time_in_force,
            "type": self.type,
        }


class OrderStateMachine:
    def __init__(self):
        self._orders: dict[str, Order] = {}
        self._lock = threading.RLock()
        self._state_change_callbacks: dict[OrderState, list] = {
            state: [] for state in OrderState
        }

    def create_order(
        self,
        ticker: str,
        side: OrderSide,
        action: OrderAction,
        price: Decimal,
        quantity: Decimal,
        client_order_id: str | None = None,
        time_in_force: str = "good_till_canceled",
        order_type: str = "limit",
    ) -> Order:
        order_id = str(uuid.uuid4())
        cid = client_order_id or str(uuid.uuid4())

        order = Order(
            id=order_id,
            client_order_id=cid,
            ticker=ticker,
            side=side,
            action=action,
            price=price,
            quantity=quantity,
            time_in_force=time_in_force,
            type=order_type,
        )

        with self._lock:
            self._orders[order_id] = order

        logger.info(
            f"Created order {order_id} for {ticker} {side.value} {action.value} {quantity} @ {price}"
        )
        return order

    def get_order(self, order_id: str) -> Order | None:
        with self._lock:
            return self._orders.get(order_id)

    def get_order_by_client_id(self, client_order_id: str) -> Order | None:
        with self._lock:
            for order in self._orders.values():
                if order.client_order_id == client_order_id:
                    return order
        return None

    def transition(
        self, order_id: str, new_state: OrderState, error_message: str | None = None
    ) -> bool:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                logger.warning(f"Order {order_id} not found for state transition")
                return False

            success = order.transition_to(new_state, error_message)
            if success:
                self._trigger_callbacks(order)
            return success

    def fill_order(
        self, order_id: str, quantity: Decimal, price: Decimal | None = None
    ) -> Decimal:
        with self._lock:
            order = self._orders.get(order_id)
            if not order:
                raise ValueError(f"Order {order_id} not found")
            return order.fill(quantity, price)

    def cancel_order(self, order_id: str) -> bool:
        return self.transition(order_id, OrderState.CANCELLED)

    def reject_order(self, order_id: str, error_message: str) -> bool:
        return self.transition(order_id, OrderState.REJECTED, error_message)

    def get_orders_by_state(self, state: OrderState) -> list:
        with self._lock:
            return [o for o in self._orders.values() if o.state == state]

    def get_open_orders(self) -> list:
        with self._lock:
            return [
                o
                for o in self._orders.values()
                if o.state
                in (OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL)
            ]

    def get_orders_for_ticker(self, ticker: str) -> list:
        with self._lock:
            return [o for o in self._orders.values() if o.ticker == ticker]

    def register_callback(self, state: OrderState, callback: Callable[[Order], None]):
        self._state_change_callbacks[state].append(callback)

    def _trigger_callbacks(self, order: Order):
        for callback in self._state_change_callbacks.get(order.state, []):
            try:
                callback(order)
            except Exception as e:
                logger.error(
                    f"Error in state change callback for order {order.id}: {e}"
                )

    def get_stats(self) -> dict:
        with self._lock:
            stats = {state.value: 0 for state in OrderState}
            for order in self._orders.values():
                stats[order.state.value] += 1
            return stats


_order_state_machine = None
_order_state_machine_lock = threading.Lock()


def get_order_state_machine() -> OrderStateMachine:
    global _order_state_machine
    with _order_state_machine_lock:
        if _order_state_machine is None:
            _order_state_machine = OrderStateMachine()
        return _order_state_machine
