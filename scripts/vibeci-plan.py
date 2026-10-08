#!/usr/bin/env python3
"""Prepare an atomic VibeCI proposal from a clean, committed maintenance baseline.

This program never invokes VibeCI, builds sources, or pushes. --push only permits
a separate controller to publish after privacy screening; the engine always
dry-runs. Public resolution is anonymous, except for Docker Hub's anonymous,
repository-scoped pull token.
"""

import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


SOURCES = {
    "server": ("mattermost/mattermost", "SERVER_COMMIT"),
    "calls": ("mattermost/mattermost-plugin-calls", "CALLS_COMMIT"),
    "transcriber": ("mattermost/calls-transcriber", "TRANSCRIBER_TAG"),
}
SERVER_IMAGE = "mattermost/mattermost-team-edition"
RECORDER_IMAGE = "mattermost/calls-recorder"
KEYS = {
    "SERVER_TAG", "SERVER_COMMIT", "SERVER_IMAGE", "CALLS_TAG", "CALLS_COMMIT",
    "RECORDER_TAG", "RECORDER_SOURCE_IMAGE", "OFFLOADER_TAG", "OFFLOADER_COMMIT",
    "TRANSCRIBER_TAG", "TRANSCRIBER_BRANCH", "GO_VERSION", "NODE_VERSION", "CALLS_VERSION",
}
SEMVER = r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_INTEGER = (1 << 63) - 1
MAX_VERSIONS = 2048
MAX_CALLS_CHECKS = 100
MAX_HTTP_BYTES = 2 << 20
HTTP_TIMEOUT = 20
GIT_TIMEOUT = 45
VERIFIER = "/opt/maintainedmost/verify.py"
INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}
IMAGE_TYPES = {
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
}
CONFIG_TYPES = {
    "application/vnd.oci.image.config.v1+json",
    "application/vnd.docker.container.image.v1+json",
}
MANIFEST_ACCEPT = ", ".join(sorted(INDEX_TYPES | IMAGE_TYPES))
PUBLIC_HOSTS = {"raw.githubusercontent.com", "api.github.com", "auth.docker.io", "registry-1.docker.io"}


class PlanError(ValueError):
    pass


def version(value, prefix=""):
    match = re.fullmatch(re.escape(prefix) + SEMVER, value) if isinstance(value, str) else None
    if not match or any(len(part) > 19 or int(part) > MAX_INTEGER for part in match.groups()):
        raise PlanError("expected a stable semantic version")
    return tuple(map(int, match.groups()))


def full_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise PlanError("expected a full lowercase Git SHA-1")
    return value


def branch_name(value):
    if (not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._/+-]{1,200}", value)
            or value.startswith("-") or value.endswith(".") or ".." in value
            or any(not part or part.startswith(".") or part.endswith(".lock") for part in value.split("/"))):
        raise PlanError("invalid branch name")
    return value


def repository_url(value):
    if (not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9_.-]{1,100}", value)
            or value.split("/")[1] in {".", ".."}):
        raise PlanError("repository must be OWNER/REPO on github.com")
    return "https://github.com/" + value + ".git"


def parse_env(text):
    if not isinstance(text, str) or not text.endswith("\n") or "\r" in text or "\x00" in text:
        raise PlanError("upstream.env must be newline-terminated text with LF line endings")
    pins = {}
    for line in text.split("\n"):
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([A-Za-z0-9_./:@+-]+)", line)
        if not match or match[1] not in KEYS or match[1] in pins:
            raise PlanError("upstream.env has an invalid, duplicate, or unknown assignment")
        pins[match[1]] = match[2]
    if pins.keys() != KEYS:
        raise PlanError("upstream.env is missing required assignments")
    for key in ("SERVER_TAG", "CALLS_TAG", "RECORDER_TAG", "OFFLOADER_TAG"):
        version(pins[key], "v")
    for key in ("SERVER_COMMIT", "CALLS_COMMIT", "OFFLOADER_COMMIT", "TRANSCRIBER_TAG"):
        full_sha(pins[key])
    version(pins["GO_VERSION"])
    version(pins["CALLS_VERSION"])
    if not re.fullmatch(r"[1-9][0-9]{0,3}", pins["NODE_VERSION"]):
        version(pins["NODE_VERSION"])
    branch_name(pins["TRANSCRIBER_BRANCH"])
    for key, image, tag in (("SERVER_IMAGE", SERVER_IMAGE, pins["SERVER_TAG"][1:]),
                            ("RECORDER_SOURCE_IMAGE", RECORDER_IMAGE, pins["RECORDER_TAG"])):
        prefix = f"docker.io/{image}:{tag}@"
        if not pins[key].startswith(prefix) or not DIGEST.fullmatch(pins[key][len(prefix):]):
            raise PlanError("image pins must match their version and contain a full sha256 digest")
    return pins


