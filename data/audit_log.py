import json
import logging
import os
import threading
import time

logger = logging.getLogger("kalshi_bot.audit_log")


class AuditLogger:
    def __init__(self, base_dir: str | None = None):
        if base_dir is None:
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "logs"))
        self._base_dir = base_dir
        self._lock = threading.Lock()

    def _ensure_dir(self) -> str:
        d = os.path.join(self._base_dir, "audit")
        os.makedirs(d, exist_ok=True)
        return d

    def _log_path(self) -> str:
        today = time.strftime("%Y-%m-%d")
        audit_dir = self._ensure_dir()
        return os.path.join(audit_dir, f"audit-{today}.log")

    def log(self, event_type: str, ticker: str, action: str, **extra) -> None:
        with self._lock:
            path = self._log_path()
            record = {
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "event_type": event_type,
                "ticker": ticker,
                "action": action,
            }
            record.update(extra)
            with open(path, "a") as f:
                f.write(json.dumps(record) + "\n")
                f.flush()

    def log_order_placed(
        self,
        ticker: str,
        action: str,
        outcome_side: str,
        price: float,
        quantity: float,
        **extra,
    ) -> None:
        self.log(
            "order_placed",
            ticker,
            action,
            outcome_side=outcome_side,
            price=price,
            quantity=quantity,
            **extra,
        )

    def log_order_filled(
        self,
        ticker: str,
        action: str,
        outcome_side: str,
        price: float,
        quantity: float,
        fee: float,
        rebate: float,
        **extra,
    ) -> None:
        self.log(
            "order_filled",
            ticker,
            action,
            outcome_side=outcome_side,
            price=price,
            quantity=quantity,
            fee=fee,
            rebate=rebate,
            **extra,
        )

    def log_order_rejected(
        self,
        ticker: str,
        action: str,
        outcome_side: str,
        reason: str,
        **extra,
    ) -> None:
        self.log(
            "order_rejected",
            ticker,
            action,
            outcome_side=outcome_side,
            reason=reason,
            **extra,
        )

    def log_kill_switch(self, reason: str, balance: float | None = None) -> None:
        extra = {"reason": reason}
        if balance is not None:
            extra["balance"] = balance
        self.log("kill_switch", "SYSTEM", "cancel_all", **extra)

    def log_strategy_trigger(
        self,
        ticker: str,
        indicator: str,
        actual: float,
        forecast: float,
        wager: float,
        side: str,
    ) -> None:
        self.log(
            "strategy_trigger",
            ticker,
            "signal",
            indicator=indicator,
            actual=actual,
            forecast=forecast,
            wager=wager,
            side=side,
        )

    def cleanup_old_logs(self, max_age_days: int = 30) -> int:
        audit_dir = os.path.join(self._base_dir, "audit")
        if not os.path.isdir(audit_dir):
            return 0
        now = time.time()
        cutoff = now - (max_age_days * 86400)
        removed = 0
        for name in os.listdir(audit_dir):
            path = os.path.join(audit_dir, name)
            if os.path.isfile(path):
                try:
                    mtime = os.path.getmtime(path)
                    if mtime < cutoff:
                        os.remove(path)
                        removed += 1
                except OSError:
                    pass
        return removed


_instance = None
_instance_lock = threading.Lock()


def get_audit_logger() -> AuditLogger:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AuditLogger()
    return _instance
