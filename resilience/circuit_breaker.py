import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

logger = logging.getLogger("kalshi_bot.circuit_breaker")


def _update_cb_metric(name: str, state: str):
    try:
        from observability.metrics import update_circuit_breaker_state as _fn
        _fn(name, state)
    except Exception:
        pass


class CircuitBreakerState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    success_threshold: int = 2
    timeout_seconds: float = 30.0
    excluded_exceptions: tuple = ()


@dataclass
class CircuitBreakerStats:
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_failure_time: float | None = None
    last_success_time: float | None = None
    state_changes: int = 0


class CircuitBreaker:
    def __init__(self, name: str, config: CircuitBreakerConfig | None = None):
        self.name = name
        self.config = config or CircuitBreakerConfig()
        self._state = CircuitBreakerState.CLOSED
        self._stats = CircuitBreakerStats()
        self._lock = threading.RLock()
        self._last_state_change = time.time()

    @property
    def state(self) -> CircuitBreakerState:
        with self._lock:
            if self._state == CircuitBreakerState.OPEN:
                if time.time() - self._last_state_change >= self.config.timeout_seconds:
                    self._transition_to_half_open()
            return self._state

    def _transition_to_open(self):
        self._state = CircuitBreakerState.OPEN
        self._last_state_change = time.time()
        self._stats.state_changes += 1
        _update_cb_metric(self.name, "open")
        logger.warning(f"Circuit breaker '{self.name}' transitioned to OPEN")

    def _transition_to_half_open(self):
        self._state = CircuitBreakerState.HALF_OPEN
        self._last_state_change = time.time()
        self._stats.state_changes += 1
        self._stats.consecutive_successes = 0
        _update_cb_metric(self.name, "half_open")
        logger.info(f"Circuit breaker '{self.name}' transitioned to HALF_OPEN")

    def _transition_to_closed(self):
        self._state = CircuitBreakerState.CLOSED
        self._last_state_change = time.time()
        self._stats.state_changes += 1
        self._stats.consecutive_failures = 0
        self._stats.consecutive_successes = 0
        _update_cb_metric(self.name, "closed")
        logger.info(f"Circuit breaker '{self.name}' transitioned to CLOSED")

    def call(self, func: Callable[..., Any], *args, bypass=False, **kwargs) -> Any:
        if bypass:
            return func(*args, **kwargs)
        if self.state == CircuitBreakerState.OPEN:
            raise CircuitBreakerOpenError(f"Circuit breaker '{self.name}' is OPEN")

        with self._lock:
            self._stats.total_calls += 1

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.config.excluded_exceptions:
            raise
        except Exception:
            self._on_failure()
            raise

    def _on_success(self):
        with self._lock:
            self._stats.successful_calls += 1
            self._stats.consecutive_failures = 0
            self._stats.consecutive_successes += 1
            self._stats.last_success_time = time.time()

            if self._state == CircuitBreakerState.HALF_OPEN:
                if self._stats.consecutive_successes >= self.config.success_threshold:
                    self._transition_to_closed()

    def _on_failure(self):
        with self._lock:
            self._stats.failed_calls += 1
            self._stats.consecutive_failures += 1
            self._stats.consecutive_successes = 0
            self._stats.last_failure_time = time.time()

            if self._state == CircuitBreakerState.HALF_OPEN:
                self._transition_to_open()
            elif self._state == CircuitBreakerState.CLOSED:
                if self._stats.consecutive_failures >= self.config.failure_threshold:
                    self._transition_to_open()

    def record_failure(self):
        with self._lock:
            self._stats.failed_calls += 1
            self._stats.consecutive_failures += 1
            self._stats.consecutive_successes = 0
            self._stats.last_failure_time = time.time()
            if self._state == CircuitBreakerState.HALF_OPEN:
                self._transition_to_open()
            elif self._state == CircuitBreakerState.CLOSED:
                if self._stats.consecutive_failures >= self.config.failure_threshold:
                    self._transition_to_open()

    def get_stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state.value,
                "total_calls": self._stats.total_calls,
                "successful_calls": self._stats.successful_calls,
                "failed_calls": self._stats.failed_calls,
                "consecutive_failures": self._stats.consecutive_failures,
                "consecutive_successes": self._stats.consecutive_successes,
                "last_failure_time": self._stats.last_failure_time,
                "last_success_time": self._stats.last_success_time,
                "state_changes": self._stats.state_changes,
            }

    def reset(self):
        with self._lock:
            self._state = CircuitBreakerState.CLOSED
            self._stats = CircuitBreakerStats()
            self._last_state_change = time.time()
            logger.info(f"Circuit breaker '{self.name}' manually reset")


class CircuitBreakerOpenError(Exception):
    pass


class CircuitBreakerRegistry:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._breakers = {}
        return cls._instance

    def get_or_create(
        self, name: str, config: CircuitBreakerConfig | None = None
    ) -> CircuitBreaker:
        with self._lock:
            if name not in self._breakers:
                self._breakers[name] = CircuitBreaker(name, config)
            return self._breakers[name]

    def get(self, name: str) -> CircuitBreaker | None:
        with self._lock:
            return self._breakers.get(name)

    def get_all_stats(self) -> dict:
        with self._lock:
            return {name: cb.get_stats() for name, cb in self._breakers.items()}

    def reset_all(self):
        with self._lock:
            for cb in self._breakers.values():
                cb.reset()
