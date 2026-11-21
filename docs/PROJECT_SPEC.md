
# AI SRE Agent

## 1. Project Overview

AI SRE Agent is an autonomous AI-powered Site Reliability Engineering platform that detects application failures, investigates root causes, generates code fixes, verifies patches, and creates GitHub pull requests for human approval.

## 2. Problem Statement

Small engineering teams often lack dedicated SRE engineers. Diagnosing production failures, identifying root causes, and implementing fixes consumes significant engineering time.

Our platform automates this workflow while keeping humans in control of code changes.

## 3. Architecture

- API: FastAPI
- Worker: Python
- Database: PostgreSQL
- Queue: Redis
- Monitoring: Prometheus
- AI: Google Gemini API
- Testing: pytest and Keploy
- Automation: n8n
- Deployment: Docker Compose
- Version control: GitHub API

## 4. Failure Scenarios

1. Bad commit bug
2. Wrong configuration
3. Missing database index
4. Database connection leak
5. Memory leak
6. Third-party API timeout
7. Bad deployment
8. Dependency version bump

## 5. Agent Workflow

1. Detect application failure.
2. Create an incident.
3. Collect logs and metrics.
4. Inspect Git history and source code.
5. Identify probable root cause.
6. Generate a reproduction test.
7. Generate a code patch.
8. Run tests in an isolated Docker environment.
9. Create a GitHub pull request.
10. Wait for human approval.
11. Deploy to staging.
12. Verify recovery.

## 6. Safety

- No automatic merging.
- No access to application secrets.
- Isolated test execution.
- Limited agent tool permissions.
- Maximum execution steps and cost per incident.
- Human approval before deployment.

## 7. Success Metrics

- Incident detection time
- Root-cause accuracy
- Time to verified fix
- Fix success rate
- Cost per incident
- Human intervention rate

## 8. Initial MVP

The first milestone is reproducing four failures reliably and observing them through structured logs and metrics.
