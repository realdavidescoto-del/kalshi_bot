import logging
import time
from decimal import Decimal

from config import Config, sign_kalshi_headers
from data.audit_log import get_audit_logger
from resilience.circuit_breaker import CircuitBreakerConfig, CircuitBreakerRegistry
from resilience.rate_limiter import get_rate_limiter

logger = logging.getLogger("kalshi_bot.kill_switch")


class KillSwitch:
    def __init__(self):
        Config.validate()
        self.api_key_id = Config.API_KEY_ID
        self.private_key = Config.get_private_key()
        self.base_url = Config.get_rest_url()
        self._last_checked_balance = Decimal("0")
        self._last_check_time = 0.0
        self._BALANCE_CACHE_TTL = 5.0
        self._circuit_breaker = CircuitBreakerRegistry().get_or_create(
            "kill_switch_api",
            CircuitBreakerConfig(failure_threshold=5, timeout_seconds=30.0),
        )
        self._rate_limiter = get_rate_limiter().get_limiter("rest_api")

    def sign_headers(self, method: str, path: str) -> dict:
        return sign_kalshi_headers(self.api_key_id, self.private_key, method, path)

    def get_balance(self) -> Decimal:
        def _do_request():
            if not self._rate_limiter.acquire(timeout=30.0):
                raise RuntimeError("Rate limiter timeout")

            path = f"/trade-api/{Config.API_VERSION}/portfolio/balance"
            url = f"{self.base_url}{path}"
            headers = self.sign_headers("GET", path)

            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="GET",
                url=url,
                headers=headers,
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )
            if response.status_code != 200:
                logger.error(f"Failed to fetch portfolio balance: {response.text}")
                raise RuntimeError(f"API Error fetching balance: {response.text}")

            data = response.json()

            if "balance_dollars" in data:
                return Decimal(data["balance_dollars"])

            raise ValueError(f"Kalshi API response missing 'balance_dollars': {data}")

        return self._circuit_breaker.call(_do_request)

    def cancel_all_orders(self) -> int:
        from observability.metrics import record_kill_switch_triggered
        record_kill_switch_triggered()

        def _do_cancel():
            logger.warning("Initiating platform-wide order cancellation...")
            get_audit_logger().log_kill_switch("manual_or_triggered_cancel")

            if not self._rate_limiter.acquire(timeout=30.0):
                raise RuntimeError("Rate limiter timeout")

            api_prefix = f"/trade-api/{Config.API_VERSION}"
            path = f"{api_prefix}/portfolio/orders?status=resting"
            url = f"{self.base_url}{path}"
            headers = self.sign_headers("GET", path)

            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="GET",
                url=url,
                headers=headers,
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )
            if response.status_code != 200:
                logger.error(f"Failed to fetch resting orders: {response.text}")
                raise RuntimeError(f"API Error listing orders: {response.text}")

            data = response.json()
            orders = data.get("orders", [])

            if not orders:
                logger.info("No resting orders found on the platform.")
                return 0

            logger.info(f"Found {len(orders)} resting orders to cancel.")
            cancelled_count = 0

            for order in orders:
                order_id = order.get("order_id")
                if not order_id:
                    continue

                if not self._rate_limiter.acquire(timeout=30.0):
                    logger.error("Rate limiter timeout during cancel, aborting further cancellations")
                    break

                cancel_path = f"{api_prefix}/portfolio/orders/{order_id}"
                cancel_url = f"{self.base_url}{cancel_path}"
                cancel_headers = self.sign_headers("DELETE", cancel_path)

                cancel_resp = Config.request_with_retry(
                    method="DELETE",
                    url=cancel_url,
                    headers=cancel_headers,
                    session=session,
                    timeout=Config.REQUEST_TIMEOUT_SEC,
                )
                if cancel_resp.status_code in (200, 204):
                    logger.info(f"Successfully cancelled order {order_id}")
                    cancelled_count += 1
                else:
                    logger.error(f"Failed to cancel order {order_id}: {cancel_resp.text}")

            logger.warning(
                f"Kill Switch action completed. Cancelled {cancelled_count} out of {len(orders)} orders."
            )
            return cancelled_count

        return self._circuit_breaker.call(_do_cancel, bypass=True)

    def check_and_trigger_with_capital(self) -> tuple:
        try:
            balance = self.get_cached_balance()
            self._last_check_time = time.time()

            logger.info(
                f"Current available balance: ${balance:.2f} (Limit: ${Config.KILL_SWITCH_MIN_BALANCE:.2f})"
            )

            if balance < Config.KILL_SWITCH_MIN_BALANCE:
                logger.critical(
                    f"BALANCE CRITICAL: ${balance:.2f} is below limit of ${Config.KILL_SWITCH_MIN_BALANCE:.2f}! "
                    f"TRIGGERING KILL SWITCH!"
                )
                self.cancel_all_orders()
                return True, balance

            return False, balance
        except Exception as e:
            logger.error(f"Error during Kill Switch check: {e}")
            logger.critical("FORCING ORDER CANCELLATION DUE TO MONITOR FAILURE!")
            self.cancel_all_orders()
            return True, Decimal("0")

    def get_positions(self) -> list[dict]:
        def _do_request():
            if not self._rate_limiter.acquire(timeout=30.0):
                raise RuntimeError("Rate limiter timeout")

            path = f"/trade-api/{Config.API_VERSION}/portfolio/positions"
            url = f"{self.base_url}{path}"
            headers = self.sign_headers("GET", path)
            session = Config.get_verified_session()
            response = Config.request_with_retry(
                method="GET",
                url=url,
                headers=headers,
                session=session,
                timeout=Config.REQUEST_TIMEOUT_SEC,
            )
            if response.status_code != 200:
                logger.error(f"Failed to fetch positions: {response.text}")
                return []
            return response.json().get("positions", [])

        return self._circuit_breaker.call(_do_request)

    def check_and_trigger(self) -> bool:
        triggered, _ = self.check_and_trigger_with_capital()
        return triggered

    def get_cached_balance(self) -> Decimal:
        now = time.time()
        if now - self._last_check_time > self._BALANCE_CACHE_TTL:
            self._last_checked_balance = self.get_balance()
            self._last_check_time = now
        return self._last_checked_balance
