import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger("kalshi_bot.rate_limiter")


def _update_rate_metric(name: str, tokens: float):
    try:
        from observability.metrics import update_rate_limiter_tokens as _fn
        _fn(name, tokens)
    except Exception:
        pass


@dataclass
class RateLimitConfig:
    requests_per_second: float = 10.0
    burst_size: int = 20
    retry_after_header: bool = True


class TokenBucketRateLimiter:
    def __init__(self, name: str, config: RateLimitConfig | None = None):
        self.name = name
        self.config = config or RateLimitConfig()
        self._tokens = float(self.config.burst_size)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()
        self._wait_count = 0
        self._total_wait_time = 0.0

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed * self.config.requests_per_second
        self._tokens = min(self.config.burst_size, self._tokens + new_tokens)
        self._last_refill = now
        _update_rate_metric(self.name, self._tokens)

    def acquire(self, tokens: int = 1, timeout: float | None = None) -> bool:
        start_time = time.monotonic()

        while True:
            with self._lock:
                self._refill()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    wait_time = time.monotonic() - start_time
                    self._wait_count += 1
                    self._total_wait_time += wait_time
                    return True

            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    return False

            sleep_time = min(0.1, tokens / self.config.requests_per_second)
            time.sleep(sleep_time)

    def try_acquire(self, tokens: int = 1) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def get_available_tokens(self) -> float:
        with self._lock:
            self._refill()
            return self._tokens

    def get_stats(self) -> dict:
        with self._lock:
            self._refill()
            return {
                "name": self.name,
                "available_tokens": self._tokens,
                "capacity": self.config.burst_size,
                "refill_rate": self.config.requests_per_second,
                "wait_count": self._wait_count,
                "total_wait_time": self._total_wait_time,
                "avg_wait_time": self._total_wait_time / self._wait_count
                if self._wait_count > 0
                else 0,
            }


class MultiTierRateLimiter:
    def __init__(self):
        self._limiters: dict[str, TokenBucketRateLimiter] = {}
        self._lock = threading.Lock()
        self._global_config = RateLimitConfig()

    def configure_tier(self, name: str, config: RateLimitConfig):
        with self._lock:
            self._limiters[name] = TokenBucketRateLimiter(name, config)

    def get_limiter(self, name: str) -> TokenBucketRateLimiter:
        with self._lock:
            if name not in self._limiters:
                self._limiters[name] = TokenBucketRateLimiter(name, self._global_config)
            return self._limiters[name]

    def acquire(
        self, tier: str, tokens: int = 1, timeout: float | None = None
    ) -> bool:
        limiter = self.get_limiter(tier)
        return limiter.acquire(tokens, timeout)

    def try_acquire(self, tier: str, tokens: int = 1) -> bool:
        limiter = self.get_limiter(tier)
        return limiter.try_acquire(tokens)

    def get_all_stats(self) -> dict:
        with self._lock:
            return {
                name: limiter.get_stats() for name, limiter in self._limiters.items()
            }

    def handle_retry_after(self, tier: str, retry_after: float):
        limiter = self.get_limiter(tier)
        with limiter._lock:
            limiter._tokens = 0
            limiter._last_refill = time.monotonic() + retry_after
        logger.warning(
            f"Rate limiter '{tier}' backing off for {retry_after}s due to 429 response"
        )


_rate_limiter_registry = MultiTierRateLimiter()


def get_rate_limiter() -> MultiTierRateLimiter:
    return _rate_limiter_registry


def configure_default_tiers():
    _rate_limiter_registry.configure_tier(
        "rest_api", RateLimitConfig(requests_per_second=10.0, burst_size=20)
    )
    _rate_limiter_registry.configure_tier(
        "websocket", RateLimitConfig(requests_per_second=5.0, burst_size=10)
    )
    _rate_limiter_registry.configure_tier(
        "fred_api", RateLimitConfig(requests_per_second=2.0, burst_size=5)
    )
