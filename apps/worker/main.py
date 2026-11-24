import ast
import base64
import json
import logging
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from uuid import uuid4

import psycopg
import redis
from google import genai
from google.genai.errors import APIError
from psycopg.types.json import Jsonb

from repository_tools import collect_repository_context, safe_source_path


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sre-worker")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
DATABASE_URL = os.environ["DATABASE_URL"]
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "").strip()
GEMINI_MAX_ATTEMPTS = max(1, min(int(os.getenv("GEMINI_MAX_ATTEMPTS", "2")), 5))
GEMINI_RETRY_BASE_SECONDS = max(1, int(os.getenv("GEMINI_RETRY_BASE_SECONDS", "2")))
TASK_MAX_ATTEMPTS = max(1, min(int(os.getenv("TASK_MAX_ATTEMPTS", "3")), 8))
TASK_RETRY_BASE_SECONDS = max(1, int(os.getenv("TASK_RETRY_BASE_SECONDS", "15")))
DEMO_METRICS_URL = os.getenv("DEMO_METRICS_URL", "http://demo:8000/metrics")
SANDBOX_URL = os.getenv("SANDBOX_URL", "http://sandbox-runner:9000")
SANDBOX_RUNNER_TOKEN = os.getenv("SANDBOX_RUNNER_TOKEN", "").strip()
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
STAGING_BASE_URL = os.getenv("STAGING_BASE_URL", "").strip().rstrip("/")
TASK_QUEUE = "sre:task_queue"
PROCESSING_QUEUE = "sre:task_processing"
DELAYED_TASK_QUEUE = "sre:task_delayed"
TRANSIENT_GEMINI_STATUS_CODES = {408, 429, 500, 502, 503, 504}

redis_client = redis.Redis.from_url(
    REDIS_URL,
    decode_responses=True,
    socket_timeout=10,
    socket_connect_timeout=5,
    health_check_interval=30,
)


class RetryableGeminiError(Exception):
    """Gemini is temporarily unavailable after bounded request retries."""


def update_task(task_id, status, result=None, error=None):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE tasks
                SET status = %s, result = %s, error = %s, updated_at = NOW()
                WHERE id = %s
                """,
                (status, Jsonb(result) if result is not None else None, error, task_id),
            )


def fetch_incident(incident_id):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, incident_type, service, endpoint, status_code,
                       details, status, detected_at
                FROM incidents WHERE id = %s
                """,
                (incident_id,),
            )
            row = cursor.fetchone()

    if row is None:
        raise ValueError(f"Incident not found: {incident_id}")

    return {
        "id": str(row[0]),
        "incident_type": row[1],
        "service": row[2],
        "endpoint": row[3],
        "status_code": row[4],
        "details": row[5] or {},
        "status": row[6],
        "detected_at": row[7].isoformat() if row[7] else None,
    }


