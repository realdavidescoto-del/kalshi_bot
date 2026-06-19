import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

logger = logging.getLogger("kalshi_bot.dlq")


def _update_dlq_metric(name: str, size: int):
    try:
        from observability.metrics import update_dlq_size as _fn
        _fn(name, size)
    except Exception:
        pass


def _record_dlq_processed(name: str, status: str):
    try:
        from observability.metrics import record_dlq_processed as _fn
        _fn(name, status)
    except Exception:
        pass


@dataclass
class DLQEntry:
    id: str
    payload: dict
    error: str
    timestamp: str
    retry_count: int = 0
    max_retries: int = 5
    next_retry: float = 0
    created_at: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = self.timestamp
        if not self.next_retry:
            self.next_retry = time.time()


class DeadLetterQueue:
    def __init__(
        self,
        name: str,
        base_dir: str = "data/dlq",
        max_retries: int = 5,
        base_backoff: float = 1.0,
        max_backoff: float = 300.0,
        processor: Callable[[dict], Any] | None = None,
    ):
        self.name = name
        self.base_dir = Path(base_dir) / name
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.base_backoff = base_backoff
        self.max_backoff = max_backoff
        self.processor = processor
        self._queue: Queue = Queue()
        self._lock = threading.Lock()
        self._running = False
        self._processor_thread: threading.Thread | None = None
        self._load_existing()

    def _load_existing(self):
        for file_path in sorted(self.base_dir.glob("*.json")):
            try:
                with open(file_path) as f:
                    data = json.load(f)
                entry = DLQEntry(**data)
                self._queue.put(entry)
            except Exception as e:
                logger.error(f"Failed to load DLQ entry from {file_path}: {e}")

    def _get_file_path(self, entry_id: str) -> Path:
        return self.base_dir / f"{entry_id}.json"

    def _persist_entry(self, entry: DLQEntry):
        file_path = self._get_file_path(entry.id)
        try:
            with open(file_path, "w") as f:
                json.dump(asdict(entry), f)
        except Exception as e:
            logger.error(f"Failed to persist DLQ entry {entry.id}: {e}")

    def _remove_entry(self, entry_id: str):
        file_path = self._get_file_path(entry_id)
        try:
            file_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error(f"Failed to remove DLQ entry {entry_id}: {e}")

    def add(self, payload: dict, error: str, max_retries: int | None = None) -> str:
        entry_id = f"{int(time.time() * 1000)}_{threading.get_ident()}"
        entry = DLQEntry(
            id=entry_id,
            payload=payload,
            error=error,
            timestamp=datetime.now(UTC).isoformat(),
            max_retries=max_retries or self.max_retries,
            next_retry=time.time(),
        )
        self._persist_entry(entry)
        self._queue.put(entry)
        _update_dlq_metric(self.name, self.size())
        logger.warning(
            f"DLQ '{self.name}': Added entry {entry_id} (queue size: {self.size()})"
        )
        return entry_id

    def size(self) -> int:
        return self._queue.qsize()

    def get_stats(self) -> dict:
        return {
            "name": self.name,
            "queue_size": self.size(),
            "directory": str(self.base_dir),
            "max_retries": self.max_retries,
        }

    def start_processor(self, interval: float = 30.0):
        if self._running:
            return
        self._running = True
        self._processor_thread = threading.Thread(
            target=self._process_loop, args=(interval,), daemon=True
        )
        self._processor_thread.start()
        logger.info(f"DLQ '{self.name}' processor started")

    def stop_processor(self):
        self._running = False
        if self._processor_thread:
            self._processor_thread.join(timeout=5.0)
        logger.info(f"DLQ '{self.name}' processor stopped")

    def _process_loop(self, interval: float):
        while self._running:
            try:
                self._process_due_entries()
            except Exception as e:
                logger.error(f"DLQ '{self.name}' processor error: {e}")
            time.sleep(interval)

    def _process_due_entries(self):
        now = time.time()
        processed = 0
        requeued = []

        while not self._queue.empty():
            try:
                entry = self._queue.get_nowait()
            except Empty:
                break

            if entry.next_retry > now:
                requeued.append(entry)
                continue

            if entry.retry_count >= entry.max_retries:
                logger.error(
                    f"DLQ '{self.name}': Entry {entry.id} exceeded max retries, moving to failed"
                )
                _record_dlq_processed(self.name, "failed")
                self._move_to_failed(entry)
                continue

            if self.processor:
                try:
                    self.processor(entry.payload)
                    _record_dlq_processed(self.name, "success")
                    logger.info(
                        f"DLQ '{self.name}': Successfully processed entry {entry.id}"
                    )
                    self._remove_entry(entry.id)
                    processed += 1
                except Exception as e:
                    entry.retry_count += 1
                    backoff = min(
                        self.base_backoff * (2**entry.retry_count), self.max_backoff
                    )
                    entry.next_retry = now + backoff
                    entry.error = str(e)
                    self._persist_entry(entry)
                    requeued.append(entry)
                    logger.warning(
                        f"DLQ '{self.name}': Retry {entry.retry_count}/{entry.max_retries} for {entry.id} in {backoff}s: {e}"
                    )
            else:
                requeued.append(entry)

        for entry in requeued:
            self._queue.put(entry)

        _update_dlq_metric(self.name, self.size())
        if processed > 0:
            logger.info(
                f"DLQ '{self.name}': Processed {processed} entries, {self.size()} remaining"
            )

    def _move_to_failed(self, entry: DLQEntry):
        failed_dir = self.base_dir / "failed"
        failed_dir.mkdir(exist_ok=True)
        failed_path = failed_dir / f"{entry.id}.json"
        try:
            with open(failed_path, "w") as f:
                json.dump(asdict(entry), f)
        except Exception as e:
            logger.error(f"Failed to move DLQ entry to failed: {e}")
        self._remove_entry(entry.id)

    def get_failed_entries(self) -> list[DLQEntry]:
        failed_dir = self.base_dir / "failed"
        entries = []
        if failed_dir.exists():
            for file_path in failed_dir.glob("*.json"):
                try:
                    with open(file_path) as f:
                        data = json.load(f)
                    entries.append(DLQEntry(**data))
                except Exception as e:
                    logger.error(f"Failed to load failed entry {file_path}: {e}")
        return entries

    def retry_failed(self, entry_id: str) -> bool:
        failed_dir = self.base_dir / "failed"
        file_path = failed_dir / f"{entry_id}.json"
        if not file_path.exists():
            return False
        try:
            with open(file_path) as f:
                data = json.load(f)
            data["retry_count"] = 0
            data["next_retry"] = time.time()
            data["timestamp"] = datetime.now(UTC).isoformat()
            entry = DLQEntry(**data)
            self._persist_entry(entry)
            self._queue.put(entry)
            file_path.unlink()
            logger.info(f"DLQ '{self.name}': Retried failed entry {entry_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to retry entry {entry_id}: {e}")
            return False


class DLQRegistry:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._queues = {}
        return cls._instance

    def get_or_create(self, name: str, **kwargs) -> DeadLetterQueue:
        with self._lock:
            if name not in self._queues:
                self._queues[name] = DeadLetterQueue(name, **kwargs)
            return self._queues[name]

    def get(self, name: str) -> DeadLetterQueue | None:
        with self._lock:
            return self._queues.get(name)

    def get_all_stats(self) -> dict:
        with self._lock:
            return {name: q.get_stats() for name, q in self._queues.items()}

    def start_all_processors(self, interval: float = 30.0):
        with self._lock:
            for q in self._queues.values():
                q.start_processor(interval)

    def stop_all_processors(self):
        with self._lock:
            for q in self._queues.values():
                q.stop_processor()


def get_dlq_registry() -> DLQRegistry:
    return DLQRegistry()
