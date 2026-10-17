#!/usr/bin/env bash
# Unattended PI loop. Each turn is a fresh Claude session; memory lives in files.
#   run_pi.sh <project> [--once] [--interval SECONDS] [--max-turns N]
# Logs: <project>/state/pi_logs/<timestamp>.jsonl (full tool trace) + state/pi_runs.jsonl (one line per turn)
set -uo pipefail
PROJECT="$(readlink -f "${1:?usage: run_pi.sh <project> [--once] [--interval S] [--max-turns N]}")"; shift
ONCE=0; INTERVAL=900; MAXTURNS=80
while [ $# -gt 0 ]; do case "$1" in
  --once) ONCE=1 ;; --interval) INTERVAL="$2"; shift ;; --max-turns) MAXTURNS="$2"; shift ;;
  *) echo "unknown option $1"; exit 2 ;; esac; shift; done
source "$(dirname "$(readlink -f "$0")")/env.sh"
cd "$PROJECT"
# One runner owns the turn budget and integrity snapshot. Never steal a stale lock.
mkdir state/runner.lock 2>/dev/null || { echo "Runner lock exists; check the other runner before recovery"; exit 5; }
trap 'rmdir state/runner.lock' EXIT
if ! PYTHONPATH="$AUTOLAB_HOME/src" "$AUTOLAB_PYTHON" -c 'from autolab.config import Config; from pathlib import Path; import sys; sys.exit(0 if Config(Path.cwd())["agents"].get("sandbox") else 1)'; then
  echo "Unattended runs require [agents] sandbox = true and an available OS sandbox"; exit 5
fi
# Regenerate the current protection rules before handing control to an agent.
autolab guard snapshot >/dev/null || exit 5
autolab skills mount >/dev/null || exit 5
MODEL="$("$AUTOLAB_PYTHON" -c 'import tomllib;print(tomllib.load(open("autolab.toml","rb"))["models"]["pi"])')"
# The PI dispatches students in the background and reacts as each reports. By default `claude -p`
# stops background tasks 10 min after the PI's last message (seen: a student cut off before
# submitting its jobs); wait for them instead. PI_TURN_TIMEOUT still bounds the whole turn.
export CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS="${PI_BG_WAIT_MS:-0}"
# Untrusted folders silently drop project permissions (and possibly hooks): refuse to run unattended.
if ! PYTHONPATH="$AUTOLAB_HOME/src" "$AUTOLAB_PYTHON" -c 'import sys,pathlib; from autolab.project import claude_trusted; sys.exit(0 if claude_trusted(pathlib.Path(".")) else 1)'; then
  echo "This project is not trusted by Claude Code yet. Run \`claude\` once in $PROJECT, accept the trust prompt, exit, then retry."
  exit 3
fi
PROMPT='You are the PI of this autonomous lab. Run exactly ONE turn as CLAUDE.md describes: run `autolab tick`, read the digest it names, act on it (delegate execution to the student-* subagents), keep state/STATE.md current, then stop. Do not wait for running jobs; the next turn polls them. If 00_human/READY is missing or 00_human/STOP exists, only run `autolab tick` and stop.'
mkdir -p state/pi_logs
while true; do
  # Import authenticated human replies only between turns, before integrity snapshots.
  autolab weixin apply || { echo "Weixin reply import failed; stopping"; exit 5; }
  if [ -e 00_human/STOP ]; then echo "$(date -Is) STOP present"; [ $ONCE = 1 ] && exit 0; sleep "$INTERVAL"; continue; fi
  # stop before a turn the remaining budget could not cover (cap minus a turn reserve)
  if ! autolab budget check >/dev/null; then
    autolab human request --kind approval --blocking --dedupe "API budget nearly used up" "API budget nearly used up ($(autolab budget check | tr -d '\n ')). Raise [budget] total_usd in autolab.toml to continue." >/dev/null
    echo "$(date -Is) budget cap minus turn reserve reached; stopping"; exit 4
  fi
  # the turn itself may not spend more than what is left (claude -p --max-budget-usd)
  CAP="$(autolab budget turn-cap 2>/dev/null)"; CAPARG=()
  [ -n "$CAP" ] && CAPARG=(--max-budget-usd "$CAP")
  # skip the (paid) Claude turn when nothing changed; exit 3 = idle, anything else = wake
  autolab wake check --max-idle "${PI_MAX_IDLE_MIN:-45}" > state/pi_logs/wake-last.txt 2>&1; WRC=$?
  if [ "$WRC" = 3 ] && [ $ONCE = 0 ]; then
    echo "$(date -Is) idle tick: $(cat state/pi_logs/wake-last.txt)"; sleep "$INTERVAL"; continue
  fi
  TS="$(date +%Y%m%d-%H%M%S)"; LOG="state/pi_logs/$TS.jsonl"; T0=$(date +%s)
  # status on the shared file system: tmux sessions are only visible on the login node that owns them
  printf '{"state":"running","host":"%s","pid":%d,"started":"%s","log":"%s"}\n' "$(hostname)" $$ "$TS" "$LOG" > state/pi_status.json
  # Scheduling/wake-up tools keep a headless session alive after its result (seen in turn 5:
  # ScheduleWakeup 1800 s stalled the loop). One turn = one process that ends.
  # hard wall-clock limit per turn (default 60 min) so a stuck session cannot block the loop
  # SIGINT first: it ends the turn, while SIGTERM records no result (cost would be unknown)
  autolab guard snapshot >/dev/null || { echo "Integrity snapshot failed; stopping"; exit 5; }
  timeout --signal=INT --kill-after=120 "${PI_TURN_TIMEOUT:-3600}" \
    claude -p "$PROMPT" --model "$MODEL" --permission-mode acceptEdits --max-turns "$MAXTURNS" "${CAPARG[@]}" \
         --disallowedTools "ScheduleWakeup,CronCreate,CronDelete,CronList,Monitor,RemoteTrigger" \
         --output-format stream-json --verbose > "$LOG" 2> "state/pi_logs/$TS.err"
  RC=$?                                  # 124 = turn hit the time limit
  [ "$RC" = 124 ] && echo "$(date -Is) turn exceeded ${PI_TURN_TIMEOUT:-3600}s and was stopped" >> state/pi_logs/runner-loop.out
  autolab budget record "$LOG" >/dev/null || { echo "Cost recording failed; stopping"; touch 00_human/STOP; exit 5; }
  # protected files changed by a tool call this turn? (restores config, else STOP + ask the human)
  autolab guard check "$LOG" >> state/pi_logs/runner-loop.out 2>&1 || { echo "Integrity check failed; stopping"; touch 00_human/STOP; exit 5; }
  autolab wake mark >/dev/null 2>&1 || true
  printf '{"ts":"%s","rc":%d,"seconds":%d,"log":"%s","model":"%s"}\n' "$TS" "$RC" "$(( $(date +%s)-T0 ))" "$LOG" "$MODEL" >> state/pi_runs.jsonl
  printf '{"state":"idle","host":"%s","last_rc":%d,"last_log":"%s","finished":"%s"}\n' "$(hostname)" "$RC" "$LOG" "$(date -Is)" > state/pi_status.json
  echo "$(date -Is) PI turn rc=$RC log=$LOG"
  [ $ONCE = 1 ] && exit $RC
  sleep "$INTERVAL"
done
