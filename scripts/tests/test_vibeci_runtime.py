"""Offline private-runtime canaries: fake planning/processes, no real credentials."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("vibeci_runtime", ROOT / "scripts/vibeci-runtime.py")
runtime = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runtime)
PLANNER = runtime.module("vibeci-plan.py")
PRIVATE = runtime.module("vibeci-private.py")
GITHUB = runtime.module("vibeci-github.py")

HOST = "private-provider.invalid"
MODEL = "token-canary-model"
TERM = "token-canary-private-term"
READ_KEY = "token-canary-read-" + "r" * 96
LLM_KEY = "token-canary-api-" + "a" * 96
WRITE_KEY = "token-canary-write-" + "w" * 96
BUILTIN_KEY = "token-canary-builtin-" + "b" * 96
CANARIES = (HOST, MODEL, TERM, READ_KEY, LLM_KEY, WRITE_KEY, BUILTIN_KEY)
REFLECTION = " ".join(CANARIES)
MODELS = {
    "providers": {"provider": {"type": "openai-responses", "base_url": "https://" + HOST + "/v1",
                                "api_key": "file:/run/secrets/llm_api_key"}},
    "models": {"model": {"provider": "provider", "model": MODEL, "max_tokens": 2048}},
    "roles": {"audit": "model", "resolve": ["model"]},
}
OWNER = b"maintainedmost-vibeci\n"
SUCCESS = b'[{"repo":"maintainedmost","status":"dry-run"}]\n'
PUBLIC_NAMES = {"summary.md", "summary.json", "plan.json", "result.json"}


def setUpModule():
    for target in ("subprocess.run", "subprocess.Popen", "socket.socket", "socket.create_connection",
                   "socket.getaddrinfo"):
        guard = mock.patch(target, side_effect=AssertionError("unmocked process/network access is forbidden"))
        guard.start()
        unittest.addModuleCleanup(guard.stop)


class RuntimeCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-canaries-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        self.checkout, self.runner = self.temp / "checkout", self.temp / "runner"
        self.checkout.mkdir()
        self.runner.mkdir()
        self.env = {
            "PATH": os.defpath, "HOME": str(self.temp), "RUNNER_TEMP": str(self.runner),
            "GITHUB_WORKSPACE": str(self.checkout), "GITHUB_REPOSITORY": "test-owner/test-project",
            "BASE_BRANCH": "main", "COMPOSE_PROJECT_NAME": "maintainedmost-vibeci-test-1",
            "VIBECI_IMAGE": "registry.example.invalid/engine@sha256:" + "a" * 64,
            "VIBECI_SANDBOX_IMAGE": "registry.example.invalid/sandbox@sha256:" + "b" * 64,
            "VIBECI_MODELS": json.dumps(MODELS), "VIBECI_PRIVATE_TERMS": json.dumps([TERM]),
            "VIBECI_LLM_API_KEY": LLM_KEY, "VIBECI_FORK_READ_TOKEN": READ_KEY,
            "GH_TOKEN": WRITE_KEY, "VIBECI_GIT_TOKEN": WRITE_KEY, "GITHUB_TOKEN": BUILTIN_KEY,
            "GITHUB_ENV": str(self.temp / "github-env"), "GITHUB_OUTPUT": str(self.temp / "github-output"),
            "GITHUB_STEP_SUMMARY": str(self.temp / "github-summary"),
        }
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.changed = True
        self.records = {
            "summary.md": b"# Maintenance Plan\n\nTrusted public metadata.\n",
            "summary.json": b'{"changed":true,"version":2}\n',
            "plan.json": b'{"base":"' + b"1" * 40 + b'","version":2}\n',
        }
        self.plan = mock.Mock(side_effect=self.fake_plan)
        self.parse_models = mock.Mock(wraps=PLANNER["parse_models"])
        modules = {"vibeci-private.py": PRIVATE, "vibeci-github.py": GITHUB,
                   "vibeci-plan.py": {**PLANNER, "plan": self.plan, "parse_models": self.parse_models}}
        loader = mock.patch.object(runtime, "module", side_effect=modules.__getitem__)
        loader.start()
        self.addCleanup(loader.stop)

    def fake_plan(self, checkout, repository, branch, data, models_file, *, push):
        data.mkdir(mode=0o700)
        for name, payload in self.records.items():
            if name != "plan.json" or self.changed:
                runtime.write_private(data / name, payload)
        if self.changed:
            config = PLANNER["build_config"](repository, branch, "vibeci/update-test",
                                              PLANNER["parse_models"](models_file.read_bytes()),
                                              {"version": 2}, {}, "Preserve fork obligations.",
                                              b"# Offline verifier fixture.\n", push)
            runtime.write_private(data / "config.json", json.dumps(config).encode())
        return {"changed": self.changed}

    def assert_public(self, *payloads):
        for payload in payloads:
            raw = payload.encode() if isinstance(payload, str) else payload
            self.assertFalse(any(value.encode().lower() in raw.lower() for value in CANARIES),
                             "private canary reached a public sink")

    def invoke(self, operation, expected=0):
        stdout, stderr = io.StringIO(), io.StringIO()
        escaped, code = False, None
        with mock.patch.object(sys, "argv", ["vibeci-runtime.py", operation]), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = runtime.main()
            except SystemExit as error:
                code = error.code
            except Exception:
                escaped = True
        self.assertFalse(escaped, "runtime exception escaped its generic diagnostic boundary")
        self.assert_public(stdout.getvalue(), stderr.getvalue())
        self.assertEqual(code, expected)
        self.assertTrue(stdout.getvalue() == "", "runtime stdout was not empty")
        self.assertTrue(stderr.getvalue() == (runtime.FAILURE + "\n" if expected else ""),
                        "runtime stderr was not the generic failure")
        for key in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            path = Path(os.environ[key])
            if path.exists():
                self.assert_public(path.read_bytes())

    def own_root(self):
        root = Path(tempfile.mkdtemp(prefix="maintainedmost-vibeci-", dir=self.runner))
        runtime.write_private(root / ".runtime-owner", OWNER)
        (root / "data").mkdir(mode=0o700)
        (root / "public-inputs").mkdir(mode=0o700)
        for name, payload in self.records.items():
            runtime.write_private(root / "public-inputs" / name, payload)
        os.environ["VIBECI_RUN_DIR"] = str(root)
        for key in ("GITHUB_ENV", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            self.env[key] = str(self.temp / (root.name + "-" + key.lower()))
            os.environ[key] = self.env[key]
        return root

    def prepared_root(self):
        self.invoke("prepare")
        outputs = dict(line.split("=", 1) for line in Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines())
        root = Path(outputs["run_dir"])
        os.environ["VIBECI_RUN_DIR"] = str(root)
        return root

    def assert_credentials_gone(self, root):
        for name in ("llm_api_key", "fork_read_token"):
            path = root / "secrets" / name
            self.assertFalse(path.exists() or path.is_symlink(), "a credential backing file survived")

    def assert_no_evidence(self, root):
        self.assertFalse((root / "artifacts").exists(), "publishable artifacts were created before screening")
        for key in ("GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            self.assertFalse(Path(self.env[key]).exists(), "a public sink was written before screening")


class PrivateIOTests(RuntimeCase):
    def test_private_files_are_exclusive_and_owner_only_even_with_permissive_umask(self):
        path = self.temp / "private"
        previous = os.umask(0)
        try:
            runtime.write_private(path, LLM_KEY.encode())
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertTrue(runtime.read_private(path) == LLM_KEY.encode(), "private round trip failed")
        with self.assertRaises(FileExistsError):
            runtime.write_private(path, b"replacement")
        self.assertTrue(path.read_bytes() == LLM_KEY.encode(), "exclusive write changed an existing file")

    def test_private_reads_reject_links_nonregular_files_and_exhausted_bounds(self):
        source = self.temp / "source"
        source.write_bytes(READ_KEY.encode())
        paths = [self.temp / name for name in ("symlink", "dangling", "hardlink", "directory", "fifo", "large")]
        paths[0].symlink_to(source)
        paths[1].symlink_to(self.temp / "missing")
        os.link(source, paths[2])
        paths[3].mkdir()
        os.mkfifo(paths[4])
        with paths[5].open("wb") as stream:
            stream.truncate(runtime.MAX_BYTES + 1)
        self.assertEqual(runtime.MAX_BYTES, 8 << 20)
        for index, path in enumerate(paths):
            with self.subTest(case=index), self.assertRaises((ValueError, OSError)):
                runtime.read_private(path)
        boundary = self.temp / "boundary"
        boundary.write_bytes(b"x" * 128)
        with mock.patch.object(runtime, "MAX_BYTES", 128):
            self.assertEqual(len(runtime.read_private(boundary)), 128)
            boundary.write_bytes(b"x" * 129)
            with self.assertRaises(ValueError):
                runtime.read_private(boundary)

    def test_unknown_operation_is_rejected_without_echoing_its_argument(self):
        self.invoke(TERM, 1)


class PrepareTests(RuntimeCase):
    def test_prepare_uses_unique_private_external_roots_and_seals_only_public_records(self):
        previous = os.umask(0)
        try:
            root = self.prepared_root()
        finally:
            os.umask(previous)
        self.assertEqual(root.parent, self.runner)
        self.assertNotIn(self.checkout, root.parents)
        for directory in (root, root / "data", root / "public-inputs"):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
        for path in (root / ".runtime-owner", root / "models.json",
                      root / "data/config.json", *(root / "public-inputs").iterdir()):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(path.stat().st_nlink, 1)
        self.assertEqual(stat.S_IMODE((root / "sandboxd.json").stat().st_mode), 0o444)
        self.assertTrue((root / "models.json").read_text() == self.env["VIBECI_MODELS"], "models changed")
        self.assertEqual({path.name for path in (root / "public-inputs").iterdir()}, set(self.records))
        for name, payload in self.records.items():
            self.assertTrue((root / "public-inputs" / name).read_bytes() == payload, "sealed metadata changed")
            self.assertNotEqual((root / "public-inputs" / name).stat().st_ino, (root / "data" / name).stat().st_ino)
        self.assertEqual(Path(self.env["GITHUB_ENV"]).read_text(), "VIBECI_RUN_DIR=" + str(root) + "\n")
        self.assertEqual(Path(self.env["GITHUB_OUTPUT"]).read_text(), "run_dir=" + str(root) + "\nchanged=true\n")
        self.assertEqual(self.plan.call_args.args, (self.checkout, "test-owner/test-project", "main",
                                                     root / "data", root / "models.json"))
        self.assertTrue(self.parse_models.called)
        broker = json.loads((root / "sandboxd.json").read_bytes())
        self.assertEqual(broker["data_host_path"], str(root / "data"))
        self.assertEqual(broker["profiles"]["maintainedmost"], {
            "image": self.env["VIBECI_SANDBOX_IMAGE"], "user": "10001:10001",
            "memory": "4g", "cpus": 2, "pids": 512, "tmp_size": "1g",
        })
        other = self.prepared_root()
        self.assertNotEqual(root, other)

    def test_prepare_noop_and_publication_opt_in_do_not_enable_engine_pushes(self):
        for event, preview, enabled, push, expected in (
            ("schedule", "true", "true", "true", True),
            ("workflow_dispatch", "false", "true", "true", True),
            ("workflow_dispatch", "true", "true", "true", False),
            ("schedule", "false", "false", "true", False),
            ("schedule", "false", "true", "false", False),
        ):
            with self.subTest(case=(event, preview, enabled, push)), mock.patch.dict(os.environ, {
                "GITHUB_EVENT_NAME": event, "PREVIEW": preview, "VIBECI_ENABLED": enabled, "VIBECI_PUSH": push,
            }):
                root = self.prepared_root()
                self.assertEqual(self.plan.call_args.kwargs, {"push": expected})
                config = json.loads((root / "data/config.json").read_bytes())
                self.assertIs(config["repos"][0]["push"]["dry_run"], True)
        self.changed = False
        self.records["summary.json"] = b'{"changed":false,"version":2}\n'
        root = self.prepared_root()
        self.assertEqual({path.name for path in (root / "public-inputs").iterdir()}, {"summary.md", "summary.json"})
        self.assertTrue(Path(self.env["GITHUB_OUTPUT"]).read_text().endswith("changed=false\n"))

    def test_prepare_requires_complete_privacy_environment_before_any_files(self):
        for key in ("VIBECI_MODELS", "VIBECI_PRIVATE_TERMS", "VIBECI_LLM_API_KEY", "GH_TOKEN"):
            for absent in (True, False):
                with self.subTest(key=key, absent=absent), mock.patch.dict(os.environ):
                    os.environ.pop(key) if absent else os.environ.update({key: " \t"})
                    self.invoke("prepare", 1)
                    self.assertEqual(list(self.runner.iterdir()), [])
                    self.assertFalse(Path(self.env["GITHUB_OUTPUT"]).exists())
                    self.plan.assert_not_called()

    def test_prepare_rejects_nonfile_credentials_driver_typos_and_numeric_bounds(self):
        changes = [("providers", "api_key", value) for value in
                   (LLM_KEY, "env:VIBECI_LLM_API_KEY", "file:/run/secrets/other", {"file": "/run/secrets/llm_api_key"})]
        changes += [("providers", "type", TERM), ("providers", "max_retries", 1 << 63),
                    ("models", "max_tokens", -1), ("models", "max_tokens", 1 << 63),
                    ("models", "max_tokens", True), ("models", "temperature", float("nan"))]
        for index, (section, key, value) in enumerate(changes):
            models = copy.deepcopy(MODELS)
            models[section]["provider" if section == "providers" else "model"][key] = value
            with self.subTest(case=index), mock.patch.dict(os.environ, VIBECI_MODELS=json.dumps(models)):
                self.invoke("prepare", 1)
                self.assertEqual(list(self.runner.iterdir()), [])
                self.plan.assert_not_called()

    def test_prepare_rejects_invalid_json_private_terms_and_environment_size(self):
        cases = [("VIBECI_MODELS", value) for value in ("{" + REFLECTION,
                 self.env["VIBECI_MODELS"][:-1] + ',"roles":{}}',
                 self.env["VIBECI_MODELS"].replace('"max_tokens": 2048', '"max_tokens": 1e999'))]
        cases += [("VIBECI_PRIVATE_TERMS", json.dumps(value)) for value in
                  ([], {}, [TERM, TERM.upper()], ["abc"], [TERM * 300], [TERM + str(i) for i in range(129)])]
        cases += [(key, "x" * 65537) for key in
                  ("VIBECI_MODELS", "VIBECI_PRIVATE_TERMS", "VIBECI_LLM_API_KEY", "GH_TOKEN")]
        for index, (key, value) in enumerate(cases):
            with self.subTest(case=index), mock.patch.dict(os.environ, {key: value}):
                self.invoke("prepare", 1)
                self.assertEqual(list(self.runner.iterdir()), [])
                self.plan.assert_not_called()
        with mock.patch.dict(os.environ, VIBECI_MODELS=self.env["VIBECI_MODELS"].ljust(65536)):
            self.prepared_root()

    def test_prepare_rejects_unpinned_images_and_private_public_identifiers(self):
        cases = [(key, image) for key in ("VIBECI_IMAGE", "VIBECI_SANDBOX_IMAGE") for image in
                 ("", "registry.example.invalid/image:latest", "registry/image@sha256:abc",
                  "registry/image@sha256:" + "A" * 64, HOST + "/image@sha256:" + "a" * 64)]
        cases += [(key, TERM) for key in ("GITHUB_REPOSITORY", "BASE_BRANCH", "COMPOSE_PROJECT_NAME")]
        for index, (key, value) in enumerate(cases):
            with self.subTest(case=index), mock.patch.dict(os.environ, {key: value}):
                self.invoke("prepare", 1)
                self.assertEqual(list(self.runner.iterdir()), [])
                self.plan.assert_not_called()

    def test_prepare_rejects_runner_temp_inside_checkout_including_symlink_aliases(self):
        child = self.checkout / "temporary"
        child.mkdir()
        alias = self.temp / "alias"
        alias.symlink_to(child, target_is_directory=True)
        for index, parent in enumerate((self.checkout, child, alias, self.temp / "missing")):
            with self.subTest(case=index), mock.patch.dict(os.environ, RUNNER_TEMP=str(parent)):
                self.invoke("prepare", 1)
                self.plan.assert_not_called()
                self.assertEqual(list(child.iterdir()), [])
                self.assertFalse(Path(self.env["GITHUB_ENV"]).exists())

    def test_prepare_registers_cleanup_before_planner_exception_with_private_diagnostics(self):
        self.plan.side_effect = RuntimeError(REFLECTION)
        self.invoke("prepare", 1)
        output = Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines()
        self.assertEqual(len(output), 1)
        self.assertTrue(output[0].startswith("run_dir="))
        root = Path(output[0].split("=", 1)[1])
        self.assertEqual((root / ".runtime-owner").read_bytes(), OWNER)
        self.assertEqual(Path(self.env["GITHUB_ENV"]).read_text(), "VIBECI_RUN_DIR=" + str(root) + "\n")
        os.environ["VIBECI_RUN_DIR"] = str(root)
        self.invoke("discard")
        self.assertFalse(root.exists())

    def test_prepare_refuses_sensitive_snapshots_without_emitting_changed(self):
        for name in self.records:
            with self.subTest(name=name), mock.patch.dict(self.records, {name: REFLECTION.encode()}):
                self.invoke("prepare", 1)
                output = Path(self.env["GITHUB_OUTPUT"]).read_text()
                self.assertNotIn("changed=", output)
                root = Path(output.splitlines()[-1].split("=", 1)[1])
                self.assertFalse((root / "public-inputs" / name).exists())

    def test_prepare_rejects_dangling_snapshot_links_instead_of_treating_them_as_absent(self):
        def linked_plan(*args, **kwargs):
            result = self.fake_plan(*args, **kwargs)
            path = args[3] / "summary.md"
            path.unlink()
            path.symlink_to(self.temp / "missing")
            return result

        self.plan.side_effect = linked_plan
        self.invoke("prepare", 1)


class EngineTests(RuntimeCase):
    def test_engine_has_file_only_read_credentials_filtered_environment_and_private_output(self):
        root = self.prepared_root()
        os.environ.update(DOCKER_AUTH_CONFIG=WRITE_KEY, GH_ENTERPRISE_TOKEN=WRITE_KEY,
                          GIT_CONFIG_VALUE_0=WRITE_KEY, HTTP_PROXY=WRITE_KEY,
                          DOCKER_CONFIG=str(self.temp / "docker-config"), DOCKER_HOST="unix:///var/run/docker.sock")
        reflected = json.dumps([{"repo": "maintainedmost", "status": "dry-run", "error": REFLECTION}]).encode()
        observed = {}

        def engine(args, **options):
            directory = root / "secrets"
            observed["directory_mode"] = stat.S_IMODE(directory.stat().st_mode)
            observed["files"] = {path.name: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                                 for path in directory.iterdir()}
            observed["result_mode"] = stat.S_IMODE(os.fstat(options["stdout"].fileno()).st_mode)
            options["stdout"].write(reflected)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(runtime.subprocess, "run", side_effect=engine) as process:
            self.invoke("engine")
        args, options = process.call_args
        self.assertEqual(process.call_count, 1)
        self.assert_public(json.dumps(args), json.dumps(options["env"]))
        self.assertEqual(args[0], ["docker", "compose", "-f", "maintenance/compose.yaml", "run", "--rm", "-T",
                                   "vibeci", "run", "-config", "/etc/vibeci/config.json", "-dry-run", "-json"])
        self.assertEqual(options["stderr"], subprocess.DEVNULL)
        self.assertEqual(options["timeout"], 7350)
        self.assertIs(options["check"], False)
        self.assertNotIn("shell", options)
        self.assertEqual(options["stdout"].name, str(root / "result.json"))
        self.assertTrue(options["stdout"].closed)
        self.assertEqual(set(options["env"]), {"PATH", "HOME", "DOCKER_CONFIG", "DOCKER_HOST",
                                               "COMPOSE_PROJECT_NAME", "VIBECI_IMAGE", "VIBECI_SANDBOX_IMAGE", "VIBECI_RUN_DIR"})
        self.assertEqual(observed["directory_mode"], 0o700)
        self.assertEqual(observed["result_mode"], 0o600)
        self.assertEqual(set(observed["files"]), {"llm_api_key", "fork_read_token"})
        self.assertTrue(observed["files"] == {"llm_api_key": (LLM_KEY.encode(), 0o444),
                                              "fork_read_token": (READ_KEY.encode(), 0o444)},
                        "credential mounts did not contain only the read/API keys")
        config_bytes = (root / "data/config.json").read_bytes()
        self.assertFalse(any(value.encode() in config_bytes for value in (READ_KEY, LLM_KEY, WRITE_KEY, BUILTIN_KEY)),
                         "a credential value entered engine configuration")
        config = json.loads(config_bytes)
        self.assertEqual(config["repos"][0]["fork"]["auth"], {"token": "file:/run/secrets/fork_read_token"})
        self.assertEqual(config["providers"]["provider-1"]["api_key"], "file:/run/secrets/llm_api_key")
        self.assertIs(config["repos"][0]["push"]["dry_run"], True)
        self.assertEqual(config["alerts"], [])
        self.assertEqual(config["log_level"], "error")
        self.assertTrue((root / "result.json").read_bytes() == reflected, "raw output was not kept private")
        self.assert_credentials_gone(root)

    def test_engine_suppresses_reflected_process_errors_and_always_unlinks_credentials(self):
        for index in range(4):
            root = self.own_root()

            def failing(args, **options):
                options["stdout"].write(REFLECTION.encode())
                if index == 0:
                    return subprocess.CompletedProcess(args, 23)
                if index == 1:
                    raise subprocess.TimeoutExpired(args, options["timeout"], output=REFLECTION, stderr=REFLECTION)
                raise (OSError if index == 2 else RuntimeError)(REFLECTION)

            with self.subTest(case=index), mock.patch.object(runtime.subprocess, "run", side_effect=failing):
                self.invoke("engine", 1)
                self.assert_credentials_gone(root)
                self.assertEqual(stat.S_IMODE((root / "result.json").stat().st_mode), 0o600)

    def test_engine_rejects_non_dry_run_malformed_duplicate_and_nonfinite_json(self):
        payloads = [b"{" + REFLECTION.encode(), b"\xff", b"[]", b"{}", SUCCESS[:-2] + b",{}]",
                    b'[{"repo":"maintainedmost","status":"dry-run","status":"dry-run"}]',
                    b'[{"repo":"maintainedmost","status":"dry-run","usage":NaN}]',
                    b'[{"repo":"maintainedmost","status":"dry-run","usage":1e999}]']
        payloads += [json.dumps([{"repo": "maintainedmost", "status": status, "error": REFLECTION}]).encode()
                     for status in ("synced", "failed", "up-to-date", None)]
        payloads.append(json.dumps([{"repo": TERM, "status": "dry-run"}]).encode())
        for index, payload in enumerate(payloads):
            root = self.own_root()

            def engine(args, **options):
                options["stdout"].write(payload)
                return subprocess.CompletedProcess(args, 0)

            with self.subTest(case=index), mock.patch.object(runtime.subprocess, "run", side_effect=engine):
                self.invoke("engine", 1)
                self.assert_credentials_gone(root)

    def test_engine_rejects_oversized_raw_result_without_reading_or_printing_it(self):
        root = self.own_root()

        def engine(args, **options):
            options["stdout"].write(REFLECTION.encode())
            options["stdout"].truncate(runtime.MAX_BYTES + 1)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(runtime.subprocess, "run", side_effect=engine):
            self.invoke("engine", 1)
        self.assert_credentials_gone(root)

    def test_engine_removes_partial_credential_files_on_write_and_chmod_exceptions(self):
        original_write, original_chmod = runtime.write_private, Path.chmod
        for filename in ("llm_api_key", "fork_read_token"):
            for stage in ("before-write", "after-write", "chmod"):
                root = self.own_root()

                def write(path, payload):
                    if path.name == filename and stage == "before-write":
                        raise OSError(REFLECTION)
                    original_write(path, payload)
                    if path.name == filename and stage == "after-write":
                        raise OSError(REFLECTION)

                def chmod(path, mode, **options):
                    if path.name == filename and stage == "chmod":
                        raise OSError(REFLECTION)
                    return original_chmod(path, mode, **options)

                with self.subTest(file=filename, stage=stage), \
                        mock.patch.object(runtime, "write_private", side_effect=write), \
                        mock.patch.object(Path, "chmod", chmod), mock.patch.object(runtime.subprocess, "run") as process:
                    self.invoke("engine", 1)
                    self.assert_credentials_gone(root)
                    process.assert_not_called()

    def test_engine_validates_both_credential_byte_limits_and_cleans_partial_creation(self):
        for variable in ("VIBECI_LLM_API_KEY", "VIBECI_FORK_READ_TOKEN"):
            for index, value in enumerate((None, " \t", "x" * 65537, "\u00e9" * 32769)):
                root = self.own_root()
                with self.subTest(variable=variable, case=index), mock.patch.dict(os.environ), \
                        mock.patch.object(runtime.subprocess, "run") as process:
                    os.environ.pop(variable) if value is None else os.environ.update({variable: value})
                    self.invoke("engine", 1)
                    self.assert_credentials_gone(root)
                    process.assert_not_called()

    def test_engine_accepts_exact_credential_byte_limit_without_exporting_values(self):
        root = self.own_root()
        observed = []

        def engine(args, **options):
            observed.extend(path.stat().st_size for path in (root / "secrets").iterdir())
            options["stdout"].write(SUCCESS)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.dict(os.environ, VIBECI_LLM_API_KEY=LLM_KEY.ljust(65536, "a"),
                             VIBECI_FORK_READ_TOKEN=READ_KEY.ljust(65536, "r")), \
                mock.patch.object(runtime.subprocess, "run", side_effect=engine) as process:
            self.invoke("engine")
            self.assert_public(json.dumps(process.call_args.args), json.dumps(process.call_args.kwargs["env"]))
        self.assertEqual(observed, [65536, 65536])
        self.assert_credentials_gone(root)

    def test_engine_refuses_existing_result_and_secret_links_without_touching_foreign_keys(self):
        foreign = self.temp / "foreign-key"
        foreign.write_bytes(READ_KEY.encode())
        foreign.chmod(0o400)
        for location in ("result.json", "secrets"):
            root = self.own_root()
            (root / location).symlink_to(foreign)
            with self.subTest(location=location), mock.patch.object(runtime.subprocess, "run") as process:
                self.invoke("engine", 1)
                process.assert_not_called()
                self.assertTrue(foreign.read_bytes() == READ_KEY.encode(), "foreign key changed")
                self.assertEqual(stat.S_IMODE(foreign.stat().st_mode), 0o400)
                if location == "result.json":
                    self.assert_credentials_gone(root)


class RestoreTests(RuntimeCase):
    def test_restore_uses_sealed_inputs_and_replaces_destination_links_with_regular_metadata(self):
        root = self.own_root()
        foreign = self.temp / "foreign-key"
        foreign.write_bytes(READ_KEY.encode())
        foreign.chmod(0o400)
        (root / "data/plan.json").symlink_to(foreign)
        for name in ("summary.md", "summary.json", "public-summary.md", "public-summary.json", "config.json"):
            (root / "data" / name).write_bytes(REFLECTION.encode())
        self.invoke("restore")
        for original, target in (("plan.json", "plan.json"), ("summary.json", "public-summary.json"),
                                 ("summary.md", "public-summary.md")):
            path = root / "data" / target
            self.assertFalse(path.is_symlink())
            self.assertTrue(stat.S_ISREG(path.stat().st_mode))
            self.assertEqual(path.stat().st_nlink, 1)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertTrue(path.read_bytes() == self.records[original], "restored metadata did not match the seal")
            self.assertTrue((root / "public-inputs" / original).read_bytes() == self.records[original], "seal changed")
        self.assertEqual({path.name for path in (root / "data").iterdir()},
                         {"plan.json", "summary.md", "summary.json", "public-summary.md", "public-summary.json", "config.json"})
        self.assertTrue(foreign.read_bytes() == READ_KEY.encode(), "restore followed a destination link")
        for name in ("summary.md", "summary.json", "config.json"):
            self.assertTrue((root / "data" / name).read_bytes() == REFLECTION.encode(), "restore changed harness data")

    def test_restore_noop_copies_only_available_public_metadata(self):
        root = self.own_root()
        (root / "public-inputs/plan.json").unlink()
        self.invoke("restore")
        self.assertEqual({path.name for path in (root / "data").iterdir()}, {"public-summary.md", "public-summary.json"})

    def test_restore_screens_all_sealed_inputs_before_any_metadata_writes(self):
        for name in self.records:
            root = self.own_root()
            (root / "public-inputs" / name).write_bytes(REFLECTION.encode())
            for target in ("plan.json", "public-summary.json", "public-summary.md"):
                (root / "data" / target).write_bytes(b"existing metadata\n")
            with self.subTest(name=name), mock.patch.object(runtime, "write_private", wraps=runtime.write_private) as write:
                self.invoke("restore", 1)
                self.assertFalse(write.called, "metadata was written before every sealed input passed screening")
                self.assertTrue(all(path.read_bytes() == b"existing metadata\n" for path in (root / "data").iterdir()))

    def test_restore_requires_privacy_inputs_before_touching_existing_metadata(self):
        root = self.own_root()
        for key in ("VIBECI_PRIVATE_TERMS", "VIBECI_MODELS", "VIBECI_LLM_API_KEY", "GH_TOKEN"):
            with self.subTest(key=key), mock.patch.dict(os.environ), \
                    mock.patch.object(runtime, "write_private") as write:
                os.environ.pop(key)
                self.invoke("restore", 1)
                write.assert_not_called()
                self.assertEqual(list((root / "data").iterdir()), [])

    def test_restore_rejects_linked_data_and_linked_sealed_directory(self):
        for directory in ("data", "public-inputs"):
            root = self.own_root()
            foreign = root / "untrusted"
            (root / directory).rename(foreign)
            (root / directory).symlink_to(foreign, target_is_directory=True)
            with self.subTest(directory=directory):
                self.invoke("restore", 1)

    def test_restore_rejects_snapshot_symlinks_hardlinks_and_nonregular_records(self):
        for kind in ("symlink", "dangling", "hardlink", "directory", "fifo"):
            root = self.own_root()
            path = root / "public-inputs/plan.json"
            path.unlink()
            if kind in {"symlink", "dangling"}:
                path.symlink_to(root / "public-inputs" / ("summary.json" if kind == "symlink" else "missing"))
            elif kind == "hardlink":
                os.link(root / "public-inputs/summary.json", path)
            elif kind == "directory":
                path.mkdir()
            else:
                os.mkfifo(path)
            with self.subTest(kind=kind):
                self.invoke("restore", 1)
                self.assertEqual(list((root / "data").iterdir()), [])


class EvidenceTests(RuntimeCase):
    def test_evidence_sanitizes_reflected_engine_errors_and_excludes_private_state(self):
        root = self.own_root()
        outcome = {"repo": "maintainedmost", "status": "failed", "commit": "2" * 40, "version": "2",
                   "patches": {"total": 3, TERM: 99}, "error": REFLECTION, "model": MODEL,
                   "message": "untrusted free-form response", "headers": {"Authorization": LLM_KEY},
                   "usage": {MODEL: 1}, TERM: REFLECTION}
        raw = json.dumps([outcome]).encode()
        runtime.write_private(root / "result.json", raw)
        for relative in ("models.json", "sandboxd.json", "engine.log", "data/config.json", "data/summary.md",
                         "data/summary.json", "data/credentials.env", "public-inputs/engine.log"):
            (root / relative).write_bytes(REFLECTION.encode())
        os.environ.update(PLAN_OUTCOME="success", RUN_OUTCOME="failure", PUBLISH_OUTCOME="skipped", CLEANUP_OUTCOME="cancelled")
        self.invoke("evidence")
        artifacts = root / "artifacts"
        self.assertEqual({path.name for path in artifacts.iterdir()}, PUBLIC_NAMES)
        self.assertEqual(stat.S_IMODE(artifacts.stat().st_mode), 0o700)
        for path in artifacts.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertFalse(path.is_symlink())
            self.assert_public(path.read_bytes())
            self.assertNotIn(b"untrusted free-form response", path.read_bytes())
        self.assertEqual(json.loads((artifacts / "result.json").read_bytes()), [{
            "repo": "maintainedmost", "status": "failed", "commit": "2" * 40, "version": 2, "patches": {"total": 3},
        }])
        self.assertEqual(Path(self.env["GITHUB_STEP_SUMMARY"]).read_bytes(), (artifacts / "summary.md").read_bytes())
        self.assertEqual(Path(self.env["GITHUB_OUTPUT"]).read_bytes(), b"ready=true\n")
        self.assertTrue((root / "result.json").read_bytes() == raw, "raw result was changed")

    def test_sensitive_known_artifact_means_zero_artifacts_and_no_ready_output(self):
        for name in self.records:
            for index, canary in enumerate(CANARIES):
                root = self.own_root()
                (root / "public-inputs" / name).write_bytes(canary.encode())
                with self.subTest(name=name, canary=index):
                    self.invoke("evidence", 1)
                    self.assert_no_evidence(root)

    def test_evidence_rejects_invalid_workflow_states_before_any_public_writes(self):
        for key in ("PLAN_OUTCOME", "RUN_OUTCOME", "PUBLISH_OUTCOME", "CLEANUP_OUTCOME"):
            for index, value in enumerate((TERM, "SUCCESS", "success\nready=true", "")):
                root = self.own_root()
                with self.subTest(key=key, case=index), mock.patch.dict(os.environ, {key: value}):
                    self.invoke("evidence", 1)
                    self.assert_no_evidence(root)

    def test_evidence_rejects_raw_parse_errors_and_invalid_results_with_only_generic_error_records(self):
        payloads = [b"{" + REFLECTION.encode(), b"\xff", b"[]", b"{}",
                    b'[{"repo":"maintainedmost","status":"failed","status":"error"}]',
                    b'[{"repo":"maintainedmost","status":"failed","usage":NaN}]',
                    json.dumps([{"repo": "maintainedmost", "status": TERM, "error": REFLECTION}]).encode()]
        for index, payload in enumerate(payloads):
            root = self.own_root()
            runtime.write_private(root / "result.json", payload)
            with self.subTest(case=index):
                self.invoke("evidence")
                for path in (root / "artifacts").iterdir():
                    self.assert_public(path.read_bytes())
                self.assertEqual(json.loads((root / "artifacts/result.json").read_bytes()),
                                 [{"repo": "maintainedmost", "status": "error"}])

    def test_evidence_never_opens_linked_raw_result_and_does_not_read_harness_data(self):
        root = self.own_root()
        (root / "data").rmdir()
        (root / "data").symlink_to(self.temp / "missing", target_is_directory=True)
        foreign = self.temp / "foreign-key"
        foreign.write_bytes(LLM_KEY.encode())
        foreign.chmod(0o400)
        (root / "result.json").symlink_to(foreign)
        with mock.patch.object(Path, "open", side_effect=AssertionError("linked raw evidence was opened")):
            # invoke() also reads public sinks through Path.open, so capture here directly.
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                runtime.evidence()
        self.assert_public(stdout.getvalue(), stderr.getvalue(), (root / "artifacts/result.json").read_bytes())
        self.assertTrue(stdout.getvalue() + stderr.getvalue() == "", "evidence wrote a diagnostic")
        self.assertEqual(json.loads((root / "artifacts/result.json").read_bytes()),
                         [{"repo": "maintainedmost", "status": "error"}])
        self.assertTrue(foreign.read_bytes() == LLM_KEY.encode(), "foreign key changed")

    def test_evidence_rejects_linked_or_nonregular_sealed_records(self):
        for kind in ("symlink", "dangling", "hardlink", "directory", "fifo", "parent"):
            root = self.own_root()
            source = root / "public-inputs/summary.md"
            if kind == "parent":
                (root / "public-inputs").rename(root / "data/untrusted")
                (root / "public-inputs").symlink_to(root / "data/untrusted", target_is_directory=True)
            else:
                source.unlink()
                if kind in {"symlink", "dangling"}:
                    source.symlink_to(root / "public-inputs" / ("plan.json" if kind == "symlink" else "missing"))
                elif kind == "hardlink":
                    os.link(root / "public-inputs/plan.json", source)
                elif kind == "directory":
                    source.mkdir()
                else:
                    os.mkfifo(source)
            with self.subTest(kind=kind):
                self.invoke("evidence", 1)
                self.assert_no_evidence(root)

    def test_evidence_requires_privacy_configuration_and_refuses_existing_artifact_links(self):
        root = self.own_root()
        with mock.patch.dict(os.environ):
            os.environ.pop("VIBECI_PRIVATE_TERMS")
            self.invoke("evidence", 1)
            self.assert_no_evidence(root)
        foreign = self.temp / "foreign"
        foreign.mkdir()
        key = foreign / "key"
        key.write_bytes(READ_KEY.encode())
        key.chmod(0o400)
        (root / "artifacts").symlink_to(foreign, target_is_directory=True)
        self.invoke("evidence", 1)
        self.assertEqual(list(foreign.iterdir()), [key])
        self.assertTrue(key.read_bytes() == READ_KEY.encode(), "artifact creation followed a foreign link")


class DiscardTests(RuntimeCase):
    def test_invalid_shared_foreign_and_symlinked_roots_are_not_touched(self):
        owned = self.own_root()
        foreign = self.runner / "foreign"
        foreign.mkdir()
        key = foreign / "readonly-key"
        key.write_bytes(READ_KEY.encode())
        key.chmod(0o400)
        wrong_owner = self.runner / "maintainedmost-vibeci-foreign"
        wrong_owner.mkdir()
        runtime.write_private(wrong_owner / ".runtime-owner", b"foreign\n")
        outside = self.temp / "maintainedmost-vibeci-outside"
        outside.mkdir()
        runtime.write_private(outside / ".runtime-owner", OWNER)
        link = self.runner / "maintainedmost-vibeci-link"
        link.symlink_to(owned, target_is_directory=True)
        unmarked = self.runner / "maintainedmost-vibeci-unmarked"
        unmarked.mkdir()
        cases = (self.runner, self.checkout, foreign, wrong_owner, outside, link, unmarked,
                 Path(owned.name), self.runner / "maintainedmost-vibeci-missing")
        for index, path in enumerate(cases):
            with self.subTest(case=index), mock.patch.dict(os.environ, VIBECI_RUN_DIR=str(path)), \
                    mock.patch.object(runtime.shutil, "rmtree") as remove, \
                    mock.patch.object(runtime.subprocess, "run") as process:
                self.invoke("discard", 1)
                remove.assert_not_called()
                process.assert_not_called()
                self.assertTrue(key.read_bytes() == READ_KEY.encode(), "foreign read-only key was touched")
                self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o400)

    def test_runtime_operations_reject_symlinked_ownership_markers(self):
        root = self.own_root()
        marker = root / ".runtime-owner"
        marker.rename(root / "owner-target")
        marker.symlink_to(root / "owner-target")
        for operation in ("engine", "restore", "evidence", "discard"):
            with self.subTest(operation=operation), mock.patch.object(runtime.subprocess, "run") as process:
                self.invoke(operation, 1)
                process.assert_not_called()
                self.assertTrue(root.exists())

    def test_discard_removes_only_its_marked_tree_including_readonly_files_without_privilege(self):
        root = self.own_root()
        foreign = self.temp / "foreign-key"
        foreign.write_bytes(READ_KEY.encode())
        foreign.chmod(0o400)
        (root / "foreign-link").symlink_to(foreign)
        runtime.write_private(root / "private-key", LLM_KEY.encode())
        (root / "private-key").chmod(0o444)
        with mock.patch.object(runtime.subprocess, "run") as process:
            self.invoke("discard")
            process.assert_not_called()
        self.assertFalse(root.exists())
        self.assertTrue(foreign.read_bytes() == READ_KEY.encode(), "discard followed a foreign link")

    def test_permission_fallback_is_mocked_exact_bounded_and_credential_free(self):
        root = self.own_root()
        remove = shutil.rmtree

        def sudo(args, **options):
            remove(root)
            return subprocess.CompletedProcess(args, 0)

        with mock.patch.object(runtime.shutil, "rmtree", side_effect=PermissionError(REFLECTION)), \
                mock.patch.object(runtime.subprocess, "run", side_effect=sudo) as process:
            self.invoke("discard")
        self.assertEqual(process.call_count, 1)
        args, options = process.call_args
        self.assert_public(json.dumps(args), json.dumps(options["env"]))
        self.assertEqual(args, (["sudo", "-n", "rm", "-rf", "--", str(root)],))
        self.assertEqual(options, {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                                   "env": {"PATH": os.defpath}, "timeout": 60, "check": False})
        self.assertFalse(root.exists())

    def test_fallback_failure_timeout_and_false_success_remain_generic_and_do_not_retry(self):
        for index in range(3):
            root = self.own_root()

            def sudo(args, **options):
                if index == 2:
                    raise subprocess.TimeoutExpired(args, 60, output=REFLECTION, stderr=REFLECTION)
                return subprocess.CompletedProcess(args, index, REFLECTION, REFLECTION)

            with self.subTest(case=index), \
                    mock.patch.object(runtime.shutil, "rmtree", side_effect=PermissionError(REFLECTION)), \
                    mock.patch.object(runtime.subprocess, "run", side_effect=sudo) as process:
                self.invoke("discard", 1)
                self.assertEqual(process.call_count, 1)
                self.assertTrue(root.exists())

    def test_nonpermission_removal_errors_never_trigger_privilege_escalation(self):
        root = self.own_root()
        with mock.patch.object(runtime.shutil, "rmtree", side_effect=OSError(REFLECTION)), \
                mock.patch.object(runtime.subprocess, "run") as process:
            self.invoke("discard", 1)
            process.assert_not_called()
            self.assertTrue(root.exists())


class RuntimeWorkflowPrivacyTests(unittest.TestCase):
    """Privacy handoffs only; broader workflow/probe coverage stays in WorkflowTests."""

    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/maintain.yml").read_text(encoding="utf-8")
        cls.compose = (ROOT / "maintenance/compose.yaml").read_text(encoding="utf-8")
        cls.steps = re.split(r"(?m)(?=^      - )", cls.workflow)[1:]
        cls.by_id = {match[1]: step for step in cls.steps
                     if (match := re.search(r"(?m)^        id: (\w+)$", step))}

    def test_models_are_secret_sourced_and_no_credentials_are_global(self):
        global_config = self.workflow.split("    steps:\n", 1)[0]
        self.assertNotIn("secrets.", global_config)
        self.assertNotRegex(global_config, r"(?m)^\s*(?:GH_TOKEN|GITHUB_TOKEN|VIBECI_(?:MODELS|PRIVATE_TERMS|LLM_API_KEY|GIT_TOKEN|FORK_READ_TOKEN)):")
        model_lines = re.findall(r"(?m)^\s*VIBECI_MODELS: (.+)$", self.workflow)
        self.assertTrue(model_lines)
        self.assertEqual(set(model_lines), {"${{ secrets.VIBECI_MODELS }}"})
        self.assertNotIn("vars.VIBECI_MODELS", self.workflow)
        for identifier in ("runtime", "publish", "evidence"):
            self.assertIn("VIBECI_PRIVATE_TERMS: ${{ secrets.VIBECI_PRIVATE_TERMS }}", self.by_id[identifier])
            self.assertIn("VIBECI_LLM_API_KEY: ${{ secrets.VIBECI_LLM_API_KEY }}", self.by_id[identifier])

    def test_engine_gets_only_stage_scoped_read_token_and_model_key_never_write_pat(self):
        engine = self.by_id["engine"]
        env = dict(re.findall(r"(?m)^          ([A-Z_]+): (.+)$", engine))
        self.assertEqual(env, {"VIBECI_FORK_READ_TOKEN": "${{ github.token }}",
                               "VIBECI_LLM_API_KEY": "${{ secrets.VIBECI_LLM_API_KEY }}"})
        self.assertIn("permissions:\n  contents: read\n  pull-requests: read\n", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("run: python3 -B scripts/vibeci-runtime.py engine", engine)
        for identifier, step in self.by_id.items():
            if "secrets.VIBECI_GIT_TOKEN" in step:
                self.assertIn(identifier, {"preflight", "publish", "evidence"})
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.by_id["runtime"])
        self.assertIn("GH_TOKEN: ${{ secrets.VIBECI_GIT_TOKEN }}", self.by_id["publish"])

    def test_cleanup_evidence_upload_and_discard_are_always_gated_and_ordered(self):
        discard = next(step for step in self.steps if "scripts/vibeci-runtime.py discard" in step)
        upload = next(step for step in self.steps if "uses: actions/upload-artifact@" in step)
        for step in (self.by_id["cleanup"], self.by_id["ownership"], self.by_id["evidence"], discard):
            self.assertIn("if: always() && steps.runtime.outputs.run_dir != ''", step)
        self.assertIn("if: always() && steps.evidence.outputs.ready == 'true'", upload)
        self.assertIn("steps.cleanup.outcome == 'success'", self.by_id["ownership"])
        publish = self.by_id["publish"]
        self.assertIn("if: success()", publish)
        self.assertIn("steps.engine.outcome == 'success' && steps.cleanup.outcome == 'success'", publish)
        self.assertLess(publish.index("scripts/vibeci-runtime.py restore"), publish.index("scripts/vibeci-github.py publish"))
        ordered = [self.by_id[name] for name in ("engine", "cleanup", "ownership", "publish", "evidence")] + [upload, discard]
        self.assertEqual([self.steps.index(step) for step in ordered], sorted(self.steps.index(step) for step in ordered))
        self.assertNotIn("continue-on-error", self.workflow)

    def test_upload_paths_are_exactly_the_four_screened_artifacts(self):
        upload = next(step for step in self.steps if "uses: actions/upload-artifact@" in step)
        paths = upload.split("          path: |\n", 1)[1].split("          if-no-files-found:", 1)[0]
        self.assertEqual({line.strip() for line in paths.splitlines()},
                         {"${{ steps.runtime.outputs.run_dir }}/artifacts/" + name for name in PUBLIC_NAMES})
        self.assertIn("if-no-files-found: error", upload)

    def test_compose_disables_logs_and_mounts_credentials_only_into_harness(self):
        hardening = self.compose.split("services:\n", 1)[0]
        self.assertIn("logging:\n    driver: none", hardening)
        self.assertNotRegex(self.compose, r"(?m)^\s*(?:environment|env_file):")
        self.assertNotRegex(self.compose, r"VIBECI_(?:GIT_TOKEN|LLM_API_KEY|FORK_READ_TOKEN)|GH_TOKEN|GITHUB_TOKEN")
        for name in ("sandboxd", "vibeci", "probe"):
            service = re.search(r"(?ms)^  " + name + r":\n(.*?)(?=^  \w+:|^\w+:|\Z)", self.compose)[1]
            self.assertIn("<<: *hardening", service)
            self.assertNotIn("logging:", service)
            if name == "vibeci":
                self.assertIn("secrets: [fork_read_token, llm_api_key]", service)
                self.assertNotIn("docker.sock", service)
                volumes = re.findall(r"(?m)^      - (.+)$", service.split("    volumes:\n", 1)[1])
                self.assertEqual(volumes, [
                    "${VIBECI_RUN_DIR:?set the isolated CI runtime directory}/data:/data",
                    "socket:/run/vibeci", "${VIBECI_RUN_DIR}/data/config.json:/etc/vibeci/config.json:ro",
                ])
            else:
                self.assertNotIn("secrets:", service)
        self.assertIn("file: ${VIBECI_RUN_DIR:?set the isolated CI runtime directory}/secrets/fork_read_token", self.compose)
        self.assertIn("file: ${VIBECI_RUN_DIR}/secrets/llm_api_key", self.compose)
        self.assertNotRegex(self.compose, r"public-inputs|models\.json|GITHUB_WORKSPACE")


if __name__ == "__main__":
    unittest.main()
