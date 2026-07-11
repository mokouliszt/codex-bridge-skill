#!/bin/sh
# codex-bridge runner
#
# usage:
#   ask.sh "prompt" [effort] [model]      effort: xhigh(default) | max | ultra
#   ask.sh --continue "prompt"            resume the most recent session
#   ask.sh --resume <session_id> "prompt" resume a specific session (id: sessions.sh list)
#
# env:
#   CODEX_MODEL=<id>   model override (else: model_cache -> config default)
#   CODEX_JSON=1       emit JSONL events
#   CODEX_DRYRUN=1     print the final command instead of executing
#   CODEX_SEARCH=0     disable web search (default: enabled)
#
# out: final answer -> stdout and /tmp/codex_last.md
set -eu

SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
export CODEX_HOME="${CODEX_HOME:-/home/claude/.codex-bridge}"

# API key auth is forbidden by user policy: ChatGPT subscription only.
unset OPENAI_API_KEY CODEX_API_KEY 2>/dev/null || true

CONTINUE=0
RESUME_ID=""
if [ "${1:-}" = "--continue" ] || [ "${1:-}" = "-C" ]; then
  CONTINUE=1; shift
elif [ "${1:-}" = "--resume" ] || [ "${1:-}" = "-r" ]; then
  RESUME_ID="${2:?--resume requires a session id}"; shift 2
fi
[ $# -ge 1 ] || { echo "usage: ask.sh [--continue | --resume <id>] \"prompt\" [xhigh|max|ultra] [model]"; exit 2; }

# lazy setup
if ! command -v codex >/dev/null 2>&1 || [ ! -f "$CODEX_HOME/auth.json" ]; then
  sh "$SKILL_DIR/scripts/setup.sh"
fi

PROMPT="$1"
EFFORT="${2:-xhigh}"
MODEL="${3:-${CODEX_MODEL:-}}"
[ -z "$MODEL" ] && [ -f "$CODEX_HOME/model_cache" ] && MODEL=$(cat "$CODEX_HOME/model_cache")

# policy: never below xhigh
case "$EFFORT" in
  xhigh|max|ultra) ;;
  *) echo "[ask] effort '$EFFORT' is below policy floor -> clamped to xhigh" >&2; EFFORT="xhigh" ;;
esac

set -- --skip-git-repo-check \
       --dangerously-bypass-approvals-and-sandbox \
       -c "model_reasoning_effort=$EFFORT" \
       -o /tmp/codex_last.md
[ "${CODEX_SEARCH:-1}" = "0" ] && set -- "$@" -c tools.web_search=false
[ -n "$MODEL" ] && set -- "$@" -m "$MODEL"
[ "${CODEX_JSON:-0}" = "1" ] && set -- "$@" --json

[ "$CONTINUE" = "1" ] && set -- resume --last "$@"
[ -n "$RESUME_ID" ] && set -- resume "$RESUME_ID" "$@"

if [ "${CODEX_DRYRUN:-0}" = "1" ]; then
  echo "DRYRUN: codex exec $* \"<prompt>\""
  exit 0
fi

codex exec "$@" "$PROMPT" </dev/null
