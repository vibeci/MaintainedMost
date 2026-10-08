#!/usr/bin/env python3
"""Trusted, offline publication boundary; never push or check out candidate code.

prepare_publication(data, candidate, base, manifest, version, base_branch) loads
the private environment at call time and returns (fresh bare publisher, safe SHA).
Workers must already be stopped, and the caller must exclusively control a real,
non-symlink run directory. Only BASE history and the candidate TREE enter the publisher, never
the raw candidate commit, refs, configuration, alternates, or engine reports.

privacy_policy().check(bytes), scan_environment_bytes(bytes), and `check FILE...`
also gate public artifacts. Required environment: VIBECI_PRIVATE_TERMS (nonempty
JSON string array, distinctive terms of at least four characters), VIBECI_MODELS
(providers/models/roles JSON with file:/run/secrets/llm_api_key references),
VIBECI_LLM_API_KEY, and GH_TOKEN. The gate never reads that secret file. Optional
read and builtin GitHub tokens are protected too. Aliases/role names are not private terms.

This is a bounded denylist, NOT a proof against arbitrary encoding: it recognizes
literals and common URL, JSON, base64, base64url and hex forms, including two
decoding layers. Encryption, compression, arbitrary splitting, custom encodings,
and undisclosed secrets require other controls. All object bytes, including
binary blobs and historical commit metadata, are checked. Limits are 16 MiB per
object/file, 10,000 objects/results and 256 MiB total publication object bytes.
All failures are generic; neither findings nor subprocess output are logged.
"""

import base64
import binascii
from collections import deque
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import selectors
import stat
import subprocess
import sys
import time
from urllib.parse import unquote_to_bytes, urlsplit


MAX_OBJECT_BYTES = 16 << 20
MAX_OBJECTS = 10000
MAX_TOTAL_BYTES = 256 << 20
MAX_SCAN_RESULTS = 10000
MAX_SCAN_BYTES = 64 << 20
MAX_ENV_BYTES = 65536
MAX_PLAN_BYTES = 196608
GIT_TIMEOUT = 30
FAILURE = "Publication privacy check failed."
IDENTITY = "Maintenance Publisher"
EMAIL = "publisher@example.invalid"
COMPONENTS = ("server", "calls", "transcriber")
PIN_FILES = {"maintenance/upstream-version", "upstream.env", "Containerfile"}
SHA = re.compile(r"[0-9a-f]{40}")
JSON_ESCAPE = re.compile(rb'(?:\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))+')
HEX_ESCAPE = re.compile(rb"(?:\\x[0-9a-fA-F]{2})+")
ENCODED = (re.compile(rb"[A-Za-z0-9+/_-]{8,}={0,2}"), re.compile(rb"[0-9a-fA-F]{8,}"))


class PrivacyError(ValueError):
    """A deliberately content-free error, safe for the controller to report."""

    def __init__(self):
        super().__init__(FAILURE)


def _json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise PrivacyError()
            result[key] = item
        return result

    def invalid(_):
        raise PrivacyError()

    # Reject non-finite floats, including exponent overflow, without retaining a
    # JSONDecodeError that could expose a private document in diagnostics.
    def number(text):
        value = float(text)
        if not float("-inf") < value < float("inf"):
            raise PrivacyError()
        return value

    return json.loads(value.decode("utf-8") if isinstance(value, bytes) else value,
                      object_pairs_hook=pairs, parse_constant=invalid, parse_float=number)


