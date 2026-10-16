#!/usr/bin/env bash
# One-shot check used by monitors (runs on any login node; reads only the shared file system).
#   watch_turn.sh <project> <target_turn_count>
# Prints one of:
#   DONE <last pi_runs.jsonl line>   the target number of turns has finished
#   STALLED <seconds>                a turn is running but its log has been idle > 20 min
#   LOOP_DOWN <runner tail>          runner silent longer than interval + 10 min, or its last line says it stopped
#   RUNNING <log age s> | WAITING <idle s>
set -uo pipefail
P="${1:?project}"; TARGET="${2:?target turn count}"; INTERVAL="${3:-900}"
cd "$P" || exit 2
n=$(wc -l < state/pi_runs.jsonl 2>/dev/null || echo 0)
if [ "$n" -ge "$TARGET" ]; then echo "DONE $(tail -1 state/pi_runs.jsonl)"; exit 0; fi
if tail -1 state/pi_logs/runner-loop.out 2>/dev/null | grep -qE "budget cap reached|STOP present"; then
  echo "LOOP_DOWN $(tail -2 state/pi_logs/runner-loop.out | tr '\n' ' ')"; exit 0
fi
if grep -q '"running"' state/pi_status.json 2>/dev/null; then
  L=$(ls -t state/pi_logs/2*.jsonl | head -1); age=$(( $(date +%s) - $(stat -c %Y "$L") ))
  [ "$age" -gt 1200 ] && echo "STALLED $age" || echo "RUNNING $age"
else
  # idle ticks (no PI turn needed) append to runner-loop.out without touching pi_status.json
  idle=$(( $(date +%s) - $(stat -c %Y state/pi_logs/runner-loop.out) ))
  [ "$idle" -gt $(( INTERVAL + 600 )) ] && echo "LOOP_DOWN runner silent ${idle}s" || echo "WAITING $idle ($(tail -1 state/pi_logs/runner-loop.out | cut -c1-60))"
fi
