import os
import re
import subprocess
from pathlib import Path

REPOSITORY = Path("/workspace").resolve()
SOURCE_ROOT = (REPOSITORY / "apps").resolve()

MAX_FILE_CHARS = 12000
MAX_DIFF_CHARS = 12000
MAX_SEARCH_RESULTS = 30

ALLOWED_EXTENSIONS = {
    ".py", ".txt", ".toml", ".yaml", ".yml",
    ".json", ".md", ".sql"
}

SENSITIVE_NAMES = {
    ".env", ".env.example", "secrets.json",
    "credentials.json"
}


def redact(text):
    patterns = [
        r'(?i)(api[_-]?key|secret|token|password)'
        r'(\s*[:=]\s*)["\'][^"\']+["\']'
    ]

    for pattern in patterns:
        text = re.sub(pattern, r'\1\2"[REDACTED]"', text)

    return text


def run_git(arguments, timeout=10):
    result = subprocess.run(
        ["git", "-C", str(REPOSITORY), *arguments],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False
    )

    if result.returncode != 0:
        return "Git inspection failed: " + result.stderr[:1500]

    return redact(result.stdout)


def git_log(limit=8):
    limit = max(1, min(int(limit), 15))

    return run_git([
        "log",
        f"-{limit}",
        "--date=short",
        "--pretty=format:%h | %ad | %s"
    ])


def git_diff():
    return run_git([
        "diff",
        "--no-ext-diff",
        "--unified=2",
        "HEAD",
        "--",
        "apps/"
    ])[:MAX_DIFF_CHARS]


def git_status():
    return run_git(["status", "--short", "--", "apps/"])


def safe_source_path(relative_path):
    relative_path = Path(relative_path)

    if relative_path.is_absolute():
        raise ValueError("Absolute paths are not allowed")

    if any(part in {"..", ".git"} for part in relative_path.parts):
        raise ValueError("Path traversal is not allowed")

    target = (REPOSITORY / relative_path).resolve()

    if not target.is_relative_to(SOURCE_ROOT):
        raise ValueError("File access is restricted to apps/")

    if target.name.lower() in SENSITIVE_NAMES:
        raise ValueError("Sensitive files cannot be read")

    if target.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError("File type is not allowed")

    if not target.is_file():
        raise FileNotFoundError(str(relative_path))

    return target


def read_file(relative_path):
    target = safe_source_path(relative_path)

    if target.stat().st_size > MAX_FILE_CHARS * 4:
        raise ValueError("File exceeds the allowed size")

    content = target.read_text(
        encoding="utf-8",
        errors="replace"
    )

    return redact(content[:MAX_FILE_CHARS])


def search_code(query):
    query = str(query).strip()

    if not query:
        return "Search query is empty."

    if len(query) > 200:
        raise ValueError("Search query is too long")

    results = []
    query_lower = query.lower()

    for root, directories, filenames in os.walk(SOURCE_ROOT):
        directories[:] = [
            directory for directory in directories
            if directory not in {
                "__pycache__", ".venv", "venv", "node_modules"
            }
        ]

        for filename in filenames:
            path = Path(root) / filename

            if path.suffix.lower() not in ALLOWED_EXTENSIONS:
                continue

            if filename.lower() in SENSITIVE_NAMES:
                continue

            try:
                if path.stat().st_size > 500_000:
                    continue

                lines = path.read_text(
                    encoding="utf-8",
                    errors="replace"
                ).splitlines()

            except (OSError, UnicodeError):
                continue

            for index, line in enumerate(lines):
                if query_lower not in line.lower():
                    continue

                relative = path.relative_to(REPOSITORY)

                start = max(0, index - 2)
                end = min(len(lines), index + 3)

                snippet = "\n".join(
                    f"{number + 1}: {lines[number]}"
                    for number in range(start, end)
                )

                results.append(
                    f"FILE: {relative}\n{redact(snippet)}"
                )

                if len(results) >= MAX_SEARCH_RESULTS:
                    return "\n\n".join(results)

    if not results:
        return f"No source matches found for: {query}"

    return "\n\n".join(results)


def collect_repository_context(incident):
    endpoint = incident.get("endpoint") or ""
    incident_type = incident.get("incident_type") or ""
    service = incident.get("service") or ""

    search_term = endpoint or incident_type or service

    context = {
        "git_status": git_status(),
        "recent_commits": git_log(),
        "tracked_changes": git_diff(),
        "source_search": search_code(search_term)
    }

    matching_files = re.findall(
        r"FILE: (apps/[^\n]+)",
        context["source_search"]
    )

    source_files = []

    for relative_path in dict.fromkeys(matching_files):
        if not relative_path.endswith(".py"):
            continue

        try:
            source_files.append(
                f"===== {relative_path} =====\n"
                f"{read_file(relative_path)}"
            )
        except (OSError, ValueError) as error:
            source_files.append(
                f"Could not inspect {relative_path}: {error}"
            )

        if len(source_files) >= 2:
            break

    context["relevant_source_files"] = (
        "\n\n".join(source_files)
        if source_files
        else "No matching Python source files were found."
    )

    return context