def replace_env(text, pins):
    return "\n".join(key + "=" + pins[key] if (key := line.partition("=")[0]) in pins else line
                     for line in text.split("\n"))


def replace_server_image(text, old, new):
    if re.search(r"(?im)^[ \t]*ARG\b[^\n]*[\\`][ \t]*$", text):
        raise PlanError("Containerfile ARG declarations must be single-line to validate SERVER_IMAGE")
    args = list(re.finditer(r"(?m)^[ \t]*(?i:ARG)[ \t]+SERVER_IMAGE\b[^\n]*$", text))
    if len(args) != 1:
        raise PlanError("Containerfile must contain exactly one ARG SERVER_IMAGE")
    match = re.fullmatch(r"([ \t]*(?i:ARG)[ \t]+SERVER_IMAGE=)([^ \t\r\n]+)([ \t]*)", args[0][0])
    if not match or match[2] != old:
        raise PlanError("Containerfile ARG SERVER_IMAGE does not match the committed pin")
    start = args[0].start() + match.start(2)
    end = args[0].start() + match.end(2)
    return text[:start] + new + text[end:]


def json_object(data, label):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise PlanError(label + " contains duplicate JSON keys")
            result[key] = value
        return result

    def invalid_constant(_):
        raise PlanError(label + " contains a non-finite JSON number")

    def finite_float(value):
        result = float(value)
        if not math.isfinite(result):
            invalid_constant(value)
        return result

    try:
        result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_constant, parse_float=finite_float)
    except (UnicodeError, ValueError, RecursionError) as error:
        if isinstance(error, PlanError):
            raise
        raise PlanError(label + " is not valid JSON") from error
    if not isinstance(result, dict):
        raise PlanError(label + " must be a JSON object")
    return result


def parse_models(data):
    return normalize_models(json_object(data, "models fragment"))


