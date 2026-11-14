#!/usr/bin/env bash
# Manual publish of the last encrypted bundle (the desk process publishes on its own; see publisher.py).
# data/_bundle.json -> secret gist (bundle.json) -> Pages deploy pinned to that gist revision.
set -euo pipefail
cd "$(dirname "$0")"
G=$(cat .secrets/GIST_ID); R=cdeason12341-cpu/desk
python3 -c 'import json,sys; print(json.dumps({"files":{"bundle.json":{"content":open("data/_bundle.json").read()}}}))' > data/_patch.json
REV=$(gh api -X PATCH "gists/$G" --input data/_patch.json --jq '.history[0].version'); rm -f data/_patch.json
gh workflow run publish.yml -R "$R" -f rev="$REV" >/dev/null
echo "dispatched rev ${REV:0:7}"