def fetch_metrics():
    request = urllib.request.Request(
        DEMO_METRICS_URL,
        headers={"Accept": "text/plain"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.read(12000).decode("utf-8", errors="replace")
    except Exception as error:
        logger.warning("Could not collect demo metrics: %s", error)
        return f"Metrics unavailable: {error}"


def fetch_application_logs(incident):
    log_file = "/var/log/sre/demo.jsonl"
    if not os.path.exists(log_file):
        return "Application log file is not available."

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as file:
            lines = file.readlines()[-1000:]
    except OSError as error:
        logger.warning("Could not read application logs: %s", error)
        return f"Application logs could not be read: {error}"

    entries = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                entries.append(value)
        except json.JSONDecodeError:
            continue

    endpoint = incident.get("endpoint")
    if endpoint:
        relevant = [entry for entry in entries if entry.get("endpoint") == endpoint]
    else:
        relevant = entries[-30:]

    relevant = relevant[-30:]
    if not relevant:
        return "No application log entries matching this incident were found."
    return json.dumps(relevant, indent=2, default=str)


def validate_reproduction_test(
    test_code, endpoint, expected_status, expected_json=None
):
    """Validate a restricted GET test with optional exact JSON-field assertion."""
    if not isinstance(test_code, str) or not test_code.strip():
        raise ValueError("No reproduction test was generated")
    if len(test_code.encode("utf-8")) > 50000:
        raise ValueError("Reproduction test exceeds the size limit")
    if not isinstance(endpoint, str) or not endpoint.startswith("/demo/"):
        raise ValueError("Unsupported reproduction endpoint")
    if type(expected_status) is not int or not 100 <= expected_status <= 599:
        raise ValueError("Invalid expected HTTP status")

    try:
        tree = ast.parse(test_code)
    except SyntaxError as error:
        raise ValueError("Reproduction test contains invalid Python syntax") from error

    allowed_nodes = {
        ast.Module, ast.Import, ast.ImportFrom, ast.alias, ast.ClassDef,
        ast.FunctionDef, ast.arguments, ast.arg, ast.Assign, ast.Name,
        ast.Attribute, ast.Call, ast.keyword, ast.Constant, ast.Expr,
        ast.Load, ast.Store, ast.Subscript,
    }
    unsupported_nodes = [
        node for node in ast.walk(tree)
        if type(node) not in allowed_nodes
    ]
    if unsupported_nodes:
        node = unsupported_nodes[0]
        raise ValueError(
            "Reproduction test contains unsupported Python syntax: "
            f"{type(node).__name__} at line "
            f"{getattr(node, 'lineno', 'unknown')}"
        )

    if len(tree.body) != 4:
        raise ValueError("Test must contain exactly three imports and one class")

    expected_imports = [
        ("unittest", None, "import"),
        ("fastapi.testclient", "TestClient", "from"),
        ("apps.demo.main", "app", "from"),
    ]
    actual_imports = []
    for node in tree.body[:3]:
        if isinstance(node, ast.Import) and len(node.names) == 1 and node.names[0].asname is None:
            actual_imports.append((node.names[0].name, None, "import"))
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and len(node.names) == 1
            and node.names[0].asname is None
        ):
            actual_imports.append((node.module, node.names[0].name, "from"))
        else:
            raise ValueError("Unexpected import in reproduction test")
    if actual_imports != expected_imports:
        raise ValueError("Reproduction test imports do not match the required imports")

    test_class = tree.body[3]
    if not isinstance(test_class, ast.ClassDef) or test_class.decorator_list:
        raise ValueError("Exactly one undecorated test class is required")
    if (
        len(test_class.bases) != 1
        or not isinstance(test_class.bases[0], ast.Attribute)
        or test_class.bases[0].attr != "TestCase"
        or not isinstance(test_class.bases[0].value, ast.Name)
        or test_class.bases[0].value.id != "unittest"
        or len(test_class.body) != 1
    ):
        raise ValueError("Test class must contain one unittest.TestCase method")

    method = test_class.body[0]
    if (
        not isinstance(method, ast.FunctionDef)
        or not method.name.startswith("test_")
        or method.decorator_list
        or method.returns is not None
        or len(method.args.args) != 1
        or method.args.args[0].arg != "self"
        or method.args.vararg is not None
        or method.args.kwarg is not None
        or method.args.kwonlyargs
        or method.args.defaults
        or method.args.kw_defaults
        or len(method.body) != (4 if expected_json is not None else 3)
    ):
        raise ValueError("Invalid reproduction test method")

    client_assignment, response_assignment, assertion = method.body[:3]
    if not (
        isinstance(client_assignment, ast.Assign)
        and len(client_assignment.targets) == 1
        and isinstance(client_assignment.targets[0], ast.Name)
        and client_assignment.targets[0].id == "client"
    ):
        raise ValueError("Invalid TestClient assignment")
    client_call = client_assignment.value
    if not (
        isinstance(client_call, ast.Call)
        and isinstance(client_call.func, ast.Name)
        and client_call.func.id == "TestClient"
        and len(client_call.args) == 1
        and isinstance(client_call.args[0], ast.Name)
        and client_call.args[0].id == "app"
        and len(client_call.keywords) == 1
        and client_call.keywords[0].arg == "raise_server_exceptions"
        and isinstance(client_call.keywords[0].value, ast.Constant)
        and client_call.keywords[0].value.value is False
    ):
        raise ValueError("Invalid TestClient initialization")

    if not (
        isinstance(response_assignment, ast.Assign)
        and len(response_assignment.targets) == 1
        and isinstance(response_assignment.targets[0], ast.Name)
        and response_assignment.targets[0].id == "response"
    ):
        raise ValueError("Invalid response assignment")
    request_call = response_assignment.value
    if not (
        isinstance(request_call, ast.Call)
        and isinstance(request_call.func, ast.Attribute)
        and request_call.func.attr == "get"
        and isinstance(request_call.func.value, ast.Name)
        and request_call.func.value.id == "client"
        and len(request_call.args) == 1
        and not request_call.keywords
        and isinstance(request_call.args[0], ast.Constant)
        and request_call.args[0].value == endpoint
    ):
        raise ValueError("Test must GET the exact incident endpoint")

    if not (isinstance(assertion, ast.Expr) and isinstance(assertion.value, ast.Call)):
        raise ValueError("Invalid HTTP status assertion")
    call = assertion.value
    if not (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "assertEqual"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and len(call.args) == 2
        and not call.keywords
    ):
        raise ValueError("Invalid HTTP status assertion")
    left, right = call.args
    if not (
        isinstance(left, ast.Attribute)
        and left.attr == "status_code"
        and isinstance(left.value, ast.Name)
        and left.value.id == "response"
        and isinstance(right, ast.Constant)
        and type(right.value) is int
        and right.value == expected_status
    ):
        raise ValueError("Test must assert the expected HTTP status code")

    if expected_json is not None:
        if expected_json != {"status": "success"}:
            raise ValueError("Unsupported expected JSON contract")

        json_assertion = method.body[3]
        if not (
            isinstance(json_assertion, ast.Expr)
            and isinstance(json_assertion.value, ast.Call)
        ):
            raise ValueError("Missing expected JSON assertion")

        json_call = json_assertion.value
        if not (
            isinstance(json_call.func, ast.Attribute)
            and json_call.func.attr == "assertEqual"
            and isinstance(json_call.func.value, ast.Name)
            and json_call.func.value.id == "self"
            and len(json_call.args) == 2
            and not json_call.keywords
        ):
            raise ValueError("Invalid expected JSON assertion")

        json_left, json_right = json_call.args
        if not (
            isinstance(json_left, ast.Subscript)
            and isinstance(json_left.value, ast.Call)
            and isinstance(json_left.value.func, ast.Attribute)
            and json_left.value.func.attr == "json"
            and isinstance(json_left.value.func.value, ast.Name)
            and json_left.value.func.value.id == "response"
            and not json_left.value.args
            and not json_left.value.keywords
            and isinstance(json_left.slice, ast.Constant)
            and json_left.slice.value == "status"
            and isinstance(json_right, ast.Constant)
            and json_right.value == "success"
        ):
            raise ValueError("Test must assert response.json()['status'] == 'success'")

    return True


def call_sandbox(files):
    if not SANDBOX_RUNNER_TOKEN:
        raise RuntimeError("SANDBOX_RUNNER_TOKEN is not configured")

    payload = json.dumps({"files": files}).encode("utf-8")
    request = urllib.request.Request(
        SANDBOX_URL.rstrip("/") + "/run",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {SANDBOX_RUNNER_TOKEN}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=65) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict) or not isinstance(result.get("passed"), bool):
        raise ValueError("Sandbox returned an invalid response")
    return result