def normalize_models(fragment):
    """Validate private model settings without echoing them and replace aliases in order."""
    if not isinstance(fragment, dict) or fragment.keys() != {"providers", "models", "roles"}:
        raise PlanError("models fragment must contain only providers, models, and roles")
    if any(not isinstance(fragment[key], dict) or not fragment[key] for key in fragment):
        raise PlanError("providers, models, and roles must be nonempty objects")
    for key in ("providers", "models"):
        if any(not isinstance(name, str) or not name.strip() for name in fragment[key]):
            raise PlanError("provider and model aliases must be nonempty strings")
    provider_keys = {"type", "base_url", "auth", "api_key", "region", "timeout", "max_retries", "stream", "max_tokens_field"}
    model_keys = {"provider", "model", "max_tokens", "context_window", "thinking", "thinking_budget", "effort", "temperature", "prompt_cache"}
    for provider in fragment["providers"].values():
        if not isinstance(provider, dict) or provider.keys() - provider_keys:
            raise PlanError("unsupported provider fields; credential headers and other secret sources are forbidden")
        provider_type = provider.get("type")
        if provider_type not in ("anthropic", "openai-responses", "openai-chat", "bedrock"):
            raise PlanError("unsupported provider type")
        if provider.get("api_key") != "file:/run/secrets/llm_api_key":
            raise PlanError("each provider api_key must be file:/run/secrets/llm_api_key")
        auth = provider.get("auth", "")
        if (auth not in ("", "bearer", "x-api-key") or (auth == "x-api-key" and provider_type != "anthropic")
                or (provider_type == "bedrock" and auth != "bearer")):
            raise PlanError("provider auth must use the approved API-key file, not other credential sources")
        if "region" in provider and not isinstance(provider["region"], str):
            raise PlanError("provider region must be a string")
        if provider_type == "bedrock" and not (provider.get("region") or provider.get("base_url")):
            raise PlanError("provider requires a region or an HTTPS base_url")
        if "base_url" in provider:
            if not isinstance(provider["base_url"], str):
                raise PlanError("provider base_url must be an HTTPS URL string")
            try:
                url = urllib.parse.urlsplit(provider["base_url"])
                valid = (url.scheme == "https" and url.hostname and url.username is None and url.password is None
                         and not (url.query or url.fragment) and (url.port is None or 0 < url.port <= 65535)
                         and not re.search(r"[\s\x00-\x1f\x7f\\]", provider["base_url"]))
            except (ValueError, TypeError):
                valid = False
            if not valid:
                raise PlanError("provider base_url must be HTTPS without credentials, query, or fragment")
        if provider.get("max_tokens_field", "") not in ("", "max_tokens", "max_completion_tokens"):
            raise PlanError("unsupported provider max_tokens_field")
        if "max_retries" in provider and (type(provider["max_retries"]) is not int or not 0 <= provider["max_retries"] <= MAX_INTEGER):
            raise PlanError("provider max_retries must be a nonnegative integer")
        if "stream" in provider and type(provider["stream"]) is not bool:
            raise PlanError("provider stream must be a boolean")
        timeout = provider.get("timeout", 0)
        if isinstance(timeout, str):
            valid = re.fullmatch(r"\+?(?:0|(?:(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:ns|us|\u00b5s|\u03bcs|ms|s|m|h))+)", timeout)
        else:
            valid = type(timeout) in (int, float) and 0 <= timeout <= MAX_INTEGER / 1e9
        if not valid:
            raise PlanError("provider timeout must be a nonnegative duration or number of seconds")
    for model in fragment["models"].values():
        if not isinstance(model, dict) or model.keys() - model_keys:
            raise PlanError("unsupported model fields")
        if not isinstance(model.get("provider"), str) or model["provider"] not in fragment["providers"]:
            raise PlanError("each model must reference a provider in the fragment")
        if not isinstance(model.get("model"), str) or not model["model"].strip():
            raise PlanError("each model must have a nonempty model ID string")
        if any(type(model[key]) is not int or not 0 <= model[key] <= MAX_INTEGER
               for key in ("max_tokens", "context_window", "thinking_budget") if key in model):
            raise PlanError("model token limits must be nonnegative integers")
        if model.get("thinking", "") not in ("", "off", "adaptive", "enabled"):
            raise PlanError("unsupported model thinking mode")
        if model.get("effort", "") not in ("", "minimal", "low", "medium", "high", "xhigh", "max"):
            raise PlanError("unsupported model effort")
        if model.get("thinking") == "enabled" and model.get("thinking_budget", 0) >= (model.get("max_tokens") or 16000):
            raise PlanError("model thinking_budget must be below max_tokens")
        if "temperature" in model and (type(model["temperature"]) not in (int, float)
                                       or not -sys.float_info.max <= model["temperature"] <= sys.float_info.max):
            raise PlanError("model temperature must be a finite number")
        if "prompt_cache" in model and type(model["prompt_cache"]) is not bool:
            raise PlanError("model prompt_cache must be a boolean")
    roles = fragment["roles"]
    if roles.keys() - {"triage", "investigate", "audit", "resolve"} or not {"audit", "resolve"} <= roles.keys():
        raise PlanError("roles must supply audit and resolve, with only optional triage and investigate")
    if not isinstance(roles["resolve"], list) or not roles["resolve"]:
        raise PlanError("roles.resolve must be a nonempty model list")
    names = [roles[key] for key in roles if key != "resolve"] + roles["resolve"]
    if any(not isinstance(name, str) or name not in fragment["models"] for name in names):
        raise PlanError("roles must reference models in the fragment")
    providers = {name: f"provider-{index}" for index, name in enumerate(fragment["providers"], 1)}
    models = {name: f"model-{index}" for index, name in enumerate(fragment["models"], 1)}
    return {
        "providers": {providers[name]: dict(value) for name, value in fragment["providers"].items()},
        "models": {models[name]: dict(value, provider=providers[value["provider"]]) for name, value in fragment["models"].items()},
        "roles": {role: [models[name] for name in value] if role == "resolve" else models[value] for role, value in roles.items()},
    }


def git(cwd, *args, data=None, public=False):
    # Never inherit credentials, Git config overrides, proxy settings or tracing.
    env = {
        "PATH": os.defpath, "HOME": os.devnull, "XDG_CONFIG_HOME": os.devnull,
        "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull, "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "SSH_ASKPASS": "/bin/false",
        "GIT_ALLOW_PROTOCOL": "https", "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1", "GIT_ATTR_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "MaintainedMost Planner", "GIT_COMMITTER_NAME": "MaintainedMost Planner",
        "GIT_AUTHOR_EMAIL": "planner@maintainedmost.invalid", "GIT_COMMITTER_EMAIL": "planner@maintainedmost.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
    }
    if public:
        env["GIT_CEILING_DIRECTORIES"] = str(Path(cwd).resolve().parent)
    settings = (
        "core.hooksPath=/dev/null", "core.fsmonitor=false", "core.sshCommand=false",
        "credential.helper=", "credential.interactive=false", "protocol.allow=never",
        "protocol.https.allow=always", "protocol.ext.allow=never", "http.extraHeader=",
        "http.proxy=", "http.followRedirects=false", "http.sslVerify=true", "http.cookieFile=",
        "http.saveCookies=false", "http.lowSpeedLimit=1024", "http.lowSpeedTime=15",
        "core.attributesFile=/dev/null", "diff.external=", "commit.gpgSign=false", "tag.gpgSign=false",
    )
    command = ["git"] + [part for setting in settings for part in ("-c", setting)] + list(args)
    try:
        result = subprocess.run(command, cwd=cwd, env=env, input=data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=GIT_TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PlanError("Git operation failed or timed out") from error
    if result.returncode:
        raise PlanError("Git operation failed (details suppressed to avoid leaking local credentials)")
    if len(result.stdout) > 16 << 20:
        raise PlanError("Git output exceeds the planning limit")
    return result.stdout


def remote_refs(data):
    refs = {}
    try:
        lines = data.decode("ascii").splitlines()
    except UnicodeError as error:
        raise PlanError("invalid public Git ref encoding") from error
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{40})\t(refs/(?:tags|heads)/[^\s]+)", line)
        if not match or match[2] in refs:
            raise PlanError("invalid or duplicate public Git ref")
        refs[match[2]] = match[1]
        if len(refs) > MAX_VERSIONS * 2:
            raise PlanError("public Git version count exceeds the planning limit")
    return refs


