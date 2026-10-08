#!/usr/bin/env python3
"""Fail-closed GitHub preflight and publication for a trusted VibeCI plan.

GH_TOKEN must be the dedicated PAT/App token, never the workflow GITHUB_TOKEN.
The operator must give that identity no branch-rule bypass rights; GitHub's
repository/protection APIs cannot prove all of a token's effective privileges.
"""

import argparse
import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import math
import os
from pathlib import Path
import re
import runpy
import subprocess
import sys
import time
from urllib.parse import quote


REQUIRED_CONTEXTS = (
    "MaintainedMost infrastructure tests",
    "MaintainedMost regression tests and complete images",
)
ACTIONS_APP_ID = 15368
MAX_JSON_BYTES = 8 << 20
CHECK_TIMEOUT = 2 * 60 * 60
CHECK_INTERVAL = 20
RESULT_STATUSES = {"synced", "dry-run", "up-to-date", "failed", "error", "backoff", "blocked", "held", "raced", "locked", "disabled"}
PATCH_COUNTS = {"total", "exact", "shifted", "refreshed", "dropped", "agent"}


class GitHubError(ValueError):
    pass


def full_sha(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value) or value == "0" * 40:
        raise GitHubError("expected a full nonzero lowercase commit SHA")
    return value


def decode_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise GitHubError("duplicate JSON keys are not allowed")
            result[key] = value
        return result

    def invalid(_):
        raise GitHubError("non-finite JSON numbers are not allowed")

    def finite(value):
        number = float(value)
        return number if math.isfinite(number) else invalid(value)

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=invalid, parse_float=finite)
    except (ValueError, RecursionError, UnicodeError) as error:
        if isinstance(error, GitHubError):
            raise
        raise GitHubError("invalid JSON response or evidence") from error


def read_evidence(path):
    if path.is_symlink() or not path.is_file():
        raise GitHubError("evidence must be a regular file, not a symlink")
    with path.open("rb") as source:
        text = source.read(MAX_JSON_BYTES + 1)
    if len(text) > MAX_JSON_BYTES:
        raise GitHubError("evidence exceeds the size limit")
    return text


def sanitize_result(path):
    """Reconstruct evidence; never preserve provider-controlled text or keys."""
    try:
        outcomes = decode_json(read_evidence(path))
        if (not isinstance(outcomes, list) or len(outcomes) != 1 or not isinstance(outcomes[0], dict)
                or outcomes[0].get("repo") != "maintainedmost"):
            raise GitHubError("invalid result shape")
        outcome = outcomes[0]
        if not isinstance(outcome.get("status"), str) or outcome["status"] not in RESULT_STATUSES:
            raise GitHubError("unknown result status")
        safe = {"repo": "maintainedmost", "status": outcome["status"]}
        for key in ("commit", "fork_head", "upstream", "target"):
            if key in outcome:
                safe[key] = full_sha(outcome[key])
        for key in ("version", "pinned"):
            if key in outcome:
                value = outcome[key]
                if isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,18}", value):
                    value = int(value)
                if type(value) is not int or not 0 <= value < (1 << 63):
                    raise GitHubError("invalid numeric selector")
                safe[key] = value
        if "patches" in outcome:
            if not isinstance(outcome["patches"], dict):
                raise GitHubError("invalid patch counts")
            safe["patches"] = {}
            for key in sorted(PATCH_COUNTS & outcome["patches"].keys()):
                value = outcome["patches"][key]
                if type(value) is not int or not 0 <= value < (1 << 63):
                    raise GitHubError("invalid patch count")
                safe["patches"][key] = value
        return [safe]
    except (GitHubError, OSError, UnicodeError):
        return [{"repo": "maintainedmost", "status": "error"}]


