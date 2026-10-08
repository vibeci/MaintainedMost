"""Offline planner tests: temporary Git fixtures, fake HTTPS, no source downloads."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import copy
from email.message import Message
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.error
import urllib.request
import urllib.response


SPEC = importlib.util.spec_from_file_location("vibeci_plan", Path(__file__).resolve().parents[1] / "vibeci-plan.py")
planner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(planner)

PINS = {
    "SERVER_TAG": "v11.11.1", "SERVER_COMMIT": "1" * 40,
    "SERVER_IMAGE": "docker.io/mattermost/mattermost-team-edition:11.11.1@sha256:" + "a" * 64,
    "CALLS_TAG": "v1.12.3", "CALLS_COMMIT": "2" * 40,
    "RECORDER_TAG": "v0.8.13",
    "RECORDER_SOURCE_IMAGE": "docker.io/mattermost/calls-recorder:v0.8.13@sha256:" + "b" * 64,
    "OFFLOADER_TAG": "v0.9.6", "OFFLOADER_COMMIT": "3" * 40,
    "TRANSCRIBER_TAG": "4" * 40, "TRANSCRIBER_BRANCH": "master",
    "GO_VERSION": "1.26.7", "NODE_VERSION": "24", "CALLS_VERSION": "1000.12.99",
}
LOCK = "# Preserve this trusted comment.\n\n" + "".join(f"{key}={value}\n" for key, value in PINS.items())
CONTAINERFILE = "# Fixed runtime instructions.\nARG SERVER_IMAGE=" + PINS["SERVER_IMAGE"] + "\nFROM ${SERVER_IMAGE}\nCOPY unchanged /unchanged\n"
PROVIDER_ALIAS = "mock-marker-provider-alias"
MODEL_ALIAS = "mock-marker-model-alias"
MODELS = {
    "providers": {PROVIDER_ALIAS: {"type": "openai-responses", "base_url": "https://private-provider.invalid/v1",
                                    "api_key": "file:/run/secrets/llm_api_key"}},
    "models": {MODEL_ALIAS: {"provider": PROVIDER_ALIAS, "model": "mock-marker-private-model-id"}},
    "roles": {"audit": MODEL_ALIAS, "resolve": [MODEL_ALIAS]},
}


def json_bytes(value):
    return json.dumps(value, separators=(",", ":")).encode()


def config_with_models(models, push=False):
    return planner.build_config("test-owner/maintainedmost", "main", "vibeci/update-fixture", models,
                                {"version": 1}, {}, "Preserve fork obligations.", b"# Verifier fixture.\n", push)


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def plugin(minimum="11.0.0", recorder="v0.8.13"):
    return {"min_server_version": minimum, "props": {"calls_recorder_version": recorder}}


def setUpModule():
    guard = mock.patch("socket.create_connection", side_effect=AssertionError("network is forbidden in planner tests"))
    guard.start()
    unittest.addModuleCleanup(guard.stop)


class FakeResolver:
    def __init__(self):
        self.refs = {
            planner.SOURCES["server"][0]: {PINS["SERVER_TAG"]: PINS["SERVER_COMMIT"]},
            planner.SOURCES["calls"][0]: {PINS["CALLS_TAG"]: PINS["CALLS_COMMIT"]},
        }
        self.plugins = {PINS["CALLS_COMMIT"]: plugin()}
        self.images = {(planner.SERVER_IMAGE, "11.11.1"): "sha256:" + "a" * 64,
                       (planner.RECORDER_IMAGE, "v0.8.13"): "sha256:" + "b" * 64}
        self.transcriber = PINS["TRANSCRIBER_TAG"]
        self.ancestor = True
        self.requests = []

    def tags(self, repository):
        self.requests.append(("tags", repository))
        return self.refs[repository]

    def head(self, repository, branch):
        self.requests.append(("head", repository, branch))
        return self.transcriber

    def calls_manifest(self, commit):
        self.requests.append(("plugin", commit))
        result = self.plugins[commit]
        if isinstance(result, Exception):
            raise result
        return result

    def image_digest(self, repository, tag):
        self.requests.append(("image", repository, tag))
        return self.images[repository, tag]

    def is_ancestor(self, old, new):
        self.requests.append(("ancestor", old, new))
        return self.ancestor

    def updates(self):
        self.refs[planner.SOURCES["server"][0]].update({"v11.11.2": "5" * 40, "v12.0.0": "6" * 40})
        self.refs[planner.SOURCES["calls"][0]].update({"v1.12.4": "7" * 40, "v1.12.5": "8" * 40})
        self.plugins.update({"7" * 40: plugin("11.11.2", "v0.8.14"), "8" * 40: plugin("12.0.0", "v0.8.15")})
        self.images.update({(planner.SERVER_IMAGE, "11.11.2"): "sha256:" + "c" * 64,
                            (planner.RECORDER_IMAGE, "v0.8.14"): "sha256:" + "d" * 64})
        self.transcriber = "9" * 40
        return self


class ParsingTests(unittest.TestCase):
    def test_strict_lock_and_complete_replacement_preserve_comments(self):
        self.assertEqual(planner.parse_env(LOCK), PINS)
        new = dict(PINS, CALLS_VERSION="1000.12.100")
        rewritten = planner.replace_env(LOCK, new)
        self.assertEqual(rewritten, LOCK.replace("CALLS_VERSION=1000.12.99", "CALLS_VERSION=1000.12.100"))
        self.assertEqual(planner.parse_env(rewritten), new)

    def test_lock_rejects_shell_syntax_unknown_missing_duplicate_and_partial_pins(self):
        invalid = [LOCK + "EXTRA=1\n", LOCK + "SERVER_TAG=v11.11.1\n", LOCK.replace("NODE_VERSION=24\n", ""),
                   LOCK.rstrip(), LOCK.replace("\n", "\r\n"), LOCK.replace("NODE_VERSION=24", " NODE_VERSION=24"),
                   LOCK.replace("NODE_VERSION=24", "export NODE_VERSION=24")]
        for value in ('"v11.11.1"', "v11.11.1 # extra", "$(touch BAD)", "`id`", "v11.11.1;id", "v11.11.1-rc1", "v011.11.1"):
            invalid.append(LOCK.replace("SERVER_TAG=v11.11.1", "SERVER_TAG=" + value))
        for key in ("SERVER_COMMIT", "CALLS_COMMIT", "OFFLOADER_COMMIT", "TRANSCRIBER_TAG"):
            invalid.append(LOCK.replace(key + "=" + PINS[key], key + "=abcdef0"))
        invalid += [LOCK.replace("sha256:" + "a" * 64, "sha256:abc"), LOCK.replace(":11.11.1@", ":11.11.2@"),
                    LOCK.replace("GO_VERSION=1.26.7", "GO_VERSION=1.26.7+bad"),
                    LOCK.replace("CALLS_VERSION=1000.12.99", f"CALLS_VERSION=1000.12.{1 << 63}"),
                    LOCK.replace("CALLS_VERSION=1000.12.99", "CALLS_VERSION=1000.12." + "9" * 5000)]
        invalid += [LOCK.replace("NODE_VERSION=24\nCALLS_VERSION=", "NODE_VERSION=24" + separator + "CALLS_VERSION=")
                    for separator in ("\v", "\f", "\x85", "\u2028")]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(planner.PlanError):
                planner.parse_env(text)

    def test_containerfile_requires_one_matching_arg_and_preserves_other_bytes(self):
        new = PINS["SERVER_IMAGE"].replace("11.11.1", "11.11.2")
        self.assertEqual(planner.replace_server_image(CONTAINERFILE, PINS["SERVER_IMAGE"], new),
                         CONTAINERFILE.replace(PINS["SERVER_IMAGE"], new))
        for text in (CONTAINERFILE + "ARG SERVER_IMAGE\n", CONTAINERFILE.replace("ARG SERVER_IMAGE=", "# ARG SERVER_IMAGE="),
                     CONTAINERFILE.replace("11.11.1", "11.11.2"), CONTAINERFILE.replace("ARG SERVER_IMAGE=", "ARG SERVER_IMAGE ="),
                     CONTAINERFILE.replace("ARG SERVER_IMAGE=", "ARG server_image="),
                     CONTAINERFILE + "ARG \\\n SERVER_IMAGE=hidden\n", CONTAINERFILE + "ARG`\n SERVER_IMAGE=hidden\n"):
            with self.subTest(text=text), self.assertRaises(planner.PlanError):
                planner.replace_server_image(text, PINS["SERVER_IMAGE"], new)

    def test_repository_and_branch_are_data_not_commands_or_urls(self):
        self.assertEqual(planner.repository_url("a-user/.github"), "https://github.com/a-user/.github.git")
        for name in ("main", "release/11.x", "topic+fix"):
            self.assertEqual(planner.branch_name(name), name)
        for name in ("https://github.com/owner/repo", "owner/repo.git?token=secret", "owner/repo/extra", "owner/..", "-owner/repo", "owner/repo;id"):
            with self.subTest(name=name), self.assertRaises(planner.PlanError):
                planner.repository_url(name)
        for name in ("-x", "main;id", "a..b", "foo/.hidden", "x.lock", "a/x.lock/b", "a//b", "@{-1}", "x/", "x.", "refs/*"):
            with self.subTest(name=name), self.assertRaises(planner.PlanError):
                planner.branch_name(name)

    def test_models_allow_only_fragment_fields_and_approved_file_reference(self):
        expected = {
            "providers": {"provider-1": MODELS["providers"][PROVIDER_ALIAS]},
            "models": {"model-1": {**MODELS["models"][MODEL_ALIAS], "provider": "provider-1"}},
            "roles": {"audit": "model-1", "resolve": ["model-1"]},
        }
        self.assertEqual(planner.parse_models(json_bytes(MODELS)), expected)
        bad = [None, [], {}]
        for key in MODELS:
            for value in (None, [], {}, "mock-marker", 1):
                bad.append({**MODELS, key: value})
        for key in ("repos", "sandbox", "verify", "data_dir", "identity", "alerts", "log_level", "push"):
            bad.append({**MODELS, key: "not allowed"})
        for secret in ("mock-marker", "env:VIBECI_LLM_API_KEY", "env:OTHER_KEY", "file:/mock-marker",
                       {"env": "VIBECI_LLM_API_KEY"}, {"file": "/run/secrets/llm_api_key"}, None, [], True, 1):
            value = copy.deepcopy(MODELS)
            value["providers"][PROVIDER_ALIAS]["api_key"] = secret
            bad.append(value)
        for key in ("headers", "access_key_id", "secret_access_key", "session_token", "unknown"):
            value = copy.deepcopy(MODELS)
            value["providers"][PROVIDER_ALIAS][key] = "mock-marker"
            bad.append(value)
        for url in ("https://mock-marker:mock-marker@private-provider.invalid", "https://@private-provider.invalid",
                    "https://private-provider.invalid/?token=mock-marker", "https://private-provider.invalid/#mock-marker",
                    "http://private-provider.invalid", "https://private-provider.invalid:mock-marker", "https://private-provider.invalid:70000",
                    "https://private-provider.invalid/\nmock-marker", "https://private-provider.invalid/\\mock-marker", 123, None, []):
            value = copy.deepcopy(MODELS)
            value["providers"][PROVIDER_ALIAS]["base_url"] = url
            bad.append(value)
        for roles in ({"resolve": [MODEL_ALIAS]}, {"audit": MODEL_ALIAS, "resolve": []},
                      {"audit": "mock-marker-unknown", "resolve": [MODEL_ALIAS]},
                      {"audit": MODEL_ALIAS, "resolve": ["mock-marker-unknown"]},
                      {"audit": MODEL_ALIAS, "resolve": MODEL_ALIAS}, {"audit": MODEL_ALIAS, "resolve": [None]},
                      {"audit": [], "resolve": [MODEL_ALIAS]}, {"audit": {}, "resolve": [MODEL_ALIAS]},
                      {"audit": MODEL_ALIAS, "resolve": [{}]}, {"audit": MODEL_ALIAS, "resolve": [[MODEL_ALIAS]]},
                      {"audit": MODEL_ALIAS, "resolve": [MODEL_ALIAS], "mock-marker": MODEL_ALIAS}):
            bad.append({**MODELS, "roles": roles})
        value = copy.deepcopy(MODELS)
        value["models"][MODEL_ALIAS]["run"] = "mock-marker"
        bad.append(value)
        for index, value in enumerate(bad):
            for direct in (False, True):
                with self.subTest(case=index, direct=direct), self.assertRaises(planner.PlanError) as caught:
                    config_with_models(value) if direct else planner.parse_models(json_bytes(value))
                self.assertNotIn("mock-marker", str(caught.exception))
                self.assertNotIn("private-provider.invalid", str(caught.exception))

    def test_model_types_and_provider_references_are_validated_at_both_entry_points(self):
        bad = []
        for section, alias, required in (("providers", PROVIDER_ALIAS, ("type", "api_key")),
                                         ("models", MODEL_ALIAS, ("provider", "model"))):
            for entry in (None, [], "mock-marker", 1):
                bad.append({**MODELS, section: {alias: entry}})
            for key in required:
                value = copy.deepcopy(MODELS)
                del value[section][alias][key]
                bad.append(value)
        for section, alias, fields in (
            ("providers", PROVIDER_ALIAS, {
                "type": (None, [], {}, True, 1, "", "mock-marker"),
                "auth": (None, [], {}, 1, "mock-marker", "x-api-key"),
                "region": (None, [], {}, 1, True),
                "timeout": (None, [], {}, True, -1, "mock-marker", "-1s", float("nan"), float("inf")),
                "max_retries": (None, [], {}, True, -1, 1.5, planner.MAX_INTEGER + 1),
                "stream": (None, [], {}, 1, "mock-marker"),
                "max_tokens_field": (None, [], {}, 1, "mock-marker"),
            }),
            ("models", MODEL_ALIAS, {
                "provider": (None, [], {}, True, 1, "", "mock-marker-unknown"),
                "model": (None, [], {}, True, 1, "", " \t"),
                "max_tokens": (None, [], {}, True, -1, 1.5, planner.MAX_INTEGER + 1),
                "context_window": (None, [], {}, True, -1, 1.5, planner.MAX_INTEGER + 1),
                "thinking_budget": (None, [], {}, True, -1, 1.5, planner.MAX_INTEGER + 1),
                "thinking": (None, [], {}, 1, "mock-marker"),
                "effort": (None, [], {}, 1, "mock-marker"),
                "temperature": (None, [], {}, True, "mock-marker", float("nan"), float("inf"), float("-inf"), 10 ** 400),
                "prompt_cache": (None, [], {}, 1, "mock-marker"),
            }),
        ):
            for key, invalid in fields.items():
                for entry in invalid:
                    value = copy.deepcopy(MODELS)
                    value[section][alias][key] = entry
                    bad.append(value)
        value = copy.deepcopy(MODELS)
        value["models"][MODEL_ALIAS].update(thinking="enabled", thinking_budget=32000, max_tokens=32000)
        bad.append(value)
        for index, value in enumerate(bad):
            for direct in (False, True):
                with self.subTest(case=index, direct=direct), self.assertRaises(planner.PlanError) as caught:
                    config_with_models(value) if direct else planner.parse_models(json_bytes(value))
                self.assertNotIn("mock-marker", str(caught.exception))
        for section in ("providers", "models"):
            entry = next(iter(MODELS[section].values()))
            for alias in (None, 1, "", " \t"):
                with self.subTest(section=section, alias=alias), self.assertRaises(planner.PlanError):
                    config_with_models({**MODELS, section: {alias: entry}})

    def test_model_options_and_private_ids_are_preserved(self):
        models = copy.deepcopy(MODELS)
        models["providers"][PROVIDER_ALIAS].update(auth="bearer", region="mock-marker", timeout="1m30s",
                                                  max_retries=0, stream=False, max_tokens_field="max_tokens")
        models["models"][MODEL_ALIAS].update(max_tokens=32000, context_window=200000, thinking="enabled",
                                            thinking_budget=8000, effort="high", temperature=0.25, prompt_cache=True)
        for timeout in ("1m30s", "0", ".5s", 0, 0.5):
            models["providers"][PROVIDER_ALIAS]["timeout"] = timeout
            config = config_with_models(models)
            self.assertEqual(config["providers"]["provider-1"], models["providers"][PROVIDER_ALIAS])
            self.assertEqual(config["models"]["model-1"], {**models["models"][MODEL_ALIAS], "provider": "provider-1"})
            self.assertEqual(planner.parse_models(json_bytes(models)), {key: config[key] for key in MODELS})

    def test_all_aliases_and_role_references_are_neutral_ordered_and_idempotent(self):
        provider_aliases = [f"{PROVIDER_ALIAS}-{index}" for index in range(12, 0, -1)]
        model_aliases = [f"{MODEL_ALIAS}-{index}" for index in range(12, 0, -1)]
        models = {
            "providers": {alias: {**MODELS["providers"][PROVIDER_ALIAS], "base_url": f"https://private-provider.invalid/mock-marker-{index}"}
                          for index, alias in enumerate(provider_aliases)},
            "models": {alias: {"provider": provider_aliases[(index + 2) % 3], "model": f"mock-marker-private-id-{index}"}
                       for index, alias in enumerate(model_aliases)},
            "roles": {"resolve": [model_aliases[10], model_aliases[0], model_aliases[10]], "audit": model_aliases[8],
                      "triage": model_aliases[2], "investigate": model_aliases[1]},
        }
        original = copy.deepcopy(models)
        config = config_with_models(models)
        serialized = json.dumps(config)
        self.assertEqual(list(config["providers"]), [f"provider-{index}" for index in range(1, 13)])
        self.assertEqual(list(config["models"]), [f"model-{index}" for index in range(1, 13)])
        for index, alias in enumerate(provider_aliases, 1):
            self.assertEqual(config["providers"][f"provider-{index}"], models["providers"][alias])
            self.assertNotIn(alias, serialized)
        for index, alias in enumerate(model_aliases):
            self.assertEqual(config["models"][f"model-{index + 1}"],
                             {**models["models"][alias], "provider": f"provider-{(index + 2) % 3 + 1}"})
            self.assertNotIn(alias, serialized)
        self.assertEqual(config["roles"], {"resolve": ["model-11", "model-1", "model-11"], "audit": "model-9",
                                           "triage": "model-3", "investigate": "model-2"})
        self.assertEqual(list(config["roles"]), list(models["roles"]))
        normalized = planner.parse_models(json_bytes(models))
        self.assertEqual(normalized, {key: config[key] for key in MODELS})
        self.assertEqual(json_bytes(normalized), json_bytes(planner.parse_models(json_bytes(normalized))))
        self.assertEqual(json_bytes(config), json_bytes(config_with_models(normalized)))
        config["providers"]["provider-1"]["base_url"] = "https://private-provider.invalid/mock-marker-changed"
        config["models"]["model-1"]["model"] = "mock-marker-changed"
        config["roles"]["resolve"].append("model-2")
        self.assertEqual(models, original)

    def test_build_config_push_never_enables_engine_writes_environment_secrets_or_alerts(self):
        credentials = {"GITHUB_TOKEN": "mock-marker-read-token", "VIBECI_GIT_TOKEN": "mock-marker-write-token",
                       "VIBECI_LLM_API_KEY": "mock-marker-api-key"}
        configs = []
        with mock.patch.dict(os.environ, credentials), mock.patch("builtins.open", side_effect=AssertionError("must not resolve credentials")):
            for push in (False, True):
                config = config_with_models(MODELS, push=push)
                configs.append(config)
                self.assertEqual(config["alerts"], [])
                self.assertEqual(config["log_level"], "error")
                self.assertEqual(config["repo_timeout"], "2h")
                for provider in config["providers"].values():
                    self.assertEqual(provider["api_key"], "file:/run/secrets/llm_api_key")
                for repo in config["repos"]:
                    self.assertEqual(repo["fork"]["auth"], {"token": "file:/run/secrets/fork_read_token"})
                    self.assertEqual(repo["push"], {"mode": "branch", "branch": "vibeci/update-fixture", "dry_run": True})
                for marker in (*credentials, *credentials.values(), "env:", PROVIDER_ALIAS, MODEL_ALIAS):
                    self.assertNotIn(marker, json.dumps(config))
        self.assertEqual(configs[0], configs[1])

    def test_public_models_example_uses_only_neutral_placeholders_and_file_auth(self):
        path = Path(__file__).resolve().parents[2] / "maintenance/models.example.json"
        example = json.loads(path.read_text())
        self.assertEqual(example, planner.parse_models(json_bytes(example)))
        self.assertEqual(list(example["providers"]), ["provider-1"])
        self.assertEqual(list(example["models"]), ["model-1"])
        self.assertEqual(example["providers"]["provider-1"]["base_url"], "https://private-provider.invalid/v1")
        self.assertEqual(example["providers"]["provider-1"]["api_key"], "file:/run/secrets/llm_api_key")
        self.assertEqual(example["models"]["model-1"]["model"], "mock-marker")

    def test_duplicate_json_keys_and_nonfinite_json_fail(self):
        for text in (b'{"providers":{},"providers":{}}', b'{"nested":{"key":1,"key":2}}', b'{"n":NaN}', b'{"n":Infinity}', b'{"n":1e9999}', b'[]'):
            with self.subTest(text=text), self.assertRaises(planner.PlanError):
                planner.json_object(text, "input")

    def test_stable_tag_filter_peeling_and_limits(self):
        tags = (f"{'1' * 40}\trefs/tags/v11.11.1\n{'2' * 40}\trefs/tags/v11.11.1^{{}}\n"
                f"{'3' * 40}\trefs/tags/v12.0.0-rc1\n{'4' * 40}\trefs/tags/v01.0.0\n"
                f"{'5' * 40}\trefs/tags/other\n{'6' * 40}\trefs/tags/v11.11.2+build\n").encode()
        self.assertEqual(planner.stable_tags(tags), {"v11.11.1": "2" * 40})
        for data in (b"bad\n", f"{'1' * 40}\trefs/tags/v1.0.0^{{}}\n".encode(), tags + tags, b"\xff"):
            with self.subTest(data=data), self.assertRaises(planner.PlanError):
                planner.stable_tags(data)
        with mock.patch.object(planner, "MAX_VERSIONS", 1), self.assertRaisesRegex(planner.PlanError, "limit"):
            planner.stable_tags(tags)


class SelectionTests(unittest.TestCase):
    def test_latest_compatible_stable_releases_with_server_major_cap_and_recorder_coupling(self):
        resolver = FakeResolver().updates()
        new = planner.select_pins(PINS, resolver)
        self.assertEqual((new["SERVER_TAG"], new["SERVER_COMMIT"]), ("v11.11.2", "5" * 40))
        self.assertEqual((new["CALLS_TAG"], new["CALLS_COMMIT"]), ("v1.12.4", "7" * 40))
        self.assertEqual(new["RECORDER_TAG"], "v0.8.14")
        self.assertIn(":v0.8.14@sha256:" + "d" * 64, new["RECORDER_SOURCE_IMAGE"])
        self.assertEqual(new["TRANSCRIBER_TAG"], "9" * 40)
        self.assertEqual(new["CALLS_VERSION"], "1000.12.100")
        for key in ("OFFLOADER_TAG", "OFFLOADER_COMMIT", "GO_VERSION", "NODE_VERSION", "TRANSCRIBER_BRANCH"):
            self.assertEqual(new[key], PINS[key])
        self.assertIn(("plugin", "8" * 40), resolver.requests)
        self.assertIn(("plugin", "7" * 40), resolver.requests)
        self.assertIn(("ancestor", "4" * 40, "9" * 40), resolver.requests)

    def test_noop_still_verifies_current_tags_images_and_manifest(self):
        resolver = FakeResolver()
        self.assertEqual(planner.select_pins(PINS, resolver), PINS)
        self.assertEqual([request for request in resolver.requests if request[0] == "image"],
                         [("image", planner.SERVER_IMAGE, "11.11.1"), ("image", planner.RECORDER_IMAGE, "v0.8.13")])
        self.assertFalse(any(request[0] == "ancestor" for request in resolver.requests))

    def test_patch_transaction_bumps_existing_bundle_without_deriving_upstream_version(self):
        old = dict(PINS, CALLS_VERSION="1000.99.900")
        for component in ("transcriber", "server"):
            resolver = FakeResolver()
            if component == "transcriber":
                resolver.transcriber = "9" * 40
            else:
                resolver.refs[planner.SOURCES["server"][0]]["v11.12.0"] = "9" * 40
                resolver.images[planner.SERVER_IMAGE, "11.12.0"] = "sha256:" + "c" * 64
            with self.subTest(component=component):
                new = planner.select_pins(old, resolver)
                self.assertEqual(new["CALLS_TAG"], old["CALLS_TAG"])
                self.assertEqual(new["CALLS_VERSION"], "1000.99.901")
        with self.assertRaisesRegex(planner.PlanError, "exhausted"):
            planner.select_pins(dict(PINS, CALLS_VERSION=f"1000.1.{planner.MAX_INTEGER}"), FakeResolver().updates())

    def test_current_source_tag_rewrites_or_disappearance_fail_even_with_new_releases(self):
        for component in ("server", "calls"):
            for missing in (False, True):
                resolver = FakeResolver().updates()
                refs = resolver.refs[planner.SOURCES[component][0]]
                if missing:
                    del refs[PINS[component.upper() + "_TAG"]]
                else:
                    refs[PINS[component.upper() + "_TAG"]] = "f" * 40
                with self.subTest(component=component, missing=missing), self.assertRaisesRegex(planner.PlanError, "rewritten"):
                    planner.select_pins(PINS, resolver)

    def test_incompatible_new_calls_never_causes_a_downgrade(self):
        resolver = FakeResolver()
        resolver.refs[planner.SOURCES["calls"][0]].update({"v1.12.2": "e" * 40, "v1.12.5": "f" * 40})
        resolver.plugins["f" * 40] = plugin("12.0.0")
        self.assertEqual(planner.select_pins(PINS, resolver), PINS)
        self.assertNotIn(("plugin", "e" * 40), resolver.requests)

    def test_unknown_or_failed_compatibility_is_not_silently_skipped(self):
        for metadata in (plugin(None), plugin("unknown"), plugin("11.0"), plugin([11, 0, 0]),
                         planner.PlanError("offline HTTP failure")):
            resolver = FakeResolver().updates()
            resolver.plugins["8" * 40] = metadata
            with self.subTest(metadata=metadata), self.assertRaises(planner.PlanError):
                planner.select_pins(PINS, resolver)
            self.assertNotIn(("plugin", "7" * 40), resolver.requests)
        with mock.patch.object(planner, "MAX_CALLS_CHECKS", 1), self.assertRaisesRegex(planner.PlanError, "limit"):
            planner.select_pins(PINS, FakeResolver().updates())

    def test_recorder_manifest_must_be_valid_match_baseline_and_never_downgrade(self):
        for recorder in (None, 123, "0.8.13", "v0.8.13-rc1", "v0.5.9", "v0.8.14"):
            resolver = FakeResolver()
            resolver.plugins[PINS["CALLS_COMMIT"]] = plugin(recorder=recorder)
            with self.subTest(recorder=recorder), self.assertRaises(planner.PlanError):
                planner.select_pins(PINS, resolver)
        resolver = FakeResolver().updates()
        resolver.plugins["7" * 40] = plugin("11.11.2", "v0.8.12")
        with self.assertRaisesRegex(planner.PlanError, "downgrade the recorder"):
            planner.select_pins(PINS, resolver)

    def test_mutable_current_image_tags_fail_instead_of_refreshing_the_digest(self):
        for repository, tag in ((planner.SERVER_IMAGE, "11.11.1"), (planner.RECORDER_IMAGE, "v0.8.13")):
            for update in (False, True):
                resolver = FakeResolver().updates() if update else FakeResolver()
                resolver.images[repository, tag] = "sha256:" + "f" * 64
                with self.subTest(repository=repository, update=update), self.assertRaisesRegex(planner.PlanError, "retagged"):
                    planner.select_pins(PINS, resolver)

    def test_transcriber_requires_a_full_head_and_proven_forward_ancestry(self):
        for head, ancestor in (("abcdef0", True), ("9" * 40, False)):
            resolver = FakeResolver()
            resolver.transcriber, resolver.ancestor = head, ancestor
            with self.subTest(head=head), self.assertRaises(planner.PlanError):
                planner.select_pins(PINS, resolver)


class RegistryTests(unittest.TestCase):
    def fixture(self, index=True, oci=True, platform="amd64", advertised="amd64"):
        media = "application/vnd.oci.image.manifest.v1+json" if oci else "application/vnd.docker.distribution.manifest.v2+json"
        config_media = "application/vnd.oci.image.config.v1+json" if oci else "application/vnd.docker.container.image.v1+json"
        index_media = "application/vnd.oci.image.index.v1+json" if oci else "application/vnd.docker.distribution.manifest.list.v2+json"
        config = json_bytes({"os": "linux", "architecture": platform})
        image = json_bytes({"schemaVersion": 2, "mediaType": media, "layers": [],
                            "config": {"mediaType": config_media, "digest": digest(config), "size": len(config)}})
        manifest = json_bytes({"schemaVersion": 2, "mediaType": index_media,
                               "manifests": [{"mediaType": media, "digest": digest(image), "size": len(image),
                                              "platform": {"os": "linux", "architecture": advertised}}]}) if index else image
        self.base = "https://registry-1.docker.io/v2/" + planner.SERVER_IMAGE + "/"
        self.tag_url = self.base + "manifests/11.11.2"
        self.child_url = self.base + "manifests/" + digest(image)
        self.config_url = self.base + "blobs/" + digest(config)
        self.routes = {
            self.tag_url: (manifest, {"content-type": index_media if index else media, "docker-content-digest": digest(manifest)}),
            self.child_url: (image, {"content-type": media, "docker-content-digest": digest(image)}),
            self.config_url: (config, {}),
        }
        self.requests = []

        def get(url, headers=None):
            self.requests.append((url, headers))
            if url.startswith("https://auth.docker.io/token?"):
                self.assertIsNone(headers)
                query = planner.urllib.parse.parse_qs(planner.urllib.parse.urlsplit(url).query)
                self.assertEqual(query, {"service": ["registry.docker.io"], "scope": [f"repository:{planner.SERVER_IMAGE}:pull"]})
                return b'{"token":"anonymous.pull.token"}', {}
            return self.routes[url]

        self.http_patch = mock.patch.object(planner, "https_get", side_effect=get)
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        return digest(manifest)

    def test_oci_and_docker_indexes_verify_raw_digest_child_and_config_without_layers(self):
        for oci in (True, False):
            expected = self.fixture(oci=oci)
            with self.subTest(oci=oci):
                self.assertEqual(planner.image_digest(planner.SERVER_IMAGE, "11.11.2"), expected)
                self.assertEqual(len(self.requests), 4)
                for url, headers in self.requests[1:]:
                    self.assertEqual(headers["Authorization"], "Bearer anonymous.pull.token")
                    if "/manifests/" in url:
                        self.assertEqual(set(headers["Accept"].split(", ")), planner.IMAGE_TYPES | planner.INDEX_TYPES)
                self.assertEqual(self.requests[-1][0], self.config_url)
            self.http_patch.stop()

    def test_single_image_requires_actual_linux_amd64_config(self):
        expected = self.fixture(index=False)
        self.assertEqual(planner.image_digest(planner.SERVER_IMAGE, "11.11.2"), expected)
        self.assertEqual(len(self.requests), 3)

    def test_absent_or_false_platform_fails_closed(self):
        for index, platform, advertised in ((True, "amd64", "arm64"), (True, "arm64", "amd64"), (False, "arm64", "amd64")):
            self.fixture(index=index, platform=platform, advertised=advertised)
            with self.subTest(index=index, platform=platform, advertised=advertised), self.assertRaisesRegex(planner.PlanError, "linux/amd64"):
                planner.image_digest(planner.SERVER_IMAGE, "11.11.2")
            self.http_patch.stop()

    def test_missing_mismatched_or_tampered_manifest_digest_fails(self):
        for value in (None, "sha256:" + "f" * 64, "sha256:abc"):
            self.fixture()
            body, headers = self.routes[self.tag_url]
            self.routes[self.tag_url] = body, {**headers, "docker-content-digest": value}
            with self.subTest(value=value), self.assertRaisesRegex(planner.PlanError, "digest mismatch"):
                planner.image_digest(planner.SERVER_IMAGE, "11.11.2")
            self.http_patch.stop()
        self.fixture()
        body, headers = self.routes[self.child_url]
        tampered = body + b" "
        self.routes[self.child_url] = tampered, {**headers, "docker-content-digest": digest(tampered)}
        with self.assertRaisesRegex(planner.PlanError, "digest mismatch"):
            planner.image_digest(planner.SERVER_IMAGE, "11.11.2")

    def test_image_config_digest_is_verified(self):
        self.fixture(index=False)
        self.routes[self.config_url] = b'{"os":"linux","architecture":"amd64","extra":true}', {}
        with self.assertRaisesRegex(planner.PlanError, "config digest"):
            planner.image_digest(planner.SERVER_IMAGE, "11.11.2")

    def test_cloudfront_config_redirect_keeps_digest_size_and_auth_guards(self):
        build_opener = urllib.request.build_opener
        for fault in (None, "digest", "size"):
            expected = self.fixture(index=False)
            self.http_patch.stop()
            config = self.routes[self.config_url][0]
            if fault == "digest":
                config = config.replace(b"amd64", b"arm64")  # Same size, wrong digest.
            elif fault == "size":
                document = json.loads(self.routes[self.tag_url][0])
                document["config"]["size"] += 1
                raw = json_bytes(document)
                self.routes[self.tag_url] = raw, {"content-type": document["mediaType"], "docker-content-digest": digest(raw)}
            cdn_url = "https://production.cloudfront.docker.com/config?signature=fixture"
            requests = []
            routes = self.routes
            config_url = self.config_url

            class FakeHTTPS(urllib.request.HTTPSHandler):
                def https_open(self, request):
                    requests.append(request)
                    status = 200
                    if request.host == "auth.docker.io":
                        raw, values = b'{"token":"anonymous.pull.token"}', {}
                    elif request.full_url == config_url:
                        raw, values, status = b"", {"Location": cdn_url}, 307
                    elif request.full_url == cdn_url:
                        raw, values = config, {}
                    else:
                        raw, values = routes[request.full_url]
                    headers = Message()
                    for key, value in values.items():
                        headers[key] = value
                    response = urllib.response.addinfourl(io.BytesIO(raw), headers, request.full_url, status)
                    response.msg = "fixture"
                    return response

            with self.subTest(fault=fault), mock.patch.object(planner.urllib.request, "build_opener",
                                                            side_effect=lambda *handlers: build_opener(*handlers, FakeHTTPS())):
                if fault:
                    with self.assertRaisesRegex(planner.PlanError, "config digest or size mismatch"):
                        planner.image_digest(planner.SERVER_IMAGE, "11.11.2")
                else:
                    self.assertEqual(planner.image_digest(planner.SERVER_IMAGE, "11.11.2"), expected)
            self.assertEqual([request.full_url for request in requests[2:]], [config_url, cdn_url])
            self.assertIsNotNone(requests[2].get_header("Authorization"))
            self.assertIsNone(requests[3].get_header("Authorization"))

    def test_legacy_or_malformed_manifest_and_missing_config_fail(self):
        for document in ({"schemaVersion": 1}, {"schemaVersion": 2, "mediaType": []},
                         {"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json"}):
            self.fixture(index=False)
            raw = json_bytes(document)
            self.routes[self.tag_url] = raw, {"content-type": "application/vnd.oci.image.manifest.v1+json", "docker-content-digest": digest(raw)}
            with self.subTest(document=document), self.assertRaises(planner.PlanError):
                planner.image_digest(planner.SERVER_IMAGE, "11.11.2")
            self.http_patch.stop()


class TransportTests(unittest.TestCase):
    def test_public_https_is_bounded_anonymous_and_ignores_proxy_credentials(self):
        response = io.BytesIO(b"public response")
        response.status = 200
        response.headers = {"Content-Type": "application/json"}
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(planner.urllib.request, "build_opener", return_value=opener) as build:
            with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://secret:secret@proxy", "VIBECI_GIT_TOKEN": "git-secret", "VIBECI_LLM_API_KEY": "model-secret"}):
                body, headers = planner.https_get("https://raw.githubusercontent.com/public/repo/commit/plugin.json")
        self.assertEqual(body, b"public response")
        self.assertEqual(headers, {"content-type": "application/json"})
        self.assertEqual(build.call_args.args[0].proxies, {})
        request = opener.open.call_args.args[0]
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], planner.HTTP_TIMEOUT)

    def test_public_https_rejects_untrusted_urls_large_responses_and_sanitizes_errors(self):
        for url in ("http://api.github.com/x", "https://token@api.github.com/x", "https://evil.invalid/x", "https://api.github.com:444/x",
                    "https://production.cloudfront.docker.com/config"):
            with self.subTest(url=url), self.assertRaises(planner.PlanError):
                planner.https_get(url)
        response = io.BytesIO(b"12345")
        response.status, response.headers = 200, {}
        with mock.patch.object(planner.urllib.request, "build_opener") as build, mock.patch.object(planner, "MAX_HTTP_BYTES", 4):
            build.return_value.open.return_value = response
            with self.assertRaisesRegex(planner.PlanError, "limit"):
                planner.https_get("https://api.github.com/x")
        with mock.patch.object(planner.urllib.request, "build_opener") as build:
            build.return_value.open.side_effect = urllib.error.URLError("literal-secret")
            with self.assertRaises(planner.PlanError) as caught:
                planner.https_get("https://api.github.com/x")
            self.assertNotIn("literal-secret", str(caught.exception))

    def test_https_deadline_includes_headers_and_empty_or_later_reads(self):
        for name, clock, chunk, reads in (("headers", [0, 21], b"", 0), ("EOF", [0, 1, 21], b"", 1),
                                          ("later read", [0, 1, 2, 21], b"content", 1)):
            response = mock.MagicMock(status=200, headers={})
            response.__enter__.return_value = response
            response.read1.return_value = chunk
            with self.subTest(name=name), mock.patch.object(planner.urllib.request, "build_opener") as build:
                build.return_value.open.return_value = response
                with mock.patch.object(planner.time, "monotonic", side_effect=clock):
                    with self.assertRaisesRegex(planner.PlanError, "time limit"):
                        planner.https_get("https://api.github.com/x")
                self.assertEqual(response.read1.call_count, reads)

    def test_docker_cdn_redirects_strip_both_forms_of_authorization(self):
        redirect = planner.PublicRedirect()
        request = urllib.request.Request("https://registry-1.docker.io/v2/mattermost/image/blobs/sha256:abc",
                                         headers={"Authorization": "Bearer anonymous-pull-token"})
        request.add_unredirected_header("Authorization", "Bearer unredirected-pull-token")
        for host in ("production.cloudflare.docker.com", "production.cloudfront.docker.com", "docker-images.example.r2.cloudflarestorage.com"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(host=host, status=status):
                    redirected = redirect.redirect_request(request, None, status, "redirect", {}, f"https://{host}/config?signature=fixture")
                    self.assertIsNone(redirected.get_header("Authorization"))
                    self.assertFalse(any(key.lower() == "authorization" for key, _ in redirected.header_items()))
                    followup = redirect.redirect_request(redirected, None, 307, "redirect", {}, f"https://{host}/final-config")
                    self.assertIsNone(followup.get_header("Authorization"))

    def test_cdn_redirect_allowlist_is_exact_and_only_for_registry_blobs(self):
        redirect = planner.PublicRedirect()
        request = urllib.request.Request("https://registry-1.docker.io/v2/mattermost/image/blobs/sha256:abc")
        for url in ("https://evil.invalid/config", "https://example.cloudfront.net/config",
                    "https://production.cloudfront.docker.com.evil.invalid/config", "https://other.production.cloudfront.docker.com/config",
                    "https://production.cloudfront.docker.com:444/config", "https://user:secret@production.cloudfront.docker.com/config"):
            with self.subTest(url=url), self.assertRaises(planner.PlanError):
                redirect.redirect_request(request, None, 307, "redirect", {}, url)
        for url in ("https://registry-1.docker.io/v2/mattermost/image/manifests/tag",
                    "https://raw.githubusercontent.com/repo/blobs/config", "https://registry-1.docker.io.evil.invalid/v2/repo/blobs/config"):
            with self.subTest(origin=url), self.assertRaises(planner.PlanError):
                redirect.redirect_request(urllib.request.Request(url), None, 307, "redirect", {},
                                          "https://production.cloudfront.docker.com/config")

    def test_registry_and_cdn_redirects_cannot_downgrade_to_http(self):
        redirect = planner.PublicRedirect()
        request = urllib.request.Request("https://registry-1.docker.io/v2/mattermost/image/blobs/sha256:abc")
        for host in ("registry-1.docker.io", "production.cloudflare.docker.com", "production.cloudfront.docker.com"):
            with self.subTest(host=host), self.assertRaises(planner.PlanError):
                redirect.redirect_request(request, None, 307, "redirect", {}, f"http://{host}/config")
        cdn_request = urllib.request.Request("https://production.cloudfront.docker.com/config")
        for url in ("http://production.cloudfront.docker.com/final-config", "https://evil.invalid/config"):
            with self.subTest(followup=url), self.assertRaises(planner.PlanError):
                redirect.redirect_request(cdn_request, None, 307, "redirect", {}, url)

    def test_raw_plugin_and_ancestry_use_exact_shas_no_auth_and_fail_closed(self):
        resolver = planner.PublicResolver(Path("/unused"))
        comparison = {"status": "ahead", "ahead_by": 2, "behind_by": 0,
                      "base_commit": {"sha": "4" * 40}, "merge_base_commit": {"sha": "4" * 40}}
        with mock.patch.object(planner, "https_get", return_value=(json_bytes(plugin()), {})) as get:
            self.assertEqual(resolver.calls_manifest("2" * 40), plugin())
            self.assertEqual(get.call_args.args, ("https://raw.githubusercontent.com/mattermost/mattermost-plugin-calls/" + "2" * 40 + "/plugin.json",))
        for change, expected in (({}, True), ({"status": "behind"}, False), ({"status": "diverged"}, False),
                                 ({"ahead_by": 0}, False), ({"merge_base_commit": {"sha": "0" * 40}}, False), ({"base_commit": None}, False)):
            with mock.patch.object(planner, "https_get", return_value=(json_bytes({**comparison, **change}), {})) as get:
                self.assertEqual(resolver.is_ancestor("4" * 40, "9" * 40), expected)
                self.assertIn("4" * 40 + "..." + "9" * 40 + "?per_page=1&page=2", get.call_args.args[0])
                self.assertEqual(get.call_args.args[1], {"Accept": "application/vnd.github+json"})

    def test_public_git_hardening_and_exact_ref_requests(self):
        result = subprocess.CompletedProcess([], 0, f"{'4' * 40}\trefs/heads/master\n".encode(), b"")
        with mock.patch.object(planner.subprocess, "run", return_value=result) as run:
            with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "malicious",
                                             "GIT_TRACE": "/secret", "GITHUB_TOKEN": "secret", "VIBECI_GIT_TOKEN": "secret", "VIBECI_LLM_API_KEY": "secret"}):
                self.assertEqual(planner.PublicResolver(Path("/output")).head(planner.SOURCES["transcriber"][0], "master"), "4" * 40)
        command, = run.call_args.args
        self.assertEqual(command[-5:], ["ls-remote", "--heads", "--", "https://github.com/mattermost/calls-transcriber.git", "refs/heads/master"])
        for option in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "credential.helper=", "protocol.allow=never", "http.followRedirects=false"):
            self.assertIn(option, command)
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CEILING_DIRECTORIES"], "/")
        self.assertEqual(run.call_args.kwargs["timeout"], planner.GIT_TIMEOUT)
        for key in ("GITHUB_TOKEN", "VIBECI_GIT_TOKEN", "VIBECI_LLM_API_KEY", "GIT_CONFIG_COUNT", "GIT_TRACE"):
            self.assertNotIn(key, env)
        with mock.patch.object(planner, "git", return_value=f"{'1' * 40}\trefs/tags/v11.11.1\n".encode()) as git:
            planner.PublicResolver(Path("/output")).tags(planner.SOURCES["server"][0])
            self.assertEqual(git.call_args.args[-2:], ("refs/tags/v*", "refs/tags/v*^{}"))


class GitPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="maintainedmost-planner-")
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name).resolve()
        self.root = self.temp / "checkout"
        self.root.mkdir()
        planner.git(self.root, "init", "--initial-branch=main", "--template=", ".")
        self.files = {
            "upstream.env": LOCK, "Containerfile": CONTAINERFILE, ".gitignore": "build/\n__pycache__/\n",
            "maintenance/upstream-version": "0\n", "maintenance/verify.py": "# Trusted offline verifier fixture.\n",
            "maintenance/intent.md": "Preserve generic OIDC, group video, automatic recording, and the fork's behavioral obligations.\n",
            "scripts/trusted.py": "# Committed planner input.\n",
            "patches/server/0002-other.patch": "server second patch\n", "patches/server/0001-feature.patch": "server first patch\n",
            "patches/calls/0001-feature.patch": "calls patch\n", "patches/transcriber/0001-feature.patch": "transcriber patch\n",
        }
        self.base = self.commit(self.files)
        self.models_file = self.temp / "models.json"
        planner.write_private(self.models_file, json_bytes(MODELS).decode())
        self.output = self.temp / "plan"

    def commit(self, files):
        for name, text in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        planner.git(self.root, "add", "--", ".")
        tree = planner.git(self.root, "write-tree").decode().strip()
        commit = planner.git(self.root, "commit-tree", tree, data=b"Temporary fixture baseline\n").decode().strip()
        planner.git(self.root, "update-ref", "refs/heads/main", commit)
        return commit

    def plan(self, resolver=None, push=False):
        return planner.plan(self.root, "test-owner/maintainedmost", "main", self.output, self.models_file,
                            push=push, resolver=resolver or FakeResolver().updates())

    def test_deterministic_two_commit_manifest_with_only_lock_blobs_and_no_tag_overwrite(self):
        old = LOCK.encode()
        new = LOCK.replace("CALLS_VERSION=1000.12.99", "CALLS_VERSION=1000.12.100").encode()
        ids = []
        for name in ("first", "second"):
            directory = self.temp / name
            directory.mkdir()
            with mock.patch.dict(os.environ, {"GIT_AUTHOR_NAME": "untrusted", "GIT_COMMITTER_DATE": "now"}):
                ids.append(planner.create_manifest(directory, 3, old, new))
            repo = directory / "input/upstream.git"
            self.assertEqual(planner.git(repo, "tag", "--list"), b"v3\nv4\n")
            self.assertEqual(planner.git(repo, "cat-file", "blob", "v3:upstream.env"), old)
            self.assertEqual(planner.git(repo, "cat-file", "blob", "v4:upstream.env"), new)
            self.assertEqual(planner.git(repo, "ls-tree", "--name-only", "v4"), b"upstream.env\n")
            self.assertEqual(planner.git(repo, "rev-parse", "v4^"), planner.git(repo, "rev-parse", "v3"))
            self.assertEqual(planner.git(repo, "rev-list", "--count", "v4"), b"2\n")
            self.assertIn(b"MaintainedMost Planner <planner@maintainedmost.invalid> 946684800 +0000", planner.git(repo, "cat-file", "commit", ids[-1]))
            with self.assertRaises(FileExistsError):
                planner.create_manifest(directory, 3, old, new)
            self.assertEqual(planner.git(repo, "rev-parse", "v4").decode().strip(), ids[-1])
        self.assertEqual(ids[0], ids[1])

    def test_real_local_remote_annotated_tags_are_peeled_not_treated_as_commits(self):
        planner.git(self.root, "tag", "-a", "v11.11.1", "-m", "Annotated fixture", self.base)
        planner.git(self.root, "tag", "v11.11.2", self.base)
        planner.git(self.root, "tag", "v12.0.0-rc1", self.base)
        result = subprocess.run(["git", "-c", "protocol.file.allow=always", "ls-remote", "--tags", "--", str(self.root), "refs/tags/v*", "refs/tags/v*^{}"],
                                cwd=self.temp, env={"PATH": os.defpath, "HOME": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                                                   "GIT_CONFIG_SYSTEM": os.devnull, "GIT_ALLOW_PROTOCOL": "file"},
                                check=True, capture_output=True, timeout=10)
        parsed = planner.stable_tags(result.stdout)
        self.assertEqual(parsed, {"v11.11.1": self.base, "v11.11.2": self.base})
        self.assertNotEqual(planner.git(self.root, "rev-parse", "v11.11.1").decode().strip(), parsed["v11.11.1"])

    def test_public_git_does_not_discover_parent_checkout_configuration(self):
        child = self.root / "build/output"
        child.mkdir(parents=True)
        with self.assertRaises(planner.PlanError):
            planner.git(child, "rev-parse", "--show-toplevel", public=True)

    def test_atomic_config_binding_inventory_and_verifier_hash(self):
        summary = self.plan()
        config = json.loads((self.output / "config.json").read_text())
        binding = json.loads((self.output / "plan.json").read_text())
        self.assertEqual(set(binding), {"base", "version", "manifest", "files", "patches"})
        self.assertEqual(binding["base"], self.base)
        self.assertEqual(binding["version"], 1)
        self.assertEqual(binding["patches"], ["patches/server/0001-feature.patch", "patches/server/0002-other.patch",
                                            "patches/calls/0001-feature.patch", "patches/transcriber/0001-feature.patch"])
        self.assertEqual(config["providers"], {"provider-1": MODELS["providers"][PROVIDER_ALIAS]})
        self.assertEqual(config["models"], {"model-1": {**MODELS["models"][MODEL_ALIAS], "provider": "provider-1"}})
        self.assertEqual(config["roles"], {"audit": "model-1", "resolve": ["model-1"]})
        self.assertEqual(config["data_dir"], "/data")
        self.assertEqual(config["repo_timeout"], "2h")
        self.assertEqual(config["alerts"], [])
        self.assertEqual(config["log_level"], "error")
        self.assertEqual(config["sandbox"], {"mode": "broker", "socket": "/run/vibeci/sandboxd.sock"})
        self.assertEqual(len(config["repos"]), 1)
        repo = config["repos"][0]
        self.assertEqual(repo["fork"], {"url": "https://github.com/test-owner/maintainedmost.git", "branch": "main",
                                       "auth": {"token": "file:/run/secrets/fork_read_token"}})
        self.assertEqual(repo["upstream"], {"url": "/data/input/upstream.git", "fetch": "full", "tags": "v1", "tag_format": "v{version}"})
        self.assertEqual(repo["description"], self.files["maintenance/intent.md"])
        patches = repo["patches"]
        self.assertEqual(set(patches["update_files"]), {"upstream.env", "Containerfile"})
        self.assertEqual(patches["version_file"], "maintenance/upstream-version")
        expected = {**patches["update_files"], "maintenance/upstream-version": "1\n"}
        self.assertEqual(binding["files"], {path: hashlib.sha256(text.encode()).hexdigest() for path, text in expected.items()})
        self.assertTrue(patches["update_files"]["upstream.env"].startswith("# Preserve this trusted comment.\n\n"))
        self.assertEqual(planner.parse_env(patches["update_files"]["upstream.env"]), summary["new"])
        self.assertEqual(patches["update_files"]["Containerfile"], CONTAINERFILE.replace(PINS["SERVER_IMAGE"], summary["new"]["SERVER_IMAGE"]))
        for component, source, patch_set in zip(planner.SOURCES, patches["sources"], patches["sets"]):
            repository, key = planner.SOURCES[component]
            self.assertEqual(source["path"], component)
            self.assertEqual(source["url"], "https://github.com/" + repository + ".git")
            self.assertEqual(source["fetch"], "partial")
            self.assertEqual(source["revision_file"], "upstream.env")
            self.assertEqual(planner.re.search(source["revision_regex"], expected["upstream.env"])[1], summary["new"][key])
            self.assertEqual(patch_set, {"root": component, "glob": f"patches/{component}/*.patch", "strip": 1})
        self.assertEqual((patches["fuzz"], patches["ignore_whitespace"], patches["drop_upstreamed"]), (0, False, False))
        self.assertEqual(patches["verify_tree"], "touched")
        self.assertEqual(repo["sandbox"], {"profile": "maintainedmost", "verify_profile": "maintainedmost"})
        self.assertEqual(repo["prefetch"], [])
        self.assertEqual(repo["review"], {"enabled": False, "block_on": "suspicious", "min_confidence": 0.7})
        self.assertEqual(repo["on_upstream_rewrite"], "hold")
        command = repo["verify"][0]["run"]
        expected_hash = hashlib.sha256(self.files["maintenance/verify.py"].encode()).hexdigest()
        self.assertEqual(len(repo["verify"]), 1)
        self.assertTrue(command.startswith(f"printf '%s\\n' '{expected_hash}  /opt/maintainedmost/verify.py' | sha256sum -c - && python3 /opt/maintainedmost/verify.py '"))
        self.assertEqual(json.loads(base64.urlsafe_b64decode(shlex.split(command)[-1])), binding)
        self.assertRegex(summary["branch"], rf"\Avibeci/update-1-{self.base[:12]}-{binding['manifest'][:12]}-[0-9a-f]{{12}}\Z")
        self.assertEqual(repo["push"], {"mode": "branch", "branch": summary["branch"], "dry_run": True})
        self.assertFalse(summary["push"])
        manifest = self.output / "input/upstream.git"
        self.assertEqual(planner.git(manifest, "cat-file", "blob", "v0:upstream.env").decode(), LOCK)
        self.assertEqual(planner.git(manifest, "cat-file", "blob", "v1:upstream.env").decode(), expected["upstream.env"])
        self.assertEqual(planner.git(self.root, "status", "--porcelain"), b"")

    def test_replanning_changes_only_proposal_branch(self):
        retry_output = self.temp / "retry"
        nonces = ["0123456789ab", "fedcba987654"]
        with mock.patch.object(planner.secrets, "token_hex", side_effect=nonces) as nonce:
            first = self.plan()
            second = planner.plan(self.root, "test-owner/maintainedmost", "main", retry_output,
                                  self.models_file, resolver=FakeResolver().updates())
        self.assertEqual(nonce.call_args_list, [mock.call(6), mock.call(6)])
        self.assertNotEqual(first["branch"], second["branch"])
        self.assertEqual([summary["branch"].rsplit("-", 1)[1] for summary in (first, second)], nonces)
        self.assertEqual(first["branch"].rsplit("-", 1)[0], second["branch"].rsplit("-", 1)[0])
        self.assertEqual(first, dict(second, branch=first["branch"]))
        self.assertEqual((self.output / "plan.json").read_bytes(), (retry_output / "plan.json").read_bytes())
        first_config = json.loads((self.output / "config.json").read_text())
        second_config = json.loads((retry_output / "config.json").read_text())
        self.assertEqual(first_config["repos"][0]["push"]["branch"], first["branch"])
        self.assertEqual(second_config["repos"][0]["push"]["branch"], second["branch"])
        second_config["repos"][0]["push"]["branch"] = first["branch"]
        self.assertEqual(first_config, second_config)

    def test_noop_writes_only_public_summaries(self):
        with mock.patch.object(planner.secrets, "token_hex") as nonce:
            summary = self.plan(FakeResolver())
        nonce.assert_not_called()
        self.assertFalse(summary["changed"])
        self.assertEqual(summary["version"], 0)
        self.assertIsNone(summary["manifest"])
        self.assertIsNone(summary["branch"])
        self.assertEqual(summary["old"], summary["new"])
        self.assertEqual({path.name for path in self.output.iterdir()}, {"summary.json", "summary.md"})
        self.assertEqual(json.loads((self.output / "summary.json").read_text()), summary)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        for path in self.output.iterdir():
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_push_only_opts_in_to_controller_publication_and_never_enables_engine_push(self):
        with mock.patch.object(planner, "git", wraps=planner.git) as git:
            summary = self.plan(push=True)
        self.assertTrue(summary["push"])
        config = json.loads((self.output / "config.json").read_text())
        self.assertEqual(config["repos"][0]["push"], {"mode": "branch", "branch": summary["branch"], "dry_run": True})
        self.assertFalse(any("push" in call.args[1:] for call in git.call_args_list))
        self.assertFalse(any("commit-tree" in call.args[1:] and call.args[0] == self.root for call in git.call_args_list))

    def test_config_serialization_preserves_numeric_alias_order(self):
        models = copy.deepcopy(MODELS)
        models["models"] = {f"{MODEL_ALIAS}-{index}": {"provider": PROVIDER_ALIAS, "model": f"mock-marker-private-id-{index}"}
                            for index in range(12, 0, -1)}
        aliases = list(models["models"])
        models["roles"] = {"audit": aliases[0], "resolve": aliases[::-1]}
        self.models_file.write_bytes(json_bytes(models))
        self.plan()
        config_text = (self.output / "config.json").read_text()
        config = json.loads(config_text)
        self.assertEqual(list(config["models"]), [f"model-{index}" for index in range(1, 13)])
        self.assertEqual(config["roles"]["resolve"], [f"model-{index}" for index in range(12, 0, -1)])
        fragment = {key: config[key] for key in MODELS}
        self.assertEqual(json_bytes(fragment), json_bytes(planner.normalize_models(fragment)))
        for alias in (PROVIDER_ALIAS, *aliases):
            self.assertNotIn(alias, config_text)

    def test_private_runtime_files_ignore_permissive_umask_and_never_overwrite(self):
        previous = os.umask(0)
        try:
            self.plan()
        finally:
            os.umask(previous)
        for path in (self.output, self.output / "input", self.output / "input/upstream.git"):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700)
        for path in (self.models_file, *(self.output / name for name in ("config.json", "plan.json", "summary.json", "summary.md"))):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                planner.write_private(path, "mock-marker")
            self.assertEqual(path.read_bytes(), original)
        link = self.output / "mock-marker-link"
        link.symlink_to(self.models_file)
        with self.assertRaises(FileExistsError):
            planner.write_private(link, "mock-marker")
        self.assertEqual(self.models_file.read_bytes(), json_bytes(MODELS))

    def test_secret_model_input_requires_private_regular_single_link_file(self):
        resolver = FakeResolver().updates()
        for mode in (0o644, 0o640, 0o604, 0o660, 0o400, 0o700):
            self.models_file.chmod(mode)
            with self.subTest(mode=oct(mode)), self.assertRaisesRegex(planner.PlanError, "mode 0600"):
                self.plan(resolver)
        self.models_file.chmod(0o600)
        original = self.models_file
        for kind in ("symlink", "hardlink", "fifo", "directory"):
            self.models_file = self.temp / kind
            if kind == "symlink":
                self.models_file.symlink_to(original)
            elif kind == "hardlink":
                os.link(original, self.models_file)
            elif kind == "fifo":
                os.mkfifo(self.models_file, 0o600)
            else:
                self.models_file.mkdir(mode=0o700)
            with self.subTest(kind=kind), self.assertRaises((planner.PlanError, OSError)):
                self.plan(resolver)
            if kind == "directory":
                self.models_file.rmdir()
            else:
                self.models_file.unlink()
        self.assertEqual(resolver.requests, [])
        self.assertFalse(self.output.exists())
        self.assertEqual(original.read_bytes(), json_bytes(MODELS))

    def test_dirty_tracked_or_staged_inputs_fail_before_any_resolution(self):
        for path in ("upstream.env", "Containerfile", "maintenance/verify.py", "patches/server/0001-feature.patch", "scripts/trusted.py"):
            target = self.root / path
            original = target.read_text()
            target.write_text(original + "dirty\n")
            resolver = FakeResolver()
            with self.subTest(path=path), self.assertRaisesRegex(planner.PlanError, "clean committed"):
                self.plan(resolver)
            self.assertEqual(resolver.requests, [])
            self.assertFalse(self.output.exists())
            target.write_text(original)
        target = self.root / "maintenance/verify.py"
        target.write_text("staged untrusted verifier\n")
        planner.git(self.root, "add", "--", "maintenance/verify.py")
        target.write_text(self.files["maintenance/verify.py"])
        with self.assertRaisesRegex(planner.PlanError, "clean committed"):
            self.plan()

    def test_untracked_maintenance_inputs_fail_but_ignored_build_outputs_do_not(self):
        for path in ("maintenance/new.json", "patches/calls/new.patch", "scripts/extra.py", ".github/workflows/new.yml"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("untracked\n")
            with self.subTest(path=path), self.assertRaisesRegex(planner.PlanError, "untracked"):
                self.plan()
            target.unlink()
        for path in ("build/output.bin", "scripts/__pycache__/ignored.pyc"):
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"ignored normal output")
        self.assertFalse(self.plan(FakeResolver())["changed"])

    def test_fresh_directory_is_required_even_if_empty_or_symlink(self):
        self.output.mkdir()
        with self.assertRaisesRegex(planner.PlanError, "fresh"):
            self.plan()
        self.output.rmdir()
        self.output.symlink_to(self.temp / "nonexistent")
        with self.assertRaisesRegex(planner.PlanError, "fresh"):
            self.plan()
        self.assertFalse((self.temp / "nonexistent").exists())

    def test_ignore_rules_cannot_hide_untracked_maintenance_sources(self):
        self.commit({".gitignore": self.files[".gitignore"] + "maintenance/extra.py\npatches/calls/extra.patch\n"})
        for name in ("maintenance/extra.py", "patches/calls/extra.patch"):
            path = self.root / name
            path.write_text("ignored maintenance source\n")
            self.assertEqual(planner.git(self.root, "status", "--porcelain"), b"")
            with self.subTest(name=name), self.assertRaisesRegex(planner.PlanError, "untracked maintenance inputs"):
                self.plan()
            path.unlink()

    def test_missing_committed_selector_or_nonregular_verifier_is_rejected(self):
        planner.git(self.root, "update-index", "--force-remove", "maintenance/upstream-version")
        tree = planner.git(self.root, "write-tree").decode().strip()
        head = planner.git(self.root, "commit-tree", tree, data=b"Missing selector fixture\n").decode().strip()
        planner.git(self.root, "update-ref", "refs/heads/main", head)
        (self.root / "maintenance/upstream-version").unlink()
        with self.assertRaisesRegex(planner.PlanError, "committed regular file"):
            self.plan()
        self.commit({"maintenance/upstream-version": "0\n"})
        target = self.root / "maintenance/verify.py"
        target.unlink()
        target.symlink_to("intent.md")
        self.commit({})
        with self.assertRaisesRegex(planner.PlanError, "committed regular file"):
            self.plan()

    def test_invalid_selector_and_vibeci_replacement_placeholder_fail(self):
        for selector in ("00\n", "1", "-1\n", "1\n2\n", f"{planner.MAX_INTEGER}\n"):
            self.commit({"maintenance/upstream-version": selector})
            with self.subTest(selector=selector), self.assertRaisesRegex(planner.PlanError, "one nonnegative integer"):
                self.plan()
        self.commit({"maintenance/upstream-version": "0\n", "Containerfile": CONTAINERFILE + "# Literal {version} would be rewritten.\n"})
        with self.assertRaisesRegex(planner.PlanError, "substitution token"):
            self.plan()
        self.assertFalse(self.output.exists())

    def test_cli_prints_only_public_summary_and_never_enables_engine_push(self):
        for push in (False, True):
            self.output = self.temp / ("push-plan" if push else "dry-plan")
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(planner.Path, "cwd", return_value=self.root), mock.patch.object(planner, "PublicResolver", return_value=FakeResolver().updates()):
                with redirect_stdout(out), redirect_stderr(err):
                    code = planner.main(["--repository", "test-owner/maintainedmost", "--output", str(self.output),
                                         "--models", str(self.models_file)] + (["--push"] if push else []))
            self.assertEqual(code, 0)
            self.assertEqual(err.getvalue(), "")
            self.assertEqual(json.loads(out.getvalue()), json.loads((self.output / "summary.json").read_text()))
            self.assertEqual(json.loads(out.getvalue())["push"], push)
            self.assertEqual(len(out.getvalue().splitlines()), 1)
            config = json.loads((self.output / "config.json").read_text())
            self.assertTrue(config["repos"][0]["push"]["dry_run"])
            self.assertEqual(config["repos"][0]["fork"]["branch"], "main")
            for public in (out.getvalue(), err.getvalue(), (self.output / "plan.json").read_text(),
                           (self.output / "summary.json").read_text(), (self.output / "summary.md").read_text()):
                self.assertNotIn("mock-marker", public)
                self.assertNotIn("private-provider.invalid", public)

    def test_cli_model_errors_never_print_private_values_or_tracebacks(self):
        invalid = [b'{"mock-marker":"https://private-provider.invalid",', b"mock-marker" * 6554,
                   b'{"mock-marker":"\xff"}', json_bytes({**MODELS, "mock-marker": "mock-marker"})]
        for section, alias, key, value in (("providers", PROVIDER_ALIAS, "api_key", "mock-marker-secret"),
                                            ("providers", PROVIDER_ALIAS, "type", {"mock-marker": "mock-marker"}),
                                            ("providers", PROVIDER_ALIAS, "base_url", "https://mock-marker@private-provider.invalid"),
                                            ("models", MODEL_ALIAS, "provider", ["mock-marker"]),
                                            ("models", MODEL_ALIAS, "model", {"mock-marker": "mock-marker"})):
            models = copy.deepcopy(MODELS)
            models[section][alias][key] = value
            invalid.append(json_bytes(models))
        for index, data in enumerate(invalid):
            self.models_file.write_bytes(data)
            out, err = io.StringIO(), io.StringIO()
            with self.subTest(case=index), mock.patch.object(planner.Path, "cwd", return_value=self.root), redirect_stdout(out), redirect_stderr(err):
                code = planner.main(["--repository", "test-owner/maintainedmost", "--output", str(self.output), "--models", str(self.models_file)])
            self.assertEqual(code, 1)
            self.assertEqual(out.getvalue(), "")
            self.assertTrue(err.getvalue().startswith("vibeci-plan: "))
            for marker in ("mock-marker", "private-provider.invalid", "Traceback"):
                self.assertNotIn(marker, err.getvalue())
            self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.assertFalse(self.output.exists())

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = planner.main(["--repository", "test-owner/maintainedmost", "--output", str(self.output),
                                 "--models", str(self.temp / "mock-marker-missing")])
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue(), "")
        self.assertNotIn("mock-marker", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())


if __name__ == "__main__":
    unittest.main()