def stable_tags(data):
    refs = remote_refs(data)
    tags = {}
    for ref, commit in refs.items():
        if ref.endswith("^{}"):
            if ref[:-3] not in refs:
                raise PlanError("annotated tag has no unpeeled ref")
            continue
        tag = ref.removeprefix("refs/tags/")
        if re.fullmatch("v" + SEMVER, tag):
            version(tag, "v")
            tags[tag] = refs.get(ref + "^{}", commit)
    if not tags or len(tags) > MAX_VERSIONS:
        raise PlanError("no stable tags, or too many versions to plan safely")
    return tags


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        # Docker config blobs may use a signed CDN URL. Never forward the pull token.
        cdn = (old.hostname == "registry-1.docker.io" and "/blobs/" in old.path
               and (new.hostname in {"production.cloudflare.docker.com", "production.cloudfront.docker.com"}
                     or (new.hostname or "").endswith(".r2.cloudflarestorage.com")))
        if (new.scheme != "https" or new.username or new.password or new.port not in (None, 443)
                or not (new.netloc == old.netloc or cdn)):
            raise PlanError("public HTTPS redirect refused")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new.netloc != old.netloc:
            redirected.remove_header("Authorization")
        return redirected


def https_get(url, headers=None):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in PUBLIC_HOSTS or parsed.username
            or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
        raise PlanError("public request must use an approved HTTPS endpoint")
    request = urllib.request.Request(url, headers={"User-Agent": "maintainedmost-release-planner", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), PublicRedirect())
    start = time.monotonic()
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            if response.status != 200:
                raise PlanError("public HTTPS request did not return HTTP 200")
            body = bytearray()
            while True:
                if time.monotonic() - start >= HTTP_TIMEOUT:
                    raise PlanError("public HTTPS response exceeds the size or time limit")
                chunk = response.read1(min(65536, MAX_HTTP_BYTES + 1 - len(body)))
                body.extend(chunk)
                if len(body) > MAX_HTTP_BYTES or time.monotonic() - start >= HTTP_TIMEOUT:
                    raise PlanError("public HTTPS response exceeds the size or time limit")
                if not chunk:
                    break
            return bytes(body), {key.lower(): value for key, value in response.headers.items()}
    except (OSError, urllib.error.URLError, ValueError) as error:
        if isinstance(error, PlanError):
            raise
        raise PlanError("public HTTPS request failed; no credentials or response body logged") from error