def run_reproduction_test(incident, test_code):
    endpoint = incident.get("endpoint")
    status_code = incident.get("status_code")
    if not endpoint or status_code is None:
        return {"status": "not_run", "reason": "Incident endpoint or status code is missing"}
    try:
        validate_reproduction_test(test_code, endpoint, int(status_code))
        source_path = safe_source_path("apps/demo/main.py")
        source = source_path.read_text(encoding="utf-8")
        result = call_sandbox({
            "apps/demo/main.py": source,
            "tests/__init__.py": "",
            "tests/test_incident_reproduction.py": test_code,
        })
        return {
            "status": "reproduced" if result["passed"] else "not_reproduced",
            "incident_reproduced": result["passed"],
            "passed": result["passed"],
            "exit_code": result.get("exit_code"),
            "output": str(result.get("output", ""))[:15000],
        }
    except Exception as error:
        logger.exception("Reproduction test failed for incident %s", incident.get("id"))
        return {"status": "rejected_or_error", "reason": str(error)[:2000]}


def generate_gemini_response(client, prompt):
    models = [GEMINI_MODEL]
    if GEMINI_FALLBACK_MODEL and GEMINI_FALLBACK_MODEL != GEMINI_MODEL:
        models.append(GEMINI_FALLBACK_MODEL)

    last_error = None
    for model in models:
        for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
            try:
                logger.info(
                    "Gemini request: model=%s attempt=%s/%s",
                    model, attempt, GEMINI_MAX_ATTEMPTS,
                )
                response = client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config={"response_mime_type": "application/json"},
                )
                if not response.text:
                    raise ValueError("Gemini returned an empty response")
                return response, model
            except APIError as error:
                status_code = getattr(error, "code", None)
                if status_code not in TRANSIENT_GEMINI_STATUS_CODES:
                    raise
                last_error = error
                if attempt < GEMINI_MAX_ATTEMPTS:
                    delay = GEMINI_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Gemini temporary error %s; retrying in %ss",
                        status_code, delay,
                    )
                    time.sleep(delay)
                else:
                    logger.warning("Gemini retries exhausted for model %s", model)

    raise RetryableGeminiError(
        f"Gemini models unavailable after bounded retries: {last_error}"
    ) from last_error


def parse_gemini_json(response_text):
    try:
        value = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise ValueError("Gemini returned invalid JSON") from error
    if not isinstance(value, dict):
        raise ValueError("Gemini response must be a JSON object")
    return value


