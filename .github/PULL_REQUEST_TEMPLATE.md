## What changed

<!-- One or two sentences about the MaintainedMost change. If this adds a patch, name the file. -->

## Why

<!-- The problem it solves, or the issue it closes. -->

## How it was tested

<!-- Server version, deployment type, and what you actually ran. "Built and
uploaded to a MaintainedMost Compose server on Mattermost 11.11.1, started a four
person call" beats "works fine". -->

## Checklist

- [ ] Patches are small and independent, one feature per patch file. Large patches conflict on the next upstream release and slow down security fixes.
- [ ] Nothing under `server/enterprise/` or any `LICENSE.enterprise` path is modified.
- [ ] Docs are updated if behaviour changed.
- [ ] Relevant regression tests and patch-application checks pass.
