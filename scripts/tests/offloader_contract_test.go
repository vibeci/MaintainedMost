package infrastructure_test

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/mattermost/calls-offloader/public/job"
	"github.com/mattermost/calls-offloader/service"
)

func TestMappedImagesPassRealOffloaderValidation(t *testing.T) {
	t.Setenv("DEV_MODE", "")
	t.Setenv("TEST_MODE", "")
	_, source, _, _ := runtime.Caller(0)
	mapper := filepath.Join(filepath.Dir(source), "..", "image-refs.py")
	for _, server := range []string{
		"ghcr.io/owner/maintainedmost:v1.5.0",
		"maintainedmost:dev",
		"maintainedmost:ci",
		"registry.example:5000/team/maintainedmost:test-abcdef",
	} {
		t.Run(server, func(t *testing.T) {
			out, err := exec.Command("python3", mapper, server, "v0.8.13").CombinedOutput()
			if err != nil {
				t.Fatalf("image mapping failed: %v: %s", err, out)
			}
			refs := strings.Fields(string(out))
			if len(refs) != 3 {
				t.Fatalf("unexpected mapping: %q", out)
			}
			registry, transcriber, recorder := refs[0], refs[1], refs[2]
			if err := (job.ServiceConfig{Runners: []string{recorder, transcriber}}).IsValid(registry); err != nil {
				t.Fatalf("init validation rejected mapped images: %v", err)
			}
			for image, kind := range map[string]job.Type{recorder: job.TypeRecording, transcriber: job.TypeTranscribing} {
				if err := job.RunnerIsValid(image, registry); err != nil {
					t.Fatal(err)
				}
				if err := (job.Config{Type: kind, Runner: image, MaxDurationSec: 60}).IsValid(registry); err != nil {
					t.Fatalf("RunJob validation rejected mapped image: %v", err)
				}
			}
		})
	}
}

func TestOffloaderRejectsOldAndUnsupportedImageContracts(t *testing.T) {
	t.Setenv("DEV_MODE", "")
	t.Setenv("TEST_MODE", "")
	registry := "ghcr.io/owner/maintainedmost"
	for _, image := range []string{
		"ghcr.io/owner/maintainedmost:v1.5.0-transcriber",
		registry + "/calls-transcriber:latest",
		registry + "/calls-transcriber:v0.0.0-dev0",
		registry + "/calls-recorder:v0.5.0",
		registry + "/calls-transcriber:v1.5.0-rc1",
		registry + "/calls-transcriber:v9223372036854775808.0.0",
		registry + "/calls-transcriber@sha256:" + strings.Repeat("a", 64),
		"mattermost/calls-recorder:v0.8.13",
	} {
		if err := job.RunnerIsValid(image, registry); err == nil {
			t.Errorf("unexpectedly accepted %s", image)
		}
		if err := (job.ServiceConfig{Runners: []string{image}}).IsValid(registry); err == nil {
			t.Errorf("init unexpectedly accepted %s", image)
		}
		if err := (job.Config{Type: job.TypeTranscribing, Runner: image, MaxDurationSec: 60}).IsValid(registry); err == nil {
			t.Errorf("RunJob unexpectedly accepted %s", image)
		}
	}
}

func TestOffloaderRegistryEnvironment(t *testing.T) {
	t.Setenv("DOCKER_IMAGE_REGISTRY", "ignored.invalid/wrong-setting")
	t.Setenv("JOBS_IMAGEREGISTRY", "")
	if err := os.Unsetenv("JOBS_IMAGEREGISTRY"); err != nil {
		t.Fatal(err)
	}
	var defaults service.Config
	defaults.SetDefaults()
	if err := defaults.ParseFromEnv(); err != nil {
		t.Fatal(err)
	}
	if defaults.Jobs.ImageRegistry != job.ImageRegistryDefault {
		t.Fatalf("unsupported DOCKER_IMAGE_REGISTRY changed the registry: %s", defaults.Jobs.ImageRegistry)
	}
	t.Setenv("JOBS_IMAGEREGISTRY", "registry.example:5000/team/maintainedmost")
	var config service.Config
	config.SetDefaults()
	if err := config.ParseFromEnv(); err != nil {
		t.Fatal(err)
	}
	if config.Jobs.ImageRegistry != "registry.example:5000/team/maintainedmost" {
		t.Fatalf("wrong registry after env parsing: %s", config.Jobs.ImageRegistry)
	}
}
