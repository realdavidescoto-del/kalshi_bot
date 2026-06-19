import base64
import json
import logging
import threading
import time
from collections.abc import Callable

import websocket
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from config import Config
from market.order_book import LocalOrderBook
from observability.metrics import (
    record_ws_latency,
    record_ws_message_received,
    record_ws_message_sent,
    record_ws_reconnection,
    record_ws_sequence_gap,
    update_ws_connection_count,
    update_ws_last_message_time,
)
from resilience.circuit_breaker import CircuitBreakerConfig, CircuitBreakerRegistry

logger = logging.getLogger("kalshi_bot.websocket")

_MAX_SNAPSHOT_SIZE = 100_000
_MAX_DELTA_SIZE = 10_000


class KalshiWebSocketClient:
    def __init__(
        self,
        ticker: str,
        order_book: LocalOrderBook,
        on_update_cb: Callable | None = None,
    ):
        Config.validate()
        self.ticker = ticker
        self.order_book = order_book
        self.on_update_cb = on_update_cb
        self.ws_url = Config.get_ws_url()
        self.ws: websocket.WebSocketApp | None = None
        self.thread: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.connected_event = threading.Event()
        self._sequence = 0
        self._last_sequence = 0
        self._last_message_time = 0.0
        self._last_ping_sent = 0.0

        self._circuit_breaker = CircuitBreakerRegistry().get_or_create(
            "websocket", CircuitBreakerConfig(failure_threshold=3, timeout_seconds=60.0)
        )

    def _get_auth_headers(self) -> list:
        timestamp = str(int(time.time() * 1000))
        path = f"/trade-api/{Config.API_VERSION}/ws"
        message = f"{timestamp}GET{path}".encode()

        private_key = Config.get_private_key()
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256(),
        )

        signature_b64 = base64.b64encode(signature).decode("utf-8")

        return [
            f"KALSHI-ACCESS-KEY: {Config.API_KEY_ID}",
            f"KALSHI-ACCESS-TIMESTAMP: {timestamp}",
            f"KALSHI-ACCESS-SIGNATURE: {signature_b64}",
        ]

    def connect(self):
        self.stop_event.clear()
        self.connected_event.clear()
        self._recreate_ws()
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _recreate_ws(self):
        headers = self._get_auth_headers()
        logger.info(f"{'Re' if self.ws else ''}Connecting to Kalshi WebSocket at {self.ws_url}...")
        self.ws = websocket.WebSocketApp(
            self.ws_url,
            header=headers,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

    def wait_for_connection(self, timeout: float = 10.0) -> bool:
        return self.connected_event.wait(timeout=timeout)

    def _run_loop(self):
        while not self.stop_event.is_set():
            try:
                self.ws.run_forever(ping_interval=10, ping_timeout=5)
            except Exception as e:
                logger.error(f"WebSocket execution error: {e}")
            if not self.stop_event.is_set():
                logger.info("Reconnecting WebSocket in 5 seconds...")
                time.sleep(5)
                record_ws_reconnection()
                self._recreate_ws()

    def disconnect(self):
        logger.info("Disconnecting WebSocket...")
        self.stop_event.set()
        if self.ws:
            self.ws.close()
        if self.thread:
            self.thread.join(timeout=2.0)
        update_ws_connection_count(0)

    def _on_open(self, ws):
        logger.info("WebSocket connection established. Subscribing...")
        self.connected_event.set()
        update_ws_connection_count(1)
        self.order_book.clear()

        sub_snapshot = {
            "id": 1,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_snapshot"], "market_ticker": self.ticker},
        }
        ws.send(json.dumps(sub_snapshot))
        sub_delta = {
            "id": 2,
            "cmd": "subscribe",
            "params": {"channels": ["orderbook_delta"], "market_ticker": self.ticker},
        }
        ws.send(json.dumps(sub_delta))
        record_ws_message_sent("subscribe")
        logger.info(f"Subscription messages sent for {self.ticker}")

    def _on_message(self, ws, message_str: str):
        try:
            now = time.time()
            self._last_message_time = now
            update_ws_last_message_time(now)
            if self._last_ping_sent > 0:
                record_ws_latency(now - self._last_ping_sent)

            if len(message_str) > _MAX_SNAPSHOT_SIZE:
                logger.warning(
                    f"Discarding oversized WebSocket message ({len(message_str)} bytes)"
                )
                return

            msg = json.loads(message_str)
            msg_type = msg.get("type")

            record_ws_message_received(msg_type or "unknown")

            if msg_type == "orderbook_snapshot":
                msg_content = msg.get("msg", {})
                if not isinstance(msg_content, dict):
                    logger.warning("Discarding snapshot with non-dict msg content")
                    return
                self._last_sequence = msg_content.get("sequence", 0)
                self.order_book.apply_snapshot(msg)
                if self.on_update_cb:
                    self.on_update_cb(self.order_book)
            elif msg_type == "orderbook_delta":
                msg_content = msg.get("msg", {})
                if not isinstance(msg_content, dict):
                    logger.warning("Discarding delta with non-dict msg content")
                    return
                if len(message_str) > _MAX_DELTA_SIZE:
                    logger.warning(
                        f"Discarding oversized delta message ({len(message_str)} bytes)"
                    )
                    return
                self._sequence = msg_content.get("sequence", 0)
                if self._last_sequence > 0 and self._sequence > self._last_sequence + 1:
                    gap = self._sequence - self._last_sequence - 1
                    logger.warning(
                        f"WebSocket sequence gap detected: {gap} messages missed"
                    )
                    record_ws_sequence_gap()
                self._last_sequence = self._sequence

                side = msg_content.get("side", "")
                price_str = msg_content.get("price_dollars")
                delta_str = msg_content.get("delta_fp")

                if not isinstance(side, str) or side.lower() not in ("yes", "no"):
                    logger.warning(f"Discarding delta with invalid side: {side}")
                    return
                if not isinstance(price_str, str) or not isinstance(delta_str, str):
                    logger.warning("Discarding delta with non-string price/delta")
                    return

                self.order_book.apply_delta(msg)
                if self.on_update_cb:
                    self.on_update_cb(self.order_book)
            elif msg_type == "subscribed":
                logger.info(f"Successfully subscribed: {msg}")
            elif msg_type == "unsubscribed":
                logger.info(f"Successfully unsubscribed: {msg}")
            elif msg_type == "error":
                logger.error(f"WebSocket server returned error: {msg}")
                self._circuit_breaker.record_failure()
            else:
                logger.debug(f"Received unknown message type: {msg_type}")
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON from WebSocket: {e}")
        except Exception as e:
            logger.error(f"Error handling WebSocket message: {e}")

    def _on_error(self, ws, error):
        message = str(error)
        logger.error(
            "WebSocket client error encountered: %s. "
            "If this is a 403/401, verify the API key, private key, and KALSHI_ENV settings.",
            message,
        )
        self._circuit_breaker.record_failure()

    def _on_close(self, ws, close_status_code, close_msg):
        logger.info(
            f"WebSocket connection closed. status={close_status_code}, msg={close_msg}"
        )
        self.connected_event.clear()
        update_ws_connection_count(0)
