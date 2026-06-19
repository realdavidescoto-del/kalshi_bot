import logging
import os
import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

logger = logging.getLogger("kalshi_bot.metrics")

REGISTRY = CollectorRegistry()

WS_CONNECTIONS = Gauge(
    "kalshi_ws_connections",
    "Number of active WebSocket connections",
    registry=REGISTRY,
)

WS_MESSAGES_RECEIVED = Counter(
    "kalshi_ws_messages_received_total",
    "Total WebSocket messages received",
    ["type"],
    registry=REGISTRY,
)

WS_MESSAGES_SENT = Counter(
    "kalshi_ws_messages_sent_total",
    "Total WebSocket messages sent",
    ["type"],
    registry=REGISTRY,
)

WS_RECONNECTIONS = Counter(
    "kalshi_ws_reconnections_total",
    "Total WebSocket reconnections",
    registry=REGISTRY,
)

WS_SEQUENCE_GAPS = Counter(
    "kalshi_ws_sequence_gaps_total",
    "Total WebSocket sequence gaps detected",
    registry=REGISTRY,
)

WS_LATENCY = Gauge(
    "kalshi_ws_latency_seconds",
    "WebSocket message latency in seconds",
    registry=REGISTRY,
)

WS_LAST_MESSAGE_TIME = Gauge(
    "kalshi_ws_last_message_timestamp",
    "Unix timestamp of last WebSocket message received",
    registry=REGISTRY,
)

ORDERS_PLACED = Counter(
    "kalshi_orders_placed_total",
    "Total orders placed",
    ["ticker", "side", "action", "status"],
    registry=REGISTRY,
)

ORDERS_REJECTED = Counter(
    "kalshi_orders_rejected_total",
    "Total orders rejected",
    ["ticker", "side", "reason"],
    registry=REGISTRY,
)

ORDER_LATENCY = Histogram(
    "kalshi_order_latency_seconds",
    "Order placement latency in seconds",
    ["ticker", "side"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    registry=REGISTRY,
)

POSITIONS_OPEN = Gauge(
    "kalshi_positions_open",
    "Number of open positions",
    ["ticker", "side"],
    registry=REGISTRY,
)

POSITION_PNL = Gauge(
    "kalshi_position_pnl_dollars",
    "Position P&L in dollars",
    ["ticker", "side", "pnl_type"],
    registry=REGISTRY,
)

TOTAL_CAPITAL = Gauge(
    "kalshi_total_capital_dollars",
    "Total available capital in dollars",
    registry=REGISTRY,
)

SECTOR_EXPOSURE = Gauge(
    "kalshi_sector_exposure_dollars",
    "Sector exposure in dollars",
    ["sector"],
    registry=REGISTRY,
)

VAR_USAGE = Gauge(
    "kalshi_var_usage_percent",
    "VaR usage as percentage of limit",
    registry=REGISTRY,
)

KILL_SWITCH_TRIGGERED = Counter(
    "kalshi_kill_switch_triggered_total",
    "Total kill switch triggers",
    registry=REGISTRY,
)

CIRCUIT_BREAKER_STATE = Gauge(
    "kalshi_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half_open, 2=open)",
    ["name"],
    registry=REGISTRY,
)

RATE_LIMITER_TOKENS = Gauge(
    "kalshi_rate_limiter_available_tokens",
    "Available tokens in rate limiter",
    ["tier"],
    registry=REGISTRY,
)

DLQ_SIZE = Gauge(
    "kalshi_dlq_size",
    "Dead letter queue size",
    ["name"],
    registry=REGISTRY,
)

DLQ_PROCESSED = Counter(
    "kalshi_dlq_processed_total",
    "Total DLQ entries processed",
    ["name", "status"],
    registry=REGISTRY,
)

SYSTEM_MEMORY_USAGE = Gauge(
    "kalshi_system_memory_usage_bytes",
    "System memory usage in bytes",
    registry=REGISTRY,
)

SYSTEM_CPU_USAGE = Gauge(
    "kalshi_system_cpu_usage_percent",
    "System CPU usage percentage",
    registry=REGISTRY,
)

SYSTEM_TEMPERATURE = Gauge(
    "kalshi_system_temperature_celsius",
    "System temperature in Celsius",
    registry=REGISTRY,
)

SHADOW_TRADES = Counter(
    "kalshi_shadow_trades_total",
    "Total shadow trades logged",
    ["ticker", "side"],
    registry=REGISTRY,
)

DB_CONNECTION_COUNT = Gauge(
    "kalshi_db_connection_count",
    "Number of active SQLite connections",
    registry=REGISTRY,
)

DB_QUERY_LATENCY = Gauge(
    "kalshi_db_query_latency_ms",
    "Average SQLite query latency in milliseconds",
    registry=REGISTRY,
)

DB_SIZE_BYTES = Gauge(
    "kalshi_db_size_bytes",
    "SQLite database file size in bytes",
    registry=REGISTRY,
)

DB_WAL_SIZE_BYTES = Gauge(
    "kalshi_db_wal_size_bytes",
    "SQLite WAL file size in bytes",
    registry=REGISTRY,
)

STRATEGY_TRIGGERS = Counter(
    "kalshi_strategy_triggers_total",
    "Total strategy triggers",
    ["indicator", "signal"],
    registry=REGISTRY,
)


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            start_time = time.time()
            method = scope.get("method", "unknown")
            endpoint = scope.get("path", "unknown")

            async def send_wrapper(message):
                if message["type"] == "http.response.start":
                    duration = time.time() - start_time
                    status = message.get("status", 0)
                    HTTP_REQUEST_DURATION.labels(method=method, endpoint=endpoint, status=str(status)).observe(duration)
                    HTTP_REQUESTS_TOTAL.labels(method=method, endpoint=endpoint, status=str(status)).inc()
                await send(message)

            await self.app(scope, receive, send_wrapper)
        else:
            await self.app(scope, receive, send)


HTTP_REQUEST_DURATION = Histogram(
    "kalshi_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint", "status"],
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
    registry=REGISTRY,
)

