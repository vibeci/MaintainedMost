#!/usr/bin/env python3
"""Validate Go reports as data from a fresh, trusted policy checkout."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import stat
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SUITES = ("server-api", "server-oidc", "server-config", "server-model", "calls", "transcriber")
MAX_REPORT_BYTES = 64 * 1024 * 1024
CHECK_TIMEOUT_SECONDS = 60


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report_dir", type=Path, metavar="REPORT_DIR",
                        help="directory containing exactly six suite.jsonl files (1 byte to 64 MiB each)")
    args = parser.parse_args()
    checker = ROOT / "scripts/check-go-tests.py"
    inventory_path = ROOT / "maintenance/required-tests.json"
    try:
        if not all(stat.S_ISREG(path.lstat().st_mode) for path in (checker, inventory_path)):
            raise ValueError("policy files must be regular files")
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if not isinstance(inventory, dict) or set(inventory) != set(SUITES):
            raise ValueError("policy must contain exactly the six required suites")
    except (OSError, ValueError):
        print("CI report validation failed: trusted checker or inventory is missing or invalid", file=sys.stderr)
        return 1

    try:
        with ExitStack() as resources:
            # Do not resolve artifact paths: that would silently follow symlinks.
            report_dir = args.report_dir.absolute()
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            directory = os.open(report_dir.anchor, flags)
            resources.callback(os.close, directory)
            for part in report_dir.parts[1:]:
                directory = os.open(part, flags, dir_fd=directory)
                resources.callback(os.close, directory)

            expected = {suite + ".jsonl" for suite in SUITES}
            found = set()
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name not in expected:
                        raise ValueError("unexpected report entry; only the six required suite.jsonl files are allowed")
                    found.add(entry.name)
            if found != expected:
                raise ValueError("missing required reports: " + ", ".join(sorted(expected - found)))

            for suite in SUITES:
                name = suite + ".jsonl"
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(f"{name}: report must be a regular file, not a symlink or hard link")
                if not 0 < info.st_size <= MAX_REPORT_BYTES:
                    raise ValueError(f"{name}: report must contain 1 byte to 64 MiB")
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(descriptor, "rb") as report:
                    opened = os.fstat(report.fileno())
                    if (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size) != (
                            opened.st_dev, opened.st_ino, opened.st_mode, opened.st_nlink, opened.st_size):
                        raise ValueError(f"{name}: report changed while opening")
                    data = report.read(MAX_REPORT_BYTES + 1)
                    if len(data) != info.st_size:
                        raise ValueError(f"{name}: report changed while reading")

                # Never import or execute artifact/producer code, or replay untrusted log output.
                result = subprocess.run(
                    [sys.executable, "-I", "-S", "-B", str(checker), "^Test",
                     "--inventory", str(inventory_path), "--suite", suite],
                    input=data, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=CHECK_TIMEOUT_SECONDS,
                )
                if result.returncode:
                    print(f"{suite}: protected test validation failed (exit {result.returncode}); "
                          "check the producer log for missing tests/packages, skips, failures, or invalid JSON",
                          file=sys.stderr)
                    return 1
                print(f"{suite}: passed")
    except ValueError as error:
        print(f"CI report validation failed: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("CI report validation failed: cannot open reports or run the trusted checker; "
              "use a readable report directory with no symlink path components", file=sys.stderr)
        return 1
    except subprocess.SubprocessError:
        print("CI report validation failed: trusted checker did not complete within its execution limit", file=sys.stderr)
        return 1

    print("Validated all six required Go suites against the trusted inventory.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
