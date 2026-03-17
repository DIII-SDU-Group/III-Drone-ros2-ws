#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  create_stack_prs.sh - create/update coordinated submodule + workspace PR stack

SYNOPSIS
  scripts/git/create_stack_prs.sh --base <base-branch> [--feature <feature-branch>] [--yes]
  scripts/git/create_stack_prs.sh -h | --help

DESCRIPTION
  Top-centric helper for a workspace branch workflow with III submodules.

  For affected III submodules (src/III-*, tools/III-*), this script:
  1) verifies branch consistency with iii_branch_guard.sh
  2) pushes the feature branch in each target submodule
  3) creates or updates a submodule PR: <feature> -> <base>
  4) stages submodule pointers in the workspace
  5) creates or updates a workspace PR: <feature> -> <base>
     with a checklist/table linking all submodule PRs.

  This is the main "push and update the stacked PR set" helper. Use it when a
  workspace feature branch also carries matching III submodule feature branches
  and you want GitHub PRs created or refreshed consistently.

REQUIREMENTS
  - gh CLI authenticated (gh auth status)
  - write permission to workspace and submodule remotes
  - clean enough branches for push (no unresolved divergence)

OPTIONS
  --base <base-branch>
      Required base branch (usually develop).

  --feature <feature-branch>
      Optional feature branch. Default: current workspace branch.

  --yes
      Apply mode. Without --yes the script runs in dry-run.

BEHAVIOR
  - Targets affected III submodules, detected as either:
    - locally changed submodule working tree, or
    - committed workspace gitlink change in <base>...HEAD.
  - Ignores changed non-III PX4-Autopilot (consistent with iii_branch_guard.sh).
  - Any other changed non-III submodule blocks execution.
  - For each target III submodule, requires actual commits on feature vs base.
    If feature exists but has no commits beyond base, the script fails and
    suggests running scripts/git/post_pr_sync.sh.

EXAMPLES
  scripts/git/create_stack_prs.sh --base develop --feature version-migration
  scripts/git/create_stack_prs.sh --base develop --feature version-migration --yes
USAGE
}

base_branch=""
feature_branch=""
apply=0

if [[ $# -eq 0 ]]; then
  usage
  exit 1
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --base)
      base_branch="${2:-}"
      shift 2
      ;;
    --feature)
      feature_branch="${2:-}"
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

if [[ -z "$base_branch" ]]; then
  echo "ERROR: --base is required" >&2
  exit 1
fi

root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [[ -z "$root" ]]; then
  echo "ERROR: not inside a git repository" >&2
  exit 1
fi
cd "$root"

if [[ -z "$feature_branch" ]]; then
  feature_branch="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
fi
if [[ -z "$feature_branch" ]]; then
  echo "ERROR: workspace is detached HEAD; pass --feature explicitly and checkout a branch" >&2
  exit 1
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: gh CLI is required" >&2
  exit 1
fi

if ! gh auth status >/dev/null 2>&1; then
  echo "ERROR: gh is not authenticated. Run: gh auth login" >&2
  exit 1
fi

# Policy + target detection in one place.
audit_out="$(mktemp)"
trap 'rm -f "$audit_out"' EXIT

if ! scripts/git/iii_branch_guard.sh audit --base "$base_branch" --feature "$feature_branch" >"$audit_out"; then
  cat "$audit_out"
  echo "ERROR: iii_branch_guard audit failed" >&2
  exit 1
fi

# Discover III submodules from .gitmodules.
mapfile -t iii_submodules < <(
  git config --file .gitmodules --get-regexp '^submodule\..*\.path$' \
    | awk '{print $2}' \
    | grep -E '^(src/III-|tools/III-)'
)

declare -A target_map=()

# 1) Locally changed III submodule worktrees.
for p in "${iii_submodules[@]}"; do
  [[ ! -d "$p" ]] && continue
  if [[ -n "$(git -C "$p" status --porcelain 2>/dev/null || true)" ]]; then
    target_map["$p"]=1
  fi
done

# 2) Committed workspace gitlink changes in base...HEAD.
for p in "${iii_submodules[@]}"; do
  if ! git diff --quiet "${base_branch}...HEAD" -- "$p"; then
    target_map["$p"]=1
  fi
done

