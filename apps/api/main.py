import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import UUID, uuid4

import psycopg
import redis
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from prometheus_client import Counter, generate_latest
from psycopg.types.json import Jsonb

from database import initialize_database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sre-api")

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
STAGING_BASE_URL = os.getenv("STAGING_BASE_URL", "").strip()
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
TASK_QUEUE = "sre:task_queue"

REQUEST_COUNT = Counter("api_requests_total", "Total API requests", ["method", "endpoint"])
ERROR_COUNT = Counter("api_errors_total", "Total API errors")


class TaskCreate(BaseModel):
    task_type: Literal["demo_task", "verify_staging"]
    payload: dict[str, Any] = Field(default_factory=dict)


class IncidentCreate(BaseModel):
    incident_type: str = Field(min_length=1, max_length=100)
    service: str = Field(default="demo-service", min_length=1, max_length=100)
    endpoint: str | None = Field(default=None, max_length=255)
    status_code: int | None = Field(default=None, ge=100, le=599)
    details: dict[str, Any] = Field(default_factory=dict)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
    redis_client.ping()
    logger.info("API started; database schema and Redis are ready")
    yield
    redis_client.close()
    logger.info("API stopped")


app = FastAPI(title="AI SRE Agent", version="2.0.0", lifespan=lifespan)


@app.middleware("http")
async def monitor_requests(request, call_next):
    REQUEST_COUNT.labels(method=request.method, endpoint=request.url.path).inc()
    try:
        response = await call_next(request)
        if response.status_code >= 500:
            ERROR_COUNT.inc()
        return response
    except Exception:
        ERROR_COUNT.inc()
        raise


@app.get("/health")
def health():
    return {"status": "healthy", "service": "api"}


@app.get("/health/ready")
def readiness():
    try:
        with psycopg.connect(DATABASE_URL, connect_timeout=3) as connection:
            connection.execute("SELECT 1")
        redis_client.ping()
        return {"status": "ready", "database": "connected", "redis": "connected"}
    except Exception as error:
        logger.exception("Readiness check failed")
        raise HTTPException(status_code=503, detail="Dependency connection failed") from error


def enqueue_task(task_id):
    try:
        redis_client.lpush(TASK_QUEUE, str(task_id))
    except redis.RedisError as error:
        logger.exception("Failed to enqueue task %s", task_id)
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE tasks SET status = 'failed', error = %s, updated_at = NOW() WHERE id = %s",
                    ("Failed to enqueue task in Redis", task_id),
                )
        raise HTTPException(status_code=503, detail="Task could not be added to the queue") from error