def command(args, token=None, timeout=60, extra_env=None):
    env = {key: os.environ[key] for key in ("PATH", "HOME") if key in os.environ}
    env.update(LANG="C.UTF-8", LC_ALL="C.UTF-8", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1", GIT_OPTIONAL_LOCKS="0",
               GIT_TERMINAL_PROMPT="0")
    if token:
        env.update(GH_TOKEN=token, GH_HOST="github.com", GH_PROMPT_DISABLED="1",
                   GH_NO_UPDATE_NOTIFIER="1", GH_PAGER="cat")
    env.update(extra_env or {})
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        raise GitHubError("GitHub/Git command failed or timed out; details suppressed") from None
    if result.returncode:
        raise GitHubError("GitHub/Git command failed; check token permissions and classic branch protection (details suppressed)")
    if len(result.stdout.encode("utf-8")) > MAX_JSON_BYTES:
        raise GitHubError("GitHub/Git response exceeds the size limit")
    return result.stdout


def cleanup(run_dir):
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    if not run_dir.is_absolute() or run_dir == Path("/") or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", project):
        raise GitHubError("cleanup requires an absolute run directory and explicit Compose project")
    env = {key: os.environ[key] for key in (
        "COMPOSE_PROJECT_NAME", "VIBECI_IMAGE", "VIBECI_SANDBOX_IMAGE", "DOCKER_HOST", "DOCKER_CONTEXT",
        "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
    ) if key in os.environ}
    env["VIBECI_RUN_DIR"] = str(run_dir)
    failures = []
    try:
        command(["docker", "compose", "-f", "maintenance/compose.yaml", "down", "--volumes"], extra_env=env)
    except GitHubError:
        failures.append("Compose shutdown failed")
    query = ["docker", "ps", "-aq", "--no-trunc", "--filter", "label=vibeci.managed=true",
             "--filter", "label=vibeci.instance=path:" + str(run_dir / "data")]
    try:
        ids = command(query, extra_env=env).splitlines()
        if len(ids) > 1024 or any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in ids):
            raise GitHubError("invalid worker container IDs")
        if ids:
            command(["docker", "rm", "-f", *ids], extra_env=env)
    except GitHubError:
        failures.append("instance worker removal failed")
    # Recheck even after shutdown/removal errors; never reclaim live worker data.
    try:
        if command(query, extra_env=env).strip():
            failures.append("instance workers remain")
    except GitHubError:
        failures.append("unable to verify instance worker cleanup")
    if failures:
        raise GitHubError("; ".join(failures))
    return {"status": "cleaned"}


def checkout_head():
    head = full_sha(command(["git", "rev-parse", "--verify", "HEAD^{commit}"]).strip())
    # Include ignored files too: only the root build directory is disposable.
    if command(["git", "-c", "core.fsmonitor=false", "status", "--porcelain=v1", "-z",
                "--untracked-files=all", "--ignored=matching", "--", ".", ":(top,exclude)build/"]):
        raise GitHubError("trusted checkout is dirty (only build/ is ignored)")
    return head


def prepare_publication(data: Path, candidate: str, base: str, manifest: str,
                        version: int, base_branch: str) -> tuple[Path, str]:
    try:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            private = runpy.run_path(str(Path(__file__).resolve().with_name("vibeci-private.py")))
            directory, safe_commit = private["prepare_publication"](data, candidate, base, manifest, version, base_branch)
            if not isinstance(directory, Path) or directory.is_symlink() or not directory.is_dir():
                raise ValueError
            safe_commit = full_sha(safe_commit)
            if safe_commit in {candidate, base, manifest}:
                raise ValueError
            return directory.resolve(), safe_commit
    except (Exception, SystemExit):
        raise GitHubError("publication privacy validation failed; details suppressed") from None


