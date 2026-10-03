#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
NAME
  create_stack_prs.sh - create/update coordinated submodule + workspace PR stack

SYNOPSIS
  scripts/git/create_stack_prs.sh --base <base-branch> [--feature <feature-branch>] [--operation-id <id>] [--all-iii] [--yes]
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
      Required base branch: develop for feature work or main for promotion.

  --feature <feature-branch>
      Optional feature branch. Default: current workspace branch.

  --operation-id <id>
      Stable lowercase/digit/hyphen operation identifier retained in PR handoff.
      Default: a deterministic stack-<feature> identifier.

  --all-iii
      Target all III submodules instead of only changed ones.

  --yes
      Apply mode. Without --yes the script runs in dry-run.

BEHAVIOR
  - By default, targets affected III submodules, detected as either:
    - locally changed submodule working tree, or
    - committed workspace gitlink change in <base>...<feature>.
  - With --all-iii, targets all III submodules.
  - Ignores changed non-III PX4-Autopilot (consistent with iii_branch_guard.sh).
  - Any other changed non-III submodule blocks execution.
  - For each target III submodule, requires actual commits on feature vs base.
    If feature exists but has no commits beyond base, the script fails and
    suggests running scripts/git/post_pr_sync.sh.

EXAMPLES
  scripts/git/create_stack_prs.sh --base develop --feature version-migration
  scripts/git/create_stack_prs.sh --base develop --feature version-migration --yes
  scripts/git/create_stack_prs.sh --base main --feature promote/develop-to-main/2026-08 --all-iii --yes
USAGE
}

base_branch=""
feature_branch=""
operation_id=""
all_iii=0
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
    --operation-id)
      operation_id="${2:-}"
      shift 2
      ;;
    --all-iii)
      all_iii=1
      shift
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
if [[ -z "$operation_id" ]]; then
  operation_id="stack-$(printf '%s' "$feature_branch" | tr '[:upper:]_/' '[:lower:]--' | sed -E 's/[^a-z0-9-]+/-/g; s/-+/-/g; s/^-//; s/-$//' | cut -c1-57)"
fi
if [[ ! "$operation_id" =~ ^[a-z0-9][a-z0-9-]{7,63}$ ]]; then
  echo "ERROR: --operation-id must match [a-z0-9][a-z0-9-]{7,63}" >&2
  exit 1
fi
if [[ -z "$feature_branch" ]]; then
  echo "ERROR: workspace is detached HEAD; pass --feature explicitly and checkout a branch" >&2
  exit 1
fi
if [[ "$base_branch" != "develop" && "$base_branch" != "main" ]]; then
  echo "ERROR: stacked PR target must be 'develop' or 'main'" >&2
  exit 1
fi
if [[ "$base_branch" == "main" && ! "$feature_branch" =~ ^promote/develop-to-main/[a-z0-9][a-z0-9._-]*$ ]]; then
  echo "ERROR: main PR branches must match promote/develop-to-main/<id>" >&2
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
plan_output="${audit_out}.stack-plan.json"
trap 'rm -f "$audit_out" "$plan_output"' EXIT

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

if (( all_iii == 1 )); then
  for p in "${iii_submodules[@]}"; do
    target_map["$p"]=1
  done
else
  # 1) Locally changed III submodule worktrees.
  for p in "${iii_submodules[@]}"; do
    [[ ! -d "$p" ]] && continue
    if [[ -n "$(git -C "$p" status --porcelain 2>/dev/null || true)" ]]; then
      target_map["$p"]=1
    fi
  done

  # 2) Committed workspace gitlink changes in base...feature.
  for p in "${iii_submodules[@]}"; do
    if ! git diff --quiet "${base_branch}...${feature_branch}" -- "$p"; then
      target_map["$p"]=1
    fi
  done
fi

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
echo "Operation ID: $operation_id"
target_mode="changed only"
if (( all_iii == 1 )); then
  target_mode="all III"
fi
echo "Target mode: $target_mode"
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

plan_state_root="${XDG_STATE_HOME:-$HOME/.local/state}/iii/automation-plans"
plan_arguments=(
  --base "$base_branch"
  --feature "$feature_branch"
  --operation-id "$operation_id"
  --state-root "$plan_state_root"
)
for p in "${targets[@]}"; do
  plan_arguments+=(--target "$p")
done
if (( apply == 1 )); then
  plan_arguments+=(--verify-existing)
fi
python3 scripts/git/create_stack_plan.py "${plan_arguments[@]}" >"$plan_output"
plan_id="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$plan_output")"
echo "Retained automation plan: $plan_state_root/$operation_id.plan.json"
echo "Retained plan ID: $plan_id"

workspace_repo="$(gh repo view --json nameWithOwner -q .nameWithOwner)"

planned_expected_old_sha() {
  python3 - "$plan_output" "$1" <<'PY'
import json
import sys

plan = json.load(open(sys.argv[1], encoding="utf-8"))
matches = [
    row for row in plan["repositories"] if row["repository"] == sys.argv[2]
]
if len(matches) != 1:
    raise SystemExit(f"retained plan does not contain exactly one repository row for {sys.argv[2]}")
print(matches[0]["expected_old_sha"] or "__MISSING__")
PY
}

