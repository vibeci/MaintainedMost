#!/usr/bin/env python3
"""Private CI runtime management. Never stream engine output or configuration."""

import argparse
import json
import os
from pathlib import Path
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile


SCRIPTS = Path(__file__).resolve().parent
FAILURE = "Private maintenance operation failed; sensitive diagnostics were suppressed."
MAX_BYTES = 8 << 20


def module(name):
    return runpy.run_path(str(SCRIPTS / name))


def write_private(path, payload):
    with open(path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as output:
        os.fchmod(output.fileno(), 0o600)
        output.write(payload)


def read_private(path):
    path = path.absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError
    with open(path, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES:
            raise ValueError
        value = source.read(MAX_BYTES + 1)
        if len(value) != info.st_size:
            raise ValueError
        return value


def runtime_root():
    root = Path(os.environ["VIBECI_RUN_DIR"])
    parent = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    if (not root.is_absolute() or root.is_symlink() or root.resolve(strict=True).parent != parent
            or not root.name.startswith("maintainedmost-vibeci-") or not root.is_dir()
            or read_private(root / ".runtime-owner") != b"maintainedmost-vibeci\n"):
        raise ValueError
    return root.resolve()


def docker_env(root):
    names = ("PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY",
             "DOCKER_CERT_PATH", "COMPOSE_PROJECT_NAME", "VIBECI_IMAGE", "VIBECI_SANDBOX_IMAGE")
    env = {name: os.environ[name] for name in names if name in os.environ}
    env["VIBECI_RUN_DIR"] = str(root)
    return env


def prepare():
    policy = module("vibeci-private.py")["privacy_policy"]()
    planner = module("vibeci-plan.py")
    models = os.environ["VIBECI_MODELS"].encode()
    planner["parse_models"](models)
    for key in ("VIBECI_IMAGE", "VIBECI_SANDBOX_IMAGE"):
        value = os.environ.get(key, "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}", value):
            raise ValueError
        policy.check(value.encode())
    for key in ("GITHUB_REPOSITORY", "BASE_BRANCH", "COMPOSE_PROJECT_NAME"):
        policy.check(os.environ[key].encode())
    parent = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    checkout = Path(os.environ["GITHUB_WORKSPACE"]).resolve(strict=True)
    if parent == checkout or checkout in parent.parents:
        raise ValueError
    root = Path(tempfile.mkdtemp(prefix="maintainedmost-vibeci-", dir=parent))
    write_private(root / ".runtime-owner", b"maintainedmost-vibeci\n")
    # Register cleanup before any private material is written.
    for key in ("GITHUB_ENV", "GITHUB_OUTPUT"):
        with open(os.environ[key], "a", encoding="utf-8") as output:
            output.write(("VIBECI_RUN_DIR=" if key == "GITHUB_ENV" else "run_dir=") + str(root) + "\n")
    write_private(root / "models.json", models)
    broker = {
        "listen": "/run/vibeci/sandboxd.sock", "docker_host": "unix:///var/run/docker.sock",
        "data_root": "/data", "data_host_path": str(root / "data"),
        "profiles": {"maintainedmost": {
            "image": os.environ["VIBECI_SANDBOX_IMAGE"], "user": "10001:10001",
            "memory": "4g", "cpus": 2, "pids": 512, "tmp_size": "1g",
        }},
    }
    write_private(root / "sandboxd.json", json.dumps(broker).encode())
    # This policy has no credentials or model settings. A capability-dropped
    # UID-0 broker cannot read a host-owned 0600 bind mount.
    (root / "sandboxd.json").chmod(0o444)
    push = (os.environ.get("VIBECI_ENABLED") == "true" and os.environ.get("VIBECI_PUSH") == "true"
            and (os.environ.get("GITHUB_EVENT_NAME") == "schedule" or
                 (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" and os.environ.get("PREVIEW") == "false")))
    summary = planner["plan"](checkout, os.environ["GITHUB_REPOSITORY"], os.environ["BASE_BRANCH"],
                              root / "data", root / "models.json", push=push)
    sealed = root / "public-inputs"
    sealed.mkdir(mode=0o700)
    for name in ("summary.md", "summary.json", "plan.json"):
        path = root / "data" / name
        if path.is_symlink():
            raise ValueError
        if not path.exists():
            continue
        payload = read_private(path)
        policy.check(payload)
        write_private(sealed / name, payload)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("changed=" + str(summary["changed"]).lower() + "\n")


def engine():
    root = runtime_root()
    secrets = root / "secrets"
    secrets.mkdir(mode=0o700)
    try:
        for filename, variable in (("llm_api_key", "VIBECI_LLM_API_KEY"), ("fork_read_token", "VIBECI_FORK_READ_TOKEN")):
            value = os.environ.get(variable, "")
            if not value.strip() or len(value.encode()) > 65536:
                raise ValueError
            path = secrets / filename
            write_private(path, value.encode())
            # The parent stays host-private. Only the harness receives these
            # read-only file mounts; values never enter Docker's environment.
            path.chmod(0o444)
        result_path = root / "result.json"
        with open(result_path, "xb", opener=lambda name, flags: os.open(name, flags, 0o600)) as result:
            command = ["docker", "compose", "-f", "maintenance/compose.yaml", "run", "--rm", "-T",
                       "vibeci", "run", "-config", "/etc/vibeci/config.json", "-dry-run", "-json"]
            status = subprocess.run(command, stdout=result, stderr=subprocess.DEVNULL, env=docker_env(root),
                                    timeout=7350, check=False)
        if status.returncode:
            raise ValueError
        outcomes = module("vibeci-github.py")["decode_json"](read_private(result_path))
        if (not isinstance(outcomes, list) or len(outcomes) != 1 or not isinstance(outcomes[0], dict)
                or outcomes[0].get("repo") != "maintainedmost" or outcomes[0].get("status") != "dry-run"):
            raise ValueError
    finally:
        # unlink is safe even when Docker teardown failed; no private values are
        # retained as named host files. Ephemeral runners remain a requirement.
        for filename in ("llm_api_key", "fork_read_token"):
            (secrets / filename).unlink(missing_ok=True)


def restore():
    root = runtime_root()
    data = root / "data"
    if data.is_symlink() or not data.is_dir():
        raise ValueError
    policy = module("vibeci-private.py")["privacy_policy"]()
    records = []
    for original, target in (("plan.json", "plan.json"), ("summary.json", "public-summary.json"),
                             ("summary.md", "public-summary.md")):
        source = root / "public-inputs" / original
        if source.is_symlink():
            raise ValueError
        if not source.exists():
            continue
        payload = read_private(source)
        policy.check(payload)
        records.append((data / target, payload))
    for destination, payload in records:
        # Replace only known runtime records, without following a harness-created
        # link. The originals are sealed outside every engine/worker mount.
        if destination.exists() or destination.is_symlink():
            destination.unlink()
        write_private(destination, payload)


def evidence():
    root = runtime_root()
    policy = module("vibeci-private.py")["privacy_policy"]()
    files = {}
    for name in ("summary.md", "summary.json", "plan.json"):
        path = root / "public-inputs" / name
        if path.is_symlink():
            raise ValueError
        if path.exists():
            files[name] = read_private(path)
    path = root / "result.json"
    if path.exists() or path.is_symlink():
        safe = module("vibeci-github.py")["sanitize_result"](path)
        files["result.json"] = (json.dumps(safe, sort_keys=True) + "\n").encode()
    summary = files.get("summary.md", b"# Maintenance Result\n") + b"\n## Workflow Result\n\n"
    for key in ("PLAN_OUTCOME", "RUN_OUTCOME", "PUBLISH_OUTCOME", "CLEANUP_OUTCOME"):
        value = os.environ.get(key, "skipped")
        if value not in {"success", "failure", "cancelled", "skipped"}:
            raise ValueError
        summary += (key + ": " + value + "\n\n").encode()
    files["summary.md"] = summary
    # Screen everything before creating any publishable artifact.
    for payload in files.values():
        policy.check(payload)
    artifacts = root / "artifacts"
    artifacts.mkdir(mode=0o700)
    for name, payload in files.items():
        write_private(artifacts / name, payload)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
        output.write(summary.decode("utf-8"))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write("ready=true\n")


def discard():
    root = runtime_root()
    # This exact, marked, newly-created runner-temp directory is the only target.
    # Container teardown is handled separately and remains a required gate.
    try:
        shutil.rmtree(root)
    except PermissionError:
        result = subprocess.run(["sudo", "-n", "rm", "-rf", "--", str(root)], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, env={"PATH": os.defpath}, timeout=60, check=False)
        if result.returncode or root.exists():
            raise ValueError


class PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError


def main():
    try:
        parser = PrivateParser(description=__doc__)
        parser.add_argument("operation", choices=("prepare", "engine", "restore", "evidence", "discard"))
        args = parser.parse_args()
        globals()[args.operation]()
    except Exception:
        print(FAILURE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
