# Source this before running Claude Code in an autolab project:
#   source ~/autolab/scripts/env.sh
# Puts a Python >= 3.11 and the `autolab` launcher first on PATH for this shell only.
_AL_HERE="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." && pwd)"
export AUTOLAB_HOME="$_AL_HERE"
export ARIS_REPO="${ARIS_REPO:-$_AL_HERE/third_party/aris}"
if [ -z "${AUTOLAB_PYTHON:-}" ]; then
  for c in /apps/external/apps/anaconda/2025.12/bin/python3 /apps/external/anaconda/2025.12/bin/python3 python3.13 python3.12 python3.11 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    "$p" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null && { AUTOLAB_PYTHON="$p"; break; }
  done
fi
export AUTOLAB_PYTHON
export PATH="$_AL_HERE/scripts:$(dirname "$AUTOLAB_PYTHON"):$PATH"
unset _AL_HERE