if (( ${#target_map[@]} > 0 )); then
  mapfile -t targets < <(printf '%s\n' "${!target_map[@]}" | sed '/^$/d' | sort)
else
  targets=()
fi

workspace_only_mode=0
if (( ${#targets[@]} == 0 )); then
  workspace_only_mode=1
fi

# Early skip: if a submodule is already on base branch and clean, it cannot
# produce a submodule PR for the feature branch; skip it from the start.
filtered_targets=()
early_skipped_base_clean=()
for p in "${targets[@]}"; do
  [[ -z "$p" ]] && continue
  sub_branch="$(git -C "$p" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  sub_dirty="$(git -C "$p" status --porcelain 2>/dev/null || true)"
  if [[ "$sub_branch" == "$base_branch" && -z "$sub_dirty" ]]; then
    early_skipped_base_clean+=("$p")
    continue
  fi
  filtered_targets+=("$p")
done
targets=("${filtered_targets[@]}")

if (( ${#targets[@]} == 0 )); then
  workspace_only_mode=1
  echo "No actionable III submodules detected."
  if (( ${#early_skipped_base_clean[@]} > 0 )); then
    echo "Skipped from start (already on base branch and clean):"
    for p in "${early_skipped_base_clean[@]}"; do
      echo "  - $p"
    done
  fi
fi

echo "Workspace branch: $feature_branch"
echo "Base branch: $base_branch"
if (( workspace_only_mode == 1 )); then
  echo "Candidate III submodules (0)"
  echo "Workspace-only PR mode: enabled"
else
  echo "Candidate III submodules (${#targets[@]}):"
  for p in "${targets[@]}"; do
    echo "  - $p"
  done
fi
if (( ${#early_skipped_base_clean[@]} > 0 )); then
  echo "Skipped from start (already on base branch and clean):"
  for p in "${early_skipped_base_clean[@]}"; do
    echo "  - $p"
  done
fi

# Allowed submodule branch names follow the workspace branch stack:
# base -> ... -> feature (using top-level branch ancestry).
mapfile -t allowed_branches < <(
  git for-each-ref --format='%(refname:short)' refs/heads | while read -r b; do
    if git merge-base --is-ancestor "$base_branch" "$b" && git merge-base --is-ancestor "$b" "$feature_branch"; then
      echo "$b"
    fi
  done
)

echo "Allowed submodule branches from workspace stack:"
for b in "${allowed_branches[@]}"; do
  echo "  - $b"
done

workspace_repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

pr_rows=()
skipped_no_delta=()
skipped_branch_mismatch=()

upsert_pr() {
  local repo="$1"
  local head="$2"
  local base="$3"
  local title="$4"
  local body="$5"

  local existing
  existing="$(gh pr list --repo "$repo" --head "$head" --base "$base" --state open --json number,url -q '.[0].url' 2>/dev/null || true)"

  if [[ -n "$existing" && "$existing" != "null" ]]; then
    if (( apply == 1 )); then
      gh pr edit "$existing" --repo "$repo" --title "$title" --body "$body" >/dev/null
    fi
    echo "$existing"
    return
  fi

  if (( apply == 1 )); then
    gh pr create --repo "$repo" --head "$head" --base "$base" --title "$title" --body "$body"
  else
    echo "DRY-RUN: would create PR in $repo ($head -> $base)" >&2
    echo "https://github.com/$repo/pull/NEW"
  fi
}

# Submodule PRs
for p in "${targets[@]}"; do
  [[ -z "$p" ]] && continue
  echo
  echo "== $p =="
  sub_branch="$(git -C "$p" symbolic-ref --quiet --short HEAD 2>/dev/null || true)"
  if [[ -z "$sub_branch" ]]; then
    echo "WARN: $p is detached HEAD; skipping submodule PR for this repo." >&2
    skipped_branch_mismatch+=("$p")
    continue
  fi

  in_allowed=0
  for b in "${allowed_branches[@]}"; do
    if [[ "$sub_branch" == "$b" ]]; then
      in_allowed=1
      break
    fi
  done

  if (( in_allowed == 0 )); then
    echo "WARN: $p is on '$sub_branch' (outside allowed stack base->feature); skipping submodule PR for this repo." >&2
    skipped_branch_mismatch+=("$p")
    continue
  fi

  remote_url="$(git -C "$p" remote get-url origin)"
  repo_slug="$(printf '%s' "$remote_url" | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')"

  # Always refresh remote base first, then gate on local feature vs remote base.
  git -C "$p" fetch --no-tags origin "$base_branch" >/dev/null 2>&1
  if ! git -C "$p" rev-parse --verify --quiet "origin/$base_branch" >/dev/null; then
    echo "ERROR: $p missing origin/$base_branch; cannot validate PR delta." >&2
    exit 1
  fi

  # Require actual local feature commits beyond remote base before creating/updating PR.
  # This catches the common case where feature was already merged and remote feature branch deleted.
  delta_count="$(git -C "$p" rev-list --count "origin/$base_branch..$feature_branch" 2>/dev/null || echo 0)"
  if [[ "$delta_count" == "0" ]]; then
    echo "WARN: $p local '$feature_branch' has no commits beyond origin/$base_branch; skipping PR for this submodule." >&2
    echo "      Hint: ./scripts/git/post_pr_sync.sh --base $base_branch --clean-only --yes" >&2
    skipped_no_delta+=("$p")
    continue
  fi

  has_remote_feature=0
  if git -C "$p" ls-remote --exit-code --heads origin "$feature_branch" >/dev/null 2>&1; then
    has_remote_feature=1
  fi

  if (( has_remote_feature == 0 )); then
    if git -C "$p" rev-parse --verify --quiet "$feature_branch" >/dev/null; then
      if (( apply == 1 )); then
        git -C "$p" push -u origin "$feature_branch"
      else
        echo "DRY-RUN: would push $p:$feature_branch (remote branch missing)"
      fi
    else
      echo "ERROR: $p has no remote branch '$feature_branch' and no local branch to push." >&2
      echo "Create/switch first (or run align): scripts/git/iii_branch_guard.sh align --base $base_branch --feature $feature_branch --yes" >&2
      exit 1
    fi
  else
    echo "Remote branch exists for $p: origin/$feature_branch"
  fi

  sub_title="[${feature_branch}] ${p}: integration changes"
  sub_body="$(printf '%s\n\n- Source branch: `%s`\n- Target branch: `%s`\n- Submodule path in workspace: `%s`\n\n%s\n' \
    "Automated stacked PR from workspace **$workspace_repo**." \
    "$feature_branch" \
    "$base_branch" \
    "$p" \
    "This PR is part of a coordinated workspace integration stack.")"

  sub_pr_url="$(upsert_pr "$repo_slug" "$feature_branch" "$base_branch" "$sub_title" "$sub_body")"
  echo "Submodule PR: $sub_pr_url"

  sha="$(git -C "$p" rev-parse --short HEAD)"
  pr_rows+=("| $p | $sha | $sub_pr_url |")

  if (( apply == 1 )); then
    git add "$p"
  fi
done

if (( ${#skipped_no_delta[@]} > 0 )); then
  echo
  echo "Skipped III submodules with no feature-vs-base delta (${#skipped_no_delta[@]}):"
  for p in "${skipped_no_delta[@]}"; do
    echo "  - $p"
  done
fi

if (( ${#skipped_branch_mismatch[@]} > 0 )); then
  echo
  echo "Skipped III submodules not on workspace feature branch (${#skipped_branch_mismatch[@]}):"
  for p in "${skipped_branch_mismatch[@]}"; do
    echo "  - $p"
  done
fi

if (( ${#pr_rows[@]} == 0 )); then
  echo
  echo "No actionable III submodule PRs to create/update after filtering."
fi

workspace_body_file="$(mktemp)"
trap 'rm -f "$audit_out" "$workspace_body_file"' EXIT

{
  echo "Coordinated workspace integration PR."
  echo
  echo "- Source branch: \`$feature_branch\`"
  echo "- Target branch: \`$base_branch\`"
  echo
  echo "### III Submodule PRs"
  echo
  echo "| Submodule | SHA | PR |"
  echo "|---|---:|---|"
  for row in "${pr_rows[@]}"; do
    echo "$row"
  done
  echo
  echo "### Merge Rule"
  echo
  echo "Workspace PR must only merge after all listed submodule PRs are merged into \`$base_branch\`."
} > "$workspace_body_file"

if (( apply == 1 )); then
  git push -u origin "$feature_branch"
fi

ws_title="[$feature_branch] workspace integration"
ws_body="$(cat "$workspace_body_file")"
ws_pr_url="$(upsert_pr "$workspace_repo" "$feature_branch" "$base_branch" "$ws_title" "$ws_body")"

echo
if (( apply == 1 )); then
  echo "Workspace PR: $ws_pr_url"
  echo "Done."
else
  echo "DRY-RUN complete. Re-run with --yes to push and create/update PRs."
fi
