# Scripts Layout

Workspace scripts are grouped by responsibility so the folder name tells you
where to look first.

## Folders

### `scripts/ci`

Scripts intended primarily for CI or branch-gate workflows.

- branch-policy verification for III submodules
- protected-branch pointer validation

Rule of thumb: if a script is meant to run in GitHub Actions or fail a PR gate,
it belongs here.

### `scripts/git`

Scripts for repository state management, branch alignment, lock-file
governance, submodule pointer refresh, and PR-stack workflows.

This is the bucket for:

- branch policy
- lock-file verification/update
- stacked PR creation/update
- post-merge sync flows
- repository/fork synchronization

Rule of thumb: if it changes or validates git state across the workspace or III
submodules, it belongs here.

### `scripts/remote`

Scripts for bootstrapping or operating remote-development/deployment workflows.

Current scope:

- local machine setup for remote III workflows

Rule of thumb: if the script prepares SSH, remote CLI, or deployment-host
interaction, it belongs here.

### `scripts/workspace`

General workspace utilities that are not CI-only and not primarily git-state
management.

Current scope:

- docker compose helpers
- curated III-only test-suite runners

Rule of thumb: if the script helps a developer inspect, build, or test the
workspace locally, it belongs here.

## Devcontainer Hooks

Devcontainer lifecycle hooks live under `.devcontainer/` instead of `scripts/`
because they are configuration-coupled to the devcontainer itself:

- `.devcontainer/post_create.sh`
- `.devcontainer/post_start.sh`

## VS Code Helpers

Editor-only helper scripts live under `.vscode/` when they are only consumed by
VS Code `tasks.json` or `launch.json`.

Current examples:

- `.vscode/get_debug_pid.sh`
- `.vscode/get_iii_drone_package_names.sh`
- `.vscode/get_package_executable_names.sh`

## Future Split Guidance

If `scripts/workspace/` grows significantly, the next logical split would be:

- `scripts/workspace/test/` for test runners
- `scripts/workspace/discovery/` for package/executable introspection
- `scripts/workspace/docker/` for container-build helpers

That split is not necessary yet, but it is the clean next step once the folder
stops being easy to scan.
