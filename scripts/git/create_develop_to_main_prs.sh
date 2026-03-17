#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  create_develop_to_main_prs.sh - create/update a coordinated develop -> main release PR stack

SYNOPSIS
  scripts/git/create_develop_to_main_prs.sh --release <release-branch> [--source <source-branch>] [--target <target-branch>] [--yes]
  scripts/git/create_develop_to_main_prs.sh -h | --help

DESCRIPTION
  This helper prepares a dedicated release branch for the workspace and all III
  submodules, then creates or updates coordinated PRs from that release branch
  into the target branch.

  The default promotion is:
  - source branch: develop
  - target branch: main

  Workflow:
  1) sync workspace + III submodules onto the source branch
  2) create/switch the workspace release branch
  3) align all III submodules onto matching release branches
  4) create/update coordinated III submodule PRs and the workspace PR

  Use a dedicated release branch instead of opening a PR directly from develop.
  That keeps develop free to continue tracking develop-head submodule pointers
  after the release PR refreshes its gitlinks to origin/main.

OPTIONS
  --release <release-branch>
      Required. Dedicated release branch name used in workspace and III submodules.

  --source <source-branch>
      Optional. Source integration branch to promote from. Default: develop.

  --target <target-branch>
      Optional. Target branch to promote into. Default: main.

  --yes
      Apply changes. Without --yes the script runs in dry-run mode.

EXAMPLES
  scripts/git/create_develop_to_main_prs.sh --release release/develop-to-main-2026-03
  scripts/git/create_develop_to_main_prs.sh --release release/develop-to-main-2026-03 --yes
USAGE
}

release_branch=""
source_branch="develop"
target_branch="main"
apply=0

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --release)
      release_branch="${2:-}"
      shift 2
      ;;
    --source)
      source_branch="${2:-}"
      shift 2
      ;;
    --target)
      target_branch="${2:-}"
      shift 2
      ;;
    --yes)
      apply=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$release_branch" ]]; then
  echo "ERROR: --release is required" >&2
  exit 1
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [[ -z "$current_branch" ]]; then
  echo "ERROR: workspace is detached HEAD; checkout '$source_branch' or '$release_branch' first" >&2
  exit 1
fi

if [[ "$current_branch" != "$source_branch" && "$current_branch" != "$release_branch" ]]; then
  echo "ERROR: workspace must be on '$source_branch' or '$release_branch' before running this helper" >&2
  echo "Current branch: $current_branch" >&2
  exit 1
fi

if [[ "$release_branch" == "$source_branch" || "$release_branch" == "$target_branch" ]]; then
  echo "ERROR: --release must be a dedicated branch distinct from source/target" >&2
  exit 1
fi

echo "Source branch: $source_branch"
echo "Target branch: $target_branch"
echo "Release branch: $release_branch"

sync_cmd=(./scripts/git/post_pr_sync.sh --base "$source_branch")
align_cmd=(./scripts/git/iii_branch_guard.sh align --base "$target_branch" --feature "$release_branch" --all-iii)
stack_cmd=(./scripts/git/create_stack_prs.sh --base "$target_branch" --feature "$release_branch" --all-iii)
if (( apply == 1 )); then
  sync_cmd+=(--yes)
  align_cmd+=(--yes)
  stack_cmd+=(--yes)
fi

echo
echo "Step 1/4: sync workspace and III submodules onto '$source_branch'"
"${sync_cmd[@]}"

echo
echo "Step 2/4: create or switch workspace release branch '$release_branch'"
if (( apply == 1 )); then
  if git rev-parse --verify --quiet "$release_branch" >/dev/null; then
    git switch "$release_branch"
  else
    git switch -c "$release_branch"
  fi
else
  if git rev-parse --verify --quiet "$release_branch" >/dev/null; then
    echo "DRY-RUN: git switch $release_branch"
  else
    echo "DRY-RUN: git switch -c $release_branch"
  fi
fi

echo
echo "Step 3/4: align all III submodules onto '$release_branch'"
"${align_cmd[@]}"

echo
echo "Step 4/4: create or update the coordinated PR stack into '$target_branch'"
"${stack_cmd[@]}"

echo
echo "Next after submodule PRs merge:"
echo "  ./scripts/git/refresh_workspace_submodule_pointers.sh --base $target_branch --feature $release_branch --all-iii --yes"
echo "  git commit -am \"chore(submodules): refresh pointers to origin/$target_branch\""
echo "  git push origin $release_branch"