def image_digest(repository, tag):
    if repository not in {SERVER_IMAGE, RECORDER_IMAGE}:
        raise PlanError("unsupported image repository")
    version(tag, "v" if repository == RECORDER_IMAGE else "")
    scope = urllib.parse.urlencode({"service": "registry.docker.io", "scope": f"repository:{repository}:pull"})
    body, _ = https_get("https://auth.docker.io/token?" + scope)
    authorization = json_object(body, "registry token")
    token = authorization.get("token", authorization.get("access_token"))
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+={0,2}", token) or len(token) > 16384:
        raise PlanError("registry did not supply a valid anonymous pull token")
    headers = {"Accept": MANIFEST_ACCEPT, "Authorization": "Bearer " + token}
    base = f"https://registry-1.docker.io/v2/{repository}/"

    def manifest(ref, expected=None, size=None):
        raw, response_headers = https_get(base + "manifests/" + ref, headers)
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        if response_headers.get("docker-content-digest") != digest or (expected and expected != digest):
            raise PlanError("registry manifest digest mismatch")
        if size is not None and size != len(raw):
            raise PlanError("registry manifest size mismatch")
        document = json_object(raw, "registry manifest")
        media = document.get("mediaType")
        if (document.get("schemaVersion") != 2 or not isinstance(media, str) or media not in INDEX_TYPES | IMAGE_TYPES
                or response_headers.get("content-type", "").split(";")[0] != media):
            raise PlanError("registry must serve an OCI or Docker schema v2 manifest")
        return digest, document

    digest, document = manifest(tag)
    if document["mediaType"] in INDEX_TYPES:
        entries = document.get("manifests")
        if not isinstance(entries, list) or len(entries) > 128:
            raise PlanError("invalid registry image index")
        matches = [entry for entry in entries if isinstance(entry, dict)
                   and isinstance(entry.get("platform"), dict)
                   and entry["platform"].get("os") == "linux" and entry["platform"].get("architecture") == "amd64"]
        if not matches:
            raise PlanError("image has no linux/amd64 manifest")
        entry = matches[0]
        if (not isinstance(entry.get("mediaType"), str) or entry["mediaType"] not in IMAGE_TYPES
                or not DIGEST.fullmatch(str(entry.get("digest", "")))
                or type(entry.get("size")) is not int or not 0 < entry["size"] <= MAX_HTTP_BYTES):
            raise PlanError("linux/amd64 descriptor must identify a bounded image manifest")
        _, document = manifest(entry["digest"], entry["digest"], entry["size"])
        if document["mediaType"] not in IMAGE_TYPES:
            raise PlanError("linux/amd64 descriptor is not an image manifest")
    config = document.get("config")
    if (not isinstance(config, dict) or not isinstance(config.get("mediaType"), str) or config["mediaType"] not in CONFIG_TYPES
            or not DIGEST.fullmatch(str(config.get("digest", ""))) or type(config.get("size")) is not int
            or not 0 < config["size"] <= MAX_HTTP_BYTES):
        raise PlanError("cannot verify linux/amd64 without a pinned image config")
    raw, _ = https_get(base + "blobs/" + config["digest"], {"Authorization": "Bearer " + token})
    if "sha256:" + hashlib.sha256(raw).hexdigest() != config["digest"] or len(raw) != config["size"]:
        raise PlanError("registry image config digest or size mismatch")
    platform = json_object(raw, "image config")
    if platform.get("os") != "linux" or platform.get("architecture") != "amd64":
        raise PlanError("image config does not provide linux/amd64")
    return digest


class PublicResolver:
    def __init__(self, directory):
        self.directory = directory

    def tags(self, repository):
        return stable_tags(git(self.directory, "ls-remote", "--tags", "--", repository_url(repository),
                               "refs/tags/v*", "refs/tags/v*^{}", public=True))

    def head(self, repository, branch):
        ref = "refs/heads/" + branch_name(branch)
        refs = remote_refs(git(self.directory, "ls-remote", "--heads", "--", repository_url(repository), ref, public=True))
        if set(refs) != {ref}:
            raise PlanError("transcriber branch did not resolve to exactly one head")
        return refs[ref]

    def calls_manifest(self, commit):
        url = f"https://raw.githubusercontent.com/{SOURCES['calls'][0]}/{full_sha(commit)}/plugin.json"
        body, _ = https_get(url)
        return json_object(body, "Calls plugin.json")

    def is_ancestor(self, old, new):
        # Later comparison pages omit file diffs; only ancestry metadata is needed.
        url = (f"https://api.github.com/repos/{SOURCES['transcriber'][0]}/compare/"
               f"{full_sha(old)}...{full_sha(new)}?per_page=1&page=2")
        body, _ = https_get(url, {"Accept": "application/vnd.github+json"})
        comparison = json_object(body, "transcriber ancestry")
        return (comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
                and type(comparison.get("ahead_by")) is int and comparison["ahead_by"] > 0
                and isinstance(comparison.get("base_commit"), dict) and comparison["base_commit"].get("sha") == old
                and isinstance(comparison.get("merge_base_commit"), dict) and comparison["merge_base_commit"].get("sha") == old)

    def image_digest(self, repository, tag):
        return image_digest(repository, tag)


def plugin_versions(document):
    if not isinstance(document, dict) or not isinstance(document.get("props"), dict):
        raise PlanError("Calls plugin.json must contain min_server_version and recorder props")
    minimum = version(document.get("min_server_version"))
    recorder = document["props"].get("calls_recorder_version")
    if version(recorder, "v") < (0, 6, 0):
        raise PlanError("Calls recorder is below the supported v0.6.0 minimum")
    return minimum, recorder


