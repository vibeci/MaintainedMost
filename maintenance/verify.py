#!/usr/bin/env python3
"""Read-only candidate guard, installed in the trusted offline sandbox image."""

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys


PIN_FILES = {"maintenance/upstream-version", "upstream.env", "Containerfile"}
COMPONENTS = ("server", "calls", "transcriber")


class Rejected(ValueError):
    pass


def decode_plan(encoded):
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise Rejected("duplicate plan key")
            result[key] = value
        return result

    if len(encoded) > 262144:
        raise Rejected("oversized plan")
    try:
        plan = json.loads(base64.b64decode(encoded, altchars=b"-_", validate=True), object_pairs_hook=unique)
    except (ValueError, UnicodeError, binascii.Error, RecursionError) as error:
        raise Rejected("invalid encoded plan") from error
    if not isinstance(plan, dict) or plan.keys() != {"base", "version", "manifest", "files", "patches"}:
        raise Rejected("unexpected plan fields")
    for key in ("base", "manifest"):
        if not isinstance(plan[key], str) or not re.fullmatch(r"[0-9a-f]{40}", plan[key]) or plan[key] == "0" * 40:
            raise Rejected("invalid planned commit")
    if type(plan["version"]) is not int or not 0 < plan["version"] < (1 << 63):
        raise Rejected("invalid maintenance version")
    if not isinstance(plan["files"], dict) or plan["files"].keys() != PIN_FILES:
        raise Rejected("unexpected planned metadata files")
    if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in plan["files"].values()):
        raise Rejected("invalid planned file digest")
    patches = plan["patches"]
    if (not isinstance(patches, list) or not patches or any(not isinstance(path, str) or
            not re.fullmatch(r"patches/(server|calls|transcriber)/[A-Za-z0-9][A-Za-z0-9_.-]*\.patch", path)
            for path in patches) or len(set(patches)) != len(patches)):
        raise Rejected("invalid patch inventory")
    if any(not any(path.startswith(f"patches/{component}/") for path in patches) for component in COMPONENTS):
        raise Rejected("missing component patch inventory")
    return plan


def git(root, *args):
    # No repository hooks, external diff, filters, lazy fetch or inherited Git
    # configuration. The fork object store is mounted read-only by the broker.
    env = {"PATH": os.defpath, "HOME": "/tmp", "LANG": "C", "LC_ALL": "C",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_OPTIONAL_LOCKS": "0",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ALLOW_PROTOCOL": ""}
    result = subprocess.run(["git", "--literal-pathspecs", "-c", "core.fsmonitor=false",
                             "-c", "core.hooksPath=/dev/null", "-c", "core.attributesFile=/dev/null",
                             "-c", "diff.external=", *args], cwd=root, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if result.returncode or len(result.stdout) > 16 << 20:
        raise Rejected("unable to inspect candidate Git objects")
    return result.stdout


def tree(root, revision):
    result = {}
    for entry in git(root, "ls-tree", "-rz", revision).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            result[path.decode("utf-8")] = tuple(metadata.decode("ascii").split())
    return result


def verify(plan, root=Path("fork")):
    if root.is_symlink() or not root.is_dir():
        raise Rejected("fork must be a real checkout directory")
    root = root.resolve()
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
    if len(parents) != 2 or parents[1] != plan["base"]:
        raise Rejected("candidate must have exactly the planned base parent")
    message = git(root, "show", "-s", "--format=%B", "HEAD").decode()
    upstream = f"Upstream: /data/input/upstream.git @ v{plan['version']} ({plan['manifest']})"
    if message.splitlines().count(upstream) != 1 or message.splitlines().count(f"VibeCI-Upstream: {plan['version']}") != 1:
        raise Rejected("candidate does not bind the planned manifest and version")
    old, new = tree(root, plan["base"]), tree(root, "HEAD")
    for snapshot in (old, new):
        actual = [path for component in COMPONENTS for path in sorted(snapshot)
                  if path.startswith(f"patches/{component}/") and path.endswith(".patch")]
        if actual != plan["patches"]:
            raise Rejected("patch inventory changed; automatic additions, removals and renames are forbidden")
    permitted = PIN_FILES | set(plan["patches"])
    for path in old.keys() | new.keys():
        if old.get(path) != new.get(path) and path not in permitted:
            raise Rejected("candidate changed a protected fork file: " + path)
    for path in permitted:
        if path not in new or new[path][0] not in {"100644", "100755"} or new[path][1] != "blob":
            raise Rejected("planned files must remain regular blobs")
        if path in plan["patches"] and new[path][:2] != old[path][:2]:
            raise Rejected("patch file mode changed")
    for path, digest in plan["files"].items():
        if new[path][0] != "100644" or hashlib.sha256(git(root, "cat-file", "blob", new[path][2])).hexdigest() != digest:
            raise Rejected("committed metadata does not match the plan: " + path)
    previous = git(root, "cat-file", "blob", old["maintenance/upstream-version"][2])
    current = git(root, "cat-file", "blob", new["maintenance/upstream-version"][2])
    if (not re.fullmatch(rb"(?:0|[1-9][0-9]{0,18})\n", previous)
            or int(previous) + 1 != plan["version"] or current != f"{plan['version']}\n".encode()):
        raise Rejected("maintenance version must advance by exactly one")
    if git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=matching"):
        raise Rejected("verification checkout diverges from the candidate commit")
    print("Candidate matches the immutable plan and protected fork boundaries; full CI is still required.")


def main():
    try:
        if len(sys.argv) != 2:
            raise Rejected("expected one encoded plan argument")
        verify(decode_plan(sys.argv[1]))
    except (Rejected, OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print("maintenance verifier: " + (str(error) if isinstance(error, Rejected) else "candidate inspection failed"), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
