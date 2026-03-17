#!/usr/bin/env bash
set -u

git fetch origin --prune --tags
git fetch upstream --prune --tags

had_conflict=0
tmpfile="$(mktemp)"

git for-each-ref --format='%(refname:short)' refs/remotes/upstream/ | grep -v '^upstream/HEAD$' > "$tmpfile"

while read -r upstream_ref; do
    branch="${upstream_ref#upstream/}"

    if ! git show-ref --verify --quiet "refs/remotes/origin/$branch"; then
        echo "[SKIP] $branch -> not present on origin"
        continue
    fi

    echo "[INFO] Processing branch: $branch"

    if git show-ref --verify --quiet "refs/heads/$branch"; then
        if ! git checkout "$branch" >/dev/null 2>&1; then
            echo "[ERROR] Could not checkout $branch"
            had_conflict=1
            continue
        fi
        if ! git reset --hard "origin/$branch" >/dev/null 2>&1; then
            echo "[ERROR] Could not reset $branch to origin/$branch"
            had_conflict=1
            continue
        fi
    else
        if ! git checkout -B "$branch" "origin/$branch" >/dev/null 2>&1; then
            echo "[ERROR] Could not create local branch $branch"
            had_conflict=1
            continue
        fi
    fi

    if git merge --no-ff --no-edit "upstream/$branch"; then
        echo "[OK] $branch merged cleanly"

        if git push origin "$branch"; then
            echo "[OK] $branch pushed to origin"
        else
            echo "[ERROR] Push failed for $branch"
            had_conflict=1
        fi
    else
        echo "[CONFLICT] Merge conflict on branch: $branch"
        had_conflict=1
        git merge --abort >/dev/null 2>&1 || true
    fi

    echo
done < "$tmpfile"

rm -f "$tmpfile"

if [ "$had_conflict" -eq 1 ]; then
    echo "Finished with one or more conflicts/errors."
    exit 1
else
    echo "Finished successfully with no merge conflicts."
    exit 0
fi