HTTP_REQUESTS_TOTAL = Counter(
    "kalshi_http_requests_total",
    "Total HTTP requests",
    ["method", "endpoint", "status"],
    registry=REGISTRY,
)


def update_ws_connection_count(count: int):
    WS_CONNECTIONS.set(count)


def record_ws_message_received(msg_type: str):
    WS_MESSAGES_RECEIVED.labels(type=msg_type).inc()


def record_ws_message_sent(msg_type: str):
    WS_MESSAGES_SENT.labels(type=msg_type).inc()


def record_ws_reconnection():
    WS_RECONNECTIONS.inc()


def record_ws_sequence_gap():
    WS_SEQUENCE_GAPS.inc()


def record_order_placed(ticker: str, side: str, action: str, status: str):
    ORDERS_PLACED.labels(ticker=ticker, side=side, action=action, status=status).inc()


def record_order_rejected(ticker: str, side: str, reason: str):
    ORDERS_REJECTED.labels(ticker=ticker, side=side, reason=reason).inc()


def record_order_latency(ticker: str, side: str, latency: float):
    ORDER_LATENCY.labels(ticker=ticker, side=side).observe(latency)


def update_positions_open(ticker: str, side: str, count: int):
    POSITIONS_OPEN.labels(ticker=ticker, side=side).set(count)


def update_position_pnl(ticker: str, side: str, pnl_type: str, value: float):
    POSITION_PNL.labels(ticker=ticker, side=side, pnl_type=pnl_type).set(value)


def update_total_capital(value: float):
    TOTAL_CAPITAL.set(value)


def update_sector_exposure(sector: str, value: float):
    SECTOR_EXPOSURE.labels(sector=sector).set(value)


def update_var_usage(percent: float):
    VAR_USAGE.set(percent)


def record_kill_switch_triggered():
    KILL_SWITCH_TRIGGERED.inc()


def update_circuit_breaker_state(name: str, state: str):
    state_map = {"closed": 0, "half_open": 1, "open": 2}
    CIRCUIT_BREAKER_STATE.labels(name=name).set(state_map.get(state, 0))


def update_rate_limiter_tokens(tier: str, tokens: float):
    RATE_LIMITER_TOKENS.labels(tier=tier).set(tokens)


def update_dlq_size(name: str, size: int):
    DLQ_SIZE.labels(name=name).set(size)


def record_dlq_processed(name: str, status: str):
    DLQ_PROCESSED.labels(name=name, status=status).inc()


def update_system_memory(bytes_used: int):
    SYSTEM_MEMORY_USAGE.set(bytes_used)


def update_system_cpu(percent: float):
    SYSTEM_CPU_USAGE.set(percent)


def update_system_temperature(celsius: float):
    SYSTEM_TEMPERATURE.set(celsius)


def record_shadow_trade(ticker: str, side: str):
    SHADOW_TRADES.labels(ticker=ticker, side=side).inc()


def record_ws_latency(seconds: float):
    WS_LATENCY.set(seconds)


def update_ws_last_message_time(timestamp: float):
    WS_LAST_MESSAGE_TIME.set(timestamp)


def update_db_stats(stats: dict):
    DB_CONNECTION_COUNT.set(stats.get("connection_count", 0))
    DB_QUERY_LATENCY.set(stats.get("avg_query_time_ms", 0))
    DB_SIZE_BYTES.set(stats.get("db_size_bytes", 0))
    DB_WAL_SIZE_BYTES.set(stats.get("wal_size_bytes", 0))


def record_strategy_trigger(indicator: str, signal: str):
    STRATEGY_TRIGGERS.labels(indicator=indicator, signal=signal).inc()


def push_to_gateway(gateway_url: str | None = None, job_name: str = "kalshi_bot", grouping_key: dict | None = None):
    if not gateway_url:
        gateway_url = os.getenv("PROMETHEUS_PUSHGATEWAY_URL", "")
        if not gateway_url:
            return
    try:
        import prometheus_client
        prometheus_client.push_to_gateway(
            gateway_url,
            job=job_name,
            registry=REGISTRY,
            grouping_key=grouping_key or {},
        )
    except Exception as e:
        logger.error(f"Failed to push metrics to gateway {gateway_url}: {e}")


def get_metrics() -> bytes:
    return generate_latest(REGISTRY)


def get_content_type() -> str:
    return CONTENT_TYPE_LATEST
