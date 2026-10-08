"""Offline candidate-guard tests; VIBECI_BINARY enables a static engine contract check."""

import base64
from contextlib import redirect_stdout
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
VERIFIER = ROOT / "maintenance/verify.py"
VERIFIER_BYTES = VERIFIER.read_bytes()
DESCRIPTION = (ROOT / "maintenance/intent.md").read_text(encoding="utf-8")
COMPONENTS = ("server", "calls", "transcriber")
PATCHES = [f"patches/{component}/0001-feature.patch" for component in COMPONENTS]
SELECTOR = "maintenance/upstream-version"
MANIFEST = "e" * 40


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = load_module("maintenance_verify", VERIFIER)


def metadata(number):
    image = f"docker.io/mattermost/mattermost-team-edition:11.11.{number + 1}@sha256:" + str(number + 1) * 64
    pins = {
        "SERVER_TAG": f"v11.11.{number + 1}", "SERVER_COMMIT": str(number + 1) * 40, "SERVER_IMAGE": image,
        "CALLS_TAG": f"v1.12.{number + 3}", "CALLS_COMMIT": str(number + 3) * 40,
        "RECORDER_TAG": "v0.8.13", "RECORDER_SOURCE_IMAGE": "docker.io/mattermost/calls-recorder:v0.8.13@sha256:" + "a" * 64,
        "OFFLOADER_TAG": "v0.9.6", "OFFLOADER_COMMIT": "7" * 40,
        "TRANSCRIBER_TAG": str(number + 5) * 40, "TRANSCRIBER_BRANCH": "master",
        "GO_VERSION": "1.26.7", "NODE_VERSION": "24", "CALLS_VERSION": f"1000.12.{99 + number}",
    }
    return {
        SELECTOR: f"{number}\n".encode(),
        "upstream.env": ("# Trusted pins.\n" + "".join(f"{key}={value}\n" for key, value in pins.items())).encode(),
        "Containerfile": f"ARG SERVER_IMAGE={image}\nFROM ${{SERVER_IMAGE}}\n".encode(),
    }


OLD, NEW = metadata(0), metadata(1)
PLAN = {"base": "a" * 40, "version": 1, "manifest": MANIFEST,
        "files": {path: hashlib.sha256(data).hexdigest() for path, data in NEW.items()}, "patches": PATCHES}


def encode_plan(plan):
    return base64.urlsafe_b64encode(json.dumps(plan, separators=(",", ":")).encode()).decode()


def setUpModule():
    network = mock.patch("socket.create_connection", side_effect=AssertionError("network is forbidden in verifier tests"))
    network.start()
    unittest.addModuleCleanup(network.stop)