def select_pins(old, resolver):
    server_tags = resolver.tags(SOURCES["server"][0])
    calls_tags = resolver.tags(SOURCES["calls"][0])
    for component, tags in (("SERVER", server_tags), ("CALLS", calls_tags)):
        if tags.get(old[component + "_TAG"]) != old[component + "_COMMIT"]:
            raise PlanError(component + " current tag is missing or was rewritten; manual baseline decision required")
    server_version = version(old["SERVER_TAG"], "v")
    server = max((tag for tag in server_tags if version(tag, "v")[0] == server_version[0]), key=lambda tag: version(tag, "v"))
    if version(server, "v") < server_version:
        raise PlanError("server downgrade refused")
    current_manifest = resolver.calls_manifest(old["CALLS_COMMIT"])
    minimum, recorder = plugin_versions(current_manifest)
    if minimum > server_version or recorder != old["RECORDER_TAG"]:
        raise PlanError("current Calls manifest is incompatible with the pinned server or recorder")
    candidates = sorted((tag for tag in calls_tags if version(tag, "v") >= version(old["CALLS_TAG"], "v")),
                        key=lambda tag: version(tag, "v"), reverse=True)
    calls = None
    for index, tag in enumerate(candidates):
        if index >= MAX_CALLS_CHECKS:
            raise PlanError("Calls compatibility lookup limit exceeded")
        document = current_manifest if tag == old["CALLS_TAG"] else resolver.calls_manifest(full_sha(calls_tags[tag]))
        minimum, candidate_recorder = plugin_versions(document)
        if minimum <= version(server, "v"):
            calls, recorder = tag, candidate_recorder
            break
    if calls is None:
        raise PlanError("no compatible Calls release without a downgrade")
    if version(recorder, "v") < version(old["RECORDER_TAG"], "v"):
        raise PlanError("selected Calls release would downgrade the recorder")
    transcriber = full_sha(resolver.head(SOURCES["transcriber"][0], old["TRANSCRIBER_BRANCH"]))
    if transcriber != old["TRANSCRIBER_TAG"] and not resolver.is_ancestor(old["TRANSCRIBER_TAG"], transcriber):
        raise PlanError("transcriber branch rewound or diverged; manual baseline decision required")
    new = dict(old, SERVER_TAG=server, SERVER_COMMIT=full_sha(server_tags[server]),
               CALLS_TAG=calls, CALLS_COMMIT=full_sha(calls_tags[calls]), RECORDER_TAG=recorder, TRANSCRIBER_TAG=transcriber)
    for key, repository, old_tag, new_tag in (
        ("SERVER_IMAGE", SERVER_IMAGE, old["SERVER_TAG"][1:], server[1:]),
        ("RECORDER_SOURCE_IMAGE", RECORDER_IMAGE, old["RECORDER_TAG"], recorder),
    ):
        live = resolver.image_digest(repository, old_tag)
        if live != old[key].split("@", 1)[1]:
            raise PlanError("current image tag was retagged; manual baseline decision required")
        if old_tag != new_tag:
            digest = resolver.image_digest(repository, new_tag)
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
                raise PlanError("invalid resolved image digest")
            new[key] = f"docker.io/{repository}:{new_tag}@{digest}"
    if new != old:
        major, minor, patch = version(old["CALLS_VERSION"])
        if patch == MAX_INTEGER:
            raise PlanError("Calls bundle patch version exhausted")
        new["CALLS_VERSION"] = f"{major}.{minor}.{patch + 1}"
    return new


def read_baseline(directory):
    root = Path(git(directory, "rev-parse", "--show-toplevel").decode().strip())
    base = full_sha(git(root, "rev-parse", "--verify", "HEAD^{commit}").decode().strip())
    status = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    input_roots = {"maintenance", "patches", "scripts", "config", ".github", "upstream.env", "Containerfile",
                   ".gitattributes", ".gitmodules", ".gitignore", ".env.example", "compose.yaml"}
    for entry in status.split(b"\0"):
        if entry and (entry[:2] != b"??" or entry[3:].decode().split("/", 1)[0] in input_roots):
            raise PlanError("planning requires clean committed tracked files and no untracked maintenance inputs")
    # Do not let ignore rules hide extra maintenance sources; ordinary build and
    # bytecode outputs are not inputs and may remain in the checkout.
    untracked = git(root, "ls-files", "--others", "-z", "--", "maintenance/upstream-version",
                    "maintenance/*.py", "maintenance/*.json", "maintenance/*.md", "patches/*/*.patch",
                    "scripts/*.py", "scripts/*.sh", "scripts/*.cjs", "scripts/*.go")
    if untracked:
        raise PlanError("untracked maintenance inputs must be committed or removed before planning")
    tree = {}
    for entry in git(root, "ls-tree", "-rz", base).split(b"\0"):
        if entry:
            metadata, path = entry.split(b"\t", 1)
            mode, kind, oid = metadata.decode().split()
            tree[path.decode()] = (mode, kind, oid)

    def read(path, limit=65536):
        if path not in tree or tree[path][0] not in {"100644", "100755"} or tree[path][1] != "blob":
            raise PlanError("required maintenance input must be a committed regular file: " + path)
        data = git(root, "cat-file", "blob", tree[path][2])
        if len(data) > limit:
            raise PlanError("committed maintenance input exceeds its size limit: " + path)
        return data

    patches = []
    for component in SOURCES:
        paths = sorted(path for path in tree if path.startswith(f"patches/{component}/") and path.endswith(".patch"))
        if not paths:
            raise PlanError("each component must have a committed patch inventory")
        for path in paths:
            if (not re.fullmatch(r"patches/[a-z]+/[A-Za-z0-9][A-Za-z0-9_.-]*\.patch", path)
                    or tree[path][0] not in {"100644", "100755"} or tree[path][1] != "blob"):
                raise PlanError("patch inventory must contain regular files directly inside each component directory")
        patches.extend(paths)
    selector = read("maintenance/upstream-version")
    if not re.fullmatch(rb"(?:0|[1-9][0-9]{0,18})\n", selector) or int(selector) >= MAX_INTEGER:
        raise PlanError("maintenance/upstream-version must contain one nonnegative integer and a newline")
    files = {path: read(path) for path in ("upstream.env", "Containerfile", "maintenance/intent.md")}
    files["maintenance/verify.py"] = read("maintenance/verify.py", 1 << 20)
    if not files["maintenance/intent.md"].strip():
        raise PlanError("committed maintenance/intent.md must describe the fork's obligations")
    return base, int(selector), files, patches


