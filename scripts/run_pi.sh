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
MODEL="$("$AUTOLAB_PYTHON" -c 'import tomllib;print(tomllib.load(open("autolab.toml","rb"))["models"]["pi"])')"
# Untrusted folders silently drop project permissions (and possibly hooks): refuse to run unattended.
if ! PYTHONPATH="$AUTOLAB_HOME/src" "$AUTOLAB_PYTHON" -c 'import sys,pathlib; from autolab.project import claude_trusted; sys.exit(0 if claude_trusted(pathlib.Path(".")) else 1)'; then
  echo "This project is not trusted by Claude Code yet. Run \`claude\` once in $PROJECT, accept the trust prompt, exit, then retry."
  exit 3
fi
PROMPT='You are the PI of this autonomous lab. Run exactly ONE turn as CLAUDE.md describes: run `autolab tick`, read the digest it names, act on it (delegate execution to the student-* subagents), keep state/STATE.md current, then stop. Do not wait for running jobs; the next turn polls them. If 00_human/READY is missing or 00_human/STOP exists, only run `autolab tick` and stop.'
mkdir -p state/pi_logs
while true; do
  if [ -e 00_human/STOP ]; then echo "$(date -Is) STOP present"; [ $ONCE = 1 ] && exit 0; sleep "$INTERVAL"; continue; fi
  if ! autolab budget status >/dev/null; then
    autolab human request --kind approval --blocking "API budget cap reached ($(autolab budget status | tr -d '\n ')). Raise [budget] total_usd in autolab.toml to continue." >/dev/null
    echo "$(date -Is) budget cap reached; stopping"; exit 4
  fi
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
  timeout --signal=TERM --kill-after=60 "${PI_TURN_TIMEOUT:-3600}" \
    claude -p "$PROMPT" --model "$MODEL" --permission-mode acceptEdits --max-turns "$MAXTURNS" \
         --disallowedTools "ScheduleWakeup,CronCreate,CronDelete,CronList,Monitor,RemoteTrigger" \
         --output-format stream-json --verbose > "$LOG" 2> "state/pi_logs/$TS.err"
  RC=$?                                  # 124 = turn hit the time limit
  [ "$RC" = 124 ] && echo "$(date -Is) turn exceeded ${PI_TURN_TIMEOUT:-3600}s and was stopped" >> state/pi_logs/runner-loop.out
  autolab budget record "$LOG" >/dev/null || true
  autolab wake mark >/dev/null 2>&1 || true
  printf '{"ts":"%s","rc":%d,"seconds":%d,"log":"%s","model":"%s"}\n' "$TS" "$RC" "$(( $(date +%s)-T0 ))" "$LOG" "$MODEL" >> state/pi_runs.jsonl
  printf '{"state":"idle","host":"%s","last_rc":%d,"last_log":"%s","finished":"%s"}\n' "$(hostname)" "$RC" "$LOG" "$(date -Is)" > state/pi_status.json
  echo "$(date -Is) PI turn rc=$RC log=$LOG"
  [ $ONCE = 1 ] && exit $RC
  sleep "$INTERVAL"
done
