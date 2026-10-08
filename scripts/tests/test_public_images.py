import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


class PublicImageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="maintainedmost-public-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / "tool bin"
        self.bin.mkdir()
        self.tmp = self.root / "temporary files"
        self.tmp.mkdir()
        self.auth = self.root / "authenticated Docker"
        self.auth.mkdir()
        self.auth_file = self.auth / "config.json"
        self.auth_contents = '{"auths":{"ghcr.io":{"auth":"fixture-credential"}},"credsStore":"fixture"}'
        self.auth_file.write_text(self.auth_contents)
        (self.root / "scripts").mkdir()
        self.checker = self.root / "scripts/check-public-images.sh"
        shutil.copy2(ROOT / "scripts/check-public-images.sh", self.checker)
        self.log = self.root / "commands.jsonl"
        self.recorder = "ghcr.io/example/maintainedmost/calls-recorder:v0.8.13"
        self.transcriber = "ghcr.io/example/maintainedmost/calls-transcriber:v1.5.0"
        self.image = "ghcr.io/example/maintainedmost:v1.5.0"
        self.env = {
            **os.environ,
            "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
            "TMPDIR": str(self.tmp),
            "HOME": str(self.root),
            "DOCKER_CONFIG": str(self.auth),
            "DOCKER_AUTH_CONFIG": "fixture-auth-override",
            "DOCKER_CUSTOM_HEADERS": "Authorization=fixture-header",
            "REGISTRY_AUTH_FILE": str(self.auth_file),
            "GH_TOKEN": "fixture-gh-secret",
            "GITHUB_TOKEN": "fixture-github-secret",
            "IMAGE": self.image,
            "RECORDER_IMAGE": self.recorder,
            "TRANSCRIBER_IMAGE": self.transcriber,
            "RELEASE_TAG": "v1.5.0",
        }
        self.tools()

    def tools(self, fail_ref=None):
        docker = self.bin / "docker"
        docker.write_text(f"""#!{sys.executable}
import json, os, stat, sys
from pathlib import Path
args = sys.argv[1:]
record = {{'tool': 'docker', 'args': args}}
if 'inspect' in args:
    config = Path(args[args.index('--config') + 1])
    record.update(config=str(config), files=sorted(p.name for p in config.iterdir()),
                  mode=stat.S_IMODE(config.stat().st_mode), environment=dict(os.environ))
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps(record) + '\\n')
if 'inspect' in args and args[-1] == {fail_ref!r}:
    sys.exit(23)
""")
        docker.chmod(0o755)
        gh = self.bin / "gh"
        gh.write_text(f"""#!{sys.executable}
import json, sys
with open({str(self.log)!r}, 'a') as log:
    log.write(json.dumps({{'tool': 'gh', 'args': sys.argv[1:]}}) + '\\n')
""")
        gh.chmod(0o755)

    def run_check(self, *refs):
        return subprocess.run([str(self.checker), *refs], cwd=self.root, env=self.env, text=True, capture_output=True)

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_anonymous_config_and_environment_are_isolated(self):
        result = self.run_check(self.recorder, self.transcriber)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.commands()), 2)
        for call, ref in zip(self.commands(), (self.recorder, self.transcriber)):
            config = Path(call["config"])
            self.assertNotEqual(config, self.auth)
            self.assertEqual(call["files"], [])
            self.assertEqual(call["mode"], 0o700)
            self.assertEqual(call["args"], ["--config", str(config), "manifest", "inspect", "--", ref])
            self.assertEqual(call["environment"]["HOME"], str(config))
            self.assertEqual(call["environment"]["PATH"], str(config))
            for key in ("GH_TOKEN", "GITHUB_TOKEN", "DOCKER_CONFIG", "DOCKER_AUTH_CONFIG", "DOCKER_CUSTOM_HEADERS", "REGISTRY_AUTH_FILE"):
                self.assertNotIn(key, call["environment"])
            self.assertFalse(config.exists(), "anonymous config must be removed after success")
        self.assertEqual(self.auth_file.read_text(), self.auth_contents)

    def test_refs_are_quoted_and_cannot_be_cli_options(self):
        marker = self.root / "must-not-exist"
        refs = ("registry.example:5000/team/image:v1.5.0", f"literal space*;$(touch {marker})", "--help")
        result = self.run_check(*refs)
        self.assertEqual(result.returncode, 0, result.stderr)
        for call, ref in zip(self.commands(), refs):
            self.assertEqual(call["args"][2:], ["manifest", "inspect", "--", ref])
        self.assertFalse(marker.exists())

    def test_unavailable_image_fails_closed_checks_all_refs_and_cleans_up(self):
        self.tools(fail_ref=self.recorder)
        result = self.run_check(self.recorder, self.transcriber)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual([call["args"][-1] for call in self.commands()], [self.recorder, self.transcriber])
        for call in self.commands():
            self.assertFalse(Path(call["config"]).exists())
        self.assertIn(self.recorder, result.stderr)
        self.assertIn("Public in Package settings", result.stderr)
        self.assertIn("correct repository", result.stderr)
        self.assertIn("existing draft release", result.stderr)
        self.assertNotIn("fixture-gh-secret", result.stdout + result.stderr)
        self.assertEqual(self.auth_file.read_text(), self.auth_contents)

    def test_no_refs_is_not_a_successful_availability_check(self):
        result = self.run_check()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_real_release_commands_enforce_order_and_failure_barriers(self):
        # Execute only the promotion run blocks, with mocked Docker/GitHub commands.
        # The actual checker runs, including its credential isolation and exit status.
        lines = (ROOT / ".github/workflows/release.yml").read_text().splitlines()
        names = ("Publish and verify job images", "Publish and verify server", "Promote latest image", "Publish complete release")
        positions = [lines.index("      - name: " + name) for name in names]
        self.assertEqual(positions, sorted(positions))
        commands = []
        for position in positions:
            block = []
            for line in lines[position + 1:]:
                if line.startswith("      - "):
                    break
                block.append(line)
            self.assertFalse(any(line.startswith("        if:") or "continue-on-error:" in line for line in block))
            run = next(index for index, line in enumerate(block) if line.startswith("        run: "))
            if block[run] == "        run: |":
                commands.extend(line[10:] for line in block[run + 1:] if line.strip())
            else:
                commands.append(block[run].removeprefix("        run: "))
        script = "set -euo pipefail\n" + "\n".join(commands)
        complete = [
            ("push", self.recorder), ("push", self.transcriber),
            ("inspect", self.recorder), ("inspect", self.transcriber),
            ("push", self.image), ("inspect", self.image),
            ("tag", "ghcr.io/example/maintainedmost:latest"),
            ("push", "ghcr.io/example/maintainedmost:latest"),
            ("release", "--latest"),
        ]
        for failed_ref, expected in ((None, complete), (self.recorder, complete[:4]), (self.transcriber, complete[:4]), (self.image, complete[:6])):
            with self.subTest(failed_ref=failed_ref):
                self.log.unlink(missing_ok=True)
                self.tools(fail_ref=failed_ref)
                result = subprocess.run(["bash", "-c", script], cwd=self.root, env=self.env, text=True, capture_output=True)
                self.assertEqual(result.returncode == 0, failed_ref is None, result.stderr)
                events = [("inspect" if "inspect" in call["args"] else call["args"][0], call["args"][-1]) for call in self.commands()]
                self.assertEqual(events, expected)


if __name__ == "__main__":
    unittest.main()