def create_manifest(output, old_version, old, new):
    path = Path(output) / "input" / "upstream.git"
    path.parent.mkdir(mode=0o700)
    path.mkdir(mode=0o700)  # Never reuse a repository or overwrite an existing tag.
    git(path, "init", "--bare", "--object-format=sha1", "--template=", ".")
    parent = None
    for number, content in ((old_version, old), (old_version + 1, new)):
        blob = full_sha(git(path, "hash-object", "-w", "--stdin", data=content).decode().strip())
        tree = full_sha(git(path, "mktree", data=f"100644 blob {blob}\tupstream.env\n".encode()).decode().strip())
        args = ["commit-tree", tree] + (["-p", parent] if parent else [])
        commit = full_sha(git(path, *args, data=f"Upstream manifest v{number}\n".encode()).decode().strip())
        git(path, "update-ref", f"refs/tags/v{number}", commit, "0" * 40)
        parent = commit
    git(path, "symbolic-ref", "HEAD", f"refs/tags/v{old_version + 1}")
    return parent


def build_config(repository, branch, proposal, models, binding, files, description, verifier, push):
    """Build a private dry-run config; push is ignored here, retained for direct callers.

    Only the plan summary opts in to separate controller publication after privacy screening.
    """
    models = normalize_models(models)
    encoded = base64.urlsafe_b64encode(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).decode()
    verifier_hash = hashlib.sha256(verifier).hexdigest()
    verify = (f"printf '%s\\n' '{verifier_hash}  {VERIFIER}' | sha256sum -c - && "
              f"python3 {VERIFIER} '{encoded}'")
    return {
        **models, "data_dir": "/data", "repo_timeout": "2h", "alerts": [], "log_level": "error",
        "identity": {"name": "MaintainedMost Maintenance", "email": "maintenance@maintainedmost.invalid"},
        "sandbox": {"mode": "broker", "socket": "/run/vibeci/sandboxd.sock"},
        "repos": [{
            "name": "maintainedmost", "fork": {"url": repository_url(repository), "branch": branch,
                                                 "auth": {"token": "file:/run/secrets/fork_read_token"}},
            "upstream": {"url": "/data/input/upstream.git", "fetch": "full", "tags": f"v{binding['version']}", "tag_format": "v{version}"},
            "description": description,
            "patches": {
                "version_file": "maintenance/upstream-version", "update_files": files,
                "sets": [{"glob": f"patches/{component}/*.patch", "root": component, "strip": 1} for component in SOURCES],
                "sources": [{"path": component, "url": repository_url(repository), "fetch": "partial",
                             "revision_file": "upstream.env", "revision_regex": rf"(?m)^{key}=([0-9a-f]{{40}})$"}
                            for component, (repository, key) in SOURCES.items()],
                "fuzz": 0, "ignore_whitespace": False, "drop_upstreamed": False, "verify_tree": "touched",
            },
            "sandbox": {"profile": "maintainedmost", "verify_profile": "maintainedmost"}, "prefetch": [],
            "verify": [{"name": "trusted-maintenance-verifier", "run": verify, "timeout": "5m"}],
            "review": {"enabled": False, "block_on": "suspicious", "min_confidence": 0.7},
            "on_upstream_rewrite": "hold", "push": {"mode": "branch", "branch": proposal, "dry_run": True},
        }],
    }


def write_private(path, content):
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as target:
        os.fchmod(target.fileno(), 0o600)
        target.write(content)