class GitHub:
    def __init__(self):
        self.token = os.environ.get("GH_TOKEN", "")
        self.repository = os.environ.get("GITHUB_REPOSITORY", "")
        self.base = os.environ.get("BASE_BRANCH", "")
        if not self.token.strip():
            raise GitHubError("GH_TOKEN must contain the dedicated VIBECI_GIT_TOKEN")
        if (not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", self.repository)
                or self.repository.split("/")[-1] in {".", ".."}):
            raise GitHubError("GITHUB_REPOSITORY must be OWNER/REPO on github.com")
        if (not re.fullmatch(r"[A-Za-z0-9._/+-]{1,200}", self.base) or self.base.startswith("-")
                or self.base.endswith(".") or ".." in self.base
                or any(not part or part.startswith(".") or part.endswith(".lock") for part in self.base.split("/"))):
            raise GitHubError("BASE_BRANCH must be a valid trusted branch")
        if os.environ.get("AUTOMERGE") not in {"true", "false"}:
            raise GitHubError("AUTOMERGE must be true or false")
        self.automerge = os.environ["AUTOMERGE"] == "true"
        self.deadline = None

    def gh(self, *args):
        timeout = 60
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise GitHubError("required checks are missing or pending at the two-hour deadline")
            timeout = min(timeout, remaining)
        return command(["gh", *args], self.token, timeout=timeout)

    def api(self, endpoint, paginate=False, method="GET", **fields):
        args = ["api", "--hostname", "github.com", "--method", method]
        if paginate:
            args += ["--paginate", "--slurp"]
        for key, value in fields.items():
            args += ["--raw-field", f"{key}={value}"]
        return decode_json(self.gh(*args, f"repos/{self.repository}" + endpoint))

    def head(self, branch):
        result = self.api("/branches/" + quote(branch, safe=""))
        if not isinstance(result, dict) or not isinstance(result.get("commit"), dict):
            raise GitHubError("GitHub branch response has no commit")
        return full_sha(result["commit"].get("sha"))

    def target(self, base):
        result = self.api("")
        if (not isinstance(result, dict) or not isinstance(result.get("full_name"), str)
                or result["full_name"].lower() != self.repository.lower()):
            raise GitHubError("GitHub repository identity does not match GITHUB_REPOSITORY")
        if result.get("default_branch") != self.base:
            raise GitHubError("BASE_BRANCH is not the current default branch")
        if self.head(self.base) != base:
            raise GitHubError("default branch moved; refuse publication and leave any proposal branch for investigation")
        return result

    def push(self, directory, candidate, branch, base):
        url = f"https://github.com/{self.repository}.git"
        ref = "refs/heads/" + branch
        authorization = base64.b64encode(("x-access-token:" + self.token).encode("utf-8")).decode("ascii")
        env = {
            "HOME": os.devnull, "XDG_CONFIG_HOME": os.devnull, "CURL_HOME": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ATTR_NOSYSTEM": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_ALLOW_PROTOCOL": "https", "GIT_PROTOCOL_FROM_USER": "0",
            "GIT_ASKPASS": os.devnull, "SSH_ASKPASS": os.devnull,
            "GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "http.extraHeader", "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": f"http.{url}.extraHeader", "GIT_CONFIG_VALUE_1": "AUTHORIZATION: basic " + authorization,
        }
        git = ["git", "-C", str(directory), "-c", "core.hooksPath=" + os.devnull,
               "-c", "core.fsmonitor=false", "-c", "credential.helper=", "-c", "credential.interactive=false",
               "-c", "http.proxy=", "-c", "http.followRedirects=false", "-c", "http.sslVerify=true",
               "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
               "-c", "push.followTags=false", "-c", "push.recurseSubmodules=no", "-c", "push.gpgSign=false"]
        if command([*git, "ls-remote", "--refs", url, ref], extra_env=env):
            raise GitHubError("proposal branch already exists; refuse to update an existing remote ref")
        self.target(base)
        # No force/lease override: Git must reject a non-fast-forward creation race.
        command([*git, "push", url, candidate + ":" + ref], extra_env=env)

    def protection(self, repository):
        if repository.get("allow_merge_commit") is not True:
            raise GitHubError("merge commits must be enabled on the repository")
        protection = self.api("/branches/" + quote(self.base, safe="") + "/protection")
        if not isinstance(protection, dict):
            raise GitHubError("classic branch protection is required; rulesets alone are insufficient")
        admins = protection.get("enforce_admins")
        if not isinstance(admins, dict) or admins.get("enabled") is not True:
            raise GitHubError("classic branch protection must enforce rules for administrators")
        checks = protection.get("required_status_checks")
        if not isinstance(checks, dict) or checks.get("strict") is not True:
            raise GitHubError("classic branch protection must require strict up-to-date status checks")
        contexts, bindings = checks.get("contexts"), checks.get("checks")
        if (not isinstance(contexts, list) or any(not isinstance(context, str) for context in contexts)
                or not isinstance(bindings, list) or any(not isinstance(check, dict) for check in bindings)):
            raise GitHubError("required status checks must have unambiguous GitHub App bindings")
        for context in REQUIRED_CONTEXTS:
            matches = [check for check in bindings if check.get("context") == context]
            if (contexts.count(context) != 1 or len(matches) != 1
                    or type(matches[0].get("app_id")) is not int or matches[0]["app_id"] != ACTIONS_APP_ID):
                raise GitHubError("both exact MaintainedMost checks must be required once, from GitHub Actions app 15368")
        return set(contexts) | {check["context"] for check in bindings if isinstance(check.get("context"), str)}

    def proposals(self):
        pages = self.api("/pulls?state=open&per_page=100", paginate=True)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise GitHubError("invalid paginated pull request response")
        proposals = []
        for page in pages:
            for pull in page:
                if not isinstance(pull, dict) or pull.get("state") != "open":
                    raise GitHubError("invalid open pull request response")
                head, base = pull.get("head"), pull.get("base")
                if not all(isinstance(side, dict) and isinstance(side.get("repo"), dict)
                           and isinstance(side["repo"].get("full_name"), str)
                           and side["repo"]["full_name"].lower() == self.repository.lower() for side in (head, base)):
                    continue
                if base.get("ref") != self.base or not isinstance(head.get("ref"), str):
                    continue
                if head["ref"].startswith("vibeci/update-"):
                    self.pull_url(pull)
                    proposals.append(pull)
        return proposals

    def pull_url(self, pull):
        number = pull.get("number")
        if type(number) is not int or number <= 0:
            raise GitHubError("invalid pull request number")
        return f"https://github.com/{self.repository}/pull/{number}"

    def validate_pull(self, pull, summary, candidate):
        if not isinstance(pull, dict) or pull.get("state") != "open" or pull.get("merged") is True:
            raise GitHubError("proposal is no longer an open pull request")
        for side, ref, sha in (("head", summary["branch"], candidate), ("base", self.base, summary["base"])):
            value = pull.get(side)
            if (not isinstance(value, dict) or not isinstance(value.get("repo"), dict)
                    or not isinstance(value["repo"].get("full_name"), str)
                    or value["repo"]["full_name"].lower() != self.repository.lower()
                    or value.get("ref") != ref or value.get("sha") != sha):
                raise GitHubError("pull request must bind the selected same-repository branch, candidate and base")
        return self.pull_url(pull)


    def checked_proposal(self, summary, candidate, number, url):
        required = self.protection(self.target(summary["base"]))
        pull = self.api("/pulls/" + number)
        if self.validate_pull(pull, summary, candidate) != url or pull.get("draft") is not False:
            raise GitHubError("proposal changed or is now a draft; refuse controller merge")
        if self.head(summary["branch"]) != candidate:
            raise GitHubError("proposal branch moved before controller merge")
        return required

    def wait_and_merge(self, summary, candidate, number, url):
        self.deadline = time.monotonic() + CHECK_TIMEOUT
        try:
            while True:
                required = self.checked_proposal(summary, candidate, number, url)
                view = decode_json(self.gh("pr", "view", url, "--repo", self.repository, "--json",
                                           "headRefOid,baseRefOid,state,isDraft,statusCheckRollup"))
                if (not isinstance(view, dict) or view.get("headRefOid") != candidate
                        or view.get("baseRefOid") != summary["base"] or view.get("state") != "OPEN"
                        or view.get("isDraft") is not False):
                    raise GitHubError("PR check snapshot no longer matches the bound head and base")
                if "statusCheckRollup" not in view:
                    raise GitHubError("PR check snapshot has no rollup")
                nodes = view["statusCheckRollup"]
                if nodes is None:
                    nodes = []  # GitHub may not have registered the new PR's checks yet.
                if not isinstance(nodes, list):
                    raise GitHubError("invalid PR check rollup")
                seen, successful = set(), set()
                pending = False
                for node in nodes:
                    if not isinstance(node, dict):
                        raise GitHubError("invalid PR check node")
                    kind = node.get("__typename")
                    name = node.get("name") if kind == "CheckRun" else node.get("context")
                    if not isinstance(name, str) or not name or name in seen:
                        raise GitHubError("missing or ambiguous PR check name")
                    seen.add(name)
                    if kind == "CheckRun":
                        status, conclusion = node.get("status"), node.get("conclusion")
                        if status == "COMPLETED" and conclusion == "SUCCESS":
                            successful.add(name)
                        elif (isinstance(status, str) and status in {"QUEUED", "IN_PROGRESS", "WAITING", "PENDING", "REQUESTED"}
                              and conclusion in (None, "")):
                            pending = True
                        else:
                            raise GitHubError("PR check failed, cancelled, skipped or has an invalid state")
                    elif kind == "StatusContext" and name not in REQUIRED_CONTEXTS:
                        if node.get("state") == "SUCCESS":
                            successful.add(name)
                        elif node.get("state") in ("PENDING", "EXPECTED"):
                            pending = True
                        else:
                            raise GitHubError("PR commit status is not successful")
                    else:
                        raise GitHubError("required Actions checks must be CheckRun nodes")
                if self.checked_proposal(summary, candidate, number, url) != required:
                    raise GitHubError("required check configuration changed before waiting or merging")
                if not pending and required <= successful:
                    # The REST SHA precondition is atomic; no deferred auto-merge
                    # queue can later merge a different head. Rules still apply.
                    merged = self.api("/pulls/" + number + "/merge", method="PUT", sha=candidate, merge_method="merge")
                    if not isinstance(merged, dict) or merged.get("merged") is not True:
                        raise GitHubError("GitHub did not confirm the protected merge")
                    commit = full_sha(merged.get("sha"))
                    if commit in {candidate, summary["base"]}:
                        raise GitHubError("GitHub did not return a new merge commit")
                    return commit
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise GitHubError("required checks are missing or pending at the two-hour deadline")
                time.sleep(min(CHECK_INTERVAL, remaining))
        finally:
            self.deadline = None