def save_investigation(incident_id, model, analysis, raw_response):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO investigations (
                    incident_id, provider, model, analysis, raw_response
                )
                VALUES (%s, 'google', %s, %s, %s)
                ON CONFLICT (incident_id) DO UPDATE SET
                    provider = EXCLUDED.provider,
                    model = EXCLUDED.model,
                    analysis = EXCLUDED.analysis,
                    raw_response = EXCLUDED.raw_response,
                    created_at = NOW()
                """,
                (incident_id, model, Jsonb(analysis), raw_response),
            )


def update_saved_analysis(incident_id, analysis):
    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE investigations SET analysis = %s, created_at = NOW() WHERE incident_id = %s",
                (Jsonb(analysis), incident_id),
            )


def validate_patched_source(source):
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Generated patch source is empty")
    if len(source.encode("utf-8")) > 100_000:
        raise ValueError("Generated patch exceeds the source size limit")
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        raise ValueError("Generated patch contains invalid Python syntax") from error

    allowed_imports = {
        "asyncio", "json", "logging", "logging.handlers", "os", "time",
        "fastapi", "fastapi.responses", "prometheus_client",
    }
    forbidden_calls = {
        "eval", "exec", "compile", "__import__", "system", "popen",
        "spawn", "fork", "execv", "execve", "urlopen", "connect",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in allowed_imports:
                    raise ValueError(f"Patch imports a disallowed module: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module not in allowed_imports or node.level:
                raise ValueError(f"Patch imports a disallowed module: {node.module}")
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls:
                raise ValueError(f"Patch contains a disallowed call: {node.func.id}")
            if isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_calls:
                raise ValueError(f"Patch contains a disallowed call: {node.func.attr}")
    return tree


def apply_classification_safeguard(
    analysis, incident, logs, repository_context
):
    endpoint = str(incident.get("endpoint") or "").lower()
    status_code = incident.get("status_code")

    source_context = str(
        repository_context.get("relevant_source_files", "")
    ).lower()
    log_context = str(logs or "").lower()

    simulation_markers = (
        "intentional demonstration exception",
        "simulated application failure",
        "simulated database unavailable",
    )

    source_confirms_simulation = (
        bool(endpoint)
        and endpoint in source_context
        and any(marker in source_context for marker in simulation_markers)
    )

    logs_confirm_simulation = any(
        marker in log_context for marker in simulation_markers
    )

    if source_confirms_simulation and logs_confirm_simulation:
        analysis["incident_classification"] = "intentional_simulation"

        safeguard_evidence = (
            "Deterministic safeguard confirmed intentional simulation: "
            "the matching application source and structured logs both "
            "contain explicit simulation evidence."
        )

        if safeguard_evidence not in analysis["evidence"]:
            analysis["evidence"].append(safeguard_evidence)

        analysis["recommended_remediation"] = [
            "No automatic remediation required: source code and logs "
            "confirm this is an intentional simulation."
        ]

        logger.warning(
            "Classification safeguard: incident %s confirmed as "
            "intentional_simulation from source and log evidence",
            incident.get("id"),
        )

    elif endpoint == "/demo/ok" and type(status_code) is int and status_code == 500:
        regression_test = load_regression_test()

        expected_contract = (
            'self.client.get("/demo/ok")',
            'self.assertEqual(response.status_code, 200)',
            'self.assertEqual(response.json()["status"], "success")',
        )

        if all(statement in regression_test for statement in expected_contract):
            analysis["incident_classification"] = "genuine_defect"

            contract_evidence = (
                "Deterministic regression-contract safeguard: the "
                "maintained regression suite requires GET /demo/ok to "
                "return HTTP 200 with JSON status success, but the "
                "observed incident returned HTTP 500."
            )

            if contract_evidence not in analysis["evidence"]:
                analysis["evidence"].append(contract_evidence)

            analysis["recommended_remediation"] = [
                "Investigate and correct the /demo/ok failure while "
                "preserving its established HTTP 200 success response "
                "and all unrelated endpoint behaviours."
            ]

            logger.warning(
                "Classification safeguard: incident %s confirmed as "
                "genuine_defect against the permanent regression contract",
                incident.get("id"),
            )

    return analysis


def validate_investigation(analysis):
    required = {
        "summary", "root_cause", "evidence", "severity", "confidence",
        "recommended_remediation", "reproduction_test", "reproduction_test_code",
        "incident_classification",
    }
    if not required.issubset(analysis):
        raise ValueError("Gemini investigation is missing required fields")
    for field in (
        "summary", "root_cause", "reproduction_test",
        "reproduction_test_code", "incident_classification",
    ):
        if not isinstance(analysis[field], str):
            raise ValueError(f"Gemini field {field} must be a string")
    for field in ("evidence", "recommended_remediation"):
        if not isinstance(analysis[field], list) or not all(
            isinstance(item, str) for item in analysis[field]
        ):
            raise ValueError(f"Gemini field {field} must be an array of strings")
    confidence = analysis["confidence"]
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ValueError("Gemini confidence must be numeric")
    if not 0 <= float(confidence) <= 1:
        raise ValueError("Gemini confidence must be between 0 and 1")
    classification = analysis["incident_classification"].strip().lower()
    if classification not in {
        "genuine_defect", "intentional_simulation", "uncertain"
    }:
        raise ValueError("Gemini returned an invalid incident classification")
    analysis["incident_classification"] = classification

    severity = str(analysis["severity"]).lower()
    if severity not in {"low", "medium", "high", "critical"}:
        raise ValueError("Gemini returned an invalid severity")
    analysis["confidence"] = float(confidence)
    analysis["severity"] = severity
    return analysis


def github_request(method, path, payload=None):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        raise RuntimeError("GITHUB_TOKEN and GITHUB_REPOSITORY must be configured")

    url = "https://api.github.com" + path
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {GITHUB_TOKEN}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-sre-agent",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"GitHub API {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub connection failed: {error.reason}") from error


def create_github_pull_request(
    incident,
    patched_source,
    change_summary,
    analysis,
    sandbox_result,
    patch_model,
):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return {
            "status": "not_configured",
            "reason": "Set GITHUB_TOKEN and GITHUB_REPOSITORY to enable pull requests",
        }

    repository_parts = GITHUB_REPOSITORY.strip("/").split("/")
    if len(repository_parts) != 2 or not all(repository_parts):
        raise ValueError("GITHUB_REPOSITORY must use the owner/repository format")

    owner, repository_name = repository_parts
    repo_path = urllib.parse.quote(
        f"{owner}/{repository_name}", safe="/"
    )

    repository = github_request("GET", f"/repos/{repo_path}")
    base_branch = repository["default_branch"]

    incident_id = str(incident["id"])
    branch = f"ai-sre/incident-{incident_id[:12]}"

    # Reuse an existing open PR instead of creating duplicates on retries.
    pull_query = urllib.parse.urlencode({
        "state": "open",
        "head": f"{owner}:{branch}",
        "base": base_branch,
    })
    existing_pulls = github_request(
        "GET", f"/repos/{repo_path}/pulls?{pull_query}"
    )

    if existing_pulls:
        existing_pull = existing_pulls[0]
        return {
            "status": "already_exists",
            "number": existing_pull["number"],
            "url": existing_pull["html_url"],
            "branch": branch,
            "base": base_branch,
        }

    encoded_branch = urllib.parse.quote(branch, safe="")
    ref_path = f"/repos/{repo_path}/git/ref/heads/{encoded_branch}"

    # Reuse the incident branch if a previous attempt created it.
    try:
        github_request("GET", ref_path)
    except RuntimeError as error:
        if "GitHub API 404" not in str(error):
            raise

        base_ref = github_request(
            "GET",
            f"/repos/{repo_path}/git/ref/heads/"
            f"{urllib.parse.quote(base_branch, safe='')}",
        )

        try:
            github_request(
                "POST",
                f"/repos/{repo_path}/git/refs",
                {
                    "ref": f"refs/heads/{branch}",
                    "sha": base_ref["object"]["sha"],
                },
            )
        except RuntimeError as create_error:
            # A concurrent retry may have created the same branch.
            if "GitHub API 422" not in str(create_error):
                raise
            github_request("GET", ref_path)

    file_path = "apps/demo/main.py"
    encoded_file_path = urllib.parse.quote(file_path, safe="/")
    content_path = f"/repos/{repo_path}/contents/{encoded_file_path}"
    branch_query = urllib.parse.urlencode({"ref": branch})

    content_payload = {
        "message": f"fix: remediate incident {incident_id[:12]}",
        "content": base64.b64encode(
            patched_source.encode("utf-8")
        ).decode("ascii"),
        "branch": branch,
    }

    # Avoid creating another commit when the branch already has this patch.
    try:
        existing_file = github_request(
            "GET", f"{content_path}?{branch_query}"
        )
        existing_content = base64.b64decode(
            existing_file["content"]
        ).decode("utf-8")

        if existing_content != patched_source:
            content_payload["sha"] = existing_file["sha"]
            github_request("PUT", content_path, content_payload)
    except RuntimeError as error:
        if "GitHub API 404" not in str(error):
            raise
        github_request("PUT", content_path, content_payload)

    evidence = analysis.get("evidence", [])
    if not isinstance(evidence, (list, dict)):
        evidence = [str(evidence)]

    evidence_text = json.dumps(evidence, indent=2, default=str)[:5000]
    root_cause = str(analysis.get("root_cause", "Not provided"))[:3000]
    severity = str(analysis.get("severity", "Not provided"))
    confidence = analysis.get("confidence", "Not provided")
    classification = str(
        analysis.get("incident_classification", "Not provided")
    )
    sandbox_output = str(sandbox_result.get("output", ""))[:8000]
    sandbox_passed = sandbox_result.get("passed") is True
    sandbox_exit_code = sandbox_result.get("exit_code", "Not provided")

    body = (
        "## Automated incident remediation proposal\n\n"
        f"**Incident ID:** `{incident_id}`\n\n"
        f"**Service:** `{incident.get('service', 'Not provided')}`\n\n"
        f"**Endpoint:** `{incident.get('endpoint', 'Not provided')}`\n\n"
        f"**Incident type:** `{incident.get('incident_type', 'Not provided')}`\n\n"
        f"**Severity:** `{severity}`\n\n"
        f"**Classification:** `{classification}`\n\n"
        f"**Investigation confidence:** `{confidence}`\n\n"
        "## Root cause\n\n"
        f"{root_cause}\n\n"
        "## Supporting evidence\n\n"
        f"```json\n{evidence_text}\n```\n\n"
        "## Proposed change\n\n"
        f"{change_summary}\n\n"
        "## Isolated sandbox verification\n\n"
        f"- Passed: **{sandbox_passed}**\n"
        f"- Exit code: `{sandbox_exit_code}`\n"
        f"- Model: `{patch_model}`\n\n"
        "### Test output\n\n"
        f"```text\n{sandbox_output}\n```\n\n"
        "## Review requirements\n\n"
        "- Review the complete source diff before merging.\n"
        "- Confirm CI results and run any additional required checks.\n"
        "- This agent does not merge pull requests.\n"
        "- Human approval is required for all changes."
    )

    pull = github_request(
        "POST",
        f"/repos/{repo_path}/pulls",
        {
            "title": (
                f"AI SRE fix: {incident.get('incident_type', 'incident')} "
                f"({incident_id[:8]})"
            ),
            "head": branch,
            "base": base_branch,
            "body": body,
        },
    )

    return {
        "status": "pull_request_created",
        "number": pull["number"],
        "url": pull["html_url"],
        "branch": branch,
        "base": base_branch,
    }


def load_regression_test():
    """Load the maintained regression suite from the read-only repository mount."""
    tests_root = Path("/workspace/tests").resolve()
    test_path = (tests_root / "test_demo_regression.py").resolve()

    if not test_path.is_relative_to(tests_root):
        raise ValueError("Regression test path is outside the tests directory")
    if not test_path.is_file():
        raise FileNotFoundError(
            "Required regression suite is missing: tests/test_demo_regression.py"
        )
    if test_path.stat().st_size > 100_000:
        raise ValueError("Regression test suite exceeds the 100 KB limit")

    test_code = test_path.read_text(encoding="utf-8")
    if not test_code.strip():
        raise ValueError("Regression test suite is empty")
    return test_code


def generate_and_verify_patch(client, incident, analysis, model):
    status_code = incident.get("status_code")
    endpoint = incident.get("endpoint")
    confidence = analysis.get("confidence")

    if analysis.get("incident_classification") != "genuine_defect":
        return {
            "status": "not_supported",
            "reason": "Automatic patching requires a genuine_defect classification",
        }

    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or confidence < 0.80
    ):
        return {
            "status": "not_supported",
            "reason": "Automatic patching requires investigation confidence of at least 0.80",
        }

    if endpoint != "/demo/ok" or type(status_code) is not int or status_code != 500:
        return {
            "status": "not_supported",
            "reason": "Automatic patching currently supports only HTTP 500 incidents on /demo/ok",
        }

    source_path = safe_source_path("apps/demo/main.py")
    current_source = source_path.read_text(encoding="utf-8")
    prompt = f"""
