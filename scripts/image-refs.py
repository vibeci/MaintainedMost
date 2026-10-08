#!/usr/bin/env python3
"""Map a server tag to job images accepted by the pinned, unmodified offloader."""

import re
import sys


COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
REPOSITORY = re.compile(rf"(?:[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]+)?/)?{COMPONENT}(?:/{COMPONENT})*")
VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-dev[0-9]*)?")
DEV_VERSION = "v1.0.0-dev0"


def validate_registry(registry):
    if len(registry) > 255 or not REPOSITORY.fullmatch(registry):
        raise ValueError("registry must be a lowercase Docker namespace, without scheme, tag, digest or trailing slash")
    host = registry.split("/", 1)[0]
    if ":" in host and not 1 <= int(host.rsplit(":", 1)[1]) <= 65535:
        raise ValueError("registry port must be between 1 and 65535")


def validate_job_image(registry, image, kind):
    validate_registry(registry)
    prefix = f"{registry}/calls-{kind}:"
    if not image.startswith(prefix):
        raise ValueError(f"job image must start with {prefix!r}")
    tag = image[len(prefix):]
    if len(prefix) - 1 > 255 or len(tag) > 128:
        raise ValueError("job image exceeds Docker repository/tag length limits")
    match = VERSION.fullmatch(tag)
    minimum = (0, 6, 0) if kind == "recorder" else (0, 1, 0)
    version = tuple(map(int, match.groups())) if match else ()
    # The offloader's semver parser uses signed 64-bit components.
    if not match or version < minimum or any(part > (1 << 63) - 1 for part in version):
        raise ValueError(f"unsupported {kind} version: use vMAJOR.MINOR.PATCH[-devN], minimum {minimum}")


def image_refs(server_image, recorder_version, registry="", transcriber_image=""):
    repository, separator, tag = server_image.rpartition(":")
    if not separator or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
        raise ValueError("server image must have an explicit Docker tag (not a digest)")
    validate_registry(repository)
    registry = registry or repository
    validate_registry(registry)
    stable = VERSION.fullmatch(tag) and "-dev" not in tag
    job_version = tag if stable else DEV_VERSION
    transcriber_image = transcriber_image or f"{registry}/calls-transcriber:{job_version}"
    validate_job_image(registry, transcriber_image, "transcriber")
    if stable and transcriber_image != f"{registry}/calls-transcriber:{tag}":
        raise ValueError("a release transcriber must use the server's stable release tag")
    recorder_image = f"{registry}/calls-recorder:{recorder_version}"
    validate_job_image(registry, recorder_image, "recorder")
    return registry, transcriber_image, recorder_image


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--check-transcriber"] and len(sys.argv) == 4:
            validate_job_image(sys.argv[2], sys.argv[3], "transcriber")
        elif 3 <= len(sys.argv) <= 5:
            print(*image_refs(*sys.argv[1:]))
        else:
            sys.exit("usage: image-refs.py SERVER_IMAGE RECORDER_VERSION [REGISTRY [TRANSCRIBER_IMAGE]]\n"
                     "       image-refs.py --check-transcriber REGISTRY IMAGE")
    except ValueError as error:
        sys.exit(f"invalid image configuration: {error}")
