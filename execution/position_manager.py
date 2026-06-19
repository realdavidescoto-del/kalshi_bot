import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from config import Config
from data.database import get_orders_for_position, update_signal_profitability
from execution.order_state import (
    OrderAction,
    OrderSide,
    get_order_state_machine,
)

logger = logging.getLogger("kalshi_bot.position_manager")


@dataclass
class Position:
    ticker: str
    side: OrderSide
    quantity: Decimal = Decimal("0")
    avg_entry_price: Decimal = Decimal("0")
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    total_fees: Decimal = Decimal("0")
    total_rebates: Decimal = Decimal("0")
    created_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    updated_at: float = field(default_factory=lambda: datetime.now(UTC).timestamp())
    highest_price: Decimal = Decimal("0")
    take_profit_tier_1_done: bool = False
    take_profit_tier_2_done: bool = False
    pending_exit: bool = False

    def update_on_fill(self, fill_qty: Decimal, fill_price: Decimal, fee: Decimal, rebate: Decimal):
        if fill_qty == 0:
            return

        if self.quantity == 0:
            self.avg_entry_price = fill_price
            self.quantity = fill_qty
            self.highest_price = fill_price
        else:
            total_cost = self.avg_entry_price * self.quantity + fill_price * fill_qty
            self.quantity += fill_qty
            self.avg_entry_price = total_cost / self.quantity
            if fill_price > self.highest_price:
                self.highest_price = fill_price

        self.total_fees += fee
        self.total_rebates += rebate
        self.updated_at = datetime.now(UTC).timestamp()

    def close_partial(
        self, close_qty: Decimal, close_price: Decimal, fee: Decimal, rebate: Decimal
    ) -> Decimal:
        if close_qty > self.quantity:
            close_qty = self.quantity

        pnl = (close_price - self.avg_entry_price) * close_qty
        self.realized_pnl += pnl - fee + rebate
        self.quantity -= close_qty
        self.total_fees += fee
        self.total_rebates += rebate
        self.pending_exit = False
        self.updated_at = datetime.now(UTC).timestamp()

        if self.quantity == 0:
            self.avg_entry_price = Decimal("0")

        return pnl

    def update_unrealized(self, current_price: Decimal):
        if self.quantity == 0:
            self.unrealized_pnl = Decimal("0")
            return

        self.unrealized_pnl = (current_price - self.avg_entry_price) * self.quantity
        if current_price > self.highest_price:
            self.highest_price = current_price

    @property
    def total_pnl(self) -> Decimal:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def net_fees(self) -> Decimal:
        return self.total_fees - self.total_rebates

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "side": self.side.value,
            "quantity": str(self.quantity),
            "avg_entry_price": str(self.avg_entry_price),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "total_pnl": str(self.total_pnl),
            "total_fees": str(self.total_fees),
            "total_rebates": str(self.total_rebates),
            "net_fees": str(self.net_fees),
            "created_at": datetime.fromtimestamp(self.created_at, tz=UTC).isoformat(),
            "updated_at": datetime.fromtimestamp(self.updated_at, tz=UTC).isoformat(),
        }