class _Policy:
    def __init__(self, values):
        self._literal = tuple({value.casefold() for value in values})
        encoded = set()
        for value in values:
            for variant in {value, value.lower(), value.upper(), value.casefold()}:
                raw = variant.encode("utf-8")
                encoded.update((base64.b64encode(raw).rstrip(b"=").lower(),
                                base64.urlsafe_b64encode(raw).rstrip(b"=").lower(),
                                raw.hex().encode("ascii")))
        self._encoded = tuple(encoded)

    def check(self, payload: bytes) -> None:
        """Reject sensitive byte content or exhausted bounds, without findings."""
        try:
            if not isinstance(payload, bytes) or len(payload) > MAX_OBJECT_BYTES:
                raise PrivacyError()
            queue = deque([(payload, 0)])
            seen = {hashlib.sha256(payload).digest()}
            total, results = len(payload), 0

            def add(value, depth):
                nonlocal total
                digest = hashlib.sha256(value).digest()
                if digest not in seen:
                    total += len(value)
                    if total > MAX_SCAN_BYTES or len(seen) >= MAX_SCAN_RESULTS:
                        raise PrivacyError()
                    seen.add(digest)
                    queue.append((value, depth))

            def unescape(match):
                try:
                    return json.loads(b'"' + match[0] + b'"').encode("utf-8")
                except (ValueError, UnicodeError):
                    return match[0]

            while queue:
                part, depth = queue.popleft()
                folded = part.decode("utf-8", errors="ignore").casefold()
                lower = part.lower()
                if (any(value in folded for value in self._literal)
                        or any(value in lower for value in self._encoded)):
                    raise PrivacyError()
                if depth == 2:
                    continue
                if b"%" in part or b"+" in part:
                    add(unquote_to_bytes(part), depth + 1)
                    add(unquote_to_bytes(part.replace(b"+", b" ")), depth + 1)
                if b"\\" in part:
                    add(JSON_ESCAPE.sub(unescape, part), depth + 1)
                    add(HEX_ESCAPE.sub(lambda match: bytes.fromhex(match[0].replace(b"\\x", b"").decode("ascii")), part), depth + 1)
                for index, pattern in enumerate(ENCODED):
                    for match in pattern.finditer(part):
                        results += 1
                        if results > MAX_SCAN_RESULTS:
                            raise PrivacyError()
                        try:
                            raw = match[0]
                            decoded = (bytes.fromhex(raw.decode("ascii")) if index else
                                       base64.b64decode(raw + b"=" * (-len(raw) % 4), altchars=b"-_", validate=True))
                        except (ValueError, binascii.Error):
                            continue
                        add(decoded, depth + 1)
        except Exception:
            raise PrivacyError() from None


def privacy_policy():
    """Load only environment values, never a credential/configuration file."""
    try:
        def required(name):
            value = os.environ.get(name, "")
            if not value.strip() or len(value.encode("utf-8")) > MAX_ENV_BYTES:
                raise PrivacyError()
            return value

        terms = _json(required("VIBECI_PRIVATE_TERMS"))
        if (not isinstance(terms, list) or not 0 < len(terms) <= 128
                or any(not isinstance(value, str) or len(value.strip()) < 4 or len(value) > 4096 for value in terms)
                or len({value.strip().casefold() for value in terms}) != len(terms)):
            raise PrivacyError()
        models_text = required("VIBECI_MODELS")
        models = _json(models_text)
        if (not isinstance(models, dict) or models.keys() != {"providers", "models", "roles"}
                or any(not isinstance(value, dict) or not value for value in models.values())):
            raise PrivacyError()
        values = [value.strip() for value in terms] + [models_text]
        provider_keys = {"type", "base_url", "auth", "api_key", "region", "timeout", "max_retries", "stream", "max_tokens_field"}
        model_keys = {"provider", "model", "max_tokens", "context_window", "thinking", "thinking_budget", "effort", "temperature", "prompt_cache"}
        for provider in models["providers"].values():
            if (not isinstance(provider, dict) or provider.keys() - provider_keys
                    or provider.get("api_key") != "file:/run/secrets/llm_api_key"):
                raise PrivacyError()
            if "base_url" in provider:
                value = provider["base_url"]
                if not isinstance(value, str) or not value or len(value) > 4096:
                    raise PrivacyError()
                url = urlsplit(value)
                if (url.scheme != "https" or not url.hostname or url.username or url.password
                        or url.query or url.fragment or url.port == 0):
                    raise PrivacyError()
                values.extend((value, url.hostname, url.hostname.encode("idna").decode("ascii")))
        for model in models["models"].values():
            if (not isinstance(model, dict) or model.keys() - model_keys or not isinstance(model.get("model"), str)
                    or not model["model"].strip() or len(model["model"]) > 4096
                    or not isinstance(model.get("provider"), str) or model["provider"] not in models["providers"]):
                raise PrivacyError()
            values.extend((model["model"], model["model"].strip()))
        for name in ("VIBECI_LLM_API_KEY", "GH_TOKEN", "VIBECI_FORK_READ_TOKEN", "GITHUB_TOKEN", "VIBECI_GIT_TOKEN"):
            if name in {"VIBECI_LLM_API_KEY", "GH_TOKEN"} or os.environ.get(name):
                token = required(name)
                if len(token.strip()) < 4:
                    raise PrivacyError()
                values.extend((token, token.strip()))
        if len(values) > 256:
            raise PrivacyError()
        return _Policy(values)
    except Exception:
        raise PrivacyError() from None


