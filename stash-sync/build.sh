#!/bin/bash
set -e

PLUGIN_ID="stash-sync"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VERSION=$(awk '/^version:/ { print $2; exit }' stash-sync.yml)
if [[ -z "$VERSION" ]]; then
    echo "Error: could not read version from stash-sync.yml" >&2
    exit 1
fi

PLUGIN_FILES=(stash-sync.yml stash-sync.py stash_sync_ui.js requirements.txt)

# ── Create zip with plugin files inside a stash-sync/ directory ──
rm -f "${PLUGIN_ID}.zip"
rm -rf _build

mkdir -p "_build/${PLUGIN_ID}"
for f in "${PLUGIN_FILES[@]}"; do
    cp "$f" "_build/${PLUGIN_ID}/"
done
(cd _build && zip -r "../${PLUGIN_ID}.zip" "${PLUGIN_ID}/")
rm -rf _build

# ── Compute sha256 ──
if command -v sha256sum &>/dev/null; then
    SHA=$(sha256sum "${PLUGIN_ID}.zip" | awk '{print $1}')
else
    SHA=$(shasum -a 256 "${PLUGIN_ID}.zip" | awk '{print $1}')
fi

DATE=$(date +"%Y-%m-%d %H:%M:%S")

# ── Update root index.yml (remove existing entry, append new one) ──
ROOT_INDEX="${SCRIPT_DIR}/../index.yml"
touch "$ROOT_INDEX"
python3 -c "
import re, sys
content = open('${ROOT_INDEX}').read()
content = re.sub(r'- id: ${PLUGIN_ID}\n(?:  [^\n]*\n)*', '', content)
open('${ROOT_INDEX}', 'w').write(content)
"
cat >> "$ROOT_INDEX" <<EOF
- id: ${PLUGIN_ID}
  name: Stash Sync
  version: ${VERSION}
  date: "${DATE}"
  path: stash-sync/${PLUGIN_ID}.zip
  sha256: ${SHA}
  metadata:
    description: Transfer scenes between two Stash instances with full metadata preservation
EOF

echo ""
echo "Built ${PLUGIN_ID}.zip  (sha256: ${SHA})"
echo ""
echo "Next steps:"
echo "  1. Commit and push:  git add -A && git commit -m 'build plugin package' && git push"
echo "  2. In each Stash instance, go to Settings > Plugins > Available Plugins > Add Source"
echo "  3. Enter the source URL (see below) and any local path name you like"
echo ""

# ── Print the source URL ──
REMOTE_URL=$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null || echo "")
if [[ -n "$REMOTE_URL" ]]; then
    REPO_PATH="${REMOTE_URL#git@github.com:}"
    REPO_PATH="${REPO_PATH#https://github.com/}"
    REPO_PATH="${REPO_PATH%.git}"
    BRANCH=$(git -C "$SCRIPT_DIR" branch --show-current 2>/dev/null || echo "main")
    echo "  Source URL:  https://raw.githubusercontent.com/${REPO_PATH}/${BRANCH}/index.yml"
else
    echo "  Source URL:  https://raw.githubusercontent.com/<you>/stash-plugins/<branch>/index.yml"
fi
echo ""