class PositionManager:
    def __init__(self):
        self._positions: dict[str, Position] = {}
        self._lock = threading.RLock()
        self._order_machine = get_order_state_machine()

    def _get_position_key(self, ticker: str, side: OrderSide) -> str:
        return f"{ticker}:{side.value}"

    def on_order_fill(
        self,
        ticker: str,
        side: OrderSide,
        action: OrderAction,
        fill_qty: Decimal,
        fill_price: Decimal,
        fee: Decimal,
        rebate: Decimal,
    ):
        key = self._get_position_key(ticker, side)

        with self._lock:
            if key not in self._positions:
                self._positions[key] = Position(ticker=ticker, side=side)

            position = self._positions[key]

            if action == OrderAction.BUY:
                position.update_on_fill(fill_qty, fill_price, fee, rebate)
            else:
                position.close_partial(fill_qty, fill_price, fee, rebate)

                if position.quantity == 0:
                    orders = get_orders_for_position(ticker, side.value.lower())
                    for o in orders:
                        sig_id = o.get("signal_id")
                        if sig_id is not None:
                            update_signal_profitability(sig_id, position.realized_pnl > 0)

            logger.info(
                f"Position updated: {ticker} {side.value} qty={position.quantity} "
                f"avg_entry={position.avg_entry_price} realized_pnl={position.realized_pnl} "
                f"unrealized_pnl={position.unrealized_pnl}"
            )

    def update_mark_prices(self, prices: dict[str, dict[OrderSide, Decimal]]):
        with self._lock:
            for _key, position in self._positions.items():
                ticker = position.ticker
                side = position.side
                if ticker in prices and side in prices[ticker]:
                    position.update_unrealized(prices[ticker][side])

    def get_position(self, ticker: str, side: OrderSide) -> Position | None:
        with self._lock:
            return self._positions.get(self._get_position_key(ticker, side))

    def get_all_positions(self) -> list[Position]:
        with self._lock:
            return list(self._positions.values())

    def get_open_positions(self) -> list[Position]:
        with self._lock:
            return [p for p in self._positions.values() if p.quantity > 0]

    def get_total_realized_pnl(self) -> Decimal:
        with self._lock:
            return sum(p.realized_pnl for p in self._positions.values())

    def get_total_unrealized_pnl(self) -> Decimal:
        with self._lock:
            return sum(p.unrealized_pnl for p in self._positions.values())

    def get_total_pnl(self) -> Decimal:
        return self.get_total_realized_pnl() + self.get_total_unrealized_pnl()

    def get_sector_exposure(self, sector: str, ticker_to_sector: dict[str, str]) -> Decimal:
        with self._lock:
            exposure = Decimal("0")
            for position in self._positions.values():
                if ticker_to_sector.get(position.ticker) == sector and position.quantity > 0:
                    exposure += position.quantity * position.avg_entry_price
            return exposure

    def sync_from_api_positions(
        self,
        api_positions: list[dict],
        ticker_to_price: dict[str, dict[OrderSide, Decimal]],
    ):
        with self._lock:
            self._positions.clear()
            for pos in api_positions:
                ticker = pos.get("ticker", "")
                side_str = pos.get("side", "")
                if side_str.lower() == "yes":
                    side = OrderSide.YES
                elif side_str.lower() == "no":
                    side = OrderSide.NO
                else:
                    continue
                key = self._get_position_key(ticker, side)
                position = Position(
                    ticker=ticker,
                    side=side,
                    quantity=Decimal(str(pos.get("count", 0))),
                    avg_entry_price=Decimal(str(pos.get("average_price", 0))),
                )
                if ticker in ticker_to_price and side in ticker_to_price[ticker]:
                    position.update_unrealized(ticker_to_price[ticker][side])
                self._positions[key] = position
            logger.info(f"Reconciled {len(self._positions)} positions from Kalshi API")

    def get_total_exposure(self) -> Decimal:
        with self._lock:
            return sum(
                p.quantity * p.avg_entry_price for p in self._positions.values() if p.quantity > 0
            )

    def get_positions_for_stop_loss(self, threshold_pct: Decimal) -> list[Position]:
        with self._lock:
            triggered = []
            for p in self._positions.values():
                if p.quantity <= 0:
                    continue
                loss_pct = (
                    p.unrealized_pnl / (p.quantity * p.avg_entry_price)
                    if p.avg_entry_price > 0
                    else Decimal("0")
                )
                if loss_pct < -threshold_pct:
                    triggered.append(p)
            return triggered

    def get_trailing_stop_positions(self, trail_pct: Decimal) -> list[Position]:
        with self._lock:
            triggered = []
            for p in self._positions.values():
                if p.quantity <= 0 or p.highest_price <= Decimal("0"):
                    continue
                current_price = p.avg_entry_price + (p.unrealized_pnl / p.quantity) if p.quantity > 0 else p.avg_entry_price
                drawdown_pct = (p.highest_price - current_price) / p.highest_price
                if drawdown_pct > trail_pct:
                    triggered.append(p)
            return triggered

    def get_take_profit_positions(self) -> list[tuple[Position, str]]:
        with self._lock:
            tier_hits = []
            for p in self._positions.values():
                if p.quantity <= 0 or p.avg_entry_price <= Decimal("0"):
                    continue
                current_price = p.avg_entry_price + (p.unrealized_pnl / p.quantity) if p.quantity > 0 else p.avg_entry_price
                gain_pct = (current_price - p.avg_entry_price) / p.avg_entry_price
                tp_tier1 = Config.TAKE_PROFIT_TIER_1_MULTIPLIER
                tp_tier2 = Config.TAKE_PROFIT_TIER_2_MULTIPLIER
                if gain_pct >= tp_tier2 and not p.take_profit_tier_2_done:
                    tier_hits.append((p, "tier_2"))
                elif gain_pct >= tp_tier1 and not p.take_profit_tier_1_done:
                    tier_hits.append((p, "tier_1"))
            return tier_hits

    def mark_take_profit_tier(self, position: Position, tier: str):
        with self._lock:
            key = self._get_position_key(position.ticker, position.side)
            pos = self._positions.get(key)
            if pos:
                if tier == "tier_1":
                    pos.take_profit_tier_1_done = True
                elif tier == "tier_2":
                    pos.take_profit_tier_2_done = True

    def set_pending_exit(self, ticker: str, side: OrderSide, pending: bool):
        with self._lock:
            key = self._get_position_key(ticker, side)
            pos = self._positions.get(key)
            if pos:
                pos.pending_exit = pending

    def has_pending_exit(self, ticker: str, side: OrderSide) -> bool:
        with self._lock:
            key = self._get_position_key(ticker, side)
            pos = self._positions.get(key)
            return pos.pending_exit if pos else False

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "total_positions": len(self._positions),
                "open_positions": len([p for p in self._positions.values() if p.quantity > 0]),
                "total_realized_pnl": str(self.get_total_realized_pnl()),
                "total_unrealized_pnl": str(self.get_total_unrealized_pnl()),
                "total_pnl": str(self.get_total_pnl()),
                "positions": {k: v.to_dict() for k, v in self._positions.items()},
            }


_position_manager = None
_position_manager_lock = threading.Lock()


def get_position_manager() -> PositionManager:
    global _position_manager
    with _position_manager_lock:
        if _position_manager is None:
            _position_manager = PositionManager()
        return _position_manager
