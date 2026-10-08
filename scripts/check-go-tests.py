#!/usr/bin/env python3
"""Relay go test -json output and enforce required test/package results."""

import argparse
import json
import math
from pathlib import Path
import re
import sys


def decode_json(text):
    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ValueError(f"invalid JSON constant: {value}")

    return json.loads(text, object_pairs_hook=unique_keys, parse_constant=invalid_constant)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern", help="required test regex (searches full test/subtest names)")
    parser.add_argument("--inventory", type=Path, help="protected suite/package/test JSON inventory")
    parser.add_argument("--suite", help="suite to require in addition to the regex")
    args = parser.parse_args()
    if (args.inventory is None) != (args.suite is None):
        parser.error("--inventory and --suite must be used together")
    try:
        required = re.compile(args.pattern)
    except re.error as error:
        parser.error(f"invalid required test regex: {error}")

    expected = set()
    if args.inventory is not None:
        try:
            inventory = decode_json(args.inventory.read_text(encoding="utf-8"))
            if not isinstance(inventory, dict):
                raise ValueError("inventory must map suites to packages")
            if args.suite not in inventory:
                raise ValueError(f"unknown suite: {args.suite}")
            packages = inventory[args.suite]
            if not isinstance(packages, dict) or not packages:
                raise ValueError("suite must contain required packages")
            for package, tests in packages.items():
                if not package or re.search(r"\s", package):
                    raise ValueError(f"invalid required package: {package!r}")
                if not isinstance(tests, list) or not tests:
                    raise ValueError(f"{package}: required tests must be a nonempty list")
                for test in tests:
                    if not isinstance(test, str) or not re.fullmatch(r"Test\w*", test):
                        raise ValueError(f"{package}: invalid top-level test: {test!r}")
                    if (package, test) in expected:
                        raise ValueError(f"{package}: duplicate required test: {test}")
                    expected.add((package, test))
        except (OSError, ValueError) as error:
            parser.error(f"invalid inventory {args.inventory}: {error}")

    actions = {"start", "run", "pause", "cont", "pass", "bench", "fail", "output", "skip",
               "build-output", "build-fail"}
    passed = 0
    test_passes = set()
    seen_packages = set()
    completed_packages = set()
    skipped = []
    failed = False
    invalid = False
    for number, line in enumerate(sys.stdin, 1):
        try:
            event = decode_json(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
            action = event.get("Action")
            if not isinstance(action, str) or action not in actions:
                raise ValueError(f"invalid Action: {action!r}")
            for key in ("Package", "Test", "Output", "ImportPath", "Time", "FailedBuild"):
                if key in event and not isinstance(event[key], str):
                    raise ValueError(f"{key} must be a string")
            for key in ("Package", "Test", "ImportPath"):
                if key in event and not event[key].strip():
                    raise ValueError(f"{key} must not be empty")
            if "Elapsed" in event:
                elapsed = event["Elapsed"]
                if (type(elapsed) not in (int, float) or elapsed < 0 or
                        isinstance(elapsed, float) and not math.isfinite(elapsed)):
                    raise ValueError("Elapsed must be a finite nonnegative number")
            test = event.get("Test", "")
            package = event.get("Package", "")
            build_event = action in ("build-output", "build-fail")
            if build_event and not event.get("ImportPath"):
                raise ValueError("build event must name its ImportPath")
            if expected and not build_event and not package:
                raise ValueError("event must name its Package when using an inventory")
            if action in ("run", "pause", "cont") and not test:
                raise ValueError(f"{action} event must name its Test")
            if action == "start" and test:
                raise ValueError("start event must be a package event")
            if action in ("output", "build-output") and "Output" not in event:
                raise ValueError(f"{action} event must contain Output")
        except ValueError as error:
            print(f"invalid go test JSON at line {number}: {error}", file=sys.stderr)
            invalid = True
            continue

        failed |= action in ("fail", "build-fail")
        if event.get("Output"):
            print(event["Output"], end="", flush=True)
        if build_event:
            continue

        if expected:
            seen_packages.add(package)
            if package in completed_packages:
                print(f"event after terminal package pass: {package}", file=sys.stderr)
                invalid = True
            if action == "pass":
                if test:
                    test_passes.add((package, test))
                else:
                    completed_packages.add(package)

        matches = bool(test and required.search(test))
        if action == "pass" and matches:
            passed += 1
        if action == "skip" and (matches or (package, test.split("/", 1)[0]) in expected or expected and not test):
            skipped.append(f"{package}: {test}" if package and test else test or package)

    missing_tests = expected - test_passes
    missing_packages = ({package for package, _ in expected} | seen_packages) - completed_packages
    if failed or invalid or not passed or skipped or missing_tests or missing_packages:
        print(
            f"required suite {args.suite or required.pattern!r}: {passed} passed, "
            f"skipped={skipped}, failed={failed}, invalid={invalid}",
            file=sys.stderr,
        )
        for package in sorted(missing_packages):
            print(f"missing terminal package pass: {package}", file=sys.stderr)
        for package, test in sorted(missing_tests):
            print(f"missing required test pass: {package}: {test}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
