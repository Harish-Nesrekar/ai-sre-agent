# AI SRE Agent — Project Specification

## Objective

Demonstrate an AI-assisted reliability workflow that detects a service incident, gathers evidence, investigates it, generates a constrained reproduction test, verifies a proposed patch in a disposable sandbox, and requests human review through a GitHub pull request.

## Architecture

- FastAPI API service
- PostgreSQL persistence for incidents, tasks and investigations
- Redis active queue, processing queue and delayed retry sorted set
- FastAPI demo application with intentional failure routes
- Metrics-polling incident detector
- Python worker using Google Gemini
- Read-only Git/repository inspection
- Docker sandbox runner and disposable test containers
- GitHub REST API integration for branch and pull-request creation
- Optional staging health verification
- Evaluation summary endpoint

## Incident workflow

1. Demo service exports counters, histograms and structured JSON logs.
2. Detector observes new error/slow-request metric deltas.
3. API persists an incident and investigation task.
4. Worker claims the task into a processing list.
5. Worker collects incident, logs, metrics and repository context.
6. Gemini returns structured root-cause analysis and a constrained reproduction test.
7. Worker validates the test AST and runs it in the sandbox.
8. For a reproduced HTTP 4xx/5xx demo incident, Gemini proposes a replacement demo source file and a status-200 verification test.
9. Worker validates and executes the patch test in the sandbox.
10. Only a passing patch test can trigger GitHub branch and PR creation.
11. A human reviews and merges the PR.
12. After staging deployment, a separate task checks `STAGING_BASE_URL/health`.

## Human control and safety

- Pull requests are created for review; they are never auto-approved or merged.
- The agent does not deploy code.
- Staging verification is a health check only.
- Repository inspection is read-only.
- Sandbox execution has no network and applies resource limits.
- Docker socket access remains a high-privilege trust boundary; runner must remain internal.
- Root-cause accuracy is not reported without labelled ground truth.

## Supported scope

Automated patch generation targets reproducible HTTP 4xx/5xx failures on the demo service and updates only `apps/demo/main.py`. Other incidents can be detected and investigated, but patch generation may be reported as unsupported.

## Evaluation

The API reports incident count, completed investigations, reproduction tests passed, patches verified, GitHub PRs created, and completed/failed tasks. Root-cause accuracy and time-to-fix require a labelled evaluation set and timestamped runs; they must not be fabricated.
