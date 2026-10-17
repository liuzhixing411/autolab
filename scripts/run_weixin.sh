#!/usr/bin/env bash
# Run alongside run_pi.sh, on the same host/account and project filesystem.
set -euo pipefail
PROJECT="$(readlink -f "${1:?usage: run_weixin.sh <project>}")"
source "$(dirname "$(readlink -f "$0")")/env.sh"
cd "$PROJECT"
umask 077
exec autolab weixin listen
