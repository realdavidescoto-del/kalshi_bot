import os
import time

os.environ["KALSHI_TESTING"] = "1"

import sys
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from resilience.circuit_breaker import CircuitBreaker, CircuitBreakerConfig, CircuitBreakerOpenError, CircuitBreakerRegistry
from resilience.rate_limiter import TokenBucketRateLimiter, RateLimitConfig, MultiTierRateLimiter
from resilience.dead_letter_queue import DeadLetterQueue, DLQRegistry


class TestCircuitBreaker:
    def test_initial_state_closed(self):
        cb = CircuitBreaker("test")
        assert cb.state.value == "closed"

    def test_transitions_to_open_after_failures(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=3, timeout_seconds=60.0))

        def failing():
            raise ValueError("fail")

        for _ in range(3):
            with pytest.raises(ValueError):
                cb.call(failing)

        assert cb.state.value == "open"

    def test_raises_open_error_when_open(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout_seconds=60.0))

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))

        with pytest.raises(CircuitBreakerOpenError):
            cb.call(lambda: "should not reach")

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout_seconds=0.1))

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))

        assert cb.state.value == "open"
        time.sleep(0.15)
        assert cb.state.value == "half_open"

    def test_closes_after_successes_in_half_open(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(
            failure_threshold=1, success_threshold=2, timeout_seconds=0.1
        ))

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))

        time.sleep(0.15)
        assert cb.state.value == "half_open"

        cb.call(lambda: "ok")
        assert cb.state.value == "half_open"

        cb.call(lambda: "ok again")
        assert cb.state.value == "closed"

    def test_excluded_exceptions_do_not_count(self):
        class CustomError(Exception):
            pass

        cb = CircuitBreaker("test", CircuitBreakerConfig(
            failure_threshold=3, excluded_exceptions=(CustomError,)
        ))

        for _ in range(5):
            with pytest.raises(CustomError):
                cb.call(lambda: (_ for _ in ()).throw(CustomError("excluded")))

        assert cb.state.value == "closed"

    def test_get_stats(self):
        cb = CircuitBreaker("test")
        stats = cb.get_stats()
        assert stats["name"] == "test"
        assert stats["state"] == "closed"

    def test_registry_singleton(self):
        r1 = CircuitBreakerRegistry()
        r2 = CircuitBreakerRegistry()
        assert r1 is r2

    def test_registry_get_or_create(self):
        reg = CircuitBreakerRegistry()
        cb1 = reg.get_or_create("unique_test")
        cb2 = reg.get_or_create("unique_test")
        assert cb1 is cb2

    def test_reset(self):
        cb = CircuitBreaker("test", CircuitBreakerConfig(failure_threshold=1, timeout_seconds=60.0))

        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))

        assert cb.state.value == "open"
        cb.reset()
        assert cb.state.value == "closed"


class TestRateLimiter:
    def test_token_bucket_acquire(self):
        rl = TokenBucketRateLimiter("test", RateLimitConfig(requests_per_second=100.0, burst_size=10))
        for _ in range(10):
            assert rl.acquire()

    def test_token_bucket_blocked(self):
        rl = TokenBucketRateLimiter("test", RateLimitConfig(requests_per_second=100.0, burst_size=3))
        for _ in range(3):
            assert rl.acquire()
        assert not rl.try_acquire()

    def test_token_bucket_refills(self):
        rl = TokenBucketRateLimiter("test", RateLimitConfig(requests_per_second=1000.0, burst_size=5))
        for _ in range(5):
            assert rl.try_acquire()
        assert not rl.try_acquire()

    def test_available_tokens(self):
        rl = TokenBucketRateLimiter("test", RateLimitConfig(requests_per_second=100.0, burst_size=10))
        assert rl.get_available_tokens() == 10.0
        rl.acquire()
        assert rl.get_available_tokens() == 9.0

    def test_get_stats(self):
        rl = TokenBucketRateLimiter("test", RateLimitConfig(requests_per_second=10.0, burst_size=20))
        stats = rl.get_stats()
        assert stats["name"] == "test"
        assert stats["capacity"] == 20
        assert stats["refill_rate"] == 10.0

    def test_acquire_with_timeout(self):
        rl = TokenBucketRateLimiter("test", RateLimitConfig(requests_per_second=0.001, burst_size=1))
        assert rl.acquire()
        assert not rl.acquire(timeout=0.01)

    def test_multi_tier(self):
        mt = MultiTierRateLimiter()
        mt.configure_tier("test", RateLimitConfig(requests_per_second=100.0, burst_size=5))
        for _ in range(5):
            assert mt.acquire("test")
        assert not mt.try_acquire("test")

    def test_handle_retry_after(self):
        mt = MultiTierRateLimiter()
        mt.configure_tier("test", RateLimitConfig(requests_per_second=100.0, burst_size=10))
        mt.acquire("test")
        mt.handle_retry_after("test", 0.1)
        tokens = mt.get_limiter("test").get_available_tokens()
        assert tokens <= 0


class TestDeadLetterQueue:
    @pytest.fixture
    def dlq(self, tmp_path):
        queue = DeadLetterQueue("test_dlq", base_dir=str(tmp_path), max_retries=3)
        yield queue

    def test_add_entry(self, dlq):
        eid = dlq.add({"key": "value"}, "test error")
        assert eid is not None
        assert dlq.size() == 1

    def test_get_stats(self, dlq):
        dlq.add({"key": "value"}, "test error")
        stats = dlq.get_stats()
        assert stats["name"] == "test_dlq"
        assert stats["queue_size"] == 1

    def test_processor_retries(self, tmp_path):
        call_count = [0]

        def failing_processor(payload):
            call_count[0] += 1
            raise ValueError("always fail")

        dlq = DeadLetterQueue("retry_test", base_dir=str(tmp_path), max_retries=2,
                              base_backoff=0.01, processor=failing_processor)
        dlq.add({"key": "value"}, "initial error")
        dlq._process_due_entries()
        assert call_count[0] == 1
        import time
        time.sleep(0.02)
        dlq._process_due_entries()
        assert call_count[0] == 2

    def test_persistence(self, tmp_path):
        dlq1 = DeadLetterQueue("persist_test", base_dir=str(tmp_path), max_retries=3)
        dlq1.add({"key": "value"}, "error")

        dlq2 = DeadLetterQueue("persist_test", base_dir=str(tmp_path), max_retries=3)
        assert dlq2.size() == 1
        assert dlq2.get_stats()["queue_size"] == 1

    def test_registry_singleton(self):
        r1 = DLQRegistry()
        r2 = DLQRegistry()
        assert r1 is r2
