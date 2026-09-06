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
- stacked push-only publishing
- stacked PR creation/update
- release promotion PR helpers
- post-merge sync flows
- repository/fork synchronization

Rule of thumb: if it changes or validates git state across the workspace or III
submodules, it belongs here.

The canonical chain is feature/work-sweep -> `develop` ->
`promote/develop-to-main/<operation-id>` -> `main` -> workspace-only `release`
-> immutable `vX.Y.Z`. Use `create_stack_prs.sh` for the feature stack,
`create_develop_to_main_prs.sh` for promotion, and
`create_main_to_release_pr.sh` for the final workspace PR. Each defaults to a
read-only plan; apply requires its explicit flag. Full ref, evidence, retry, and
gitlink rules are in [`../docs/dependency-governance.md`](../docs/dependency-governance.md).

### `scripts/remote`

Retained compatibility location for retired remote bootstrap entry points.

Current scope:

- `install_remote.bash` fails without mutation and prints the native GC and
  checkout-development replacements

Current host provisioning is owned by `iii gc provision`; remote runtime and
receiver operations are owned by the canonical III CLI.

### `scripts/workspace`

General workspace utilities that are not CI-only and not primarily git-state
management.

Current scope:

- docker compose helpers
- curated III-only test-suite runners
- `iii-dev`, the host-to-devcontainer development/operator command bridge

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
