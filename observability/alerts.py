import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum

from resilience.circuit_breaker import CircuitBreakerRegistry
from resilience.dead_letter_queue import get_dlq_registry

logger = logging.getLogger("kalshi_bot.alerts")


class AlertSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Alert:
    name: str
    severity: AlertSeverity
    message: str
    timestamp: float = field(default_factory=time.time)
    labels: dict[str, str] = field(default_factory=dict)
    value: float | None = None
    threshold: float | None = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp,
            "labels": self.labels,
            "value": self.value,
            "threshold": self.threshold,
        }


class AlertRule:
    def __init__(self, name: str, check_fn: Callable[[], Alert | None], interval: float = 60.0):
        self.name = name
        self.check_fn = check_fn
        self.interval = interval
        self.last_check = 0.0
        self.last_alert: Alert | None = None
        self.alert_cooldown = 300.0

    def should_check(self) -> bool:
        return time.time() - self.last_check >= self.interval

    def check(self) -> Alert | None:
        self.last_check = time.time()
        try:
            alert = self.check_fn()
            if alert:
                if self._should_fire(alert):
                    self.last_alert = alert
                    return alert
            else:
                self.last_alert = None
        except Exception as e:
            logger.error(f"Error checking alert rule '{self.name}': {e}")
        return None

    def _should_fire(self, alert: Alert) -> bool:
        if self.last_alert is None:
            return True
        if time.time() - self.last_alert.timestamp >= self.alert_cooldown:
            return True
        return False


