#!/usr/bin/env bash
# Publish the MeshMoe node installer + GGUF shards to a GitHub release.
#
# Run this from a machine that (a) has the build artifacts in
# client-dsh/dist/ and (b) can reach the GitHub API with a token that has
# `repo` scope (the PAT already used by this repo's origin URL works).
#
#   export GITHUB_TOKEN=<token>
#   ./deploy/publish-release.sh v1.0.0
#
# Why a script and not CI: the installer is ~735MB and the shards are 2.7GB;
# the Windows dev box cannot reach github.com directly, so this is meant to be
# run where the artifacts already live (or from siliconvalley after copying
# them there - note it only has ~8GB free).
set -euo pipefail

TAG="${1:-v1.0.0}"
REPO="${GITHUB_REPO:-MeshTok/meshmoe}"
DIST="${DIST:-client-dsh/dist}"
API="https://api.github.com/repos/$REPO"
: "${GITHUB_TOKEN:?set GITHUB_TOKEN (repo scope)}"

auth=(-H "Authorization: token $GITHUB_TOKEN" -H "Accept: application/vnd.github+json")

echo "== release $TAG on $REPO =="
rel_id=$(curl -sS "${auth[@]}" "$API/releases/tags/$TAG" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("id",""))' 2>/dev/null || true)
if [ -z "$rel_id" ]; then
  rel_id=$(curl -sS -X POST "${auth[@]}" "$API/releases" \
    -d "$(python3 - "$TAG" <<'PY'
import json,sys
print(json.dumps({
  "tag_name": sys.argv[1],
  "name": "MeshMoe Node " + sys.argv[1],
  "body": "Standalone mesh node: llama.cpp runtime + launchers + bundled Python.\n"
          "The 2.7 GB model ships as two GGUF shards; get them from this release "
          "or let ensure-model.cmd download them on first run.",
  "draft": False, "prerelease": False}))
PY
)" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
  echo "created release id=$rel_id"
else
  echo "release exists id=$rel_id"
fi

upload() {
  local f="$1" name; name=$(basename "$f")
  echo "uploading $name ($(du -h "$f" | cut -f1)) ..."
  curl -sS -X POST "${auth[@]}" \
    -H "Content-Type: application/octet-stream" \
    --data-binary @"$f" \
    "https://uploads.github.com/repos/$REPO/releases/$rel_id/assets?name=$name" \
    | python3 -c 'import sys,json;d=json.load(sys.stdin);print("  ->",d.get("state",d.get("message")))'
}

upload "$DIST/MeshMoe-Node-1.0.0-x64-setup.exe"
upload "$DIST/model-shards/MeshMoe-4B-00001-of-00002.gguf"
upload "$DIST/model-shards/MeshMoe-4B-00002-of-00002.gguf"
echo "== done: https://github.com/$REPO/releases/tag/$TAG =="
