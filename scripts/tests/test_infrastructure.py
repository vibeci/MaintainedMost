import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class InfrastructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maintainedmost-infra-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        for name in ("lib.sh", "check-upstream.sh", "build-server.sh", "build-calls.sh", "build-image.sh", "build-transcriber.sh", "test-component.sh", "check-go-tests.py", "image-refs.py"):
            shutil.copy2(ROOT / "scripts" / name, self.root / "scripts" / name)
        (self.root / "maintenance").mkdir()
        shutil.copy2(ROOT / "maintenance/required-tests.json", self.root / "maintenance/required-tests.json")
        (self.root / "upstream.env").write_text(
            "SERVER_TAG=v1.9.0\nSERVER_COMMIT=" + "a" * 40 + "\n"
            "SERVER_IMAGE=registry.invalid/server:1.9.0\n"
            "CALLS_TAG=v1.9.0\nCALLS_COMMIT=" + "a" * 40 + "\n"
            "CALLS_VERSION=1000.9.0\nTRANSCRIBER_TAG=" + "a" * 40 + "\n"
            "TRANSCRIBER_BRANCH=master\nGO_VERSION=1.26.7\n"
            "RECORDER_TAG=v0.8.13\n"
            "RECORDER_SOURCE_IMAGE=docker.io/mattermost/calls-recorder:v0.8.13@sha256:8532abe26d24a6b8dbe0efc154b60aad3f72d77ab1b9128c6cd793b138e61ab2\n"
        )
        (self.root / "bin").mkdir()
        self.log = self.root / "commands.jsonl"
        self.env = {
            **os.environ,
            "PATH": str(self.root / "bin") + os.pathsep + os.environ["PATH"],
            "COMMAND_LOG": str(self.log),
            "REAL_GIT": shutil.which("git"),
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "REMOTE_REFS": "a\trefs/tags/v1.9.0\nb\trefs/tags/v1.10.0\nc\trefs/tags/v2.0.0-rc1",
            "RUN_TESTS": "0",
        }
        for key in ("SERVER_TEST_PACKAGES", "SERVER_TEST_PATTERN", "SERVER_CONFIG_TEST_PATTERN", "CALLS_TEST_PACKAGES", "CALLS_TEST_PATTERN", "CALLS_JEST_TEST_PATTERN", "GO_TEST_FLAGS", "CALLS_IMAGE_REGISTRY", "TRANSCRIBER_IMAGE", "TARGETARCH", "SOURCE_URL"):
            self.env.pop(key, None)
        self.stub("git", """
args = sys.argv[1:]
if args[0] == 'ls-remote':
    if os.getenv('FAIL_NETWORK'): sys.exit(1)
    print(os.environ['REMOTE_REFS'])
elif args[0] == 'clone':
    if os.getenv('FAIL_CLONE'): sys.exit(1)
    path = Path(args[-1])
    path.mkdir(parents=True)
    (path / 'value').write_text('one\\n')
    (path / 'plugin.json').write_text('{"id":"com.mattermost.calls","name":"Calls","homepage_url":"https://upstream.invalid/calls","support_url":"https://upstream.invalid/issues","min_server_version":"12.0.0","props":{"calls_recorder_version":"v0.8.13"}}')
    (path / 'server/public').mkdir(parents=True)
    (path / 'webapp/channels/dist').mkdir(parents=True)
    (path / 'webapp/channels/dist/index.html').write_text('built webapp')
    (path / 'webapp/install_mattermost_webapp.sh').write_text('readonly COMMITHASH=' + 'b' * 40 + '\\n')
    (path / 'standalone').mkdir()
    subprocess.run([os.environ['REAL_GIT'], 'init', '--quiet', str(path)], check=True)
elif args[0] == '-C' and args[2] in ('remote', 'fetch', 'checkout', 'rev-parse'):
    if args[2] == 'rev-parse': print('a' * 40)
    if args[2] == 'checkout': (Path(args[1]) / 'value').write_text('one\\n')
    if args[2] == 'fetch' and os.getenv('FAIL_CLONE'): sys.exit(1)
else:
    sys.exit(subprocess.call([os.environ['REAL_GIT']] + args))
""")

    def stub(self, name, body):
        path = self.root / "bin" / name
        if name == "go":
            body = "assert os.environ['GOTOOLCHAIN'] == 'go1.26.7'\n" + body
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, subprocess, sys\nfrom pathlib import Path\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
            "    log.write(json.dumps([Path(sys.argv[0]).name] + sys.argv[1:]) + '\\n')\n"
            + body
        )
        path.chmod(0o755)
        return path

    def run_shell(self, command, *args, **env):
        return subprocess.run(
            ["bash", "-euo", "pipefail", "-c", command, "test", *map(str, args)],
            cwd=self.root,
            env={**self.env, **env},
            text=True,
            capture_output=True,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def patches(self, component="server", conflict=False):
        directory = self.root / "patches" / component
        directory.mkdir(parents=True)
        for number, before, after in ((1, "missing" if conflict else "one", "two"), (2, "two", "three")):
            (directory / f"{number:04}.patch").write_text(
                f"diff --git a/value b/value\n--- a/value\n+++ b/value\n@@ -1 +1 @@\n-{before}\n+{after}\n"
            )
        return directory

    def test_absolute_output_directory_can_be_missing_and_contain_spaces(self):
        result = self.run_shell('source scripts/lib.sh; absolute_dir "new output/nested"')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str((self.root / "new output/nested").resolve()))

    def test_full_commit_checkout_fetches_instead_of_using_branch_option(self):
        commit = "b" * 40
        result = self.run_shell('source scripts/lib.sh; checkout_source local-repo "$1" checkout', commit)
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertIn(["git", "-C", "checkout", "fetch", "--quiet", "--depth", "1", "origin", commit], commands)
        self.assertFalse(any(command[1] == "clone" for command in commands))

    def test_tag_checkout_retains_tag_for_upstream_manifest_versioning(self):
        result = self.run_shell('source scripts/lib.sh; checkout_source local-repo v1.10.0 checkout')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["git", "clone", "--quiet", "--depth", "1", "--branch", "v1.10.0", "local-repo", "checkout"], self.commands())

    def test_relative_patch_paths_apply_sequentially_from_another_directory(self):
        self.patches()
        work = self.root / "source checkout"
        subprocess.run([self.env["REAL_GIT"], "init", "--quiet", str(work)], check=True)
        (work / "value").write_text("one\n")
        result = self.run_shell('source scripts/lib.sh; apply_patches "$1" patches/server', work)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((work / "value").read_text(), "three\n")

    def test_missing_patch_series_is_not_success(self):
        (self.root / "empty").mkdir()
        result = self.run_shell('source scripts/lib.sh; apply_patches . empty')
        self.assertNotEqual(result.returncode, 0)

    def test_upstream_semver_sort_and_combined_application(self):
        self.patches()
        result = self.run_shell('bash scripts/check-upstream.sh server')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("new_ref=v1.10.0", result.stdout)
        self.assertIn("verification=clean", result.stdout)
        self.assertEqual((self.root / "build/upstream-server/value").read_text(), "three\n")

    def test_network_and_clone_failures_are_not_patch_conflicts(self):
        self.patches()
        for failure in ("FAIL_NETWORK", "FAIL_CLONE"):
            with self.subTest(failure=failure):
                result = self.run_shell('bash scripts/check-upstream.sh server', **{failure: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("verification=conflict", result.stdout)
                self.assertNotIn("verification=clean", result.stdout)

    def test_actual_patch_conflict_fails_and_stops_the_series(self):
        self.patches(conflict=True)
        result = self.run_shell('bash scripts/check-upstream.sh server')
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertIn("verification=conflict", result.stdout)
        self.assertFalse(any("0002.patch" in arg for command in self.commands() for arg in command))

    def test_unchanged_upstream_does_not_clone(self):
        result = self.run_shell('bash scripts/check-upstream.sh server', REMOTE_REFS="a\trefs/tags/v1.9.0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("verification=unchanged", result.stdout)
        self.assertFalse(any(command[1] == "clone" for command in self.commands()))

    def test_transcriber_watches_branch_against_immutable_pin(self):
        self.patches("transcriber")
        commit = "b" * 40
        result = self.run_shell('bash scripts/check-upstream.sh transcriber', REMOTE_REFS=f"{commit}\trefs/heads/master")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"new_ref={commit}", result.stdout)
        self.assertIn("branch=master", result.stdout)

    def test_calls_alert_exposes_minimum_server_version(self):
        self.patches("calls")
        result = self.run_shell('bash scripts/check-upstream.sh calls')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("min_server=12.0.0", result.stdout)

    def test_pin_mismatch_is_fatal_before_patch_application(self):
        self.patches()
        result = self.run_shell('source scripts/lib.sh; ROOT="$PWD"; prepare_source repo v1.9.0 server wrong-commit')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not match pinned commit", result.stderr)
        self.assertFalse(any("apply" in command for command in self.commands()))

    def test_transcriber_uses_upstream_native_build_without_publishing(self):
        self.patches("transcriber")
        self.stub("make", "")
        engine = self.stub("docker", "")
        for arch in (None, "amd64"):
            with self.subTest(arch=arch):
                platform = {} if arch is None else {"TARGETARCH": arch}
                result = self.run_shell('bash scripts/build-transcriber.sh registry.invalid/maintainedmost/calls-transcriber:v1.5.0', CONTAINER_ENGINE=str(engine), CI="true", **platform)
                self.assertEqual(result.returncode, 0, result.stderr)
                command = [command for command in self.commands() if command[0] == "make"][-1]
                self.assertIn("docker-build", command)
                self.assertIn("CI=false", command)
                self.assertIn("DOCKER_BUILD_OUTPUT_TYPE=docker", command)
                self.assertIn("ARCH=amd64", command)
                self.assertIn("DOCKER_BUILD_PLATFORMS=linux/amd64", command)
                self.assertIn("DOCKER_TAG=registry.invalid/maintainedmost/calls-transcriber:v1.5.0", command)

    def test_transcriber_arm64_uses_native_platform_and_rebranded_default(self):
        self.patches("transcriber")
        self.stub("make", "")
        engine = self.stub("docker", "")
        result = self.run_shell('bash scripts/build-transcriber.sh', TARGETARCH="arm64", CONTAINER_ENGINE=str(engine), CI="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(command for command in self.commands() if command[0] == "make")
        for argument in ("docker-build", "ARCH=arm64", "DOCKER_BUILD_PLATFORMS=linux/arm64", "DOCKER_TAG=maintainedmost/calls-transcriber:v1.0.0-dev0", "CI=false", "DOCKER_BUILD_OUTPUT_TYPE=docker"):
            self.assertIn(argument, command)

    def test_transcriber_invalid_platform_fails_before_engine_or_checkout(self):
        for arch in ("", "x86_64", "aarch64", "arm/v7", "linux/arm64", "arm64 amd64"):
            with self.subTest(arch=arch):
                result = self.run_shell('bash scripts/build-transcriber.sh', TARGETARCH=arch)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported TARGETARCH", result.stderr)
                self.assertFalse(self.log.exists())
                self.assertFalse((self.root / "build").exists())

    def test_image_cross_compiles_and_bakes_matching_transcriber(self):
        for name in ("Containerfile", "config/overrides.json", "config/image-context.ignore", "scripts/merge-config.py"):
            destination = self.root / name
            destination.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / name, destination)
        server = self.stub("build-server.sh", """
Path(sys.argv[1]).write_text('binary')
with open(os.environ['COMMAND_LOG'], 'a') as log:
    log.write(json.dumps(['target', os.environ['GOOS'], os.environ['GOARCH'], os.environ['CGO_ENABLED'], os.environ['SKIP_WEBAPP'], os.environ['GOTOOLCHAIN']]) + '\\n')
""")
        calls = self.stub("build-calls.sh", """
Path(sys.argv[1]).mkdir(parents=True)
(Path(sys.argv[1]) / 'maintainedmost-calls-1000.9.0.tar.gz').write_text('bundle')
""")
        transcriber = self.stub("build-transcriber.sh", "assert os.environ['TARGETARCH'] == 'amd64'\n")
        for script in (server, calls, transcriber):
            shutil.copy2(script, self.root / "scripts" / script.name)
        engine = self.stub("docker", "")
        result = self.run_shell('bash scripts/build-image.sh registry.invalid/maintainedmost:v1.5.0', CONTAINER_ENGINE=str(engine), SOURCE_URL="https://github.com/fork-owner/maintainedmost", GOOS="darwin", GOARCH="arm64", TARGETARCH="arm64", CGO_ENABLED="1", SKIP_WEBAPP="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertIn(["target", "linux", "amd64", "0", "0", "go1.26.7"], commands)
        self.assertIn(["build-transcriber.sh", "registry.invalid/maintainedmost/calls-transcriber:v1.5.0"], commands)
        image = next(command for command in commands if command[:2] == ["docker", "build"])
        self.assertIn("TRANSCRIBER_IMAGE=registry.invalid/maintainedmost/calls-transcriber:v1.5.0", image)
        self.assertIn("CALLS_IMAGE_REGISTRY=registry.invalid/maintainedmost", image)
        self.assertIn("SOURCE_URL=https://github.com/fork-owner/maintainedmost", image)
        self.assertIn("linux/amd64", image)
        pull = next(command for command in commands if command[:2] == ["docker", "pull"])
        self.assertEqual(pull[2:4], ["--platform", "linux/amd64"])
        self.assertIn("@sha256:", pull[-1])
        self.assertIn(["docker", "tag", pull[-1], "registry.invalid/maintainedmost/calls-recorder:v0.8.13"], commands)
        self.assertFalse(any(command[:2] == ["docker", "push"] for command in commands))
        for source in ({}, {"SOURCE_URL": ""}):
            with self.subTest(source=source):
                self.log.unlink()
                result = self.run_shell('bash scripts/build-image.sh', CONTAINER_ENGINE=str(engine), **source)
                self.assertEqual(result.returncode, 0, result.stderr)
                commands = self.commands()
                image = next(command for command in commands if command[:2] == ["docker", "build"])
                self.assertIn("maintainedmost:dev", image)
                self.assertIn("SOURCE_URL=", image)
                self.assertIn("CALLS_IMAGE_REGISTRY=maintainedmost", image)
                self.assertIn("TRANSCRIBER_IMAGE=maintainedmost/calls-transcriber:v1.0.0-dev0", image)
                self.assertIn(["build-transcriber.sh", "maintainedmost/calls-transcriber:v1.0.0-dev0"], commands)
                stage = self.root / "build/image"
                self.assertEqual((stage / "maintainedmost-server").read_text(), "binary")
                self.assertEqual((stage / "maintainedmost-calls.tar.gz").read_text(), "bundle")
                self.assertEqual((stage / ".dockerignore").read_bytes(), (ROOT / "config/image-context.ignore").read_bytes())
                self.assertFalse(any(command[:2] == ["docker", "push"] for command in commands))

    def test_invalid_image_override_fails_before_work_or_engine_side_effects(self):
        stage = self.root / "build/image"
        stage.mkdir(parents=True)
        (stage / "keep").write_text("previous output")
        result = self.run_shell('bash scripts/build-image.sh registry.invalid/maintainedmost:v1.5.0', TRANSCRIBER_IMAGE="registry.invalid/maintainedmost:v1.5.0-transcriber")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual((stage / "keep").read_text(), "previous output")

    def test_standalone_transcriber_rejects_unsupported_ref_before_checkout(self):
        result = self.run_shell('bash scripts/build-transcriber.sh maintainedmost:dev-transcriber')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())

    def test_calls_checks_manifest_recorder_version_before_building(self):
        self.patches("calls")
        pins = self.root / "upstream.env"
        pins.write_text(pins.read_text().replace("RECORDER_TAG=v0.8.13", "RECORDER_TAG=v0.8.14"))
        result = self.run_shell('bash scripts/build-calls.sh output')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Calls selects recorder v0.8.13", result.stderr)
        self.assertFalse(any(command[0] in ("npm", "go", "make") for command in self.commands()))

    def test_calls_build_uses_host_platform_for_helpers(self):
        self.patches("calls")
        self.stub("npm", "")
        self.stub("go", """
if sys.argv[1:] == ['env', 'GOHOSTOS']: print('darwin')
elif sys.argv[1:] == ['env', 'GOHOSTARCH']: print('arm64')
else: sys.exit(1)
""")
        self.stub("make", """
import tarfile
assert os.environ['GOOS'] == 'darwin'
assert os.environ['GOARCH'] == 'arm64'
work = Path(sys.argv[2])
(work / 'dist').mkdir()
with tarfile.open(work / 'dist/calls.tar.gz', 'w:gz') as bundle:
    bundle.add(work / 'plugin.json', arcname='com.mattermost.calls/plugin.json')
""")
        for source_url in (None, "", "https://github.com/fork-owner/maintainedmost/"):
            with self.subTest(source_url=source_url):
                source = {} if source_url is None else {"SOURCE_URL": source_url}
                result = self.run_shell('bash scripts/build-calls.sh output', GOOS="linux", GOARCH="amd64", CGO_ENABLED="0", GOTOOLCHAIN="local", **source)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(["make", "-C", str((self.root / "build/calls").resolve()), "dist"], self.commands())
                target = self.root / "output/maintainedmost-calls-1000.9.0.tar.gz"
                with tarfile.open(target) as bundle:
                    manifest = json.load(bundle.extractfile("com.mattermost.calls/plugin.json"))
                self.assertEqual(manifest["id"], "com.mattermost.calls")
                self.assertEqual(manifest["name"], "Calls (MaintainedMost)")
                self.assertEqual(manifest["version"], "1000.9.0")
                self.assertEqual(manifest["props"]["calls_recorder_version"], "v0.8.13")
                if source_url:
                    self.assertEqual(manifest["homepage_url"], source_url.rstrip("/"))
                    self.assertEqual(manifest["support_url"], source_url.rstrip("/") + "/issues")
                else:
                    self.assertNotIn("homepage_url", manifest)
                    self.assertNotIn("support_url", manifest)
                digest, name = target.with_suffix(".gz.sha256").read_text().split()
                self.assertEqual(name, target.name)
                self.assertEqual(digest, hashlib.sha256(target.read_bytes()).hexdigest())

    def test_server_output_does_not_delete_an_unrelated_client_directory(self):
        self.patches()
        brand = self.stub("brand-assets.sh", "")
        shutil.copy2(brand, self.root / "scripts/brand-assets.sh")
        self.stub("go", "if 'build' in sys.argv: Path(sys.argv[sys.argv.index('-o') + 1]).write_text('binary')\n")
        self.stub("npm", "")
        client = self.root / "new output/client"
        client.mkdir(parents=True)
        (client / "keep").write_text("user data")
        result = self.run_shell('bash scripts/build-server.sh "new output/server"', SKIP_WEBAPP="0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to replace non-generated", result.stderr)
        self.assertEqual((client / "keep").read_text(), "user data")

    def test_server_can_replace_its_own_generated_webapp(self):
        self.patches()
        brand = self.stub("brand-assets.sh", "")
        shutil.copy2(brand, self.root / "scripts/brand-assets.sh")
        self.stub("go", "if 'build' in sys.argv: Path(sys.argv[sys.argv.index('-o') + 1]).write_text('binary')\n")
        self.stub("npm", "")
        client = self.root / "new output/client"
        client.mkdir(parents=True)
        (client / ".maintainedmost-generated").touch()
        (client / "stale").write_text("old bundle")
        result = self.run_shell('bash scripts/build-server.sh "new output/server"', SKIP_WEBAPP="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((client / "stale").exists())
        self.assertEqual((client / "index.html").read_text(), "built webapp")
        self.assertTrue((client / ".maintainedmost-generated").is_file())

    def test_server_gate_runs_app_api4_full_oidc_and_config_suites_with_race(self):
        (self.root / "source/server/public").mkdir(parents=True)
        self.stub("go", """
if sys.argv[1:] == ['env', 'GOHOSTOS']: print('linux')
elif sys.argv[1:] == ['env', 'GOHOSTARCH']: print('amd64')
elif sys.argv[1] == 'test':
    suite = {'./channels/api4': 'server-api', './channels/app/oauthproviders/openid': 'server-oidc', './config': 'server-config', './model': 'server-model'}[sys.argv[-1]]
    inventory = json.loads((Path(os.environ['COMMAND_LOG']).parent / 'maintenance/required-tests.json').read_text())
    for package, tests in inventory[suite].items():
        for test in tests: print(json.dumps({'Action': 'pass', 'Package': package, 'Test': test}))
        print(json.dumps({'Action': 'pass', 'Package': package}))
""")
        result = self.run_shell('bash scripts/test-component.sh server source', GOTOOLCHAIN="local")
        self.assertEqual(result.returncode, 0, result.stderr)
        suites = [command for command in self.commands() if command[:2] == ["go", "test"]]
        self.assertEqual(len(suites), 4)
        self.assertEqual(suites[0][-2:], ["./channels/app", "./channels/api4"])
        self.assertEqual(suites[0][suites[0].index("-run") + 1], "^TestMaintainedMost")
        self.assertIn("-short", suites[0])
        self.assertEqual(suites[1][-3:], ["-run", ".", "./channels/app/oauthproviders/openid"])
        self.assertEqual(suites[2][-1], "./config")
        self.assertIn("^Test(GetClientConfig|GetLimitedClientConfig)$", suites[2])
        self.assertIn("-short", suites[2])
        self.assertEqual(suites[3][-1], "./model")
        for suite in suites:
            self.assertIn("-race", suite)
            self.assertIn("-count=1", suite)

    def test_calls_generates_manifests_then_runs_affected_go_and_all_jest_tests(self):
        source = self.root / "source"
        (source / "webapp/node_modules/.bin").mkdir(parents=True)
        jest = self.stub("jest", "")
        shutil.copy2(jest, source / "webapp/node_modules/.bin/jest")
        self.stub("make", """
assert os.environ['GOTOOLCHAIN'] == 'go1.26.7'
if sys.argv[1] == 'setup-go-work': Path('go.work').write_text('go 1.26.7')
elif sys.argv[1] == 'apply':
    assert Path('go.work').exists()
    Path('server').mkdir(exist_ok=True)
    Path('server/manifest.go').touch()
    Path('webapp/src').mkdir(exist_ok=True)
    Path('webapp/src/manifest.ts').touch()
""")
        self.stub("go", """
if sys.argv[1:] == ['env', 'GOHOSTOS']: print('linux')
elif sys.argv[1:] == ['env', 'GOHOSTARCH']: print('amd64')
elif sys.argv[1] == 'test':
    assert Path('server/manifest.go').exists()
    assert Path('webapp/src/manifest.ts').exists()
    inventory = json.loads((Path(os.environ['COMMAND_LOG']).parent / 'maintenance/required-tests.json').read_text())
    for package, tests in inventory['calls'].items():
        for test in tests: print(json.dumps({'Action': 'pass', 'Package': package, 'Test': test}))
        print(json.dumps({'Action': 'pass', 'Package': package}))
""")
        result = self.run_shell('bash scripts/test-component.sh calls source', GOTOOLCHAIN="local")
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.commands()
        self.assertLess(commands.index(["make", "setup-go-work"]), commands.index(["make", "apply"]))
        suite = next(command for command in commands if command[:2] == ["go", "test"])
        self.assertLess(commands.index(["make", "apply"]), commands.index(suite))
        self.assertEqual(suite[-2:], ["./server", "./server/license"])
        pattern = suite[suite.index("-run") + 1]
        for name in ("MaintainedMost", "GetClientConfig", "AddUserSession", "HandleJoin", "HandleBotGetProfileForSession", "HandleBotUploadData", "ConfigurationIsValid", "ConfigurationWillBeSaved", "ApplyEnvOverrides", "FieldNameToEnvKey", "SetFieldFromEnv", "SetOverridesDeprecatedRTCDURL", "JobServiceApplyEnvOverrides"):
            self.assertRegex("Test" + name, pattern)
        self.assertIn(["jest", "--ci", "--runInBand", "--coverage=false"], commands)


class ImageContextTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("docker"), "Docker is required for the actual context-filter check")
    def test_only_declared_image_inputs_reach_the_builder(self):
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                          "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"}}
        available = subprocess.run(["docker", "info"], env=env, capture_output=True, timeout=20)
        if available.returncode:
            self.skipTest("Docker engine is unavailable")
        with tempfile.TemporaryDirectory(prefix="maintainedmost-context-") as temporary:
            root = Path(temporary).resolve()
            context, output = root / "context", root / "output"
            context.mkdir()
            allowed = {
                "Containerfile": "FROM scratch\nCOPY . /\n",
                "maintainedmost-server": "synthetic binary\n",
                "maintainedmost-calls.tar.gz": "synthetic bundle\n",
                "client/index.html": "public client\n",
                "config/overrides.json": "{}\n",
                "scripts/merge-config.py": "# public helper\n",
            }
            excluded = (".env", "secrets/token", "models.json", "client/.env", "client/secrets/token",
                        "config/private.json", "config/local.json", "scripts/private.json",
                        "scripts/nested/private.json", "plugin/unused.tar.gz")
            for name, content in {**allowed, **dict.fromkeys(excluded, "synthetic excluded marker\n")}.items():
                path = context / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            shutil.copy2(ROOT / "config/image-context.ignore", context / ".dockerignore")
            result = subprocess.run(["docker", "build", "--output", "type=local,dest=" + str(output),
                                     "--file", str(context / "Containerfile"), str(context)],
                                    env=env, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, "actual Docker context verification failed")
            actual = {str(path.relative_to(output)) for path in output.rglob("*") if path.is_file()}
            self.assertEqual(actual, set(allowed))


