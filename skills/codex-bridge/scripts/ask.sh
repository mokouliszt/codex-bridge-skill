#!/bin/sh
# codex-bridge runner
#
# usage:
#   ask.sh "prompt" <effort> [model]                    new session
#   ask.sh --continue "prompt" <effort> [model]         resume the most recent session
#   ask.sh --resume <session_id> "prompt" <effort> [model]
#                                                      resume a specific session (id: sessions.sh list)
#
# There is NO default model or effort. Both are required on every call (model via
# the 3rd argument or CODEX_MODEL). They must come from the user: either what the
# user explicitly specified in the conversation, or their answer when Claude asked
# them (models.sh choices gives the ranked list to offer). See SKILL.md.
#
# env:
#   CODEX_MODEL=<id>              model chosen/specified by the user (3rd argument wins)
#   CODEX_JSON=1                  emit JSONL events
#   CODEX_DRYRUN=1                print the final command instead of executing
#   CODEX_SEARCH=0                disable web search (default: enabled)
#   CODEX_ASYNC=1                 skip the inline wait entirely; return the job_id immediately
#                                  (use this up front for tasks you already expect to be long,
#                                  e.g. ultra-effort delegated work, to avoid burning the inline budget)
#   CODEX_INLINE_MAX_SECONDS=240  how long ask.sh blocks waiting before falling back to a
#                                  background job + job_id (default 240s)
#
# Long tasks and the sandbox:
#   codex keeps running inside THIS sandbox. If your assistant turn ends (no more tool
#   calls) while codex is still working, the sandbox is torn down and the job is lost --
#   this is not something any script can prevent by itself. If ask.sh falls back to
#   background mode (see below), keep calling wait.sh back-to-back, with no other reply
#   content, until it reports status=done. See SKILL.md for the full explanation.
#
# out:
#   finished inline  -> full codex CLI output on stdout (same as before) + /tmp/codex_last.md
#   still running    -> short "status=running" block + job_id on stdout, exit code 75
#                       -> continue with: wait.sh <job_id>
set -eu
umask 077

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"
. "$SKILL_DIR/scripts/_lib.sh"

# API key auth is forbidden by user policy: ChatGPT subscription only.
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

CONTINUE=0
RESUME_ID=""
if [ "${1:-}" = "--continue" ] || [ "${1:-}" = "-C" ]; then
  CONTINUE=1; shift
elif [ "${1:-}" = "--resume" ] || [ "${1:-}" = "-r" ]; then
  RESUME_ID="${2:?--resume requires a session id}"; shift 2
fi
[ $# -ge 1 ] || { echo "usage: ask.sh [--continue | --resume <id>] \"prompt\" <low|medium|high|xhigh|max|ultra> [model]"; exit 2; }

PROMPT="$1"
EFFORT="${2:-}"
MODEL="${3:-${CODEX_MODEL:-}}"

# No silent defaults: both values must be chosen by the user. Checked before
# lazy setup so a missing choice costs nothing (no npm install, no auth flow).
if [ -z "$MODEL" ] || [ -z "$EFFORT" ]; then
  echo "[ask] model and effort are required (got model='${MODEL:-<none>}' effort='${EFFORT:-<none>}')." >&2
  echo "[ask] If the user has not specified them, ask the user first:" >&2
  echo "[ask]   sh \"$SKILL_DIR/scripts/models.sh\" choices   # ranked strongest-first" >&2
  echo "[ask] then re-run: ask.sh [...] \"prompt\" <effort> <model>" >&2
  exit 2
fi
case "$EFFORT" in
  low|medium|high|xhigh|max|ultra) ;;
  *) echo "[ask] unsupported effort '$EFFORT'; use low|medium|high|xhigh|max|ultra" >&2; exit 2 ;;
esac

# lazy setup
if ! command -v codex >/dev/null 2>&1 || [ ! -f "$CODEX_HOME/auth.json" ]; then
  sh "$SKILL_DIR/scripts/setup.sh"
fi
# setup.sh may have been unable to produce an auth.json at all (fresh install / no
# bundled credentials, e.g. a public skill checkout) -- in that case it has already
# printed the login.py instructions. Don't attempt codex exec against no credentials;
# that just produces a confusing raw CLI error instead of the actionable message.
if [ ! -f "$CODEX_HOME/auth.json" ]; then
  echo "[ask] no auth.json yet -- finish the login flow printed above, then re-run ask.sh" >&2
  exit 3
fi

# Model and effort are always passed explicitly, including on resume, so a
# legacy config.toml, model_cache or the resumed session's own settings can
# never silently decide them.

