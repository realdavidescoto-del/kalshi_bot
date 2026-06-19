import os
import time
from typing import Any

import psutil
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from data.database import get_db_stats
from observability.metrics import MetricsMiddleware, get_content_type, get_metrics
from resilience.circuit_breaker import CircuitBreakerRegistry
from resilience.dead_letter_queue import get_dlq_registry
from resilience.rate_limiter import get_rate_limiter

app = FastAPI(title="Kalshi Bot Health API", version="1.0.0")
app.add_middleware(MetricsMiddleware)

_start_time = time.time()

_HEALTH_SECRET = os.getenv("HEALTH_SECRET", "")
_DISABLE_HEALTH_AUTH = os.getenv("DISABLE_HEALTH_AUTH", "").lower() in ("1", "true", "yes")


def _verify_health_auth(request: Request):
    if _DISABLE_HEALTH_AUTH:
        return
    if not _HEALTH_SECRET:
        return
    token = request.headers.get("X-API-Key") or request.headers.get("Authorization", "")
    if token == _HEALTH_SECRET:
        return
    raise HTTPException(status_code=403, detail="Invalid or missing health API key")


class HealthResponse(BaseModel):
    status: str
    uptime_seconds: float
    version: str = "1.0.0"


class ReadinessResponse(BaseModel):
    ready: bool
    checks: dict[str, Any]


class MetricsResponse(Response):
    media_type = get_content_type()

    def render(self, content: bytes) -> bytes:
        return content


def get_system_info() -> dict[str, Any]:
    process = psutil.Process(os.getpid())
    mem = process.memory_info()
    return {
        "memory_rss_mb": round(mem.rss / 1024 / 1024, 2),
        "memory_vms_mb": round(mem.vms / 1024 / 1024, 2),
        "cpu_percent": process.cpu_percent(),
        "num_threads": process.num_threads(),
        "open_files": len(process.open_files()),
        "connections": len(process.connections()),
    }


@app.get("/health", response_model=HealthResponse)
async def health(request: Request):
    _verify_health_auth(request)
    return HealthResponse(
        status="healthy",
        uptime_seconds=round(time.time() - _start_time, 2),
    )


@app.get("/ready")
async def readiness(request: Request):
    _verify_health_auth(request)
    checks = {}
    ready = True

    try:
        cb_registry = CircuitBreakerRegistry()
        cb_stats = cb_registry.get_all_stats()
        open_breakers = [
            name for name, stats in cb_stats.items() if stats["state"] == "open"
        ]
        checks["circuit_breakers"] = {
            "status": "ok" if not open_breakers else "degraded",
            "open_breakers": open_breakers,
            "details": cb_stats,
        }
        if open_breakers:
            ready = False
    except Exception as e:
        checks["circuit_breakers"] = {"status": "error", "error": str(e)}
        ready = False

    try:
        rate_limiter = get_rate_limiter()
        rl_stats = rate_limiter.get_all_stats()
        checks["rate_limiters"] = {"status": "ok", "details": rl_stats}
    except Exception as e:
        checks["rate_limiters"] = {"status": "error", "error": str(e)}

    try:
        dlq_registry = get_dlq_registry()
        dlq_stats = dlq_registry.get_all_stats()
        total_dlq_size = sum(s["queue_size"] for s in dlq_stats.values())
        checks["dead_letter_queues"] = {
            "status": "ok" if total_dlq_size < 100 else "degraded",
            "total_size": total_dlq_size,
            "details": dlq_stats,
        }
        if total_dlq_size >= 1000:
            ready = False
    except Exception as e:
        checks["dead_letter_queues"] = {"status": "error", "error": str(e)}

    try:
        system_info = get_system_info()
        checks["system"] = {
            "status": "ok" if system_info["memory_rss_mb"] < 1024 else "degraded",
            "details": system_info,
        }
        if system_info["memory_rss_mb"] > 2048:
            ready = False
    except Exception as e:
        checks["system"] = {"status": "error", "error": str(e)}

    try:
        db_stats = get_db_stats()
        checks["database"] = {
            "status": "ok" if db_stats["connection_count"] < 50 else "degraded",
            "details": db_stats,
        }
        if db_stats["avg_query_time_ms"] > 1000:
            ready = False
    except Exception as e:
        checks["database"] = {"status": "error", "error": str(e)}

    from fastapi.responses import JSONResponse
    resp = ReadinessResponse(ready=ready, checks=checks)
    status_code = 200 if ready else 503
    return JSONResponse(content=resp.model_dump(), status_code=status_code)


@app.get("/metrics", response_class=MetricsResponse)
async def metrics(request: Request):
    _verify_health_auth(request)
    return MetricsResponse(content=get_metrics())


@app.get("/system")
async def system_info(request: Request):
    _verify_health_auth(request)
    return get_system_info()


def create_health_app() -> FastAPI:
    return app
