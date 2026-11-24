# AI SRE Agent

An AI-assisted Site Reliability Engineering workflow that detects demo-service incidents, gathers evidence, investigates likely root causes with Google Gemini, generates reproduction tests, verifies them in an isolated Docker container, proposes a code patch, and opens a GitHub pull request for human review.

**The agent never merges pull requests or deploys code.** Staging verification is a separate health check after a human has deployed an approved change.

## Implemented workflow

- FastAPI incident/task API, PostgreSQL persistence and Redis queues.
- Demo FastAPI service with `/demo/crash`, `/demo/database`, `/demo/slow` and `/demo/ok`.
- Metrics-based detector and structured JSON application logs.
- Gemini investigation with bounded retries and optional fallback model.
- Read-only Git history/source inspection.
- Constrained reproduction-test validation and isolated Docker execution.
- Gemini-generated patch for reproducible HTTP 4xx/5xx demo incidents.
- Patch verification before GitHub branch/PR creation.
- Optional staging `/health` verification and evaluation summary.

The automated patch scope is intentionally limited to `apps/demo/main.py` and demo HTTP 4xx/5xx endpoints. This is a hackathon prototype, not a general-purpose production repair agent.

## Requirements

- Docker Desktop with Docker Compose v2.
- Google Gemini API key.
- GitHub repository/token only if automatic PR creation is desired.

## 1. Configure environment

If `.env` does not exist yet, create it from the example:

```bash
cp .env.example .env
```

If you already have a working `.env`, **do not overwrite it**. Keep your existing Gemini key and add any optional variables you need from `.env.example`.

Set the required values in `.env`:

```env
GEMINI_API_KEY=your_real_key
GEMINI_MODEL=gemini-3.5-flash-lite
```

Do not commit `.env`.

Optional GitHub PR automation:

```env
GITHUB_TOKEN=your_github_token
GITHUB_REPOSITORY=Harish-Nesrekar/ai-sre-agent
```

The token needs repository Contents read/write and Pull requests read/write permissions. Do not grant administration or Actions permissions unless separately required.

Optional staging verification:

```env
STAGING_BASE_URL=https://your-staging-host
```

The worker checks `STAGING_BASE_URL/health`; it does not deploy anything.

## 2. Start the complete stack

From the repository root:

```bash
docker compose up --build -d
```

The first build may take several minutes because it builds the isolated sandbox image and installs dependencies.

```bash
docker compose ps
docker compose logs --tail=80 api demo detector sandbox-runner worker
```

## 3. Verify readiness

```bash
curl http://localhost:8000/health/ready
curl http://localhost:8001/health
curl http://localhost:8000/evaluation/summary
```

Expected API readiness:

```json
{"status":"ready","database":"connected","redis":"connected"}
```

The sandbox runner is internal-only at `http://sandbox-runner:9000`.

## 4. Trigger an incident

```bash
curl http://localhost:8001/demo/ok
curl -i http://localhost:8001/demo/crash
```

The detector polls metrics every five seconds. It should create an incident and enqueue an investigation.

```bash
docker compose logs -f detector worker
curl http://localhost:8000/incidents
```

Copy an incident UUID and inspect it:

```bash
curl http://localhost:8000/incidents/INCIDENT_UUID/investigation
```

Transient Gemini 408/429/5xx errors are retried with bounded backoff. If Gemini remains unavailable, the task is delayed and retried up to the configured task limit.

## 5. GitHub pull requests

Before enabling PR creation:

1. Commit and push the project baseline to the configured GitHub repository.
2. Set `GITHUB_TOKEN` and `GITHUB_REPOSITORY` in `.env`.
3. Restart the stack with `docker compose up --build -d`.

For a reproduced supported HTTP error, Gemini proposes a replacement `apps/demo/main.py` and a test expecting the endpoint to return HTTP 200. The worker runs that test in the sandbox. Only a passing patch test can create a branch and pull request.

Review every generated diff and CI result. The agent does not approve, merge or deploy PRs.

Without GitHub configuration, investigation and sandbox verification still work; PR creation is reported as not configured.

## 6. Staging verification

After a human has reviewed, merged and deployed a fix to staging:

```bash
curl -X POST http://localhost:8000/incidents/INCIDENT_UUID/verify-staging
```

The worker checks `STAGING_BASE_URL/health` and stores the result. This is a health check only; it does not deploy or roll back.

## 7. Evaluation

```bash
curl http://localhost:8000/evaluation/summary
```

The response reports incident count, completed investigations, reproduction tests passed, verified patches, created PRs and task outcomes. Root-cause accuracy requires human-labelled ground truth; the API does not invent an accuracy score.

## API endpoints

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/health` | API liveness |
| GET | `/health/ready` | PostgreSQL and Redis readiness |
| GET | `/incidents` | Recent incidents |
| POST | `/incidents` | Create an incident |
| GET | `/incidents/{id}/investigation` | Investigation/workflow result |
| POST | `/incidents/{id}/verify-staging` | Queue staging health check |
| GET | `/tasks/{id}` | Task status/result |
| GET | `/evaluation/summary` | Evaluation counters |
| GET | `/metrics` | Prometheus metrics |

API docs: <http://localhost:8000/docs>
Demo docs: <http://localhost:8001/docs>

## Troubleshooting

```bash
docker compose logs --tail=150
docker compose logs --tail=100 worker
docker compose exec redis redis-cli LLEN sre:task_queue
docker compose exec redis redis-cli LLEN sre:task_processing
docker compose exec redis redis-cli ZCARD sre:task_delayed
```

Rebuild after source changes:

```bash
docker compose up --build -d
```

If you change `POSTGRES_PASSWORD` after the database volume is initialized, PostgreSQL retains the old password in that volume. For a disposable local demo only:

```bash
docker compose down -v
docker compose up --build -d
```

**Warning:** `docker compose down -v` permanently deletes local PostgreSQL data.

## Security boundaries and limitations

- Sandbox containers have no network, run as UID 10001, drop Linux capabilities and have memory/CPU/PID limits.
- The sandbox runner mounts the Docker socket. Docker socket access is effectively host-level control. Keep the runner private to the Compose network and never expose port 9000 publicly.
- This sandbox is for constrained hackathon-generated code, not hostile multi-tenant code or a hardened adversarial execution boundary.
- Repository source is mounted read-only. GitHub writes occur only after a passing sandbox test.
- No automatic merge, production deployment or rollback is performed.
- Automatic patching currently supports reproducible HTTP 4xx/5xx demo incidents. Slow-request and infrastructure-only incidents are investigated but not automatically patched.