You are a senior Python/FastAPI reliability engineer.
Return ONLY one JSON object with exactly these fields:
- patched_source: complete replacement source for apps/demo/main.py
- change_summary: concise explanation of the root-cause fix
- verification_test_code: complete Python unittest source

Repair scope:
- The only supported endpoint is GET /demo/ok.
- Its established behavior is HTTP 200 with JSON {{"status": "success"}}.
- The permanent regression suite is the behavioral contract.
- Fix the underlying cause of the unexpected HTTP 500. Do not hide it by
  weakening error handling, returning a fabricated success response, or
  changing unrelated routes.

Rules:
1. Make the smallest root-cause correction supported by the supplied source
   and investigation evidence.
2. Preserve all existing routes, response contracts, metrics, and logging.
3. Do not add secrets, external services, subprocess calls, shell commands,
   file writes, or network clients.
4. Do not remove or weaken existing functionality or security checks.
5. verification_test_code must use exactly these imports in order:
   import unittest
   from fastapi.testclient import TestClient
   from apps.demo.main import app
6. It must contain one unittest.TestCase class with one test_ method and
   exactly four statements:
   client = TestClient(app, raise_server_exceptions=False)
   response = client.get("/demo/ok")
   self.assertEqual(response.status_code, 200)
   self.assertEqual(response.json()["status"], "success")
