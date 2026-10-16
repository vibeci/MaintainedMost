import re
import shutil
import subprocess
import tempfile
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]

BASE_ENV = {
    # Mirrors the live pins so the copied prose files match the fixture.
    "SERVER_TAG": "v12.0.0",
    "SERVER_COMMIT": "1f5e0e9ba40da7ba0741659346e0a385a623d6a4",
    "SERVER_IMAGE": "docker.io/mattermost/mattermost-team-edition:12.0.0@sha256:c213d4eb01c2973a3814f57dab4e0629287b92896cb2f98d27134aa62ee5c3cc",
    "CALLS_TAG": "v1.12.5",
    "CALLS_COMMIT": "24414f57bca4332e53addc73dbbfa832e0b4d895",
    "RECORDER_TAG": "v0.8.13",
    "RECORDER_SOURCE_IMAGE": "docker.io/mattermost/calls-recorder:v0.8.13@sha256:8532abe26d24a6b8dbe0efc154b60aad3f72d77ab1b9128c6cd793b138e61ab2",
    "OFFLOADER_TAG": "v0.9.6",
    "OFFLOADER_COMMIT": "f239bd8c53b8281514cb354a988359b517d3df8c",
    "TRANSCRIBER_TAG": "c09a9a3c88ea29b2560002999f9b48e7d92f515f",
    "TRANSCRIBER_BRANCH": "master",
    "GO_VERSION": "1.26.7",
    "NODE_VERSION": "24",
    "CALLS_VERSION": "1000.12.5",
}

# Every file scripts/bump-pins.sh may rewrite, copied from the live tree so the
# fixture matches the real prose layout.
MUTABLE = (
    "upstream.env", "Containerfile", "README.md", "SECURITY.md",
    "CONTRIBUTING.md", "docs/README.md", "docs/install.md",
    "docs/licensing.md", "docs/upgrading.md",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
)


def write_env(path, pins):
    path.write_text("".join(f"{key}={value}\n" for key, value in pins.items()))


class NextVersionTests(unittest.TestCase):
    def run_script(self, old, new, current):
        temp = tempfile.TemporaryDirectory(prefix="maintainedmost-version-")
        self.addCleanup(temp.cleanup)
        old_file = Path(temp.name) / "old.env"
        new_file = Path(temp.name) / "new.env"
        write_env(old_file, old)
        write_env(new_file, new)
        result = subprocess.run(
            [str(ROOT / "scripts/next-version.sh"), str(old_file), str(new_file), current],
            capture_output=True, text=True,
        )
        return result.returncode, result.stdout.strip()

    def check(self, current, expected, **changes):
        new = dict(BASE_ENV, **changes)
        code, version = self.run_script(BASE_ENV, new, current)
        self.assertEqual(code, 0)
        self.assertEqual(version, expected)

    def test_server_major_rolls_our_major(self):
        old = dict(BASE_ENV, SERVER_TAG="v11.11.1")
        code, version = self.run_script(old, BASE_ENV, "v1.5.0")
        self.assertEqual((code, version), (0, "v2.0.0"))

    def test_server_minor_rolls_our_minor(self):
        self.check("v2.0.0", "v2.1.0", SERVER_TAG="v12.1.0")

    def test_calls_minor_rolls_our_minor(self):
        self.check("v2.0.0", "v2.1.0", CALLS_TAG="v1.13.0")

    def test_calls_major_rolls_our_minor(self):
        self.check("v2.0.0", "v2.1.0", CALLS_TAG="v2.0.0")

    def test_calls_patch_rolls_our_patch(self):
        self.check("v2.0.0", "v2.0.1", CALLS_TAG="v1.12.6")

    def test_transcriber_only_rolls_patch(self):
        self.check("v2.0.0", "v2.0.1", TRANSCRIBER_TAG="d" * 40)

    def test_toolchain_only_rolls_patch(self):
        self.check("v2.0.0", "v2.0.1", GO_VERSION="1.26.8")

    def test_bundle_only_rolls_patch(self):
        self.check("v2.0.0", "v2.0.1", CALLS_VERSION="1000.12.6")

    def test_combined_major_wins(self):
        old = dict(BASE_ENV, SERVER_TAG="v11.11.1", CALLS_TAG="v1.12.3")
        code, version = self.run_script(old, BASE_ENV, "v1.5.0")
        self.assertEqual((code, version), (0, "v2.0.0"))

    def test_no_change_exits_silently(self):
        code, version = self.run_script(BASE_ENV, BASE_ENV, "v2.0.0")
        self.assertEqual(code, 1)
        self.assertEqual(version, "")

    def test_invalid_current_is_an_error(self):
        code, _ = self.run_script(BASE_ENV, BASE_ENV, "v2.0")
        self.assertEqual(code, 2)


class BumpPinsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maintainedmost-bump-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(ROOT / "scripts/bump-pins.sh", self.root / "scripts/bump-pins.sh")
        for name in MUTABLE:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == "upstream.env":
                write_env(target, BASE_ENV)
            else:
                shutil.copy2(ROOT / name, target)

    def run_bump(self, *args):
        return subprocess.run(
            [str(self.root / "scripts/bump-pins.sh"), *args],
            capture_output=True, text=True, cwd=self.root,
        )

    def test_calls_bump_derives_bundle_and_syncs_prose(self):
        result = self.run_bump(
            "calls", "--tag", "v1.12.6", "--commit", "f" * 40, "--min-server", "12.0.0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        env = (self.root / "upstream.env").read_text()
        self.assertIn("CALLS_TAG=v1.12.6\n", env)
        self.assertIn("CALLS_VERSION=1000.12.6\n", env)
        self.assertIn("built from Calls source **1.12.6**", (self.root / "docs/upgrading.md").read_text())
        self.assertIn("maintainedmost-calls-1000.12.6.tar.gz", (self.root / "docs/install.md").read_text())
        self.assertNotIn("24414f57bca4332e53addc73dbbfa832e0b4d895",
                         (self.root / "docs/licensing.md").read_text())
        for name in MUTABLE:
            if name == "upstream.env":
                continue
            content = (self.root / name).read_text()
            self.assertNotIn("1.12.5", content, name)
            self.assertNotIn("1000.12.5", content, name)

    def test_calls_minor_bump_resets_bundle_base(self):
        result = self.run_bump("calls", "--tag", "v1.13.0", "--commit", "f" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CALLS_VERSION=1000.13.0\n", (self.root / "upstream.env").read_text())

    def test_explicit_bundle_is_honored(self):
        result = self.run_bump(
            "calls", "--tag", "v1.12.5", "--commit", "f" * 40, "--bundle", "1000.12.6",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CALLS_VERSION=1000.12.6\n", (self.root / "upstream.env").read_text())

    def test_minimum_server_guard_leaves_tree_untouched(self):
        before = (self.root / "upstream.env").read_text()
        result = self.run_bump(
            "calls", "--tag", "v1.12.6", "--commit", "f" * 40, "--min-server", "99.0.0",
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs server", result.stderr)
        self.assertEqual((self.root / "upstream.env").read_text(), before)

    def test_server_bump_syncs_container_default(self):
        image = "docker.io/mattermost/mattermost-team-edition:12.1.0@sha256:" + "e" * 64
        result = self.run_bump(
            "server", "--tag", "v12.1.0", "--commit", "e" * 40, "--image", image,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"ARG SERVER_IMAGE={image}", (self.root / "Containerfile").read_text())
        self.assertIn("Mattermost server 12.1.0", (self.root / "docs/licensing.md").read_text())
        self.assertNotIn("12.0.0", (self.root / "docs/licensing.md").read_text())

    def test_server_image_must_match_tag(self):
        result = self.run_bump(
            "server", "--tag", "v12.1.0", "--commit", "e" * 40,
            "--image", "docker.io/mattermost/mattermost-team-edition:12.0.0@sha256:" + "e" * 64,
        )
        self.assertEqual(result.returncode, 2)

    def test_transcriber_bump_rotates_short_shas(self):
        result = self.run_bump("transcriber", "--commit", "a" * 40)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("TRANSCRIBER_TAG=" + "a" * 40, (self.root / "upstream.env").read_text())
        self.assertIn("transcriber aaaaaaa", (self.root / "docs/upgrading.md").read_text())


class PinReferenceTests(unittest.TestCase):
    def test_tracked_references_match_upstream_env(self):
        pins = dict(re.findall(r"^([A-Z_]+)=(.*)$", (ROOT / "upstream.env").read_text(), re.MULTILINE))
        server = pins["SERVER_TAG"].removeprefix("v")
        calls = pins["CALLS_TAG"].removeprefix("v")
        bundle = pins["CALLS_VERSION"]
        short = pins["TRANSCRIBER_TAG"][:7]
        expectations = {
            "README.md": [f"**{server}**", f"**{calls}**", f"**{bundle}**", f"commit **{short}**"],
            "CONTRIBUTING.md": [f"Calls source {calls}", f"server {server}", f"Bundle version {bundle}"],
            "SECURITY.md": [f"server {server}", f"Calls source {calls}", f"bundle {bundle}"],
            "docs/README.md": [bundle, f"Calls source {calls}", f"Server {server}", f"v{server}"],
            "docs/install.md": [f"v{server}", f"bundle version {bundle}", f"maintainedmost-calls-{bundle}.tar.gz"],
            "docs/licensing.md": [f"server {server}", f"Calls {calls}", pins["SERVER_COMMIT"], pins["CALLS_COMMIT"]],
            "docs/upgrading.md": [f"**{bundle}**", f"Calls source **{calls}**", f"server {server}"],
            ".github/ISSUE_TEMPLATE/feature_request.yml": [f"Mattermost {server}", f"Calls {bundle}"],
            ".github/ISSUE_TEMPLATE/bug_report.yml": [f'"{server}"', f'"{bundle}"'],
            ".github/PULL_REQUEST_TEMPLATE.md": [f"Mattermost {server}"],
        }
        for name, needles in expectations.items():
            content = (ROOT / name).read_text()
            for needle in needles:
                with self.subTest(path=name, needle=needle):
                    self.assertIn(needle, content)
        self.assertIn("ARG SERVER_IMAGE=" + pins["SERVER_IMAGE"], (ROOT / "Containerfile").read_text())


class WorkflowStructureTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / ".github/workflows" / name).read_text()

    def test_upgrade_opens_bot_prs_and_routes_conflicts(self):
        workflow = self.read("upgrade.yml")
        self.assertIn("workflow_run", workflow)
        self.assertIn("pull-requests: write", workflow)
        self.assertIn("upgrade-attempt.sh", workflow)
        self.assertIn("bot/upgrade-", workflow)
        self.assertIn("needs-agent", workflow)
        self.assertIn("Closes #", workflow)

    def test_auto_merge_only_touches_bot_branches(self):
        workflow = self.read("auto-merge.yml")
        self.assertIn("bot/upgrade-", workflow)
        self.assertIn("github-actions", workflow)
        self.assertIn("vibeci-viber", workflow)
        self.assertIn("--merge", workflow)

    def test_tag_release_fires_on_pin_changes(self):
        workflow = self.read("tag-release.yml")
        self.assertIn("upstream.env", workflow)
        self.assertIn("next-version.sh", workflow)
        self.assertIn("create-github-app-token", workflow)
        self.assertIn("VIBECI_APP", workflow)


if __name__ == "__main__":
    unittest.main()
