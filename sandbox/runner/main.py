import io
import os
import re
import hmac
import subprocess
import tarfile
import logging
from pathlib import PurePosixPath

import docker
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("sandbox-runner")

app = FastAPI(title="AI SRE Sandbox Runner")

SANDBOX_IMAGE = os.getenv(
    "SANDBOX_IMAGE",
    "ai-sre-sandbox-base:latest"
)

SANDBOX_RUNNER_TOKEN = os.getenv("SANDBOX_RUNNER_TOKEN", "").strip()

MAX_FILES = 10
MAX_FILE_SIZE = 100_000
MAX_TOTAL_SIZE = 500_000
MAX_OUTPUT_SIZE = 20_000

ALLOWED_PREFIXES = ("apps/demo/", "tests/")
ALLOWED_EXTENSIONS = {".py", ".txt"}

docker_client = docker.from_env(timeout=75)


class SandboxRequest(BaseModel):
    files: dict[str, str] = Field(min_length=1, max_length=MAX_FILES)


def validate_files(files):
    validated = {}
    total_size = 0

    for filename, content in files.items():
        path = PurePosixPath(filename)

        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe file path")

        if not filename.startswith(ALLOWED_PREFIXES):
            raise ValueError("Files must be inside apps/demo/ or tests/")

        if path.suffix not in ALLOWED_EXTENSIONS:
            raise ValueError("Unsupported file extension")

        if not isinstance(content, str):
            raise ValueError("File content must be text")

        encoded = content.encode("utf-8")

        if len(encoded) > MAX_FILE_SIZE:
            raise ValueError(f"File too large: {filename}")

        total_size += len(encoded)

        if total_size > MAX_TOTAL_SIZE:
            raise ValueError("Total source size exceeds the limit")

        validated[filename] = encoded

    if not any(
        re.fullmatch(r"tests/test_[A-Za-z0-9_]+\.py", name)
        for name in validated
    ):
        raise ValueError("A valid tests/test_*.py file is required")

    return validated


def create_archive(files):
    files = dict(files)
    files.setdefault("tests/__init__.py", b"")
    buffer = io.BytesIO()

    with tarfile.open(fileobj=buffer, mode="w") as archive:
        directories = set()

        for filename in files:
            parent = PurePosixPath(filename).parent
            while str(parent) != ".":
                directories.add(str(parent))
                parent = parent.parent

        for directory in sorted(directories):
            info = tarfile.TarInfo(name=directory + "/")
            info.type = tarfile.DIRTYPE
            info.mode = 0o755
            info.uid = 10001
            info.gid = 10001
            archive.addfile(info)

        for filename, content in files.items():
            info = tarfile.TarInfo(name=filename)
            info.size = len(content)
            info.mode = 0o644
            info.uid = 10001
            info.gid = 10001
            info.type = tarfile.REGTYPE
            archive.addfile(info, io.BytesIO(content))

    buffer.seek(0)
    return buffer.getvalue()


@app.get("/health")
def health():
    try:
        docker_client.ping()
        docker_client.images.get(SANDBOX_IMAGE)

        return {
            "status": "ready",
            "docker": "connected",
            "sandbox_image": SANDBOX_IMAGE
        }

    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail=f"Sandbox unavailable: {str(error)[:300]}"
        )


def authenticate_runner_request(authorization: str | None):
    if not SANDBOX_RUNNER_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Sandbox runner authentication is not configured",
        )

    scheme, separator, supplied_token = (authorization or "").partition(" ")

    supplied_token_bytes = supplied_token.strip().encode("utf-8")
    expected_token_bytes = SANDBOX_RUNNER_TOKEN.encode("utf-8")

    if (
        not separator
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied_token_bytes, expected_token_bytes)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing sandbox runner token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.post("/run")
def run_tests(
    request: SandboxRequest,
    authorization: str | None = Header(default=None),
):
    authenticate_runner_request(authorization)
    container = None

    try:
        files = validate_files(request.files)
        archive = create_archive(files)

        container = docker_client.containers.create(
            image=SANDBOX_IMAGE,
            command=["python", "-c", "import time; time.sleep(300)"],
            working_dir="/workspace",
            user="10001:10001",
            network_mode="none",
            read_only=False,
            mem_limit="512m",
            nano_cpus=1_000_000_000,
            pids_limit=128,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            tmpfs={
                "/tmp": "rw,noexec,nosuid,nodev,size=64m",
                "/workspace": "rw,noexec,nosuid,nodev,size=128m,uid=10001,gid=10001,mode=755",
                "/var/log/sre": "rw,noexec,nosuid,nodev,size=16m,uid=10001,gid=10001,mode=755",
            },
            labels={
                "app": "ai-sre-agent",
                "purpose": "isolated-test"
            }
        )

        container.start()

        import base64
        import json

        transfer_data = {
            name: base64.b64encode(content).decode("ascii")
            for name, content in files.items()
        }
        transfer_payload = base64.b64encode(
            json.dumps(transfer_data).encode("utf-8")
        ).decode("ascii")

        transfer_script = (
            "import base64,json,os,sys\n"
            "files=json.loads(base64.b64decode(sys.argv[1]))\n"
            "for name,content in files.items():\n"
            " os.makedirs(os.path.dirname(name), exist_ok=True)\n"
            " with open(name, \"wb\") as target: target.write(base64.b64decode(content))\n"
            "print(\"Transferred files:\", sorted(files), flush=True)\n"
        )

        transfer_result = container.exec_run(
            ["python", "-c", transfer_script, transfer_payload],
            workdir="/workspace",
            user="10001:10001",
        )
        transfer_output = (transfer_result.output or b"").decode(
            "utf-8", errors="replace"
        )
        logger.info("Sandbox file transfer: %s", transfer_output)

        if transfer_result.exit_code != 0:
            raise RuntimeError(
                f"Could not write files into sandbox: {transfer_output[:1000]}"
            )

        test_command = (
            "import subprocess,sys\n"
            "try:\n"
            " result=subprocess.run("
            "[sys.executable,'-m','unittest','discover',"
            "'-s','/workspace/tests','-p','test_*.py','-v'],"
            "cwd='/workspace',timeout=45)\n"
            " sys.exit(result.returncode)\n"
            "except subprocess.TimeoutExpired:\n"
            " print('TEST_TIMEOUT: exceeded 45 seconds',flush=True)\n"
            " sys.exit(124)\n"
        )

        execution = container.exec_run(
            ["python", "-c", test_command],
            workdir="/workspace",
            user="10001:10001",
            stdout=True,
            stderr=True,
            demux=False,
        )

        raw_output = execution.output or b""
        if isinstance(raw_output, tuple):
            raw_output = b"".join(part or b"" for part in raw_output)
        output = raw_output.decode(
            "utf-8",
            errors="replace"
        )[-MAX_OUTPUT_SIZE:]

        exit_code = execution.exit_code

        logger.info(
            "Sandbox execution completed with exit code %s",
            exit_code
        )

        return {
            "passed": exit_code == 0,
            "exit_code": exit_code,
            "output": output
        }

    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    except Exception as error:
        logger.exception("Sandbox execution failed")
        raise HTTPException(
            status_code=500,
            detail=f"Sandbox execution failed: {str(error)[:500]}"
        )

    finally:
        if container is not None:
            try:
                container.remove(force=True)
            except Exception:
                logger.exception("Could not remove sandbox container")
