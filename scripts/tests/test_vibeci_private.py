"""Offline publication tests: synthetic canaries, isolated bare Git, no secrets."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import runpy
import secrets
import subprocess
import sys
import tempfile
import traceback
import unittest
from unittest import mock
from urllib.parse import quote, quote_plus


SCRIPT = Path(__file__).resolve().parents[1] / "vibeci-private.py"
API = runpy.run_path(str(SCRIPT))
GLOBALS = API["prepare_publication"].__globals__
PrivacyError = API["PrivacyError"]
TEMP_ROOT = Path(tempfile.gettempdir()).resolve()
TERM = "DummyVendorCanary"
MODEL = "dummy-model-canary-42"
HOST = "gateway.dummy-provider.invalid"
URL = "https://" + HOST + "/v1"
LLM_TOKEN = 'llm-canary-Qx7+/="\\9'
GH_TOKEN = "github-canary-Wr8+/=7"
READ_TOKEN = "reader-canary-Eu9+/_"
BUILTIN_TOKEN = "builtin-canary-Ti6+/_"
MODELS = {
    "providers": {"generic": {"type": "dummy", "base_url": URL, "api_key": "file:/run/secrets/llm_api_key"}},
    "models": {"strong": {"provider": "generic", "model": MODEL}},
    "roles": {"audit": "strong", "resolve": ["strong"]},
}
ENV = {
    "PATH": os.defpath, "HOME": os.devnull, "XDG_CONFIG_HOME": os.devnull, "TMPDIR": str(TEMP_ROOT),
    "VIBECI_PRIVATE_TERMS": json.dumps([TERM]), "VIBECI_MODELS": json.dumps(MODELS),
    "VIBECI_LLM_API_KEY": LLM_TOKEN, "GH_TOKEN": GH_TOKEN, "VIBECI_FORK_READ_TOKEN": READ_TOKEN,
    "GITHUB_TOKEN": BUILTIN_TOKEN,
}
MANIFEST = "e" * 40
PATCHES = [f"patches/{component}/0001-feature.patch" for component in ("server", "calls", "transcriber")]
PATCH = b"--- a/feature.txt\n+++ b/feature.txt\n@@ -1 +1 @@\n-old\n+preserved behavior\n"


def setUpModule():
    guard = mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)


def metadata(number):
    return {"maintenance/upstream-version": f"{number}\n".encode(),
            "upstream.env": f"PIN_VERSION={number}\n".encode(),
            "Containerfile": f"ARG PIN_VERSION={number}\nFROM scratch\n".encode()}


def scan_current_head():
    """Read-only integration probe. Return counts/booleans, never object contents."""
    repository = SCRIPT.parents[1] / ".git"
    models = copy.deepcopy(MODELS)
    models["providers"]["generic"]["base_url"] = "https://dummy-" + secrets.token_hex(16) + ".example.invalid/v1"
    models["models"]["strong"]["model"] = "dummy-model-" + secrets.token_hex(16)
    env = {**ENV, "VIBECI_MODELS": json.dumps(models),
           "VIBECI_PRIVATE_TERMS": json.dumps(["DummyCanary" + secrets.token_hex(16)])}
    for key in ("VIBECI_LLM_API_KEY", "GH_TOKEN", "VIBECI_FORK_READ_TOKEN", "GITHUB_TOKEN"):
        env[key] = "dummy-credential-" + secrets.token_hex(24)
    report = {"passed": False, "collection_passed": False, "paths_passed": False, "nonbare_source": False,
              "objects_checked": 0, "bytes_checked": 0, "max_object_bytes": 0, "png_blobs": 0,
              "package_lock_blobs": 0, "path_results": 0, "max_snapshot_paths": 0, "encoded_runs_on_refusal": 0,
              "object_limit": API["MAX_OBJECTS"], "object_bytes_limit": API["MAX_OBJECT_BYTES"],
              "total_bytes_limit": API["MAX_TOTAL_BYTES"], "scan_results_limit": API["MAX_SCAN_RESULTS"],
              "scan_bytes_limit": API["MAX_SCAN_BYTES"], "path_limit": API["MAX_OBJECTS"]}
    original_git = GLOBALS["_git"]

    def readonly_git(root, *args, **kwargs):
        if root != repository or not args or args[0] not in {"rev-parse", "cat-file"}:
            raise AssertionError("history probe attempted a non-read-only Git operation")
        return original_git(root, *args, **kwargs)

    try:
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.dict(GLOBALS, {"_git": readonly_git}):
            policy = API["privacy_policy"]()

            class CountedPolicy:
                def check(self, payload):
                    report["objects_checked"] += 1
                    report["bytes_checked"] += len(payload)
                    report["max_object_bytes"] = max(report["max_object_bytes"], len(payload))
                    report["png_blobs"] += payload.startswith(b"\x89PNG\r\n\x1a\n")
                    try:
                        policy.check(payload)
                    except PrivacyError:
                        report["encoded_runs_on_refusal"] = sum(1 for pattern in API["ENCODED"] for _ in pattern.finditer(payload))
                        raise

            report["nonbare_source"] = readonly_git(repository, "rev-parse", "--is-bare-repository", limit=64) == b"false\n"
            head = API["_sha"](readonly_git(repository, "rev-parse", "--verify", "HEAD^{commit}", limit=128).decode().strip())
            objects, trees, history = API["_collect"](repository, [(head, "commit")], CountedPolicy())
            report["collection_passed"] = True
            report.update({kind + "s": sum(entry[0] == kind for entry in objects.values()) for kind in ("commit", "tree", "blob")})
            report["snapshots"] = len(history)
            remaining, lock_blobs = API["MAX_OBJECTS"], set()
            for tree in history:
                snapshot, count = API["_snapshot"](tree, trees, policy, remaining)
                remaining -= count
                report["path_results"] += count
                report["max_snapshot_paths"] = max(report["max_snapshot_paths"], count)
                lock_blobs.update(entry[2] for path, entry in snapshot.items() if path.endswith("package-lock.json"))
            report["package_lock_blobs"] = len(lock_blobs)
            report["paths_passed"] = True
            report["head_unchanged"] = readonly_git(repository, "rev-parse", "--verify", "HEAD^{commit}", limit=128) == (head + "\n").encode()
            report["passed"] = report["head_unchanged"] and report["nonbare_source"]
    except Exception:
        pass
    return report


class CurrentHistoryTests(unittest.TestCase):
    def test_real_head_graph_is_read_only_and_within_privacy_limits(self):
        repository = SCRIPT.parents[1] / ".git"
        if not repository.is_dir() or (repository / "shallow").exists():
            self.skipTest("read-only integration requires a full ordinary Git checkout")
        report = scan_current_head()
        self.assertTrue(report["passed"], json.dumps(report, sort_keys=True))


class CanaryCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, ENV, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def generic_failure(self, operation):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                operation()
            except PrivacyError as error:
                self.assertEqual(str(error), "Publication privacy check failed.")
                diagnostic = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                for value in (TERM, MODEL, HOST, LLM_TOKEN, GH_TOKEN, READ_TOKEN, BUILTIN_TOKEN):
                    self.assertNotIn(value, diagnostic)
                self.assertTrue(error.__suppress_context__ or error.__context__ is None)
            else:
                self.fail("expected a closed privacy gate")
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")


class PolicyTests(CanaryCase):
    def test_literal_binary_case_and_common_encodings(self):
        policy = API["privacy_policy"]()
        values = [TERM, TERM.lower(), TERM.upper(), TERM.swapcase(), MODEL, MODEL.upper(),
                  HOST, HOST.upper(), URL, LLM_TOKEN, GH_TOKEN, READ_TOKEN, BUILTIN_TOKEN]
        for index, value in enumerate(values):
            raw = value.encode()
            encodings = [raw, quote(value, safe="").encode(), quote_plus(value).encode(),
                         "".join(f"%{byte:02X}" for byte in raw).encode(),
                         "".join(f"%{byte:02x}" for byte in raw).encode(),
                         json.dumps(value)[1:-1].encode(), json.dumps(value)[1:-1].replace("/", "\\/").encode(),
                         "".join(f"\\u{ord(char):04X}" for char in value).encode(),
                         "".join(f"\\x{byte:02x}" for byte in raw).encode(),
                         "".join(f"\\x{byte:02X}" for byte in raw).encode(),
                         base64.b64encode(raw), base64.b64encode(raw).rstrip(b"="),
                         base64.urlsafe_b64encode(raw), base64.urlsafe_b64encode(raw).rstrip(b"="),
                         raw.hex().encode(), raw.hex().upper().encode(),
                         base64.b64encode(b"a wrapped value: " + raw + b"; end"),
                         base64.b64encode(base64.b64encode(raw)),
                         quote(base64.b64encode(raw).decode(), safe="").encode()]
            for encoding, encoded in enumerate(encodings):
                with self.subTest(value=index, encoding=encoding):
                    self.generic_failure(lambda: policy.check(b"\xff\0!" + encoded + b"!\0\xfe"))

    def test_partial_url_json_unicode_and_nested_json_encodings(self):
        values = [b"Dummy%56endorCanary", b"Dummy\\u0056endorCanary", b"Dummy%2556endorCanary",
                  base64.b64encode(json.dumps({"credential": LLM_TOKEN}).encode())]
        policy = API["privacy_policy"]()
        for index, value in enumerate(values):
            with self.subTest(index=index):
                self.generic_failure(lambda: policy.check(value))
        os.environ["VIBECI_PRIVATE_TERMS"] = json.dumps(["Dumm\u00fdCan\u00e1ry", "dummy word canary"])
        policy = API["privacy_policy"]()
        for value in ("DUMM\u00ddCAN\u00c1RY".encode(), json.dumps("DUMM\u00ddCAN\u00c1RY").encode(),
                      b"dummy+word+canary", b"dummy%20word%20canary"):
            self.generic_failure(lambda: policy.check(value))

    def test_role_and_provider_aliases_are_not_prohibited(self):
        policy = API["privacy_policy"]()
        policy.check(b"A strong generic provider adapter uses file:/run/secrets/llm_api_key.\n\0\xff")
        policy.check(b"Ordinary public patch content and a binary PNG header: \x89PNG\r\n")
        self.assertNotIn(TERM, repr(policy))
        self.assertNotIn(LLM_TOKEN, repr(policy))

    def test_only_the_approved_file_key_reference_is_accepted_without_reading_it(self):
        for reference in (None, "", LLM_TOKEN, "env:VIBECI_LLM_API_KEY", "file:/tmp/other-key", "/run/secrets/llm_api_key"):
            models = copy.deepcopy(MODELS)
            models["providers"]["generic"]["api_key"] = reference
            with mock.patch.dict(os.environ, {"VIBECI_MODELS": json.dumps(models)}):
                self.generic_failure(API["privacy_policy"])
        with mock.patch("builtins.open", side_effect=AssertionError("no secret file reads")), \
                mock.patch("os.open", side_effect=AssertionError("no secret file reads")):
            API["privacy_policy"]().check(b"public content")

    def test_outer_environment_whitespace_cannot_weaken_the_denylist(self):
        with mock.patch.dict(os.environ, {"VIBECI_PRIVATE_TERMS": json.dumps([" " + TERM + " "]),
                                           "GH_TOKEN": "\n" + GH_TOKEN + "\n"}):
            policy = API["privacy_policy"]()
            for value in (TERM, GH_TOKEN, "\n" + GH_TOKEN + "\n"):
                self.generic_failure(lambda: policy.check(value.encode()))

    def test_environment_is_loaded_at_each_public_call_without_file_reads(self):
        with mock.patch("builtins.open", side_effect=AssertionError("no file reads")), \
                mock.patch("os.open", side_effect=AssertionError("no file reads")):
            old_policy = API["privacy_policy"]()
            new_token = "changed-credential-canary-99"
            os.environ["GH_TOKEN"] = new_token
            old_policy.check(new_token.encode())
            self.generic_failure(lambda: API["scan_environment_bytes"](new_token.encode()))
        del os.environ["VIBECI_FORK_READ_TOKEN"]
        API["privacy_policy"]().check(READ_TOKEN.encode())
        os.environ["VIBECI_GIT_TOKEN"] = READ_TOKEN
        self.generic_failure(lambda: API["scan_environment_bytes"](READ_TOKEN.encode()))

    def test_missing_malformed_or_oversized_environment_fails_generically(self):
        required = ("VIBECI_PRIVATE_TERMS", "VIBECI_MODELS", "VIBECI_LLM_API_KEY", "GH_TOKEN")
        for key in required:
            for value in (None, "", " ", "x" * 65537):
                with self.subTest(key=key, value_length=None if value is None else len(value)), \
                        mock.patch.dict(os.environ, ENV, clear=True):
                    if value is None:
                        del os.environ[key]
                    else:
                        os.environ[key] = value
                    self.generic_failure(API["privacy_policy"])
        for terms in ([], {}, "private", ["abc"], ["    "], [False], [TERM, TERM.lower()], ["x" * 4097]):
            with self.subTest(terms_type=type(terms).__name__), mock.patch.dict(os.environ, {"VIBECI_PRIVATE_TERMS": json.dumps(terms)}):
                self.generic_failure(API["privacy_policy"])
        invalid = ["{" + LLM_TOKEN, "[]", '{"providers":{},"providers":{}}', '{"number":NaN}',
                   '{"number":1e999}', "[" * 2000 + "]" * 2000]
        for document in invalid:
            with mock.patch.dict(os.environ, {"VIBECI_MODELS": document}):
                self.generic_failure(API["privacy_policy"])
        for part, replacement in (("providers", {}), ("models", {}), ("roles", [])):
            fragment = dict(MODELS, **{part: replacement})
            with mock.patch.dict(os.environ, {"VIBECI_MODELS": json.dumps(fragment)}):
                self.generic_failure(API["privacy_policy"])

    def test_invalid_provider_and_model_configuration_is_not_silently_ignored(self):
        fragments = []
        for value in (None, 42, "", "http://dummy.invalid/v1", "https://user:pass@dummy.invalid/",
                      "https://dummy.invalid/?secret=value", "https://dummy.invalid:bad/"):
            fragment = copy.deepcopy(MODELS)
            fragment["providers"]["generic"]["base_url"] = value
            fragments.append(fragment)
        for value in (None, "", 42):
            fragment = copy.deepcopy(MODELS)
            fragment["models"]["strong"]["model"] = value
            fragments.append(fragment)
        fragment = copy.deepcopy(MODELS)
        fragment["providers"]["generic"]["api_key"] = LLM_TOKEN
        fragments.append(fragment)
        fragment = copy.deepcopy(MODELS)
        fragment["models"]["strong"]["provider"] = "missing-alias"
        fragments.append(fragment)
        fragment = copy.deepcopy(MODELS)
        fragment["providers"]["generic"]["headers"] = {"Authorization": LLM_TOKEN}
        fragments.append(fragment)
        for index, fragment in enumerate(fragments):
            with self.subTest(index=index), mock.patch.dict(os.environ, {"VIBECI_MODELS": json.dumps(fragment)}):
                self.generic_failure(API["privacy_policy"])

    def test_scanner_size_result_and_decoding_budgets_fail_closed(self):
        policy = API["privacy_policy"]()
        with mock.patch.dict(GLOBALS, {"MAX_OBJECT_BYTES": 32}):
            self.generic_failure(lambda: policy.check(b"x" * 33))
        with mock.patch.dict(GLOBALS, {"MAX_SCAN_RESULTS": 2}):
            self.generic_failure(lambda: policy.check(b"abcdefgh ijklmnop qrstuvwx"))
        with mock.patch.dict(GLOBALS, {"MAX_SCAN_BYTES": 24}):
            self.generic_failure(lambda: policy.check(b"%41" * 8))
        for payload in (None, "plain text", bytearray(b"plain bytes")):
            self.generic_failure(lambda: policy.check(payload))


class PublicationTests(CanaryCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix="publication-canary-", dir=TEMP_ROOT)
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        self.git_env = {
            "PATH": os.defpath, "HOME": os.devnull, "XDG_CONFIG_HOME": os.devnull,
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0",
            "GIT_AUTHOR_NAME": "Dummy Fixture", "GIT_COMMITTER_NAME": "Dummy Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "@1700000000 +0000", "GIT_COMMITTER_DATE": "@1700000000 +0000",
        }
        self.fixture()

    def git_result(self, repository, *args, data=None):
        return subprocess.run(["git", "--literal-pathspecs", "--git-dir=" + str(repository),
                               "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                               "-c", "credential.helper=", "-c", "commit.gpgSign=false", "-c", "protocol.allow=never", *args],
                              cwd=repository.parent, env=self.git_env, input=data, capture_output=True, timeout=20, check=False)

    def git(self, repository, *args, data=None):
        result = self.git_result(repository, *args, data=data)
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        return result.stdout

    def object(self, kind, payload):
        return self.git(self.mirror, "hash-object", "-t", kind, "-w", "--stdin", data=payload).decode().strip()

    def tree(self, files):
        root = {}
        for path, value in files.items():
            node = root
            parts = path.split("/")
            for component in parts[:-1]:
                node = node.setdefault(component, {})
            node[parts[-1]] = value

        def build(node):
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    mode, kind, oid = "040000", "tree", build(value)
                else:
                    mode, payload = value if isinstance(value, tuple) else ("100644", value)
                    kind, oid = "blob", self.object("blob", payload)
                entries.append(f"{mode} {kind} {oid}\t{name}".encode() + b"\0")
            return self.git(self.mirror, "mktree", "-z", data=b"".join(entries)).decode().strip()

        return build(root)

    def commit(self, tree, message, parents=()):
        args = [part for parent in parents for part in ("-p", parent)]
        return self.git(self.mirror, "commit-tree", tree, *args, data=message).decode().strip()

    def fixture(self, base_files=None, ancestor_files=None, ancestor_message=b"Earlier public history\n"):
        self.run = Path(tempfile.mkdtemp(prefix="run-", dir=self.temp))
        self.data = self.run / "data"
        self.mirror = self.data / "mirrors/maintainedmost.git"
        self.mirror.mkdir(parents=True)
        self.publisher = self.run / "publication.git"
        self.git(self.mirror, "init", "--bare", "--object-format=sha1", "--template=", str(self.mirror))
        self.files = {**metadata(0), **dict.fromkeys(PATCHES, PATCH),
                      "README.md": b"Public strong generic adapter.\n",
                      "assets/pixel.bin": b"\x89PNG\r\n\x00\xffpublic binary fixture\x00",
                      "maintenance/verify.py": b"raise RuntimeError('candidate code must never execute')\n"}
        if base_files:
            self.files.update(base_files)
        parents = ()
        if ancestor_files is not None:
            parents = (self.commit(self.tree({**self.files, **ancestor_files}), ancestor_message),)
        self.base_tree = self.tree(self.files)
        self.base = self.commit(self.base_tree, b"Trusted public baseline\n", parents)
        self.plan = {"base": self.base, "version": 1, "manifest": MANIFEST,
                     "files": {path: hashlib.sha256(value).hexdigest() for path, value in metadata(1).items()},
                     "patches": list(PATCHES)}
        self.branch = "main"
        self.new_files = {**self.files, **metadata(1), PATCHES[0]: PATCH.replace(b"+preserved", b"+still preserved")}
        self.candidate()
        self.write_plan()
        summary = {"changed": True, "push": True, "base": self.base, "manifest": MANIFEST, "version": 1,
                   "branch": f"vibeci/update-1-{self.base[:12]}-{MANIFEST[:12]}-0123456789ab"}
        (self.data / "public-summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (self.data / "public-summary.md").write_text("Public upstream maintenance proposal.\n", encoding="utf-8")

    def write_plan(self):
        (self.data / "plan.json").write_text(json.dumps(self.plan), encoding="utf-8")

    def message(self):
        return (f"Raw engine summary\n\nUpstream: /data/input/upstream.git @ v{self.plan['version']} ({self.plan['manifest']})\n"
                f"VibeCI-Upstream: {self.plan['version']}\n").encode()

    def candidate(self, files=None, message=None, parents=None, tree=None):
        self.candidate_tree = tree or self.tree(self.new_files if files is None else files)
        self.raw = self.commit(self.candidate_tree, self.message() if message is None else message,
                               (self.base,) if parents is None else parents)
        self.git(self.mirror, "update-ref", "refs/vibeci/proposed/" + self.branch, self.raw)
        return self.raw

    def prepare(self, **changes):
        kwargs = {"data": self.data, "candidate": self.raw, "base": self.base,
                  "manifest": MANIFEST, "version": 1, "base_branch": self.branch}
        kwargs.update(changes)
        return API["prepare_publication"](**kwargs)

    def refused(self, operation=None):
        self.generic_failure(self.prepare if operation is None else operation)
        self.assertFalse(self.publisher.exists())

    def test_positive_exact_tree_parent_neutral_identity_and_only_expected_objects(self):
        refs_before = self.git(self.mirror, "show-ref")
        with redirect_stdout(io.StringIO()) as stdout, redirect_stderr(io.StringIO()) as stderr:
            publisher, safe = self.prepare()
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertEqual(publisher, self.publisher)
        self.assertNotEqual(safe, self.raw)
        self.assertEqual(self.git(publisher, "rev-parse", safe + "^{tree}").decode().strip(), self.candidate_tree)
        self.assertEqual(self.git(publisher, "rev-list", "--parents", "-n", "1", safe).decode().split(), [safe, self.base])
        message = self.git(publisher, "cat-file", "commit", safe)
        self.assertIn(b"author Maintenance Publisher <publisher@example.invalid> 1700000001 +0000\n", message)
        self.assertIn(b"committer Maintenance Publisher <publisher@example.invalid> 1700000001 +0000\n", message)
        self.assertTrue(message.endswith((f"Update upstream maintenance to 1\n\n"
                                         f"Upstream: /data/input/upstream.git @ v1 ({MANIFEST})\nVibeCI-Upstream: 1\n").encode()))
        self.assertNotIn(b"Raw engine", message)
        expected = set(self.git(self.mirror, "rev-list", "--objects", "--no-object-names", self.base).decode().split())
        expected.add(self.candidate_tree)
        for entry in self.git(self.mirror, "ls-tree", "-r", "-t", "-z", self.candidate_tree).split(b"\0"):
            if entry:
                expected.add(entry.split(b"\t")[0].split()[2].decode())
        actual = set(self.git(publisher, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").decode().split())
        self.assertEqual(actual, expected | {safe})
        self.assertNotIn(self.raw, actual)
        self.assertNotEqual(self.git_result(publisher, "cat-file", "-e", self.raw).returncode, 0)
        self.assertEqual(self.git(self.mirror, "show-ref"), refs_before)
        self.assertFalse((publisher / "objects/info/alternates").exists())
        self.assertFalse((publisher / "commondir").exists())
        self.assertEqual(self.git(publisher, "remote"), b"")
        self.assertNotEqual((publisher / "objects" / self.base[:2] / self.base[2:]).stat().st_ino,
                            (self.mirror / "objects" / self.base[:2] / self.base[2:]).stat().st_ino)

    def test_private_raw_summary_and_identity_are_rewritten_not_rejected_or_copied(self):
        private = (TERM + " " + URL + " " + MODEL + " " + LLM_TOKEN + " " + GH_TOKEN).encode()
        with mock.patch.dict(self.git_env, {"GIT_AUTHOR_NAME": TERM, "GIT_COMMITTER_NAME": TERM}):
            self.candidate(message=private + b"\n" + self.message())
        unreachable = self.object("blob", b"unreachable private data: " + private)
        (self.data / "config.json").write_bytes(private)
        (self.data / "result.json").write_bytes(private)
        publisher, safe = self.prepare()
        ids = self.git(publisher, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").decode().split()
        self.assertNotIn(self.raw, ids)
        self.assertNotIn(unreachable, ids)
        for oid in ids:
            kind = self.git(publisher, "cat-file", "-t", oid).decode().strip()
            payload = self.git(publisher, "cat-file", kind, oid)
            API["scan_environment_bytes"](payload)
            for value in (TERM, URL, HOST, MODEL, LLM_TOKEN, GH_TOKEN):
                self.assertNotIn(value.encode(), payload)
        self.assertNotEqual(safe, self.raw)

    def test_packed_mirror_does_not_copy_private_pack_neighbors(self):
        self.candidate(message=(TERM + " " + GH_TOKEN + "\n").encode() + self.message())
        private_blob = self.object("blob", LLM_TOKEN.encode())
        self.git(self.mirror, "update-ref", "refs/private/no-publish", private_blob)
        self.git(self.mirror, "repack", "-a", "-d")
        self.assertTrue(list((self.mirror / "objects/pack").glob("*.pack")))
        self.assertFalse((self.mirror / "objects" / self.raw[:2] / self.raw[2:]).exists())
        publisher, safe = self.prepare()
        objects = self.git(publisher, "cat-file", "--batch-all-objects", "--batch-check=%(objectname)").decode().split()
        self.assertIn(safe, objects)
        self.assertNotIn(private_blob, objects)
        self.assertNotIn(self.raw, objects)
        self.assertFalse(list((publisher / "objects/pack").glob("*.pack")))

    def test_already_neutral_raw_commit_still_gets_a_new_id_without_copying_raw(self):
        message = (f"Update upstream maintenance to 1\n\nUpstream: /data/input/upstream.git @ v1 ({MANIFEST})\n"
                   "VibeCI-Upstream: 1\n").encode()
        with mock.patch.dict(self.git_env, {"GIT_AUTHOR_NAME": "Maintenance Publisher", "GIT_COMMITTER_NAME": "Maintenance Publisher",
                                           "GIT_AUTHOR_EMAIL": "publisher@example.invalid", "GIT_COMMITTER_EMAIL": "publisher@example.invalid"}):
            self.candidate(message=message)
        publisher, safe = self.prepare()
        self.assertNotEqual(safe, self.raw)
        self.assertNotEqual(self.git_result(publisher, "cat-file", "-e", self.raw).returncode, 0)

    def test_candidate_patch_provider_token_and_binary_leaks_fail_before_publisher_creation(self):
        for index, payload in enumerate((TERM.lower().encode(), URL.encode(), MODEL.upper().encode(),
                                         base64.urlsafe_b64encode(READ_TOKEN.encode()), GH_TOKEN.encode().hex().upper().encode(),
                                         b"\0\xff" + LLM_TOKEN.encode() + b"\xfe\0")):
            with self.subTest(index=index):
                self.candidate(files={**self.new_files, PATCHES[0]: PATCH + payload})
                self.refused()

    def test_private_paths_tree_components_and_branches_are_rejected(self):
        for name in (TERM.swapcase() + "/child.bin", "nested/" + HOST.upper() + "/child.bin",
                     "nested/" + base64.urlsafe_b64encode(MODEL.encode()).decode() + "/file.bin"):
            with self.subTest(name=name):
                self.fixture(base_files={name: b"innocent public bytes"})
                self.refused()
        self.fixture(base_files={"dummy/canary/file.bin": b"innocent"})
        with mock.patch.dict(os.environ, {"VIBECI_PRIVATE_TERMS": json.dumps(["dummy/canary"])}):
            self.refused()
        self.fixture()
        for branch in ("topic/" + TERM, "topic/" + HOST, "topic/" + GH_TOKEN.encode().hex()):
            with self.subTest(branch=branch):
                self.refused(lambda: self.prepare(base_branch=branch))

    def test_base_historical_blobs_paths_and_commit_messages_are_scanned(self):
        for historical_files, message in (({"old.bin": b"\xff" + LLM_TOKEN.encode()}, b"Public prior commit\n"),
                                           ({"removed/" + TERM + ".txt": b"public"}, b"Public prior commit\n"),
                                           ({}, ("Earlier history " + HOST + "\n").encode())):
            self.fixture(ancestor_files=historical_files, ancestor_message=message)
            self.refused()
        self.fixture(base_files={"assets/pixel.bin": b"\xff\x00" + READ_TOKEN.encode()})
        self.refused()

    def test_replace_graft_and_shallow_history_cannot_hide_a_private_ancestor(self):
        for kind in ("replace", "graft", "shallow"):
            self.fixture(ancestor_files={"erased.bin": LLM_TOKEN.encode()})
            if kind == "replace":
                replacement = self.commit(self.base_tree, b"Apparently public root\n")
                self.git(self.mirror, "update-ref", "refs/replace/" + self.base, replacement)
            elif kind == "graft":
                (self.mirror / "info").mkdir(exist_ok=True)
                (self.mirror / "info/grafts").write_text(self.base + "\n", encoding="ascii")
            else:
                (self.mirror / "shallow").write_text(self.base + "\n", encoding="ascii")
            self.refused()

    def test_path_enumeration_has_a_shared_history_budget(self):
        files = {f"many/file-{number}.txt": b"shared public bytes" for number in range(24)}
        self.fixture(base_files=files)
        with mock.patch.dict(GLOBALS, {"MAX_OBJECTS": 50}):
            self.refused()

    def test_public_evidence_and_proposal_branch_are_scanned_not_sanitized(self):
        original_plan = json.dumps(self.plan).encode()
        for name in ("public-summary.md", "public-summary.json", "plan.json"):
            path = self.data / name
            original = path.read_bytes()
            values = (TERM.encode(), base64.b64encode(GH_TOKEN.encode()))
            for value in values:
                path.write_bytes(original + b"\n" + value)
                self.refused()
            path.write_bytes(original)
        path = self.data / "public-summary.json"
        summary = json.loads(path.read_bytes())
        summary["branch"] = "topic/" + HOST.upper()
        path.write_text(json.dumps(summary), encoding="utf-8")
        self.refused()
        (self.data / "plan.json").write_bytes(original_plan)

    def test_public_json_must_be_unambiguous_utf8_object(self):
        path = self.data / "public-summary.json"
        for value in (b"[]", b"{", b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":1e999}',
                      b"\xff", '{"value":"public"}'.encode("utf-16")):
            path.write_bytes(value)
            self.refused()

    def test_exact_base_parent_proposed_ref_and_raw_message_binding(self):
        other = self.commit(self.base_tree, b"Unplanned intermediate commit\n", (self.base,))
        for parents in ((), (other,), (self.base, other), (other, self.base)):
            self.candidate(parents=parents)
            self.refused()
        self.candidate()
        self.git(self.mirror, "update-ref", "refs/vibeci/proposed/main", self.base)
        self.refused()
        self.git(self.mirror, "update-ref", "-d", "refs/vibeci/proposed/main")
        self.refused()
        message = self.message()
        lines = message.decode().splitlines()
        invalid = [message.replace(MANIFEST.encode(), b"f" * 40), message.replace(b"@ v1", b"@ v2"),
                   message.replace(b"VibeCI-Upstream: 1", b"VibeCI-Upstream: 2"),
                   message.replace(b"/data/input/upstream.git", b"/other/input.git")]
        for line in (lines[-2], lines[-1]):
            invalid.extend((message.replace(line.encode(), b""), message + line.encode() + b"\n"))
        for value in invalid:
            self.candidate(message=value)
            self.refused()

    def test_argument_and_plan_shapes_and_bindings_fail_closed(self):
        for key, values in {"candidate": [None, "a" * 39, "A" * 40, "0" * 40, self.base],
                            "base": ["f" * 40], "manifest": ["f" * 40],
                            "version": [True, "1", 0, 2, 1 << 63],
                            "base_branch": ["-option", "a..b", "part/.hidden", "x.lock", "main\n"]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    self.refused(lambda: self.prepare(**{key: value}))
        original = copy.deepcopy(self.plan)
        invalid = [{}, {**original, "extra": True}, {**original, "files": {}}, {**original, "version": True},
                   {**original, "patches": PATCHES[:-1]}, {**original, "patches": PATCHES + [PATCHES[0]]},
                   {**original, "patches": list(reversed(PATCHES))}, {**original, "manifest": "f" * 40},
                   {**original, "patches": ["patches/server/../secret.patch", *PATCHES[1:]]}]
        for plan in invalid:
            self.plan = plan
            self.write_plan()
            self.refused()
        duplicate = json.dumps(original)[:-1] + ',"base":' + json.dumps(self.base) + "}"
        (self.data / "plan.json").write_text(duplicate, encoding="utf-8")
        self.refused()

    def test_metadata_hashes_modes_versions_and_protected_files(self):
        for path in metadata(1):
            for change in (b"incorrect planned bytes\n", ("100755", metadata(1)[path]), ("120000", metadata(1)[path])):
                self.candidate(files={**self.new_files, path: change})
                self.refused()
        for path in ("README.md", "maintenance/verify.py", "new-protected.txt"):
            self.candidate(files={**self.new_files, path: b"unapproved change\n"})
            self.refused()
        for current in (b"01\n", b"1", b"2\n", b"1\r\n"):
            self.plan["files"]["maintenance/upstream-version"] = hashlib.sha256(current).hexdigest()
            self.write_plan()
            self.candidate(files={**self.new_files, "maintenance/upstream-version": current})
            self.refused()
        self.fixture(base_files={"maintenance/upstream-version": b"00\n"})
        self.refused()

    def test_patch_inventory_additions_drops_renames_and_modes_are_forbidden(self):
        for change in ("add", "drop", "rename", "executable", "symlink"):
            files = dict(self.new_files)
            if change in {"drop", "rename"}:
                del files[PATCHES[0]]
            if change in {"add", "rename"}:
                files["patches/server/0002-extra.patch"] = PATCH
            if change in {"executable", "symlink"}:
                files[PATCHES[0]] = ("100755" if change == "executable" else "120000", PATCH)
            self.candidate(files=files)
            self.refused()
        self.candidate(files={**self.new_files, "patches/server/0002-extra.patch": PATCH})
        self.plan["patches"].insert(1, "patches/server/0002-extra.patch")
        self.write_plan()
        self.refused()

    def test_existing_executable_patch_modes_and_unchanged_patches_are_allowed(self):
        self.fixture(base_files={PATCHES[1]: ("100755", PATCH)})
        self.candidate(files={**self.files, **metadata(1)})
        publisher, safe = self.prepare()
        self.assertEqual(self.git(publisher, "rev-parse", safe + "^{tree}").decode().strip(), self.candidate_tree)

    def test_symlink_missing_nonregular_and_hardlinked_evidence_is_rejected(self):
        target = self.temp / "outside-canary"
        target.write_bytes(LLM_TOKEN.encode())
        for kind in ("missing", "symlink", "directory", "fifo", "hardlink"):
            self.fixture()
            path = self.data / "public-summary.md"
            path.unlink()
            if kind == "symlink":
                path.symlink_to(target)
            elif kind == "directory":
                path.mkdir()
            elif kind == "fifo":
                os.mkfifo(path)
            elif kind == "hardlink":
                os.link(target, path)
            self.refused()

    def test_symlink_runtime_paths_and_source_objects_are_rejected(self):
        for kind in ("data", "mirrors", "mirror", "object"):
            self.fixture()
            path = {"data": self.data, "mirrors": self.data / "mirrors", "mirror": self.mirror,
                    "object": self.mirror / "objects" / self.raw[:2] / self.raw[2:]}[kind]
            target = self.run / "moved-canary"
            path.rename(target)
            path.symlink_to(target, target_is_directory=target.is_dir())
            self.refused()

    def test_existing_or_symlink_publisher_is_never_reused_or_removed(self):
        for kind in ("directory", "file", "symlink", "dangling"):
            self.fixture()
            target = self.run / "unrelated"
            target.mkdir()
            marker = target / "keep"
            marker.write_bytes(b"preserve this unrelated fixture")
            if kind == "directory":
                self.publisher.mkdir()
            elif kind == "file":
                self.publisher.write_bytes(b"preserve existing file")
            else:
                self.publisher.symlink_to(target if kind == "symlink" else self.run / "missing")
            self.generic_failure(self.prepare)
            self.assertTrue(self.publisher.exists() or self.publisher.is_symlink())
            self.assertEqual(marker.read_bytes(), b"preserve this unrelated fixture")

    def test_missing_objects_gitlinks_and_wrong_object_types_fail_closed(self):
        missing = self.git(self.mirror, "rev-parse", self.raw + ":" + PATCHES[0]).decode().strip()
        (self.mirror / "objects" / missing[:2] / missing[2:]).unlink()
        self.refused()
        for mode, kind in ((b"160000", "commit"), (b"40000", "blob")):
            self.fixture()
            oid = self.object(kind, b"not a tree\n") if kind == "blob" else self.base
            tree = self.object("tree", mode + b" unsupported\0" + bytes.fromhex(oid))
            self.candidate(tree=tree)
            self.refused()

    def test_oversized_objects_and_total_object_budgets_fail_closed(self):
        self.candidate(files={**self.new_files, PATCHES[0]: b"x" * ((16 << 20) + 1)})
        self.refused()
        self.candidate()
        for limits in ({"MAX_OBJECTS": 8}, {"MAX_TOTAL_BYTES": 256}):
            with mock.patch.dict(GLOBALS, limits):
                self.refused()
        with self.assertRaises(PrivacyError):
            API["_git"](self.mirror, "cat-file", "--batch-all-objects", "--batch-check", limit=32)

    def test_alternates_and_linked_object_stores_are_refused(self):
        for name in ("commondir", "objects/info/alternates", "objects/info/http-alternates"):
            self.fixture()
            path = self.mirror / name
            path.parent.mkdir(exist_ok=True)
            path.write_text(str(self.temp / "never-read") + "\n", encoding="utf-8")
            self.refused()

    def test_local_config_includes_are_rejected_before_git_can_read_them(self):
        target = self.temp / "unread-private-config"
        target.write_text(LLM_TOKEN, encoding="utf-8")
        for section in ('[include]', '[InClUdEiF "gitdir:**"]', '[in\\\nclude]', '\ufeff \t[Include]'):
            self.fixture()
            config = self.mirror / "config"
            config.write_text(section + "\n path = " + str(target) + "\n" + config.read_text(encoding="utf-8"), encoding="utf-8")
            operation = mock.Mock(side_effect=AssertionError("Git must not see an include"))
            with mock.patch.dict(GLOBALS, {"_git": operation}):
                self.refused()
            operation.assert_not_called()
        self.fixture()
        (self.mirror / "config.worktree").write_text("[include]\n path = " + str(target) + "\n", encoding="utf-8")
        with (self.mirror / "config").open("a", encoding="utf-8") as output:
            output.write("[extensions]\n worktreeConfig = true\n")
        operation = mock.Mock(side_effect=AssertionError("Git must not see secondary config"))
        with mock.patch.dict(GLOBALS, {"_git": operation}):
            self.refused()
        operation.assert_not_called()

    def test_real_git_config_errors_never_escape_to_diagnostics(self):
        (self.mirror / "config").write_text("[" + TERM + " " + LLM_TOKEN + "\n", encoding="utf-8")
        self.refused()

    def test_private_subprocess_errors_and_corrupt_headers_are_generic(self):
        for error in (OSError(LLM_TOKEN), subprocess.TimeoutExpired([GH_TOKEN], 1, output=TERM, stderr=HOST),
                      subprocess.CalledProcessError(1, [READ_TOKEN], output=MODEL, stderr=LLM_TOKEN)):
            with mock.patch.dict(GLOBALS, {"_git": mock.Mock(side_effect=error)}):
                self.refused()
        raw = self.git(self.mirror, "cat-file", "commit", self.raw)
        reordered = raw.replace(b"parent " + self.base.encode() + b"\n", b"").replace(
            b"\n\n", b"\nparent " + self.base.encode() + b"\n\n", 1)
        for corrupted in (raw.replace(b"tree ", b"unknown ", 1), raw.replace(b"1700000000", b"invalid-time"),
                          raw.replace(b"\nauthor ", b"\ntree " + self.candidate_tree.encode() + b"\nauthor ", 1), reordered):
            oid = self.git(self.mirror, "hash-object", "--literally", "-t", "commit", "-w", "--stdin", data=corrupted).decode().strip()
            (self.mirror / "refs/vibeci/proposed/main").write_text(oid + "\n", encoding="ascii")
            self.raw = oid
            self.refused()

    def test_git_has_no_inherited_secret_config_tracing_or_credentials(self):
        os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="credential.helper", GIT_CONFIG_VALUE_0=LLM_TOKEN,
                          GIT_TRACE=GH_TOKEN, GIT_OBJECT_DIRECTORY=READ_TOKEN, GIT_ALTERNATE_OBJECT_DIRECTORIES=TERM,
                          SSH_AUTH_SOCK=LLM_TOKEN, HTTPS_PROXY=GH_TOKEN)
        original = subprocess.Popen
        calls = []

        def inspect(command, **kwargs):
            calls.append((command, kwargs))
            env = kwargs["env"]
            self.assertFalse(set(ENV) - {"PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR"} & env.keys())
            for name in ("GIT_CONFIG_COUNT", "GIT_TRACE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
                         "SSH_AUTH_SOCK", "HTTPS_PROXY"):
                self.assertNotIn(name, env)
            self.assertEqual(env["HOME"], os.devnull)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
            self.assertEqual(env["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertEqual(env["GIT_NO_LAZY_FETCH"], "1")
            self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "")
            self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
            for setting in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "credential.helper=", "protocol.allow=never"):
                self.assertIn(setting, command)
            self.assertEqual(kwargs["stdout"], subprocess.PIPE)
            self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
            if "commit-tree" in command:
                self.assertIn("--git-dir=" + str(self.publisher), command)
            self.assertNotIn("checkout", command)
            self.assertNotIn("push", command)
            return original(command, **kwargs)

        with mock.patch("subprocess.Popen", side_effect=inspect):
            self.prepare()
        self.assertGreater(len(calls), 10)

    def test_candidate_code_hooks_filters_and_helpers_are_never_executed(self):
        marker = self.temp / "must-not-exist"
        script = self.temp / "malicious-hook"
        script.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\nexit 1\n', encoding="utf-8")
        script.chmod(0o755)
        hooks = self.mirror / "hooks"
        hooks.mkdir()
        for name in ("pre-commit", "post-commit", "reference-transaction"):
            path = hooks / name
            path.write_bytes(script.read_bytes())
            path.chmod(0o755)
        config = self.mirror / "config"
        with config.open("a", encoding="utf-8") as output:
            output.write(f'\n[core]\n fsmonitor = {script}\n hooksPath = {hooks}\n'
                         f'[credential]\n helper = !{script}\n[filter "canary"]\n process = {script}\n'
                         f'[diff]\n external = {script}\n')
        self.prepare()
        self.assertFalse(marker.exists())

    def test_final_rescan_detects_a_mutated_written_object(self):
        real_git = GLOBALS["_git"]

        def corrupt(repository, *args, **kwargs):
            result = real_git(repository, *args, **kwargs)
            if args[0] == "commit-tree":
                oid = result.decode().strip()
                (repository / "objects" / oid[:2] / oid[2:]).write_bytes(b"corrupt " + LLM_TOKEN.encode())
            return result

        with mock.patch.dict(GLOBALS, {"_git": corrupt}):
            self.generic_failure(self.prepare)
        self.assertTrue(self.publisher.is_dir())

    def test_check_cli_is_silent_on_success_and_generic_on_every_failure(self):
        public = self.run / "public.bin"
        public.write_bytes(b"public binary\0\xff")

        def cli(*args, env=None):
            return subprocess.run([sys.executable, "-B", str(SCRIPT), *args], cwd=self.run,
                                  env=ENV if env is None else env, capture_output=True, timeout=20, check=False)

        result = cli("check", str(public), str(self.data / "plan.json"))
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        private = self.run / "private-canary.bin"
        private.write_bytes(b"\xff" + GH_TOKEN.encode())
        link = self.run / "linked.bin"
        link.symlink_to(public)
        named = self.run / (TERM + ".bin")
        named.write_bytes(b"public bytes")
        oversized = self.run / "oversized.bin"
        with oversized.open("wb") as output:
            output.truncate((16 << 20) + 1)
        for args in ((), ("check",), (LLM_TOKEN,), ("check", str(private)), ("check", str(link)),
                     ("check", str(named)), ("check", str(oversized)),
                     ("check", str(self.run / (READ_TOKEN.encode().hex() + ".missing")))):
            result = cli(*args)
            self.assertEqual((result.returncode, result.stdout, result.stderr),
                             (1, b"", b"Publication privacy check failed.\n"))
        result = cli("check", str(public), env={"PATH": os.defpath})
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (1, b"", b"Publication privacy check failed.\n"))


if __name__ == "__main__":
    unittest.main()
