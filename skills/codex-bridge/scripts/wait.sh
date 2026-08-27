#!/bin/sh
# codex-bridge background job poller
#
# usage:
#   wait.sh <job_id> [max_wait_seconds=60] [poll_interval_seconds=5]
#
# Call this repeatedly, back-to-back, whenever ask.sh falls back to background
# mode (job_id printed, exit code 75). Each call blocks *inside this one shell
# command* for up to max_wait_seconds using a real sleep loop -- Claude does not
# need to sleep or reason between polls, just re-invoke this script. This is
# what keeps Claude-side token consumption low: every poll is one short tool
# call and a one-line result, never the growing codex log.
#
# CRITICAL: do not end your assistant turn while status=running. The sandbox is
# torn down the moment there is no more in-flight tool call, and that kills the
# backgrounded codex process with it (this is the whole reason this script
# exists -- see SKILL.md). Keep polling until status=done or status=crashed.
#
# out (stdout, always short):
#   status=running total_elapsed=<n>s job_id=<id>                       -> call again
#   status=done exit_code=<n> answer=<job_dir>/answer.md total_elapsed=<n>s job_id=<id>
#   status=crashed exit_code=125 ...                                    -> process vanished, no exit_code was ever written (OOM/SIGKILL/reused PID)
#   status=unknown job_id=<id>                                          -> bad/expired id
#
# exit code mirrors the outcome: 0 on running or a clean (exit_code=0) finish,
# the job's own exit_code when it finished non-zero, 125 on a detected crash/
# orphan, 2 on an unknown/invalid job_id.
set -eu
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
. "$SKILL_DIR/scripts/_lib.sh"

JOB_ID="${1:?usage: wait.sh <job_id> [max_wait_seconds] [poll_interval_seconds]}"
MAX_WAIT="${2:-60}"
POLL_INT="${3:-5}"

case "$MAX_WAIT" in ''|*[!0-9]*) echo "[wait] max_wait_seconds must be a non-negative integer, got '$MAX_WAIT' -- using 60" >&2; MAX_WAIT=60 ;; esac
case "$POLL_INT" in ''|*[!0-9]*) echo "[wait] poll_interval_seconds must be a non-negative integer, got '$POLL_INT' -- using 5" >&2; POLL_INT=5 ;; esac
[ "$POLL_INT" -lt 1 ] && POLL_INT=1   # 0 would busy-loop

if ! codex_bridge_valid_job_id "$JOB_ID"; then
  echo "status=unknown job_id=$JOB_ID"
  echo "hint: job_id looks malformed, not just missing -- copy it verbatim from ask.sh's output"
  exit 2
fi
JOB_DIR="$CODEX_HOME/bg/$JOB_ID"

if [ ! -d "$JOB_DIR" ]; then
  echo "status=unknown job_id=$JOB_ID"
  echo "hint: sh scripts/jobs.sh list"
  exit 2
fi

STARTED_AT=$(sed -n 's/^started_at=//p' "$JOB_DIR/meta" 2>/dev/null || echo "")
NOW=$(date +%s)
if [ -n "$STARTED_AT" ]; then TOTAL_ELAPSED=$((NOW - STARTED_AT)); else TOTAL_ELAPSED="?"; fi

waited=0
while [ ! -f "$JOB_DIR/done" ] && [ "$waited" -lt "$MAX_WAIT" ]; do
  if ! codex_bridge_job_alive "$JOB_DIR"; then
    codex_bridge_mark_orphan "$JOB_DIR"
    break
  fi
  sleep "$POLL_INT"
  waited=$((waited + POLL_INT))
  NOW=$(date +%s)
  [ -n "$STARTED_AT" ] && TOTAL_ELAPSED=$((NOW - STARTED_AT))
done

if [ -f "$JOB_DIR/done" ]; then
  EXITCODE=$(cat "$JOB_DIR/exit_code" 2>/dev/null || echo 1)
  cp "$JOB_DIR/answer.md" /tmp/codex_last.md 2>/dev/null || cp "$JOB_DIR/log" /tmp/codex_last.md 2>/dev/null || true
  if [ "$EXITCODE" = "0" ]; then
    echo "status=done exit_code=0 answer=$JOB_DIR/answer.md (also copied to /tmp/codex_last.md) total_elapsed=${TOTAL_ELAPSED}s job_id=$JOB_ID"
    exit 0
  elif [ "$EXITCODE" = "125" ]; then
    echo "status=crashed exit_code=125 total_elapsed=${TOTAL_ELAPSED}s job_id=$JOB_ID"
    echo "--- log tail ---"; tail -n 15 "$JOB_DIR/log" 2>/dev/null || true
    exit 125
  else
    echo "status=done exit_code=$EXITCODE answer=$JOB_DIR/answer.md total_elapsed=${TOTAL_ELAPSED}s job_id=$JOB_ID"
    codex_bridge_report_failure "$JOB_DIR" "$EXITCODE"
    exit "$EXITCODE"
  fi
else
  echo "status=running total_elapsed=${TOTAL_ELAPSED}s job_id=$JOB_ID"
  exit 0
fi