7. Do not use Markdown fences.
8. Do not claim tests were executed. The isolated sandbox will verify the patch.

INCIDENT:
{json.dumps(incident, default=str)}
INVESTIGATION:
{json.dumps(analysis, default=str)}
PERMANENT REGRESSION SUITE:
{load_regression_test()}
CURRENT SOURCE:
{current_source}
"""
    response, patch_model = generate_gemini_response(client, prompt)
    patch_data = parse_gemini_json(response.text)
    if set(patch_data) != {"patched_source", "change_summary", "verification_test_code"}:
        raise ValueError("Gemini patch response has unexpected fields")
    patched_source = patch_data["patched_source"]
    change_summary = patch_data["change_summary"]
    test_code = patch_data["verification_test_code"]
    if not isinstance(patched_source, str) or not patched_source.strip():
        raise ValueError("Gemini did not return patched source")
    if len(patched_source.encode("utf-8")) > 100_000:
        raise ValueError("Generated patch exceeds the source size limit")
    if not isinstance(change_summary, str) or not change_summary.strip():
        raise ValueError("Gemini did not return a change summary")
    validate_patched_source(patched_source)
    validate_reproduction_test(
        test_code, endpoint, 200, expected_json={"status": "success"}
    )

    regression_test_code = load_regression_test()
    sandbox_result = call_sandbox({
        "apps/demo/main.py": patched_source,
        "tests/__init__.py": "",
        "tests/test_patch_verification.py": test_code,
        "tests/test_demo_regression.py": regression_test_code,
    })
    patch_result = {
        "status": "verified" if sandbox_result["passed"] else "verification_failed",
        "model": patch_model,
        "change_summary": change_summary,
        "sandbox": {
            "passed": sandbox_result["passed"],
            "exit_code": sandbox_result.get("exit_code"),
            "output": str(sandbox_result.get("output", ""))[:15000],
        },
    }
    if not sandbox_result["passed"]:
        return patch_result

    try:
        patch_result["github"] = create_github_pull_request(
            incident,
            patched_source,
            change_summary,
            analysis,
            sandbox_result,
            patch_model,
        )
    except Exception as error:
        logger.exception("Verified patch could not be submitted to GitHub")
        patch_result["github"] = {
            "status": "failed",
            "reason": str(error)[:2000],
        }
    return patch_result


def investigate_incident(incident_id):
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")

    incident = fetch_incident(incident_id)
    metrics = fetch_metrics()
    logs = fetch_application_logs(incident)
    repository_context = collect_repository_context(incident)
    client = genai.Client(api_key=GEMINI_API_KEY)

    prompt = f"""