class AlertManager:
    def __init__(self):
        self._rules: list[AlertRule] = []
        self._handlers: list[Callable[[Alert], None]] = []
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None

    def add_rule(self, rule: AlertRule):
        with self._lock:
            self._rules.append(rule)

    def add_handler(self, handler: Callable[[Alert], None]):
        with self._lock:
            self._handlers.append(handler)

    def start(self, interval: float = 30.0):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, args=(interval,), daemon=True)
        self._thread.start()
        logger.info("Alert manager started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("Alert manager stopped")

    def _run_loop(self, interval: float):
        while self._running:
            try:
                self._evaluate_rules()
            except Exception as e:
                logger.error(f"Error in alert evaluation loop: {e}")
            time.sleep(interval)

    def _evaluate_rules(self):
        alerts_to_fire = []
        with self._lock:
            for rule in self._rules:
                if rule.should_check():
                    alert = rule.check()
                    if alert:
                        alerts_to_fire.append(alert)

        for alert in alerts_to_fire:
            self._fire_alert(alert)

    def _fire_alert(self, alert: Alert):
        logger.log(
            logging.CRITICAL if alert.severity == AlertSeverity.CRITICAL else logging.WARNING,
            f"ALERT [{alert.severity.value.upper()}] {alert.name}: {alert.message}",
            extra={"alert": alert.to_dict()},
        )
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as e:
                logger.error(f"Error in alert handler: {e}")


_alert_manager = None
_alert_manager_lock = threading.Lock()


def get_alert_manager() -> AlertManager:
    global _alert_manager
    with _alert_manager_lock:
        if _alert_manager is None:
            _alert_manager = AlertManager()
            _setup_default_rules(_alert_manager)
        return _alert_manager


def _setup_default_rules(manager: AlertManager):
    cb_registry = CircuitBreakerRegistry()
    dlq_registry = get_dlq_registry()

    def check_ws_disconnect() -> Alert | None:
        cb_stats = cb_registry.get_all_stats()
        for name, stats in cb_stats.items():
            if "ws" in name.lower() or "websocket" in name.lower():
                if stats["state"] == "open":
                    return Alert(
                        name="websocket_disconnected",
                        severity=AlertSeverity.CRITICAL,
                        message=f"WebSocket circuit breaker '{name}' is OPEN - connection lost",
                        labels={"circuit_breaker": name},
                        value=2.0,
                        threshold=1.0,
                    )
        return None

    def check_order_rejection_rate() -> Alert | None:
        try:
            from prometheus_client import REGISTRY as PROM_REGISTRY

            rejected = 0.0
            placed = 0.0
            for metric in PROM_REGISTRY.collect():
                if metric.name == "kalshi_orders_rejected_total":
                    for sample in metric.samples:
                        rejected += sample.value
                elif metric.name == "kalshi_orders_placed_total":
                    for sample in metric.samples:
                        placed += sample.value
            if placed > 10:
                rate = rejected / placed
                if rate > 0.1:
                    return Alert(
                        name="high_order_rejection_rate",
                        severity=AlertSeverity.WARNING,
                        message=f"Order rejection rate is {rate:.1%} (rejected: {rejected}, placed: {placed})",
                        value=rate,
                        threshold=0.1,
                    )
        except Exception:
            pass
        return None

    def check_kill_switch() -> Alert | None:
        try:
            from config import Config
            from safety.kill_switch import KillSwitch

            ks = KillSwitch()
            balance = ks.get_balance()
            if balance < Config.KILL_SWITCH_MIN_BALANCE * Decimal("1.5"):
                return Alert(
                    name="balance_approaching_kill_switch",
                    severity=AlertSeverity.WARNING,
                    message=f"Balance ${balance:.2f} approaching kill switch threshold ${Config.KILL_SWITCH_MIN_BALANCE:.2f}",
                    value=float(balance),
                    threshold=float(Config.KILL_SWITCH_MIN_BALANCE * Decimal("1.5")),
                )
        except Exception:
            pass
        return None

    def check_circuit_breakers() -> Alert | None:
        cb_stats = cb_registry.get_all_stats()
        for name, stats in cb_stats.items():
            if stats["state"] == "open":
                return Alert(
                    name=f"circuit_breaker_open_{name}",
                    severity=AlertSeverity.CRITICAL,
                    message=f"Circuit breaker '{name}' is OPEN",
                    labels={"circuit_breaker": name},
                    value=2.0,
                    threshold=1.0,
                )
        return None

    def check_dlq_growth() -> Alert | None:
        dlq_stats = dlq_registry.get_all_stats()
        for name, stats in dlq_stats.items():
            if stats["queue_size"] > 100:
                return Alert(
                    name=f"dlq_growth_{name}",
                    severity=AlertSeverity.WARNING
                    if stats["queue_size"] < 1000
                    else AlertSeverity.CRITICAL,
                    message=f"Dead letter queue '{name}' has {stats['queue_size']} entries",
                    labels={"dlq": name},
                    value=float(stats["queue_size"]),
                    threshold=100.0,
                )
        return None

    def check_memory_usage() -> Alert | None:
        try:
            import psutil

            process = psutil.Process()
            mem_mb = process.memory_info().rss / 1024 / 1024
            if mem_mb > 512:
                return Alert(
                    name="high_memory_usage",
                    severity=AlertSeverity.WARNING if mem_mb < 1024 else AlertSeverity.CRITICAL,
                    message=f"Process memory usage is {mem_mb:.0f}MB",
                    value=mem_mb,
                    threshold=512.0,
                )
        except Exception:
            pass
        return None

    def check_temperature() -> Alert | None:
        if sys.platform == "win32":
            return None
        try:
            import psutil

            temps = psutil.sensors_temperatures()
            for name, entries in temps.items():
                for entry in entries:
                    if entry.current and entry.current > 75:
                        return Alert(
                            name="high_temperature",
                            severity=AlertSeverity.CRITICAL,
                            message=f"System temperature {entry.current:.1f}C exceeds 75C threshold",
                            labels={"sensor": f"{name}:{entry.label}"},
                            value=entry.current,
                            threshold=75.0,
                        )
        except Exception:
            pass
        return None

    manager.add_rule(AlertRule("ws_disconnect", check_ws_disconnect, interval=30.0))
    manager.add_rule(AlertRule("order_rejection_rate", check_order_rejection_rate, interval=60.0))
    manager.add_rule(AlertRule("kill_switch_approaching", check_kill_switch, interval=60.0))
    manager.add_rule(AlertRule("circuit_breakers", check_circuit_breakers, interval=30.0))
    manager.add_rule(AlertRule("dlq_growth", check_dlq_growth, interval=60.0))
    manager.add_rule(AlertRule("memory_usage", check_memory_usage, interval=60.0))
    manager.add_rule(AlertRule("temperature", check_temperature, interval=60.0))


def log_alert_handler(alert: Alert):
    import json
    import os

    log_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
    os.makedirs(log_dir, exist_ok=True)
    alert_log = os.path.join(log_dir, "alerts.log")
    with open(alert_log, "a") as f:
        f.write(json.dumps(alert.to_dict()) + "\n")


def slack_alert_handler(webhook_url: str):
    import json as json_mod
    import urllib.request

    def _handler(alert: Alert):
        if not webhook_url:
            return
        try:
            payload = json_mod.dumps({
                "text": (
                    f"[{alert.severity.value.upper()}] {alert.name}\n"
                    f"{alert.message}\n"
                    f"Value: {alert.value} | Threshold: {alert.threshold}"
                ),
                "attachments": [{"color": "danger" if alert.severity == AlertSeverity.CRITICAL else "warning",
                                 "fields": [{"title": k, "value": str(v), "short": True}
                                            for k, v in alert.labels.items()]}],
            }).encode()
            req = urllib.request.Request(webhook_url, data=payload,
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")

    return _handler


def smtp_alert_handler(smtp_config: dict | None = None):
    if not smtp_config:
        return None

    def _handler(alert: Alert):
        try:
            import smtplib
            from email.message import EmailMessage

            msg = EmailMessage()
            msg.set_content(
                f"Alert: {alert.name}\n"
                f"Severity: {alert.severity.value}\n"
                f"Message: {alert.message}\n"
                f"Value: {alert.value}\n"
                f"Threshold: {alert.threshold}\n"
                f"Labels: {alert.labels}"
            )
            msg["Subject"] = f"[Kalshi Bot] {alert.severity.value.upper()} - {alert.name}"
            msg["From"] = smtp_config.get("from_addr", "kalshi-bot@localhost")
            msg["To"] = smtp_config.get("to_addr", "")
            if not msg["To"]:
                return

            with smtplib.SMTP(
                smtp_config.get("host", "localhost"),
                smtp_config.get("port", 25),
                timeout=15,
            ) as server:
                if smtp_config.get("use_tls"):
                    server.starttls()
                user = smtp_config.get("user")
                password = smtp_config.get("password")
                if user and password:
                    server.login(user, password)
                server.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")

    return _handler


def setup_alerts(slack_webhook_url: str | None = None,
                 smtp_config: dict | None = None):
    manager = get_alert_manager()
    manager.add_handler(log_alert_handler)
    if slack_webhook_url:
        manager.add_handler(slack_alert_handler(slack_webhook_url))
    smtp_handler = smtp_alert_handler(smtp_config)
    if smtp_handler:
        manager.add_handler(smtp_handler)
    return manager
