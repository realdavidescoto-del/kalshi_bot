import logging
import threading
from decimal import Decimal

logger = logging.getLogger("kalshi_bot.order_book")

class LocalOrderBook:
    def __init__(self, max_depth: int = 200):
        self._lock = threading.Lock()
        self.max_depth = max_depth
        self.yes_bids: dict[Decimal, Decimal] = {}
        self.no_bids: dict[Decimal, Decimal] = {}
        self.market_ticker: str | None = None
        self.market_id: str | None = None

    def clear(self):
        with self._lock:
            self.yes_bids.clear()
            self.no_bids.clear()

    def apply_snapshot(self, snapshot_msg: dict):
        with self._lock:
            self.yes_bids.clear()
            self.no_bids.clear()
            msg_content = snapshot_msg.get("msg", {})
            self.market_ticker = msg_content.get("market_ticker")
            self.market_id = msg_content.get("market_id")
            for price_str, qty_str in msg_content.get("yes_dollars_fp", []):
                price = Decimal(price_str)
                qty = Decimal(qty_str)
                if qty > 0:
                    self.yes_bids[price] = qty
            for price_str, qty_str in msg_content.get("no_dollars_fp", []):
                price = Decimal(price_str)
                qty = Decimal(qty_str)
                if qty > 0:
                    self.no_bids[price] = qty
            self._prune_book()
            logger.info(
                f"Initialized order book snapshot for {self.market_ticker}. "
                f"YES Bids: {len(self.yes_bids)}, NO Bids: {len(self.no_bids)}"
            )

    def apply_delta(self, delta_msg: dict):
        with self._lock:
            msg_content = delta_msg.get("msg", {})
            side = msg_content.get("side", "").lower()
            price_str = msg_content.get("price_dollars")
            delta_str = msg_content.get("delta_fp")
            if not side or not price_str or not delta_str:
                return
            price = Decimal(price_str)
            delta = Decimal(delta_str)
            target_book = self.yes_bids if side == "yes" else self.no_bids
            existing_qty = target_book.get(price, Decimal("0.0"))
            new_qty = existing_qty + delta
            if new_qty <= Decimal("0.0"):
                target_book.pop(price, None)
            else:
                target_book[price] = new_qty
            self._prune_book()
            logger.debug(
                f"Applied delta for {self.market_ticker}: {side} bid @ {price_str} delta={delta_str} -> new_qty={new_qty}"
            )

    def _prune_book(self):
        if len(self.yes_bids) > self.max_depth:
            sorted_prices = sorted(self.yes_bids.keys(), reverse=True)
            for price in sorted_prices[self.max_depth:]:
                del self.yes_bids[price]
        if len(self.no_bids) > self.max_depth:
            sorted_prices = sorted(self.no_bids.keys(), reverse=True)
            for price in sorted_prices[self.max_depth:]:
                del self.no_bids[price]

    def get_yes_bids(self) -> list[tuple[Decimal, Decimal]]:
        with self._lock:
            return sorted(self.yes_bids.items(), key=lambda x: x[0], reverse=True)

    def get_yes_asks(self) -> list[tuple[Decimal, Decimal]]:
        with self._lock:
            asks = []
            for no_bid_price, qty in self.no_bids.items():
                yes_ask_price = Decimal("1.0000") - no_bid_price
                asks.append((yes_ask_price, qty))
            return sorted(asks, key=lambda x: x[0])

    def get_best_yes_bid(self) -> tuple[Decimal | None, Decimal | None]:
        with self._lock:
            bids = sorted(self.yes_bids.items(), key=lambda x: x[0], reverse=True)
            return bids[0] if bids else (None, None)

    def get_best_yes_ask(self) -> tuple[Decimal | None, Decimal | None]:
        with self._lock:
            asks = []
            for no_bid_price, qty in self.no_bids.items():
                yes_ask_price = Decimal("1.0000") - no_bid_price
                asks.append((yes_ask_price, qty))
            asks.sort(key=lambda x: x[0])
            return asks[0] if asks else (None, None)

    def get_no_bids(self) -> list[tuple[Decimal, Decimal]]:
        with self._lock:
            return sorted(self.no_bids.items(), key=lambda x: x[0], reverse=True)

    def get_no_asks(self) -> list[tuple[Decimal, Decimal]]:
        with self._lock:
            asks = []
            for yes_bid_price, qty in self.yes_bids.items():
                no_ask_price = Decimal("1.0000") - yes_bid_price
                asks.append((no_ask_price, qty))
            return sorted(asks, key=lambda x: x[0])

    def get_best_no_bid(self) -> tuple[Decimal | None, Decimal | None]:
        with self._lock:
            bids = sorted(self.no_bids.items(), key=lambda x: x[0], reverse=True)
            return bids[0] if bids else (None, None)

    def get_best_no_ask(self) -> tuple[Decimal | None, Decimal | None]:
        with self._lock:
            asks = []
            for yes_bid_price, qty in self.yes_bids.items():
                no_ask_price = Decimal("1.0000") - yes_bid_price
                asks.append((no_ask_price, qty))
            asks.sort(key=lambda x: x[0])
            return asks[0] if asks else (None, None)

    def get_yes_book(self) -> dict:
        with self._lock:
            return {
                "bids": sorted(self.yes_bids.items(), key=lambda x: x[0], reverse=True),
                "asks": sorted(
                    [(Decimal("1.0000") - p, q) for p, q in self.no_bids.items()],
                    key=lambda x: x[0],
                ),
            }

    def get_no_book(self) -> dict:
        with self._lock:
            return {
                "bids": sorted(self.no_bids.items(), key=lambda x: x[0], reverse=True),
                "asks": sorted(
                    [(Decimal("1.0000") - p, q) for p, q in self.yes_bids.items()],
                    key=lambda x: x[0],
                ),
            }