You are an AI Site Reliability Engineering investigator.
Analyze only the evidence provided. Separate confirmed observations from hypotheses.
Return one JSON object with exactly these fields:
summary (string), root_cause (string), evidence (array of strings),
severity (low|medium|high|critical), confidence (number 0..1),
incident_classification (genuine_defect|intentional_simulation|uncertain),
recommended_remediation (array of strings), reproduction_test (string),
reproduction_test_code (string).

Classify the incident using the supplied source code, logs, metrics and repository context:
- genuine_defect: evidence indicates unexpected behavior that violates intended application behavior.
- intentional_simulation: evidence indicates the behavior is deliberately implemented for testing,
  demonstrations, chaos engineering or simulation.
- uncertain: evidence is insufficient or contradictory.

Do not classify an incident as a genuine defect merely because it returned HTTP 4xx or 5xx.
An endpoint explicitly implemented to simulate failures must be classified as intentional_simulation.
When intent cannot be established confidently, choose uncertain.
If endpoint or status code is missing, or the endpoint is not under /demo/,
set reproduction_test_code to an empty string.
For supported incidents, the reproduction test must be complete Python source:
import unittest
from fastapi.testclient import TestClient
from apps.demo.main import app
class TestIncidentReproduction(unittest.TestCase):
    def test_incident(self):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(<exact endpoint string>)
        self.assertEqual(response.status_code, <observed integer status>)