# Collision-checked job dir: mkdir (no -p) fails if it already exists, so a
# same-second PID coincidence gets a retry instead of silently reusing/merging
# another job's directory.
mkdir -p "$CODEX_HOME/bg"
TRY=0
while :; do
  JOB_ID="$(date +%Y%m%d-%H%M%S)-$$-$TRY"
  JOB_DIR="$CODEX_HOME/bg/$JOB_ID"
  mkdir "$JOB_DIR" 2>/dev/null && break
  TRY=$((TRY + 1))
  [ "$TRY" -gt 20 ] && { echo "[ask] could not allocate a job dir after 20 tries" >&2; exit 1; }
done
codex_bridge_valid_job_id "$JOB_ID" || { echo "[ask] internal error: generated job_id '$JOB_ID' failed its own format check" >&2; exit 1; }

set -- --skip-git-repo-check \
       --dangerously-bypass-approvals-and-sandbox \
       -c "model_reasoning_effort=$EFFORT" \
       -o "$JOB_DIR/answer.md"
[ "${CODEX_SEARCH:-1}" = "0" ] && set -- "$@" -c tools.web_search=false
[ -n "$MODEL" ] && set -- "$@" -m "$MODEL"
[ "${CODEX_JSON:-0}" = "1" ] && set -- "$@" --json

[ "$CONTINUE" = "1" ] && set -- resume --last "$@"
[ -n "$RESUME_ID" ] && set -- resume "$RESUME_ID" "$@"

if [ "${CODEX_DRYRUN:-0}" = "1" ]; then
  echo "DRYRUN: codex exec $* \"<prompt>\""
  rmdir "$JOB_DIR" 2>/dev/null || true
  exit 0
fi

{
  echo "started_at=$(date +%s)"
  echo "prompt_head=$(printf '%s' "$PROMPT" | tr '\n' ' ' | cut -c1-60)"
  echo "effort=$EFFORT"
  echo "model=${MODEL:-default}"
  echo "cwd=$(pwd)"
} > "$JOB_DIR/meta"

# Launch in a fully detached session (new sid/pgid via setsid) so the job survives
# this one bash_tool call returning -- it still cannot survive the assistant's
# whole turn ending (sandbox teardown kills it regardless of setsid).
setsid sh -c '
  JOB_DIR="$1"; shift
  PROMPT="$1"; shift
  codex exec "$@" "$PROMPT" </dev/null
  ec=$?
  echo "$ec" > "$JOB_DIR/exit_code"
  : > "$JOB_DIR/done"
  exit "$ec"
' _bgrunner "$JOB_DIR" "$PROMPT" "$@" >"$JOB_DIR/log" 2>&1 &
BGPID=$!
echo "$BGPID" > "$JOB_DIR/pid"
_stat="$(cat "/proc/$BGPID/stat" 2>/dev/null)" || _stat=""
if [ -n "$_stat" ]; then
  printf '%s' "${_stat##*) }" | cut -d' ' -f20 > "$JOB_DIR/pid_start" 2>/dev/null || : > "$JOB_DIR/pid_start"
else
  : > "$JOB_DIR/pid_start"
fi

# Bounded inline wait: keeps the old synchronous feel for anything that finishes
# quickly, with zero change to how the caller reads the result.
INLINE_MAX="${CODEX_INLINE_MAX_SECONDS:-240}"
[ "${CODEX_ASYNC:-0}" = "1" ] && INLINE_MAX=0
POLL_INT=3
elapsed=0
while [ ! -f "$JOB_DIR/done" ] && [ "$elapsed" -lt "$INLINE_MAX" ]; do
  sleep "$POLL_INT"
  elapsed=$((elapsed + POLL_INT))
done

if [ -f "$JOB_DIR/done" ]; then
  EXITCODE=$(cat "$JOB_DIR/exit_code" 2>/dev/null || echo 1)
  if [ "$EXITCODE" = "0" ]; then
    cp "$JOB_DIR/answer.md" /tmp/codex_last.md 2>/dev/null || true
    cat "$JOB_DIR/log"
  else
    codex_bridge_report_failure "$JOB_DIR" "$EXITCODE"
  fi
  exit "$EXITCODE"
else
  cat <<EOF
[ask] still running after ${elapsed}s -- switching to background/poll mode
job_id: $JOB_ID

next:   sh "$SKILL_DIR/scripts/wait.sh" $JOB_ID

Do NOT end your reply/turn while this job is status=running -- the sandbox will be
torn down and the job lost with it. Keep calling wait.sh back-to-back (no filler
commentary in between) until it reports status=done, then read the answer path it
prints ($JOB_DIR/answer.md -- also mirrored to /tmp/codex_last.md, but the job-specific
path is the one to trust if you may have more than one job in flight at once).
EOF
  exit 75
fi