def preflight():
    github = GitHub()
    repository = github.target(checkout_head())
    proposals = github.proposals()
    if proposals:
        return {"skip": "true", "pull_request": github.pull_url(proposals[0]), "open_proposals": str(len(proposals))}
    if github.automerge:
        github.protection(repository)
    return {"skip": "false", "pull_request": "", "open_proposals": "0"}


def publish(data, result):
    # Only the restored record is screened by prepare_publication; the raw copy is worker-controlled.
    summary = decode_json(read_evidence(data / "public-summary.json"))
    if (not isinstance(summary, dict) or summary.get("changed") is not True
            or type(summary.get("push")) is not bool or type(summary.get("version")) is not int
            or not 0 < summary["version"] < (1 << 63)):
        raise GitHubError("publication requires a changed plan with a boolean push flag and positive version")
    base, manifest = full_sha(summary.get("base")), full_sha(summary.get("manifest"))
    prefix = f"vibeci/update-{summary['version']}-{base[:12]}-{manifest[:12]}-"
    branch = summary.get("branch")
    if not isinstance(branch, str) or not re.fullmatch(re.escape(prefix) + r"[0-9a-f]{12}", branch):
        raise GitHubError("proposal branch is not the branch selected by the planner")
    outcomes = decode_json(read_evidence(result))
    if (not isinstance(outcomes, list) or len(outcomes) != 1 or not isinstance(outcomes[0], dict)
            or outcomes[0].get("repo") != "maintainedmost"):
        raise GitHubError("result must contain exactly one maintainedmost repository outcome")
    outcome = outcomes[0]
    if outcome.get("status") != "dry-run" or outcome.get("error"):
        raise GitHubError("result is not a successful dry-run outcome; non-producing statuses are not success")
    candidate = full_sha(outcome.get("commit"))
    if (candidate == base or outcome.get("target") != manifest or outcome.get("fork_head") != base
            or outcome.get("version") != str(summary["version"])):
        raise GitHubError("result does not bind the planned candidate, manifest, fork head and version")
    if summary["push"]:
        event = os.environ.get("GITHUB_EVENT_NAME")
        if (os.environ.get("VIBECI_ENABLED") != "true" or os.environ.get("VIBECI_PUSH") != "true"
                or not (event == "schedule" or (event == "workflow_dispatch" and os.environ.get("PREVIEW") == "false"))):
            raise GitHubError("publication gates are closed; leave any proposal branch for investigation")
    github = GitHub()
    if checkout_head() != base:
        raise GitHubError("trusted checkout HEAD no longer matches the planned base")
    repository = github.target(base)
    publisher, candidate = prepare_publication(data, candidate, base, manifest, summary["version"], github.base)
    if not summary["push"]:
        return {"status": "validated-dry-run", "published": "false", "pull_request": ""}
    if github.automerge:
        if os.environ.get("VIBECI_AUTOMERGE") != "true":
            raise GitHubError("VIBECI_AUTOMERGE must explicitly permit auto-merge")
        github.protection(repository)
    github.push(publisher, candidate, branch, base)
    if github.head(branch) != candidate:
        raise GitHubError("proposal branch moved or does not match the candidate")
    proposals = github.proposals()
    if proposals:
        if len(proposals) != 1:
            raise GitHubError("another maintenance proposal is open; refuse duplicate publication")
        url = github.validate_pull(proposals[0], summary, candidate)
        # Never start merging a proposal that another caller already opened.
        return {"status": "existing-proposal", "published": "false", "pull_request": url}
    body = data / "public-summary.md"
    read_evidence(body)
    github.target(base)
    if github.head(branch) != candidate:
        raise GitHubError("proposal branch moved before PR creation")
    url = github.gh("pr", "create", "--repo", github.repository, "--base", github.base, "--head", branch,
                    "--title", f"Update upstream maintenance to {summary['version']}", "--body-file", str(body)).strip()
    match = re.fullmatch(re.escape(f"https://github.com/{github.repository}/pull/") + r"([1-9][0-9]*)", url)
    if not match:
        raise GitHubError("GitHub did not return the expected same-repository pull request URL")
    pull = github.api("/pulls/" + match[1])
    if github.validate_pull(pull, summary, candidate) != url:
        raise GitHubError("created pull request identity changed")
    if github.automerge:
        commit = github.wait_and_merge(summary, candidate, match[1], url)
        return {"status": "merged", "published": "true", "pull_request": url, "merge_commit": commit}
    return {"status": "proposal-created", "published": "true", "pull_request": url}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("preflight")
    finalize = subparsers.add_parser("publish")
    finalize.add_argument("--data", type=Path, required=True)
    finalize.add_argument("--result", type=Path, required=True)
    teardown = subparsers.add_parser("cleanup")
    teardown.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "preflight":
            outputs = preflight()
        elif args.command == "publish":
            outputs = publish(args.data, args.result)
        else:
            outputs = cleanup(args.run_dir)
        print(json.dumps(outputs, sort_keys=True))
        if os.environ.get("GITHUB_OUTPUT"):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
                output.writelines(f"{key}={value}\n" for key, value in outputs.items())
        message = ("Existing maintenance proposal; no planning or merge attempted." if outputs.get("skip") == "true"
                   else outputs.get("status", "Preflight passed; no existing maintenance proposal."))
        message += " " + outputs.get("pull_request", "")
    except (GitHubError, OSError, UnicodeError) as error:
        message = str(error) if isinstance(error, GitHubError) else "unable to read or write required GitHub evidence"
        print("vibeci-github: " + message, file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
                output.write("\nVibeCI GitHub refused: " + message + "\n")
        return 1
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as output:
            output.write("\nVibeCI GitHub: " + message.strip() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