Do not add statements, imports, assertions, decorators or helper functions.
Do not claim to have executed tests, fixed code or inspected evidence not supplied.
INCIDENT:
{json.dumps(incident, default=str)}
PROMETHEUS METRICS:
{metrics}
STRUCTURED APPLICATION LOGS:
{logs}
READ-ONLY REPOSITORY CONTEXT:
{json.dumps(repository_context, default=str)}
"""
    response, used_model = generate_gemini_response(client, prompt)
    analysis = validate_investigation(parse_gemini_json(response.text))
    analysis = apply_classification_safeguard(
        analysis, incident, logs, repository_context
    )
    if analysis["reproduction_test_code"]:
        analysis["sandbox_verification"] = run_reproduction_test(
            incident, analysis["reproduction_test_code"]
        )
    else:
        analysis["sandbox_verification"] = {
            "status": "not_run",
            "reason": "No safe reproduction test was generated",
        }

    save_investigation(incident_id, used_model, analysis, response.text)

    if analysis["sandbox_verification"].get("status") == "reproduced":
        if analysis["incident_classification"] == "genuine_defect":
            try:
                analysis["patch_workflow"] = generate_and_verify_patch(
                    client, incident, analysis, used_model
                )
            except RetryableGeminiError:
                raise
            except Exception as error:
                logger.exception("Patch workflow failed for incident %s", incident_id)
                analysis["patch_workflow"] = {
                    "status": "failed",
                    "reason": str(error)[:2000],
                }
        else:
            analysis["patch_workflow"] = {
                "status": "skipped",
                "reason": (
                    "Automatic patch generation requires a genuine defect. "
                    "This incident was classified as "
                    + analysis["incident_classification"]
                    + "; human review is required."
                ),
            }
        update_saved_analysis(incident_id, analysis)

    return {
        "incident_id": incident_id,
        "provider": "google",
        "model": used_model,
        "analysis": analysis,
    }


def verify_staging(incident_id):
    if not STAGING_BASE_URL:
        raise ValueError("STAGING_BASE_URL is not configured")
    incident = fetch_incident(incident_id)
    health_url = STAGING_BASE_URL + "/health"
    request = urllib.request.Request(health_url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(5000).decode("utf-8", errors="replace")
            result = {
                "status": "healthy" if 200 <= response.status < 300 else "unhealthy",
                "url": health_url,
                "http_status": response.status,
                "response": body,
                "verified_at": time.time(),
            }
    except Exception as error:
        result = {
            "status": "unhealthy",
            "url": health_url,
            "error": str(error)[:1000],
            "verified_at": time.time(),
        }

    with psycopg.connect(DATABASE_URL) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT analysis FROM investigations WHERE incident_id = %s",
                (incident_id,),
            )
            row = cursor.fetchone()
            if row:
                analysis = row[0] or {}
                analysis["staging_verification"] = result
                cursor.execute(
                    "UPDATE investigations SET analysis = %s, created_at = NOW() WHERE incident_id = %s",
                    (Jsonb(analysis), incident_id),
                )
    return {"incident_id": incident["id"], "staging_verification": result}


def schedule_task_retry(task_id, error):
    attempt_key = f"sre:task_attempts:{task_id}"
    attempt = redis_client.incr(attempt_key)
    redis_client.expire(attempt_key, 7 * 24 * 60 * 60)
    if attempt >= TASK_MAX_ATTEMPTS:
        update_task(
            task_id, "failed",
            error=f"Retry limit ({TASK_MAX_ATTEMPTS} executions) reached: {error}",
        )
        redis_client.delete(attempt_key)
        logger.error("Task permanently failed after %s attempts: %s", attempt, task_id)
        return

    delay = TASK_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    redis_client.zadd(DELAYED_TASK_QUEUE, {task_id: time.time() + delay})
    update_task(
        task_id, "retrying",
        error=f"Temporary Gemini outage; retry {attempt}/{TASK_MAX_ATTEMPTS - 1}: {error}",
    )
    logger.warning(
        "Task retry scheduled: task=%s retry=%s/%s delay=%ss",
        task_id, attempt, TASK_MAX_ATTEMPTS - 1, delay,
    )


def promote_due_tasks():
    due = redis_client.zrangebyscore(
        DELAYED_TASK_QUEUE, min="-inf", max=time.time(), start=0, num=50
    )
    promote_script = (
        "if redis.call('ZREM', KEYS[1], ARGV[1]) == 1 then "
        "return redis.call('LPUSH', KEYS[2], ARGV[1]) "
        "else return 0 end"
    )
    for task_id in due:
        redis_client.eval(
            promote_script, 2, DELAYED_TASK_QUEUE, TASK_QUEUE, task_id
        )


def process_task(task_id):
    logger.info("Processing task: %s", task_id)
    try:
        update_task(task_id, "processing")
        with psycopg.connect(DATABASE_URL) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT task_type, payload FROM tasks WHERE id = %s",
                    (task_id,),
                )
                task = cursor.fetchone()
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        task_type, payload = task
        if task_type == "investigate_incident":
            incident_id = (payload or {}).get("incident_id")
            if not incident_id:
                raise ValueError("Investigation task has no incident_id")
            result = investigate_incident(incident_id)
        elif task_type == "verify_staging":
            incident_id = (payload or {}).get("incident_id")
            if not incident_id:
                raise ValueError("Staging verification task has no incident_id")
            result = verify_staging(incident_id)
        elif task_type == "demo_task":
            result = {
                "message": "Task processed successfully",
                "original_payload": payload,
                "worker": "sre-worker",
            }
        else:
            raise ValueError(f"Unsupported task type: {task_type}")

        update_task(task_id, "completed", result=result)
        redis_client.delete(f"sre:task_attempts:{task_id}")
        logger.info("Task completed: %s", task_id)
    except RetryableGeminiError as error:
        logger.exception("Temporary Gemini failure for task: %s", task_id)
        try:
            schedule_task_retry(task_id, error)
        except Exception as retry_error:
            logger.exception("Could not schedule retry for task %s", task_id)
            try:
                update_task(task_id, "failed", error=f"Retry scheduling failed: {retry_error}")
            except Exception:
                logger.exception("Could not persist task failure: %s", task_id)
    except Exception as error:
        logger.exception("Task failed: %s", task_id)
        try:
            update_task(task_id, "failed", error=str(error))
        except Exception:
            logger.exception("Could not update failed task: %s", task_id)


def recover_processing_tasks():
    recovered = 0
    while True:
        task_id = redis_client.rpoplpush(PROCESSING_QUEUE, TASK_QUEUE)
        if task_id is None:
            break
        recovered += 1
    if recovered:
        logger.warning("Recovered %s interrupted task(s)", recovered)


def main():
    logger.info("SRE Worker started")
    logger.info("Listening for tasks on %s", TASK_QUEUE)
    logger.info("Gemini primary model: %s", GEMINI_MODEL)
    logger.info("Gemini fallback model: %s", GEMINI_FALLBACK_MODEL or "disabled")
    logger.info("Sandbox URL: %s", SANDBOX_URL)
    logger.info("GitHub PR automation: %s", "enabled" if GITHUB_TOKEN and GITHUB_REPOSITORY else "disabled")

    while True:
        try:
            redis_client.ping()
            recover_processing_tasks()
            promote_due_tasks()
            item = redis_client.brpoplpush(TASK_QUEUE, PROCESSING_QUEUE, timeout=5)
            if item is None:
                continue
            try:
                process_task(item)
            finally:
                redis_client.lrem(PROCESSING_QUEUE, 1, item)
        except redis.RedisError:
            logger.exception("Redis connection error")
            time.sleep(5)
        except Exception:
            logger.exception("Unexpected worker error")
            time.sleep(5)


if __name__ == "__main__":
    main()