class ImageRefsTests(unittest.TestCase):
    def mapping(self, server, registry="", transcriber=""):
        return subprocess.run([sys.executable, str(ROOT / "scripts/image-refs.py"), server, "v0.8.13", registry, transcriber], text=True, capture_output=True)

    def test_release_dev_ci_and_registry_port_mapping(self):
        for server, registry, version in (
            ("ghcr.io/owner/maintainedmost:v1.5.0", "ghcr.io/owner/maintainedmost", "v1.5.0"),
            ("maintainedmost:dev", "maintainedmost", "v1.0.0-dev0"),
            ("maintainedmost:ci", "maintainedmost", "v1.0.0-dev0"),
            ("localhost:5000/team/maintainedmost:test-abcdef", "localhost:5000/team/maintainedmost", "v1.0.0-dev0"),
        ):
            with self.subTest(server=server):
                result = self.mapping(server)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.split(), [registry, f"{registry}/calls-transcriber:{version}", f"{registry}/calls-recorder:v0.8.13"])

    def test_explicit_shared_namespace_and_valid_dev_override(self):
        registry = "registry.example:5000/jobs"
        transcriber = registry + "/calls-transcriber:v1.2.3-dev4"
        result = self.mapping("maintainedmost:dev", registry, transcriber)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.split(), [registry, transcriber, registry + "/calls-recorder:v0.8.13"])

    def test_invalid_server_and_registry_refs_are_rejected(self):
        for server in ("maintainedmost", "localhost:5000/maintainedmost", "UPPER/maintainedmost:dev", "https://registry/repo:dev", "repo@sha256:" + "a" * 64, "repo:bad tag", "repo:v0.0.0"):
            with self.subTest(server=server):
                self.assertNotEqual(self.mapping(server).returncode, 0)
        for registry in ("registry/", "https://registry/repo", "registry:bad/repo", "registry:0/repo", "registry:65536/repo", "bad namespace", "registry/repo:tag"):
            with self.subTest(registry=registry):
                self.assertNotEqual(self.mapping("repo:dev", registry).returncode, 0)

    def test_rejected_transcriber_overrides_never_silently_fall_back(self):
        registry = "ghcr.io/owner/maintainedmost"
        for image in (
            registry + ":v1.5.0-transcriber",
            registry + "/calls-transcriber:latest",
            registry + "/calls-transcriber:v0.0.0-dev0",
            registry + "/calls-transcriber:v1.5.0-rc1",
            registry + "/calls-transcriber:v01.5.0",
            registry + "/calls-transcriber:v9223372036854775808.0.0",
            registry + "/calls-transcriber@sha256:" + "a" * 64,
            "other/namespace/calls-transcriber:v1.5.0",
            registry + "/calls-recorder:v1.5.0",
            registry + "/calls-transcriber:v2.0.0",
        ):
            with self.subTest(image=image):
                result = self.mapping(registry + ":v1.5.0", "", image)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


