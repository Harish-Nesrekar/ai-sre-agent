import asyncio
import json
import logging
import os
import time

from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import Response
from prometheus_client import Counter, Histogram, generate_latest


LOG_DIRECTORY = "/var/log/sre"
LOG_FILE = os.path.join(LOG_DIRECTORY, "demo.jsonl")

os.makedirs(LOG_DIRECTORY, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("demo-service")

file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=5 * 1024 * 1024,
    backupCount=3
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(file_handler)

app = FastAPI(
    title="SRE Demo Application",
    version="1.0.0"
)

REQUEST_COUNT = Counter(
    "demo_requests_total",
    "Total demo application requests",
    ["endpoint", "status"]
)

ERROR_COUNT = Counter(
    "demo_errors_total",
    "Total demo application errors",
    ["failure_type"]
)

REQUEST_LATENCY = Histogram(
    "demo_request_duration_seconds",
    "Demo application request duration",
    ["endpoint"]
)

failure_counts = {
    "application_crash": 0,
    "slow_api": 0,
    "database_failure": 0
}


def log_event(event, **details):
    record = {
        "service": "demo-service",
        "event": event,
        "timestamp": time.time(),
        **details
    }

    logger.info(json.dumps(record, separators=(",", ":")))


@app.get("/health")
def health():
    return {
        "status": "healthy",
        "service": "demo-service"
    }


@app.get("/demo/ok")
def normal_request():
    REQUEST_COUNT.labels(
        endpoint="/demo/ok",
        status="200"
    ).inc()

    log_event(
        "normal_request",
        endpoint="/demo/ok",
        status_code=200
    )

    return {
        "status": "success"
    }


@app.get("/demo/crash")
def application_crash():
    failure_counts["application_crash"] += 1

    REQUEST_COUNT.labels(
        endpoint="/demo/crash",
        status="500"
    ).inc()

    ERROR_COUNT.labels(
        failure_type="application_crash"
    ).inc()

    log_event(
        "application_failure",
        endpoint="/demo/crash",
        status_code=500,
        failure_type="application_crash",
        error="Intentional demonstration exception"
    )

    raise HTTPException(
        status_code=500,
        detail="Simulated application failure"
    )


@app.get("/demo/slow")
async def slow_api(
    seconds: float = Query(default=5, ge=1, le=15)
):
    start = time.perf_counter()

    await asyncio.sleep(seconds)

    duration = time.perf_counter() - start

    REQUEST_COUNT.labels(
        endpoint="/demo/slow",
        status="200"
    ).inc()

    REQUEST_LATENCY.labels(
        endpoint="/demo/slow"
    ).observe(duration)

    failure_counts["slow_api"] += 1

    log_event(
        "slow_request",
        endpoint="/demo/slow",
        status_code=200,
        duration_seconds=round(duration, 3),
        threshold_exceeded=duration > 2
    )

    return {
        "status": "success",
        "delay_seconds": round(duration, 3)
    }


@app.get("/demo/database")
def database_failure():
    failure_counts["database_failure"] += 1

    REQUEST_COUNT.labels(
        endpoint="/demo/database",
        status="503"
    ).inc()

    ERROR_COUNT.labels(
        failure_type="database_failure"
    ).inc()

    log_event(
        "dependency_failure",
        endpoint="/demo/database",
        status_code=503,
        failure_type="database_failure",
        dependency="database",
        error="Simulated database unavailable"
    )

    raise HTTPException(
        status_code=503,
        detail="Simulated database unavailable"
    )


@app.post("/admin/reset")
def reset_failures():
    for key in failure_counts:
        failure_counts[key] = 0

    log_event(
        "failure_counters_reset",
        endpoint="/admin/reset",
        status_code=200
    )

    return {
        "status": "reset",
        "failure_counts": failure_counts
    }


@app.get("/admin/stats")
def get_stats():
    return {
        "failure_counts": failure_counts
    }


@app.get("/metrics")
def metrics():
    return Response(
        generate_latest(),
        media_type="text/plain"
    )