@app.post("/tasks", status_code=202)
def create_task(task: TaskCreate):
    if task.task_type == "verify_staging":
        incident_id = task.payload.get("incident_id")
        if not incident_id:
            raise HTTPException(status_code=422, detail="incident_id is required")
        try:
            UUID(str(incident_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail="incident_id must be a UUID") from error

    task_id = uuid4()
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO tasks (id, task_type, payload, status)
                VALUES (%s, %s, %s, 'pending')
                """,
                (task_id, task.task_type, Jsonb(task.payload)),
            )
    enqueue_task(task_id)
    return {"id": str(task_id), "task_type": task.task_type, "status": "pending"}


@app.get("/tasks/{task_id}")
def get_task(task_id: UUID):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, task_type, payload, status, result, error, created_at, updated_at
                FROM tasks WHERE id = %s
                """,
                (task_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return {
        "id": str(row[0]), "task_type": row[1], "payload": row[2],
        "status": row[3], "result": row[4], "error": row[5],
        "created_at": row[6], "updated_at": row[7],
    }


@app.post("/incidents", status_code=201)
def create_incident(incident: IncidentCreate):
    incident_id, task_id = uuid4(), uuid4()
    payload = {"incident_id": str(incident_id)}
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO incidents (
                    id, incident_type, service, endpoint, status_code, details, status
                ) VALUES (%s, %s, %s, %s, %s, %s, 'open')
                """,
                (
                    incident_id, incident.incident_type, incident.service,
                    incident.endpoint, incident.status_code, Jsonb(incident.details),
                ),
            )
            cursor.execute(
                """
                INSERT INTO tasks (id, task_type, payload, status)
                VALUES (%s, 'investigate_incident', %s, 'pending')
                """,
                (task_id, Jsonb(payload)),
            )
    enqueue_task(task_id)
    logger.info("Incident queued: incident=%s task=%s", incident_id, task_id)
    return {
        "id": str(incident_id), "incident_type": incident.incident_type,
        "service": incident.service, "endpoint": incident.endpoint,
        "status_code": incident.status_code, "details": incident.details,
        "status": "open", "investigation_task_id": str(task_id),
        "investigation_status": "pending",
    }


@app.get("/incidents")
def list_incidents():
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.id, i.incident_type, i.service, i.endpoint, i.status_code,
                       i.details, i.status, i.detected_at, i.updated_at,
                       t.status AS task_status
                FROM incidents i
                LEFT JOIN tasks t
                  ON t.payload->>'incident_id' = i.id::text
                 AND t.task_type = 'investigate_incident'
                ORDER BY i.detected_at DESC
                LIMIT 100
                """
            )
            rows = cursor.fetchall()
    return [
        {
            "id": str(r[0]), "incident_type": r[1], "service": r[2],
            "endpoint": r[3], "status_code": r[4], "details": r[5],
            "status": r[6], "detected_at": r[7], "updated_at": r[8],
            "investigation_task_status": r[9],
        }
        for r in rows
    ]


@app.get("/incidents/{incident_id}/investigation")
def get_investigation(incident_id: UUID):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.status, inv.provider, inv.model, inv.analysis, inv.created_at
                FROM incidents i
                LEFT JOIN investigations inv ON inv.incident_id = i.id
                WHERE i.id = %s
                """,
                (incident_id,),
            )
            row = cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Incident not found")
    return {
        "incident_id": str(incident_id),
        "incident_status": row[0],
        "investigation_status": "completed" if row[3] is not None else "pending",
        "investigation": None if row[3] is None else {
            "provider": row[1], "model": row[2], "analysis": row[3], "created_at": row[4],
        },
    }


@app.post("/incidents/{incident_id}/verify-staging", status_code=202)
def request_staging_verification(incident_id: UUID):
    if not STAGING_BASE_URL:
        raise HTTPException(status_code=503, detail="STAGING_BASE_URL is not configured")
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM incidents WHERE id = %s", (incident_id,))
            if cursor.fetchone() is None:
                raise HTTPException(status_code=404, detail="Incident not found")
            task_id = uuid4()
            cursor.execute(
                """
                INSERT INTO tasks (id, task_type, payload, status)
                VALUES (%s, 'verify_staging', %s, 'pending')
                """,
                (task_id, Jsonb({"incident_id": str(incident_id)})),
            )
    enqueue_task(task_id)
    return {"task_id": str(task_id), "status": "pending", "staging_url": STAGING_BASE_URL}


@app.get("/evaluation/summary")
def evaluation_summary():
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM incidents")
            total_incidents = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM investigations")
            investigated = cursor.fetchone()[0]
            cursor.execute(
                """
                SELECT
                  COUNT(*) FILTER (
                    WHERE analysis->'sandbox_verification'->>'status' = 'reproduced'
                  ),
                  COUNT(*) FILTER (
                    WHERE analysis->'patch_workflow'->>'status' = 'verified'
                  ),
                  COUNT(*) FILTER (
                    WHERE analysis->'patch_workflow'->'github'->>'status' = 'pull_request_created'
                  )
                FROM investigations
                """
            )
            reproduced, verified_patches, pull_requests = cursor.fetchone()
            cursor.execute(
                "SELECT COUNT(*) FILTER (WHERE status = 'completed'), COUNT(*) FILTER (WHERE status = 'failed') FROM tasks"
            )
            completed_tasks, failed_tasks = cursor.fetchone()
    return {
        "incidents_total": total_incidents,
        "investigations_completed": investigated,
        "reproduction_tests_passed": reproduced,
        "patches_verified": verified_patches,
        "github_pull_requests_created": pull_requests,
        "tasks_completed": completed_tasks,
        "tasks_failed": failed_tasks,
        "root_cause_accuracy": "Requires human-labelled ground truth; not inferred automatically",
    }


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type="text/plain")