class TestGateTests(unittest.TestCase):
    def test_required_test_results_cannot_be_empty_or_skipped(self):
        for action, success in (("pass", True), ("skip", False), ("fail", False), (None, False)):
            with self.subTest(action=action):
                event = {"Action": action, "Test": "TestMaintainedMostRegression"} if action else {"Action": "pass", "Package": "example"}
                result = subprocess.run([sys.executable, str(ROOT / "scripts/check-go-tests.py"), "^TestMaintainedMost"], input=json.dumps(event) + "\n", text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, success, result.stderr)

    def test_previous_test_prefix_cannot_satisfy_the_rebranded_gate(self):
        event = {"Action": "pass", "Test": "TestMattermoreRegression"}
        result = subprocess.run([sys.executable, str(ROOT / "scripts/check-go-tests.py"), "^TestMaintainedMost"], input=json.dumps(event) + "\n", text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0 passed", result.stderr)

    def test_active_infrastructure_has_no_retired_project_links(self):
        paths = [ROOT / name for name in ("Containerfile", "compose.yaml", ".env.example", "upstream.env", "config/overrides.json")]
        paths.extend(path for path in (ROOT / "scripts").iterdir() if path.is_file())
        paths.extend(path for path in (ROOT / ".github").rglob("*") if path.is_file())
        paths.extend((ROOT / "patches").glob("*/*.patch"))
        retired_links = r"(?i)\b(?:[a-z0-9-]+\.)?mattermore\.dev\b|(?:github\.com|raw\.githubusercontent\.com|ghcr\.io)/dennisklappe/mattermore\b"
        upstream_pointer = "https://github.com/dennisklappe/mattermore.git"
        for path in paths:
            with self.subTest(path=path.relative_to(ROOT)):
                content = path.read_text()
                if path.suffix == ".patch":
                    # Removed lines describe upstream/history, not shipped behavior.
                    content = "\n".join(line for line in content.splitlines() if not line.startswith("-"))
                if path == ROOT / ".github/vibeci.jsonc":
                    # The canonical maintenance config must name the upstream
                    # it merges from; that functional pointer is not a retired link.
                    content = content.replace(upstream_pointer, "")
                self.assertNotRegex(content, retired_links)

    def test_retired_website_has_no_workflow_hooks(self):
        for name in ("site", "worker", ".github/workflows/site.yml", ".github/workflows/uptime.yml"):
            self.assertFalse((ROOT / name).exists(), name)
        for path in (ROOT / ".github").rglob("*"):
            if path.is_file():
                with self.subTest(path=path.relative_to(ROOT)):
                    self.assertNotRegex(path.read_text(), r"site/|worker/|wrangler|upload-pages-artifact|deploy-pages")

    def test_branding_patch_preserves_upstream_and_original_fork_attribution(self):
        patch = (ROOT / "patches/server/0008-maintainedmost-branding.patch").read_text()
        notices = (
            " // Copyright (c) 2015-present Mattermost, Inc. All Rights Reserved.\n"
            "+// Copyright (c) 2026-present Mattermore contributors. All Rights Reserved.\n"
            " // See LICENSE.txt for license information.\n"
        )
        self.assertEqual(patch.count(notices), 2)
        self.assertNotIn("-// Copyright", patch)
        self.assertNotRegex(patch, r"Copyright[^\n]*MaintainedMost")
        for number in range(3, 7):
            historical = next((ROOT / "patches/server").glob(f"{number:04}-*.patch"))
            self.assertIn("From: Dennis Klappe <dennisklappe@gmail.com>\n", historical.read_text())

    def test_inline_wordmark_uses_the_artwork_canvas(self):
        artwork = (ROOT / "brand/wordmark-dark.svg").read_text()
        canvas = re.search(r'viewBox="([^"]+)"', artwork).group(1)
        _, _, width, height = canvas.split()
        patch = (ROOT / "patches/server/0008-maintainedmost-branding.patch").read_text()
        self.assertTrue(f"+        viewBox='{canvas}'\n" in patch, f"inline wordmark must use the artwork canvas: {canvas}")
        self.assertEqual(patch.count(f"+                <rect width='{width}' height='{height}' fill='#fff'/>"), 2)

    def test_transcriber_backend_example_uses_a_patched_image(self):
        patch = (ROOT / "patches/transcriber/0001-openai-compatible-backend.patch").read_text()
        example = next(line for line in patch.splitlines() if line.startswith("+docker run ") and "TRANSCRIBE_API=openai/api" in line)
        self.assertEqual(example.rsplit(" ", 1)[-1], "maintainedmost/calls-transcriber:v1.0.0-dev0")

    def test_runtime_brand_defaults_preserve_official_app_downloads(self):
        config = json.loads((ROOT / "config/overrides.json").read_text())
        self.assertEqual(config["TeamSettings"]["SiteName"], "MaintainedMost")
        self.assertEqual(config["SupportSettings"], dict.fromkeys(("AboutLink", "HelpLink", "ReportAProblemLink", "TermsOfServiceLink", "PrivacyPolicyLink"), ""))
        self.assertEqual(config["NativeAppSettings"], {
            "AppDownloadLink": "https://mattermost.com/pl/download-apps",
            "AndroidAppDownloadLink": "https://play.google.com/store/apps/details?id=com.mattermost.rn",
            "IosAppDownloadLink": "https://apps.apple.com/app/mattermost/id1257222717",
        })
        container = (ROOT / "Containerfile").read_text()
        self.assertIn('ARG SOURCE_URL=\n', container)
        self.assertIn('org.opencontainers.image.source="${SOURCE_URL}"', container)
        self.assertIn('org.opencontainers.image.url="${SOURCE_URL}"', container)
        self.assertIn('org.opencontainers.image.title="MaintainedMost"', container)

    def test_compose_project_override_keeps_volume_keys_and_job_network_in_sync(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("name: ${COMPOSE_PROJECT_NAME:-maintainedmost}\n", compose)
        self.assertIn("DOCKER_NETWORK: ${COMPOSE_PROJECT_NAME:-maintainedmost}_default\n", compose)
        self.assertIn("  maintainedmost:\n    image: ${MAINTAINEDMOST_IMAGE:-ghcr.io/vibeci/maintainedmost:latest}\n", compose)
        self.assertIn("MM_CALLS_JOB_SERVICE_IMAGE_REGISTRY: ${CALLS_IMAGE_REGISTRY:-ghcr.io/vibeci/maintainedmost}\n", compose)
        self.assertIn("JOBS_IMAGEREGISTRY: ${CALLS_IMAGE_REGISTRY:-ghcr.io/vibeci/maintainedmost}\n", compose)
        self.assertNotIn("${MAINTAINEDMOST_IMAGE:?", compose)
        self.assertNotIn("${CALLS_IMAGE_REGISTRY:?", compose)
        self.assertNotIn("ghcr.io/OWNER/", compose)
        self.assertIn("POSTGRES_DB: mattermost\n", compose)
        volume_keys = re.findall(r"^  ([a-z-]+):$", compose.split("\nvolumes:\n", 1)[1], re.M)
        self.assertEqual(volume_keys, ["postgres", "mattermost-data", "mattermost-config", "mattermost-logs", "mattermost-plugins", "mattermost-client-plugins", "offloader"])
        example = (ROOT / ".env.example").read_text()
        self.assertIn("COMPOSE_PROJECT_NAME=maintainedmost\n", example)
        self.assertIn("MAINTAINEDMOST_IMAGE=ghcr.io/vibeci/maintainedmost:latest\n", example)
        self.assertIn("CALLS_IMAGE_REGISTRY=ghcr.io/vibeci/maintainedmost\n", example)
        self.assertNotIn("ghcr.io/OWNER/", example)
        # Operator docs must stay in sync with the shipped defaults so a
        # compose/.env change without a docs update (or vice versa) fails fast.
        selfhost = (ROOT / "docs/selfhost.md").read_text()
        self.assertIn("MAINTAINEDMOST_IMAGE=ghcr.io/vibeci/maintainedmost:latest\n", selfhost)
        self.assertIn("CALLS_IMAGE_REGISTRY=ghcr.io/vibeci/maintainedmost\n", selfhost)
        self.assertNotIn("ghcr.io/OWNER/", selfhost)

    def test_prepackaged_skip_does_not_skip_rebranded_calls(self):
        container = (ROOT / "Containerfile").read_text()
        skip = re.search(r"^ENV MM_PREPACKAGED_PLUGINS_SKIP=(.*)$", container, re.M).group(1).split(",")
        bundle = re.search(r"/mattermost/prepackaged_plugins/([^\s]+)", container).group(1)
        self.assertEqual(bundle, "maintainedmost-calls-linux-amd64.tar.gz")
        self.assertFalse(bundle.startswith(tuple(skip)))
        for upstream in ("mattermost-plugin-calls-linux-amd64.tar.gz", "mattermost-plugin-playbooks-linux-amd64.tar.gz"):
            self.assertTrue(upstream.startswith(tuple(skip)))

    def test_release_reuses_matching_rebranded_artifacts(self):
        build = (ROOT / ".github/workflows/build.yml").read_text()
        release = (ROOT / ".github/workflows/release.yml").read_text()
        for name in ("maintainedmost-calls-plugin", "maintainedmost-release-images"):
            self.assertIn("name: " + name + "\n", build)
            self.assertIn("name: " + name + "\n", release)
        self.assertIn('docker save "$IMAGE_TAG" "$TRANSCRIBER_IMAGE" "$RECORDER_IMAGE" -o "$RUNNER_TEMP/maintainedmost-images.tar"', build)
        self.assertIn("docker load -i dist/images/maintainedmost-images.tar", release)
        self.assertIn("SOURCE_URL: ${{ github.server_url }}/${{ github.repository }}", build)
        self.assertIn("uses: ./.github/workflows/build.yml", release)
        self.assertIn('image="ghcr.io/${REPOSITORY,,}:$tag"', release)
        self.assertIn("tag=\"test-$GITHUB_SHA\"", release)

    def test_runtime_pin_matches_server_source_version(self):
        pins = dict(re.findall(r"^([A-Z_]+)=(.*)$", (ROOT / "upstream.env").read_text(), re.MULTILINE))
        self.assertRegex(pins["TRANSCRIBER_TAG"], r"^[0-9a-f]{40}$")
        self.assertRegex(pins["SERVER_COMMIT"], r"^[0-9a-f]{40}$")
        self.assertRegex(pins["CALLS_COMMIT"], r"^[0-9a-f]{40}$")
        self.assertGreater(tuple(map(int, pins["CALLS_VERSION"].split("."))), (1000, 12, 3))
        self.assertIn(":" + pins["SERVER_TAG"].removeprefix("v") + "@sha256:", pins["SERVER_IMAGE"])
        self.assertIn("ARG SERVER_IMAGE=" + pins["SERVER_IMAGE"], (ROOT / "Containerfile").read_text())
        self.assertIn("calls-recorder:" + pins["RECORDER_TAG"] + "@sha256:", pins["RECORDER_SOURCE_IMAGE"])

    def test_entrypoint_scripts_are_executable(self):
        for name in ("build-server.sh", "build-calls.sh", "build-image.sh", "build-transcriber.sh", "check-upstream.sh", "test-component.sh", "test-offloader.sh"):
            with self.subTest(name=name):
                self.assertTrue(os.access(ROOT / "scripts" / name, os.X_OK))


if __name__ == "__main__":
    unittest.main()
