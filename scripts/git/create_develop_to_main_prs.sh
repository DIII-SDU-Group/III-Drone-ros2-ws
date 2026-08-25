#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  create_develop_to_main_prs.sh - prepare or refresh a verified develop-to-main promotion stack

SYNOPSIS
  scripts/git/create_develop_to_main_prs.sh --promotion-id <id> [--phase prepare|refresh] [--yes]
  scripts/git/create_develop_to_main_prs.sh -h | --help

DESCRIPTION
  Coordinates the only supported editable-III promotion into main. The fixed
  source is develop, the fixed target is main, and the mechanical branch is
  promote/develop-to-main/<id> in the workspace and all editable III repos.

  prepare:
    sync develop, create/switch the promotion branches, and create/update the
    linked submodule and workspace PR stack.

  refresh:
    after every linked submodule PR has merged into main, refresh workspace
    gitlinks to the exact origin/main heads, update the dependency lock, verify
    the mechanical-diff contract, commit if needed, and push the promotion branch.

  Both phases are dry-run unless --yes is supplied. A dry-run prints the exact
  commands without switching branches, fetching, pushing, committing, or editing
  pull requests.

OPTIONS
  --promotion-id <id>
      Required stable operation identifier. Allowed: lowercase letters, digits,
      dot, underscore, and hyphen; the first character must be alphanumeric.

  --phase prepare|refresh
      Optional lifecycle phase. Default: prepare.

  --yes
      Apply the selected phase. Without this flag the command is read-only.

EXAMPLES
  scripts/git/create_develop_to_main_prs.sh --promotion-id 2026-08-qualification
  scripts/git/create_develop_to_main_prs.sh --promotion-id 2026-08-qualification --yes
  scripts/git/create_develop_to_main_prs.sh --promotion-id 2026-08-qualification --phase refresh --yes
USAGE
}

promotion_id=""
phase="prepare"
apply=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --promotion-id) promotion_id="${2:-}"; shift 2 ;;
    --phase) phase="${2:-}"; shift 2 ;;
    --yes) apply=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage; exit 1 ;;
  esac
done

if [[ ! "$promotion_id" =~ ^[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "ERROR: --promotion-id must match [a-z0-9][a-z0-9._-]*" >&2
  exit 1
fi
if [[ "$phase" != "prepare" && "$phase" != "refresh" ]]; then
  echo "ERROR: --phase must be 'prepare' or 'refresh'" >&2
  exit 1
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

source_branch="develop"
target_branch="main"
promotion_branch="promote/develop-to-main/$promotion_id"

echo "Promotion phase: $phase"
echo "Source branch: $source_branch"
echo "Target branch: $target_branch"
echo "Mechanical branch: $promotion_branch"

print_command() {
  printf '  '
  printf '%q ' "$@"
  printf '\n'
}

if (( apply == 0 )); then
  echo "DRY-RUN plan:"
  if [[ "$phase" == "prepare" ]]; then
    print_command ./scripts/git/post_pr_sync.sh --base "$source_branch" --yes
    print_command git switch -c "$promotion_branch"
    print_command ./scripts/git/iii_branch_guard.sh align --base "$target_branch" --feature "$promotion_branch" --all-iii --yes
    print_command ./scripts/git/create_stack_prs.sh --base "$target_branch" --feature "$promotion_branch" --all-iii --yes
  else
    print_command git switch "$promotion_branch"
    print_command ./scripts/git/refresh_workspace_submodule_pointers.sh --base "$target_branch" --feature "$promotion_branch" --all-iii --yes
    print_command ./scripts/git/verify_submodule_lock.sh
    print_command python scripts/ci/verify_promotion_source.py --phase source --base main --head "$promotion_branch" --base-sha origin/main --head-sha HEAD --develop-ref origin/develop --json
    print_command git commit -m "chore(submodules): refresh promotion pointers to origin/main"
    print_command git push origin "$promotion_branch"
  fi
  echo "DRY-RUN complete. Re-run with --yes to apply."
  exit 0
fi

if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "ERROR: tracked workspace changes must be committed before promotion automation" >&2
  exit 2
fi

if [[ "$phase" == "prepare" ]]; then
  ./scripts/git/post_pr_sync.sh --base "$source_branch" --yes
  git fetch --no-tags origin "$source_branch" "$target_branch"
  git branch --force "$target_branch" "origin/$target_branch"
  mapfile -t iii_submodules < <(
    git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
      | awk '{print $2}' \
      | grep -E '^(src/III-|tools/III-)'
  )
  for path in "${iii_submodules[@]}"; do
    git -C "$path" fetch --no-tags origin "$target_branch"
    git -C "$path" branch --force "$target_branch" "origin/$target_branch"
  done
  if git show-ref --verify --quiet "refs/heads/$promotion_branch"; then
    git switch "$promotion_branch"
    git merge --ff-only "origin/$source_branch"
  else
    git switch -c "$promotion_branch" "origin/$source_branch"
  fi
  ./scripts/git/iii_branch_guard.sh align \
    --base "$target_branch" --feature "$promotion_branch" --all-iii --yes
  ./scripts/git/create_stack_prs.sh \
    --base "$target_branch" --feature "$promotion_branch" --all-iii --yes
  echo
  echo "Prepare complete. Merge every linked submodule PR into main, then run:"
  echo "  $0 --promotion-id $promotion_id --phase refresh --yes"
  exit 0
fi

current_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
if [[ "$current_branch" != "$promotion_branch" ]]; then
  if ! git show-ref --verify --quiet "refs/heads/$promotion_branch"; then
    echo "ERROR: local promotion branch does not exist: $promotion_branch" >&2
    exit 1
  fi
  git switch "$promotion_branch"
fi

git fetch --no-tags origin "$source_branch" "$target_branch"
./scripts/git/refresh_workspace_submodule_pointers.sh \
  --base "$target_branch" --feature "$promotion_branch" --all-iii --yes
./scripts/git/verify_submodule_lock.sh

PYTHONPATH=deployment/src python scripts/ci/verify_promotion_source.py \
  --phase source \
  --base "$target_branch" \
  --head "$promotion_branch" \
  --base-sha "origin/$target_branch" \
  --head-sha HEAD \
  --develop-ref "origin/$source_branch" \
  --json

if ! git diff --cached --quiet; then
  git commit -m "chore(submodules): refresh promotion pointers to origin/main"
  git push origin "$promotion_branch"
  echo "Promotion pointers and lock committed and pushed."
else
  echo "Promotion refresh is already current; no commit or push needed."
fi