class DecodePlanTests(unittest.TestCase):
    def test_exact_binding_round_trip_and_integer_limit(self):
        self.assertEqual(set(PLAN), {"base", "version", "manifest", "files", "patches"})
        for number in (1, (1 << 63) - 1):
            plan = dict(PLAN, version=number)
            with self.subTest(version=number):
                self.assertEqual(guard.decode_plan(encode_plan(plan)), plan)

    def test_invalid_encoding_json_and_size_are_rejected(self):
        invalid = {"empty": "", "alphabet": "!not-base64!", "padding": "e30", "nonascii": "\u2603",
                   "trailing junk": encode_plan(PLAN) + "!", "whitespace": encode_plan(PLAN) + "\n",
                   "oversized": "A" * 262145}
        for label, raw in (("utf8", b"\xff"), ("syntax", b"{"), ("array", b"[]"), ("null", b"null"),
                           ("scalar", b"1"), ("string", b'"plan"'), ("trailing JSON", b"{}{}"),
                           ("recursion", b"[" * 2000 + b"0" + b"]" * 2000)):
            invalid[label] = base64.urlsafe_b64encode(raw).decode()
        for label, encoded in invalid.items():
            with self.subTest(case=label), self.assertRaises(guard.Rejected):
                guard.decode_plan(encoded)

    def test_duplicate_keys_at_both_object_levels_are_rejected(self):
        document = json.dumps(PLAN, separators=(",", ":"))
        duplicates = [document[:-1] + "," + json.dumps(key) + ":" + json.dumps(value) + "}"
                      for key, value in PLAN.items()]
        for path, digest in PLAN["files"].items():
            pair = json.dumps(path) + ":" + json.dumps(digest)
            duplicates.append(document.replace(pair, pair + "," + pair, 1))
        for index, raw in enumerate(duplicates):
            with self.subTest(duplicate=index), self.assertRaises(guard.Rejected):
                guard.decode_plan(base64.urlsafe_b64encode(raw.encode()).decode())

    def test_top_level_and_metadata_fields_are_exact(self):
        invalid = [{key: value for key, value in PLAN.items() if key != missing} for missing in PLAN]
        invalid.append(dict(PLAN, extra=True))
        for missing in PLAN["files"]:
            invalid.append(dict(PLAN, files={key: value for key, value in PLAN["files"].items() if key != missing}))
        invalid.append(dict(PLAN, files={**PLAN["files"], "maintenance/intent.md": "a" * 64}))
        invalid.extend(dict(PLAN, files=value) for value in (None, [], "files", True))
        for plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(guard.Rejected):
                guard.decode_plan(encode_plan(plan))

    def test_version_commit_and_content_hash_validation(self):
        invalid = {
            "version": (None, False, True, "1", 1.0, 0, -1, 1 << 63, float("nan"), float("inf")),
            "base": (None, True, 1, [], {}, "", "0" * 40, "a" * 39, "a" * 41, "A" * 40, "g" * 40, "a" * 40 + "\n"),
            "digest": (None, True, 1, [], {}, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64,
                       "sha256:" + "a" * 64, "a" * 64 + "\n"),
        }
        invalid["manifest"] = invalid["base"]
        for field, values in invalid.items():
            for value in values:
                plan = copy.deepcopy(PLAN)
                if field == "digest":
                    plan["files"]["upstream.env"] = value
                else:
                    plan[field] = value
                with self.subTest(field=field, value=value), self.assertRaises(guard.Rejected):
                    guard.decode_plan(encode_plan(plan))

    def test_patch_inventory_types_duplicates_components_and_paths(self):
        invalid = [None, {}, "patches", [], PATCHES + [PATCHES[0]]]
        invalid.extend([path for path in PATCHES if f"/{component}/" not in path] for component in COMPONENTS)
        for path in (None, 1, [], "/patches/server/a.patch", "../patches/server/a.patch",
                     "patches/server/../a.patch", "patches/server/nested/a.patch", "patches/other/a.patch",
                     "patches/server/.hidden.patch", "patches/server/-option.patch", "patches/server/a b.patch",
                     "patches/server/a.patch.bak", "patches/server/a.PATCH", "patches\\server\\a.patch",
                     "patches/server/a\n.patch", "patches/server/a\0.patch"):
            invalid.append(PATCHES + [path])
        for patches in invalid:
            with self.subTest(patches=patches), self.assertRaises(guard.Rejected):
                guard.decode_plan(encode_plan(dict(PLAN, patches=patches)))