assert_remote_matches_plan() {
  local repo="$1"
  local current_sha="$2"
  local expected_sha
  expected_sha="$(planned_expected_old_sha "$repo")"
  if [[ -z "$current_sha" ]]; then
    current_sha="__MISSING__"
  fi
  if [[ "$current_sha" != "$expected_sha" ]]; then
    echo "ERROR: stale retained plan for $repo:$feature_branch; expected $expected_sha, observed $current_sha" >&2
    echo "Re-run dry-run planning before any mutation." >&2
    exit 1
  fi
}

pr_rows=()
pr_markers=()
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

  local_feature_sha="$(git -C "$p" rev-parse "$feature_branch")"
  remote_feature_sha="$(git -C "$p" ls-remote --heads origin "refs/heads/$feature_branch" | awk 'NR == 1 {print $1}')"
  assert_remote_matches_plan "$repo_slug" "$remote_feature_sha"
  has_remote_feature=0
  if [[ -n "$remote_feature_sha" ]]; then
    has_remote_feature=1
  fi

  if (( has_remote_feature == 0 )); then
    if git -C "$p" rev-parse --verify --quiet "$feature_branch" >/dev/null; then
      if (( apply == 1 )); then
        git -C "$p" push -u \
          --force-with-lease="refs/heads/$feature_branch:" \
          origin "$local_feature_sha:refs/heads/$feature_branch"
      else
        echo "DRY-RUN: would create $p:$feature_branch with an exact missing-ref lease"
      fi
    else
      echo "ERROR: $p has no remote branch '$feature_branch' and no local branch to push." >&2
      echo "Create/switch first (or run align): scripts/git/iii_branch_guard.sh align --base $base_branch --feature $feature_branch --yes" >&2
      exit 1
    fi
  elif [[ "$remote_feature_sha" != "$local_feature_sha" ]]; then
    if ! git -C "$p" merge-base --is-ancestor "$remote_feature_sha" "$local_feature_sha"; then
      echo "ERROR: $p remote feature head is not an ancestor of the retained local head; refusing rewrite." >&2
      exit 1
    fi
    if (( apply == 1 )); then
      git -C "$p" push \
        --force-with-lease="refs/heads/$feature_branch:$remote_feature_sha" \
        origin "$local_feature_sha:refs/heads/$feature_branch"
    else
      echo "DRY-RUN: would update $p:$feature_branch from $remote_feature_sha to $local_feature_sha with an exact lease"
    fi
  else
    echo "Remote branch already matches $p:$feature_branch at $local_feature_sha"
  fi

  sub_title="[${feature_branch}] ${p}: integration changes"
  # Backticks are literal Markdown delimiters in this printf template.
  # shellcheck disable=SC2016
  sub_body="$(printf '%s\n\n- Operation ID: `%s`\n- Retained plan ID: `%s`\n- Source branch: `%s`\n- Source SHA: `%s`\n- Target branch: `%s`\n- Submodule path in workspace: `%s`\n\n%s\n\n%s\n' \
    "Automated stacked PR from workspace **$workspace_repo**." \
    "$operation_id" \
    "$plan_id" \
    "$feature_branch" \
    "$local_feature_sha" \
    "$base_branch" \
    "$p" \
    "This PR is part of a coordinated workspace integration stack." \
    "<!-- iii-pr-transport-v1; authenticate refs and checks through GitHub APIs -->")"

  sub_pr_url="$(upsert_pr "$repo_slug" "$feature_branch" "$base_branch" "$sub_title" "$sub_body")"
  echo "Submodule PR: $sub_pr_url"

  sha="$(git -C "$p" rev-parse --short HEAD)"
  pr_rows+=("| $p | $sha | $sub_pr_url |")
  pr_markers+=("<!-- iii-submodule-pr: path=$p url=$sub_pr_url -->")

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
trap 'rm -f "$audit_out" "$plan_output" "$workspace_body_file"' EXIT

{
  echo "Coordinated workspace integration PR."
  echo
  echo "- Source branch: \`$feature_branch\`"
  echo "- Target branch: \`$base_branch\`"
  echo "- Operation ID: \`$operation_id\`"
  echo "- Retained plan ID: \`$plan_id\`"
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
  echo
  echo "### Pointer Refresh Rule"
  echo
  echo "After the submodule PRs merge, refresh this workspace branch so every III gitlink points at the latest \`origin/$base_branch\` head:"
  echo
  echo "\`\`\`bash"
  echo "./scripts/git/refresh_workspace_submodule_pointers.sh --base $base_branch --feature $feature_branch --yes"
  echo "\`\`\`"
  if (( ${#pr_markers[@]} > 0 )); then
    echo
    for marker in "${pr_markers[@]}"; do
      echo "$marker"
    done
  fi
} > "$workspace_body_file"

workspace_local_sha="$(git rev-parse "$feature_branch")"
workspace_remote_sha="$(git ls-remote --heads origin "refs/heads/$feature_branch" | awk 'NR == 1 {print $1}')"
assert_remote_matches_plan "$workspace_repo" "$workspace_remote_sha"
if [[ -n "$workspace_remote_sha" && "$workspace_remote_sha" != "$workspace_local_sha" ]] \
  && ! git merge-base --is-ancestor "$workspace_remote_sha" "$workspace_local_sha"; then
  echo "ERROR: workspace remote feature head is not an ancestor of the retained local head; refusing rewrite." >&2
  exit 1
fi
if (( apply == 1 )) && [[ "$workspace_remote_sha" != "$workspace_local_sha" ]]; then
  git push -u \
    --force-with-lease="refs/heads/$feature_branch:$workspace_remote_sha" \
    origin "$workspace_local_sha:refs/heads/$feature_branch"
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
