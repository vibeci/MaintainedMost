"""Check MaintainedMost's native build configuration without models or an image.

Requires git, make, patch, CMake and a C/C++ compiler. TRANSCRIBER_TEST_SOURCE
may name a local git clone; WHISPER_TEST_ARCHIVE may name the pinned tarball.
Otherwise the same upstream sources used by the native build are downloaded.
"""

import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
X86_OPTIONS = (
    "NATIVE", "AVX", "AVX2", "AVX_VNNI", "BMI2", "FMA", "F16C",
    "AVX512", "AVX512_VBMI", "AVX512_VNNI", "AVX512_BF16",
    "AMX_TILE", "AMX_INT8", "AMX_BF16",
)


def command(*args, **kwargs):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, timeout=180, **kwargs)
    if result.returncode:
        raise AssertionError(f"{args!r} exited {result.returncode}:\n{result.stdout}")
    return result.stdout


class NativeBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = tempfile.TemporaryDirectory(prefix="maintainedmost-native-")
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.source = cls.root / "transcriber"
        pins = dict(re.findall(r"^([A-Z_]+)=(.*)$", (ROOT / "upstream.env").read_text(), re.M))
        command(
            "bash", "-euc",
            'source "$1/scripts/lib.sh"; checkout_source "$2" "$3" "$4"; '
            'apply_patches "$4" "$1/patches/transcriber"',
            "test", str(ROOT),
            os.environ.get("TRANSCRIBER_TEST_SOURCE", "https://github.com/mattermost/calls-transcriber.git"),
            pins["TRANSCRIBER_TAG"], str(cls.source),
        )
        if command("git", "-C", str(cls.source), "rev-parse", "HEAD").strip() != pins["TRANSCRIBER_TAG"]:
            raise AssertionError("transcriber checkout does not match the pin")

        # Execute upstream make, intercepting only Docker so no image is built/pushed.
        engine = cls.root / "docker-probe"
        log = cls.root / "docker-args.json"
        engine.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"with open({str(log)!r}, 'w') as output:\n"
            "    json.dump(sys.argv[1:], output)\n"
        )
        engine.chmod(0o755)
        cls.build_commands = {}
        for arch in ("amd64", "arm64"):
            command(
                "make", "-C", str(cls.source), "docker-build", "CI=false", f"ARCH={arch}",
                f"GO_VERSION={pins['GO_VERSION']}", f"DOCKER={engine}", "DOCKER_BUILDER_MISSING=0",
                "DOCKER_TAG=maintainedmost/calls-transcriber:v1.0.0-dev0",
                f"DOCKER_BUILD_PLATFORMS=linux/{arch}", "DOCKER_BUILD_OUTPUT_TYPE=docker",
                "GOFLAGS=-p=2 -trimpath",
            )
            cls.build_commands[arch] = json.loads(log.read_text())
        cls.build_command = cls.build_commands["amd64"]
        cls.build_args = {
            value.split("=", 1)[0]: value.split("=", 1)[1]
            for flag, value in zip(cls.build_command, cls.build_command[1:])
            if flag == "--build-arg"
        }
        archive = os.environ.get("WHISPER_TEST_ARCHIVE")
        if archive:
            cls.archive = Path(archive).read_bytes()
        else:
            version = cls.build_args["WHISPER_VERSION"]
            url = f"https://github.com/ggerganov/whisper.cpp/archive/refs/tags/v{version}.tar.gz"
            with urllib.request.urlopen(url, timeout=60) as response:
                cls.archive = response.read()
        digest = hashlib.sha256(cls.archive).hexdigest()
        if digest != cls.build_args["WHISPER_SHA"]:
            raise AssertionError(f"Whisper archive SHA256 mismatch: {digest}")

    def prepare(self, arch):
        temp = tempfile.TemporaryDirectory(prefix=f"maintainedmost-native-{arch}-")
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        with tarfile.open(fileobj=io.BytesIO(self.archive), mode="r:gz") as archive:
            archive.extractall(root, filter="data")
        whisper = root / f"whisper.cpp-{self.build_args['WHISPER_VERSION']}"
        configure = root / f"opus-{self.build_args['OPUS_VERSION']}" / "configure"
        configure.parent.mkdir()
        configure.write_text("#!/bin/sh\nexit 0\n")
        configure.chmod(0o755)
        hook = root / "probe.sh"
        hook.write_text(r'''
cd() {
    if [ "$1" = /tmp ]; then builtin cd "$NATIVE_TEST_TMP"; else builtin cd "$@"; fi
}
wget() { :; }
sha256sum() { while IFS= read -r line; do :; done; }
tar() { :; }
make() { printf '%s\0' "$@" > "$NATIVE_TEST_TMP/make-args"; }
cmake() {
    if [ "$1" = --build ]; then
        printf '%s\0' "$@" > "$NATIVE_TEST_TMP/cmake-build-args"; exit 0
    fi
    printf '%s\0' "$@" > "$NATIVE_TEST_TMP/cmake-args"
}
''')
        # Capture the actual CMake invocations. The Whisper source patch is real;
        # only downloads/Opus compilation are bypassed, not the flag-selection code.
        command(
            "bash", str(self.source / "build/prepare_deps.sh"),
            self.build_args["OPUS_VERSION"], self.build_args["OPUS_SHA"],
            self.build_args["WHISPER_VERSION"], self.build_args["WHISPER_SHA"], "",
            self.build_args["ONNX_VERSION"], arch,
            self.build_args["AZURE_SDK_VERSION"], self.build_args["AZURE_SDK_SHA"], "true",
            env={**os.environ, "BASH_ENV": str(hook), "NATIVE_TEST_TMP": str(root)},
        )
        args = (root / "cmake-args").read_text().rstrip("\0").split("\0")
        self.assertEqual(args[:2], ["-B", "build"])
        self.assertEqual((root / "make-args").read_text().split("\0")[:-1], ["-j2"])
        self.assertEqual((root / "cmake-build-args").read_text().split("\0")[:-1],
                         ["--build", "build", "--parallel", "2", "--config", "Release"])
        return whisper, args

    def test_native_make_uses_the_patched_dependency_script(self):
        for arch, invocation in self.build_commands.items():
            with self.subTest(arch=arch):
                self.assertEqual(invocation[:2], ["buildx", "build"])
                self.assertIn("--output=type=docker", invocation)
                self.assertEqual(invocation[invocation.index("--platform") + 1], f"linux/{arch}")
                self.assertIn("GOFLAGS=-p=2 -trimpath", invocation)
        dockerfile = self.source / self.build_command[self.build_command.index("-f") + 1]
        content = dockerfile.read_text()
        self.assertIn("COPY ./build /src/build", content)
        self.assertIn("RUN /bin/bash ./build/prepare_deps.sh", content)
        builder = content.split(" AS builder\n", 1)[1].split("FROM base AS runner", 1)[0]
        self.assertIn("ARG GOFLAGS", builder)
        for dependency in ("OPUS", "WHISPER", "ONNX", "AZURE_SDK"):
            self.assertIn(f"${{{dependency}_VERSION}}", content)

    def configure(self, whisper, args, arch):
        build = whisper / "build"
        options = [
            "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_BUILD_EXAMPLES=OFF",
            "-DGGML_METAL=OFF", "-DGGML_BLAS=OFF", "-DGGML_ACCELERATE=OFF",
        ]
        if sys.platform == "darwin":
            options.append(f"-DCMAKE_OSX_ARCHITECTURES={arch}")
        env = {key: value for key, value in os.environ.items()
               if key not in ("CFLAGS", "CXXFLAGS", "SOURCE_DATE_EPOCH")}
        output = command("cmake", "-G", "Unix Makefiles", "-S", str(whisper),
                         "-B", str(build), *args[2:], *options, env=env)
        self.assertNotIn("Manually-specified variables were not used", output)
        cache = dict(re.findall(r"^(GGML_[A-Z0-9_]+):(?:BOOL|STRING)=(.*)$", (build / "CMakeCache.txt").read_text(), re.M))
        commands = json.loads((build / "compile_commands.json").read_text())
        return output, cache, commands, env

    def test_amd64_actual_cmake_cache_and_compiler_baseline(self):
        if sys.platform != "darwin" and platform.machine().lower() not in ("x86_64", "amd64"):
            self.skipTest("requires an x86-64 compiler or macOS cross-architecture compiler")
        whisper, args = self.prepare("amd64")
        output, cache, commands, env = self.configure(whisper, args, "x86_64")
        self.assertIn("x86 detected", output)
        for option in (*X86_OPTIONS, "CPU_ALL_VARIANTS", "BACKEND_DL"):
            self.assertEqual(cache[f"GGML_{option}"], "OFF", option)
        self.assertEqual(cache["GGML_CPU"], "ON")
        forbidden = (
            "-march=native", "-mcpu=native", "-mtune=native", "-mavx", "-mfma",
            "-mf16c", "-mbmi", "-mamx", "-msse3", "-mssse3", "-msse4", "-DGGML_SSE42",
        )
        for entry in commands:
            flags = shlex.split(entry["command"])
            self.assertFalse([flag for flag in flags if flag.startswith(forbidden)], entry)
            if "ggml-cpu.dir/" in entry["command"]:
                self.assertIn("-march=x86-64", flags)
                self.assertIn("-mtune=generic", flags)
        cpu = next(entry for entry in commands if entry["file"].endswith("/ggml-cpu.c"))
        flags = shlex.split(cpu["command"])
        index = flags.index("-o")
        del flags[index:index + 2]
        flags.remove("-c")
        flags.remove(cpu["file"])
        macros = command(*flags, "-dM", "-E", "-x", "c", "/dev/null", cwd=cpu["directory"], env=env)
        self.assertIn("#define __SSE2__ 1", macros)
        self.assertNotRegex(macros, r"#define __(?:SSE3|SSSE3|SSE4_1|SSE4_2|AVX\w*|FMA\w*|F16C|BMI\w*|AMX\w*)__ ")

    def test_arm64_keeps_existing_architecture_and_unmodified_whisper(self):
        whisper, args = self.prepare("arm64")
        self.assertEqual(args, ["-B", "build", "-DGGML_NATIVE=OFF", "-DGGML_CPU_ARM_ARCH=armv8-a"])
        cmake = "ggml/src/ggml-cpu/CMakeLists.txt"
        with tarfile.open(fileobj=io.BytesIO(self.archive), mode="r:gz") as archive:
            original = archive.extractfile(f"{whisper.name}/{cmake}").read()
        self.assertEqual((whisper / cmake).read_bytes(), original)
        if sys.platform != "darwin" and platform.machine().lower() not in ("aarch64", "arm64"):
            return  # The flags/source checks above still run on x86-only compilers.
        output, cache, commands, _ = self.configure(whisper, args, "arm64")
        self.assertIn("ARM detected", output)
        self.assertEqual(cache["GGML_NATIVE"], "OFF")
        self.assertEqual(cache["GGML_CPU_ARM_ARCH"], "armv8-a")
        self.assertEqual(cache["GGML_CPU"], "ON")
        for entry in commands:
            self.assertNotIn("=native", entry["command"])
            if "ggml-cpu.dir/" in entry["command"]:
                self.assertIn("-march=armv8-a", shlex.split(entry["command"]))


class BuildPlatformTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="maintainedmost-platform-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        scripts = self.root / "scripts"
        scripts.mkdir()
        for name in ("build-transcriber.sh", "image-refs.py"):
            shutil.copy2(ROOT / "scripts" / name, scripts / name)
        shutil.copy2(ROOT / "upstream.env", self.root / "upstream.env")
        self.log = self.root / "commands"
        (scripts / "lib.sh").write_text('''
container_engine() { echo engine >> "$COMMAND_LOG"; printf '%s\n' docker; }
prepare_source() { echo prepare >> "$COMMAND_LOG"; }
''')
        binary = self.root / "bin"
        binary.mkdir()
        probe = binary / "make"
        probe.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as output:\n"
            "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        )
        probe.chmod(0o755)
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("TARGETARCH", "CALLS_IMAGE_REGISTRY", "TRANSCRIBER_IMAGE")}
        self.env.update(PATH=str(binary) + os.pathsep + self.env["PATH"],
                        COMMAND_LOG=str(self.log), RUN_TESTS="0")

    def test_default_and_explicit_platforms(self):
        for arch in (None, "amd64", "arm64"):
            with self.subTest(arch=arch):
                env = {**self.env, **({"TARGETARCH": arch} if arch is not None else {})}
                command("bash", str(self.root / "scripts/build-transcriber.sh"), env=env)
                invocation = json.loads(self.log.read_text().splitlines()[-1])
                self.assertIn(f"ARCH={arch or 'amd64'}", invocation)
                self.assertIn(f"DOCKER_BUILD_PLATFORMS=linux/{arch or 'amd64'}", invocation)
                self.assertIn("DOCKER_TAG=maintainedmost/calls-transcriber:v1.0.0-dev0", invocation)
                self.assertIn("CI=false", invocation)
                self.assertIn("DOCKER_BUILD_OUTPUT_TYPE=docker", invocation)

    def test_invalid_arch_fails_before_engine_or_checkout(self):
        for arch in ("", "x86_64", "aarch64", "arm/v7", "linux/arm64", "arm64 amd64"):
            with self.subTest(arch=arch):
                result = subprocess.run(
                    ["bash", str(self.root / "scripts/build-transcriber.sh")],
                    env={**self.env, "TARGETARCH": arch}, text=True, capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsupported TARGETARCH", result.stderr)
                self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
