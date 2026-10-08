"""Offline orchestration tests; only the fake-runner Bash fixture runs a process."""

from contextlib import redirect_stderr, redirect_stdout
import base64
import copy
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[2]
# The runner fixture uses Bash builtins only, with fake Docker/sudo and an empty PATH.
RUN_SCRIPT = subprocess.run
SPEC = importlib.util.spec_from_file_location("vibeci_github", ROOT / "scripts/vibeci-github.py")
github = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(github)
RUNTIME_SPEC = importlib.util.spec_from_file_location("vibeci_runtime", ROOT / "scripts/vibeci-runtime.py")
runtime = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(runtime)
BASE, MANIFEST, CANDIDATE, OTHER, MERGED = (character * 40 for character in "12345")
RAW_CANDIDATE = "6" * 40
BRANCH = f"vibeci/update-8-{BASE[:12]}-{MANIFEST[:12]}-a1b2c3d4e5f6"
REPOSITORY = "test-owner/test-project"
URL = f"https://github.com/{REPOSITORY}/pull/17"


def setUpModule():
    for target in ("subprocess.run", "socket.create_connection"):
        guard = mock.patch(target, side_effect=AssertionError("unmocked processes/network are forbidden"))
        guard.start()
        unittest.addModuleCleanup(guard.stop)


class GitHubTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.data.mkdir(mode=0o700)
        self.publisher = self.root / "clean publisher.git"
        self.publisher.mkdir(mode=0o700)
        self.result = self.root / "result.json"
        self.summary = {"changed": True, "push": True, "base": BASE, "manifest": MANIFEST,
                        "version": 8, "branch": BRANCH, "old": {}, "new": {}}
        self.raw_summary = copy.deepcopy(self.summary)
        self.outcomes = [{"repo": "maintainedmost", "status": "dry-run", "commit": RAW_CANDIDATE,
                          "fork_head": BASE, "target": MANIFEST, "version": "8"}]
        self.repository = {"full_name": REPOSITORY, "private": False, "visibility": "public",
                           "default_branch": "main", "allow_auto_merge": False, "allow_merge_commit": True}
        self.protection = {"enforce_admins": {"enabled": True}, "required_status_checks": {
            "strict": True, "contexts": list(github.REQUIRED_CONTEXTS),
            "checks": [{"context": context, "app_id": 15368} for context in github.REQUIRED_CONTEXTS],
        }}
        self.pull = {"number": 17, "state": "open", "merged": False, "draft": False,
                     "head": {"ref": BRANCH, "sha": CANDIDATE, "repo": {"full_name": REPOSITORY}},
                      "base": {"ref": "main", "sha": BASE, "repo": {"full_name": REPOSITORY}}}
        self.view = {"headRefOid": CANDIDATE, "baseRefOid": BASE, "state": "OPEN", "isDraft": False,
                     "statusCheckRollup": [{"__typename": "CheckRun", "name": name, "status": "COMPLETED",
                                            "conclusion": "SUCCESS"} for name in github.REQUIRED_CONTEXTS]}
        self.merge_reply = {"merged": True, "sha": MERGED, "message": "Pull Request successfully merged"}
        self.pages = [[]]
        self.local_head, self.remote_base, self.remote_candidate = BASE, BASE, CANDIDATE
        self.dirty = ""
        self.calls, self.sequences, self.raw = [], {}, {}
        self.remote_ref = ""
        self.fail_git = None
        self.fail_endpoint = None
        self.create_url = URL
        self.now = 0
        self.on_sleep = self.after_view = self.before_merge = None
        self.env = {
            "PATH": os.defpath, "HOME": str(self.root), "GH_TOKEN": "dedicated-bot-secret",
            "GITHUB_TOKEN": "builtin-must-not-be-used", "GH_HOST": "untrusted.example.invalid",
            "GH_ENTERPRISE_TOKEN": "enterprise-secret", "VIBECI_LLM_API_KEY": "llm-secret",
            "VIBECI_PRIVATE_TERMS": '["private-provider.invalid","canary-private-model"]',
            "VIBECI_MODELS": json.dumps({
                "providers": {"canary": {"type": "openai-chat", "base_url": "https://private-provider.invalid",
                                         "api_key": "file:/run/secrets/llm_api_key"}},
                "models": {"canary": {"provider": "canary", "model": "canary-private-model"}},
                "roles": {"audit": "canary", "resolve": ["canary"]},
            }),
            "GITHUB_REPOSITORY": REPOSITORY, "BASE_BRANCH": "main", "AUTOMERGE": "false",
            "VIBECI_ENABLED": "true", "VIBECI_PUSH": "true", "VIBECI_AUTOMERGE": "false",
            "GITHUB_EVENT_NAME": "workflow_dispatch", "PREVIEW": "false",
            "GITHUB_OUTPUT": str(self.root / "outputs"), "GITHUB_STEP_SUMMARY": str(self.root / "job-summary"),
        }
        environment = mock.patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        process = mock.patch.object(github.subprocess, "run", side_effect=self.fake_run)
        process.start()
        self.addCleanup(process.stop)
        self.privacy = mock.Mock(return_value=(self.publisher, CANDIDATE))
        loader = mock.patch.object(github.runpy, "run_path", return_value={"prepare_publication": self.privacy})
        self.loader = loader.start()
        self.addCleanup(loader.stop)
        clock = mock.patch.object(github.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        sleep = mock.patch.object(github.time, "sleep", side_effect=self.sleep)
        self.sleep_mock = sleep.start()
        self.addCleanup(sleep.stop)

    def sleep(self, seconds):
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()

    def endpoint(self, suffix=""):
        return "repos/" + REPOSITORY + suffix

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "git":
            if args[1:] == ["rev-parse", "--verify", "HEAD^{commit}"]:
                output = self.local_head + "\n"
            elif "status" in args:
                output = self.dirty
            elif "ls-remote" in args or "push" in args:
                operation = "ls-remote" if "ls-remote" in args else "push"
                self.assertEqual(args[1:3], ["-C", str(self.publisher)])
                self.assertTrue(self.privacy.called)
                url, ref = f"https://github.com/{REPOSITORY}.git", "refs/heads/" + BRANCH
                expected = ["--refs", url, ref] if operation == "ls-remote" else [url, CANDIDATE + ":" + ref]
                self.assertEqual(args[args.index(operation) + 1:], expected)
                if operation == self.fail_git:
                    return subprocess.CompletedProcess(args, 1, "private-provider.invalid", "dedicated-bot-secret llm-secret")
                output = self.remote_ref if operation == "ls-remote" else ""
            else:
                self.fail("unexpected Git command: " + repr(args))
        elif args[:2] == ["gh", "api"]:
            method = args[args.index("--method") + 1]
            self.assertIn(method, {"GET", "PUT"})
            self.assertEqual(args[args.index("--hostname") + 1], "github.com")
            endpoint = args[-1]
            if method == "PUT":
                self.assertEqual(endpoint, self.endpoint("/pulls/17/merge"))
                fields = dict(args[index + 1].split("=", 1) for index, value in enumerate(args) if value == "--raw-field")
                self.assertEqual(fields, {"sha": CANDIDATE, "merge_method": "merge"})
                if self.before_merge:
                    self.before_merge()
                if self.remote_candidate != fields["sha"]:
                    return subprocess.CompletedProcess(args, 1, "", "Head branch was modified")
            if endpoint == self.fail_endpoint:
                return subprocess.CompletedProcess(args, 1, "", "dedicated-bot-secret llm-secret")
            if endpoint in self.raw:
                return subprocess.CompletedProcess(args, 0, self.raw[endpoint], "")
            if endpoint in self.sequences:
                sequence = self.sequences[endpoint]
                value = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            elif endpoint == self.endpoint():
                value = self.repository
            elif endpoint == self.endpoint("/branches/" + quote(os.environ["BASE_BRANCH"], safe="")):
                value = {"commit": {"sha": self.remote_base}}
            elif endpoint == self.endpoint("/branches/" + quote(BRANCH, safe="")):
                value = {"commit": {"sha": self.remote_candidate}}
            elif endpoint.endswith("/protection"):
                value = self.protection
            elif endpoint == self.endpoint("/pulls?state=open&per_page=100"):
                self.assertIn("--paginate", args)
                self.assertIn("--slurp", args)
                value = self.pages
            elif endpoint == self.endpoint("/pulls/17"):
                value = self.pull
            elif endpoint == self.endpoint("/pulls/17/merge"):
                value = self.merge_reply
            else:
                self.fail("unexpected GitHub endpoint: " + endpoint)
            output = json.dumps(value)
        elif args[:3] == ["gh", "pr", "create"]:
            output = self.create_url + "\n"
        elif args[:3] == ["gh", "pr", "view"]:
            self.assertEqual(args[3:], [URL, "--repo", REPOSITORY, "--json",
                                        "headRefOid,baseRefOid,state,isDraft,statusCheckRollup"])
            sequence = self.sequences.get("view", [self.view])
            value = sequence.pop(0) if len(sequence) > 1 else sequence[0]
            output = json.dumps(value)
            if self.after_view:
                self.after_view()
        else:
            self.fail("unexpected command: " + repr(args))
        return subprocess.CompletedProcess(args, 0, output, "")

    def write_evidence(self):
        (self.data / "public-summary.json").write_text(json.dumps(self.summary), encoding="utf-8")
        (self.data / "summary.json").write_text(json.dumps(self.raw_summary), encoding="utf-8")
        (self.data / "summary.md").write_text("# Raw planner summary: private-provider.invalid\n", encoding="utf-8")
        (self.data / "public-summary.md").write_text("# Screened public planner summary\n", encoding="utf-8")
        self.result.write_text(json.dumps(self.outcomes), encoding="utf-8")

    def publish(self):
        self.write_evidence()
        return github.publish(self.data, self.result)

    def mutations(self):
        return [args for args, _ in self.calls if args[:3] == ["gh", "pr", "create"]
                or (args[:2] == ["gh", "api"] and args[args.index("--method") + 1] != "GET")]

    def pushes(self):
        return [args for args, _ in self.calls if args[0] == "git" and "push" in args]

    def automerge(self):
        os.environ.update(AUTOMERGE="true", VIBECI_AUTOMERGE="true")

    def test_preflight_verifies_clean_default_branch_without_mutations(self):
        self.assertEqual(github.preflight(), {"skip": "false", "pull_request": "", "open_proposals": "0"})
        self.assertEqual(self.mutations(), [])
        status = next(args for args, _ in self.calls if "status" in args)
        self.assertIn("--ignored=matching", status)
        self.assertIn("--untracked-files=all", status)
        self.assertEqual(status[-3:], ["--", ".", ":(top,exclude)build/"])

    def test_preflight_rejects_wrong_identity_default_or_stale_base(self):
        for changes in ({"full_name": "different/repository"}, {"default_branch": "other"}):
            with self.subTest(changes=changes), mock.patch.dict(self.repository, changes):
                with self.assertRaises(github.GitHubError):
                    github.preflight()
        self.remote_base = OTHER
        with self.assertRaisesRegex(github.GitHubError, "default branch moved"):
            github.preflight()
        self.assertEqual(self.mutations(), [])

    def test_private_fork_preflight_and_preview_preserve_identity_checks_without_writes(self):
        self.repository.update(private=True, visibility="private", fork=True)
        self.protection = None
        self.assertEqual(github.preflight()["skip"], "false")
        self.summary["push"] = False
        self.assertEqual(self.publish()["status"], "validated-dry-run")
        self.privacy.assert_called_once_with(self.data, RAW_CANDIDATE, BASE, MANIFEST, 8, "main")
        self.assertEqual(self.mutations() + self.pushes(), [])

    def test_private_automerge_still_requires_classic_protection_before_any_write(self):
        self.repository.update(private=True, visibility="private", fork=True)
        self.protection = None
        self.automerge()
        for operation in (github.preflight, self.publish):
            with self.subTest(operation=operation.__name__), self.assertRaisesRegex(github.GitHubError, "classic branch protection"):
                operation()
        self.assertEqual(self.mutations() + self.pushes(), [])

    def test_preflight_rejects_dirty_tracked_untracked_and_ignored_inputs(self):
        for status in (" M scripts/check.py\0", "?? new.py\0", "!! scripts/hidden.py\0", "!! dist/\0"):
            self.dirty = status
            with self.subTest(status=status), self.assertRaisesRegex(github.GitHubError, "dirty"):
                github.preflight()
        self.assertEqual(self.mutations(), [])

    def test_same_repo_default_base_proposal_on_later_pages_skips_even_drafts(self):
        self.automerge()
        unrelated = copy.deepcopy(self.pull)
        unrelated["head"]["ref"] = "ordinary-update"
        proposal = copy.deepcopy(self.pull)
        proposal["draft"] = True
        proposal["head"]["ref"] = "vibeci/update-some-other-version"
        self.pages = [[unrelated], [proposal], [dict(proposal, number=18)]]
        self.protection = None
        self.repository["allow_auto_merge"] = False
        result = github.preflight()
        self.assertEqual(result, {"skip": "true", "pull_request": URL, "open_proposals": "2"})
        self.assertFalse(any(args[-1].endswith("/protection") for args, _ in self.calls))
        self.assertEqual(self.mutations(), [])

    def test_foreign_deleted_head_and_other_base_proposals_do_not_block_planning_or_publication(self):
        changes = (("head", "repo", {"full_name": "another/fork"}), ("head", "repo", None),
                   ("head", "repo", {"full_name": None}), ("head", "ref", None), ("base", "ref", "other"),
                   ("base", "repo", {"full_name": "another/repository"}))
        ignored = []
        for side, key, value in changes:
            pull = copy.deepcopy(self.pull)
            pull[side][key] = value
            ignored.append(pull)
        ignored.append(dict(self.pull, head=None))
        self.pages = [ignored[:2], ignored[2:]]
        self.assertEqual(github.preflight()["skip"], "false")
        self.assertEqual(self.publish()["status"], "proposal-created")
        self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_paginated_response_must_be_unambiguous(self):
        for pages in ({"items": []}, [self.pull], [[{}]], [[dict(self.pull, number=True)]]):
            self.pages = pages
            with self.subTest(pages=pages), self.assertRaises(github.GitHubError):
                github.preflight()

    def test_slashes_in_default_branch_are_encoded_in_api_path(self):
        os.environ["BASE_BRANCH"] = "release/11.x"
        self.repository["default_branch"] = "release/11.x"
        self.assertEqual(github.preflight()["skip"], "false")
        self.assertTrue(any(args[-1].endswith("/branches/release%2F11.x") for args, _ in self.calls))

    def test_only_explicit_bot_token_reaches_gh_and_isolated_git_transport(self):
        poison = {
            "SYSTEMROOT": "/canary/system",
            "GIT_DIR": "/canary/raw.git", "GIT_WORK_TREE": "/canary/worktree",
            "GIT_OBJECT_DIRECTORY": "/canary/objects", "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/canary/alternate",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "!canary-helper",
            "GIT_CONFIG_PARAMETERS": "canary-config", "GIT_CONFIG": "/canary/config", "GIT_CONFIG_SYSTEM": "/canary/config",
            "GIT_CONFIG_GLOBAL": "/canary/config", "GIT_EXEC_PATH": "/canary/git", "GIT_TEMPLATE_DIR": "/canary/templates",
            "GIT_SSH_COMMAND": "canary-ssh", "GIT_PROXY_COMMAND": "canary-proxy", "SSH_AUTH_SOCK": "/canary/agent",
            "GIT_TRACE": "1", "GIT_TRACE_PACKET": "1", "GIT_TRACE_CURL": "1", "GIT_CURL_VERBOSE": "1",
            "HTTP_PROXY": "https://private-provider.invalid", "HTTPS_PROXY": "https://private-provider.invalid",
            "ALL_PROXY": "https://private-provider.invalid", "https_proxy": "https://private-provider.invalid",
            "LD_PRELOAD": "/canary/preload", "DYLD_INSERT_LIBRARIES": "/canary/preload",
            "VIBECI_GIT_TOKEN": "publisher-alias-must-not-leak",
        }
        os.environ.update(poison)
        github.preflight()
        self.publish()
        for args, options in self.calls:
            env = options["env"]
            self.assertFalse({"GITHUB_TOKEN", "VIBECI_LLM_API_KEY", "VIBECI_GIT_TOKEN", "VIBECI_PRIVATE_TERMS",
                              "VIBECI_MODELS", "GH_ENTERPRISE_TOKEN"} & env.keys())
            self.assertNotIn("dedicated-bot-secret", " ".join(args))
            self.assertNotIn("llm-secret", " ".join(args))
            self.assertNotIn("private-provider.invalid", " ".join(args))
            self.assertFalse(options.get("shell", False))
            self.assertTrue(options["capture_output"])
            self.assertEqual(options["timeout"], 60)
            if args[0] == "gh":
                self.assertEqual(env["GH_TOKEN"], "dedicated-bot-secret")
                self.assertEqual(env["GH_HOST"], "github.com")
            else:
                self.assertNotIn("GH_TOKEN", env)
                if "ls-remote" in args or "push" in args:
                    self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
                    self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
                    self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
                    self.assertEqual(env["GIT_CONFIG_KEY_1"], f"http.https://github.com/{REPOSITORY}.git.extraHeader")
                    scheme, encoded = env["GIT_CONFIG_VALUE_1"].rsplit(" ", 1)
                    self.assertEqual(scheme, "AUTHORIZATION: basic")
                    self.assertEqual(base64.b64decode(encoded).decode(), "x-access-token:dedicated-bot-secret")
                    self.assertNotIn(encoded, " ".join(args))
                    for key in ("HOME", "XDG_CONFIG_HOME", "CURL_HOME", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
                                "GIT_ASKPASS", "SSH_ASKPASS"):
                        self.assertEqual(env[key], os.devnull)
                    for key in ("GIT_CONFIG_NOSYSTEM", "GIT_ATTR_NOSYSTEM", "GIT_NO_REPLACE_OBJECTS", "GIT_NO_LAZY_FETCH"):
                        self.assertEqual(env[key], "1")
                    self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
                    self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
                    self.assertEqual(env["GIT_PROTOCOL_FROM_USER"], "0")
                    for config in ("core.hooksPath=" + os.devnull, "core.fsmonitor=false", "credential.helper=",
                                   "credential.interactive=false", "http.proxy=", "http.followRedirects=false",
                                   "http.sslVerify=true", "protocol.allow=never", "protocol.https.allow=always",
                                   "push.followTags=false", "push.recurseSubmodules=no", "push.gpgSign=false"):
                        self.assertIn(config, args)
                else:
                    self.assertNotIn("GIT_CONFIG_COUNT", env)
            for key, value in poison.items():
                self.assertNotEqual(env.get(key), value)
        self.assertEqual(list(self.publisher.iterdir()), [])
        self.calls.clear()
        os.environ.pop("GH_TOKEN")
        with self.assertRaisesRegex(github.GitHubError, "GH_TOKEN"):
            github.preflight()
        self.assertEqual(self.calls, [])

    def test_invalid_environment_fails_before_any_commands(self):
        for key, value in (("GITHUB_REPOSITORY", "https://github.com/owner/repo"),
                           ("GITHUB_REPOSITORY", "owner/.."), ("BASE_BRANCH", "-option"),
                           ("BASE_BRANCH", "main;id"), ("BASE_BRANCH", "foo/.hidden"),
                           ("BASE_BRANCH", "foo.lock"), ("BASE_BRANCH", "foo..bar"),
                           ("AUTOMERGE", "TRUE"), ("AUTOMERGE", "")):
            with self.subTest(key=key, value=value), mock.patch.dict(os.environ, {key: value}):
                with self.assertRaises(github.GitHubError):
                    github.preflight()
        self.assertEqual(self.calls, [])

    def test_automerge_preflight_requires_both_exact_action_app_checks(self):
        self.automerge()
        self.assertEqual(github.preflight()["skip"], "false")
        self.assertTrue(any(args[-1].endswith("/protection") for args, _ in self.calls))
        self.assertEqual(self.mutations(), [])
        self.protection["required_status_checks"]["contexts"].append("Additional security check")
        self.protection["required_status_checks"]["checks"].append({"context": "Additional security check", "app_id": 1})
        self.assertEqual(github.preflight()["skip"], "false")

    def test_absent_weak_ambiguous_and_different_app_check_bindings_fail(self):
        original = copy.deepcopy(self.protection)
        invalid = [None, {}, {"rulesets": [{"required_status_checks": github.REQUIRED_CONTEXTS}]}]
        for key, value in (("strict", False), ("strict", "true"), ("contexts", []), ("checks", []),
                           ("contexts", None), ("checks", None)):
            protection = copy.deepcopy(original)
            protection["required_status_checks"][key] = value
            invalid.append(protection)
        for app in (None, -1, 15369, "15368", True):
            for index in (0, 1):
                protection = copy.deepcopy(original)
                protection["required_status_checks"]["checks"][index]["app_id"] = app
                invalid.append(protection)
        for key in ("contexts", "checks"):
            protection = copy.deepcopy(original)
            protection["required_status_checks"][key].append(protection["required_status_checks"][key][0])
            invalid.append(protection)
        protection = copy.deepcopy(original)
        protection["required_status_checks"]["checks"][0].pop("app_id")
        invalid.append(protection)
        protection = copy.deepcopy(original)
        protection["required_status_checks"]["checks"][1]["context"] += " (PR)"
        invalid.append(protection)
        for admins in (None, {}, {"enabled": False}, {"enabled": "true"}):
            invalid.append(dict(original, enforce_admins=admins))
        self.automerge()
        for protection in invalid:
            self.protection = protection
            with self.subTest(protection=protection), self.assertRaises(github.GitHubError):
                github.preflight()
        self.assertEqual(self.mutations(), [])

    def test_repo_merge_settings_and_unavailable_classic_protection_fail_closed(self):
        self.automerge()
        self.repository.pop("allow_auto_merge")
        self.assertEqual(github.preflight()["skip"], "false")
        for value in (False, None, "true", 1):
            with self.subTest(value=value), mock.patch.dict(self.repository, {"allow_merge_commit": value}):
                with self.assertRaises(github.GitHubError):
                    github.preflight()
        self.fail_endpoint = self.endpoint("/branches/main/protection")
        with self.assertRaises(github.GitHubError):
            github.preflight()
        self.assertFalse(any("rulesets" in args[-1] for args, _ in self.calls))
        self.assertEqual(self.mutations(), [])

    def test_manual_publication_uses_selected_branch_and_body_file_for_public_or_private_forks(self):
        self.repository["allow_auto_merge"] = False
        self.protection = None
        for private in (False, True):
            self.calls.clear()
            self.repository.update(private=private, visibility="private" if private else "public", fork=True)
            with self.subTest(private=private):
                result = self.publish()
                self.assertEqual(result, {"status": "proposal-created", "published": "true", "pull_request": URL})
                mutations = self.mutations()
                self.assertEqual(len(mutations), 1)
                self.assertEqual(len(self.pushes()), 1)
                create = mutations[0]
                for flag, value in (("--repo", REPOSITORY), ("--base", "main"), ("--head", BRANCH),
                                    ("--body-file", str(self.data / "public-summary.md"))):
                    self.assertEqual(create[create.index(flag) + 1], value)

    def test_preview_validates_dry_run_without_branch_queries_or_mutations(self):
        self.summary["push"] = False
        self.outcomes[0]["status"] = "dry-run"
        self.automerge()
        self.protection = None
        self.repository["allow_auto_merge"] = False
        os.environ.update(VIBECI_ENABLED="false", PREVIEW="true")
        self.assertEqual(self.publish()["status"], "validated-dry-run")
        self.privacy.assert_called_once_with(self.data, RAW_CANDIDATE, BASE, MANIFEST, 8, "main")
        self.assertEqual(self.mutations() + self.pushes(), [])
        self.assertFalse(any("vibeci%2Fupdate-" in args[-1] or args[-1].endswith("/protection") for args, _ in self.calls))

    def test_publication_ignores_poisoned_raw_summary_control_fields(self):
        poisoned_branch = BRANCH.rsplit("-", 1)[0] + "-" + b"canary".hex()
        for key, value in (("branch", poisoned_branch), ("push", False), ("base", OTHER),
                           ("manifest", OTHER), ("version", 9), ("changed", False)):
            self.calls.clear()
            self.privacy.reset_mock()
            self.raw_summary = dict(self.summary, **{key: value})
            with self.subTest(field=key), mock.patch.object(github, "read_evidence", wraps=github.read_evidence) as read:
                self.assertEqual(self.publish()["status"], "proposal-created")
                self.assertNotIn(mock.call(self.data / "summary.json"), read.call_args_list)
                self.assertIn(mock.call(self.data / "public-summary.json"), read.call_args_list)
            self.privacy.assert_called_once_with(self.data, RAW_CANDIDATE, BASE, MANIFEST, 8, "main")
            self.assertEqual(len(self.pushes()), 1)
            self.assertEqual(self.pushes()[0][-1], CANDIDATE + ":refs/heads/" + BRANCH)
            create = self.mutations()[0]
            self.assertEqual(create[create.index("--head") + 1], BRANCH)
            self.assertEqual(create[create.index("--title") + 1], "Update upstream maintenance to 8")
            self.assertFalse(any(poisoned_branch in " ".join(args) for args, _ in self.calls))

    def test_public_preview_cannot_be_promoted_by_a_raw_summary_push_flag(self):
        self.summary["push"] = False
        self.raw_summary.update(push=True, branch=BRANCH.rsplit("-", 1)[0] + "-" + b"canary".hex())
        self.assertEqual(self.publish()["status"], "validated-dry-run")
        self.privacy.assert_called_once_with(self.data, RAW_CANDIDATE, BASE, MANIFEST, 8, "main")
        self.assertEqual(self.mutations() + self.pushes(), [])
        self.assertFalse(any("ls-remote" in args or "vibeci%2Fupdate-" in args[-1] for args, _ in self.calls))

    def test_missing_invalid_or_linked_public_summary_never_falls_back_to_raw(self):
        public = self.data / "public-summary.json"
        for mode in ("missing", "invalid", "symlink"):
            self.write_evidence()
            public.unlink()
            if mode == "invalid":
                public.write_text("not JSON", encoding="utf-8")
            elif mode == "symlink":
                public.symlink_to(self.data / "summary.json")
            try:
                with self.subTest(mode=mode), self.assertRaises(github.GitHubError):
                    github.publish(self.data, self.result)
            finally:
                public.unlink(missing_ok=True)
        self.assertEqual(self.calls, [])
        self.privacy.assert_not_called()

    def test_full_public_summary_is_screened_before_any_proposal_branch_call(self):
        spec = importlib.util.spec_from_file_location("vibeci_private_summary_test", ROOT / "scripts/vibeci-private.py")
        private = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(private)
        os.environ["VIBECI_PRIVATE_TERMS"] = '["private-provider.invalid","canary"]'
        policy = private.privacy_policy()
        rejected = []

        def check(payload):
            try:
                policy.check(payload)
            except private.PrivacyError:
                rejected.append(payload)
                raise

        screened = mock.Mock(check=mock.Mock(side_effect=check))
        modules = {
            str(ROOT / "scripts/vibeci-private.py"): vars(private),
            str(ROOT / "maintenance/verify.py"): {
                "decode_plan": mock.Mock(return_value={"base": BASE, "manifest": MANIFEST, "version": 8}),
            },
        }
        self.loader.side_effect = modules.__getitem__
        (self.data / "plan.json").write_text("{}", encoding="utf-8")
        poisoned_branch = BRANCH.rsplit("-", 1)[0] + "-" + b"canary".hex()
        with mock.patch.object(private, "privacy_policy", return_value=screened), \
                mock.patch.object(private, "_git", side_effect=AssertionError("Git execution is forbidden")) as git:
            for push in (False, True):
                for changes in ({"branch": poisoned_branch}, {"new": {"note": "private-provider.invalid"}}):
                    self.calls.clear()
                    rejected.clear()
                    with self.subTest(push=push, changes=changes), mock.patch.dict(self.summary, dict(changes, push=push)):
                        with self.assertRaisesRegex(github.GitHubError, "privacy validation failed"):
                            self.publish()
                        self.assertEqual(rejected, [(self.data / "public-summary.json").read_bytes()])
                    self.assertEqual(self.mutations() + self.pushes(), [])
                    self.assertFalse(any("ls-remote" in args or "vibeci%2Fupdate-" in args[-1] for args, _ in self.calls))
            git.assert_not_called()

    def test_privacy_receives_raw_binding_and_only_safe_commit_is_pushed_from_clean_store(self):
        self.assertEqual(self.publish()["status"], "proposal-created")
        self.loader.assert_called_once_with(str(ROOT / "scripts/vibeci-private.py"))
        self.privacy.assert_called_once_with(self.data, RAW_CANDIDATE, BASE, MANIFEST, 8, "main")
        self.assertEqual(len(self.pushes()), 1)
        push = self.pushes()[0]
        self.assertEqual(push[1:3], ["-C", str(self.publisher)])
        self.assertEqual(push[-3:], ["push", f"https://github.com/{REPOSITORY}.git", CANDIDATE + ":refs/heads/" + BRANCH])
        index = next(index for index, (args, _) in enumerate(self.calls) if args == push)
        self.assertIn("ls-remote", self.calls[index - 3][0])
        self.assertEqual(self.calls[index - 2][0][-1], self.endpoint())
        self.assertEqual(self.calls[index - 1][0][-1], self.endpoint("/branches/main"))
        self.assertEqual(self.calls[index + 1][0][-1], self.endpoint("/branches/" + quote(BRANCH, safe="")))
        for args, _ in self.calls:
            self.assertNotIn(RAW_CANDIDATE, " ".join(args))
            self.assertFalse({"--force", "--force-with-lease", "--all", "--mirror", "--tags", "-f"} & set(args))

    def test_privacy_loader_and_checker_failures_are_generic_and_precede_all_public_writes(self):
        canary = "private-provider.invalid dedicated-bot-secret llm-secret canary-private-model"

        def refuse(*args):
            print(canary)
            print(canary, file=sys.stderr)
            raise RuntimeError(canary)

        for push in (False, True):
            self.summary["push"] = push
            self.write_evidence()
            for boundary in (self.loader, self.privacy):
                self.calls.clear()
                stdout, stderr = io.StringIO(), io.StringIO()
                boundary.side_effect = refuse
                try:
                    with self.subTest(push=push, loader=boundary is self.loader), redirect_stdout(stdout), redirect_stderr(stderr):
                        self.assertEqual(github.main(["publish", "--data", str(self.data), "--result", str(self.result)]), 1)
                finally:
                    boundary.side_effect = None
                self.assertIn("publication privacy validation failed", stderr.getvalue())
                public = stdout.getvalue() + stderr.getvalue() + Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
                for value in canary.split():
                    self.assertNotIn(value, public)
                self.assertFalse(Path(os.environ["GITHUB_OUTPUT"]).exists())
                self.assertEqual(self.mutations() + self.pushes(), [])
                self.assertFalse(any("ls-remote" in args for args, _ in self.calls))

    def test_privacy_boundary_rejects_invalid_store_safe_sha_or_swapped_return_values(self):
        linked = self.root / "linked-publisher.git"
        linked.symlink_to(self.publisher, target_is_directory=True)
        invalid = [None, [], (CANDIDATE, self.publisher), (self.result, CANDIDATE), (linked, CANDIDATE),
                   (self.root / "missing", CANDIDATE)]
        invalid += [(self.publisher, sha) for sha in (None, "short", "0" * 40, BASE, MANIFEST, RAW_CANDIDATE)]
        for value in invalid:
            self.privacy.return_value = value
            with self.subTest(value=value), self.assertRaisesRegex(github.GitHubError, "privacy validation failed"):
                self.publish()
        self.assertEqual(self.mutations() + self.pushes(), [])

    def test_privacy_is_not_called_until_raw_evidence_and_repository_identity_are_valid(self):
        for push in (False, True):
            self.summary["push"] = push
            for attribute in ("local_head", "remote_base"):
                with self.subTest(push=push, attribute=attribute), mock.patch.object(self, attribute, OTHER):
                    with self.assertRaises(github.GitHubError):
                        self.publish()
            for changes in ({"full_name": "another/fork"}, {"default_branch": "other"}):
                with self.subTest(push=push, changes=changes), mock.patch.dict(self.repository, changes):
                    with self.assertRaises(github.GitHubError):
                        self.publish()
            with mock.patch.dict(self.outcomes[0], {"fork_head": CANDIDATE}), self.assertRaises(github.GitHubError):
                self.publish()
        self.privacy.assert_not_called()
        self.loader.assert_not_called()
        self.assertEqual(self.mutations() + self.pushes(), [])

    def test_preexisting_remote_proposal_ref_is_never_updated_even_at_safe_sha(self):
        for sha in (CANDIDATE, RAW_CANDIDATE, BASE, OTHER):
            self.remote_ref = sha + "\trefs/heads/" + BRANCH + "\n"
            with self.subTest(sha=sha), self.assertRaisesRegex(github.GitHubError, "branch already exists"):
                self.publish()
        self.assertEqual(self.mutations() + self.pushes(), [])

    def test_git_transport_failure_suppresses_sensitive_output_and_never_creates_a_pr(self):
        self.write_evidence()
        for operation in ("ls-remote", "push"):
            self.calls.clear()
            self.fail_git = operation
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(operation=operation), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(github.main(["publish", "--data", str(self.data), "--result", str(self.result)]), 1)
            public = stdout.getvalue() + stderr.getvalue() + Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text()
            for canary in ("private-provider.invalid", "dedicated-bot-secret", "llm-secret"):
                self.assertNotIn(canary, public)
            self.assertIn("details suppressed", stderr.getvalue())
            self.assertEqual(self.mutations(), [])
            self.assertEqual(len(self.pushes()), int(operation == "push"))

    def test_raw_engine_sha_cannot_satisfy_safe_branch_or_pr_head_binding(self):
        self.remote_candidate = RAW_CANDIDATE
        with self.assertRaisesRegex(github.GitHubError, "does not match the candidate"):
            self.publish()
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.mutations(), [])
        self.calls.clear()
        self.remote_candidate = CANDIDATE
        self.pull["head"]["sha"] = RAW_CANDIDATE
        self.automerge()
        with self.assertRaisesRegex(github.GitHubError, "candidate and base"):
            self.publish()
        self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_nonproducing_and_wrong_push_statuses_are_not_success(self):
        for push in (False, True):
            self.summary["push"] = push
            for status in ("up-to-date", "noop", "skipped", "backoff", "held", "raced", "blocked", "failed", "error", "",
                           "synced"):
                self.outcomes[0]["status"] = status
                with self.subTest(push=push, status=status), self.assertRaisesRegex(github.GitHubError, "not a successful"):
                    self.publish()
        self.assertEqual(self.calls, [])
        self.privacy.assert_not_called()

    def test_result_requires_one_named_repository_and_complete_plan_binding(self):
        valid = copy.deepcopy(self.outcomes)
        invalid = [[], valid * 2, {"repos": valid}, None, [None], [dict(valid[0], repo="other")]]
        for key, value in (("commit", "1234567"), ("commit", "0" * 40), ("commit", BASE),
                           ("commit", None), ("target", OTHER), ("fork_head", OTHER),
                           ("version", 8), ("version", "v8"), ("version", "9"), ("error", "engine failed")):
            invalid.append([dict(valid[0], **{key: value})])
        for key in ("commit", "target", "fork_head", "version", "repo", "status"):
            value = copy.deepcopy(valid)
            value[0].pop(key)
            invalid.append(value)
        for outcomes in invalid:
            self.outcomes = outcomes
            with self.subTest(outcomes=outcomes), self.assertRaises(github.GitHubError):
                self.publish()
        self.assertEqual(self.calls, [])

    def test_candidate_cannot_replace_commit_in_dry_run_results(self):
        candidate_only = dict(self.outcomes[0])
        candidate_only["candidate"] = candidate_only.pop("commit")
        for push in (True, False):
            self.summary["push"] = push
            for fields in ({}, {"commit": None}, {"commit": ""}):
                self.outcomes = [dict(candidate_only, **fields)]
                with self.subTest(push=push, fields=fields), self.assertRaisesRegex(github.GitHubError, "commit SHA"):
                    self.publish()
        self.assertEqual(self.calls, [])

    def test_noop_malformed_or_retargeted_plan_cannot_publish(self):
        for key, value in (("changed", False), ("changed", "true"), ("push", 1), ("push", "true"),
                           ("version", True), ("version", "8"), ("version", 0), ("version", 1 << 63),
                           ("base", "partial"), ("manifest", OTHER), ("branch", "main"),
                           ("branch", "vibeci/update-unbound")):
            with self.subTest(key=key, value=value), mock.patch.dict(self.summary, {key: value}):
                with self.assertRaises(github.GitHubError):
                    self.publish()
        self.assertEqual(self.calls, [])

    def test_nonce_branch_must_be_fully_anchored_and_bound_to_the_plan(self):
        invalid = (BRANCH.rsplit("-", 1)[0], BRANCH[:-1], BRANCH + "0", BRANCH + "\n", BRANCH + "/suffix",
                   BRANCH.upper(), BRANCH.replace("update-8-", "update-9-"),
                   BRANCH.replace(BASE[:12], OTHER[:12]), BRANCH.replace(MANIFEST[:12], OTHER[:12]), None)
        for branch in invalid:
            with self.subTest(branch=branch), mock.patch.dict(self.summary, {"branch": branch}):
                with self.assertRaisesRegex(github.GitHubError, "proposal branch"):
                    self.publish()
        self.assertEqual(self.calls, [])

    def test_publication_rechecks_all_enable_push_event_and_preview_gates(self):
        for changes in ({"VIBECI_ENABLED": "false"}, {"VIBECI_ENABLED": ""}, {"VIBECI_PUSH": "false"},
                        {"VIBECI_PUSH": "TRUE"}, {"GITHUB_EVENT_NAME": "pull_request"},
                        {"GITHUB_EVENT_NAME": "pull_request_target"}, {"GITHUB_EVENT_NAME": "workflow_run"},
                        {"PREVIEW": "true"}, {"PREVIEW": ""}, {"PREVIEW": "False"}):
            with self.subTest(changes=changes), mock.patch.dict(os.environ, changes):
                with self.assertRaisesRegex(github.GitHubError, "publication gates"):
                    self.publish()
        self.assertEqual(self.calls, [])

    def test_schedule_publication_ignores_manual_preview_and_automerge_needs_its_own_flag(self):
        os.environ.update(GITHUB_EVENT_NAME="schedule", PREVIEW="true", VIBECI_AUTOMERGE="true")
        self.assertEqual(self.publish()["status"], "proposal-created")
        self.assertEqual(len(self.mutations()), 1)
        self.calls.clear()
        os.environ.update(AUTOMERGE="true", VIBECI_AUTOMERGE="false")
        with self.assertRaisesRegex(github.GitHubError, "VIBECI_AUTOMERGE"):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_stale_local_or_remote_base_and_wrong_candidate_refuse_pr_creation(self):
        for attribute in ("local_head", "remote_base", "remote_candidate"):
            with self.subTest(attribute=attribute), mock.patch.object(self, attribute, OTHER):
                with self.assertRaises(github.GitHubError):
                    self.publish()
        self.dirty = " M upstream.env\0"
        with self.assertRaisesRegex(github.GitHubError, "dirty"):
            self.publish()
        self.assertEqual(self.mutations(), [])

    def test_base_or_proposal_race_before_creation_leaves_branch_without_pr(self):
        for branch, first in (("main", BASE), (BRANCH, CANDIDATE)):
            self.calls.clear()
            self.sequences = {self.endpoint("/branches/" + quote(branch, safe="")): [
                {"commit": {"sha": first}}, {"commit": {"sha": OTHER}},
            ]}
            with self.subTest(branch=branch), self.assertRaises(github.GitHubError):
                self.publish()
            self.assertEqual(len(self.pushes()), int(branch == BRANCH))
        self.assertEqual(self.mutations(), [])

    def test_selected_pr_opened_after_push_is_reused_but_never_merged_as_a_side_effect(self):
        self.automerge()
        self.pages = [[], [self.pull]]
        self.assertEqual(self.publish(), {"status": "existing-proposal", "published": "false", "pull_request": URL})
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.mutations(), [])

    def test_existing_same_repo_ambiguous_or_wrong_commit_proposals_refuse_new_pr(self):
        invalid = [[self.pull, dict(self.pull, number=18)]]
        for side, key, value in (("head", "ref", "vibeci/update-other"), ("head", "sha", OTHER), ("base", "sha", OTHER)):
            pull = copy.deepcopy(self.pull)
            pull[side][key] = value
            invalid.append([pull])
        for pulls in invalid:
            self.pages = [pulls]
            with self.subTest(pulls=pulls), self.assertRaises(github.GitHubError):
                self.publish()
        self.assertEqual(self.mutations(), [])

    def test_created_pr_url_and_head_are_validated_before_any_merge(self):
        self.automerge()
        self.create_url = "https://github.com/another/repository/pull/17"
        with self.assertRaisesRegex(github.GitHubError, "same-repository"):
            self.publish()
        self.assertEqual([args[2] for args in self.mutations()], ["create"])
        self.calls.clear()
        self.create_url = URL
        self.pull["head"]["repo"]["full_name"] = "another/fork"
        with self.assertRaisesRegex(github.GitHubError, "same-repository"):
            self.publish()
        self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_controller_merge_revalidates_checks_and_uses_atomic_sha_put_without_deferred_merge(self):
        self.automerge()
        self.assertEqual(self.publish(), {"status": "merged", "published": "true", "pull_request": URL, "merge_commit": MERGED})
        self.assertEqual(len(self.mutations()), 2)
        merge = self.mutations()[1]
        self.assertEqual(merge, ["gh", "api", "--hostname", "github.com", "--method", "PUT",
                                 "--raw-field", "sha=" + CANDIDATE, "--raw-field", "merge_method=merge",
                                 self.endpoint("/pulls/17/merge")])
        self.assertEqual(sum(args[-1].endswith("/protection") for args, _ in self.calls), 3)
        self.assertFalse(any("--admin" in args or "--auto" in args or "approve" in args
                             or args[:3] == ["gh", "pr", "merge"] for args, _ in self.calls))
        self.assertEqual(len(self.pushes()), 1)
        self.sleep_mock.assert_not_called()

    def test_controller_waits_for_initially_absent_pending_and_additional_pr_checks(self):
        self.automerge()
        self.view["statusCheckRollup"].append({"__typename": "StatusContext", "context": "External validation", "state": "SUCCESS"})
        pending = copy.deepcopy(self.view)
        pending["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion="")
        pending["statusCheckRollup"][-1]["state"] = "PENDING"
        expected = copy.deepcopy(pending)
        expected["statusCheckRollup"][-1]["state"] = "EXPECTED"
        self.sequences["view"] = [dict(self.view, statusCheckRollup=None), dict(self.view, statusCheckRollup=[]), expected, pending, self.view]
        self.assertEqual(self.publish()["status"], "merged")
        self.assertEqual(self.sleep_mock.call_args_list, [mock.call(20)] * 4)
        self.assertEqual(self.now, 80)
        self.assertEqual(sum(args[-1].endswith("/protection") for args, _ in self.calls), 11)

    def test_failed_cancelled_skipped_ambiguous_or_invalid_checks_never_merge(self):
        self.automerge()
        good = copy.deepcopy(self.view["statusCheckRollup"])
        invalid = [[dict(good[0], conclusion=conclusion), good[1]] for conclusion in (
            "FAILURE", "CANCELLED", "SKIPPED", "NEUTRAL", "TIMED_OUT", "ACTION_REQUIRED", "", None,
        )]
        invalid += [good + [good[0]], [dict(good[0], status={}), good[1]], [None], {},
                    [{"__typename": "StatusContext", "context": github.REQUIRED_CONTEXTS[0], "state": "SUCCESS"}, good[1]],
                    good + [{"__typename": "StatusContext", "context": "External", "state": "FAILURE"}]]
        for nodes in invalid:
            self.calls.clear()
            self.view["statusCheckRollup"] = nodes
            with self.subTest(nodes=nodes), self.assertRaises(github.GitHubError):
                self.publish()
            self.assertEqual([args[2] for args in self.mutations()], ["create"])
        self.sleep_mock.assert_not_called()

    def test_missing_or_pending_required_checks_time_out_without_a_deferred_action(self):
        self.automerge()
        self.assertEqual(github.CHECK_TIMEOUT, 7200)
        self.assertEqual(github.CHECK_INTERVAL, 20)
        good = copy.deepcopy(self.view["statusCheckRollup"])
        for nodes in ([], good[:1], [dict(node, status="QUEUED", conclusion=None) for node in good]):
            self.calls.clear()
            self.sleep_mock.reset_mock()
            self.now = 0
            self.view["statusCheckRollup"] = nodes
            with self.subTest(nodes=nodes), mock.patch.object(github, "CHECK_TIMEOUT", 60):
                with self.assertRaisesRegex(github.GitHubError, "missing or pending.*deadline"):
                    self.publish()
            self.assertEqual(self.now, 60)
            self.assertEqual(self.sleep_mock.call_args_list, [mock.call(20)] * 3)
            self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_required_extra_contexts_must_also_complete_successfully(self):
        self.automerge()
        checks = self.protection["required_status_checks"]
        checks["contexts"].append("External validation")
        checks["checks"].append({"context": "External validation", "app_id": 123})
        with mock.patch.object(github, "CHECK_TIMEOUT", 20), self.assertRaisesRegex(github.GitHubError, "deadline"):
            self.publish()
        self.assertEqual([args[2] for args in self.mutations()], ["create"])
        self.calls.clear()
        self.view["statusCheckRollup"].append({"__typename": "StatusContext", "context": "External validation", "state": "SUCCESS"})
        self.assertEqual(self.publish()["status"], "merged")

    def test_delayed_head_base_or_protection_changes_stop_the_controller(self):
        self.automerge()
        original_view, original_protection, original_pull = copy.deepcopy((self.view, self.protection, self.pull))
        changes = [lambda: setattr(self, "remote_candidate", OTHER), lambda: setattr(self, "remote_base", OTHER),
                   lambda: self.view.update(headRefOid=OTHER), lambda: self.view.update(baseRefOid=OTHER),
                   lambda: self.pull["head"].update(sha=OTHER),
                   lambda: self.protection["required_status_checks"].update(strict=False),
                   lambda: self.protection["required_status_checks"]["checks"][0].update(app_id=999)]
        for index, change in enumerate(changes):
            self.calls.clear()
            self.sleep_mock.reset_mock()
            self.now = 0
            self.remote_candidate, self.remote_base = CANDIDATE, BASE
            self.view, self.protection, self.pull = copy.deepcopy((original_view, original_protection, original_pull))
            self.view["statusCheckRollup"][0].update(status="IN_PROGRESS", conclusion="")
            self.on_sleep = change
            with self.subTest(change=index), self.assertRaises(github.GitHubError):
                self.publish()
            self.assertEqual(self.sleep_mock.call_args_list, [mock.call(20)])
            self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_head_changed_during_check_query_is_rechecked_before_wait_or_merge(self):
        self.automerge()
        for pending in (False, True):
            self.calls.clear()
            self.remote_candidate = CANDIDATE
            self.view["statusCheckRollup"][0].update(status="IN_PROGRESS" if pending else "COMPLETED",
                                                    conclusion="" if pending else "SUCCESS")
            self.after_view = lambda: setattr(self, "remote_candidate", OTHER)
            with self.subTest(pending=pending), self.assertRaisesRegex(github.GitHubError, "branch moved"):
                self.publish()
            self.assertEqual([args[2] for args in self.mutations()], ["create"])
        self.sleep_mock.assert_not_called()

    def test_changed_required_contexts_after_success_refuse_merge(self):
        self.automerge()
        self.after_view = lambda: self.protection["required_status_checks"]["contexts"].append("New required check")
        with self.assertRaisesRegex(github.GitHubError, "configuration changed"):
            self.publish()
        self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_atomic_merge_rejects_a_head_race_after_the_final_read(self):
        self.automerge()
        self.before_merge = lambda: setattr(self, "remote_candidate", OTHER)
        with self.assertRaises(github.GitHubError):
            self.publish()
        self.assertEqual(len(self.mutations()), 2)
        self.assertEqual(self.mutations()[-1][-1], self.endpoint("/pulls/17/merge"))
        self.assertIn("sha=" + CANDIDATE, self.mutations()[-1])

    def test_protected_merge_response_and_additional_rule_rejections_fail_closed(self):
        self.automerge()
        for reply in (None, {}, {"merged": False, "sha": MERGED}, {"merged": "true", "sha": MERGED},
                      {"merged": True, "sha": "short"}, {"merged": True, "sha": "0" * 40},
                      {"merged": True, "sha": CANDIDATE}, {"merged": True, "sha": BASE}):
            self.merge_reply = reply
            with self.subTest(reply=reply), self.assertRaises(github.GitHubError):
                self.publish()
        self.fail_endpoint = self.endpoint("/pulls/17/merge")
        with self.assertRaises(github.GitHubError):
            self.publish()
        self.assertFalse(any(args[:3] in (["gh", "pr", "merge"], ["gh", "pr", "review"]) for args, _ in self.calls))

    def test_protection_or_repository_settings_changed_after_creation_refuse_automerge(self):
        self.automerge()
        weak = copy.deepcopy(self.protection)
        weak["required_status_checks"]["strict"] = False
        cases = [
            {self.endpoint("/branches/main/protection"): [self.protection, weak]},
            {self.endpoint(): [self.repository, self.repository, dict(self.repository, allow_merge_commit=False)]},
        ]
        for sequences in cases:
            self.calls.clear()
            self.sequences = sequences
            with self.subTest(sequences=sequences), self.assertRaises(github.GitHubError):
                self.publish()
            self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_branch_movement_after_creation_and_just_before_merge_is_refused(self):
        self.automerge()
        for branch, first, unchanged_reads in ((BRANCH, CANDIDATE, 2), ("main", BASE, 3), ("main", BASE, 4)):
            self.calls.clear()
            self.sequences = {self.endpoint("/branches/" + quote(branch, safe="")):
                              [{"commit": {"sha": first}}] * unchanged_reads + [{"commit": {"sha": OTHER}}]}
            with self.subTest(branch=branch, unchanged_reads=unchanged_reads), self.assertRaisesRegex(github.GitHubError, "moved"):
                self.publish()
            self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_pr_changed_or_paused_before_automerge_is_refused(self):
        self.automerge()
        changes = [{"draft": True}, {"state": "closed"}, {"merged": True},
                   {"head": dict(self.pull["head"], sha=OTHER)}, {"base": dict(self.pull["base"], ref="other")}]
        for change in changes:
            self.calls.clear()
            self.sequences = {self.endpoint("/pulls/17"): [self.pull, dict(self.pull, **change)]}
            with self.subTest(change=change), self.assertRaises(github.GitHubError):
                self.publish()
            self.assertEqual([args[2] for args in self.mutations()], ["create"])

    def test_bad_json_symlinks_and_oversized_evidence_are_rejected(self):
        for value in ('{"a":1,"a":2}', '{"nested":{"a":1,"a":2}}', '{"a":NaN}', '{"a":1e9999}', 'not JSON'):
            with self.subTest(value=value), self.assertRaises(github.GitHubError):
                github.decode_json(value)
        self.write_evidence()
        link = self.root / "linked-result.json"
        link.symlink_to(self.result)
        with self.assertRaisesRegex(github.GitHubError, "symlink"):
            github.publish(self.data, link)
        with mock.patch.object(github, "MAX_JSON_BYTES", 1), self.assertRaisesRegex(github.GitHubError, "size limit"):
            github.publish(self.data, self.result)
        self.assertEqual(self.calls, [])

    def test_sanitized_result_preserves_only_validated_structural_fields(self):
        secret = "provider-secret-marker"
        self.outcomes[0].update(status="failed", upstream=OTHER, pinned="7", message=secret, error=secret,
                                usage={secret: 1}, model=secret, headers={"Authorization": secret},
                                patches={"total": 3, "shifted": 1, "agent": 0, secret: 123, "message": secret})
        self.write_evidence()
        safe = github.sanitize_result(self.result)
        self.assertEqual(safe, [{"repo": "maintainedmost", "status": "failed", "commit": RAW_CANDIDATE,
                                 "fork_head": BASE, "upstream": OTHER, "target": MANIFEST, "version": 8, "pinned": 7,
                                 "patches": {"total": 3, "shifted": 1, "agent": 0}}])
        self.assertNotIn(secret, json.dumps(safe))
        self.assertIn(secret, self.result.read_text())
        self.assertEqual(self.calls, [])

    def test_malformed_or_secret_bearing_structural_result_fields_become_generic_errors(self):
        secret = "provider-secret-marker"
        invalid = [[], self.outcomes * 2, {"message": secret}]
        for key, value in (("repo", secret), ("status", secret), ("commit", secret), ("fork_head", secret),
                           ("upstream", secret), ("target", secret), ("version", secret), ("pinned", True),
                           ("version", -1), ("version", 1 << 63), ("patches", [secret]),
                           ("patches", {"total": -1}), ("patches", {"total": True}), ("patches", {"total": 1.5})):
            invalid.append([dict(self.outcomes[0], **{key: value})])
        texts = [json.dumps(value) for value in invalid] + [secret, '{"message":"' + secret, '{"a":1,"a":2}', '{"a":NaN}']
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            for text in texts:
                self.result.write_text(text)
                with self.subTest(text=text):
                    self.assertEqual(github.sanitize_result(self.result), [{"repo": "maintainedmost", "status": "error"}])
            self.write_evidence()
            with mock.patch.object(github, "MAX_JSON_BYTES", 1):
                self.assertEqual(github.sanitize_result(self.result), [{"repo": "maintainedmost", "status": "error"}])
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        self.assertEqual(self.calls, [])

    def test_cli_outputs_and_failures_are_reported_without_command_secrets(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(github.main(["preflight"]), 0)
        self.assertEqual(json.loads(stdout.getvalue())["skip"], "false")
        self.assertIn("skip=false\n", Path(os.environ["GITHUB_OUTPUT"]).read_text())
        self.assertIn("Preflight passed", Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text())
        self.fail_endpoint = self.endpoint()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(github.main(["preflight"]), 1)
        self.assertIn("refused", Path(os.environ["GITHUB_STEP_SUMMARY"]).read_text())
        self.assertNotIn("dedicated-bot-secret", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("llm-secret", stdout.getvalue() + stderr.getvalue())
        self.assertEqual(self.mutations(), [])


class CleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "run with spaces"
        self.root.mkdir()
        self.instance = "path:" + str(self.root / "data")
        self.owned = ["a" * 64, "b" * 64]
        self.unrelated = ["c" * 64, "d" * 64]
        self.containers = {
            self.owned[0]: {"vibeci.managed": "true", "vibeci.instance": self.instance},
            self.owned[1]: {"vibeci.managed": "true", "vibeci.instance": self.instance},
            self.unrelated[0]: {"vibeci.managed": "true", "vibeci.instance": "path:/another/run/data"},
            self.unrelated[1]: {"vibeci.managed": "false", "vibeci.instance": self.instance},
        }
        self.calls, self.query_outputs = [], []
        self.down_error = self.remove_error = self.leave_workers = False
        self.query_failures = set()
        self.queries = 0
        environment = mock.patch.dict(os.environ, {
            "PATH": os.defpath, "COMPOSE_PROJECT_NAME": "maintainedmost-vibeci-fixture-123-1",
            "VIBECI_IMAGE": "registry.example.invalid/engine@sha256:" + "a" * 64,
            "VIBECI_SANDBOX_IMAGE": "registry.example.invalid/guard@sha256:" + "b" * 64,
            "VIBECI_GIT_TOKEN": "secret-marker", "VIBECI_LLM_API_KEY": "secret-marker",
            "GH_TOKEN": "secret-marker", "GITHUB_TOKEN": "secret-marker",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        process = mock.patch.object(github.subprocess, "run", side_effect=self.docker)
        process.start()
        self.addCleanup(process.stop)

    def docker(self, args, **options):
        self.calls.append(args)
        self.assertEqual(options["timeout"], 60)
        self.assertEqual(options["env"]["COMPOSE_PROJECT_NAME"], "maintainedmost-vibeci-fixture-123-1")
        self.assertEqual(options["env"]["VIBECI_RUN_DIR"], str(self.root))
        self.assertFalse(options.get("shell", False))
        self.assertFalse({"GH_TOKEN", "GITHUB_TOKEN", "VIBECI_GIT_TOKEN", "VIBECI_LLM_API_KEY"} & options["env"].keys())
        code, output = 0, ""
        if args == ["docker", "compose", "-f", "maintenance/compose.yaml", "down", "--volumes"]:
            if self.down_error == "timeout":
                raise subprocess.TimeoutExpired(args, 60)
            code = int(self.down_error)
        elif args[:2] == ["docker", "ps"]:
            self.queries += 1
            self.assertEqual(args, ["docker", "ps", "-aq", "--no-trunc", "--filter", "label=vibeci.managed=true",
                                    "--filter", "label=vibeci.instance=" + self.instance])
            if self.queries in self.query_failures:
                code = 1
            elif self.query_outputs:
                output = self.query_outputs.pop(0)
            else:
                filters = dict(args[index + 1].removeprefix("label=").split("=", 1)
                               for index, value in enumerate(args) if value == "--filter")
                output = "".join(container + "\n" for container, labels in self.containers.items()
                                 if all(labels.get(key) == value for key, value in filters.items()))
        elif args[:3] == ["docker", "rm", "-f"]:
            self.assertTrue(args[3:])
            self.assertTrue(all(re.fullmatch(r"[0-9a-f]{64}", value) for value in args[3:]))
            self.assertFalse(set(args[3:]) & set(self.unrelated))
            code = int(self.remove_error)
            if not code and not self.leave_workers:
                for container in args[3:]:
                    del self.containers[container]
        else:
            self.fail("unexpected Docker command: " + repr(args))
        return subprocess.CompletedProcess(args, code, output, "secret-marker" if code else "")

    def test_cleanup_removes_only_both_label_matches_using_full_ids_then_requeries(self):
        self.assertEqual(github.cleanup(self.root), {"status": "cleaned"})
        self.assertEqual(self.calls[0], ["docker", "compose", "-f", "maintenance/compose.yaml", "down", "--volumes"])
        self.assertIn(["docker", "rm", "-f", *self.owned], self.calls)
        self.assertEqual(self.queries, 2)
        self.assertEqual(set(self.containers), set(self.unrelated))

    def test_empty_selection_never_invokes_docker_rm(self):
        for container in self.owned:
            del self.containers[container]
        self.assertEqual(github.cleanup(self.root)["status"], "cleaned")
        self.assertFalse(any(args[:2] == ["docker", "rm"] for args in self.calls))
        self.assertEqual(self.queries, 2)
        self.assertEqual(set(self.containers), set(self.unrelated))

    def test_compose_failure_still_removes_workers_and_preserves_failed_exit(self):
        for failure in (True, "timeout"):
            self.calls.clear()
            self.queries = 0
            for container in self.owned:
                self.containers[container] = {"vibeci.managed": "true", "vibeci.instance": self.instance}
            self.down_error = failure
            with self.subTest(failure=failure), self.assertRaisesRegex(github.GitHubError, "Compose shutdown failed"):
                github.cleanup(self.root)
            self.assertIn(["docker", "rm", "-f", *self.owned], self.calls)
            self.assertEqual(self.queries, 2)
            self.assertEqual(set(self.containers), set(self.unrelated))

    def test_short_malformed_or_excessive_ids_fail_without_removing_anything(self):
        for output in ("a" * 12 + "\n", "--all\n", self.owned[0] + "\ninvalid\n", "A" * 64 + "\n",
                       "a" * 64 + ";other\n", ("a" * 64 + "\n") * 1025):
            self.calls.clear()
            self.queries = 0
            self.query_outputs = [output]
            with self.subTest(output=output[:80]), self.assertRaisesRegex(github.GitHubError, "worker removal failed"):
                github.cleanup(self.root)
            self.assertFalse(any(args[:2] == ["docker", "rm"] for args in self.calls))
            self.assertEqual(self.queries, 2)
            self.assertEqual(len(self.containers), 4)

    def test_remove_failure_remaining_workers_and_query_failures_are_not_success(self):
        for mode in ("remove", "remaining", "initial-query", "final-query"):
            self.calls.clear()
            self.queries = 0
            for container in self.owned:
                self.containers[container] = {"vibeci.managed": "true", "vibeci.instance": self.instance}
            self.remove_error = mode == "remove"
            self.leave_workers = mode == "remaining"
            self.query_failures = {1} if mode == "initial-query" else {2} if mode == "final-query" else set()
            with self.subTest(mode=mode), self.assertRaises(github.GitHubError):
                github.cleanup(self.root)
            self.assertEqual(self.queries, 2)
            self.assertTrue(set(self.unrelated) <= self.containers.keys())

    def test_cleanup_cli_failure_is_generic_and_invalid_scope_runs_nothing(self):
        self.down_error = True
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(github.main(["cleanup", "--run-dir", str(self.root)]), 1)
        self.assertIn("Compose shutdown failed", stderr.getvalue())
        self.assertNotIn("secret-marker", stdout.getvalue() + stderr.getvalue())
        self.calls.clear()
        for directory in (Path("relative"), Path("/")):
            with self.subTest(directory=directory), self.assertRaises(github.GitHubError):
                github.cleanup(directory)
        with mock.patch.dict(os.environ, {"COMPOSE_PROJECT_NAME": ""}), self.assertRaises(github.GitHubError):
            github.cleanup(self.root)
        self.assertEqual(self.calls, [])


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = (ROOT / ".github/workflows/maintain.yml").read_text(encoding="utf-8")
        cls.compose = (ROOT / "maintenance/compose.yaml").read_text(encoding="utf-8")
        cls.harness = cls.compose.split("\n  vibeci:\n", 1)[1].split("\n  probe:\n", 1)[0]
        cls.probe = cls.compose.split("\n  probe:\n", 1)[1].split("\nvolumes:\n", 1)[0]
        cls.probe_script = textwrap.dedent(cls.probe.split("      - |\n", 1)[1].split("    volumes:", 1)[0])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.checkout = self.root / "checkout"
        self.checkout.mkdir()
        self.runner = self.root / "runner"
        self.runner.mkdir()
        self.models = {
            "providers": {"canary-provider": {"type": "openai-chat", "base_url": "https://private-provider.invalid/v1",
                                              "api_key": "file:/run/secrets/llm_api_key"}},
            "models": {"canary-model": {"provider": "canary-provider", "model": "canary-private-model $(never execute this)"}},
            "roles": {"audit": "canary-model", "resolve": ["canary-model"]},
        }
        self.env = {"PATH": os.defpath, "HOME": str(self.root),
                    "VIBECI_IMAGE": "registry.example.invalid/engine@sha256:" + "a" * 64,
                    "VIBECI_SANDBOX_IMAGE": "registry.example.invalid/sandbox@sha256:" + "b" * 64,
                    "VIBECI_MODELS": json.dumps(self.models),
                    "VIBECI_PRIVATE_TERMS": '["private-provider.invalid","canary-private-model"]',
                    "VIBECI_LLM_API_KEY": "canary-llm-key", "GH_TOKEN": "canary-read-only-token",
                    "GITHUB_REPOSITORY": REPOSITORY, "BASE_BRANCH": "main",
                    "COMPOSE_PROJECT_NAME": "maintainedmost-vibeci-fixture-123-1",
                    "VIBECI_ENABLED": "true", "VIBECI_PUSH": "true", "VIBECI_AUTOMERGE": "false",
                    "GITHUB_EVENT_NAME": "workflow_dispatch", "PREVIEW": "true",
                    "RUNNER_TEMP": str(self.runner), "GITHUB_WORKSPACE": str(self.checkout),
                    "GITHUB_ENV": str(self.root / "env"), "GITHUB_OUTPUT": str(self.root / "outputs"),
                    "GITHUB_STEP_SUMMARY": str(self.root / "job-summary")}
        self.changed = True
        self.plan = mock.Mock(side_effect=self.planned_update)
        load = runtime.module
        planner = dict(load("vibeci-plan.py"), plan=self.plan)
        modules = mock.patch.object(runtime, "module", side_effect=lambda name: planner if name == "vibeci-plan.py" else load(name))
        modules.start()
        self.addCleanup(modules.stop)

    def planned_update(self, checkout, repository, branch, data, models, push=False):
        """Replace Git planning only; runtime validation and sealing stay real."""
        data.mkdir(mode=0o700)
        summary = {"changed": self.changed, "push": push, "base": BASE, "manifest": MANIFEST, "version": 8, "branch": BRANCH}
        runtime.write_private(data / "summary.json", (json.dumps(summary) + "\n").encode())
        runtime.write_private(data / "summary.md", b"# Trusted planner summary\n")
        if self.changed:
            runtime.write_private(data / "plan.json", json.dumps({"base": BASE, "manifest": MANIFEST, "version": 8}).encode())
            runtime.write_private(data / "config.json", models.read_bytes())
        return summary

    def step(self, identifier):
        steps = re.split(r"(?m)(?=^      - )", self.workflow)
        return next(step for step in steps if f"        id: {identifier}\n" in step)

    def shell(self, identifier):
        code = self.step(identifier).split("        run: ", 1)[1]
        return textwrap.dedent(code[2:]) if code.startswith("|\n") else code.splitlines()[0] + "\n"

    def prepare(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            runtime.prepare()
        root = Path(Path(self.env["GITHUB_ENV"]).read_text().splitlines()[-1].split("=", 1)[1])
        self.env["VIBECI_RUN_DIR"] = str(root)
        return root

    def test_workflow_is_opt_in_read_only_default_branch_and_non_cancelling(self):
        self.assertIn("    environment: maintenance\n", self.workflow)
        for name in ("build.yml", "release.yml", "upstream-check.yml"):
            other = (ROOT / ".github/workflows" / name).read_text()
            self.assertNotRegex(other, r"(?m)^\s*environment:\s*maintenance\s*$")
        self.assertIn("cron: '17 7 * * *'", self.workflow)
        self.assertRegex(self.workflow, r"preview:\n(?:.*\n){1,3}        default: true")
        self.assertIn("permissions:\n  contents: read\n  pull-requests: read\n", self.workflow)
        self.assertNotIn(": write", self.workflow)
        self.assertIn("if: vars.VIBECI_ENABLED == 'true'", self.workflow)
        self.assertIn("runs-on: ubuntu-latest", self.workflow)
        self.assertIn("timeout-minutes: 270", self.workflow)
        self.assertIn("group: maintainedmost-vibeci\n  cancel-in-progress: false", self.workflow)
        self.assertIn("ref: ${{ github.event.repository.default_branch }}", self.workflow)
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("fetch-depth: 0", self.workflow)
        self.assertNotIn("pull_request_target:", self.workflow)
        self.assertNotIn("pull_request:", self.workflow)
        self.assertNotIn("secrets.GITHUB_TOKEN", self.workflow)
        for identifier in ("preflight", "publish"):
            self.assertIn("GH_TOKEN: ${{ secrets.VIBECI_GIT_TOKEN }}", self.step(identifier))
        self.assertEqual(self.shell("preflight").strip(), "python3 -B scripts/vibeci-github.py preflight")
        self.assertNotIn("--admin", self.workflow)
        self.assertNotIn("secrets.", self.workflow.split("    steps:\n", 1)[0])
        self.assertNotIn("vars.VIBECI_MODELS", self.workflow)
        self.assertNotIn("vars.VIBECI_PRIVATE_TERMS", self.workflow)
        for identifier in ("runtime", "publish", "evidence"):
            for variable in ("VIBECI_MODELS", "VIBECI_PRIVATE_TERMS", "VIBECI_LLM_API_KEY"):
                self.assertIn(variable + ": ${{ secrets." + variable + " }}", self.step(identifier))
        self.assertIn("GH_TOKEN: ${{ github.token }}", self.step("runtime"))
        for identifier in ("runtime", "broker", "engine"):
            self.assertNotIn("secrets.VIBECI_GIT_TOKEN", self.step(identifier))

    def test_runtime_planner_interface_preserves_all_push_and_preview_gates(self):
        self.assertIn("if: steps.preflight.outputs.skip == 'false'", self.step("runtime"))
        self.assertEqual(self.shell("runtime").strip(), "python3 -B scripts/vibeci-runtime.py prepare")
        for variable in ("VIBECI_ENABLED", "VIBECI_PUSH", "VIBECI_AUTOMERGE"):
            self.assertIn(variable + ": ${{ vars." + variable + " }}", self.workflow)
        self.assertIn("PREVIEW: ${{ github.event_name != 'workflow_dispatch' || inputs.preview }}", self.workflow)
        self.assertIn("vars.VIBECI_AUTOMERGE == 'true' && vars.VIBECI_PUSH == 'true'", self.workflow)
        self.assertIn("github.event_name == 'schedule' || (github.event_name == 'workflow_dispatch' && inputs.preview == false)", self.workflow)
        self.assertNotRegex(self.workflow, r"(?m)^\s*(?:npm|pnpm|bun|node|eval|source)[ \t]+(?![=\s])")
        for changes, push in (({}, False), ({"PREVIEW": "false"}, True), ({"GITHUB_EVENT_NAME": "schedule"}, True),
                              ({"PREVIEW": "false", "VIBECI_ENABLED": "false"}, False),
                              ({"PREVIEW": "false", "VIBECI_PUSH": "false"}, False),
                              ({"PREVIEW": "false", "VIBECI_PUSH": "TRUE"}, False),
                              ({"PREVIEW": "false", "GITHUB_EVENT_NAME": "pull_request"}, False),
                              ({"PREVIEW": "False"}, False), ({"PREVIEW": ""}, False)):
            with self.subTest(changes=changes), mock.patch.dict(self.env, changes):
                root = self.prepare()
                self.plan.assert_called_with(self.checkout, REPOSITORY, "main", root / "data", root / "models.json", push=push)
                self.assertIs(json.loads((root / "public-inputs/summary.json").read_text())["push"], push)
                self.assertEqual(Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines()[-1], "changed=true")
        self.changed = False
        self.prepare()
        self.assertEqual(Path(self.env["GITHUB_OUTPUT"]).read_text().splitlines()[-1], "changed=false")

    def test_runtime_is_unique_external_and_emits_only_offline_pinned_profile(self):
        root = self.prepare()
        other = self.prepare()
        self.assertNotEqual(root, other)
        self.assertEqual(root.parent, self.runner.resolve())
        self.assertTrue((root / "data").is_dir())
        self.assertEqual(root.stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "public-inputs").stat().st_mode & 0o777, 0o700)
        self.assertEqual((root / "models.json").read_text(), self.env["VIBECI_MODELS"])
        self.assertEqual((root / ".runtime-owner").read_bytes(), b"maintainedmost-vibeci\n")
        for name in ("summary.md", "summary.json", "plan.json"):
            self.assertEqual((root / "public-inputs" / name).read_bytes(), (root / "data" / name).read_bytes())
        for name in ("models.json", "public-inputs/summary.md", "public-inputs/summary.json", "public-inputs/plan.json"):
            self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual((root / "sandboxd.json").stat().st_mode & 0o777, 0o444)
        policy = json.loads((root / "sandboxd.json").read_text())
        self.assertIn(type(policy["profiles"]["maintainedmost"]["cpus"]), (int, float))
        self.assertEqual(policy, {
            "listen": "/run/vibeci/sandboxd.sock", "docker_host": "unix:///var/run/docker.sock",
            "data_root": "/data", "data_host_path": str(root / "data"),
            "profiles": {"maintainedmost": {"image": self.env["VIBECI_SANDBOX_IMAGE"], "user": "10001:10001",
                                           "memory": "4g", "cpus": 2, "pids": 512, "tmp_size": "1g"}},
        })
        self.assertNotIn("VIBECI_MODELS=", Path(self.env["GITHUB_ENV"]).read_text())
        self.assertIn('docker pull "$VIBECI_IMAGE"', self.workflow)
        self.assertIn('docker pull "$VIBECI_SANDBOX_IMAGE"', self.workflow)
        self.assertLess(self.workflow.index("scripts/vibeci-runtime.py prepare"), self.workflow.index("docker pull"))
        preload = next(step for step in re.split(r"(?m)(?=^      - )", self.workflow) if 'docker pull "$VIBECI_IMAGE"' in step)
        self.assertIn("if: steps.runtime.outputs.changed == 'true'", preload)

    def test_bad_images_fail_before_any_runtime_material_is_created(self):
        for key in ("VIBECI_IMAGE", "VIBECI_SANDBOX_IMAGE"):
            for image in ("", "registry/image:latest", "registry/image@sha256:abc", "registry/image@sha256:" + "A" * 64,
                          "--option@sha256:" + "a" * 64, "registry/image@sha256:" + "a" * 64 + ";id"):
                with self.subTest(key=key, image=image), mock.patch.dict(self.env, {key: image}):
                    with self.assertRaises(ValueError):
                        self.prepare()
        self.plan.assert_not_called()
        self.assertEqual(list(self.runner.iterdir()), [])

    def test_private_model_secret_requires_the_canonical_file_credential_reference(self):
        for reference in ("env:VIBECI_LLM_API_KEY", "file:/tmp/another-key", "canary-inline-key"):
            models = copy.deepcopy(self.models)
            models["providers"]["canary-provider"]["api_key"] = reference
            with self.subTest(reference=reference), mock.patch.dict(self.env, {"VIBECI_MODELS": json.dumps(models)}):
                with self.assertRaises(ValueError):
                    self.prepare()
        self.plan.assert_not_called()
        self.assertEqual(list(self.runner.iterdir()), [])

    def test_broker_ownership_probe_and_run_failure_contract(self):
        broker, engine = self.step("broker"), self.step("engine")
        self.assertIn('sudo chown -R --no-dereference 10001:10001 "$VIBECI_RUN_DIR/data"', broker)
        self.assertIn("docker compose -f maintenance/compose.yaml up -d sandboxd", broker)
        self.assertIn("docker compose -f maintenance/compose.yaml run --rm -T probe", broker)
        self.assertLess(broker.index("up -d sandboxd"), broker.index("run --rm -T probe"))
        self.assertNotIn("sudo install", broker)
        self.assertNotIn("curl", broker)
        self.assertNotIn("timeout", broker)
        self.assertNotIn("$VIBECI_RUN_DIR/socket", self.workflow)
        self.assertNotIn("test -S", broker)
        self.assertIn("if: steps.runtime.outputs.changed == 'true'", broker)
        self.assertIn("if: steps.runtime.outputs.changed == 'true'", engine)
        self.assertEqual(self.shell("engine").strip(), "python3 -B scripts/vibeci-runtime.py engine")
        self.assertIn("VIBECI_LLM_API_KEY: ${{ secrets.VIBECI_LLM_API_KEY }}", engine)
        self.assertIn("VIBECI_FORK_READ_TOKEN: ${{ github.token }}", engine)
        self.assertNotIn("GH_TOKEN:", engine)
        self.assertNotIn("VIBECI_GIT_TOKEN", engine)
        self.assertNotIn("VIBECI_MODELS", engine)
        self.assertNotIn("VIBECI_PRIVATE_TERMS", engine)
        self.assertNotIn("continue-on-error", self.workflow)
        self.assertNotIn("|| true", self.workflow)

    def test_control_socket_volume_is_private_and_probe_has_no_network_or_credentials(self):
        self.assertIn("COMPOSE_PROJECT_NAME: maintainedmost-vibeci-${{ github.run_id }}-${{ github.run_attempt }}", self.workflow)
        self.assertEqual(self.compose.split("\nvolumes:\n", 1)[1].split("\nsecrets:\n", 1)[0].strip(), "socket: {}")
        self.assertEqual(self.compose.count("      - socket:/run/vibeci\n"), 2)
        self.assertNotIn("${VIBECI_RUN_DIR}/socket", self.compose)
        self.assertIn("image: ${VIBECI_SANDBOX_IMAGE:", self.probe)
        self.assertIn("pull_policy: never", self.probe)
        self.assertIn('user: "10001:10001"', self.probe)
        self.assertIn("network_mode: none", self.probe)
        self.assertIn("entrypoint: [python3]", self.probe)
        self.assertIn("      - socket:/run/vibeci:ro", self.probe)
        self.assertNotIn("environment:", self.probe)
        self.assertNotIn("VIBECI_GIT_TOKEN", self.probe)
        self.assertNotIn("VIBECI_LLM_API_KEY", self.probe)
        self.assertNotIn("docker.sock", self.probe)
        self.assertNotIn("secrets:", self.probe)
        self.assertNotIn("environment:", self.compose)
        self.assertIn("secrets: [fork_read_token, llm_api_key]", self.harness)
        self.assertNotIn("docker.sock", self.harness)
        self.assertIn("fork_read_token:\n    file: ${VIBECI_RUN_DIR:?set the isolated CI runtime directory}/secrets/fork_read_token", self.compose)
        self.assertIn("llm_api_key:\n    file: ${VIBECI_RUN_DIR}/secrets/llm_api_key", self.compose)
        self.assertIn("  logging:\n    driver: none", self.compose.split("\nservices:\n", 1)[0])
        self.assertEqual(self.compose.count("    <<: *hardening\n"), 3)

    def test_probe_waits_for_http_health_not_merely_a_connected_socket(self):
        connections = [mock.Mock() for _ in range(3)]
        sockets = [mock.Mock() for _ in range(3)]
        sockets[0].connect.side_effect = FileNotFoundError("socket not ready")
        connections[1].getresponse.return_value.status = 503
        connections[2].getresponse.return_value.status = 200
        with mock.patch("http.client.HTTPConnection", side_effect=connections) as http, \
                mock.patch("socket.socket", side_effect=sockets) as unix, \
                mock.patch("time.monotonic", side_effect=[0, 0, 1, 2]), \
                mock.patch("time.sleep") as sleep:
            exec(compile(self.probe_script, "compose.yaml probe", "exec"), {})
        self.assertEqual(http.call_args_list, [mock.call("localhost", timeout=2)] * 3)
        self.assertEqual(unix.call_args_list, [mock.call(socket.AF_UNIX)] * 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1)] * 2)
        connections[0].request.assert_not_called()
        for connection in connections[1:]:
            connection.request.assert_called_once_with("GET", "/v1/health")
        for connection, unix_socket in zip(connections, sockets):
            unix_socket.connect.assert_called_once_with("/run/vibeci/sandboxd.sock")
            unix_socket.settimeout.assert_called_once_with(2)
            connection.close.assert_called_once_with()

    def test_probe_fails_at_its_60_second_deadline(self):
        for response in (503, http.client.HTTPException("invalid health response")):
            connection = mock.Mock()
            if isinstance(response, Exception):
                connection.getresponse.side_effect = response
            else:
                connection.getresponse.return_value.status = response
            with self.subTest(response=response), \
                    mock.patch("http.client.HTTPConnection", return_value=connection), \
                    mock.patch("socket.socket"), \
                    mock.patch("time.monotonic", side_effect=[0, 0, 59, 60]) as clock, \
                    mock.patch("time.sleep") as sleep:
                with self.assertRaisesRegex(SystemExit, "did not become healthy"):
                    exec(compile(self.probe_script, "compose.yaml probe", "exec"), {})
            self.assertEqual(clock.call_count, 4)
            self.assertEqual(connection.request.call_args_list, [mock.call("GET", "/v1/health")] * 2)
            self.assertEqual(connection.close.call_count, 2)
            self.assertEqual(sleep.call_args_list, [mock.call(1)] * 2)

    def test_fake_runner_requires_probe_success_and_always_cleans_its_project_volume(self):
        fixture = textwrap.dedent("""\
            sudo() {
              [[ "$*" == "chown -R --no-dereference 10001:10001 $VIBECI_RUN_DIR/data" ]] || return 99
              printf '%s\\n' "sudo $*" >> "$RUNNER_LOG"
            }
            docker() {
              printf '%s\\n' "$COMPOSE_PROJECT_NAME: docker $*" >> "$RUNNER_LOG"
              case "$*" in
                'compose -f maintenance/compose.yaml up -d sandboxd') return 0 ;;
                'compose -f maintenance/compose.yaml run --rm -T probe')
                  [[ -z "${VIBECI_GIT_TOKEN+x}" && -z "${VIBECI_LLM_API_KEY+x}" && -z "${VIBECI_FORK_READ_TOKEN+x}" ]] || return 99
                  return "$PROBE_EXIT" ;;
                *) return 99 ;;
              esac
            }
            """)
        for probe_exit, engine_exit in ((0, 0), (23, 0), (0, 19)):
            with self.subTest(probe_exit=probe_exit, engine_exit=engine_exit):
                root = self.prepare()
                log = root / "runner.log"
                project = f"maintainedmost-vibeci-fixture-{probe_exit}-{engine_exit}"
                env = {"PATH": "", "VIBECI_RUN_DIR": str(root), "RUNNER_LOG": str(log),
                       "RUNNER_TEMP": str(self.runner), "COMPOSE_PROJECT_NAME": project, "PROBE_EXIT": str(probe_exit),
                       "VIBECI_IMAGE": self.env["VIBECI_IMAGE"], "VIBECI_SANDBOX_IMAGE": self.env["VIBECI_SANDBOX_IMAGE"]}
                engine_requests = []
                outcome = [{"repo": "maintainedmost", "status": "dry-run"}]

                def docker(args, **options):
                    with log.open("a") as stream:
                        stream.write(project + ": " + " ".join(args) + "\n")
                    if "vibeci" in args:
                        credentials = {name: (root / "secrets" / name).read_text() for name in ("fork_read_token", "llm_api_key")}
                        engine_requests.append((args, options, credentials))
                        options["stdout"].write((json.dumps(outcome) + "\n").encode())
                        return subprocess.CompletedProcess(args, engine_exit)
                    return subprocess.CompletedProcess(args, 0, "", "")

                def run_step(identifier):
                    step_env = dict(env)
                    if identifier == "engine":
                        step_env.update(VIBECI_FORK_READ_TOKEN=self.env["GH_TOKEN"], VIBECI_LLM_API_KEY=self.env["VIBECI_LLM_API_KEY"])
                    if identifier in {"engine", "cleanup"}:
                        stdout, stderr = io.StringIO(), io.StringIO()
                        with mock.patch.dict(os.environ, step_env, clear=True), mock.patch.object(subprocess, "run", side_effect=docker), \
                                redirect_stdout(stdout), redirect_stderr(stderr):
                            if identifier == "engine":
                                with mock.patch.object(sys, "argv", ["vibeci-runtime.py", "engine"]):
                                    code = runtime.main()
                            else:
                                code = github.main(["cleanup", "--run-dir", str(root)])
                        return subprocess.CompletedProcess([identifier], code, stdout.getvalue(), stderr.getvalue())
                    return RUN_SCRIPT(["/bin/bash", "--noprofile", "--norc", "-e", "-o", "pipefail"],
                                      input=fixture + self.shell(identifier), env=step_env,
                                      capture_output=True, text=True, timeout=5, check=False)

                broker = run_step("broker")
                self.assertEqual(broker.returncode, probe_exit, broker.stderr)
                if broker.returncode == 0:
                    engine = run_step("engine")
                    self.assertEqual(engine.returncode, int(engine_exit != 0), engine.stderr)
                    self.assertEqual(engine.stdout, "")
                    self.assertEqual(engine.stderr, runtime.FAILURE + "\n" if engine_exit else "")
                    self.assertEqual(json.loads((root / "result.json").read_text()), outcome)
                    self.assertEqual(len(engine_requests), 1)
                    args, options, credentials = engine_requests[0]
                    self.assertEqual(args, ["docker", "compose", "-f", "maintenance/compose.yaml", "run", "--rm", "-T",
                                            "vibeci", "run", "-config", "/etc/vibeci/config.json", "-dry-run", "-json"])
                    self.assertEqual(Path(options["stdout"].name), root / "result.json")
                    self.assertEqual(options["stderr"], subprocess.DEVNULL)
                    self.assertEqual(options["timeout"], 7350)
                    self.assertFalse(options["check"])
                    self.assertFalse({"GH_TOKEN", "GITHUB_TOKEN", "VIBECI_GIT_TOKEN", "VIBECI_FORK_READ_TOKEN",
                                      "VIBECI_LLM_API_KEY", "VIBECI_MODELS", "VIBECI_PRIVATE_TERMS"} & options["env"].keys())
                    self.assertEqual(credentials, {"fork_read_token": self.env["GH_TOKEN"], "llm_api_key": self.env["VIBECI_LLM_API_KEY"]})
                    self.assertEqual(list((root / "secrets").iterdir()), [])
                else:
                    self.assertFalse((root / "result.json").exists())
                    self.assertFalse((root / "secrets").exists())
                    self.assertEqual(engine_requests, [])
                cleanup = run_step("cleanup")
                self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
                commands = ["up -d sandboxd", "run --rm -T probe"]
                if probe_exit == 0:
                    commands.append("run --rm -T vibeci run -config /etc/vibeci/config.json -dry-run -json")
                commands.append("down --volumes")
                query = f"{project}: docker ps -aq --no-trunc --filter label=vibeci.managed=true --filter label=vibeci.instance=path:{root}/data"
                self.assertEqual(log.read_text().splitlines(), [
                    f"sudo chown -R --no-dereference 10001:10001 {root}/data",
                    *[f"{project}: docker compose -f maintenance/compose.yaml {command}" for command in commands],
                    query, query,
                ])
                self.assertFalse((root / "socket").exists())

    def test_cleanup_runs_on_failure_and_publish_requires_successful_engine_and_cleanup(self):
        cleanup, ownership, publish = (self.step(name) for name in ("cleanup", "ownership", "publish"))
        self.assertIn("if: always() && steps.runtime.outputs.run_dir != ''", cleanup)
        self.assertEqual(self.shell("cleanup").strip(), 'python3 -B scripts/vibeci-github.py cleanup --run-dir "$VIBECI_RUN_DIR"')
        self.assertNotRegex(cleanup, r"--rmi|prune|docker volume rm")
        self.assertIn("if: always()", ownership)
        self.assertIn("steps.cleanup.outcome == 'success'", ownership)
        self.assertIn('"$(id -u):$(id -g)" "$VIBECI_RUN_DIR/data"', ownership)
        self.assertIn("if: success() && steps.runtime.outputs.changed == 'true'", publish)
        self.assertIn("steps.engine.outcome == 'success' && steps.cleanup.outcome == 'success'", publish)
        self.assertLess(self.workflow.index("id: cleanup"), self.workflow.index("id: publish"))
        self.assertLess(self.workflow.index("id: ownership"), self.workflow.index("id: publish"))
        self.assertEqual(self.shell("publish").strip().splitlines(), [
            "python3 -B scripts/vibeci-runtime.py restore",
            'python3 -B scripts/vibeci-github.py publish --data "$VIBECI_RUN_DIR/data" --result "$VIBECI_RUN_DIR/result.json"',
        ])
        self.assertIn("if: always()", self.step("evidence"))
        self.assertEqual(self.shell("evidence").strip(), "python3 -B scripts/vibeci-runtime.py evidence")
        for variable, step in (("PLAN_OUTCOME", "runtime"), ("RUN_OUTCOME", "engine"),
                               ("PUBLISH_OUTCOME", "publish"), ("CLEANUP_OUTCOME", "cleanup")):
            self.assertIn(variable + ": ${{ steps." + step + ".outcome }}", self.step("evidence"))
        self.assertIn("if: always() && steps.evidence.outputs.ready == 'true'", self.workflow)
        self.assertLess(self.workflow.index("id: evidence"), self.workflow.index("actions/upload-artifact@"))
        discard = self.workflow.rsplit("      - ", 1)[1]
        self.assertIn("if: always() && steps.runtime.outputs.run_dir != ''", discard)
        self.assertIn("run: python3 -B scripts/vibeci-runtime.py discard", discard)
        self.assertLess(self.workflow.index("actions/upload-artifact@"), self.workflow.index("scripts/vibeci-runtime.py discard"))

    def test_restore_uses_sealed_public_records_without_following_worker_links(self):
        root = self.prepare()
        files = (("plan.json", "plan.json"), ("summary.json", "public-summary.json"), ("summary.md", "public-summary.md"))
        for original, target in files:
            path = root / "data" / target
            path.unlink(missing_ok=True)
            path.symlink_to(root / "models.json")
        with mock.patch.dict(os.environ, self.env, clear=True):
            runtime.restore()
        for original, target in files:
            path = root / "data" / target
            self.assertFalse(path.is_symlink())
            self.assertEqual(path.read_bytes(), (root / "public-inputs" / original).read_bytes())
        self.assertEqual((root / "models.json").read_text(), self.env["VIBECI_MODELS"])

    def test_discard_removes_only_the_current_marked_private_runtime(self):
        other, root = self.prepare(), self.prepare()
        with mock.patch.dict(os.environ, self.env, clear=True):
            runtime.discard()
        self.assertFalse(root.exists())
        self.assertTrue(other.is_dir())
        self.assertTrue(self.checkout.is_dir())

    def test_artifacts_are_exactly_allowlisted_and_capture_failure_without_secrets(self):
        root = self.prepare()
        data = root / "data"
        secret = "private-provider.invalid " + self.env["VIBECI_LLM_API_KEY"]
        for name in ("summary.md", "summary.json", "plan.json"):
            (data / name).write_text(secret)
        raw = [{"repo": "maintainedmost", "status": "failed", "commit": CANDIDATE, "version": "8",
                "patches": {"total": 3}, "message": secret, "error": secret, "usage": {secret: 1},
                "model": secret, "headers": {"Authorization": secret}}]
        (root / "result.json").write_text(json.dumps(raw))
        (data / "config.json").write_text("must-not-upload-config")
        (data / "credentials.env").write_text("must-not-upload-credential")
        env = dict(self.env, VIBECI_RUN_DIR=str(root), PLAN_OUTCOME="success", RUN_OUTCOME="failure",
                    PUBLISH_OUTCOME="skipped", CLEANUP_OUTCOME="success")
        with mock.patch.dict(os.environ, env, clear=True):
            runtime.evidence()
        expected = {"summary.md", "summary.json", "plan.json", "result.json"}
        self.assertEqual({path.name for path in (root / "artifacts").iterdir()}, expected)
        self.assertIn("RUN_OUTCOME: failure", (root / "artifacts/summary.md").read_text())
        self.assertIn("PUBLISH_OUTCOME: skipped", Path(self.env["GITHUB_STEP_SUMMARY"]).read_text())
        for name in ("summary.json", "plan.json"):
            self.assertEqual((root / "artifacts" / name).read_bytes(), (root / "public-inputs" / name).read_bytes())
        self.assertIn("ready=true", Path(self.env["GITHUB_OUTPUT"]).read_text())
        uploads = [step for step in re.split(r"(?m)(?=^      - )", self.workflow) if "uses: actions/upload-artifact@" in step]
        self.assertEqual(len(uploads), 1)
        upload = uploads[0]
        self.assertIn("if: always() && steps.evidence.outputs.ready == 'true'", upload)
        paths = upload.split("          path: |\n", 1)[1].split("          if-no-files-found:", 1)[0]
        self.assertEqual({line.strip() for line in paths.splitlines()},
                         {"${{ steps.runtime.outputs.run_dir }}/artifacts/" + name for name in expected})
        self.assertIn("retention-days: 7", upload)
        self.assertIn("if-no-files-found: error", upload)
        self.assertFalse(any("must-not-upload" in path.read_text() for path in (root / "artifacts").iterdir()))
        self.assertFalse(any(secret in path.read_text() for path in (root / "artifacts").iterdir()))
        self.assertNotIn(secret, Path(self.env["GITHUB_STEP_SUMMARY"]).read_text())
        self.assertEqual(json.loads((root / "artifacts/result.json").read_text()), [
            {"repo": "maintainedmost", "status": "failed", "commit": CANDIDATE, "version": 8, "patches": {"total": 3}},
        ])
        self.assertEqual(json.loads((root / "result.json").read_text()), raw)

    def test_malformed_raw_result_artifact_contains_only_a_generic_error(self):
        root = self.prepare()
        secret = self.env["VIBECI_LLM_API_KEY"]
        (root / "result.json").write_text('{"message":"' + secret)
        env = dict(self.env, VIBECI_RUN_DIR=str(root), PLAN_OUTCOME="success", RUN_OUTCOME="failure",
                    PUBLISH_OUTCOME="skipped", CLEANUP_OUTCOME="success")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(stdout), redirect_stderr(stderr):
            runtime.evidence()
        self.assertEqual(json.loads((root / "artifacts/result.json").read_text()), [{"repo": "maintainedmost", "status": "error"}])
        self.assertFalse(any(secret in path.read_text() for path in (root / "artifacts").iterdir()))
        self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue() + Path(self.env["GITHUB_STEP_SUMMARY"]).read_text())

    def test_artifact_symlink_cannot_smuggle_config_or_models(self):
        root = self.prepare()
        (root / "public-inputs/summary.md").unlink()
        (root / "public-inputs/summary.md").symlink_to(root / "models.json")
        with mock.patch.dict(os.environ, self.env, clear=True), self.assertRaises(ValueError):
            runtime.evidence()
        self.assertFalse((root / "artifacts").exists())
        self.assertNotIn("ready=true", Path(self.env["GITHUB_OUTPUT"]).read_text())

    def test_cleanup_failure_retains_host_result_without_reading_container_data(self):
        root = self.prepare()
        (root / "data/summary.md").unlink()
        (root / "data/summary.md").symlink_to(root / "models.json")
        (root / "result.json").write_text('[{"repo":"maintainedmost","status":"failed"}]')
        env = dict(self.env, VIBECI_RUN_DIR=str(root), PLAN_OUTCOME="success", RUN_OUTCOME="failure",
                    PUBLISH_OUTCOME="skipped", CLEANUP_OUTCOME="failure")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(runtime, "read_private", wraps=runtime.read_private) as read:
            runtime.evidence()
        self.assertTrue(all(root / "data" not in call.args[0].parents for call in read.call_args_list))
        self.assertEqual({path.name for path in (root / "artifacts").iterdir()}, {"summary.md", "summary.json", "plan.json", "result.json"})
        self.assertIn("Trusted planner summary", (root / "artifacts/summary.md").read_text())
        self.assertIn("CLEANUP_OUTCOME: failure", Path(self.env["GITHUB_STEP_SUMMARY"]).read_text())
        self.assertIn("ready=true", Path(self.env["GITHUB_OUTPUT"]).read_text())


if __name__ == "__main__":
    unittest.main()
