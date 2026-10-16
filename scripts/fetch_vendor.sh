#!/usr/bin/env bash
# Re-create third_party/ (vendor clones) at the commits pinned in core/MANIFEST.tsv.
# Only third_party/aris/tools is needed at runtime (ARIS_REPO); the rest is for upgrades.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TP="$HERE/third_party"; mkdir -p "$TP"
clone() { # dir repo commit
  if [ ! -d "$TP/$1/.git" ]; then git -c core.longpaths=true clone --filter=blob:none --quiet "https://github.com/$2.git" "$TP/$1"; fi
  git -C "$TP/$1" fetch --quiet origin "$3" 2>/dev/null || true
  git -C "$TP/$1" -c advice.detachedHead=false checkout --quiet "$3"
  echo "$1 @ $(git -C "$TP/$1" rev-parse --short=12 HEAD)"
}
pin() { awk -F'\t' -v s="$1" '$2==s {print $3; exit}' "$HERE/core/MANIFEST.tsv"; }
clone aris wanshuiyin/Auto-claude-code-research-in-sleep "$(pin wanshuiyin/Auto-claude-code-research-in-sleep)"
if [ "${1:-}" = "--all" ]; then
  clone kdense K-Dense-AI/scientific-agent-skills "$(pin K-Dense-AI/scientific-agent-skills)"
  clone vercel-skills vercel-labs/skills "$(pin vercel-labs/skills)"
  clone anthropic-skills anthropics/skills "$(pin anthropics/skills)"
fi

# Pure-Python helpers the framework imports from third_party/pylib (no system install needed)
PY="${AUTOLAB_PYTHON:-python3}"
"$PY" -m pip install --quiet --upgrade --target "$TP/pylib" "pypdf>=6,<7" && echo "pylib: pypdf installed"