def plan(directory, repository, branch, output, models_file, push=False, resolver=None):
    repository_url(repository)
    branch_name(branch)
    output = Path(output).absolute()
    if output.exists() or output.is_symlink():
        raise PlanError("--output must name a fresh, nonexistent directory")
    with open(models_file, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600 or metadata.st_nlink != 1:
            raise PlanError("models fragment must be a regular, single-link file with mode 0600")
        models_data = source.read(65537)
    if len(models_data) > 65536:
        raise PlanError("models fragment exceeds 64 KiB")
    models = parse_models(models_data)
    base, old_version, committed, patches = read_baseline(directory)
    text = committed["upstream.env"].decode("utf-8")
    containerfile = committed["Containerfile"].decode("utf-8")
    old = parse_env(text)
    replace_server_image(containerfile, old["SERVER_IMAGE"], old["SERVER_IMAGE"])
    if "{version}" in text or "{version}" in containerfile:
        raise PlanError("update_files inputs must not contain VibeCI's {version} substitution token")
    output.mkdir(mode=0o700)
    output.chmod(0o700)
    new = select_pins(old, resolver or PublicResolver(output))
    summary = {"changed": new != old, "base": base, "branch": None, "version": old_version,
               "manifest": None, "push": bool(push), "old": old, "new": new}
    if summary["changed"]:
        replacements = {"upstream.env": replace_env(text, new),
                        "Containerfile": replace_server_image(containerfile, old["SERVER_IMAGE"], new["SERVER_IMAGE"])}
        for content in replacements.values():
            if len(content.encode()) > 65536:
                raise PlanError("update_files replacement exceeds VibeCI's 64 KiB limit")
        manifest = create_manifest(output, old_version, text.encode(), replacements["upstream.env"].encode())
        number = old_version + 1
        # A retry must not reuse an already-updated proposal as VibeCI's base.
        proposal = f"vibeci/update-{number}-{base[:12]}-{manifest[:12]}-{secrets.token_hex(6)}"
        if proposal == branch:
            raise PlanError("proposal branch must differ from the trusted base branch")
        expected = {**replacements, "maintenance/upstream-version": f"{number}\n"}
        binding = {"base": base, "version": number, "manifest": manifest,
                   "files": {path: hashlib.sha256(content.encode()).hexdigest() for path, content in expected.items()},
                   "patches": patches}
        config = build_config(repository, branch, proposal, models, binding, replacements,
                              committed["maintenance/intent.md"].decode("utf-8"), committed["maintenance/verify.py"], push)
        for name, value in (("plan.json", binding), ("config.json", config)):
            # Preserve numeric alias order through JSON round trips (including model-10).
            write_private(output / name, json.dumps(value, indent=2, sort_keys=name != "config.json") + "\n")
        summary.update(branch=proposal, version=number, manifest=manifest)
    lines = ["# Atomic Upstream Maintenance", "", f"Base: `{base}`", "",
             f"Maintenance selector: `{old_version}` -> `{summary['version']}`", ""]
    if summary["changed"]:
        lines += [f"Proposal: `{summary['branch']}`", f"Manifest: `{summary['manifest']}`", "",
                  "| Pin | Before | After |", "| --- | --- | --- |"]
        lines += [f"| {key} | `{old[key]}` | `{new[key]}` |" for key in sorted(KEYS) if old[key] != new[key]]
        lines += ["", "Server major upgrades require a manual baseline decision. Offloader and toolchain pins are unchanged.",
                  "The Calls bundle version advances even when its upstream source is unchanged.",
                  "All baseline patches and fork behavioral obligations must survive trusted offline verification and normal CI.",
                  "", "Publication: " + ("proposal branch only" if push else "dry run (no push)") + "."]
    else:
        lines += ["No upstream update. Current tag, image digest, platform, and Calls compatibility checks passed."]
    write_private(output / "summary.md", "\n".join(lines) + "\n")
    write_private(output / "summary.json", json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True, help="GitHub OWNER/REPO; no URL or credentials")
    parser.add_argument("--branch", default="main", help="trusted fork branch (default: main)")
    parser.add_argument("--output", required=True, type=Path, help="fresh directory, later mounted at /data")
    parser.add_argument("--models", required=True, type=Path, help="private 0600 providers/models/roles JSON with file:/run/secrets/llm_api_key")
    parser.add_argument("--push", action="store_true", help="allow separate controller publication after privacy screening; engine always dry-runs")
    args = parser.parse_args(argv)
    try:
        summary = plan(Path.cwd(), args.repository, args.branch, args.output, args.models, args.push)
    except (PlanError, OSError, UnicodeError) as error:
        message = str(error) if isinstance(error, PlanError) else "unable to read or write a required planning input/output"
        print("vibeci-plan: " + message, file=sys.stderr)
        return 1
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