def scan_environment_bytes(payload: bytes) -> None:
    """Convenience artifact gate using a newly loaded environment policy."""
    privacy_policy().check(payload)


def _directory(path):
    if not path.is_dir() or any(part.is_symlink() for part in (path, *path.parents)):
        raise PrivacyError()


def _read_file(path, limit=MAX_OBJECT_BYTES):
    _directory(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise PrivacyError()
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if (len(data) > limit or len(data) != before.st_size
            or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
               (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
        raise PrivacyError()
    return data


def _git(repository, *args, data=None, limit=MAX_OBJECT_BYTES + 128, timestamp=946684800):
    # No inherited environment, credential sources, tracing, protocols, hooks,
    # filters, replacements or implicit fetches. Read raw objects, never checkout.
    env = {"PATH": os.defpath, "HOME": os.devnull, "XDG_CONFIG_HOME": os.devnull,
           "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_ATTR_NOSYSTEM": "1", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
           "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
           "GIT_ALLOW_PROTOCOL": "", "GIT_AUTHOR_NAME": IDENTITY, "GIT_COMMITTER_NAME": IDENTITY,
           "GIT_AUTHOR_EMAIL": EMAIL, "GIT_COMMITTER_EMAIL": EMAIL,
           "GIT_AUTHOR_DATE": f"@{timestamp} +0000", "GIT_COMMITTER_DATE": f"@{timestamp} +0000"}
    settings = ("core.hooksPath=/dev/null", "core.fsmonitor=false", "core.attributesFile=/dev/null",
                "core.sshCommand=false", "credential.helper=", "credential.interactive=false",
                "protocol.allow=never", "diff.external=", "commit.gpgSign=false", "tag.gpgSign=false",
                "core.commitGraph=false", "gc.auto=0", "maintenance.auto=false")
    command = ["git", "--literal-pathspecs", "--no-replace-objects"]
    command += [part for setting in settings for part in ("-c", setting)]
    command += ["--git-dir=" + str(repository), *args]
    with subprocess.Popen(command, cwd=repository.parent, env=env,
                          stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
        try:
            output, offset = bytearray(), 0
            deadline = time.monotonic() + GIT_TIMEOUT
            with selectors.DefaultSelector() as selector:
                os.set_blocking(process.stdout.fileno(), False)
                selector.register(process.stdout, selectors.EVENT_READ)
                if data:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE)
                elif process.stdin:
                    process.stdin.close()
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PrivacyError()
                    events = selector.select(remaining)
                    if not events:
                        raise PrivacyError()
                    for key, event in events:
                        if event & selectors.EVENT_READ:
                            chunk = os.read(key.fd, min(65536, limit + 1 - len(output)))
                            if chunk:
                                output.extend(chunk)
                                if len(output) > limit:
                                    raise PrivacyError()
                            else:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            offset += os.write(key.fd, memoryview(data)[offset:offset + 65536])
                            if offset == len(data):
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
            if process.wait(timeout=max(0.001, deadline - time.monotonic())):
                raise PrivacyError()
            return bytes(output)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()


def _sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value) or value == "0" * 40:
        raise PrivacyError()
    return value


def _object(repository, oid, expected):
    raw = _git(repository, "cat-file", "--batch", data=(oid + "\n").encode("ascii"))
    header, separator, body = raw.partition(b"\n")
    fields = header.split(b" ")
    if (not separator or len(fields) != 3 or fields[:2] != [oid.encode(), expected.encode()]
            or not re.fullmatch(rb"0|[1-9][0-9]{0,8}", fields[2])):
        raise PrivacyError()
    size = int(fields[2])
    if size > MAX_OBJECT_BYTES or len(body) != size + 1 or not body.endswith(b"\n"):
        raise PrivacyError()
    payload = body[:-1]
    if hashlib.sha1(expected.encode() + b" " + str(size).encode() + b"\0" + payload).hexdigest() != oid:
        raise PrivacyError()
    return payload


def _commit(payload):
    header, separator, message = payload.partition(b"\n\n")
    if not separator or b"\0" in payload:
        raise PrivacyError()
    fields, previous = {}, None
    for line in header.split(b"\n"):
        if line.startswith(b" ") and previous in {b"gpgsig", b"gpgsig-sha256", b"mergetag"}:
            continue
        key, space, value = line.partition(b" ")
        if (not space or not value or key not in {b"tree", b"parent", b"author", b"committer", b"encoding",
                                                b"gpgsig", b"gpgsig-sha256", b"mergetag"}):
            raise PrivacyError()
        fields.setdefault(key, []).append(value)
        if len(fields[key]) > MAX_OBJECTS:
            raise PrivacyError()
        previous = key
    if (not header.startswith(b"tree ") or any(len(values) != 1 for key, values in fields.items() if key != b"parent")
            or not {b"tree", b"author", b"committer"} <= fields.keys()):
        raise PrivacyError()
    required = [b"tree " + fields[b"tree"][0]]
    required += [b"parent " + value for value in fields.get(b"parent", [])]
    required += [key + b" " + fields[key][0] for key in (b"author", b"committer")]
    if header.split(b"\n")[:len(required)] != required:
        raise PrivacyError()
    tree = _sha(fields[b"tree"][0].decode("ascii"))
    parents = [_sha(value.decode("ascii")) for value in fields.get(b"parent", [])]
    if len(set(parents)) != len(parents):
        raise PrivacyError()
    for key in (b"author", b"committer"):
        match = re.fullmatch(rb"[^<>\n]+ <[^<>\n]*> ([0-9]{1,12}) ([+-])([0-9]{2})([0-9]{2})", fields[key][0])
        if not match or int(match[1]) >= 253402300799 or int(match[3]) > 23 or int(match[4]) > 59:
            raise PrivacyError()
    return tree, parents, int(match[1]), message


def _tree(payload):
    entries, names, offset, previous = [], set(), 0, None
    while offset < len(payload):
        space = payload.find(b" ", offset)
        nul = payload.find(b"\0", space + 1)
        if space < offset or nul <= space + 1 or nul + 21 > len(payload):
            raise PrivacyError()
        mode, name = payload[offset:space], payload[space + 1:nul]
        if (mode not in {b"40000", b"100644", b"100755", b"120000"}
                or b"/" in name or name.lower() in {b".", b"..", b".git"} or name in names):
            raise PrivacyError()
        kind = "tree" if mode == b"40000" else "blob"
        order = name + (b"/" if kind == "tree" else b"")
        if previous is not None and order <= previous:
            raise PrivacyError()
        oid = _sha(payload[nul + 1:nul + 21].hex())
        entries.append((name.decode("utf-8"), mode.decode("ascii"), kind, oid))
        if len(entries) > MAX_OBJECTS:
            raise PrivacyError()
        names.add(name)
        previous, offset = order, nul + 21
    return entries


def _collect(repository, roots, policy, reserve=0):
    objects, trees, commit_trees, wanted = {}, {}, set(), {}
    pending, total, links = list(roots), 0, 0
    while pending:
        oid, kind = pending.pop()
        if oid in wanted:
            if wanted[oid] != kind:
                raise PrivacyError()
            continue
        wanted[oid] = kind
        if kind not in {"commit", "tree", "blob"} or len(wanted) + reserve > MAX_OBJECTS:
            raise PrivacyError()
        payload = _object(repository, oid, kind)
        total += len(payload)
        if total > MAX_TOTAL_BYTES:
            raise PrivacyError()
        policy.check(payload)
        objects[oid] = (kind, payload)
        if kind == "commit":
            tree, parents, _, _ = _commit(payload)
            commit_trees.add(tree)
            pending.extend([(tree, "tree"), *((parent, "commit") for parent in parents)])
            links += len(parents) + 1
        elif kind == "tree":
            entries = trees[oid] = _tree(payload)
            pending.extend((entry[3], entry[2]) for entry in entries)
            links += len(entries)
        if links > MAX_OBJECTS * 10:
            raise PrivacyError()
    return objects, trees, commit_trees


def _snapshot(root, trees, policy, remaining):
    snapshot, pending, count = {}, [("", root)], 0
    while pending:
        prefix, oid = pending.pop()
        for name, mode, kind, child in trees[oid]:
            path = prefix + name
            count += 1
            if count > remaining or len(path.encode("utf-8")) > 4096:
                raise PrivacyError()
            policy.check(path.encode("utf-8"))
            if kind == "tree" and trees[child]:
                pending.append((path + "/", child))
            else:
                snapshot[path] = (mode, kind, child)
    return snapshot, count


def prepare_publication(data: Path, candidate: str, base: str, manifest: str,
                        version: int, base_branch: str) -> tuple[Path, str]:
    """Validate immutable inputs and return the only repository/SHA safe to push.

    Public evidence must be named plan.json, public-summary.md and
    public-summary.json in data. Existing publication.git is never reused or
    deleted, even after an earlier failure. Cleanup belongs to the controller.
    """
    try:
        policy = privacy_policy()
        data = Path(data).absolute()
        _directory(data)
        publisher = data.parent / "publication.git"
        if publisher.exists() or publisher.is_symlink():
            raise PrivacyError()
        _sha(candidate), _sha(base), _sha(manifest)
        if candidate == base or type(version) is not int or not 0 < version < (1 << 63):
            raise PrivacyError()
        if (not isinstance(base_branch, str) or not re.fullmatch(r"[A-Za-z0-9._/+-]{1,200}", base_branch)
                or base_branch.startswith("-") or base_branch.endswith(".") or ".." in base_branch
                or any(not part or part.startswith(".") or part.endswith(".lock") for part in base_branch.split("/"))):
            raise PrivacyError()
        policy.check(base_branch.encode())
        plan_bytes = _read_file(data / "plan.json", MAX_PLAN_BYTES)
        policy.check(plan_bytes)
        _json(plan_bytes)
        verifier = runpy.run_path(str(Path(__file__).resolve().parents[1] / "maintenance/verify.py"))
        plan = verifier["decode_plan"](base64.urlsafe_b64encode(plan_bytes).decode("ascii"))
        if (plan["base"], plan["manifest"], plan["version"]) != (base, manifest, version):
            raise PrivacyError()
        for name in ("public-summary.md", "public-summary.json"):
            payload = _read_file(data / name)
            policy.check(payload)
            if name.endswith(".json") and not isinstance(_json(payload), dict):
                raise PrivacyError()

        mirror = data / "mirrors" / "maintainedmost.git"
        _directory(mirror)
        # Refuse linked stores and filesystem indirection before asking Git to
        # inspect anything. The engine mirror is otherwise private and untrusted.
        for name in ("commondir", "config.worktree", "objects/info/alternates", "objects/info/http-alternates"):
            path = mirror / name
            if path.exists() or path.is_symlink():
                raise PrivacyError()
        count = 0
        for directory, dirs, files in os.walk(mirror, followlinks=False):
            for name in dirs + files:
                info = (Path(directory) / name).lstat()
                count += 1
                if (count > MAX_OBJECTS * 4 or not (stat.S_ISDIR(info.st_mode) or
                        (stat.S_ISREG(info.st_mode) and info.st_nlink == 1))):
                    raise PrivacyError()
        # Git's local includes can read files outside the mirror even with global
        # and system configuration disabled. Reject them before invoking Git.
        config = re.sub(rb"\\\r?\n", b"", _read_file(mirror / "config", MAX_ENV_BYTES).removeprefix(b"\xef\xbb\xbf"))
        if re.search(rb'(?im)^\s*\[\s*include(?:if)?(?=[\s.\]"])', config):
            raise PrivacyError()
        if _git(mirror, "rev-parse", "--is-bare-repository", limit=64) != b"true\n":
            raise PrivacyError()
        proposed = "refs/vibeci/proposed/" + base_branch
        if _git(mirror, "show-ref", "--verify", "--hash", proposed, limit=128) != (candidate + "\n").encode():
            raise PrivacyError()
        raw = _object(mirror, candidate, "commit")
        tree, parents, timestamp, message = _commit(raw)
        upstream = f"Upstream: /data/input/upstream.git @ v{version} ({manifest})"
        lines = message.decode("utf-8").splitlines()
        if parents != [base] or lines.count(upstream) != 1 or lines.count(f"VibeCI-Upstream: {version}") != 1:
            raise PrivacyError()

        objects, trees, historical = _collect(mirror, [(base, "commit"), (tree, "tree")], policy, reserve=1)
        if candidate in objects:
            raise PrivacyError()
        old_tree = _commit(objects[base][1])[0]
        snapshots, remaining = {}, MAX_OBJECTS
        for historical_tree in historical | {tree}:
            snapshot, used = _snapshot(historical_tree, trees, policy, remaining)
            remaining -= used
            if historical_tree in {old_tree, tree}:
                snapshots[historical_tree] = snapshot
        old, new = snapshots[old_tree], snapshots[tree]
        for snapshot in (old, new):
            actual = [path for component in COMPONENTS for path in sorted(snapshot)
                      if path.startswith(f"patches/{component}/") and path.endswith(".patch")]
            if actual != plan["patches"]:
                raise PrivacyError()
        permitted = PIN_FILES | set(plan["patches"])
        for path in old.keys() | new.keys():
            if old.get(path) != new.get(path) and path not in permitted:
                raise PrivacyError()
        for path in permitted:
            if path not in new or new[path][0] not in {"100644", "100755"} or new[path][1] != "blob":
                raise PrivacyError()
            if path in plan["patches"] and new[path][:2] != old[path][:2]:
                raise PrivacyError()
        for path, digest in plan["files"].items():
            if new[path][0] != "100644" or hashlib.sha256(objects[new[path][2]][1]).hexdigest() != digest:
                raise PrivacyError()
        previous = objects[old["maintenance/upstream-version"][2]][1]
        current = objects[new["maintenance/upstream-version"][2]][1]
        if (not re.fullmatch(rb"(?:0|[1-9][0-9]{0,18})\n", previous)
                or int(previous) + 1 != version or current != f"{version}\n".encode()):
            raise PrivacyError()

        public_message = f"Update upstream maintenance to {version}\n\n{upstream}\nVibeCI-Upstream: {version}\n".encode()
        policy.check(public_message)
        publisher.mkdir(mode=0o700)
        _git(publisher, "init", "--bare", "--object-format=sha1", "--template=", str(publisher), limit=4096)
        expected = set(objects)
        while objects:
            oid, (kind, payload) = objects.popitem()
            if _git(publisher, "hash-object", "-t", kind, "-w", "--stdin", data=payload, limit=128) != (oid + "\n").encode():
                raise PrivacyError()
        # Advancing the validated timestamp by one second guarantees different
        # metadata even if the engine happened to use our neutral identity/text.
        safe = _sha(_git(publisher, "commit-tree", tree, "-p", base, data=public_message,
                         timestamp=timestamp + 1, limit=128).decode("ascii").strip())
        if safe == candidate or safe in expected:
            raise PrivacyError()
        final, final_trees, historical = _collect(publisher, [(safe, "commit")], policy)
        if set(final) != expected | {safe} or _commit(final[safe][1])[:2] != (tree, [base]):
            raise PrivacyError()
        remaining = MAX_OBJECTS
        for historical_tree in historical:
            _, used = _snapshot(historical_tree, final_trees, policy, remaining)
            remaining -= used
        return publisher, safe
    except Exception:
        raise PrivacyError() from None


def main(argv=None):
    """`check FILE...`: no success output; one fixed error and exit 1 on refusal."""
    try:
        args = sys.argv[1:] if argv is None else argv
        if not 2 <= len(args) <= MAX_OBJECTS + 1 or args[0] != "check":
            raise PrivacyError()
        policy, total = privacy_policy(), 0
        for name in args[1:]:
            policy.check(os.fsencode(name))
            payload = _read_file(Path(name).absolute())
            total += len(payload)
            if total > MAX_TOTAL_BYTES:
                raise PrivacyError()
            policy.check(payload)
        return 0
    except Exception:
        print(FAILURE, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
