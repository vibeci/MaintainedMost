from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "scripts/check-go-tests.py"
INVENTORY = ROOT / "maintenance/required-tests.json"
REPORT_CHECKER = ROOT / "scripts/check-ci-test-reports.py"


def events_for(packages):
    events = []
    for package, tests in packages.items():
        events.append({"Action": "start", "Package": package})
        for test in tests:
            events.append({"Action": "run", "Package": package, "Test": test})
            events.append({"Action": "pass", "Package": package, "Test": test})
        events.append({"Action": "pass", "Package": package})
    return events


def json_lines(events):
    return "".join(json.dumps(event) + "\n" for event in events)


class GoTestGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="maintainedmost-test-gate-")
        self.addCleanup(temporary.cleanup)
        self.inventory = Path(temporary.name) / "required tests.json"
        self.packages = {
            "example.test/api": ["TestRequired", "TestOther"],
            "example.test/model": ["TestRequired"],
        }
        self.inventory.write_text(json.dumps({"example": self.packages}))
        self.events = events_for(self.packages)

    def command(self, pattern="^Test", inventory=None, suite="example", protected=True):
        command = [sys.executable, "-B", str(CHECKER), pattern]
        if protected:
            command.extend(["--inventory", str(inventory or self.inventory), "--suite", suite])
        return command

    def check(self, events=(), raw=None, **options):
        return subprocess.run(
            self.command(**options), input=json_lines(events) if raw is None else raw,
            text=True, capture_output=True, timeout=10,
        )

    def assert_rejected(self, events=(), **options):
        result = self.check(events, **options)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_all_required_tests_and_packages_pass(self):
        events = [
            {"Action": "build-output", "ImportPath": "example.test/dependency", "Output": "build warning\n"},
            *[{**event, "Time": "2026-10-05T22:00:00Z", "Elapsed": 0.01} for event in self.events],
        ]
        result = self.check(events)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "build warning\n")

    def test_parallel_interleaved_packages_and_variable_subtests_pass(self):
        api = events_for({"example.test/api": ["TestRequired", "TestOther"]})
        model = events_for({"example.test/model": ["TestRequired"]})
        api[2:2] = [
            {"Action": action, "Package": "example.test/api", "Test": test}
            for test in ("TestRequired/new_case", "TestRequired/new_case/nested")
            for action in ("run", "pause", "cont", "pass")
        ]
        events = []
        for index in range(max(len(api), len(model))):
            events.extend(stream[index] for stream in (api, model) if index < len(stream))
        result = self.check(events)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_subtests_duplicates_and_unrelated_passes_cannot_replace_a_required_test(self):
        for replacement in (None, "TestRequired", "TestUnrelated", "TestOther/subtest"):
            with self.subTest(replacement=replacement):
                events = []
                for event in self.events:
                    if event.get("Test") == "TestOther":
                        if replacement is not None:
                            events.append({**event, "Test": replacement})
                    else:
                        events.append(event)
                result = self.assert_rejected(events)
                self.assertIn("missing required test pass: example.test/api: TestOther", result.stderr)

    def test_same_name_in_another_package_cannot_replace_a_required_test(self):
        events = [event for event in self.events
                  if not (event["Package"] == "example.test/model" and event.get("Test") == "TestRequired")]
        result = self.assert_rejected(events)
        self.assertIn("missing required test pass: example.test/model: TestRequired", result.stderr)

    def test_wrong_package_passes_cannot_replace_an_entire_required_package(self):
        events = [{**event, "Package": "example.test/wrong"}
                  if event["Package"] == "example.test/model" else event for event in self.events]
        result = self.assert_rejected(events)
        self.assertIn("missing terminal package pass: example.test/model", result.stderr)
        self.assertIn("missing required test pass: example.test/model: TestRequired", result.stderr)

    def test_successful_packages_without_their_required_tests_fail(self):
        self.assert_rejected([event for event in self.events if "Test" not in event])

    def test_test_passes_without_terminal_package_passes_fail(self):
        events = [event for event in self.events if event["Action"] != "pass" or "Test" in event]
        result = self.assert_rejected(events)
        self.assertIn("missing terminal package pass: example.test/api", result.stderr)
        self.assertNotIn("missing required test pass:", result.stderr)

    def test_truncated_additional_package_fails_even_after_required_packages_pass(self):
        for action in ("start", "pass"):
            with self.subTest(action=action):
                event = {"Action": action, "Package": "example.test/extra"}
                if action == "pass":
                    event["Test"] = "TestExtra"
                result = self.assert_rejected([*self.events, event])
                self.assertIn("missing terminal package pass: example.test/extra", result.stderr)

    def test_events_after_a_package_pass_are_not_terminal_success(self):
        for event in (
            {"Action": "pass", "Package": "example.test/api"},
            {"Action": "pass", "Package": "example.test/api", "Test": "TestOther"},
            {"Action": "output", "Package": "example.test/api", "Output": "late output\n"},
        ):
            with self.subTest(event=event):
                result = self.assert_rejected([*self.events, event])
                self.assertIn("event after terminal package pass", result.stderr)
        self.assert_rejected([{"Action": "pass", "Package": "example.test/api"}, *self.events])

    def test_required_test_and_subtest_skips_fail_even_with_later_passes(self):
        for test in ("TestRequired", "TestRequired/subtest", "TestRequired/subtest/nested", "TestOther/subtest"):
            with self.subTest(test=test):
                event = {"Action": "skip", "Package": "example.test/api", "Test": test}
                result = self.assert_rejected([event, *self.events], pattern="^TestRequired$")
                self.assertIn(f"example.test/api: {test}", result.stderr)

    def test_regex_matching_skip_outside_inventory_also_fails(self):
        event = {"Action": "skip", "Package": "example.test/api", "Test": "TestUnlisted/subtest"}
        result = self.assert_rejected([event, *self.events], pattern="^TestUnlisted|^TestRequired")
        self.assertIn("TestUnlisted/subtest", result.stderr)

    def test_unrequired_unmatched_skip_does_not_change_regex_contract(self):
        event = {"Action": "skip", "Package": "example.test/api", "Test": "TestOptional/subtest"}
        result = self.check([event, *self.events], pattern="^TestRequired$")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_skipped_package_is_not_a_successful_completion(self):
        events = [{**event, "Action": "skip"} if "Test" not in event and event["Action"] == "pass"
                  else event for event in self.events]
        self.assert_rejected(events)
        result = self.assert_rejected([{"Action": "skip", "Package": "example.test/api"}, *self.events])
        self.assertIn("skipped=['example.test/api']", result.stderr)

    def test_any_failure_event_is_fatal_even_with_later_passes(self):
        for test in (None, "TestRequired", "TestOther/nested", "TestUnrelated"):
            with self.subTest(test=test):
                event = {"Action": "fail", "Package": "example.test/api"}
                if test is not None:
                    event["Test"] = test
                result = self.assert_rejected([event, *self.events], pattern="^TestRequired$")
                self.assertIn("failed=True", result.stderr)
        result = self.assert_rejected([
            {"Action": "build-output", "ImportPath": "example.test/dependency", "Output": "compiler error\n"},
            {"Action": "build-fail", "ImportPath": "example.test/dependency"},
            *self.events,
        ])
        self.assertIn("failed=True", result.stderr)
        self.assertEqual(result.stdout, "compiler error\n")

    def test_empty_and_truncated_json_fail(self):
        for raw in ("", "\n", " \n", '{"Action":', json_lines(self.events) + '{"Action":"pass"'):
            with self.subTest(raw=raw):
                self.assert_rejected(raw=raw)

    def test_malformed_events_fail_even_if_all_required_results_follow(self):
        malformed = [
            None, [], "not an event", {}, {"Action": None}, {"Action": []}, {"Action": "unknown"},
            {"Action": "pass", "Test": "TestRequired"},
            {"Action": "pass", "Package": None}, {"Action": "pass", "Package": " "},
            {"Action": "pass", "Package": "example.test/api", "Test": []},
            {"Action": "pass", "Package": "example.test/api", "Test": ""},
            {"Action": "pass", "Package": "example.test/api", "Output": None},
            {"Action": "pass", "Package": "example.test/api", "Time": 1},
            {"Action": "pass", "Package": "example.test/api", "FailedBuild": []},
            {"Action": "run", "Package": "example.test/api"},
            {"Action": "start", "Package": "example.test/api", "Test": "TestRequired"},
            {"Action": "output", "Package": "example.test/api"},
            {"Action": "build-output", "Output": "missing import path"},
            {"Action": "build-fail", "ImportPath": 1},
        ]
        malformed.extend({"Action": "pass", "Package": "example.test/api", "Elapsed": elapsed}
                         for elapsed in (True, -1, "1", None, [], float("nan"), float("inf")))
        for event in malformed:
            with self.subTest(event=event):
                result = self.assert_rejected([event, *self.events])
                self.assertIn("invalid go test JSON at line 1:", result.stderr)
        for raw in (
            '{"Action":"fail","Action":"pass","Package":"example.test/api"}\n',
            '{"Action":"pass","Package":"example.test/api","Elapsed":1e999}\n',
            '{} {}\n',
        ):
            with self.subTest(raw=raw):
                self.assert_rejected(raw=raw + json_lines(self.events))

    def test_invalid_inventory_fails_closed(self):
        for inventory in (
            [], {}, {"example": {}}, {"example": []}, {"example": {"": ["TestRequired"]}},
            {"example": {"example.test/api": []}}, {"example": {"example.test/api": "TestRequired"}},
            {"example": {"example.test/api": ["TestRequired/subtest"]}},
            {"example": {"example.test/api": [None]}},
            {"example": {"example.test/api": ["TestRequired", "TestRequired"]}},
        ):
            with self.subTest(inventory=inventory):
                self.inventory.write_text(json.dumps(inventory))
                result = self.assert_rejected(self.events)
                self.assertIn("invalid inventory", result.stderr)
        for raw in ('{"example":', '{"example":{},"example":{}}'):
            self.inventory.write_text(raw)
            self.assert_rejected(self.events)
        self.inventory.unlink()
        self.assert_rejected(self.events)

    def test_unknown_suite_invalid_regex_and_unpaired_options_fail(self):
        result = self.assert_rejected(self.events, suite="nonexistent")
        self.assertIn("unknown suite: nonexistent", result.stderr)
        self.assert_rejected(self.events, pattern="[")
        for options in (("--inventory", str(self.inventory)), ("--suite", "example")):
            with self.subTest(options=options):
                result = subprocess.run(
                    [*self.command(protected=False), *options], input=json_lines(self.events),
                    text=True, capture_output=True, timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("must be used together", result.stderr)

    def test_lone_regex_caller_still_accepts_a_matching_packageless_pass(self):
        event = {"Action": "pass", "Test": "TestMaintainedMostRegression"}
        result = self.check([event], protected=False, pattern="^TestMaintainedMost")
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.check([event], protected=False, pattern="MaintainedMost")
        self.assertEqual(result.returncode, 0, result.stderr)
        event = {"Action": "pass", "Test": "TestOther/matching_child"}
        result = self.check([event], protected=False, pattern="/matching_child$")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_lone_regex_still_rejects_empty_unmatched_skipped_and_failed_results(self):
        for events in (
            [], [{"Action": "pass", "Package": "example.test/api"}],
            [{"Action": "pass", "Test": "TestMattermoreRegression"}],
            [{"Action": "pass", "Test": "TestMaintainedMostRegression"},
             {"Action": "skip", "Test": "TestMaintainedMostRegression/nested"}],
            [{"Action": "pass", "Test": "TestMaintainedMostRegression"},
             {"Action": "fail", "Test": "TestUnrelated"}],
        ):
            with self.subTest(events=events):
                self.assert_rejected(events, protected=False, pattern="^TestMaintainedMost")

    def test_inventory_is_not_filtered_by_the_regex(self):
        events = [event for event in self.events if event.get("Test") != "TestOther"]
        result = self.assert_rejected(events, pattern="^TestRequired$")
        self.assertIn("missing required test pass: example.test/api: TestOther", result.stderr)
        result = self.assert_rejected(self.events, pattern="^TestMissing$")
        self.assertIn("0 passed", result.stderr)

    def test_human_output_is_flushed_before_end_of_stream(self):
        with subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True) as process:
            try:
                process.stdin.write(json_lines([
                    self.events[0],
                    {"Action": "output", "Package": "example.test/api", "Output": "human output\n"},
                ]))
                process.stdin.flush()
                ready, _, _ = select.select([process.stdout], [], [], 5)
                self.assertTrue(ready, "checker buffered human output until EOF")
                self.assertEqual(process.stdout.readline(), "human output\n")
                stdout, stderr = process.communicate(json_lines(self.events[1:]), timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual(stdout, "")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()

    def test_shipped_inventory_suites_require_named_tests_not_counts(self):
        inventory = json.loads(INVENTORY.read_text())
        self.assertEqual(set(inventory), {
            "server-api", "server-oidc", "server-config", "server-model", "calls", "transcriber",
        })
        for suite, packages in inventory.items():
            with self.subTest(suite=suite):
                events = events_for(packages)
                result = self.check(events, inventory=INVENTORY, suite=suite)
                self.assertEqual(result.returncode, 0, result.stderr)
                for package, tests in packages.items():
                    missing = [event for event in events
                               if (event["Package"], event.get("Test")) != (package, tests[0])]
                    result = self.assert_rejected(missing, inventory=INVENTORY, suite=suite)
                    self.assertIn(f"missing required test pass: {package}: {tests[0]}", result.stderr)


class ComponentGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="maintainedmost-component-gate-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        for directory in ("scripts", "maintenance", "bin", "source/server/public", "source/webapp/node_modules/.bin"):
            (self.root / directory).mkdir(parents=True, exist_ok=True)
        for name in ("test-component.sh", "check-go-tests.py"):
            shutil.copy2(ROOT / "scripts" / name, self.root / "scripts" / name)
        shutil.copy2(INVENTORY, self.root / "maintenance/required-tests.json")
        (self.root / "upstream.env").write_text("GO_VERSION=1.26.7\n")
        (self.root / "source/server/go.work").touch()
        self.log = self.root / "commands.jsonl"
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("SERVER_TEST_", "SERVER_CONFIG_TEST_", "CALLS_TEST_", "TRANSCRIBER_TEST_"))
                    and key not in ("GO_TEST_FLAGS", "GOFLAGS", "CALLS_JEST_TEST_PATTERN")}
        self.env.update(PATH=str(self.root / "bin") + os.pathsep + os.environ["PATH"],
                        COMMAND_LOG=str(self.log), RESULT_INVENTORY=str(INVENTORY))
        self.stub(self.root / "bin/go", """
args = sys.argv[1:]
assert os.environ['GOTOOLCHAIN'] == 'go1.26.7'
if args == ['env', 'GOHOSTOS']:
    print('linux')
elif args == ['env', 'GOHOSTARCH']:
    print('amd64')
elif args[0] == 'test':
    assert (os.environ['GOOS'], os.environ['GOARCH'], os.environ['CGO_ENABLED']) == ('linux', 'amd64', '1')
    component = os.environ['COMPONENT']
    if component == 'server':
        module = 'github.com/mattermost/mattermost/server/' + ('public' if Path.cwd().name == 'public' else 'v8')
    elif component == 'calls':
        module = 'github.com/mattermost/mattermost-plugin-calls'
    else:
        module = 'github.com/mattermost/calls-transcriber'
    flags = os.environ.get('GOFLAGS', '').split() + args
    selector = flags[max(index for index, arg in enumerate(flags) if arg == '-run') + 1]
    skip = next((arg.split('=', 1)[1] for arg in flags if arg.startswith('-skip=')), '')
    inventory = json.loads(Path(os.environ['RESULT_INVENTORY']).read_text())
    for packages in inventory.values():
        for package, tests in packages.items():
            if not package.startswith(module + '/'):
                continue
            relative = './' + package[len(module) + 1:]
            if relative not in args and './...' not in args:
                continue
            if package == os.environ.get('OMIT_PACKAGE'):
                continue
            print(json.dumps({'Action': 'start', 'Package': package}))
            for test in tests:
                if not re.search(selector.split('/')[0], test) or skip and re.search(skip, test):
                    continue
                if package + ':' + test == os.environ.get('OMIT_TEST'):
                    continue
                for action in ('run', 'pass'):
                    print(json.dumps({'Action': action, 'Package': package, 'Test': test}))
            print(json.dumps({'Action': 'pass', 'Package': package}))
    sys.exit(int(os.environ.get('GO_EXIT_CODE', '0')))
else:
    sys.exit('unexpected go command: ' + repr(args))
""")
        self.stub(self.root / "bin/make", "")
        self.stub(self.root / "source/webapp/node_modules/.bin/jest", "")

    def stub(self, path, body):
        path.write_text(
            f"#!{sys.executable}\n"
            "import json, os, re, sys\nfrom pathlib import Path\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
            "    log.write(json.dumps([Path(sys.argv[0]).name] + sys.argv[1:]) + '\\n')\n" + body
        )
        path.chmod(0o755)

    def run_component(self, component, **env):
        self.log.unlink(missing_ok=True)
        return subprocess.run(
            ["bash", str(self.root / "scripts/test-component.sh"), component, str(self.root / "source")],
            env={**self.env, "COMPONENT": component, **env}, text=True, capture_output=True, timeout=20,
        )

    def commands(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def test_default_components_enforce_all_suites_with_real_checker(self):
        inventory = json.loads(INVENTORY.read_text())
        for component in ("server", "calls", "transcriber"):
            with self.subTest(component=component):
                result = self.run_component(component, GOOS="darwin", GOARCH="arm64", CGO_ENABLED="0")
                self.assertEqual(result.returncode, 0, result.stderr)
                suites = [command for command in self.commands() if command[:2] == ["go", "test"]]
                self.assertEqual(len(suites), 4 if component == "server" else 1)
                for command in suites:
                    for flag in ("-json", "-race", "-count=1"):
                        self.assertIn(flag, command)
                if component == "calls":
                    self.assertIn(["jest", "--ci", "--runInBand", "--coverage=false"], self.commands())
                for suite, packages in inventory.items():
                    if suite.split("-")[0] == component:
                        report = self.root / "build/test-results" / (suite + ".jsonl")
                        self.assertEqual(report.read_text(), json_lines(events_for(packages)))

    def test_report_is_overwritten_rather_than_appended(self):
        result = self.run_component("transcriber")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = self.root / "build/test-results/transcriber.jsonl"
        expected = report.read_bytes()
        report.write_text("stale report\n")
        result = self.run_component("transcriber")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(report.read_bytes(), expected)

    def test_tee_failure_is_not_masked_by_passing_checker(self):
        self.stub(self.root / "bin/tee", "sys.stdout.write(sys.stdin.read())\nsys.exit(29)\n")
        result = self.run_component("calls")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(command[0] == "jest" for command in self.commands()))

    def test_replaced_producer_checker_cannot_hide_a_deleted_test_from_fresh_validation(self):
        trusted = self.root / "fresh trusted checkout"
        (trusted / "scripts").mkdir(parents=True)
        (trusted / "maintenance").mkdir()
        for source in (CHECKER, REPORT_CHECKER):
            shutil.copy2(source, trusted / "scripts" / source.name)
        shutil.copy2(INVENTORY, trusted / "maintenance/required-tests.json")
        inventory = json.loads(INVENTORY.read_text())
        package = next(iter(inventory["calls"]))
        missing_test = inventory["calls"][package][0]
        # The synthetic Go producer treats this catalog as its available source tests.
        catalog = self.root / "source/available-tests.json"
        catalog.write_text(json.dumps(inventory))
        for component in ("server", "transcriber"):
            result = self.run_component(component)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.stub(self.root / "bin/make", """
if sys.argv[1:] == ['apply']:
    root = Path(os.environ['COMMAND_LOG']).parent
    (root / 'scripts/check-go-tests.py').write_text('import sys\\nsys.stdin.buffer.read()\\n')
    catalog = Path(os.environ['RESULT_INVENTORY'])
    inventory = json.loads(catalog.read_text())
    package = next(iter(inventory['calls']))
    inventory['calls'][package].pop(0)
    catalog.write_text(json.dumps(inventory))
    (root / 'maintenance/required-tests.json').write_text(json.dumps(inventory))
""")
        result = self.run_component("calls", RESULT_INVENTORY=str(catalog))
        self.assertEqual(result.returncode, 0, result.stderr)
        reports = self.root / "build/test-results"
        self.assertEqual({path.name for path in reports.iterdir()}, {suite + ".jsonl" for suite in inventory})
        events = [json.loads(line) for line in (reports / "calls.jsonl").read_text().splitlines()]
        self.assertFalse(any(event.get("Test") == missing_test and event.get("Package") == package for event in events))
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(trusted / "scripts/check-ci-test-reports.py"), str(reports)],
            cwd=self.root, text=True, capture_output=True, timeout=20,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("calls: protected test validation failed", result.stderr)

    def test_every_shipped_package_and_suite_has_an_unwaivable_test_contract(self):
        inventory = json.loads(INVENTORY.read_text())
        for suite, packages in inventory.items():
            for package, tests in packages.items():
                for omission in ({"OMIT_PACKAGE": package}, {"OMIT_TEST": package + ":" + tests[0]}):
                    with self.subTest(suite=suite, omission=omission):
                        result = self.run_component(suite.split("-")[0], **omission)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(f"required suite '{suite}'", result.stderr)
                        self.assertIn(f"missing required test pass: {package}: {tests[0]}", result.stderr)
                        self.assertFalse(any(command[0] == "jest" for command in self.commands()))

    def test_narrowed_selectors_and_skipped_tests_cannot_waive_inventory(self):
        for component, overrides in (
            ("server", {"SERVER_TEST_PACKAGES": "./channels/app"}),
            ("server", {"SERVER_TEST_PATTERN": "^TestMaintainedMostOIDCCallback$"}),
            ("server", {"SERVER_CONFIG_TEST_PATTERN": "^TestConfigDefaults$"}),
            ("calls", {"CALLS_TEST_PACKAGES": "./server"}),
            ("calls", {"CALLS_TEST_PATTERN": "^TestMaintainedMostRecordingDefaults$"}),
            ("transcriber", {"TRANSCRIBER_TEST_PACKAGES": "./cmd/transcriber/config"}),
            ("transcriber", {"TRANSCRIBER_TEST_PATTERN": "^TestConfigIsValid$"}),
            ("transcriber", {"GO_TEST_FLAGS": "-skip=TestMaintainedMost"}),
            ("transcriber", {"GOFLAGS": "-skip=TestMaintainedMost"}),
        ):
            with self.subTest(component=component, overrides=overrides):
                result = self.run_component(component, **overrides)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("missing required test pass:", result.stderr)

    def test_extra_flags_cannot_disable_race_json_or_fresh_test_execution(self):
        overrides = "-race=false -json=false -count=0 -run ^TestNonexistent$ -timeout=5s"
        result = self.run_component("transcriber", GO_TEST_FLAGS=overrides, GOFLAGS=overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        command = next(command for command in self.commands() if command[:2] == ["go", "test"])
        for override, enforced in (("-race=false", "-race"), ("-json=false", "-json"), ("-count=0", "-count=1")):
            self.assertLess(command.index(override), command.index(enforced))
        selectors = [command[index + 1] for index, arg in enumerate(command) if arg == "-run"]
        self.assertEqual(selectors, ["^TestNonexistent$", "."])
        self.assertIn("-timeout=5s", command)

    def test_package_overrides_cannot_inject_flags_after_mandatory_flags(self):
        result = self.run_component("calls", CALLS_TEST_PACKAGES="-race=false ./server ./server/license")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("test package selectors must not contain flags", result.stderr)
        self.assertFalse(any(command[:2] == ["go", "test"] for command in self.commands()))

    def test_missing_inventory_has_no_fallback(self):
        (self.root / "maintenance/required-tests.json").unlink()
        result = self.run_component("transcriber")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid inventory", result.stderr)

    def test_go_exit_failure_is_not_masked_by_complete_passing_events(self):
        result = self.run_component("calls", GO_EXIT_CODE="23")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(command[0] == "jest" for command in self.commands()))


class CIReportGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="maintainedmost-ci-reports-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.trusted = self.root / "trusted checkout"
        (self.trusted / "scripts").mkdir(parents=True)
        (self.trusted / "maintenance").mkdir()
        for source in (CHECKER, REPORT_CHECKER):
            shutil.copy2(source, self.trusted / "scripts" / source.name)
        shutil.copy2(INVENTORY, self.trusted / "maintenance/required-tests.json")
        self.validator = self.trusted / "scripts/check-ci-test-reports.py"
        self.reports = self.root / "downloaded reports"
        self.reports.mkdir()
        self.inventory = json.loads(INVENTORY.read_text())
        for suite, packages in self.inventory.items():
            (self.reports / (suite + ".jsonl")).write_text(json_lines(events_for(packages)))

    def check(self, directory=None):
        return subprocess.run(
            [sys.executable, "-I", "-B", str(self.validator), str(directory or self.reports)],
            cwd=self.reports, text=True, capture_output=True, timeout=20,
        )

    def assert_rejected(self, directory=None):
        result = self.check(directory)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_all_six_complete_reports_pass_using_policy_outside_report_directory(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        for suite in self.inventory:
            self.assertIn(suite + ": passed", result.stdout)
        self.assertIn("Validated all six required Go suites", result.stdout)

    def test_each_missing_report_fails(self):
        for suite in self.inventory:
            with self.subTest(suite=suite):
                report = self.reports / (suite + ".jsonl")
                data = report.read_bytes()
                report.unlink()
                result = self.assert_rejected()
                self.assertIn("missing required reports: " + report.name, result.stderr)
                report.write_bytes(data)

    def test_empty_missing_and_nondirectory_report_paths_fail(self):
        empty = self.root / "empty"
        empty.mkdir()
        for path in (empty, self.root / "missing", self.reports / "calls.jsonl"):
            with self.subTest(path=path):
                self.assert_rejected(path)

    def test_unexpected_files_directories_and_filename_case_fail_without_echoing_names(self):
        for name, directory in (("untrusted-secret-entry", False), ("extra.jsonl", False),
                                ("nested", True), ("check-go-tests.py", False)):
            with self.subTest(name=name):
                entry = self.reports / name
                if directory:
                    entry.mkdir()
                else:
                    entry.write_text("untrusted artifact contents")
                result = self.assert_rejected()
                self.assertIn("unexpected report entry", result.stderr)
                self.assertNotIn(name, result.stdout + result.stderr)
                if directory:
                    entry.rmdir()
                else:
                    entry.unlink()
        report = self.reports / "server-api.jsonl"
        wrong_case = self.reports / "SERVER-api.jsonl"
        report.rename(wrong_case)
        result = self.assert_rejected()
        self.assertIn("unexpected report entry", result.stderr)
        self.assertNotIn(wrong_case.name, result.stdout + result.stderr)

    def test_symlinked_report_directory_and_ancestor_are_rejected(self):
        direct = self.root / "linked reports"
        direct.symlink_to(self.reports, target_is_directory=True)
        ancestor = self.root / "linked parent"
        ancestor.symlink_to(self.root, target_is_directory=True)
        for path in (direct, ancestor / self.reports.name, direct / ".." / self.reports.name):
            with self.subTest(path=path):
                self.assert_rejected(path)

    def test_symlinked_hardlinked_and_nonregular_reports_fail(self):
        report = self.reports / "server-api.jsonl"
        outside = self.root / "outside.jsonl"
        outside.write_bytes(report.read_bytes())
        report.unlink()
        for target in (outside, self.reports / "calls.jsonl", self.root / "missing"):
            with self.subTest(target=target):
                report.symlink_to(target)
                self.assert_rejected()
                report.unlink()
        os.link(outside, report)
        self.assert_rejected()
        report.unlink()
        report.mkdir()
        self.assert_rejected()
        report.rmdir()
        os.mkfifo(report)
        self.assert_rejected()

    def test_empty_and_oversized_reports_fail_before_parsing(self):
        report = self.reports / "server-api.jsonl"
        for size in (0, 64 * 1024 * 1024 + 1):
            with self.subTest(size=size):
                with report.open("wb") as output:
                    output.truncate(size)
                result = self.assert_rejected()
                self.assertIn("report must contain 1 byte to 64 MiB", result.stderr)

    def test_missing_tests_packages_completions_skips_and_malformed_json_fail(self):
        packages = self.inventory["server-api"]
        package = next(iter(packages))
        test = packages[package][0]
        events = events_for(packages)
        cases = [
            "not JSON\n", json_lines(events) + '{"Action":',
            json_lines(event for event in events if (event["Package"], event.get("Test")) != (package, test)),
            json_lines(event for event in events if event["Package"] != package),
            json_lines(event for event in events if event["Action"] != "pass" or "Test" in event),
            json_lines(event for event in events if "Test" not in event),
        ]
        cases.extend(json_lines([{"Action": action, "Package": package, "Test": name}, *events])
                     for action, name in (("skip", test), ("skip", test + "/nested"), ("fail", "TestUnrelated")))
        report = self.reports / "server-api.jsonl"
        for index, data in enumerate(cases):
            with self.subTest(case=index):
                report.write_text(data)
                result = self.assert_rejected()
                self.assertIn("server-api: protected test validation failed", result.stderr)

    def test_report_contents_are_not_executed_or_replayed_on_success_or_failure(self):
        report = self.reports / "server-api.jsonl"
        package = next(iter(self.inventory["server-api"]))
        good = report.read_text()
        secret = "untrusted-secret-report-text"
        report.write_text(json_lines([{"Action": "output", "Package": package, "Output": secret}]) + good)
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout + result.stderr)
        report.write_text(json_lines([{"Action": secret}]) + good)
        result = self.assert_rejected()
        self.assertNotIn(secret, result.stdout + result.stderr)
        marker = self.root / "must-not-execute"
        report.write_text(f"from pathlib import Path; Path({str(marker)!r}).touch()\n")
        self.assert_rejected()
        self.assertFalse(marker.exists())

    def test_missing_invalid_or_symlinked_trusted_policy_fails_closed(self):
        policy = self.trusted / "maintenance/required-tests.json"
        policy.write_text("{}")
        self.assert_rejected()
        policy.unlink()
        self.assert_rejected()
        policy.symlink_to(INVENTORY)
        self.assert_rejected()
        policy.unlink()
        shutil.copy2(INVENTORY, policy)
        checker = self.trusted / "scripts/check-go-tests.py"
        checker.unlink()
        self.assert_rejected()
        checker.symlink_to(CHECKER)
        self.assert_rejected()

    def test_checker_nonzero_exit_fails_without_replaying_child_errors(self):
        (self.trusted / "scripts/check-go-tests.py").write_text(
            "import sys\nsys.stdin.buffer.read()\nprint('private child failure', file=sys.stderr)\nsys.exit(23)\n"
        )
        result = self.assert_rejected()
        self.assertIn("exit 23", result.stderr)
        self.assertNotIn("private child failure", result.stdout + result.stderr)

    def test_subprocess_launch_errors_and_timeouts_fail_with_trusted_absolute_command(self):
        spec = importlib.util.spec_from_file_location("ci_test_reports", self.validator)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for failure in (OSError("private launch failure"), subprocess.TimeoutExpired("private command", 60)):
            with self.subTest(failure=type(failure).__name__):
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(sys, "argv", [str(self.validator), str(self.reports)]), \
                        mock.patch.object(module.subprocess, "run", side_effect=failure) as run, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertNotEqual(module.main(), 0)
                args, options = run.call_args
                self.assertEqual(args[0], [
                    sys.executable, "-I", "-S", "-B", str(self.trusted / "scripts/check-go-tests.py"), "^Test",
                    "--inventory", str(self.trusted / "maintenance/required-tests.json"), "--suite", "server-api",
                ])
                self.assertEqual(options["cwd"], self.trusted)
                self.assertEqual(options["input"], (self.reports / "server-api.jsonl").read_bytes())
                self.assertEqual(options["stdout"], subprocess.DEVNULL)
                self.assertEqual(options["stderr"], subprocess.DEVNULL)
                self.assertNotIn("private", stdout.getvalue() + stderr.getvalue())


class ProtectedWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/build.yml").read_text()
        self.gate = self.workflow.split("\n  regression-gate:\n", 1)[1]

    def test_final_required_check_runs_on_a_fresh_worker_with_base_policy(self):
        self.assertIn("name: MaintainedMost regression tests and complete images\n", self.gate)
        self.assertIn("needs: [infrastructure, images]\n", self.gate)
        self.assertIn("if: always()\n", self.gate)
        self.assertIn("ref: ${{ github.event.pull_request.base.sha || github.sha }}\n", self.gate)
        self.assertIn("persist-credentials: false\n", self.gate)
        self.assertIn("path: policy\n", self.gate)
        self.assertIn("path: ${{ runner.temp }}/maintainedmost-go-test-results\n", self.gate)
        self.assertIn('run: python3 -I -B policy/scripts/check-ci-test-reports.py "$REPORT_DIR"\n', self.gate)
        self.assertNotIn("continue-on-error", self.gate)
        self.assertNotIn("build-image.sh", self.gate)

    def test_producer_failure_or_skip_cannot_leave_a_green_required_gate(self):
        script = re.search(r"        run: \|\n((?:          [^\n]*\n)+)", self.gate).group(1)
        for infrastructure, images in (("success", "success"), ("failure", "success"),
                                       ("success", "failure"), ("success", "skipped"),
                                       ("cancelled", "skipped"), ("", "")):
            with self.subTest(infrastructure=infrastructure, images=images):
                result = subprocess.run(["bash", "-e", "-c", script], capture_output=True,
                                        env={**os.environ, "INFRASTRUCTURE_RESULT": infrastructure,
                                             "BUILD_RESULT": images})
                self.assertEqual(result.returncode == 0, infrastructure == images == "success")

    def test_reports_are_uploaded_even_when_the_producer_fails(self):
        producer = self.workflow.split("\n  images:\n", 1)[1].split("\n  regression-gate:\n", 1)[0]
        self.assertIn("name: MaintainedMost complete image build\n", producer)
        self.assertIn("if: always()\n        with:\n          name: maintainedmost-go-test-results\n", producer)
        self.assertIn("path: build/test-results/*.jsonl\n", producer)
        self.assertIn("name: maintainedmost-go-test-results\n", self.gate)


if __name__ == "__main__":
    unittest.main()