class GitVerifyTests(unittest.TestCase):
    def fixture(self, previous=b"0\n", version=1, current=None):
        temporary = tempfile.TemporaryDirectory(prefix="maintainedmost-verifier-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        self.root = self.temp / "fork"
        self.root.mkdir()
        self.env = {
            "PATH": os.defpath, "HOME": str(self.temp), "XDG_CONFIG_HOME": str(self.temp), "TMPDIR": str(self.temp),
            "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ALLOW_PROTOCOL": "", "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_AUTHOR_NAME": "Verifier Fixture", "GIT_COMMITTER_NAME": "Verifier Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid", "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
        self.git("init", "--initial-branch=main", "--object-format=sha1", "--template=", ".")
        self.protected = {
            ".gitignore": b"ignored/\n",
            ".github/workflows/build.yml": b"name: Protected CI\non: pull_request\n",
            "maintenance/required-tests.json": b'{"server-api":{"example.invalid/app":["TestPreserved"]}}\n',
            "maintenance/intent.md": DESCRIPTION.encode(), "maintenance/verify.py": VERIFIER_BYTES,
        }
        patch = b"--- a/feature.txt\n+++ b/feature.txt\n@@ -1 +1 @@\n-upstream\n+fork behavior\n"
        for path, data in {**OLD, **self.protected, SELECTOR: previous, **dict.fromkeys(PATCHES, patch)}.items():
            self.write(path, data)
        self.base = self.commit(message="Trusted baseline\n", parents=())
        self.expected = {**NEW, SELECTOR: f"{version}\n".encode() if current is None else current}
        self.plan = {"base": self.base, "version": version, "manifest": MANIFEST,
                     "files": {path: hashlib.sha256(data).hexdigest() for path, data in self.expected.items()},
                     "patches": list(PATCHES)}
        for path, data in {**self.expected, **dict.fromkeys(PATCHES, patch.replace(b"-upstream", b"-new upstream"))}.items():
            self.write(path, data)
        self.candidate = self.commit()

    def git(self, *args, data=None):
        result = subprocess.run(
            ["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-c", "core.autocrlf=false",
             "-c", "core.filemode=true", "-c", "core.fsmonitor=false", "-c", "commit.gpgSign=false", *args],
            cwd=self.root, env=self.env, input=data, capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        return result.stdout

    def write(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def message(self):
        return (f"Upstream: /data/input/upstream.git @ v{self.plan['version']} ({self.plan['manifest']})\n\n"
                f"VibeCI-Upstream: {self.plan['version']}\n")

    def commit(self, message=None, parents=None):
        # Plumbing gives each mutation precisely the intended parent(s), without hooks or resets.
        self.git("add", "--all", "--", ".")
        tree = self.git("write-tree").decode().strip()
        parents = (self.base,) if parents is None else parents
        args = [part for parent in parents for part in ("-p", parent)]
        commit = self.git("commit-tree", tree, *args, data=(self.message() if message is None else message).encode()).decode().strip()
        self.git("update-ref", "HEAD", commit)
        return commit

    def verify(self):
        guard.verify(guard.decode_plan(encode_plan(self.plan)), self.root)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(VERIFIER), *args], cwd=self.temp, env=self.env,
                              capture_output=True, text=True, timeout=30, check=False)

    def test_valid_single_commit_passes_api_and_cli_without_writes(self):
        self.fixture()
        self.assertEqual(self.git("rev-list", "--parents", "-n", "1", "HEAD").decode().split(), [self.candidate, self.base])
        self.assertEqual(set(self.git("diff", "--name-only", self.base, "HEAD").decode().splitlines()), set(NEW) | set(PATCHES))
        index = (self.root / ".git/index").read_bytes()
        out = io.StringIO()
        with redirect_stdout(out):
            self.verify()
        result = self.cli(encode_plan(self.plan))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout, out.getvalue())
        self.assertIn("full CI is still required", result.stdout)
        self.assertEqual(self.git("rev-parse", "HEAD").decode().strip(), self.candidate)
        self.assertEqual((self.root / ".git/index").read_bytes(), index)
        self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching"), b"")

    def test_cli_rejections_are_nonzero_without_tracebacks_or_success_output(self):
        self.fixture()
        for args in ((), ("invalid",), (encode_plan(self.plan), "extra")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertTrue(result.stderr.startswith("maintenance verifier: "))
                self.assertNotIn("Traceback", result.stderr)
        self.write("untracked.txt", b"not committed\n")
        result = self.cli(encode_plan(self.plan))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("checkout diverges", result.stderr)

    def test_wrong_missing_and_merge_parents_are_rejected(self):
        self.fixture()
        other = self.commit(message="Unplanned intermediate commit\n")
        for parents in ((), (other,), (self.base, other), (other, self.base)):
            with self.subTest(parents=parents):
                self.commit(parents=parents)
                with self.assertRaisesRegex(guard.Rejected, "exactly the planned base parent"):
                    self.verify()

    def test_manifest_and_version_commit_lines_must_match_exactly_once(self):
        self.fixture()
        message = self.message()
        invalid = [message.replace(MANIFEST, "f" * 40), message.replace("@ v1", "@ v2"),
                   message.replace("VibeCI-Upstream: 1", "VibeCI-Upstream: 2"),
                   message.replace("/data/input/upstream.git", "/data/input/other.git")]
        for line in (message.splitlines()[0], message.splitlines()[-1]):
            invalid.extend((message.replace(line, ""), message.replace(line, " " + line), message + line + "\n"))
        for text in invalid:
            with self.subTest(message=text):
                self.commit(message=text)
                with self.assertRaisesRegex(guard.Rejected, "bind the planned manifest and version"):
                    self.verify()

    def test_workflows_inventory_intent_and_verifier_are_protected(self):
        paths = (".github/workflows/build.yml", "maintenance/required-tests.json", "maintenance/intent.md", "maintenance/verify.py")
        for name in paths:
            for change in ("edit", "delete", "mode"):
                with self.subTest(path=name, change=change):
                    self.fixture()
                    path = self.root / name
                    if change == "edit":
                        self.write(name, self.protected[name] + b"\n")
                    elif change == "delete":
                        path.unlink()
                    else:
                        path.chmod(0o755)
                    self.commit()
                    with self.assertRaisesRegex(guard.Rejected, "protected fork file: " + re.escape(name)):
                        self.verify()
        for name in (".github/workflows/unapproved.yml", "scripts/bypass.py", ".gitignore"):
            with self.subTest(path=name):
                self.fixture()
                self.write(name, b"unapproved change\n")
                self.commit()
                with self.assertRaisesRegex(guard.Rejected, "protected fork file: " + re.escape(name)):
                    self.verify()

    def test_metadata_hashes_bind_exact_committed_bytes(self):
        for name in NEW:
            with self.subTest(path=name):
                self.fixture()
                self.write(name, self.expected[name] + b"\n")
                self.commit()
                with self.assertRaisesRegex(guard.Rejected, "committed metadata does not match the plan: " + re.escape(name)):
                    self.verify()

    def test_metadata_cannot_be_executable_symlinked_or_deleted(self):
        for name in NEW:
            for change in ("executable", "symlink", "delete"):
                with self.subTest(path=name, change=change):
                    self.fixture()
                    path = self.root / name
                    if change == "executable":
                        path.chmod(0o755)
                    else:
                        path.unlink()
                        if change == "symlink":
                            target = self.temp / "same-content"
                            target.write_bytes(self.expected[name])
                            path.symlink_to(target)
                    self.commit()
                    reason = "committed metadata" if change == "executable" else "regular blobs"
                    with self.assertRaisesRegex(guard.Rejected, reason):
                        self.verify()

    def test_patch_deletions_additions_and_renames_are_rejected(self):
        for name in PATCHES:
            for change in ("delete", "add", "rename"):
                with self.subTest(path=name, change=change):
                    self.fixture()
                    path = self.root / name
                    if change == "delete":
                        path.unlink()
                    elif change == "add":
                        path.with_name("0002-extra.patch").write_bytes(path.read_bytes())
                    else:
                        path.rename(path.with_name("0002-renamed.patch"))
                    self.commit()
                    with self.assertRaisesRegex(guard.Rejected, "patch inventory changed"):
                        self.verify()

    def test_patch_modes_and_symlinks_are_rejected(self):
        for name in PATCHES:
            for symlink in (False, True):
                with self.subTest(path=name, symlink=symlink):
                    self.fixture()
                    path = self.root / name
                    if symlink:
                        target = self.temp / "same-patch"
                        target.write_bytes(path.read_bytes())
                        path.unlink()
                        path.symlink_to(target)
                    else:
                        path.chmod(0o755)
                    self.commit()
                    with self.assertRaisesRegex(guard.Rejected, "regular blobs" if symlink else "patch file mode changed"):
                        self.verify()

    def test_plan_inventory_must_match_base_and_component_order_not_just_candidate(self):
        for change in ("new inventory", "reordered"):
            with self.subTest(change=change):
                self.fixture()
                if change == "new inventory":
                    name = "patches/server/0002-extra.patch"
                    self.write(name, (self.root / PATCHES[0]).read_bytes())
                    self.plan["patches"].insert(1, name)
                    self.commit()
                else:
                    self.plan["patches"].reverse()
                with self.assertRaisesRegex(guard.Rejected, "patch inventory changed"):
                    self.verify()

    def test_dirty_staged_ignored_and_untracked_files_are_rejected(self):
        cases = (("unstaged", "upstream.env"), ("unstaged", PATCHES[0]), ("unstaged", "maintenance/intent.md"),
                 ("staged", "Containerfile"), ("index only", "upstream.env"), ("deleted", PATCHES[1]),
                 ("mode", "Containerfile"), ("untracked", "untracked.txt"),
                 ("untracked", "patches/server/0002-extra.patch"), ("ignored", "ignored/output.bin"))
        for change, name in cases:
            with self.subTest(change=change, path=name):
                self.fixture()
                path = self.root / name
                if change == "deleted":
                    path.unlink()
                elif change == "mode":
                    path.chmod(0o755)
                else:
                    original = path.read_bytes() if path.exists() else b""
                    self.write(name, original + b"not committed\n")
                    if change in {"staged", "index only"}:
                        self.git("add", "--", name)
                    if change == "index only":
                        self.write(name, original)
                if change == "ignored":
                    self.assertEqual(self.git("status", "--porcelain=v1", "--untracked-files=all"), b"")
                with self.assertRaisesRegex(guard.Rejected, "checkout diverges"):
                    self.verify()

    def test_version_must_advance_once_even_with_matching_content_hashes(self):
        cases = [(b"0\n", 2, b"2\n"), (b"1\n", 1, b"1\n"), (b"2\n", 1, b"1\n")]
        cases += [(previous, 1, b"1\n") for previous in (b"00\n", b"0", b"-1\n", b"0\r\n", b"0\n0\n", b"9" * 20 + b"\n")]
        cases += [(b"0\n", 1, current) for current in (b"01\n", b"1", b"1\r\n", b"1\n\n", b"2\n", b"0\n")]
        for previous, version, current in cases:
            with self.subTest(previous=previous, version=version, current=current):
                self.fixture(previous, version, current)
                with self.assertRaisesRegex(guard.Rejected, "advance by exactly one"):
                    self.verify()

    def test_later_versions_and_maximum_valid_increment_pass(self):
        for version in (2, (1 << 63) - 1):
            with self.subTest(version=version):
                self.fixture(f"{version - 1}\n".encode(), version)
                with redirect_stdout(io.StringIO()):
                    self.verify()

    def test_checkout_root_cannot_be_missing_a_file_or_a_symlink(self):
        self.fixture()
        link = self.temp / "linked-fork"
        link.symlink_to(self.root, target_is_directory=True)
        for root in (self.temp / "missing", self.root / "upstream.env", link):
            with self.subTest(root=root), self.assertRaisesRegex(guard.Rejected, "real checkout directory"):
                guard.verify(self.plan, root)

    @unittest.skipUnless(os.environ.get("VIBECI_BINARY"), "VIBECI_BINARY is unset; optional real-engine config contract skipped")
    def test_real_engine_static_config_contract_without_credentials(self):
        self.fixture()
        binary = Path(os.environ["VIBECI_BINARY"]).expanduser().resolve()
        self.assertTrue(binary.is_file() and os.access(binary, os.X_OK), "VIBECI_BINARY must name an executable file")
        planner = load_module("vibeci_plan_contract", ROOT / "scripts/vibeci-plan.py")
        models = {
            "providers": {"offline": {"type": "openai-responses", "base_url": "https://models.example.invalid/v1",
                                      "api_key": "file:/run/secrets/llm_api_key"}},
            "models": {"strong": {"provider": "offline", "model": "fixture-model"}},
            "roles": {"audit": "strong", "resolve": ["strong"]},
        }
        branch = f"vibeci/update-1-{self.base[:12]}-{MANIFEST[:12]}"
        replacements = {path: self.expected[path].decode() for path in ("upstream.env", "Containerfile")}
        config = planner.build_config("test-owner/maintainedmost", "main", branch, models, self.plan,
                                      replacements, DESCRIPTION, VERIFIER_BYTES, push=False)
        self.assertEqual(config["providers"]["provider-1"]["api_key"], "file:/run/secrets/llm_api_key")
        self.assertEqual(config["repos"][0]["fork"]["auth"], {"token": "file:/run/secrets/fork_read_token"})
        path = self.temp / "config.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        # Only the static config command is allowed, with no inherited credentials or user config.
        env = {key: self.env[key] for key in ("PATH", "HOME", "XDG_CONFIG_HOME", "TMPDIR", "LANG", "LC_ALL")}
        result = subprocess.run([str(binary), "config", "-config", str(path)], cwd=self.temp, env=env,
                                capture_output=True, text=True, timeout=30, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = json.loads(result.stdout)
        self.assertEqual(actual["data_dir"], "/data")
        self.assertEqual(actual["sandbox"]["mode"], "broker")
        self.assertEqual(actual["sandbox"]["socket"], "/run/vibeci/sandboxd.sock")
        self.assertEqual(len(actual["repos"]), 1)
        repo = actual["repos"][0]
        self.assertEqual(repo["description"], DESCRIPTION)
        self.assertEqual(repo["push"], {"mode": "branch", "branch": branch, "dry_run": True})
        self.assertEqual(repo["sandbox"]["profile"], "maintainedmost")
        self.assertEqual(repo["sandbox"]["verify_profile"], "maintainedmost")
        self.assertEqual(repo["upstream"]["url"], "/data/input/upstream.git")
        self.assertEqual(repo["upstream"]["tags"], "v1")
        patches = repo["patches"]
        self.assertEqual(patches["version_file"], SELECTOR)
        self.assertEqual(patches["update_files"], replacements)
        self.assertEqual(patches["sets"], [{"glob": f"patches/{component}/*.patch", "root": component, "strip": 1}
                                           for component in COMPONENTS])
        self.assertEqual(len(patches["sources"]), 3)
        for source, component, repository, key in zip(
                patches["sources"], COMPONENTS,
                ("mattermost", "mattermost-plugin-calls", "calls-transcriber"),
                ("SERVER_COMMIT", "CALLS_COMMIT", "TRANSCRIBER_TAG")):
            with self.subTest(component=component):
                self.assertEqual(source["path"], component)
                self.assertEqual(source["url"], f"https://github.com/mattermost/{repository}.git")
                self.assertEqual(source["fetch"], "partial")
                self.assertEqual(source["revision_file"], "upstream.env")
                self.assertEqual(source["revision_regex"], rf"(?m)^{key}=([0-9a-f]{{40}})$")
                self.assertRegex(re.search(source["revision_regex"], replacements["upstream.env"])[1], r"^[0-9a-f]{40}$")
        self.assertEqual(len(repo["verify"]), 1)
        command = repo["verify"][0]["run"]
        self.assertIn(hashlib.sha256(VERIFIER_BYTES).hexdigest() + "  /opt/maintainedmost/verify.py", command)
        self.assertEqual(guard.decode_plan(shlex.split(command)[-1]), self.plan)


if __name__ == "__main__":
    unittest.main()
