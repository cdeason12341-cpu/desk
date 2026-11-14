#!/usr/bin/env bash
# Box-side publish: data/*.json -> secret gist -> Pages deploy pinned to that gist revision.
set -euo pipefail
cd "$(dirname "$0")"
G=$(cat .secrets/GIST_ID); R=cdeason12341-cpu/desk
for f in data/*.json; do gh gist edit "$G" -f "$(basename "$f")" "$f" >/dev/null; done
REV=$(gh api "gists/$G" --jq '.history[0].version')
gh workflow run publish.yml -R "$R" -f rev="$REV" >/dev/null
echo "dispatched rev ${REV:0:7}"
